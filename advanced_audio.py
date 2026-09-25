"""Finite ElevenAPI audio operations with owned resources and durable receipts."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import struct
import wave
from typing import Any
from uuid import uuid4
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

from cost_accounting import accounting, identity_for
from gateway_accounting import check_context, finalize_reservation, state
from generation_job_repository import repository
from pricing_registry import PricingError, registry
from voice_resources import MAX_SAMPLE_BYTES, fail, owner_for, provider_request, resolve_voice_for_speech, validate_sample

router = APIRouter(tags=["advanced-audio"])
log = logging.getLogger("ai_gateway.advanced_audio")
SPEECH = {"elevenlabs-v3-tts": "eleven_v3", "elevenlabs-multilingual-v2": "eleven_multilingual_v2"}
MUSIC = {"elevenlabs-music": "music_v1", "elevenlabs-music-2.5": "music_v2_5"}
SFX = "elevenlabs-sfx"


def number(value, minimum, maximum, label, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum or (integer and int(value) != value):
        fail(422, "AUDIO_INPUT_INVALID", f"{label} must be between {minimum} and {maximum}.")
    return value


def object_value(value, label):
    if not isinstance(value, dict):
        fail(422, "AUDIO_INPUT_INVALID", f"{label} must be an object.")
    return value


def output_format(data, *, music=False):
    value = data.get("output_format", data.get("response_format", "auto" if music else "mp3"))
    if not isinstance(value, str):
        fail(422, "AUDIO_FORMAT_INVALID", "Choose a supported audio output format.")
    mapping = {"mp3": "mp3_44100_128", "wav": "pcm_44100", "pcm": "pcm_24000", "opus": "opus_48000_128"}
    result = mapping.get(value, value)
    import re
    if not isinstance(result, str) or (result != "auto" and not re.fullmatch(r"(?:mp3|opus)_\d+_\d+|(?:pcm|ulaw|alaw)_\d+", result)):
        fail(422, "AUDIO_FORMAT_INVALID", "Choose a supported audio output format.")
    return result


async def read_json(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            fail(413, "AUDIO_INPUT_TOO_LARGE", "The audio instructions are too large.")
    try:
        return object_value(json.loads(body), "Request")
    except ValueError:
        fail(422, "AUDIO_INPUT_INVALID", "Audio instructions must be valid JSON.")


def free_plan_profile(model):
    return {"version": "elevenlabs-music-plan-free@2026-09-24", "status": "verified", "enabled": True,
        "vendor": "elevenlabs", "upstream_model": MUSIC[model], "currency": "USD", "extractor": "components",
        "rate_source": "provider_documented_free", "components": [{"usage": "requests", "rate": "0", "per": "1", "unit": "request"}],
        "zero_reasons": ["provider_documented_free"], "official_source": "https://elevenlabs.io/docs/api-reference/music/create-composition-plan"}


async def begin_operation(request, user, data, operation, *, fingerprint_data=None, free=False):
    owner = owner_for(user, request.headers)
    model = data.get("model")
    allowed_models = getattr(user, "models", None) or []
    if allowed_models and "all-proxy-models" not in allowed_models and model not in allowed_models:
        fail(403, "AUDIO_MODEL_FORBIDDEN", "This application key cannot use the selected model.")
    request_id = request.headers.get("idempotency-key") or data.get("request_id") or str(uuid4())
    if not isinstance(request_id, str) or len(request_id) > 200:
        fail(422, "AUDIO_REQUEST_ID_INVALID", "The request ID is invalid.")
    if data.get("request_id") and data["request_id"] != request_id:
        fail(409, "AUDIO_IDEMPOTENCY_CONFLICT", "The request IDs do not match.")
    fingerprint = hashlib.sha256(json.dumps([operation, fingerprint_data if fingerprint_data is not None else data], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    pool = await repository.pool()
    row = await pool.fetchrow("""insert into gateway_audio_requests(id,app_id,owner_id,request_id,request_hash,operation,model)
        values($1,$2,$3,$4,$5,$6,$7) on conflict(app_id,owner_id,request_id) do nothing returning *""",
        "audio_" + uuid4().hex, *owner, request_id, fingerprint, operation, model)
    if not row:
        row = await pool.fetchrow("select * from gateway_audio_requests where app_id=$1 and owner_id=$2 and request_id=$3", *owner, request_id)
        if row["request_hash"] != fingerprint:
            fail(409, "AUDIO_IDEMPOTENCY_CONFLICT", "This request ID already contains different audio instructions.")
        return row, False
    try:
        route = "speech" if model in SPEECH else "image_generation"
        profile = free_plan_profile(model) if free else registry().select(model, route, {"tools": False})
        check_context(profile)
        identity = identity_for(user)
        await accounting.check_budgets(identity, model)
        accounting_id = await accounting.begin(model=model, route="audio_plan" if free else route, identity=identity, accounting_id=row["id"])
        await accounting.attempt(accounting_id, profile, attempt_id=accounting_id + ":1")
        row = await pool.fetchrow("update gateway_audio_requests set accounting_id=$2 where id=$1 returning *", row["id"], accounting_id)
        current = state.get()
        if current is not None:
            current.update(accounting_id=accounting_id, profile=profile, alias=model, route=route,
                attempts=[accounting_id + ":1"], reservation=getattr(user, "budget_reservation", None))
    except PricingError as error:
        await pool.execute("update gateway_audio_requests set state='failed',error_code=$2 where id=$1", row["id"], error.code)
        fail(503, error.code, str(error))
    return row, True


async def receipt(row, response, usage, *, free=False, outcome="success"):
    if not row["accounting_id"]:
        return
    try:
        await accounting.observe(row["accounting_id"] + ":1", raw_usage=usage,
            served_model=SPEECH.get(row["model"], MUSIC.get(row["model"], "eleven_text_to_sound_v2")),
            provider_request_id=response.headers.get("request-id") if response is not None else None,
            zero_reason="provider_documented_free" if free else None, outcome=outcome)
        await accounting.finish(row["accounting_id"])
        await finalize_reservation(state.get() or {})
    except Exception:
        log.exception("advanced_audio_cost_receipt_pending", extra={"accounting_id": row["accounting_id"]})


async def replay(row):
    current = state.get()
    if current is not None and row["accounting_id"]:
        current["accounting_id"] = row["accounting_id"]
    metadata = json.loads(row["response_metadata"]) if isinstance(row["response_metadata"], str) else row["response_metadata"]
    if row["state"] == "ready" and metadata is not None:
        pool = await repository.pool()
        payload = await pool.fetchrow("select * from gateway_audio_payloads where request_id=$1", row["id"])
        return {**metadata, **({"audio_base64": base64.b64encode(payload["audio"]).decode(), "content_type": payload["content_type"]} if payload else {})}
    return JSONResponse({"request_id": row["request_id"], "id": row["id"], "state": row["state"],
        "accounting_id": row["accounting_id"], "error": {"code": row["error_code"] or "AUDIO_OPERATION_PENDING",
        "message": "The original audio operation has not completed. It will not be submitted twice."}}, status_code=202 if row["state"] in {"creating", "outcome_unknown"} else 409)


async def finish_operation(row, metadata, audio=None, content_type=None):
    pool = await repository.pool()
    result = {**metadata, "request_id": row["request_id"], "accounting_id": row["accounting_id"]}
    deleted = False
    async with pool.acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchrow("select state from gateway_audio_requests where id=$1 for update", row["id"])
            deleted = current["state"] == "deleted"
            if deleted:
                await connection.execute("update gateway_song_resources set state='deleted',deleted_at=coalesce(deleted_at,now()),composition_plan=null where request_id=$1", row["id"])
            else:
                if audio is not None:
                    await connection.execute("insert into gateway_audio_payloads(request_id,content_type,audio) values($1,$2,$3)", row["id"], content_type, audio)
                await connection.execute("update gateway_audio_requests set state='ready',response_metadata=$2::jsonb,updated_at=now() where id=$1", row["id"], json.dumps(result))
    if deleted:
        fail(410, "AUDIO_RESOURCE_DELETED", "This audio resource was deleted.")
    return {**result, **({"audio_base64": base64.b64encode(audio).decode(), "content_type": content_type} if audio is not None else {})}


async def submit(row, path, *, json_body=None, params=None, files=None, data=None):
    try:
        response = await provider_request("POST", path, json=json_body, params=params, files=files, data=data)
    except HTTPException:
        await mark_failed(row, "AUDIO_PROVIDER_NOT_CONFIGURED")
        await receipt(row, None, {}, outcome="failure")
        raise
    except httpx.HTTPError:
        await mark_failed(row, "AUDIO_OUTCOME_UNKNOWN", unknown=True)
        await receipt(row, None, {}, outcome="unknown")
        fail(504, "AUDIO_OUTCOME_UNKNOWN", "The provider outcome is unknown. Retrieve the original request before submitting another generation.")
    if not response.is_success:
        unknown = response.status_code >= 500
        await mark_failed(row, "AUDIO_OUTCOME_UNKNOWN" if unknown else "AUDIO_PROVIDER_REJECTED", unknown=unknown)
        await receipt(row, response, {}, outcome="failure")
        fail(502 if unknown else 422, "AUDIO_PROVIDER_REJECTED", "The provider could not complete these audio settings.")
    return response


async def mark_failed(row, code, *, unknown=False):
    pool = await repository.pool()
    await pool.execute("update gateway_audio_requests set state=$2,error_code=$3,updated_at=now() where id=$1 and state not in ('ready','deleted')", row["id"], "outcome_unknown" if unknown else "failed", code)


async def audio_bytes(response, format_name, *, json_audio=False, channels=1):
    metadata = {}
    if json_audio:
        payload = response.json()
        try:
            data = base64.b64decode(payload["audio_base64"], validate=True)
        except (KeyError, ValueError):
            fail(502, "AUDIO_OUTPUT_INVALID", "The provider returned invalid audio.")
        metadata = {key: payload[key] for key in ("alignment", "normalized_alignment") if payload.get(key) is not None}
    else:
        data = response.content
    if not data:
        fail(502, "AUDIO_OUTPUT_INVALID", "The provider returned empty audio.")
    content_type = "audio/mpeg"
    if format_name.startswith("pcm_"):
        # PCM has no container; return a playable lossless WAV with its actual rate.
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(int(format_name.split("_")[1]))
            audio.writeframes(data)
        data, content_type = output.getvalue(), "audio/wav"
    elif format_name.startswith("opus_"):
        content_type = "audio/ogg"
    elif format_name.startswith(("ulaw_", "alaw_")):
        rate = int(format_name.split("_")[1])
        codec = 7 if format_name.startswith("ulaw_") else 6
        fmt = struct.pack("<HHIIHH", codec, 1, rate, rate, 1, 8)
        data = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
        content_type = "audio/wav"
    await validate_sample(data)
    return data, content_type, metadata


async def operation_audio(row, response, format_name, *, json_audio=False, channels=1, receipt_recorded=False):
    try:
        return await audio_bytes(response, format_name, json_audio=json_audio, channels=channels)
    except (HTTPException, ValueError, TypeError, KeyError, wave.Error):
        # A paid response with invalid media is terminal, not a still-running job.
        await mark_failed(row, "AUDIO_OUTPUT_INVALID")
        if not receipt_recorded:
            await receipt(row, response, {}, outcome="unknown")
        fail(502, "AUDIO_OUTPUT_INVALID", "The provider returned invalid audio. The original request will not be submitted again.")


@router.get("/v1/audio/requests/{request_id}")
async def get_audio_request(request_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    pool = await repository.pool()
    row = await pool.fetchrow("select * from gateway_audio_requests where app_id=$1 and owner_id=$2 and (id=$3 or request_id=$3)", *owner_for(user, request.headers), request_id)
    if not row:
        fail(404, "AUDIO_REQUEST_UNAVAILABLE", "The audio request is unavailable.")
    return await replay(row)


async def speech_operation(request, user, data, *, dialogue=False):
    model = data.get("model")
    if model not in SPEECH or (dialogue and model != "elevenlabs-v3-tts"):
        fail(422, "AUDIO_MODEL_INCOMPATIBLE", "Dialogue requires Eleven v3; speech requires an enabled ElevenLabs speech model.")
    format_name = output_format(data)
    timed = data.get("with_timestamps") is True
    if dialogue:
        inputs = data.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            fail(422, "AUDIO_INPUT_INVALID", "Add at least one dialogue turn.")
        turns = []
        for turn in inputs:
            turn = object_value(turn, "Dialogue turn")
            if not isinstance(turn.get("text"), str) or not turn["text"].strip() or not isinstance(turn.get("voice"), str):
                fail(422, "AUDIO_INPUT_INVALID", "Each dialogue turn needs text and a voice.")
            turns.append({"text": turn["text"], "voice_id": await resolve_voice_for_speech(turn["voice"], model, user, request.headers)})
        if len({turn["voice_id"] for turn in turns}) > 10:
            fail(422, "AUDIO_INPUT_INVALID", "Dialogue supports up to ten unique voices.")
        provider_body = {"model_id": SPEECH[model], "inputs": turns}
        path = "/v1/text-to-dialogue" + ("/with-timestamps" if timed else "")
        characters = sum(len(turn["text"]) for turn in turns)
    else:
        text = data.get("input")
        if not isinstance(text, str) or not text.strip() or not isinstance(data.get("voice"), str):
            fail(422, "AUDIO_INPUT_INVALID", "Speech needs text and a voice.")
        voice = await resolve_voice_for_speech(data["voice"], model, user, request.headers)
        provider_body = {"model_id": SPEECH[model], "text": text}
        for key in ("voice_settings", "language_code", "seed", "previous_text", "next_text"):
            if key in data:
                provider_body[key] = data[key]
        path = "/v1/text-to-speech/" + quote(voice, safe="") + ("/with-timestamps" if timed else "")
        characters = len(text)
    row, created = await begin_operation(request, user, data, "dialogue" if dialogue else "speech")
    if not created:
        return await replay(row)
    response = await submit(row, path, json_body=provider_body, params={"output_format": format_name})
    await receipt(row, response, {"billable_characters": response.headers.get("character-cost", characters)})
    audio, content_type, metadata = await operation_audio(row, response, format_name, json_audio=timed, receipt_recorded=True)
    return await finish_operation(row, metadata, audio, content_type)


@router.post("/v1/audio/dialogue")
async def dialogue(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    return await speech_operation(request, user, await read_json(request), dialogue=True)


@router.post("/v1/audio/speech-with-timestamps")
async def speech_timestamps(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    data = await read_json(request)
    data["with_timestamps"] = True
    return await speech_operation(request, user, data)


async def owned_song(song_id, owner):
    pool = await repository.pool()
    row = await pool.fetchrow("select * from gateway_song_resources where id=$1 and app_id=$2 and owner_id=$3", song_id, *owner)
    if not row or row["deleted_at"] or row["state"] != "ready":
        fail(404, "SONG_UNAVAILABLE", "The source song is unavailable.")
    return row


async def resolve_plan(plan, model, owner):
    plan = json.loads(json.dumps(object_value(plan, "Composition plan")))
    if model == "elevenlabs-music":
        sections = plan.get("sections")
        if not isinstance(sections, list) or not sections or "chunks" in plan:
            fail(422, "MUSIC_PLAN_INVALID", "Music v1 requires a sections composition plan.")
        for section in sections:
            object_value(section, "Section")
            number(section.get("duration_ms"), 3000, 120000, "Section duration", integer=True)
        return plan
    chunks = plan.get("chunks")
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 30 or "sections" in plan:
        fail(422, "MUSIC_PLAN_INVALID", "Music 2.5 requires one to thirty composition chunks.")
    async def resolve_reference(reference, *, conditioning=False):
        reference = object_value(reference, "Audio reference")
        song = await owned_song(reference.get("song_id"), owner)
        interval = object_value(reference.get("range"), "Source range")
        start = number(interval.get("start_ms"), 0, song["duration_ms"] or 600000, "Range start", integer=True)
        end = number(interval.get("end_ms"), start + 50, song["duration_ms"] or 600000, "Range end", integer=True)
        if conditioning and end - start > 30000:
            fail(422, "MUSIC_PLAN_INVALID", "Conditioning references may contain at most thirty seconds.")
        reference["song_id"] = song["provider_song_id"]
    for chunk in chunks:
        object_value(chunk, "Composition chunk")
        if "song_id" in chunk:
            await resolve_reference(chunk)
        else:
            number(chunk.get("duration_ms"), 3000, 120000, "Chunk duration", integer=True)
            if not isinstance(chunk.get("text"), str):
                fail(422, "MUSIC_PLAN_INVALID", "Each generated chunk needs text.")
            for key in ("positive_styles", "negative_styles"):
                styles = chunk.get(key, [])
                if not isinstance(styles, list) or len(styles) > 50 or any(not isinstance(style, str) for style in styles):
                    fail(422, "MUSIC_PLAN_INVALID", "Each style list supports up to fifty text entries.")
            if chunk.get("context_adherence", "high") not in {"low", "medium", "high"}:
                fail(422, "MUSIC_PLAN_INVALID", "Choose low, medium or high context adherence.")
            if chunk.get("conditioning_ref"):
                await resolve_reference(chunk["conditioning_ref"], conditioning=True)
            if chunk.get("condition_strength", "medium") not in {None, "low", "medium", "high", "xhigh"}:
                fail(422, "MUSIC_PLAN_INVALID", "The conditioning strength is invalid.")
    return plan


async def prepare_music(data, owner):
    model = data.get("model")
    if model not in MUSIC:
        fail(422, "AUDIO_MODEL_INCOMPATIBLE", "Choose an ElevenLabs music model.")
    plan = data.get("composition_plan")
    prompt = data.get("prompt")
    if (plan is not None) == bool(isinstance(prompt, str) and prompt.strip()):
        fail(422, "MUSIC_PLAN_INVALID", "Use either a prompt or a composition plan.")
    payload = {"model_id": MUSIC[model]}
    if plan is not None:
        payload["composition_plan"] = await resolve_plan(plan, model, owner)
        if "music_length_ms" in data or data.get("force_instrumental"):
            fail(422, "MUSIC_PLAN_INVALID", "Duration and instrumental mode belong to prompt generation; sections define plan duration and lyrics.")
        if "seed" in data and data["seed"] is not None:
            payload["seed"] = number(data["seed"], 0, 2147483647, "Seed", integer=True)
        if model == "elevenlabs-music" and "respect_sections_durations" in data:
            payload["respect_sections_durations"] = data["respect_sections_durations"]
    else:
        if len(prompt) > 4100:
            fail(422, "MUSIC_PROMPT_TOO_LONG", "Music prompts may contain up to 4,100 characters.")
        if data.get("seed") is not None:
            fail(422, "MUSIC_PLAN_INVALID", "Use a composition plan to set a music seed.")
        payload["prompt"] = prompt
        if data.get("music_length_ms") is not None:
            payload["music_length_ms"] = number(data["music_length_ms"], 3000, 600000, "Music duration", integer=True)
        if "force_instrumental" in data:
            payload["force_instrumental"] = data["force_instrumental"]
    if model == "elevenlabs-music-2.5":
        payload["store_for_inpainting"] = data.get("store_for_inpainting", True)
    return payload


async def media_duration_ms(audio, content_type):
    from generation_job_adapters import probe_media_bytes
    extension = ".wav" if content_type == "audio/wav" else ".ogg" if content_type == "audio/ogg" else ".mp3"
    probe = await asyncio.to_thread(probe_media_bytes, audio, extension)
    duration = (probe.get("format") or {}).get("duration")
    return round(float(duration) * 1000) if duration is not None else None


async def create_song_row(row, provider_id, duration_ms, composition_plan=None):
    pool = await repository.pool()
    song = await pool.fetchrow("""insert into gateway_song_resources(id,app_id,owner_id,request_id,provider_song_id,state,duration_ms,composition_plan)
        values($1,$2,$3,$4,$5,'ready',$6,$7::jsonb) returning *""", "song_" + uuid4().hex,
        row["app_id"], row["owner_id"], row["id"], provider_id, duration_ms, json.dumps(composition_plan) if composition_plan else None)
    return song["id"]


@router.post("/v1/audio/generations")
async def generate_audio(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    data = await read_json(request)
    return await generate_audio_data(request, user, data)


async def generate_audio_data(request, user, data):
    model = data.get("model")
    owner = owner_for(user, request.headers)
    format_name = output_format(data, music=model in MUSIC)
    if model in MUSIC:
        payload = await prepare_music(data, owner)
        path = "/v1/music"
    elif model == SFX:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            fail(422, "AUDIO_INPUT_INVALID", "Describe the sound effect.")
        payload = {"model_id": "eleven_text_to_sound_v2", "text": prompt}
        if data.get("duration_seconds") is not None:
            payload["duration_seconds"] = number(data["duration_seconds"], 0.5, 30, "Sound duration")
        if data.get("prompt_influence") is not None:
            payload["prompt_influence"] = number(data["prompt_influence"], 0, 1, "Prompt influence")
        if "loop" in data:
            if not isinstance(data["loop"], bool):
                fail(422, "AUDIO_INPUT_INVALID", "Loop must be true or false.")
            payload["loop"] = data["loop"]
        path = "/v1/sound-generation"
    else:
        fail(422, "AUDIO_MODEL_INCOMPATIBLE", "Choose an ElevenLabs music or sound-effects model.")
    row, created = await begin_operation(request, user, data, "audio_generation")
    if not created:
        return await replay(row)
    response = await submit(row, path, json_body=payload, params={"output_format": format_name})
    audio, content_type, _ = await operation_audio(row, response, format_name, channels=2 if model in MUSIC else 1)
    duration_ms = await media_duration_ms(audio, content_type)
    usage = {"audio_seconds": duration_ms / 1000} if duration_ms is not None else {}
    if response.headers.get("character-cost"):
        usage["billable_characters"] = response.headers["character-cost"]
    await receipt(row, response, usage)
    metadata = {"duration_ms": duration_ms}
    if model in MUSIC and response.headers.get("song-id"):
        metadata["song_id"] = await create_song_row(row, response.headers["song-id"], duration_ms, data.get("composition_plan"))
        if data.get("composition_plan"):
            metadata["composition_plan"] = data["composition_plan"]
    return await finish_operation(row, metadata, audio, content_type)


@router.post("/v1/audio/music/plan")
async def music_plan(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    data = await read_json(request)
    model = data.get("model")
    if model not in MUSIC or not isinstance(data.get("prompt"), str) or not data["prompt"].strip() or len(data["prompt"]) > 4100:
        fail(422, "MUSIC_PLAN_INVALID", "Choose a music model and enter a prompt of up to 4,100 characters.")
    payload = {"model_id": MUSIC[model], "prompt": data["prompt"]}
    if data.get("music_length_ms") is not None:
        payload["music_length_ms"] = number(data["music_length_ms"], 3000, 600000, "Music duration", integer=True)
    if data.get("source_composition_plan") is not None:
        payload["source_composition_plan"] = await resolve_plan(data["source_composition_plan"], model, owner_for(user, request.headers))
    row, created = await begin_operation(request, user, data, "music_plan", free=True)
    if not created:
        return await replay(row)
    response = await submit(row, "/v1/music/plan", json_body=payload)
    await receipt(row, response, {"requests": 1}, free=True)
    try:
        plan = object_value(response.json(), "Returned composition plan")
        returned_sections = plan.get("chunks" if model == "elevenlabs-music-2.5" else "sections")
        if not isinstance(returned_sections, list) or not returned_sections or any(not isinstance(section, dict) for section in returned_sections):
            raise ValueError("Missing composition sections")
    except (ValueError, HTTPException):
        await mark_failed(row, "MUSIC_PLAN_INVALID")
        fail(502, "MUSIC_PLAN_INVALID", "The provider returned an invalid composition plan.")
    # Plans derived from stored audio may echo provider IDs. Restore owned IDs.
    pool = await repository.pool()
    for chunk in plan.get("chunks", []):
        for reference in (chunk, chunk.get("conditioning_ref") or {}):
            if reference.get("song_id"):
                song_id = await pool.fetchval("select id from gateway_song_resources where provider_song_id=$1 and app_id=$2 and owner_id=$3 and deleted_at is null", reference["song_id"], row["app_id"], row["owner_id"])
                if not song_id:
                    await mark_failed(row, "MUSIC_PLAN_INVALID")
                    fail(502, "MUSIC_PLAN_INVALID", "The returned plan contains an unavailable source song.")
                reference["song_id"] = song_id
    return await finish_operation(row, {"composition_plan": plan})


@router.post("/v1/songs")
async def import_song(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    owner_for(user, request.headers)
    upload_limit = int(os.environ.get("GENERATION_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > upload_limit + 1024 * 1024:
            fail(413, "SONG_TOO_LARGE", "The song exceeds the configured upload limit.")
    request._body = bytes(body)
    async with request.form(max_files=1, max_fields=3, max_part_size=upload_limit) as form:
        file = form.get("file")
        if not hasattr(file, "read"):
            fail(422, "SONG_FILE_REQUIRED", "Choose an audio file.")
        audio = await file.read(upload_limit + 1)
        if not audio or len(audio) > upload_limit:
            fail(413, "SONG_TOO_LARGE", "Choose an audio file within the configured upload limit.")
        await validate_sample(audio)
        data = {"model": "elevenlabs-music-2.5", "request_id": str(form.get("request_id") or request.headers.get("idempotency-key") or uuid4()), "sha256": hashlib.sha256(audio).hexdigest()}
        row, created = await begin_operation(request, user, data, "song_import")
        if not created:
            return await replay(row)
        response = await submit(row, "/v1/music/upload", files={"file": (file.filename or "song", audio, file.content_type or "audio/mpeg")})
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict) or not isinstance(payload.get("song_id"), str) or not payload["song_id"]:
            await mark_failed(row, "SONG_IMPORT_OUTCOME_UNKNOWN", unknown=True)
            await receipt(row, response, {}, outcome="unknown")
            fail(502, "SONG_IMPORT_OUTCOME_UNKNOWN", "The provider upload outcome is unknown.")
        content_type = file.content_type or "audio/mpeg"
        duration_ms = await media_duration_ms(audio, content_type)
        await receipt(row, response, {"audio_seconds": duration_ms / 1000} if duration_ms is not None else {})
        song_id = await create_song_row(row, payload["song_id"], duration_ms)
        return await finish_operation(row, {"song_id": song_id, "duration_ms": duration_ms}, audio, content_type)


def public_song(row):
    plan = row["composition_plan"]
    return {"id": row["id"], "state": row["state"], "duration_ms": row["duration_ms"],
        "composition_plan": json.loads(plan) if isinstance(plan, str) else plan,
        "provider_cleanup_supported": False}


@router.get("/v1/songs/{song_id}")
async def get_song(song_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    return public_song(await owned_song(song_id, owner_for(user, request.headers)))


@router.get("/v1/songs")
async def list_songs(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    pool = await repository.pool()
    rows = await pool.fetch("select * from gateway_song_resources where app_id=$1 and owner_id=$2 and deleted_at is null order by created_at desc", *owner_for(user, request.headers))
    return {"object": "list", "data": [public_song(row) for row in rows]}


@router.get("/v1/songs/{song_id}/content")
async def song_content(song_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    song = await owned_song(song_id, owner_for(user, request.headers))
    pool = await repository.pool()
    payload = await pool.fetchrow("select * from gateway_audio_payloads where request_id=$1", song["request_id"])
    if not payload:
        fail(404, "SONG_CONTENT_UNAVAILABLE", "The song content is unavailable.")
    return Response(payload["audio"], media_type=payload["content_type"], headers={"Cache-Control": "private, no-store"})


@router.delete("/v1/songs/{song_id}")
async def delete_song(song_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    pool = await repository.pool()
    song = await pool.fetchrow("select * from gateway_song_resources where id=$1 and app_id=$2 and owner_id=$3", song_id, *owner_for(user, request.headers))
    if not song:
        fail(404, "SONG_UNAVAILABLE", "The song is unavailable.")
    async with pool.acquire() as connection:
        async with connection.transaction():
            # Match completion/owner-erasure lock order so deletion can race delivery.
            await connection.fetchrow("select id from gateway_audio_requests where id=$1 for update", song["request_id"])
            song = await connection.fetchrow("update gateway_song_resources set state='deleted',deleted_at=coalesce(deleted_at,now()),composition_plan=null where id=$1 returning *", song_id)
            await connection.execute("delete from gateway_audio_payloads where request_id=$1", song["request_id"])
            await connection.execute("update gateway_audio_requests set state='deleted',response_metadata=null where id=$1", song["request_id"])
    return public_song(song)


@router.post("/v1/songs/{song_id}/takes")
async def song_take(song_id: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    await owned_song(song_id, owner_for(user, request.headers))
    data = await read_json(request)
    if data.get("model") != "elevenlabs-music-2.5" or data.get("operation") not in {"edit", "extend"}:
        fail(422, "MUSIC_EDIT_UNSUPPORTED", "Music editing and extension require Music 2.5.")
    plan = data.get("composition_plan") or {"chunks": data.get("chunks")}
    chunks = object_value(plan, "Composition plan").get("chunks")
    if not isinstance(chunks, list):
        fail(422, "MUSIC_PLAN_INVALID", "Music 2.5 requires a chunks composition plan.")
    references = [ref.get("song_id") for chunk in chunks for ref in (
        object_value(chunk, "Composition chunk"), object_value(chunk.get("conditioning_ref") or {}, "Conditioning reference"))]
    if song_id not in references:
        fail(422, "MUSIC_PLAN_INVALID", "The edited plan must reference the selected source song.")
    return await generate_audio_data(request, user, {**data, "composition_plan": plan})


class SpeechOptionsMiddleware:
    """Route only explicit timestamp requests; native speech stays untouched."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in {"/audio/speech", "/v1/audio/speech"}:
            return await self.app(scope, receive, send)
        messages, body = [], bytearray()
        while True:
            message = await receive()
            messages.append(message)
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("with_timestamps") is True:
                scope = dict(scope, path="/v1/audio/speech-with-timestamps", raw_path=b"/v1/audio/speech-with-timestamps")
        except (ValueError, TypeError):
            pass
        async def replay_receive():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay_receive, send)


