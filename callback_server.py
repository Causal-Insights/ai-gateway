"""Minimal public BytePlus callback receiver."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from generation_job_models import TERMINAL_STATUSES
from generation_job_repository import repository
from generation_job_scheduler import enqueue_poll


logger = logging.getLogger("ai_gateway.callbacks")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Warm the database pool and apply migrations before the public receiver is
    # marked ready; callback request handling then stays comfortably bounded.
    await repository.pool()
    try:
        yield
    finally:
        await repository.close()


app = FastAPI(
    title="AI Gateway Callbacks",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/callbacks/byteplus/{job_id}", status_code=202)
async def byteplus_callback(
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = "",
) -> dict[str, bool]:
    maximum = int(os.environ.get("GENERATION_CALLBACK_MAX_BYTES", "65536"))
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum:
                raise HTTPException(413, "Callback payload is too large.")
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length.") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise HTTPException(413, "Callback payload is too large.")
    try:
        data = __import__("json").loads(bytes(body) or b"{}")
    except Exception as exc:
        raise HTTPException(400, "Malformed callback JSON.") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "Callback JSON must be an object.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    # The public callback is only a wake-up signal. Never trust its status;
    # the private poll worker verifies the terminal state with BytePlus.
    job = await repository.record_callback(job_id, token_hash)
    if not job:
        raise HTTPException(404, "Callback job or token was not found.")
    async def enqueue_verification() -> None:
        try:
            await enqueue_poll(job_id, datetime.now(timezone.utc))
        except Exception:
            logger.exception("callback_verification_enqueue_failed", extra={"generation_job_id": job_id})

    if job.get("status") not in TERMINAL_STATUSES:
        background_tasks.add_task(enqueue_verification)
    return {"accepted": True}
