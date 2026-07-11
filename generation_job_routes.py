"""FastAPI routes and orchestration for durable generation jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from generation_job_adapters import ProviderAdapterError, adapter_for, provider_for_model
from generation_job_models import (
    GenerationJobCreate,
    GenerationJobResponse,
    JobError,
    JobResult,
    ProviderStatus,
    TERMINAL_STATUSES,
    safe_client_metadata,
)
from generation_job_repository import repository
from generation_job_scheduler import enqueue_poll, next_poll_time


logger = logging.getLogger("ai_gateway.generation_jobs")
router = APIRouter(tags=["generation-jobs"])


def _owner(user: UserAPIKeyAuth) -> tuple[str, dict[str, Any]]:
    raw_identity = next(
        (
            str(value)
            for value in (
                getattr(user, "api_key", None),
                getattr(user, "token", None),
                getattr(user, "key_name", None),
                getattr(user, "user_id", None),
            )
            if value
        ),
        "anonymous",
    )
    owner_hash = hashlib.sha256(raw_identity.encode()).hexdigest()
    context = {
        key: value
        for key, value in {
            "key_alias": getattr(user, "key_alias", None),
            "user_id": getattr(user, "user_id", None),
            "team_id": getattr(user, "team_id", None),
            "project_id": getattr(user, "project_id", None),
        }.items()
        if value is not None
    }
    context["api_key_hash"] = owner_hash
    return owner_hash, context


def _hash_request(payload: GenerationJobCreate, upload_bytes: dict[str, tuple[str, bytes, str]]) -> str:
    canonical = payload.model_dump(mode="json", exclude_none=False)
    canonical["uploads"] = {
        name: {
            "filename": value[0],
            "mime_type": value[2],
            "sha256": hashlib.sha256(value[1]).hexdigest(),
        }
        for name, value in sorted(upload_bytes.items())
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _parse_request(request: Request) -> tuple[GenerationJobCreate, dict[str, tuple[str, bytes, str]]]:
    content_type = request.headers.get("content-type", "")
    uploads: dict[str, tuple[str, bytes, str]] = {}
    if content_type.startswith("application/json"):
        raw = await request.json()
    elif content_type.startswith("multipart/form-data"):
        limit = int(os.environ.get("GENERATION_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
        form = await request.form(max_files=10, max_fields=5, max_part_size=limit)
        raw_payload = form.get("request") or form.get("payload")
        if not isinstance(raw_payload, str):
            raise HTTPException(422, "Multipart requests require a JSON 'request' field.")
        try:
            raw = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "Multipart request JSON is invalid.") from exc
        total = 0
        for name, item in form.multi_items():
            if isinstance(item, (UploadFile, StarletteUploadFile)):
                data = await item.read(limit + 1)
                total += len(data)
                if len(data) > limit or total > limit:
                    raise HTTPException(413, "Generation media upload is too large.")
                uploads[name] = (item.filename or name, data, item.content_type or "application/octet-stream")
    else:
        raise HTTPException(415, "Use application/json or multipart/form-data.")
    try:
        return GenerationJobCreate.model_validate(raw), uploads
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors()) from exc


def _public_status(status: str) -> str:
    return "queued" if status == "submitting" else status


def _response(job: dict[str, Any], base_url: str) -> GenerationJobResponse:
    status = _public_status(str(job["status"]))
    next_poll = job.get("next_poll_at")
    poll_after_ms = None
    if status not in TERMINAL_STATUSES:
        if next_poll:
            poll_after_ms = max(1000, int((next_poll - datetime.now(timezone.utc)).total_seconds() * 1000))
        else:
            poll_after_ms = 5000
    error = None
    if job.get("error_code"):
        error = JobError(
            code=str(job["error_code"]),
            message=str(job.get("error_message") or "Generation failed."),
            retryable=bool(job.get("error_retryable")),
        )
    result = None
    if status == "completed":
        result = JobResult(
            content_url=f"{base_url.rstrip('/')}/v1/generation-jobs/{job['id']}/content",
            mime_type=job.get("result_mime_type") or "video/mp4",
        )
    return GenerationJobResponse(
        id=job["id"],
        modality=job["modality"],
        model=job["model"],
        status=status,
        progress=float(job["progress"]) if job.get("progress") is not None else None,
        provider_request_id=job.get("provider_request_id"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        poll_after_ms=poll_after_ms,
        result=result,
        usage=job.get("usage"),
        cost_usd=float(job["response_cost_usd"]) if job.get("response_cost_usd") is not None else None,
        error=error,
    )


async def _record_spend(job: dict[str, Any]) -> None:
    """Feed terminal usage through LiteLLM callbacks, guarded by the job row lock."""
    if job.get("response_cost_usd") is None:
        return

    async def emit(row: dict[str, Any]) -> None:
        from litellm.litellm_core_utils.litellm_logging import Logging
        from litellm.types.utils import ImageObject, ImageResponse

        now = datetime.now(timezone.utc)
        response = ImageResponse(created=int(now.timestamp()), data=[ImageObject(url="gateway://generation-job")])
        response._hidden_params["response_cost"] = float(row["response_cost_usd"])
        identity = row.get("owner_context") or {}
        logging_obj = Logging(
            model=row["model"],
            messages=[{"role": "user", "content": "[durable generation job]"}],
            stream=False,
            call_type="image_generation",
            start_time=row.get("submitted_at") or row["created_at"],
            litellm_call_id=row["id"],
            function_id=row["id"],
            kwargs={
                "model": row["model"],
                "litellm_call_id": row["id"],
                "user": identity.get("user_id"),
                "api_key": identity.get("api_key_hash"),
                "team_id": identity.get("team_id"),
                "metadata": {"generation_job_id": row["id"], "provider": row["provider"]},
                "response_cost": float(row["response_cost_usd"]),
            },
        )
        await logging_obj.async_success_handler(
            result=response,
            start_time=row.get("submitted_at") or row["created_at"],
            end_time=row.get("completed_at") or now,
        )

    try:
        await repository.log_spend_once(job["id"], emit)
    except Exception:
        logger.exception("generation_job_spend_logging_failed", extra={"generation_job_id": job["id"]})


async def _schedule(job_id: str, when: datetime) -> None:
    await repository.schedule_next(job_id, when)
    try:
        queued = await enqueue_poll(job_id, when)
        if not queued:
            logger.info("generation_poll_queue_not_configured", extra={"generation_job_id": job_id})
    except Exception:
        logger.exception("generation_poll_enqueue_failed", extra={"generation_job_id": job_id})


@router.post("/v1/generation-jobs", response_model=GenerationJobResponse, status_code=202)
async def create_generation_job(
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
    user: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> GenerationJobResponse:
    payload, uploads = await _parse_request(request)
    try:
        provider = provider_for_model(payload.model)
    except ProviderAdapterError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc
    owner_hash, owner_context = _owner(user)
    request_hash = _hash_request(payload, uploads)
    job_id = f"gen_{uuid4().hex}"
    callback_token = secrets.token_urlsafe(32) if provider == "byteplus" else None
    callback_hash = hashlib.sha256(callback_token.encode()).hexdigest() if callback_token else None
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(os.environ.get("GENERATION_JOB_MAX_AGE_SECONDS", "7200")))
    )
    job, created, conflict = await repository.create_or_get(
        job_id=job_id,
        owner_key_hash=owner_hash,
        owner_context=owner_context,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        modality=payload.modality,
        model=payload.model,
        provider=provider,
        request_metadata={"client_metadata": safe_client_metadata(payload.metadata)},
        deadline_at=deadline,
        callback_token_hash=callback_hash,
    )
    if conflict:
        raise HTTPException(
            409,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "Idempotency-Key was already used with a different request.",
            },
        )
    response.headers["Location"] = f"/v1/generation-jobs/{job['id']}"
    if not created:
        return _response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))

    callback_base = (os.environ.get("GENERATION_CALLBACK_BASE_URL") or "").rstrip("/")
    callback_url = None
    if callback_token and callback_base:
        callback_url = f"{callback_base}/callbacks/byteplus/{job_id}?token={callback_token}"
    try:
        submitted = await adapter_for(provider).submit(
            payload, job_id=job_id, callback_url=callback_url, upload_bytes=uploads
        )
        first_poll = next_poll_time(0)
        job = await repository.mark_submitted(
            job_id,
            provider_request_id=submitted.provider_request_id,
            provider_status=submitted.provider_status,
            progress=submitted.progress,
            request_metadata=submitted.request_metadata,
            next_poll_at=first_poll,
        )
        try:
            await enqueue_poll(job_id, first_poll)
        except Exception:
            logger.exception("generation_poll_enqueue_failed", extra={"generation_job_id": job_id})
    except ProviderAdapterError as exc:
        code = "SUBMISSION_OUTCOME_UNKNOWN" if exc.outcome_unknown else exc.code
        job = await repository.mark_submission_failed(job_id, code=code, message=str(exc))
    except Exception as exc:
        logger.exception("generation_submission_unhandled", extra={"generation_job_id": job_id})
        job = await repository.mark_submission_failed(
            job_id,
            code="SUBMISSION_OUTCOME_UNKNOWN",
            message="The provider submission outcome could not be determined.",
        )
    return _response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))


@router.get("/v1/generation-jobs/{job_id}", response_model=GenerationJobResponse)
async def get_generation_job(
    job_id: str,
    request: Request,
    user: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> GenerationJobResponse:
    owner_hash, _ = _owner(user)
    job = await repository.get(job_id, owner_hash)
    if not job:
        raise HTTPException(404, "Generation job not found.")
    return _response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))


async def _remote_content(url: str, incoming_range: Optional[str], maximum: int):
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=60), follow_redirects=True)
    headers = {"Range": incoming_range} if incoming_range else {}
    response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    if response.status_code >= 400:
        status_code = response.status_code
        await response.aclose()
        await client.aclose()
        if status_code in {401, 403, 404, 410}:
            raise ProviderAdapterError(
                "The provider content URL expired.", code="CONTENT_URL_EXPIRED", retryable=True
            )
        raise HTTPException(502, "Provider content is not currently available.")
    length = response.headers.get("content-length")
    if length and int(length) > maximum:
        await response.aclose()
        await client.aclose()
        raise HTTPException(413, "Generated content exceeds the gateway size limit.")

    async def body():
        seen = 0
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                seen += len(chunk)
                if seen > maximum:
                    raise RuntimeError("generated content exceeds configured limit")
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    forwarded = {
        key: value
        for key in ("content-length", "content-range", "accept-ranges", "etag", "last-modified")
        if (value := response.headers.get(key))
    }
    return body(), response.status_code, response.headers.get("content-type", "video/mp4"), forwarded


@router.get("/v1/generation-jobs/{job_id}/content")
async def get_generation_job_content(
    job_id: str,
    request: Request,
    user: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    owner_hash, _ = _owner(user)
    job = await repository.get(job_id, owner_hash)
    if not job:
        raise HTTPException(404, "Generation job not found.")
    if job["status"] != "completed":
        raise HTTPException(409, detail={"code": "CONTENT_NOT_READY", "status": _public_status(job["status"])})
    try:
        source = await adapter_for(job["provider"]).content(job)
    except ProviderAdapterError as exc:
        raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
    maximum = int(os.environ.get("GENERATION_MAX_CONTENT_BYTES", str(2 * 1024 * 1024 * 1024)))
    headers = {"Content-Disposition": f'attachment; filename="{job_id}.mp4"'}
    if source.url:
        try:
            body, status, mime, forwarded = await _remote_content(
                source.url, request.headers.get("range"), maximum
            )
        except ProviderAdapterError as exc:
            if exc.code != "CONTENT_URL_EXPIRED" or job["provider"] == "vertex":
                raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
            try:
                refreshed = await adapter_for(job["provider"]).retrieve(job)
            except ProviderAdapterError as refresh_error:
                raise HTTPException(
                    502, detail={"code": refresh_error.code, "message": str(refresh_error)}
                ) from refresh_error
            if refreshed.status != "completed" or not refreshed.result_url:
                raise HTTPException(502, detail={"code": "CONTENT_URL_EXPIRED", "message": str(exc)}) from exc
            job = await repository.refresh_result_url(
                job_id, refreshed.result_url, refreshed.result_mime_type
            )
            body, status, mime, forwarded = await _remote_content(
                refreshed.result_url, request.headers.get("range"), maximum
            )
        headers.update(forwarded)
        return StreamingResponse(body, status_code=status, media_type=mime, headers=headers)
    content = source.content or b""
    if len(content) > maximum:
        raise HTTPException(413, "Generated content exceeds the gateway size limit.")

    async def chunks():
        view = memoryview(content)
        for offset in range(0, len(view), 1024 * 1024):
            yield view[offset : offset + 1024 * 1024]
            await asyncio.sleep(0)

    return StreamingResponse(chunks(), media_type=source.mime_type, headers=headers)


async def _verify_internal(request: Request) -> None:
    configured = os.environ.get("GENERATION_INTERNAL_SECRET")
    supplied = request.headers.get("X-Gateway-Internal-Secret", "")
    if configured and hmac.compare_digest(configured, supplied):
        return
    authorization = request.headers.get("authorization", "")
    expected_email = os.environ.get("GENERATION_POLL_SERVICE_ACCOUNT_EMAIL")
    audience = os.environ.get("GENERATION_POLL_AUDIENCE") or os.environ.get("GENERATION_POLL_TARGET_URL")
    if authorization.startswith("Bearer ") and expected_email and audience:
        token = authorization.removeprefix("Bearer ").strip()
        try:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import id_token

            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token, token, GoogleAuthRequest(), audience
            )
            if claims.get("email") == expected_email and claims.get("email_verified", True):
                return
        except Exception:
            logger.warning("generation_internal_oidc_rejected")
    if not configured and not os.environ.get("K_SERVICE"):
        return
    raise HTTPException(401, "Invalid internal task authentication.")


@router.post("/internal/generation-jobs/{job_id}/poll", include_in_schema=False)
async def poll_generation_job(job_id: str, request: Request) -> dict[str, Any]:
    await _verify_internal(request)
    job = await repository.get(job_id)
    if job and job["status"] in TERMINAL_STATUSES:
        await _record_spend(job)
    if not job or job["status"] in TERMINAL_STATUSES:
        return {"accepted": True, "terminal": bool(job)}
    if not job.get("provider_request_id"):
        age = datetime.now(timezone.utc) - job["created_at"]
        if age > timedelta(seconds=60):
            await repository.mark_submission_failed(
                job_id,
                code="SUBMISSION_OUTCOME_UNKNOWN",
                message="The gateway restarted before a provider request ID was durably recorded.",
            )
        return {"accepted": True, "terminal": age > timedelta(seconds=60)}
    deadline_reached = datetime.now(timezone.utc) >= job["deadline_at"]
    try:
        provider_status = await adapter_for(job["provider"]).retrieve(job)
        job = await repository.apply_provider_status(job_id, provider_status)
    except ProviderAdapterError as exc:
        if not exc.retryable:
            provider_status = ProviderStatus(
                status="failed", provider_status=job.get("provider_status") or "unknown",
                error_code=exc.code, error_message=str(exc), error_retryable=False,
            )
            job = await repository.apply_provider_status(job_id, provider_status)
        elif deadline_reached:
            job = await repository.mark_expired(job_id)
        else:
            when = next_poll_time(int(job.get("consecutive_poll_errors") or 0) + 1)
            job = await repository.record_poll_error(job_id, message=str(exc), next_poll_at=when)
            try:
                await enqueue_poll(job_id, when)
            except Exception:
                logger.exception("generation_poll_enqueue_failed", extra={"generation_job_id": job_id})
            return {"accepted": True, "status": job["status"]}
    if deadline_reached and job["status"] not in TERMINAL_STATUSES:
        job = await repository.mark_expired(job_id)
    if job["status"] in TERMINAL_STATUSES:
        await _record_spend(job)
        return {"accepted": True, "status": job["status"], "terminal": True}
    when = next_poll_time(int(job.get("poll_attempts") or 0))
    await _schedule(job_id, when)
    return {"accepted": True, "status": job["status"], "terminal": False}


@router.post("/internal/generation-jobs/reconcile", include_in_schema=False)
async def reconcile_generation_jobs(request: Request) -> dict[str, int]:
    await _verify_internal(request)
    jobs = await repository.due_jobs(limit=int(os.environ.get("GENERATION_RECONCILE_BATCH", "100")))
    enqueued = 0
    for job in jobs:
        if job["status"] == "submitting":
            if datetime.now(timezone.utc) - job["created_at"] > timedelta(seconds=60):
                await repository.mark_submission_failed(
                    job["id"], code="SUBMISSION_OUTCOME_UNKNOWN",
                    message="The provider submission did not durably record a request ID.",
                )
            continue
        try:
            if await enqueue_poll(job["id"], datetime.now(timezone.utc)):
                enqueued += 1
        except Exception:
            logger.exception("generation_reconcile_enqueue_failed", extra={"generation_job_id": job["id"]})
    spend_jobs = await repository.jobs_needing_spend(
        limit=int(os.environ.get("GENERATION_RECONCILE_BATCH", "100"))
    )
    for spend_job in spend_jobs:
        await _record_spend(spend_job)
    return {"due": len(jobs), "enqueued": enqueued, "spend_reconciled": len(spend_jobs)}


@router.post("/internal/generation-jobs/cleanup", include_in_schema=False)
async def cleanup_generation_jobs(request: Request) -> dict[str, int]:
    await _verify_internal(request)
    return {"deleted": await repository.cleanup(int(os.environ.get("GENERATION_RETENTION_DAYS", "30")))}
