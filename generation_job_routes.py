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

from generation_job_adapters import (
    ADAPTER_REVISIONS,
    ProviderAdapterError,
    adapter_for,
    adapter_for_job,
    is_gemini_omni_model,
    provider_for_model,
    route_for,
)
from grok_video_contract import validate_grok_video_v2, ADAPTER_REVISION as GROK15_ADAPTER_REVISION
from seedance_video_contract import MODELS as SEEDANCE20_MODELS, validate_seedance_video_v2, ADAPTER_REVISION as SEEDANCE20_ADAPTER_REVISION

from generation_job_models import (
    GenerationJobCreate,
    GenerationJobCreateV2,
    GenerationJobResponse,
    JobError,
    JobResult,
    ProviderStatus,
    TERMINAL_STATUSES,
    safe_client_metadata,
)
from generation_job_repository import repository
from generation_job_scheduler import enqueue_poll, next_poll_time
from cost_accounting import accounting, canonical_key, identity_for
from pricing_registry import registry, PricingError


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
    context.update(identity_for(user))
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


def _hash_request_v2(payload: GenerationJobCreateV2, upload_bytes: dict[str, tuple[str, bytes, str]]) -> str:
    document = payload.model_dump(mode="json", exclude_none=False)
    for item in document["media"]:
        if item.get("timestamp_seconds") is None:
            item.pop("timestamp_seconds", None)  # Preserve pre-keyframe V2 hashes.
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    uploads = json.dumps(
        sorted(
            [[name, hashlib.sha256(value[1]).hexdigest(), value[2]] for name, value in upload_bytes.items()]
        ),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{canonical}\n{uploads}".encode()).hexdigest()
    return f"gj2:{digest}"


def _peek_schema_version(raw: Any) -> Optional[int]:
    if not isinstance(raw, dict):
        return None
    if "request_schema_version" not in raw:
        return None
    value = raw.get("request_schema_version")
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, detail={"code": "UNSUPPORTED_REQUEST_SCHEMA", "message": "request_schema_version is invalid."})
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            422, detail={"code": "UNSUPPORTED_REQUEST_SCHEMA", "message": "request_schema_version is invalid."}
        ) from exc


async def _parse_request(
    request: Request,
) -> tuple[GenerationJobCreate | GenerationJobCreateV2, dict[str, tuple[str, bytes, str]]]:
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
    schema_version = _peek_schema_version(raw)
    if schema_version is None:
        try:
            return GenerationJobCreate.model_validate(raw), uploads
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors()) from exc
    if schema_version != 2:
        raise HTTPException(
            422,
            detail={"code": "UNSUPPORTED_REQUEST_SCHEMA", "message": f"Unsupported request schema {schema_version}."},
        )
    try:
        payload = GenerationJobCreateV2.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors()) from exc
    return payload, uploads


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
        outputs=[{key: value for key, value in output.items() if key != "url"} | {
            "content_url": f"{base_url.rstrip('/')}/v1/generation-jobs/{job['id']}/outputs/{index}"
        } for index, output in enumerate((job.get("request_metadata") or {}).get("outputs", []))],
        generation={key: value for key, value in (job.get("request_metadata") or {}).items()
                    if key in {"draft", "duration_seconds", "actual_duration_seconds", "resolution", "has_input_video", "operation", "contract_revision", "profile_id", "audio_mode"}},
        usage=job.get("usage"),
        cost_usd=float(job["cost_contract"]["cost_usd"]) if (job.get("cost_contract") or {}).get("cost_usd") is not None else None,
        accounting_id=job.get("accounting_id"),
        cost_status=(job.get("cost_contract") or {}).get("cost_status", "pending"),
        cost_source=(job.get("cost_contract") or {}).get("cost_source"),
        pricing_version=(job.get("cost_contract") or {}).get("pricing_version"),
        breakdown=(job.get("cost_contract") or {}).get("breakdown", []),
        billing_eligible=(job.get("cost_contract") or {}).get("billing_eligible", False),
        error=error,
    )