@router.post("/v1/audio/sessions")
async def create_speech_session(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    import secrets
    from datetime import datetime, timedelta, timezone
    data = await read_json(request)
    owner = owner_for(user, request.headers)
    model = data.get("model")
    if model not in SPEECH:
        fail(422, "AUDIO_MODEL_INCOMPATIBLE", "Choose an ElevenLabs speech model.")
    allowed_models = getattr(user, "models", None) or []
    if allowed_models and "all-proxy-models" not in allowed_models and model not in allowed_models:
        fail(403, "AUDIO_MODEL_FORBIDDEN", "This application key cannot use the selected model.")
    voices = data.get("voices") or ([data["voice"]] if data.get("voice") else [])
    if not isinstance(voices, list) or not voices or any(not isinstance(voice, str) for voice in voices):
        fail(422, "VOICE_REQUIRED", "Choose the session voices.")
    if len(set(voices)) > (10 if model == "elevenlabs-v3-tts" else 1):
        fail(422, "AUDIO_MODEL_INCOMPATIBLE", "Multilingual sessions support one voice; Eleven v3 supports up to ten.")
    resolved = {voice: await resolve_voice_for_speech(voice, model, user, request.headers) for voice in voices}
    quota = number(data.get("max_characters"), 1, 2147483647, "Admitted character count", integer=True)
    format_name = output_format(data)
    try:
        profile = registry().select(model, "speech")
        identity = identity_for(user)
        await accounting.check_budgets(identity, model)
    except PricingError as error:
        fail(503, error.code, str(error))
    request_id = request.headers.get("idempotency-key") or data.get("request_id") or str(uuid4())
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        fail(422, "AUDIO_REQUEST_ID_INVALID", "The request ID is invalid.")
    if data.get("request_id") and data["request_id"] != request_id:
        fail(409, "AUDIO_IDEMPOTENCY_CONFLICT", "The request IDs do not match.")
    fingerprint = hashlib.sha256(json.dumps(["speech_session", data], sort_keys=True).encode()).hexdigest()
    pool = await repository.pool()
    row = await pool.fetchrow("""insert into gateway_audio_requests(id,app_id,owner_id,request_id,request_hash,operation,model)
        values($1,$2,$3,$4,$5,'speech_session',$6) on conflict(app_id,owner_id,request_id) do nothing returning *""",
        "audio_" + uuid4().hex, *owner, request_id, fingerprint, model)
    if row is None:
        row = await pool.fetchrow("select * from gateway_audio_requests where app_id=$1 and owner_id=$2 and request_id=$3", *owner, request_id)
        if row["request_hash"] != fingerprint:
            fail(409, "AUDIO_IDEMPOTENCY_CONFLICT", "This session request already has different settings.")
        existing = await pool.fetchrow("select claimed_at from gateway_speech_sessions where id=$1", row["id"])
        if (existing and existing["claimed_at"]) or row["state"] != "creating":
            return await replay(row)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=1)
    await pool.execute("""insert into gateway_speech_sessions(id,token_hash,expires_at,voices,output_format,max_characters,profile,identity)
        values($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8::jsonb) on conflict(id) do update
        set token_hash=excluded.token_hash,expires_at=excluded.expires_at where gateway_speech_sessions.claimed_at is null""",
        row["id"], hashlib.sha256(token.encode()).hexdigest(), expires, json.dumps(resolved), format_name, quota, json.dumps(profile), json.dumps(identity))
    # The ticket does not submit generation. WS admission repeats the budget check.
    await finalize_reservation({"reservation": getattr(user, "budget_reservation", None)})
    return {"id": row["id"], "request_id": request_id, "expires_at": expires.isoformat(), "max_characters": quota,
        "stream_path": f"/v1/audio/sessions/{row['id']}/stream?session_token={token}"}


