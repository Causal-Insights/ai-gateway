"""OpenAI Images SSE with the Gateway's existing authentication and accounting."""
import json
import logging
import os

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.proxy._types import ProxyException
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from starlette.datastructures import UploadFile

from gateway_accounting import GatewayAccounting, accounting, capture, finalize_reservation, state

stream_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
FIELDS = {"prompt", "n", "size", "quality", "background", "output_format", "output_compression",
          "moderation", "partial_images", "stream", "input_fidelity", "user", "image", "mask"}
log = logging.getLogger(__name__)


@stream_app.exception_handler(ProxyException)
async def auth_error(request, exc):
    # This separately dispatched ASGI app does not inherit the proxy's handlers.
    from litellm.proxy.proxy_server import openai_exception_handler
    return await openai_exception_handler(request, exc)


def _sum_usage(left, right):
    """Completed image events report token usage; partial images never contribute."""
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict):
            result[key] = _sum_usage(result.get(key, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = result.get(key, 0) + value
    return result


class _ImageStreamResponse(StreamingResponse):
    def __init__(self, events, finish):
        super().__init__(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        self.finish = finish

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Covers cancellation before the iterator starts and failed client sends.
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.finish()


@stream_app.post("/images/generations")
@stream_app.post("/v1/images/generations")
@stream_app.post("/images/edits")
@stream_app.post("/v1/images/edits")
async def stream_images(request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    current = state.get()
    if current is None:
        current = {}
        state.set(current)
    current["reservation"] = user.budget_reservation
    client = response = attempt = None
    evidence = {}
    outcome = "incomplete"

    async def finish():
        try:
            if attempt is not None:
                await capture(attempt, evidence, outcome=outcome)
            if current.get("accounting_id"):
                await accounting.finish(current["accounting_id"])
        finally:
            try:
                await finalize_reservation(current)
            finally:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    if client is not None:
                        await client.aclose()

    async def finish_safely():
        # Persistence failure must not discard an otherwise valid generated image.
        with anyio.CancelScope(shield=True):
            try:
                await finish()
            except Exception:
                log.exception("Images stream finalization failed")

    try:
        edit = request.url.path.endswith("/edits")
        multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
        files = []
        if multipart:
            async with request.form(max_files=32, max_fields=32, max_part_size=64 * 1024 * 1024) as form:
                body = {}
                for name, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        if name in {"image", "image[]", "mask"}:
                            files.append((name, (value.filename or name, await value.read(), value.content_type)))
                    else:
                        body[name] = value
            for name in ("n", "partial_images", "output_compression"):
                if name in body:
                    try:
                        body[name] = int(body[name])
                    except ValueError:
                        raise HTTPException(400, f"{name} must be an integer") from None
            body["stream"] = True
        else:
            body = await request.json()
        route = "image_edit" if edit else "image_generation"
        await GatewayAccounting().async_pre_call_hook(user, None, body, route)
        profile = current["profile"]
        upstream = profile["upstream_model"].removeprefix("openai/")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(503, "Image provider credentials are unavailable")
        payload = {key: value for key, value in body.items() if key in FIELDS}
        payload.update(model=upstream, stream=True)
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20))
        args = {"json": payload}
        if multipart:
            args = {"data": {key: str(value).lower() if isinstance(value, bool) else str(value)
                             for key, value in payload.items()}, "files": files}
        upstream_request = client.build_request(
            "POST", "https://api.openai.com/v1/images/" + ("edits" if edit else "generations"),
            headers={"Authorization": "Bearer " + api_key}, **args)
        attempt = await accounting.attempt(current["accounting_id"], profile)
        current["attempts"].append(attempt)
        response = await client.send(upstream_request, stream=True)
        if response.is_error:
            await response.aread()
            try:
                detail = response.json()
            except ValueError:
                detail = {"error": {"message": "Image provider request failed."}}
            evidence = detail
            outcome = "failure"
            await finish_safely()
            return JSONResponse(detail, status_code=response.status_code)
    except BaseException:
        await finish_safely()
        raise

    async def events():
        nonlocal evidence, outcome
        pending = []
        usage = {}
        usage_complete = True

        def record_event():
            nonlocal evidence, outcome, usage, usage_complete
            raw = "\n".join(pending)
            pending.clear()
            try:
                event = json.loads(raw)
            except ValueError:
                return
            if not isinstance(event, dict) or event.get("type") not in {
                "image_generation.completed", "image_edit.completed"
            }:
                return
            if isinstance(event.get("usage"), dict) and event["usage"]:
                usage = _sum_usage(usage, event["usage"])
            else:
                usage_complete = False
            # Pricing a subtotal as the whole request would hide an unmetered
            # completed image. Retain its known subtotal for reconciliation.
            recorded = usage if usage_complete else {"gateway_image_partial_usage": usage}
            evidence = {"usage": recorded, "model": upstream, "id": response.headers.get("x-request-id")}
            outcome = "success"

        try:
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    pending.append(line[5:].lstrip())
                elif line == "" and pending:
                    record_event()
                yield (line + "\n").encode()
            if pending:
                # Some providers close immediately after the final data line.
                record_event()
                yield b"\n"
        finally:
            # A client may disconnect after the completed data line, before its
            # separator. Preserve that confirmed usage while closing the stream.
            if pending:
                record_event()

    return _ImageStreamResponse(events(), finish_safely)