async def _record_spend(job: dict[str, Any]) -> None:
    """Record the execution regardless of attribution or pricing verification."""
    try:
        if not job.get("accounting_id"):
            await accounting.recover_job(job)
            return
        metadata = job.get("request_metadata") or {}
        served_options = {key: metadata[key] for key in ("resolution", "generate_audio") if key in metadata}
        if "has_input_video" in metadata:
            served_options["input_video"] = metadata["has_input_video"]
        pool = await accounting.pool()
        attempt_id = await pool.fetchval("select attempt_id from gateway_cost_attempts where accounting_id=$1 order by created_at limit 1", job["accounting_id"])
        await accounting.observe(
            attempt_id or job["accounting_id"] + ":1", raw_usage=job.get("usage"),
            served_model=metadata.get("served_model") or metadata.get("upstream_model"),
            provider_request_id=job.get("provider_request_id"),
            outcome="success" if job["status"] == "completed" else "failure",
            served_options=served_options or None,
        )
        await accounting.finish(job["accounting_id"])
    except Exception:
        logger.exception("generation_job_spend_logging_failed", extra={"generation_job_id": job["id"]})


async def _cost_response(job, base_url):
    if job.get("accounting_id"):
        job["cost_contract"] = await accounting.get(job["accounting_id"])
    return _response(job, base_url)


async def _schedule(job_id: str, when: datetime) -> None:
    await repository.schedule_next(job_id, when)
    try:
        queued = await enqueue_poll(job_id, when)
        if not queued:
            logger.info("generation_poll_queue_not_configured", extra={"generation_job_id": job_id})
    except Exception:
        logger.exception("generation_poll_enqueue_failed", extra={"generation_job_id": job_id})


def _is_compatible_omni_previous_job(requested_model: str, previous: Optional[dict[str, Any]]) -> bool:
    """Allow same-owner continuation across the original and 1.1 aliases."""
    return bool(
        is_gemini_omni_model(requested_model)
        and previous
        and previous.get("status") == "completed"
        and previous.get("provider") == "vertex"
        and is_gemini_omni_model(str(previous.get("model") or ""))
        and previous.get("provider_request_id")
    )


