"""Provider adapters for one-shot submit, retrieve, and content operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import socket
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from generation_job_models import GenerationJobCreate, ProviderStatus, ProviderSubmission


RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
ENABLED_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ENABLED_ENV_VALUES


class ProviderAdapterError(Exception):
    """A normalized provider failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        retryable: bool = False,
        outcome_unknown: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.status_code = status_code


@dataclass
class ContentSource:
    url: Optional[str] = None
    content: Optional[bytes] = None
    mime_type: str = "video/mp4"


def _clean_error(value: Any) -> str:
    if isinstance(value, dict):
        error = value.get("error") or value
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "Provider request failed")[:4000]
    return str(value or "Provider request failed")[:4000]


async def _validate_public_https_url(url: str) -> None:
    """Reject non-public media targets before the gateway downloads them."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderAdapterError("Media input must use a public HTTPS URL.", code="INVALID_MEDIA_INPUT")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ProviderAdapterError("Media input host could not be resolved.", code="INVALID_MEDIA_INPUT") from exc
    if not addresses:
        raise ProviderAdapterError("Media input host could not be resolved.", code="INVALID_MEDIA_INPUT")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise ProviderAdapterError("Media input resolved to an invalid address.", code="INVALID_MEDIA_INPUT") from exc
        if not ip.is_global:
            raise ProviderAdapterError("Media input resolved to a non-public address.", code="INVALID_MEDIA_INPUT")


async def _download_media(url: str) -> tuple[str, bytes, str]:
    """Download one trusted-size media input, validating every redirect target."""
    maximum = int(os.environ.get("GENERATION_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    current = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
        for _redirect in range(4):
            await _validate_public_https_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderAdapterError("Media redirect had no destination.", code="INVALID_MEDIA_INPUT")
                    current = urljoin(current, location)
                    continue
                if response.is_error:
                    raise ProviderAdapterError(
                        f"Media input could not be downloaded (HTTP {response.status_code}).",
                        code="INVALID_MEDIA_INPUT",
                    )
                length = response.headers.get("content-length")
                if length:
                    try:
                        if int(length) > maximum:
                            raise ProviderAdapterError("Media input is too large.", code="INVALID_MEDIA_INPUT")
                    except ValueError as exc:
                        raise ProviderAdapterError("Media input length is invalid.", code="INVALID_MEDIA_INPUT") from exc
                chunks: list[bytes] = []
                seen = 0
                async for chunk in response.aiter_bytes(1024 * 1024):
                    seen += len(chunk)
                    if seen > maximum:
                        raise ProviderAdapterError("Media input is too large.", code="INVALID_MEDIA_INPUT")
                    chunks.append(chunk)
                filename = PurePosixPath(unquote(urlsplit(current).path)).name or "media-input"
                mime_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
                return filename, b"".join(chunks), mime_type
    raise ProviderAdapterError("Media input redirected too many times.", code="INVALID_MEDIA_INPUT")


async def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: Optional[dict[str, Any]] = None,
    submission: bool = False,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.request(method, url, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProviderAdapterError(
            "The provider did not return a definitive response.",
            code="SUBMISSION_OUTCOME_UNKNOWN" if submission else "STATUS_RETRIEVAL_TRANSIENT",
            retryable=not submission,
            outcome_unknown=submission,
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"error": {"message": response.text[:2000]}}
    if response.is_error:
        retryable = response.status_code in RETRYABLE_HTTP_STATUSES
        raise ProviderAdapterError(
            _clean_error(data),
            code=(
                "SUBMISSION_OUTCOME_UNKNOWN"
                if submission and retryable
                else "PROVIDER_REJECTED_SUBMISSION"
                if submission
                else "STATUS_RETRIEVAL_TRANSIENT"
                if retryable
                else "PROVIDER_STATUS_ERROR"
            ),
            retryable=retryable and not submission,
            outcome_unknown=submission and retryable,
            status_code=response.status_code,
        )
    if not isinstance(data, dict):
        raise ProviderAdapterError(
            "Provider returned a malformed JSON response.",
            code="SUBMISSION_OUTCOME_UNKNOWN" if submission else "PROVIDER_STATUS_ERROR",
            outcome_unknown=submission,
        )
    return data


class BaseAdapter:
    provider: str

    async def submit(
        self,
        request: GenerationJobCreate,
        *,
        job_id: str,
        callback_url: Optional[str],
        upload_bytes: Optional[dict[str, tuple[str, bytes, str]]] = None,
    ) -> ProviderSubmission:
        raise NotImplementedError

    async def retrieve(self, job: dict[str, Any]) -> ProviderStatus:
        raise NotImplementedError

    async def content(self, job: dict[str, Any]) -> ContentSource:
        if job.get("result_url"):
            return ContentSource(url=str(job["result_url"]), mime_type=job.get("result_mime_type") or "video/mp4")
        raise ProviderAdapterError("Completed job has no content location.", code="CONTENT_NOT_AVAILABLE")


class XAIAdapter(BaseAdapter):
    provider = "xai"
    base_url = "https://api.x.ai/v1"
    usd_ticks_per_dollar = 10_000_000_000

    @staticmethod
    def upstream_model(model: str) -> str:
        aliases = {
            "grok-video": "grok-imagine-video",
            "grok-video-1.5": "grok-imagine-video-1.5",
        }
        return aliases.get(model, model)

    @staticmethod
    def _media_value(media: Any, upload_bytes: Optional[dict[str, tuple[str, bytes, str]]]) -> dict[str, str]:
        if media.url:
            return {"url": media.url}
        if not upload_bytes or media.upload_field not in upload_bytes:
            raise ProviderAdapterError(
                f"Missing multipart field {media.upload_field!r}.", code="INVALID_MEDIA_INPUT"
            )
        filename, content, mime_type = upload_bytes[media.upload_field]
        import base64

        encoded = base64.b64encode(content).decode("ascii")
        return {"url": f"data:{mime_type or 'application/octet-stream'};base64,{encoded}"}

    async def _upload_file(
        self,
        media: Any,
        upload_bytes: Optional[dict[str, tuple[str, bytes, str]]],
        api_key: str,
    ) -> dict[str, str]:
        """Upload Grok 1.5 starting-frame bytes to xAI without exposing its key to clients."""
        if media.upload_field:
            if not upload_bytes or media.upload_field not in upload_bytes:
                raise ProviderAdapterError(
                    f"Missing multipart field {media.upload_field!r}.", code="INVALID_MEDIA_INPUT"
                )
            filename, content, mime_type = upload_bytes[media.upload_field]
        else:
            filename, content, mime_type = await _download_media(str(media.url))
        if not mime_type.startswith("image/"):
            raise ProviderAdapterError("Grok starting frames must be images.", code="INVALID_MEDIA_INPUT")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
                response = await client.post(
                    f"{self.base_url}/files",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, content, mime_type)},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderAdapterError(
                "The Grok starting frame could not be uploaded.", code="PROVIDER_MEDIA_UPLOAD_FAILED"
            ) from exc
        try:
            data = response.json()
        except Exception:
            data = {"error": {"message": response.text[:2000]}}
        if response.is_error:
            raise ProviderAdapterError(_clean_error(data), code="PROVIDER_MEDIA_UPLOAD_FAILED")
        file_id = data.get("id") if isinstance(data, dict) else None
        if not file_id:
            raise ProviderAdapterError(
                "xAI starting-frame upload returned no file ID.", code="PROVIDER_MEDIA_UPLOAD_FAILED"
            )
        return {"file_id": str(file_id)}

    async def submit(self, request: GenerationJobCreate, *, job_id: str, callback_url: Optional[str], upload_bytes=None) -> ProviderSubmission:
        api_key = os.environ.get("GROK_API_KEY")
        if not api_key:
            raise ProviderAdapterError("GROK_API_KEY is not configured.", code="PROVIDER_NOT_CONFIGURED")
        videos = [item for item in request.media_inputs if item.type == "video"]
        images = [item for item in request.media_inputs if item.type == "image"]
        if len(videos) > 1:
            raise ProviderAdapterError("xAI accepts at most one input video.", code="INVALID_MEDIA_INPUT")
        upstream_model = self.upstream_model(request.model)
        is_15 = "1.5" in upstream_model
        if is_15 and videos:
            if not _env_enabled("GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED"):
                raise ProviderAdapterError(
                    "grok-video-1.5 editing and extension are disabled until the exact 1.5 "
                    "endpoint passes the paid staging contract probes; use grok-video.",
                    code="CAPABILITY_NOT_VERIFIED",
                )
        elif is_15 and not _env_enabled("GROK_VIDEO_15_CONTRACT_VERIFIED"):
            raise ProviderAdapterError(
                "grok-video-1.5 generation is disabled until its text, image, reference, "
                "voice, and returned-model contracts pass the paid staging probes.",
                code="CAPABILITY_NOT_VERIFIED",
            )
        if request.duration_seconds is not None and request.duration_seconds > 15:
            raise ProviderAdapterError("xAI video duration must be 15 seconds or less.", code="INVALID_REQUEST")
        if request.operation == "generate" and videos:
            raise ProviderAdapterError("Generate operations cannot include a source video.", code="INVALID_REQUEST")
        if request.operation in {"edit", "extend"} and not videos:
            raise ProviderAdapterError(
                f"xAI {request.operation} operations require one source video.", code="INVALID_REQUEST"
            )
        if request.reference_voice_ids and not is_15:
            raise ProviderAdapterError(
                "Preset voice references require grok-video-1.5.", code="CAPABILITY_NOT_SUPPORTED"
            )
        payload: dict[str, Any] = {"model": upstream_model}
        if request.prompt:
            payload["prompt"] = request.prompt
        if videos:
            endpoint = "/videos/extensions" if request.operation == "extend" else "/videos/edits"
            payload["video"] = self._media_value(videos[0], upload_bytes)
            if images:
                raise ProviderAdapterError("xAI video operations cannot include images.", code="INVALID_MEDIA_INPUT")
            if request.operation == "extend" and request.duration_seconds is not None:
                if not 2 <= request.duration_seconds <= 10:
                    raise ProviderAdapterError(
                        "xAI extension duration must be between 2 and 10 seconds.", code="INVALID_REQUEST"
                    )
                payload["duration"] = request.duration_seconds
            elif request.operation != "extend" and request.duration_seconds is not None:
                raise ProviderAdapterError(
                    "xAI video editing does not accept a custom duration.", code="INVALID_REQUEST"
                )
            if request.aspect_ratio or request.resolution:
                raise ProviderAdapterError(
                    "xAI video editing and extension preserve the source format.", code="INVALID_REQUEST"
                )
        else:
            endpoint = "/videos/generations"
            first = next((item for item in images if item.role == "first_frame"), None)
            references = [item for item in images if item is not first]
            if len(references) > 7:
                raise ProviderAdapterError("xAI accepts at most seven reference images.", code="INVALID_MEDIA_INPUT")
            if first and references:
                raise ProviderAdapterError(
                    "xAI requests cannot combine a starting frame with reference images.", code="INVALID_MEDIA_INPUT"
                )
            if first:
                payload["image"] = (
                    await self._upload_file(first, upload_bytes, api_key)
                    if "1.5" in upstream_model
                    else self._media_value(first, upload_bytes)
                )
            if references:
                payload["reference_images"] = [self._media_value(item, upload_bytes) for item in references]
                if payload.get("prompt") and "<IMAGE_" not in payload["prompt"].upper():
                    placeholders = ", ".join(f"<IMAGE_{index}>" for index in range(1, len(references) + 1))
                    payload["prompt"] = f"{payload['prompt'].rstrip()}\n\nReference images in order: {placeholders}."
            if request.reference_voice_ids:
                payload["reference_audios"] = [
                    {"voice_id": voice_id} for voice_id in request.reference_voice_ids
                ]
                if payload.get("prompt") and "<AUDIO_" not in payload["prompt"].upper():
                    voices = ", ".join(
                        f"<AUDIO_{index}>" for index in range(len(request.reference_voice_ids))
                    )
                    payload["prompt"] = f"{payload['prompt'].rstrip()}\n\nPreset voices in order: {voices}."
            if request.duration_seconds is not None:
                payload["duration"] = request.duration_seconds
            if request.aspect_ratio and not ("1.5" in upstream_model and first):
                payload["aspect_ratio"] = request.aspect_ratio
            if request.resolution:
                if references and request.resolution.lower() == "1080p":
                    raise ProviderAdapterError(
                        "xAI reference-to-video is capped at 720p.", code="INVALID_REQUEST"
                    )
                payload["resolution"] = request.resolution
        data = await _json_request(
            "POST",
            f"{self.base_url}{endpoint}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            body=payload,
            submission=True,
        )
        request_id = data.get("request_id")
        if not request_id:
            raise ProviderAdapterError(
                "xAI accepted the request without returning a request ID.",
                code="SUBMISSION_OUTCOME_UNKNOWN",
                outcome_unknown=True,
            )
        return ProviderSubmission(
            provider_request_id=str(request_id),
            provider_status=str(data.get("status") or "pending"),
            request_metadata={
                "upstream_model": upstream_model,
                "duration_seconds": request.duration_seconds,
                "resolution": request.resolution,
                "image_count": len(images),
                "has_input_video": bool(videos),
                "operation": (
                    request.operation
                    if request.operation != "auto"
                    else "edit"
                    if videos
                    else "generate"
                ),
                "reference_voice_count": len(request.reference_voice_ids),
            },
        )

    @staticmethod
    def _fallback_cost(metadata: dict[str, Any], body: dict[str, Any]) -> Optional[float]:
        duration = ((body.get("video") or {}).get("duration") or metadata.get("duration_seconds") or 8)
        try:
            seconds = int(duration)
        except (TypeError, ValueError):
            return None
        resolution = str(metadata.get("resolution") or "480p").lower()
        model = str(metadata.get("upstream_model") or "")
        price = 0.08 if "1.5" in model else 0.05
        if resolution == "720p":
            price = 0.14 if "1.5" in model else 0.07
        elif resolution == "1080p":
            price = 0.25 if "1.5" in model else 0.07
        image_price = 0.01 if "1.5" in model else 0.002
        input_video_cost = seconds * 0.01 if metadata.get("has_input_video") else 0
        return seconds * price + int(metadata.get("image_count") or 0) * image_price + input_video_cost

    async def retrieve(self, job: dict[str, Any]) -> ProviderStatus:
        api_key = os.environ.get("GROK_API_KEY")
        data = await _json_request(
            "GET",
            f"{self.base_url}/videos/{job['provider_request_id']}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        raw = str(data.get("status") or "pending").lower()
        expected_model = str((job.get("request_metadata") or {}).get("upstream_model") or "")
        returned_model = str(data.get("model") or "")
        if returned_model and expected_model and returned_model != expected_model:
            return ProviderStatus(
                status="failed",
                provider_status=raw,
                error_code="PROVIDER_MODEL_MISMATCH",
                error_message=f"xAI returned model {returned_model!r}; expected {expected_model!r}.",
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        cost: Optional[float] = None
        if usage and usage.get("cost_in_usd_ticks") is not None:
            try:
                cost = int(usage["cost_in_usd_ticks"]) / self.usd_ticks_per_dollar
            except (TypeError, ValueError):
                pass
        if raw == "done":
            video_url = (data.get("video") or {}).get("url")
            if not video_url:
                return ProviderStatus(
                    status="failed", provider_status=raw, error_code="PROVIDER_MALFORMED_RESULT",
                    error_message="xAI completed without a video URL."
                )
            return ProviderStatus(
                status="completed", provider_status=raw, progress=100, result_url=video_url,
                usage=usage, cost_usd=cost if cost is not None else self._fallback_cost(job.get("request_metadata") or {}, data),
            )
        if raw in {"failed", "expired", "cancelled"}:
            error = data.get("error") or {}
            return ProviderStatus(
                status="expired" if raw == "expired" else "cancelled" if raw == "cancelled" else "failed",
                provider_status=raw,
                error_code=str(error.get("code") or f"XAI_{raw.upper()}"),
                error_message=_clean_error(error or raw),
            )
        return ProviderStatus(status="in_progress" if raw not in {"queued", "pending"} else "queued", provider_status=raw, progress=data.get("progress"))


class BytePlusAdapter(BaseAdapter):
    provider = "byteplus"
    default_base = "https://ark.ap-southeast.bytepluses.com/api/v3"

    @staticmethod
    def upstream_model(model: str) -> str:
        aliases = {
            "seedance-2.0": "dreamina-seedance-2-0-260128",
            "seedance-2.0-fast": "dreamina-seedance-2-0-fast-260128",
            "seedance-2": "dreamina-seedance-2-0-260128",
            "seedance": "dreamina-seedance-2-0-260128",
        }
        return aliases.get(model, model.removeprefix("seedance/"))

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key:
            raise ProviderAdapterError("BYTEDANCE_API_KEY is not configured.", code="PROVIDER_NOT_CONFIGURED")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def submit(self, request: GenerationJobCreate, *, job_id: str, callback_url: Optional[str], upload_bytes=None) -> ProviderSubmission:
        if not request.prompt.strip():
            raise ProviderAdapterError("Seedance requires a non-empty prompt.", code="INVALID_REQUEST")
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for media in request.media_inputs:
            media_url = media.url
            if not media_url and upload_bytes and media.upload_field in upload_bytes:
                import base64

                _filename, media_bytes, mime_type = upload_bytes[media.upload_field]
                media_url = f"data:{mime_type or 'application/octet-stream'};base64,{base64.b64encode(media_bytes).decode('ascii')}"
            if not media_url:
                raise ProviderAdapterError("Seedance media input is missing.", code="INVALID_MEDIA_INPUT")
            if media.type == "image":
                content.append({"type": "image_url", "image_url": {"url": media_url}, "role": media.role})
            else:
                if not media_url.startswith("https://"):
                    raise ProviderAdapterError(
                        "Seedance input videos must use an HTTPS URL.", code="INVALID_MEDIA_INPUT"
                    )
                content.append({"type": "video_url", "video_url": {"url": media_url}, "role": "reference_video"})
        upstream_model = self.upstream_model(request.model)
        body: dict[str, Any] = {
            "model": upstream_model,
            "content": content,
            "resolution": request.resolution or "480p",
            "ratio": request.aspect_ratio or "1:1",
            "duration": request.duration_seconds or 4,
            "generate_audio": request.generate_audio,
            "watermark": False,
            "safety_identifier": hashlib.sha256(job_id.encode()).hexdigest()[:32],
        }
        if callback_url:
            body["callback_url"] = callback_url
        base = (os.environ.get("SEEDANCE_ARK_BASE") or self.default_base).rstrip("/")
        data = await _json_request(
            "POST", f"{base}/contents/generations/tasks", headers=self._headers(), body=body, submission=True
        )
        task_id = data.get("id")
        if not task_id:
            raise ProviderAdapterError(
                "BytePlus accepted the request without returning a task ID.",
                code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True,
            )
        return ProviderSubmission(
            provider_request_id=str(task_id),
            provider_status=str(data.get("status") or "queued"),
            request_metadata={"upstream_model": upstream_model, "has_input_video": any(m.type == "video" for m in request.media_inputs)},
        )

    @staticmethod
    def _cost(job: dict[str, Any], data: dict[str, Any]) -> Optional[float]:
        usage = data.get("usage") or {}
        try:
            tokens = int(usage.get("completion_tokens") or usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= 0:
            return None
        metadata = job.get("request_metadata") or {}
        is_fast = "fast" in str(data.get("model") or metadata.get("upstream_model") or "").lower()
        has_video = bool(metadata.get("has_input_video"))
        if is_fast:
            rate = float(os.environ.get("SEEDANCE_PRICE_PER_MTOK_FAST_VIDEO" if has_video else "SEEDANCE_PRICE_PER_MTOK_FAST", "3.30" if has_video else "5.60"))
        else:
            rate = float(os.environ.get("SEEDANCE_PRICE_PER_MTOK_VIDEO" if has_video else "SEEDANCE_PRICE_PER_MTOK", "4.30" if has_video else "7.00"))
        return tokens * rate / 1_000_000.0

    async def retrieve(self, job: dict[str, Any]) -> ProviderStatus:
        base = (os.environ.get("SEEDANCE_ARK_BASE") or self.default_base).rstrip("/")
        data = await _json_request(
            "GET", f"{base}/contents/generations/tasks/{job['provider_request_id']}", headers=self._headers()
        )
        raw = str(data.get("status") or "running").lower()
        if raw == "succeeded":
            video_url = (data.get("content") or {}).get("video_url")
            if not video_url:
                return ProviderStatus(status="failed", provider_status=raw, error_code="PROVIDER_MALFORMED_RESULT", error_message="BytePlus completed without a video URL.")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            return ProviderStatus(status="completed", provider_status=raw, progress=100, result_url=video_url, usage=usage, cost_usd=self._cost(job, data))
        if raw in {"failed", "expired", "cancelled"}:
            error = data.get("error") or {}
            return ProviderStatus(
                status=raw, provider_status=raw,
                error_code=str(error.get("code") or f"BYTEPLUS_{raw.upper()}"),
                error_message=_clean_error(error or raw),
            )
        return ProviderStatus(status="queued" if raw in {"queued", "submitted"} else "in_progress", provider_status=raw, progress=data.get("progress"))


class VertexAdapter(BaseAdapter):
    provider = "vertex"
    omni_model = "gemini-omni-flash-preview"
    omni_input_cost_per_token = 1.5e-6
    omni_output_cost_per_token = 9e-6
    omni_output_video_cost_per_token = 1.75e-5

    @classmethod
    def _is_omni(cls, model: str, metadata: Optional[dict[str, Any]] = None) -> bool:
        normalized = (model or "").strip().lower().removeprefix("vertex_ai/")
        upstream = str((metadata or {}).get("upstream_model") or "").lower().removeprefix("vertex_ai/")
        return normalized in {"gemini-omni-flash", cls.omni_model} or upstream == cls.omni_model

    @staticmethod
    async def _vertex_headers() -> dict[str, str]:
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest

            credentials, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if not credentials.valid or credentials.expired or not credentials.token:
                await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
        except Exception as exc:
            raise ProviderAdapterError(
                "Vertex application-default credentials are unavailable.", code="PROVIDER_NOT_CONFIGURED"
            ) from exc
        return {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"}

    @staticmethod
    def _omni_url(interaction_id: Optional[str] = None) -> str:
        project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        if not project:
            raise ProviderAdapterError(
                "GOOGLE_CLOUD_PROJECT is not configured.", code="PROVIDER_NOT_CONFIGURED"
            )
        base = f"https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/interactions"
        return f"{base}/{interaction_id}" if interaction_id else base

    @staticmethod
    def _mp4_duration_seconds(content: bytes) -> Optional[float]:
        """Read the ISO-BMFF movie header without invoking an external decoder."""

        def boxes(start: int, end: int):
            offset = start
            while offset + 8 <= end:
                size = int.from_bytes(content[offset : offset + 4], "big")
                kind = content[offset + 4 : offset + 8]
                header = 8
                if size == 1:
                    if offset + 16 > end:
                        return
                    size = int.from_bytes(content[offset + 8 : offset + 16], "big")
                    header = 16
                elif size == 0:
                    size = end - offset
                if size < header or offset + size > end:
                    return
                yield kind, offset + header, offset + size
                offset += size

        for kind, payload_start, box_end in boxes(0, len(content)):
            if kind != b"moov":
                continue
            for child_kind, child_start, child_end in boxes(payload_start, box_end):
                if child_kind != b"mvhd" or child_end - child_start < 20:
                    continue
                version = content[child_start]
                if version == 0 and child_end - child_start >= 20:
                    timescale = struct.unpack_from(">I", content, child_start + 12)[0]
                    duration = struct.unpack_from(">I", content, child_start + 16)[0]
                elif version == 1 and child_end - child_start >= 32:
                    timescale = struct.unpack_from(">I", content, child_start + 20)[0]
                    duration = struct.unpack_from(">Q", content, child_start + 24)[0]
                else:
                    return None
                if not timescale or not duration:
                    return None
                return duration / timescale
        return None

    @staticmethod
    async def _omni_media_content(media: Any, upload_bytes) -> tuple[dict[str, str], bytes]:
        if media.upload_field:
            if not upload_bytes or media.upload_field not in upload_bytes:
                raise ProviderAdapterError(
                    f"Missing multipart field {media.upload_field!r}.", code="INVALID_MEDIA_INPUT"
                )
            _filename, content, mime_type = upload_bytes[media.upload_field]
        else:
            _filename, content, mime_type = await _download_media(str(media.url))
        expected = f"{media.type}/"
        if not (mime_type or "").startswith(expected):
            raise ProviderAdapterError(
                f"Gemini Omni {media.type} input has incompatible MIME type {mime_type!r}.",
                code="INVALID_MEDIA_INPUT",
            )
        return {
            "type": media.type,
            "data": base64.b64encode(content).decode("ascii"),
            "mime_type": mime_type,
        }, content

    @staticmethod
    def _omni_video_item(data: dict[str, Any]) -> Optional[dict[str, Any]]:
        for step in reversed(data.get("steps") or []):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for item in reversed(step.get("content") or []):
                if isinstance(item, dict) and item.get("type") == "video":
                    return item
        output = data.get("output_video")
        return output if isinstance(output, dict) else None

    @staticmethod
    def _omni_cost(usage: Any) -> Optional[float]:
        if not isinstance(usage, dict):
            return None
        try:
            input_tokens = int(usage.get("total_input_tokens") or 0)
            output_tokens = int(usage.get("total_output_tokens") or 0)
            thought_tokens = int(usage.get("total_thought_tokens") or 0)
        except (TypeError, ValueError):
            return None
        video_tokens = 0
        for item in usage.get("output_tokens_by_modality") or []:
            if isinstance(item, dict) and str(item.get("modality") or "").lower() == "video":
                try:
                    video_tokens += int(item.get("tokens") or 0)
                except (TypeError, ValueError):
                    return None
        video_tokens = min(max(0, video_tokens), max(0, output_tokens))
        try:
            from litellm import get_model_info

            model_info = get_model_info(
                model="gemini-omni-flash-preview", custom_llm_provider="vertex_ai"
            )
        except Exception:
            model_info = {}
        try:
            input_rate = float(
                model_info.get("input_cost_per_token")
                or os.environ.get("GEMINI_OMNI_INPUT_COST_PER_TOKEN")
                or VertexAdapter.omni_input_cost_per_token
            )
            output_rate = float(
                model_info.get("output_cost_per_token")
                or os.environ.get("GEMINI_OMNI_OUTPUT_COST_PER_TOKEN")
                or VertexAdapter.omni_output_cost_per_token
            )
            video_rate = float(
                model_info.get("output_cost_per_video_token")
                or os.environ.get("GEMINI_OMNI_OUTPUT_VIDEO_COST_PER_TOKEN")
                or VertexAdapter.omni_output_video_cost_per_token
            )
        except (TypeError, ValueError):
            return None
        text_and_thought_tokens = max(0, output_tokens - video_tokens) + max(0, thought_tokens)
        return input_tokens * input_rate + text_and_thought_tokens * output_rate + video_tokens * video_rate

    async def _submit_omni(self, request: GenerationJobCreate, upload_bytes=None) -> ProviderSubmission:
        images = [item for item in request.media_inputs if item.type == "image"]
        videos = [item for item in request.media_inputs if item.type == "video"]
        first_frames = [item for item in images if item.role == "first_frame"]
        references = [item for item in images if item.role != "first_frame"]
        if request.reference_voice_ids:
            raise ProviderAdapterError(
                "Gemini Omni does not support audio references or voice editing.",
                code="CAPABILITY_NOT_SUPPORTED",
            )
        if any(item.role == "last_frame" for item in request.media_inputs):
            raise ProviderAdapterError(
                "Gemini Omni does not support last-frame interpolation.", code="CAPABILITY_NOT_SUPPORTED"
            )
        if len(first_frames) > 1:
            raise ProviderAdapterError("Gemini Omni accepts one first frame.", code="INVALID_MEDIA_INPUT")
        if len(images) > 10:
            raise ProviderAdapterError("Gemini Omni accepts up to ten images.", code="INVALID_MEDIA_INPUT")
        if len(videos) > 1:
            raise ProviderAdapterError("Gemini Omni accepts one source video.", code="INVALID_MEDIA_INPUT")
        if videos and images:
            raise ProviderAdapterError(
                "Gemini Omni video editing cannot include image references.", code="INVALID_MEDIA_INPUT"
            )
        if request.previous_job_id and videos:
            raise ProviderAdapterError(
                "previous_job_id is mutually exclusive with a source video.", code="INVALID_REQUEST"
            )
        if request.previous_job_id and request.media_inputs:
            raise ProviderAdapterError(
                "Stateful Gemini Omni edits accept a prompt but no new media.", code="INVALID_REQUEST"
            )
        if request.operation == "extend":
            raise ProviderAdapterError(
                "Gemini Omni does not support video extension.", code="CAPABILITY_NOT_SUPPORTED"
            )
        has_edit_source = bool(videos or request.previous_job_id)
        if request.operation == "generate" and has_edit_source:
            raise ProviderAdapterError("Generate operations cannot include an edit source.", code="INVALID_REQUEST")
        if request.operation == "edit" and not has_edit_source:
            raise ProviderAdapterError("Gemini Omni edits require source video or previous_job_id.", code="INVALID_REQUEST")
        if request.duration_seconds is not None and not 3 <= request.duration_seconds <= 10:
            raise ProviderAdapterError(
                "Gemini Omni duration must be between 3 and 10 seconds.", code="INVALID_REQUEST"
            )
        if request.resolution and request.resolution.lower() != "720p":
            raise ProviderAdapterError("Gemini Omni output is fixed at 720p.", code="INVALID_REQUEST")
        if request.aspect_ratio and request.aspect_ratio not in {"16:9", "9:16"}:
            raise ProviderAdapterError(
                "Gemini Omni aspect_ratio must be 16:9 or 9:16.", code="INVALID_REQUEST"
            )
        if has_edit_source and request.aspect_ratio:
            raise ProviderAdapterError(
                "Gemini Omni video editing inherits the source aspect ratio; omit aspect_ratio.",
                code="INVALID_REQUEST",
            )
        prompt = request.prompt.strip()
        if not prompt:
            raise ProviderAdapterError("Gemini Omni requires a prompt.", code="INVALID_REQUEST")
        if request.duration_seconds is not None:
            prompt = f"Create exactly a {request.duration_seconds}-second video. {prompt}"

        contents: list[dict[str, str]] = []
        for media in request.media_inputs:
            content, raw = await self._omni_media_content(media, upload_bytes)
            if media.type == "video":
                duration = self._mp4_duration_seconds(raw)
                if duration is None:
                    raise ProviderAdapterError(
                        "Gemini Omni source videos must be MP4 files with readable duration metadata.",
                        code="INVALID_MEDIA_INPUT",
                    )
                if duration > 10:
                    raise ProviderAdapterError(
                        "Gemini Omni source videos must be 10 seconds or shorter.",
                        code="INVALID_MEDIA_INPUT",
                    )
            contents.append(content)
        if images:
            declarations: list[str] = []
            for index, media in enumerate(images, start=1):
                if media.role == "first_frame":
                    declarations.append(f"[# Sources <FIRST_FRAME>@Image{index}]")
                else:
                    ref_index = references.index(media)
                    declarations.append(f"[# References <IMAGE_REF_{ref_index}>@Image{index}]")
            prompt = " ".join(declarations) + " " + prompt
            if first_frames:
                prompt += " Use the first-frame image as the starting frame."
            if references:
                prompt += " Use the other images as references, not as literal initial frames."
        if contents:
            contents.append({"type": "text", "text": prompt})
            interaction_input: Any = [{"type": "user_input", "content": contents}]
        else:
            interaction_input = prompt
        task = (
            "edit"
            if has_edit_source
            else "image_to_video"
            if first_frames and not references
            else "reference_to_video"
            if references
            else "text_to_video"
        )
        response_format: dict[str, Any] = {"type": "video"}
        if not has_edit_source:
            response_format["aspect_ratio"] = request.aspect_ratio or "16:9"
        body: dict[str, Any] = {
            "model": self.omni_model,
            "input": interaction_input,
            "background": True,
            "store": True,
            "stream": False,
            "response_format": response_format,
            "generation_config": {"video_config": {"task": task}},
        }
        if request._previous_interaction_id:
            body["previous_interaction_id"] = request._previous_interaction_id
        data = await _json_request(
            "POST",
            self._omni_url(),
            headers=await self._vertex_headers(),
            body=body,
            submission=True,
        )
        interaction_id = data.get("id")
        if not interaction_id:
            raise ProviderAdapterError(
                "Vertex accepted the Omni request without an interaction ID.",
                code="SUBMISSION_OUTCOME_UNKNOWN",
                outcome_unknown=True,
            )
        return ProviderSubmission(
            provider_request_id=str(interaction_id),
            provider_status=str(data.get("status") or "in_progress"),
            progress=100 if data.get("status") == "completed" else None,
            request_metadata={
                "upstream_model": self.omni_model,
                "operation": request.operation if request.operation != "auto" else task,
                "previous_job_id": request.previous_job_id,
                "duration_seconds": request.duration_seconds,
                "resolution": "720p",
                "aspect_ratio": request.aspect_ratio if not has_edit_source else None,
                "image_count": len(images),
                "has_input_video": bool(videos),
            },
        )

    async def _retrieve_omni(self, job: dict[str, Any]) -> ProviderStatus:
        data = await _json_request(
            "GET",
            self._omni_url(str(job["provider_request_id"])),
            headers=await self._vertex_headers(),
        )
        raw = str(data.get("status") or "in_progress").lower()
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        if raw == "completed":
            video = self._omni_video_item(data)
            if not video or not (video.get("data") or video.get("uri")):
                return ProviderStatus(
                    status="failed",
                    provider_status=raw,
                    error_code="PROVIDER_MALFORMED_RESULT",
                    error_message="Gemini Omni completed without video content.",
                )
            return ProviderStatus(
                status="completed",
                provider_status=raw,
                progress=100,
                result_mime_type=str(video.get("mime_type") or "video/mp4"),
                usage=usage,
                cost_usd=self._omni_cost(usage),
            )
        if raw in {"failed", "cancelled", "incomplete"}:
            error = data.get("error") or data.get("incomplete_details") or {}
            return ProviderStatus(
                status="cancelled" if raw == "cancelled" else "failed",
                provider_status=raw,
                error_code=str(error.get("code") or f"OMNI_{raw.upper()}"),
                error_message=_clean_error(error or raw),
            )
        if raw == "requires_action":
            return ProviderStatus(
                status="failed",
                provider_status=raw,
                error_code="OMNI_REQUIRES_ACTION",
                error_message="Gemini Omni returned an unsupported requires_action state.",
            )
        return ProviderStatus(
            status="queued" if raw == "queued" else "in_progress",
            provider_status=raw,
            progress=data.get("progress"),
            usage=usage,
        )

    async def _omni_content(self, job: dict[str, Any]) -> ContentSource:
        data = await _json_request(
            "GET",
            self._omni_url(str(job["provider_request_id"])),
            headers=await self._vertex_headers(),
        )
        video = self._omni_video_item(data)
        if not video:
            raise ProviderAdapterError("Gemini Omni video content is unavailable.", code="CONTENT_NOT_AVAILABLE")
        mime_type = str(video.get("mime_type") or "video/mp4")
        encoded = video.get("data")
        if encoded:
            try:
                return ContentSource(content=base64.b64decode(encoded, validate=True), mime_type=mime_type)
            except (ValueError, TypeError) as exc:
                raise ProviderAdapterError(
                    "Gemini Omni returned malformed video data.", code="CONTENT_RETRIEVAL_FAILED"
                ) from exc
        uri = str(video.get("uri") or "")
        if not uri.startswith("https://"):
            raise ProviderAdapterError(
                "Gemini Omni returned an unsupported video URI.", code="CONTENT_RETRIEVAL_FAILED"
            )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), follow_redirects=True) as client:
                response = await client.get(uri, headers=await self._vertex_headers())
                response.raise_for_status()
                return ContentSource(
                    content=response.content,
                    mime_type=response.headers.get("content-type", mime_type).split(";", 1)[0],
                )
        except httpx.HTTPError as exc:
            raise ProviderAdapterError(
                "Gemini Omni video download failed.", code="CONTENT_RETRIEVAL_FAILED", retryable=True
            ) from exc

    @staticmethod
    def _router():
        from litellm.proxy.proxy_server import llm_router

        if llm_router is None:
            raise ProviderAdapterError("LiteLLM router is not initialized.", code="PROVIDER_NOT_CONFIGURED")
        return llm_router

    @staticmethod
    async def _input_reference(request: GenerationJobCreate, upload_bytes):
        media = next((item for item in request.media_inputs if item.type == "image"), None)
        if not media:
            return None
        if media.upload_field:
            if not upload_bytes or media.upload_field not in upload_bytes:
                raise ProviderAdapterError(f"Missing multipart field {media.upload_field!r}.", code="INVALID_MEDIA_INPUT")
            filename, content, _mime = upload_bytes[media.upload_field]
            stream = BytesIO(content)
            stream.name = filename
            return stream
        try:
            filename, content, mime_type = await _download_media(str(media.url))
        except ProviderAdapterError as exc:
            raise ProviderAdapterError("Could not fetch the Veo input reference.", code="INVALID_MEDIA_INPUT") from exc
        if not mime_type.startswith("image/"):
            raise ProviderAdapterError("Veo input references must be images.", code="INVALID_MEDIA_INPUT")
        stream = BytesIO(content)
        stream.name = filename or "reference-image"
        return stream

    async def submit(self, request: GenerationJobCreate, *, job_id: str, callback_url: Optional[str], upload_bytes=None) -> ProviderSubmission:
        if self._is_omni(request.model):
            return await self._submit_omni(request, upload_bytes)
        if request.previous_job_id or request.reference_voice_ids or request.operation not in {"auto", "generate"}:
            raise ProviderAdapterError(
                "The selected Veo alias does not support this durable-job operation.",
                code="CAPABILITY_NOT_SUPPORTED",
            )
        if len(request.media_inputs) > 1:
            raise ProviderAdapterError("Veo accepts at most one input reference.", code="INVALID_MEDIA_INPUT")
        if any(media.type != "image" for media in request.media_inputs):
            raise ProviderAdapterError("Veo only accepts an image input reference.", code="INVALID_MEDIA_INPUT")
        try:
            response = await self._router().avideo_generation(
                model=request.model,
                prompt=request.prompt,
                input_reference=await self._input_reference(request, upload_bytes),
                seconds=str(request.duration_seconds) if request.duration_seconds else None,
                size=request.resolution,
                timeout=60,
                extra_body={
                    key: value for key, value in {
                        "aspect_ratio": request.aspect_ratio,
                        "generate_audio": request.generate_audio,
                    }.items() if value is not None
                },
            )
        except Exception as exc:
            raise ProviderAdapterError(
                "Veo submission did not return a definitive response.",
                code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True,
            ) from exc
        data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        if not data.get("id"):
            raise ProviderAdapterError("Veo returned no video ID.", code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True)
        return ProviderSubmission(
            provider_request_id=str(data["id"]),
            provider_status=str(data.get("status") or "queued"),
            progress=data.get("progress"),
            request_metadata={"upstream_model": data.get("model") or request.model},
        )

    async def retrieve(self, job: dict[str, Any]) -> ProviderStatus:
        if self._is_omni(str(job.get("model") or ""), job.get("request_metadata") or {}):
            return await self._retrieve_omni(job)
        try:
            response = await self._router().avideo_status(video_id=job["provider_request_id"], model=job["model"], timeout=30)
        except Exception as exc:
            raise ProviderAdapterError("Veo status retrieval failed.", code="STATUS_RETRIEVAL_TRANSIENT", retryable=True) from exc
        data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        raw = str(data.get("status") or "in_progress").lower()
        if raw in {"completed", "succeeded"}:
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            cost = None
            hidden = getattr(response, "_hidden_params", {}) or {}
            try:
                cost = float(hidden["response_cost"]) if hidden.get("response_cost") is not None else None
            except (TypeError, ValueError):
                pass
            return ProviderStatus(status="completed", provider_status=raw, progress=100, usage=usage, cost_usd=cost)
        if raw in {"failed", "expired", "cancelled"}:
            error = data.get("error") or {}
            return ProviderStatus(status=raw, provider_status=raw, error_code=str(error.get("code") or f"VEO_{raw.upper()}"), error_message=_clean_error(error or raw))
        return ProviderStatus(status="queued" if raw == "queued" else "in_progress", provider_status=raw, progress=data.get("progress"))

    async def content(self, job: dict[str, Any]) -> ContentSource:
        if self._is_omni(str(job.get("model") or ""), job.get("request_metadata") or {}):
            return await self._omni_content(job)
        try:
            response = await self._router().avideo_content(video_id=job["provider_request_id"], model=job["model"], timeout=60)
        except Exception as exc:
            raise ProviderAdapterError("Veo content retrieval failed.", code="CONTENT_RETRIEVAL_FAILED", retryable=True) from exc
        if isinstance(response, bytes):
            return ContentSource(content=response)
        if hasattr(response, "content") and isinstance(response.content, bytes):
            return ContentSource(content=response.content, mime_type=response.headers.get("content-type", "video/mp4"))
        raise ProviderAdapterError("Veo returned an unsupported content response.", code="CONTENT_RETRIEVAL_FAILED")


_ADAPTERS: dict[str, BaseAdapter] = {
    "xai": XAIAdapter(),
    "byteplus": BytePlusAdapter(),
    "vertex": VertexAdapter(),
}


def provider_for_model(model: str) -> str:
    normalized = model.strip().lower()
    if normalized.startswith(("grok-video", "grok-imagine-video")):
        return "xai"
    if normalized.startswith(("seedance", "dreamina-seedance")):
        return "byteplus"
    if normalized.startswith("veo-") or normalized.startswith("vertex_ai/veo-"):
        return "vertex"
    if normalized in {
        "gemini-omni-flash",
        "gemini-omni-flash-preview",
        "vertex_ai/gemini-omni-flash-preview",
    }:
        return "vertex"
    raise ProviderAdapterError(
        f"Model {model!r} is not enabled for durable video jobs.", code="UNSUPPORTED_MODEL"
    )


def adapter_for(provider: str) -> BaseAdapter:
    return _ADAPTERS[provider]
