"""Model-scoped request policies applied before LiteLLM route handling."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any


FIXED_REASONING_EFFORT = {
    "gpt-5.6-sol-medium": "medium",
    "gpt-5.6-terra-medium": "medium",
    "gpt-5.6-luna-medium": "medium",
    "gpt-5.6-luna-high": "high",
}
GEMINI_FLASH_MODELS = {"gemini-3.7-flash", "gemini-3.5-flash-lite"}
OMNI_MODELS = {"gemini-omni-flash", "gemini-omni-flash-preview"}
CHAT_PATHS = {"/chat/completions", "/v1/chat/completions"}
RESPONSES_PATHS = {"/responses", "/v1/responses"}
POLICY_PATHS = CHAT_PATHS | RESPONSES_PATHS
IMAGE_PATHS = {"/images/generations", "/v1/images/generations", "/images/edits", "/v1/images/edits"}
GROK_IMAGE_2_MODELS = {"grok-imagine-image-2.0", "grok-image/grok-imagine-image-2.0"}
GROK_IMAGE_2_QUALITIES = {"low", "medium"}
GROK_IMAGE_2_RESOLUTIONS = {"1k", "2k"}
GROK_IMAGE_2_ASPECT_RATIOS = {
    "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2",
    "19.5:9", "9:19.5", "20:9", "9:20", "auto",
}
GROK_IMAGE_RESPONSE_FORMATS = {"url", "b64_json"}
SEEDREAM_IMAGE_MODELS = {
    "seedream-5.0",
    "seedream-5.0-lite",
    "seedream-5.0-pro",
    "seedream/seedream-5-0-260128",
    "seedream/seedream-5-0-lite-260128",
    "seedream/dola-seedream-5-0-pro-260628",
}
SEEDREAM_PRO_MODELS = {
    "seedream-5.0-pro",
    "seedream/dola-seedream-5-0-pro-260628",
}
SEEDREAM_PRESERVED_IMAGE_PARAMS = {
    "size": "seedream_size",
    "n": "seedream_output_count",
    "response_format": "seedream_response_format",
    "output_format": "seedream_output_format",
    "stream": "seedream_stream",
}
SEEDANCE_DURABLE_ONLY_MODELS = {
    "seedance-2.5",
    "seedance/dreamina-seedance-2-5-260628",
}
GEMINI_UNSUPPORTED_PARAMS = {
    "candidate_count",
    "temperature",
    "thinking_budget",
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


def _normalize_grok_image_2_size(body: dict[str, Any]) -> PolicyError | None:
    raw_size = body.get("size")
    if raw_size is None:
        return None
    size = str(raw_size).strip().lower()
    if size in GROK_IMAGE_2_RESOLUTIONS:
        body.setdefault("resolution", size)
        body.pop("size", None)
        return None
    if size in GROK_IMAGE_2_ASPECT_RATIOS:
        body.setdefault("aspect_ratio", size)
        body.pop("size", None)
        return None
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if match:
        width, height = (int(value) for value in match.groups())
        if width > 0 and height > 0:
            divisor = math.gcd(width, height)
            aspect_ratio = f"{width // divisor}:{height // divisor}"
            if aspect_ratio in GROK_IMAGE_2_ASPECT_RATIOS:
                body.setdefault("aspect_ratio", aspect_ratio)
                body.setdefault("resolution", "2k" if max(width, height) >= 2000 else "1k")
                body.pop("size", None)
                return None
    return PolicyError(
        code="INVALID_IMAGE_SIZE",
        message=(
            "size must be 1k, 2k, a supported aspect ratio, or dimensions whose "
            "ratio is supported by grok-imagine-image-2.0."
        ),
    )


def _preserve_seedream_image_params(body: dict[str, Any]) -> None:
    """Copy fields LiteLLM consumes before custom-provider dispatch.

    The private copies are model-scoped and are reconstructed by the Seedream
    handler. Keeping the public fields in place preserves the OpenAI-compatible
    request contract for policy validation and logging.
    """
    for public_key, private_key in SEEDREAM_PRESERVED_IMAGE_PARAMS.items():
        if public_key in body:
            body[private_key] = body[public_key]


def _seedream_reference_count(body: dict[str, Any]) -> int:
    for key in (
        "image_urls",
        "images",
        "image",
        "reference_image_urls",
        "referenceImageUrls",
    ):
        value = body.get(key)
        if isinstance(value, list):
            return len([item for item in value if item])
        if isinstance(value, str) and value.strip():
            return 1
    return 0


def _validate_seedream_pro_request(body: dict[str, Any]) -> PolicyError | None:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return PolicyError(
            code="INVALID_IMAGE_PROMPT",
            message="prompt must be a non-empty string.",
        )

    raw_size = str(body.get("size") or "1K").strip().lower()
    if raw_size not in {"1k", "2k"}:
        match = re.fullmatch(r"(\d+)x(\d+)", raw_size)
        if not match:
            return PolicyError(
                code="INVALID_IMAGE_SIZE",
                message="seedream-5.0-pro size must be 1K, 2K, or valid pixel dimensions.",
            )
        width, height = (int(value) for value in match.groups())
        pixels = width * height
        ratio = width / height if height else 0
        if not (921_600 <= pixels <= 4_624_220 and 1 / 16 <= ratio <= 16):
            return PolicyError(
                code="INVALID_IMAGE_SIZE",
                message=(
                    "seedream-5.0-pro pixel dimensions must satisfy its published "
                    "pixel and aspect-ratio limits."
                ),
            )

    output_count = body.get("n", 1)
    if isinstance(output_count, bool) or output_count != 1:
        return PolicyError(
            code="INVALID_IMAGE_COUNT",
            message="seedream-5.0-pro supports exactly one output image per request.",
        )
    output_format = str(body.get("output_format") or "png").strip().lower()
    if output_format not in {"png", "jpeg", "jpg"}:
        return PolicyError(
            code="INVALID_IMAGE_OUTPUT_FORMAT",
            message="seedream-5.0-pro output_format must be png or jpeg.",
        )
    if body.get("stream") is True:
        return PolicyError(
            code="IMAGE_STREAMING_UNSUPPORTED",
            message="seedream-5.0-pro does not support streaming output.",
        )
    sequential = body.get("sequential_image_generation")
    if sequential is not None and sequential is not False and sequential != "disabled":
        return PolicyError(
            code="SEQUENTIAL_IMAGE_GENERATION_UNSUPPORTED",
            message="seedream-5.0-pro does not support sequential multi-image generation.",
        )
    if _seedream_reference_count(body) > 10:
        return PolicyError(
            code="TOO_MANY_REFERENCE_IMAGES",
            message="seedream-5.0-pro supports up to 10 reference images.",
        )
    return None


def apply_request_policy(path: str, body: Any) -> tuple[Any, PolicyError | None]:
    """Return a policy-normalized request body or a client-facing error."""
    if not isinstance(body, dict):
        return body, None
    model = str(body.get("model") or "").strip()
    if path in IMAGE_PATHS and model in GROK_IMAGE_2_MODELS:
        size_error = _normalize_grok_image_2_size(body)
        if size_error is not None:
            return body, size_error
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return body, PolicyError(
                code="INVALID_IMAGE_PROMPT",
                message="prompt must be a non-empty string.",
            )
        quality = str(body.get("quality") or "medium").strip().lower()
        if quality not in GROK_IMAGE_2_QUALITIES:
            return body, PolicyError(
                code="INVALID_IMAGE_QUALITY",
                message="quality must be low or medium for grok-imagine-image-2.0.",
            )
        resolution = str(body.get("resolution") or "1k").strip().lower()
        if resolution not in GROK_IMAGE_2_RESOLUTIONS:
            return body, PolicyError(
                code="INVALID_IMAGE_RESOLUTION",
                message="resolution must be 1k or 2k for grok-imagine-image-2.0.",
            )
        response_format = str(body.get("response_format") or "url").strip().lower()
        if response_format not in GROK_IMAGE_RESPONSE_FORMATS:
            return body, PolicyError(
                code="INVALID_IMAGE_RESPONSE_FORMAT",
                message="response_format must be url or b64_json.",
            )
        output_count = body.get("n", 1)
        if isinstance(output_count, bool) or not isinstance(output_count, int) or not 1 <= output_count <= 10:
            return body, PolicyError(
                code="INVALID_IMAGE_COUNT",
                message="n must be an integer from 1 through 10.",
            )
        for public_key, private_key in (
            ("n", "xai_output_count"),
            ("quality", "xai_render_quality"),
            ("response_format", "xai_response_format"),
        ):
            if public_key in body:
                body[private_key] = body[public_key]
        return body, None
    if path in IMAGE_PATHS and model in SEEDANCE_DURABLE_ONLY_MODELS:
        return body, PolicyError(
            code="SEEDANCE_REQUIRES_DURABLE_JOB",
            message=(
                "seedance-2.5 must be submitted through /v1/generation-jobs; "
                "the legacy Images route is not supported for this model."
            ),
        )
    if path in IMAGE_PATHS and model in SEEDREAM_IMAGE_MODELS:
        if model in SEEDREAM_PRO_MODELS:
            validation_error = _validate_seedream_pro_request(body)
            if validation_error is not None:
                return body, validation_error
        _preserve_seedream_image_params(body)
        return body, None
    if path not in POLICY_PATHS:
        return body, None
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
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in POLICY_PATHS | IMAGE_PATHS:
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