@router.post("/v1/generation-jobs", response_model=GenerationJobResponse, status_code=202)
async def create_generation_job(
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
    user: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> GenerationJobResponse:
    payload, uploads = await _parse_request(request)
    schema_version = 2 if isinstance(payload, GenerationJobCreateV2) else 1
    from video_capabilities import REVISION as EXPANDED_REVISION, validate as validate_expanded
    expanded = schema_version == 2 and payload.contract_revision == EXPANDED_REVISION
    if expanded:
        try:
            validate_expanded(payload)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_VIDEO_CONTRACT", "message": str(exc)}) from exc
    if schema_version == 2 and not expanded and payload.model == "grok-video-1.5":
        try:
            validate_grok_video_v2(payload)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_VIDEO_CONTRACT", "message": str(exc)}) from exc
    if schema_version == 2 and not expanded and payload.model in SEEDANCE20_MODELS:
        try:
            validate_seedance_video_v2(payload)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_VIDEO_CONTRACT", "message": str(exc)}) from exc
    try:
        provider = provider_for_model(payload.model)
        provider_route = route_for(payload.model, schema_version, payload.contract_revision if expanded else None)
    except ProviderAdapterError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc
    owner_hash, owner_context = _owner(user)
    if payload.previous_job_id and expanded and payload.model == "seedance-2.5":
        previous = await repository.get(payload.previous_job_id, owner_hash)
        if not previous or previous.get("model") != payload.model or previous.get("status") != "completed" or not (previous.get("request_metadata") or {}).get("draft"):
            raise HTTPException(422, detail={"code": "INVALID_PREVIOUS_JOB", "message": "Choose your completed Seedance 2.5 draft job."})
        payload._previous_provider_id = previous["provider_request_id"]
        payload._previous_metadata = previous.get("request_metadata") or {}
    elif payload.previous_job_id:
        if provider != "vertex" or not is_gemini_omni_model(payload.model):
            raise HTTPException(
                422,
                detail={
                    "code": "INVALID_PREVIOUS_JOB",
                    "message": "previous_job_id is supported only for Gemini Omni jobs.",
                },
            )
        previous = await repository.get(payload.previous_job_id, owner_hash)
        if not _is_compatible_omni_previous_job(payload.model, previous):
            raise HTTPException(
                422,
                detail={
                    "code": "INVALID_PREVIOUS_JOB",
                    "message": "previous_job_id must identify your completed Gemini Omni job.",
                },
            )
        payload._previous_interaction_id = str(previous["provider_request_id"])
    request_hash = (
        _hash_request_v2(payload, uploads)
        if isinstance(payload, GenerationJobCreateV2)
        else _hash_request(payload, uploads)
    )
    job_id = f"gen_{uuid4().hex}"
    callback_token = secrets.token_urlsafe(32) if provider == "byteplus" else None
    callback_hash = hashlib.sha256(callback_token.encode()).hexdigest() if callback_token else None
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(os.environ.get("GENERATION_JOB_MAX_AGE_SECONDS", "7200")))
    )
    metadata = {
        "client_metadata": safe_client_metadata(payload.metadata),
        "operation": payload.operation,
        "previous_job_id": payload.previous_job_id,
    }
    if isinstance(payload, GenerationJobCreateV2):
        metadata["profile_id"] = payload.profile_id
        metadata["contract_revision"] = payload.contract_revision
        metadata["reference_voice_ids"] = payload.voice_ids
    else:
        metadata["reference_voice_ids"] = payload.reference_voice_ids
    job, created, conflict = await repository.create_or_get(
        job_id=job_id,
        owner_key_hash=owner_hash,
        owner_context=owner_context,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        modality="video",
        model=payload.model,
        provider=provider,
        request_metadata=metadata,
        deadline_at=deadline,
        callback_token_hash=callback_hash,
        request_schema_version=schema_version,
        provider_route=provider_route,
        adapter_revision=f"{provider_route}@2026-09-24" if expanded else GROK15_ADAPTER_REVISION if schema_version == 2 and payload.model == "grok-video-1.5" else SEEDANCE20_ADAPTER_REVISION if schema_version == 2 and payload.model in SEEDANCE20_MODELS else ADAPTER_REVISIONS.get(provider_route, f"{provider_route}@2026-09-03"),
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
        return await _cost_response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))

    from gateway_accounting import check_context, options_for, state
    try:
        billing_options = options_for(payload.model_dump())
        if expanded and payload.profile_id == "generate.from_draft":
            billing_options.update(resolution="1080p", input_video=bool(payload._previous_metadata.get("has_input_video")))
        profile = registry().select(payload.model, "generation_job", billing_options)
        check_context(profile)
        await accounting.check_budgets(owner_context, payload.model)
        accounting_id = await accounting.begin(model=payload.model, route="generation_job",
            identity=owner_context, owner_hash=owner_hash, accounting_id=job_id)
        await accounting.attempt(accounting_id, profile, attempt_id=accounting_id + ":1")
        pool = await repository.pool()
        await pool.execute("update gateway_generation_jobs set accounting_id=$2 where id=$1", job_id, accounting_id)
        job["accounting_id"] = accounting_id
        current = state.get()
        if current is not None:
            current.update(accounting_id=accounting_id, durable_job=True, profile=profile, alias=payload.model)
    except PricingError as exc:
        await repository.mark_submission_failed(job_id, code=exc.code, message=str(exc))
        raise HTTPException(503, detail={"code": exc.code, "message": str(exc)}) from exc

    callback_base = (os.environ.get("GENERATION_CALLBACK_BASE_URL") or "").rstrip("/")
    callback_url = None
    if callback_token and callback_base:
        callback_url = f"{callback_base}/callbacks/byteplus/{job_id}?token={callback_token}"
    try:
        submitted = await adapter_for_job({"provider": provider, "provider_route": provider_route}).submit(
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
        job = await repository.mark_submission_failed(
            job_id,
            code=code,
            message=str(exc),
            retryable=exc.retryable and not exc.outcome_unknown,
            usage=exc.usage,
        )
    except Exception as exc:
        logger.exception("generation_submission_unhandled", extra={"generation_job_id": job_id})
        job = await repository.mark_submission_failed(
            job_id,
            code="SUBMISSION_OUTCOME_UNKNOWN",
            message="The provider submission outcome could not be determined.",
        )
    if job["status"] in TERMINAL_STATUSES:
        await _record_spend(job)
    return await _cost_response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))


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
    return await _cost_response(job, os.environ.get("GATEWAY_PUBLIC_BASE_URL") or str(request.base_url))


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