async def claim_session(session_id, token):
    pool = await repository.pool()
    row = await pool.fetchrow("""update gateway_speech_sessions set claimed_at=now() where id=$1 and token_hash=$2
        and claimed_at is null and expires_at>now() returning *""", session_id, hashlib.sha256(token.encode()).hexdigest())
    if not row:
        return None
    return {**dict(row), **{key: json.loads(row[key]) if isinstance(row[key], str) else row[key] for key in ("voices", "profile", "identity")}}


def websocket_input(message, *, model, voices, characters, limit):
    """Finite wire compiler: clients cannot change models, voices, auth or format."""
    object_value(message, "Speech message")
    if message.get("type") == "close":
        return ({"close_socket": True} if model == "elevenlabs-v3-tts" else {"text": ""}), 0, True
    if message.get("type") == "flush":
        return ({"flush": True} if model == "elevenlabs-v3-tts" else {"text": " ", "flush": True}), 0, False
    if message.get("type") != "text" or not isinstance(message.get("text"), str):
        fail(422, "AUDIO_STREAM_INPUT_INVALID", "Send text, flush or close messages.")
    text = message["text"]
    if characters + len(text) > limit:
        fail(422, "AUDIO_STREAM_QUOTA_EXCEEDED", "This session has reached its admitted character count.")
    voice = message.get("voice") or next(iter(voices))
    if voice not in voices:
        fail(403, "VOICE_UNAVAILABLE", "The voice is outside this session's authorization.")
    if model == "elevenlabs-v3-tts":
        payload = {"inputs": [{"text": text, "voice_id": voices[voice], "new_turn": message.get("new_turn") is True}]}
    else:
        payload = {"text": text, "try_trigger_generation": True}
    return payload, len(text), False


