"""Model-scoped request policies applied before LiteLLM route handling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


FIXED_REASONING_EFFORT = {
    "gpt-5.6-sol-medium": "medium",
    "gpt-5.6-terra-medium": "medium",
    "gpt-5.6-luna-medium": "medium",
    "gpt-5.6-luna-high": "high",
}
GEMINI_FLASH_MODELS = {"gemini-3.6-flash", "gemini-3.5-flash-lite"}
OMNI_MODELS = {"gemini-omni-flash", "gemini-omni-flash-preview"}
CHAT_PATHS = {"/chat/completions", "/v1/chat/completions"}
RESPONSES_PATHS = {"/responses", "/v1/responses"}
POLICY_PATHS = CHAT_PATHS | RESPONSES_PATHS
GEMINI_UNSUPPORTED_PARAMS = {
    "temperature",
    "top_p",
    "top_k",
    "topK",
    "frequency_penalty",
    "presence_penalty",
}


@dataclass(frozen=True)
class PolicyError:
    code: str
    message: str
    status_code: int = 400


def _reasoning_efforts(body: dict[str, Any]) -> list[str]:
    values: list[str] = []
    direct = body.get("reasoning_effort")
    if direct is not None:
        values.append(str(direct).strip().lower())
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        values.append(str(reasoning["effort"]).strip().lower())
    return values


def _strip_gemini_params(body: dict[str, Any]) -> None:
    for key in GEMINI_UNSUPPORTED_PARAMS:
        body.pop(key, None)
    extra_body = body.get("extra_body")
    if isinstance(extra_body, dict):
        for key in GEMINI_UNSUPPORTED_PARAMS:
            extra_body.pop(key, None)
    generation_config = body.get("generation_config")
    if isinstance(generation_config, dict):
        for key in GEMINI_UNSUPPORTED_PARAMS:
            generation_config.pop(key, None)


def apply_request_policy(path: str, body: Any) -> tuple[Any, PolicyError | None]:
    """Return a policy-normalized request body or a client-facing error."""
    if path not in POLICY_PATHS or not isinstance(body, dict):
        return body, None
    model = str(body.get("model") or "").strip()
    if model in OMNI_MODELS:
        return body, PolicyError(
            code="OMNI_REQUIRES_DURABLE_JOB",
            message=(
                f"{model} is a video generation model. Submit it through "
                "/v1/generation-jobs instead of Chat Completions or Responses."
            ),
        )
    fixed_effort = FIXED_REASONING_EFFORT.get(model)
    if fixed_effort:
        supplied = _reasoning_efforts(body)
        if any(value != fixed_effort for value in supplied):
            return body, PolicyError(
                code="FIXED_REASONING_EFFORT",
                message=f"{model} always uses reasoning effort {fixed_effort!r}.",
            )
        if path in RESPONSES_PATHS:
            reasoning = dict(body.get("reasoning") or {})
            reasoning["effort"] = fixed_effort
            body["reasoning"] = reasoning
            body.pop("reasoning_effort", None)
        else:
            body["reasoning_effort"] = fixed_effort
    if model in GEMINI_FLASH_MODELS:
        _strip_gemini_params(body)
    return body, None


class GatewayRequestPolicyMiddleware:
    """Small ASGI middleware that rewrites only JSON inference requests."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in POLICY_PATHS:
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                await self.app(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw = b"".join(chunks)
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if parsed is not None:
            parsed, error = apply_request_policy(str(scope.get("path")), parsed)
            if error:
                payload = json.dumps(
                    {"error": {"type": "invalid_request_error", "code": error.code, "message": error.message}},
                    separators=(",", ":"),
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": error.status_code,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return
            raw = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode()
        headers = [(key, value) for key, value in scope.get("headers", []) if key.lower() != b"content-length"]
        headers.append((b"content-length", str(len(raw)).encode()))
        policy_scope = dict(scope)
        policy_scope["headers"] = headers
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                # Streaming handlers continue reading the ASGI receive channel
                # to detect a real client disconnect. Returning a synthetic
                # disconnect here cancels every otherwise-healthy stream.
                return await receive()
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}

        await self.app(policy_scope, replay, send)