@router.get("/v1/generation-jobs/{job_id}/outputs/{output_index}")
async def get_generation_job_output(job_id: str, output_index: int, request: Request,
                                    user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    owner_hash, _ = _owner(user)
    job = await repository.get(job_id, owner_hash)
    if not job:
        raise HTTPException(404, "Generation job not found")
    outputs = (job.get("request_metadata") or {}).get("outputs", [])
    if job["status"] != "completed" or not 0 <= output_index < len(outputs):
        raise HTTPException(404, "Generation output not found")
    output = outputs[output_index]
    maximum = int(os.environ.get("GENERATION_MAX_CONTENT_BYTES", str(2 * 1024 * 1024 * 1024)))
    try:
        if output["url"].startswith(("gateway:", "gs://")):
            source = await adapter_for_job(job).content({**job, "result_url": output["url"]})
            return Response(content=source.content, media_type=source.mime_type)
        body, status, mime, headers = await _remote_content(output["url"], request.headers.get("range"), maximum)
    except ProviderAdapterError as exc:
        if exc.code != "CONTENT_URL_EXPIRED":
            raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
        refreshed = await adapter_for_job(job).retrieve(job)
        renewed = (refreshed.result_metadata or {}).get("outputs") or []
        if refreshed.status != "completed" or output_index >= len(renewed):
            raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
        pool = await repository.pool()
        await pool.execute("update gateway_generation_jobs set request_metadata=request_metadata || $2::jsonb where id=$1",
                           job_id, json.dumps({"outputs": renewed}))
        body, status, mime, headers = await _remote_content(renewed[output_index]["url"], request.headers.get("range"), maximum)
    return StreamingResponse(body, status_code=status, media_type=mime, headers=headers)


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
        source = await adapter_for_job(job).content(job)
    except ProviderAdapterError as exc:
        raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
    maximum = int(os.environ.get("GENERATION_MAX_CONTENT_BYTES", str(2 * 1024 * 1024 * 1024)))
    extension = "mov" if source.mime_type == "video/quicktime" else "mp4"
    headers = {"Content-Disposition": f'attachment; filename="{job_id}.{extension}"'}
    if source.url:
        try:
            body, status, mime, forwarded = await _remote_content(
                source.url, request.headers.get("range"), maximum
            )
        except ProviderAdapterError as exc:
            if exc.code != "CONTENT_URL_EXPIRED" or job["provider"] == "vertex":
                raise HTTPException(502, detail={"code": exc.code, "message": str(exc)}) from exc
            try:
                refreshed = await adapter_for_job(job).retrieve(job)
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
        provider_status = await adapter_for_job(job).retrieve(job)
        if provider_status.status in TERMINAL_STATUSES:
            # Cost evidence survives a later failure updating the media job.
            await _record_spend({**job, "status": provider_status.status,
                                 "usage": provider_status.usage or job.get("usage"),
                                 "request_metadata": {**(job.get("request_metadata") or {}),
                                                      "served_model": provider_status.served_model}})
        job = await repository.apply_provider_status(job_id, provider_status)
    except ProviderAdapterError as exc:
        if exc.usage:
            await _record_spend({**job, "status": "failed", "usage": exc.usage})
        if not exc.retryable:
            provider_status = ProviderStatus(
                status="failed", provider_status=job.get("provider_status") or "unknown",
                error_code=exc.code, error_message=str(exc), error_retryable=False,
                usage=exc.usage,
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
    await accounting.reconcile()
    from gateway_accounting import invalidate_caches
    await invalidate_caches()
    report = await accounting.report()
    issue_count = sum(len(report[key]) for key in ("issues", "unfinished_requests", "aggregate_divergence", "expired_pricing"))
    if issue_count:
        logger.error("accounting_reconciliation_alert", extra={"issue_count": issue_count})
    return {"due": len(jobs), "enqueued": enqueued, "spend_reconciled": len(spend_jobs)}


@router.post("/internal/generation-jobs/cleanup", include_in_schema=False)
async def cleanup_generation_jobs(request: Request) -> dict[str, int]:
    await _verify_internal(request)
    return {"deleted": await repository.cleanup(int(os.environ.get("GENERATION_RETENTION_DAYS", "30")))}
