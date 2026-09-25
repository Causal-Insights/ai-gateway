"""Owned ElevenLabs voices, separate from priced speech execution.

Samples are validated in memory and sent once; only consent and digests persist.
Provider-side samples are retained with the voice and removed by provider deletion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any
from uuid import uuid4
from urllib.parse import quote

import httpx
import litellm
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from starlette.datastructures import UploadFile

from generation_job_repository import repository

router = APIRouter(tags=["voices"])
SPEECH_MODELS = {"elevenlabs-v3-tts", "elevenlabs-multilingual-v2"}
MAX_SAMPLE_BYTES = 25 * 1024 * 1024


def fail(status: int, code: str, message: str):
    raise HTTPException(status, detail={"code": code, "message": message})


def owner_for(user: UserAPIKeyAuth, headers: Any) -> tuple[str, str]:
    # Only authenticated key metadata grants delegation; body metadata is ignored.
    metadata = getattr(user, "metadata", None) or {}
    app_id = metadata.get("gateway_app_id")
    if metadata.get("gateway_resource_delegate") is not True or not isinstance(app_id, str) or not app_id.strip():
        fail(403, "VOICE_DELEGATION_REQUIRED", "This key cannot manage private voice resources.")
    owner = next((value for name, value in headers.items() if name.lower() == "x-gateway-resource-owner"), "")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
        fail(401, "VOICE_OWNER_REQUIRED", "An authenticated resource owner is required.")
    return app_id, owner


def public_voice(row: Any) -> dict:
    required = row["state"] == "verification_required"
    return {
        "id": row["id"], "request_id": row["request_id"], "name": row["name"], "state": row["state"],
        "compatible_models": sorted(SPEECH_MODELS),
        "verification": {"required": required, **({
            "message": "The provider requires voice verification. Ask the workspace administrator to complete it, then refresh."
        } if required else {})},
        **({"error": {"code": row["error_code"], "message": "The voice operation needs attention."}} if row["error_code"] else {}),
        "cleanup_pending": row["deleted_at"] is not None and row["cleanup_completed_at"] is None,
    }


async def provider_request(method: str, path: str, **kwargs) -> httpx.Response:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        fail(503, "VOICE_PROVIDER_NOT_CONFIGURED", "The voice provider is not configured.")
    async with httpx.AsyncClient(timeout=120) as client:
        return await client.request(method, "https://api.elevenlabs.io" + path, headers={"xi-api-key": key}, **kwargs)


async def validate_sample(data: bytes) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-xerror", "-err_detect", "explode", "-protocol_whitelist", "pipe", "-i", "pipe:0",
        "-map", "0:a:0", "-f", "null", "-", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.communicate(data), timeout=45)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        fail(422, "VOICE_SAMPLES_INVALID", "The audio sample could not be decoded.")
    if process.returncode:
        fail(422, "VOICE_SAMPLES_INVALID", "The audio sample could not be decoded.")


async def read_create(request: Request) -> tuple[str, str, dict, list[tuple[str, bytes, str]], str, list[str]]:
    # Bound the total multipart body, including chunked uploads, before parsing.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_SAMPLE_BYTES + 1024 * 1024:
            fail(413, "VOICE_SAMPLES_TOO_LARGE", "Voice samples exceed the upload limit.")
    request._body = bytes(body)
    async with request.form(max_files=10, max_fields=5, max_part_size=MAX_SAMPLE_BYTES) as form:
        name = str(form.get("name") or "").strip()
        request_id = request.headers.get("idempotency-key") or str(form.get("request_id") or "")
        if not name or len(name) > 160 or not request_id or len(request_id) > 200:
            fail(422, "VOICE_INPUT_INVALID", "A voice name and idempotent request ID are required.")
        if form.get("request_id") and str(form["request_id"]) != request_id:
            fail(409, "VOICE_IDEMPOTENCY_CONFLICT", "The request IDs do not match.")
        try:
            consent = json.loads(str(form.get("consent") or "{}"))
        except (ValueError, TypeError):
            consent = {}
        if not isinstance(consent, dict) or not all(consent.get(key) for key in ("version", "actor_id", "accepted_at")):
            fail(422, "VOICE_CONSENT_REQUIRED", "Explicit speaker permission is required.")
        samples = []
        for field, item in form.multi_items():
            if field in {"files", "files[]"} and isinstance(item, UploadFile):
                data = await item.read(MAX_SAMPLE_BYTES + 1)
                if not data or not str(item.content_type).startswith("audio/"):
                    fail(422, "VOICE_SAMPLES_INVALID", "Choose a non-empty audio sample.")
                samples.append((item.filename or "sample", data, item.content_type))
        if not samples or sum(len(sample[1]) for sample in samples) > MAX_SAMPLE_BYTES:
            fail(422, "VOICE_SAMPLES_INVALID", "Choose audio samples totaling no more than 25 MB.")
        digests = [hashlib.sha256(sample[1]).hexdigest() for sample in samples]
        # The boundary receipt is the authority; client-provided hashes cannot describe other bytes.
        consent = {key: consent[key] for key in ("version", "actor_id", "accepted_at")}
        consent["sample_hashes"] = digests
        fingerprint = hashlib.sha256(json.dumps({"name": name, "consent": consent, "samples": digests}, sort_keys=True).encode()).hexdigest()
        return name, request_id, consent, samples, fingerprint, digests


async def owned_row(voice_id: str, owner: tuple[str, str]):
    pool = await repository.pool()
    row = await pool.fetchrow("select * from gateway_voice_resources where id=$1 and app_id=$2 and owner_id=$3", voice_id, *owner)
    if not row:
        fail(404, "VOICE_UNAVAILABLE", "The selected voice is unavailable.")
    return row


def verification_required(payload: dict, prior: bool = False) -> bool:
    verification = payload.get("voice_verification") or payload
    if verification.get("is_verified") is True:
        return False
    value = verification.get("requires_verification")
    return value if isinstance(value, bool) else prior


async def record_provider_voice(row: Any, payload: dict):
    pool = await repository.pool()
    required = verification_required(payload, row["requires_verification"])
    provider_id = payload.get("voice_id")
    if not isinstance(provider_id, str) or not provider_id:
        return row
    updated = await pool.fetchrow("""update gateway_voice_resources set provider_voice_id=$2,
        requires_verification=$3,state=case when deleted_at is not null then 'deleted' else $4 end,
        error_code=null,updated_at=now() where id=$1 returning *""",
        row["id"], provider_id, required, "verification_required" if required else "ready")
    return updated


async def refresh_voice(row: Any):
    if row["cleanup_completed_at"] or row["state"] == "failed":
        return row
    if row["provider_voice_id"]:
        response = await provider_request("GET", f"/v1/voices/{row['provider_voice_id']}")
        if response.status_code == 404:
            pool = await repository.pool()
            return await pool.fetchrow("""update gateway_voice_resources set state='deleted',deleted_at=coalesce(deleted_at,now()),
                cleanup_completed_at=now(),updated_at=now() where id=$1 returning *""", row["id"])
        if not response.is_success:
            return row
        return await record_provider_voice(row, response.json())
    # A deterministic provider label reconciles a timed-out POST without consuming another voice slot.
    cursor = None
    while True:
        params = {"page_size": 100, **({"next_page_token": cursor} if cursor else {})}
        response = await provider_request("GET", "/v2/voices", params=params)
        if not response.is_success:
            return row
        payload = response.json()
        for voice in payload.get("voices", []):
            if (voice.get("labels") or {}).get("gateway_resource_id") == row["id"]:
                return await record_provider_voice(row, voice)
        next_cursor = payload.get("next_page_token")
        if not payload.get("has_more") or not next_cursor or next_cursor == cursor:
            return row
        cursor = next_cursor


@router.post("/v1/voices")
async def create_voice(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    owner = owner_for(user, request.headers)
    name, request_id, consent, samples, fingerprint, digests = await read_create(request)
    if consent["actor_id"] != owner[1]:
        fail(403, "VOICE_CONSENT_OWNER_MISMATCH", "Consent must belong to the authenticated speaker-resource owner.")
    if not os.environ.get("ELEVENLABS_API_KEY", "").strip():
        fail(503, "VOICE_PROVIDER_NOT_CONFIGURED", "The voice provider is not configured.")
    for _, data, _ in samples:
        await validate_sample(data)
    pool = await repository.pool()
    voice_id = "voice_" + uuid4().hex
    inserted = await pool.fetchrow("""insert into gateway_voice_resources(id,app_id,owner_id,request_id,request_hash,name,consent,sample_hashes)
        values($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb) on conflict(app_id,owner_id,request_id) do nothing returning *""",
        voice_id, *owner, request_id, fingerprint, name, json.dumps(consent), json.dumps(digests))
    if not inserted:
        row = await pool.fetchrow("select * from gateway_voice_resources where app_id=$1 and owner_id=$2 and request_id=$3", *owner, request_id)
        if row["request_hash"] != fingerprint:
            fail(409, "VOICE_IDEMPOTENCY_CONFLICT", "This request ID already contains different samples or settings.")
        try:
            row = await refresh_voice(row)
        except (httpx.HTTPError, ValueError):
            pass
        return public_voice(row)
    await pool.execute("insert into gateway_voice_events(voice_id,action) values($1,'create_intent')", voice_id)
    try:
        response = await provider_request("POST", "/v1/voices/add", data={"name": name,
            "labels": json.dumps({"gateway_resource_id": voice_id})}, files=[("files", sample) for sample in samples])
        if response.is_success:
            row = await record_provider_voice(inserted, response.json())
            if row["provider_voice_id"]:
                await pool.execute("insert into gateway_voice_events(voice_id,action) values($1,'created')", voice_id)
                return JSONResponse(public_voice(row), status_code=201)
        definitive = 400 <= response.status_code < 500
        code = "VOICE_QUOTA_EXCEEDED" if response.status_code == 429 else "VOICE_PROVIDER_REJECTED" if definitive else "VOICE_CREATE_OUTCOME_UNKNOWN"
    except (httpx.HTTPError, ValueError, HTTPException):
        definitive, code = False, "VOICE_CREATE_OUTCOME_UNKNOWN"
    row = await pool.fetchrow("""update gateway_voice_resources set state=case when deleted_at is not null then 'deleted' else $2 end,
        error_code=$3,updated_at=now() where id=$1 returning *""", voice_id, "failed" if definitive else "outcome_unknown", code)
    return JSONResponse(public_voice(row), status_code=202)


@router.get("/v1/voices")
async def list_voices(request: Request, request_id: str | None = None, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    owner = owner_for(user, request.headers)
    pool = await repository.pool()
    rows = await pool.fetch("""select * from gateway_voice_resources where app_id=$1 and owner_id=$2
        and ($3::text is null or request_id=$3) order by created_at desc""", *owner, request_id)
    return {"object": "list", "data": [public_voice(row) for row in rows]}


@router.get("/v1/voices/{voice_id}")
async def get_voice(voice_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    row = await owned_row(voice_id, owner_for(user, request.headers))
    try:
        row = await refresh_voice(row)
    except (httpx.HTTPError, ValueError):
        pass  # Keep the durable lifecycle state when a read is temporarily unavailable.
    return public_voice(row)


@router.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    row = await owned_row(voice_id, owner_for(user, request.headers))
    definitely_absent = row["state"] == "failed" and not row["provider_voice_id"]
    pool = await repository.pool()
    row = await pool.fetchrow("""update gateway_voice_resources set state='deleted',deleted_at=coalesce(deleted_at,now()),
        updated_at=now() where id=$1 returning *""", voice_id)
    if definitely_absent:
        row = await pool.fetchrow("update gateway_voice_resources set cleanup_completed_at=now(),updated_at=now() where id=$1 returning *", voice_id)
    if row["cleanup_completed_at"]:
        return public_voice(row)
    try:
        row = await refresh_voice(row)
        if row["provider_voice_id"]:
            response = await provider_request("DELETE", f"/v1/voices/{row['provider_voice_id']}")
            if response.is_success or response.status_code == 404:
                row = await pool.fetchrow("update gateway_voice_resources set cleanup_completed_at=now(),updated_at=now() where id=$1 returning *", voice_id)
                await pool.execute("insert into gateway_voice_events(voice_id,action) values($1,'deleted')", voice_id)
    except (httpx.HTTPError, ValueError):
        pass
    return JSONResponse(public_voice(row), status_code=202 if row["cleanup_completed_at"] is None else 200)


async def resolve_voice_for_speech(voice: str, model: str, user: UserAPIKeyAuth, headers: Any) -> str:
    pool = await repository.pool()
    if voice.startswith("voice:"):
        # LiteLLM 1.102.1 overwrites proxy_server_request from the real Request
        # before hooks; never read a body-supplied owner or metadata identity.
        row = await owned_row(voice.removeprefix("voice:"), owner_for(user, headers))
        if row["deleted_at"] or row["state"] == "deleted":
            fail(409, "VOICE_UNAVAILABLE", "The selected voice is unavailable.")
        if row["state"] == "verification_required":
            fail(409, "VOICE_VERIFICATION_REQUIRED", "Complete provider voice verification, then refresh.")
        if row["state"] != "ready":
            fail(409, "VOICE_UNAVAILABLE", "The selected voice is not ready.")
        if model not in SPEECH_MODELS:
            fail(409, "VOICE_MODEL_INCOMPATIBLE", "This private voice is incompatible with the selected model.")
        return row["provider_voice_id"]
    elif await pool.fetchval("select exists(select 1 from gateway_voice_resources where provider_voice_id=$1)", voice):
        fail(403, "VOICE_RESOURCE_ID_REQUIRED", "Use the authorized private voice resource ID.")
    elif await pool.fetchval("""select exists(select 1 from gateway_voice_resources where provider_voice_id is null
        and state in ('creating','outcome_unknown','deleted') and cleanup_completed_at is null)"""):
        # A timed-out creation may exist upstream before its provider ID is
        # recorded locally. Check its immutable creation label before speech.
        try:
            response = await provider_request("GET", "/v1/voices/" + quote(voice, safe=""))
            if response.is_success:
                payload = response.json()
                resource_id = (payload.get("labels") or {}).get("gateway_resource_id")
                row = await pool.fetchrow("select * from gateway_voice_resources where id=$1", resource_id) if resource_id else None
                if row:
                    await record_provider_voice(row, payload)
                    fail(403, "VOICE_RESOURCE_ID_REQUIRED", "Use the authorized private voice resource ID.")
            elif response.status_code >= 500 or response.status_code == 429:
                fail(503, "VOICE_PROVIDER_UNAVAILABLE", "Voice ownership could not be resolved. Try again shortly.")
        except (httpx.HTTPError, ValueError):
            fail(503, "VOICE_PROVIDER_UNAVAILABLE", "Voice ownership could not be resolved. Try again shortly.")
    return voice


class VoiceAccess(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if getattr(call_type, "value", call_type) not in {"speech", "aspeech"} or not isinstance(data.get("voice"), str):
            return data
        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        data["voice"] = await resolve_voice_for_speech(data["voice"], data.get("model"), user_api_key_dict, headers)
        return data


def install():
    if not any(isinstance(callback, VoiceAccess) for callback in litellm.callbacks):
        litellm.callbacks.insert(0, VoiceAccess())