from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect
from urllib.parse import urlencode


@router.websocket("/v1/audio/sessions/{session_id}/stream")
async def speech_stream(websocket: WebSocket, session_id: str):
    token = websocket.query_params.get("session_token", "")
    # Uvicorn formats its access line when accepting; do not log bearer tickets.
    websocket.scope["query_string"] = b""
    session = await claim_session(session_id, token)
    if session is None:
        await websocket.close(code=4401)
        return
    pool = await repository.pool()
    row = await pool.fetchrow("select * from gateway_audio_requests where id=$1", session_id)
    await websocket.accept()
    context_token = state.set({})
    characters, chunks, alignments, completed, persisted = 0, [], [], False, False
    try:
        delegated_user = UserAPIKeyAuth(metadata={"gateway_app_id": row["app_id"], "gateway_resource_delegate": True})
        for public_voice in session["voices"]:
            session["voices"][public_voice] = await resolve_voice_for_speech(public_voice, row["model"], delegated_user, {"X-Gateway-Resource-Owner": row["owner_id"]})
        await accounting.check_budgets(session["identity"], row["model"])
        accounting_id = await accounting.begin(model=row["model"], route="speech", identity=session["identity"], accounting_id=row["id"])
        await accounting.attempt(accounting_id, session["profile"], attempt_id=accounting_id + ":1")
        row = await pool.fetchrow("update gateway_audio_requests set accounting_id=$2 where id=$1 returning *", row["id"], accounting_id)
        state.get().update(accounting_id=accounting_id, profile=session["profile"], alias=row["model"], attempts=[accounting_id + ":1"])
        dialogue = row["model"] == "elevenlabs-v3-tts"
        path = "/v1/text-to-dialogue/stream-input" if dialogue else "/v1/text-to-speech/" + quote(next(iter(session["voices"].values())), safe="") + "/stream-input"
        params = {"model_id": SPEECH[row["model"]], "output_format": session["output_format"], "sync_alignment": "true"}
        upstream = "wss://api.elevenlabs.io" + path + "?" + urlencode(params)
        async with asyncio.timeout(300):
            async with websocket_connect(upstream, additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, max_size=None) as provider:
                await provider.send(json.dumps({"voices": list(session["voices"].values())} if dialogue else {"text": " "}))
                async def send_text():
                    nonlocal characters
                    while True:
                        message = await websocket.receive_json()
                        payload, count, closing = websocket_input(message, model=row["model"], voices=session["voices"], characters=characters, limit=session["max_characters"])
                        await provider.send(json.dumps(payload))
                        characters += count
                        if closing:
                            return
                async def receive_audio():
                    nonlocal completed
                    async for message in provider:
                        payload = json.loads(message)
                        if payload.get("error") or payload.get("type") == "error":
                            fail(502, "AUDIO_STREAM_PROVIDER_FAILED", "The speech stream could not complete.")
                        if payload.get("audio"):
                            raw = base64.b64decode(payload["audio"], validate=True)
                            chunks.append(raw)
                            event = {"type": "audio", "audio_base64": payload["audio"], "output_format": session["output_format"]}
                            alignment = payload.get("alignment") or payload.get("normalizedAlignment")
                            if alignment:
                                alignments.append(alignment)
                                event["alignment"] = alignment
                            await websocket.send_json(event)
                        if payload.get("isFinal") or payload.get("is_final"):
                            completed = True
                            return
                sender, receiver = asyncio.create_task(send_text()), asyncio.create_task(receive_audio())
                try:
                    done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if receiver not in done:
                        await receiver
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
        if not completed:
            fail(502, "AUDIO_STREAM_INCOMPLETE", "The provider closed before completing speech.")
        await receipt(row, None, {"billable_characters": characters})
        data, content_type, _ = await audio_bytes(httpx.Response(200, content=b"".join(chunks)), session["output_format"])
        result = await finish_operation(row, {"alignment_chunks": alignments}, data, content_type)
        persisted = True
        await websocket.send_json({"type": "completed", "request_id": row["request_id"], "accounting_id": row["accounting_id"],
            "result_path": f"/v1/audio/requests/{row['id']}", "content_type": result["content_type"]})
        await websocket.close(code=1000)
    except (Exception, asyncio.CancelledError) as error:
        if persisted:
            return
        if row["accounting_id"]:
            await receipt(row, None, {}, outcome="unknown")
        if chunks:
            try:
                data, content_type, _ = await audio_bytes(httpx.Response(200, content=b"".join(chunks)), session["output_format"])
                await finish_operation(row, {"alignment_chunks": alignments, "incomplete": True, "error_code": "AUDIO_STREAM_INCOMPLETE"}, data, content_type)
            except Exception:
                await mark_failed(row, "AUDIO_STREAM_INCOMPLETE", unknown=characters > 0)
        else:
            await mark_failed(row, "AUDIO_STREAM_INCOMPLETE", unknown=characters > 0)
        try:
            await websocket.send_json({"type": "error", "code": "AUDIO_STREAM_INCOMPLETE", "message": "The stream ended before completion. Retrieve the original request before retrying."})
            await websocket.close(code=1011)
        except (WebSocketDisconnect, RuntimeError):
            pass
        if isinstance(error, asyncio.CancelledError):
            raise
    finally:
        state.reset(context_token)


@router.delete("/v1/audio/owner-data")
async def delete_owner_audio(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    owner = owner_for(user, request.headers)
    pool = await repository.pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("select id from gateway_audio_requests where app_id=$1 and owner_id=$2 for update", *owner)
            await connection.execute("delete from gateway_audio_payloads where request_id in (select id from gateway_audio_requests where app_id=$1 and owner_id=$2)", *owner)
            await connection.execute("update gateway_song_resources set state='deleted',deleted_at=coalesce(deleted_at,now()),composition_plan=null where app_id=$1 and owner_id=$2", *owner)
            await connection.execute("update gateway_speech_sessions set expires_at=now(),voices='{}'::jsonb where id in (select id from gateway_audio_requests where app_id=$1 and owner_id=$2)", *owner)
            await connection.execute("update gateway_audio_requests set state='deleted',response_metadata=null,error_code='AUDIO_RESOURCE_DELETED',updated_at=now() where app_id=$1 and owner_id=$2", *owner)
    return {"deleted": True, "provider_cleanup_supported": False}
