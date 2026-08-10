"""xAI Grok video custom LiteLLM handler."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

import httpx
from litellm import CustomLLM
from litellm.types.utils import ImageObject, ImageResponse

if TYPE_CHECKING:
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

from custom_handler_common import normalize_error
from legacy_usage import log_legacy_video_usage


class GrokVideoException(Exception):
    """Raised when xAI Grok video generation submission or polling fails."""

    pass


class GrokImageException(Exception):
    """Raised when xAI image generation or edit calls fail."""

    pass


class GrokVideoLLM(CustomLLM):
    """
    Wraps xAI's video generation and edit endpoints.
    API docs: https://docs.x.ai/developers/model-capabilities/video
    Endpoints:
      - https://api.x.ai/v1/videos/generations
      - https://api.x.ai/v1/videos/edits
    Auth: GROK_API_KEY

    Billing: prefers ``usage.cost_in_usd_ticks`` from xAI poll responses; falls back to
    duration × per-second rate by resolution (see ``litellm_config.yaml`` / env overrides).
    """

    XAI_BASE = "https://api.x.ai/v1"
    POLL_INTERVAL = 3   # seconds between status checks
    POLL_TIMEOUT = 600  # maximum seconds to wait
    MAX_REFERENCE_IMAGES = 7
    MAX_REFERENCE_DURATION = 10
    DEFAULT_XAI_MODEL = "grok-imagine-video"

    # xAI: 1 USD = 10_000_000_000 ticks (see GET /v1/videos/{request_id} usage)
    USD_TICKS_PER_DOLLAR = 10_000_000_000
    DEFAULT_PRICE_PER_SECOND_480P = 0.05
    DEFAULT_PRICE_PER_SECOND_720P = 0.07
    DEFAULT_PRICE_PER_SECOND_1080P = 0.07
    DEFAULT_PRICE_PER_REFERENCE_IMAGE = 0.002
    DEFAULT_PRICE_PER_SECOND_480P_15 = 0.08
    DEFAULT_PRICE_PER_SECOND_720P_15 = 0.14
    DEFAULT_PRICE_PER_SECOND_1080P_15 = 0.25
    DEFAULT_PRICE_PER_REFERENCE_IMAGE_15 = 0.01

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        m = (model or "").strip()
        if m.startswith("grok-video/"):
            return m[len("grok-video/") :].strip()
        return m

    def _resolve_upstream_model(self, model: str) -> str:
        stripped = self._strip_provider_prefix(model)
        if stripped and stripped != "grok-video":
            return stripped
        return os.environ.get("GROK_VIDEO_MODEL") or self.DEFAULT_XAI_MODEL

    @staticmethod
    def _is_video_15_model(upstream_model: str) -> bool:
        return "1.5" in (upstream_model or "").lower()

    @staticmethod
    def _env_enabled(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _coerce_int(name: str, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            s = value.strip()
            if s.isdigit():
                return int(s)
        raise ValueError(f"{name} must be an integer")

    @staticmethod
    def _normalize_media_object(name: str, value: Any) -> dict:
        if isinstance(value, str) and value.strip():
            return {"url": value.strip()}
        if isinstance(value, dict):
            if "image_url" in value and "url" not in value:
                value = {**value, "url": value.get("image_url")}
            out = {}
            if value.get("url"):
                out["url"] = str(value.get("url")).strip()
            if value.get("file_id"):
                out["file_id"] = str(value.get("file_id")).strip()
            if out.get("url") and out.get("file_id"):
                raise ValueError(f"{name} must include either url or file_id, not both")
            if not out:
                raise ValueError(f"{name} must include url or file_id")
            return out
        raise ValueError(f"{name} must be a url string or object with url/file_id")

    def _normalize_reference_images(self, raw_reference_images: Any) -> list:
        if raw_reference_images is None:
            return []
        if not isinstance(raw_reference_images, list) or len(raw_reference_images) == 0:
            raise ValueError("reference_images must be a non-empty list")
        if len(raw_reference_images) > self.MAX_REFERENCE_IMAGES:
            raise ValueError(f"reference_images supports up to {self.MAX_REFERENCE_IMAGES} images")
        normalized = []
        for idx, item in enumerate(raw_reference_images, start=1):
            try:
                normalized.append(self._normalize_media_object(f"reference_images[{idx}]", item))
            except ValueError as e:
                raise ValueError(str(e)) from e
        return normalized

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    @classmethod
    def _cost_from_usd_ticks(cls, usage: Any) -> Optional[float]:
        if not isinstance(usage, dict):
            return None
        ticks = usage.get("cost_in_usd_ticks")
        if ticks is None:
            return None
        try:
            return int(ticks) / cls.USD_TICKS_PER_DOLLAR
        except (TypeError, ValueError):
            return None

    def _price_per_second(self, resolution: Optional[str], upstream_model: str) -> float:
        res = (resolution or "480p").strip().lower()
        if self._is_video_15_model(upstream_model):
            if res == "720p":
                return self._env_float(
                    "GROK_VIDEO_15_PRICE_PER_SECOND_720P", self.DEFAULT_PRICE_PER_SECOND_720P_15
                )
            if res == "1080p":
                return self._env_float(
                    "GROK_VIDEO_15_PRICE_PER_SECOND_1080P", self.DEFAULT_PRICE_PER_SECOND_1080P_15
                )
            return self._env_float(
                "GROK_VIDEO_15_PRICE_PER_SECOND_480P", self.DEFAULT_PRICE_PER_SECOND_480P_15
            )
        if res == "720p":
            return self._env_float("GROK_VIDEO_PRICE_PER_SECOND_720P", self.DEFAULT_PRICE_PER_SECOND_720P)
        if res == "1080p":
            return self._env_float("GROK_VIDEO_PRICE_PER_SECOND_1080P", self.DEFAULT_PRICE_PER_SECOND_1080P)
        return self._env_float("GROK_VIDEO_PRICE_PER_SECOND_480P", self.DEFAULT_PRICE_PER_SECOND_480P)

    def _reference_image_price(self, upstream_model: str) -> float:
        if self._is_video_15_model(upstream_model):
            return self._env_float(
                "GROK_VIDEO_15_PRICE_PER_REFERENCE_IMAGE", self.DEFAULT_PRICE_PER_REFERENCE_IMAGE_15
            )
        return self._env_float(
            "GROK_VIDEO_PRICE_PER_REFERENCE_IMAGE", self.DEFAULT_PRICE_PER_REFERENCE_IMAGE
        )

    def _estimate_cost(
        self,
        *,
        duration_seconds: int,
        resolution: Optional[str],
        reference_image_count: int,
        has_image_input: bool,
        has_video_input: bool,
        upstream_model: str,
    ) -> float:
        """Fallback when xAI does not return usage.cost_in_usd_ticks."""
        seconds = max(0, int(duration_seconds))
        cost = seconds * self._price_per_second(resolution, upstream_model)
        ref_price = self._reference_image_price(upstream_model)
        if reference_image_count > 0:
            cost += reference_image_count * ref_price
        elif has_image_input:
            cost += ref_price
        if has_video_input:
            cost += seconds * self._env_float("GROK_VIDEO_INPUT_VIDEO_PER_SECOND", 0.01)
        return cost

    @staticmethod
    def _video_response(video_url: str, *, response_cost: Optional[float] = None) -> ImageResponse:
        resp = ImageResponse(created=int(time.time()), data=[ImageObject(url=video_url)])
        if response_cost is not None:
            hidden = getattr(resp, "_hidden_params", None)
            if not isinstance(hidden, dict):
                hidden = {}
                resp._hidden_params = hidden
            hidden["response_cost"] = float(response_cost)
        return resp

    def _resolve_response_cost(
        self,
        *,
        status_data: dict,
        requested_duration: Optional[int],
        resolution: Optional[str],
        reference_image_count: int,
        has_image_input: bool,
        has_video_input: bool,
        upstream_model: str,
    ) -> Optional[float]:
        usage_cost = self._cost_from_usd_ticks(status_data.get("usage"))
        if usage_cost is not None:
            return usage_cost

        video_meta = status_data.get("video") or {}
        billed_seconds = video_meta.get("duration")
        if billed_seconds is None:
            billed_seconds = requested_duration if requested_duration is not None else 8
        try:
            billed_seconds = int(billed_seconds)
        except (TypeError, ValueError):
            billed_seconds = requested_duration or 8

        return self._estimate_cost(
            duration_seconds=billed_seconds,
            resolution=resolution,
            reference_image_count=reference_image_count,
            has_image_input=has_image_input,
            has_video_input=has_video_input,
            upstream_model=upstream_model,
        )

    async def aimage_generation(
        self,
        model: str,
        prompt: str,
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[AsyncHTTPHandler] = None,
        **kwargs: Any,
    ) -> ImageResponse:
        optional_params = dict(optional_params or {})
        log_legacy_video_usage(provider="xai", model=model, operation="submit_and_poll")
        api_key = os.environ.get("GROK_API_KEY")
        if not api_key:
            raise ValueError("GROK_API_KEY is required for grok-video")

        upstream_model = (
            optional_params.pop("xai_model", None)
            or optional_params.pop("upstream_model", None)
            or self._resolve_upstream_model(model)
        )

        if "referenceImageUrls" in optional_params and "reference_image_urls" not in optional_params:
            optional_params["reference_image_urls"] = optional_params.pop("referenceImageUrls")

        if "image_url" in optional_params and "image" not in optional_params:
            optional_params["image"] = optional_params.pop("image_url")
        if "video_url" in optional_params and "video" not in optional_params:
            optional_params["video"] = optional_params.pop("video_url")

        if "image_file_id" in optional_params:
            image_obj = optional_params.get("image")
            if image_obj is None:
                optional_params["image"] = {"file_id": optional_params.pop("image_file_id")}
            else:
                optional_params.pop("image_file_id")

        if "video_file_id" in optional_params:
            video_obj = optional_params.get("video")
            if video_obj is None:
                optional_params["video"] = {"file_id": optional_params.pop("video_file_id")}
            else:
                optional_params.pop("video_file_id")

        raw_operation = str(optional_params.pop("operation", "auto") or "auto").strip().lower()
        if raw_operation not in {"auto", "generate", "edit", "extend"}:
            raise ValueError("operation must be auto, generate, edit, or extend")

        raw_duration = optional_params.pop("duration", None)
        raw_seconds = optional_params.pop("seconds", None)
        duration = raw_duration if raw_duration is not None else raw_seconds
        if duration is not None:
            duration = self._coerce_int("duration", duration)

        image_input = optional_params.pop("image", None)
        video_input = optional_params.pop("video", None)

        raw_reference_images = optional_params.pop("reference_images", None)
        raw_reference_image_urls = optional_params.pop("reference_image_urls", None)
        raw_reference_image_file_ids = optional_params.pop("reference_image_file_ids", None)
        if raw_reference_images is None and raw_reference_image_urls is not None:
            raw_reference_images = raw_reference_image_urls
        if raw_reference_images is None and raw_reference_image_file_ids is not None:
            if not isinstance(raw_reference_image_file_ids, list):
                raise ValueError("reference_image_file_ids must be a list")
            raw_reference_images = [{"file_id": item} for item in raw_reference_image_file_ids]
        reference_images = self._normalize_reference_images(raw_reference_images)

        raw_reference_audios = optional_params.pop("reference_audios", None)
        raw_voice_ids = optional_params.pop("reference_voice_ids", None)
        if raw_reference_audios is not None and raw_voice_ids is not None:
            raise ValueError("use reference_audios or reference_voice_ids, not both")
        if raw_voice_ids is not None:
            if not isinstance(raw_voice_ids, list):
                raise ValueError("reference_voice_ids must be a list")
            raw_reference_audios = [{"voice_id": item} for item in raw_voice_ids]
        reference_audios: list[dict[str, str]] = []
        if raw_reference_audios is not None:
            if not isinstance(raw_reference_audios, list) or not 1 <= len(raw_reference_audios) <= 3:
                raise ValueError("reference_audios supports one to three preset voices")
            for index, audio in enumerate(raw_reference_audios):
                voice_id = audio.get("voice_id") if isinstance(audio, dict) else audio
                voice_id = str(voice_id or "").strip().lower()
                if not voice_id:
                    raise ValueError(f"reference_audios[{index}] requires voice_id")
                reference_audios.append({"voice_id": voice_id})

        prompt_text = (prompt or "").strip()
        if self._is_video_15_model(upstream_model) and video_input is not None:
            if not self._env_enabled("GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED"):
                raise ValueError(
                    "grok-imagine-video-1.5 editing and extension are disabled until the exact "
                    "1.5 endpoint passes the paid staging contract probes; use grok-imagine-video"
                )
        if raw_operation == "generate" and video_input is not None:
            raise ValueError("operation=generate cannot include a video")
        if raw_operation in {"edit", "extend"} and video_input is None:
            raise ValueError(f"operation={raw_operation} requires a video")
        if reference_audios and not self._is_video_15_model(upstream_model):
            raise ValueError("preset voice references require grok-imagine-video-1.5")
        operation = raw_operation if raw_operation != "auto" else "edit" if video_input is not None else "generate"
        endpoint = (
            "/videos/extensions"
            if operation == "extend"
            else "/videos/edits"
            if video_input is not None
            else "/videos/generations"
        )
        resolution = optional_params.get("resolution")
        reference_image_count = len(reference_images)
        has_image_input = image_input is not None
        has_video_input = video_input is not None

        if video_input is not None:
            if not prompt_text:
                raise ValueError(f"prompt is required for {endpoint}")
            if image_input is not None:
                raise ValueError("image is not supported for /v1/videos/edits")
            if reference_images:
                raise ValueError("reference_images are not supported for /v1/videos/edits")
            video_obj = self._normalize_media_object("video", video_input)
            payload = {
                "model": upstream_model,
                "prompt": prompt_text,
                "video": video_obj,
            }
            if operation == "extend" and duration is not None:
                if not 2 <= duration <= 10:
                    raise ValueError("extension duration must be between 2 and 10 seconds")
                payload["duration"] = duration
            elif operation != "extend" and duration is not None:
                raise ValueError("video editing does not accept a custom duration")
            if optional_params.get("aspect_ratio") is not None or optional_params.get("resolution") is not None:
                raise ValueError("video editing and extension preserve the source format")
            for key in ("output", "storage_options", "user"):
                if key in optional_params:
                    payload[key] = optional_params.pop(key)
        else:
            image_obj = self._normalize_media_object("image", image_input) if image_input is not None else None
            if not prompt_text and image_obj is None:
                raise ValueError("prompt is required for text-to-video generation")
            if reference_images and not prompt_text:
                raise ValueError("prompt is required when using reference_images")
            if duration is not None and not (1 <= duration <= 15):
                raise ValueError("duration must be between 1 and 15 seconds")
            if image_obj is not None and reference_images:
                raise ValueError("image and reference_images cannot be combined in one xAI request")
            payload = {
                "model": upstream_model,
            }
            if prompt_text:
                payload["prompt"] = prompt_text
            if image_obj is not None:
                payload["image"] = image_obj
            if reference_images:
                payload["reference_images"] = reference_images
            if reference_audios:
                payload["reference_audios"] = reference_audios
                if "<AUDIO_" not in prompt_text.upper():
                    voices = ", ".join(f"<AUDIO_{index}>" for index in range(len(reference_audios)))
                    payload["prompt"] = f"{payload['prompt'].rstrip()}\n\nPreset voices in order: {voices}."
            if duration is not None:
                payload["duration"] = duration
            for key in ("aspect_ratio", "resolution", "output", "storage_options", "user"):
                if key in optional_params:
                    payload[key] = optional_params.pop(key)
            if reference_images and str(payload.get("resolution") or "").lower() == "1080p":
                raise ValueError("reference-to-video is capped at 720p")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout or self.POLL_TIMEOUT + 30) as http:
            response = await http.post(
                f"{self.XAI_BASE}{endpoint}",
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = e.response.text
                raise GrokVideoException(normalize_error(detail)) from e

            data = response.json()
            request_id = data.get("request_id")

            returned_model = data.get("model")
            if returned_model and returned_model != upstream_model:
                raise GrokVideoException(
                    f"xAI returned model {returned_model!r}; expected {upstream_model!r}"
                )

            direct_video_url = (data.get("video") or {}).get("url")
            if direct_video_url:
                cost = self._resolve_response_cost(
                    status_data=data,
                    requested_duration=duration,
                    resolution=resolution,
                    reference_image_count=reference_image_count,
                    has_image_input=has_image_input,
                    has_video_input=has_video_input,
                    upstream_model=upstream_model,
                )
                return self._video_response(direct_video_url, response_cost=cost)

            if not request_id:
                raise GrokVideoException(normalize_error(data.get("error", {}).get("message", data)))

            deadline = time.monotonic() + self.POLL_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(self.POLL_INTERVAL)
                status_response = await http.get(
                    f"{self.XAI_BASE}/videos/{request_id}",
                    headers=headers,
                )
                try:
                    status_response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    try:
                        detail = e.response.json()
                    except Exception:
                        detail = e.response.text
                    raise GrokVideoException(normalize_error(detail)) from e

                status_data = status_response.json()
                status = status_data.get("status")

                returned_model = status_data.get("model")
                if returned_model and returned_model != upstream_model:
                    raise GrokVideoException(
                        f"xAI returned model {returned_model!r}; expected {upstream_model!r}"
                    )

                if status == "done":
                    video_url = (status_data.get("video") or {}).get("url")
                    if not video_url:
                        raise GrokVideoException("missing video url in completed Grok request")
                    cost = self._resolve_response_cost(
                        status_data=status_data,
                        requested_duration=duration,
                        resolution=resolution,
                        reference_image_count=reference_image_count,
                        has_image_input=has_image_input,
                        has_video_input=has_video_input,
                        upstream_model=upstream_model,
                    )
                    return self._video_response(video_url, response_cost=cost)
                if status in {"failed", "expired"}:
                    err = status_data.get("error") or {}
                    err_code = err.get("code")
                    err_msg = normalize_error(err.get("message", status))
                    if err_code:
                        raise GrokVideoException(
                            f"Grok request {request_id} failed ({err_code}): {err_msg}"
                        )
                    raise GrokVideoException(f"Grok request {request_id} failed: {err_msg}")

        raise GrokVideoException(f"Grok request {request_id} timed out after {self.POLL_TIMEOUT}s")


class GrokImageLLM(CustomLLM):
    """
    Wraps xAI image generation and edit endpoints.

    xAI image *edits* use ``application/json`` (not OpenAI-style multipart). The
    LiteLLM proxy still accepts multipart uploads; this handler turns uploaded
    bytes/streams into ``data:...;base64,...`` URLs before calling xAI.

    Endpoints:
      - https://api.x.ai/v1/images/generations
      - https://api.x.ai/v1/images/edits
    Auth: GROK_API_KEY

    Docs: https://docs.x.ai/developers/model-capabilities/images/editing
    """

    XAI_BASE = "https://api.x.ai/v1"
    DEFAULT_XAI_MODEL = "grok-imagine-image-quality"
    USD_TICKS_PER_DOLLAR = 10_000_000_000
    INPUT_IMAGE_PRICE = 0.01
    OUTPUT_IMAGE_PRICE_1K = 0.05
    OUTPUT_IMAGE_PRICE_2K = 0.07

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        value = (model or "").strip()
        return value.removeprefix("grok-image/").strip()

    def _resolve_upstream_model(self, model: str) -> str:
        stripped = self._strip_provider_prefix(model)
        if stripped and stripped != "grok-image":
            return stripped
        return os.environ.get("GROK_IMAGE_MODEL") or self.DEFAULT_XAI_MODEL

    @staticmethod
    def _unwrap_openai_file_tuple(value: Any) -> tuple[Any, Optional[str]]:
        """
        OpenAI / httpx file types are often (filename, fileobj) or
        (filename, bytes, content_type).
        """
        if not isinstance(value, tuple) or not value:
            return value, None
        first = value[0]
        if len(value) >= 2 and isinstance(first, str):
            second = value[1]
            if isinstance(second, (bytes, bytearray, memoryview)):
                return bytes(second), first
            if hasattr(second, "read"):
                return second, first
        return value, None

    @staticmethod
    def _read_stream_bytes(stream: Any) -> bytes:
        read = getattr(stream, "read", None)
        if read is None:
            raise ValueError("expected a binary stream with read()")
        chunks: list[bytes] = []
        while True:
            chunk = read(1024 * 1024)
            if chunk is None:
                break
            if isinstance(chunk, str):
                raise ValueError("stream read() returned str, expected bytes")
            if not chunk:
                break
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    @staticmethod
    def _bytes_to_image_url_field(raw: bytes, filename_hint: Optional[str]) -> dict[str, str]:
        mime, _ = mimetypes.guess_type(filename_hint or "")
        if not mime:
            mime = "image/png"
        b64 = base64.standard_b64encode(raw).decode("ascii")
        return {"url": f"data:{mime};base64,{b64}", "type": "image_url"}

    @classmethod
    def _normalize_image_object(cls, name: str, value: Any) -> dict:
        """
        Build the JSON ``image`` object for xAI: ``url`` + ``type`` (or ``file_id``).
        Accepts URLs, data URIs, xAI-style dicts, local paths, raw bytes, and
        multipart-style ``BytesIO`` / OpenAI file tuples from LiteLLM.
        """
        if value is None:
            raise ValueError(f"{name} is None")

        value, tuple_name = cls._unwrap_openai_file_tuple(value)
        filename_hint = tuple_name

        if isinstance(value, str) and value.strip():
            s = value.strip()
            if s.startswith(("http://", "https://", "data:")):
                return {"url": s, "type": "image_url"}
            path = Path(s)
            if path.is_file():
                raw = path.read_bytes()
                return cls._bytes_to_image_url_field(raw, path.name)
            return {"url": s, "type": "image_url"}

        if isinstance(value, Path):
            if not value.is_file():
                raise ValueError(f"{name}: path is not a file: {value}")
            raw = value.read_bytes()
            return cls._bytes_to_image_url_field(raw, value.name)

        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls._bytes_to_image_url_field(bytes(value), filename_hint)

        if isinstance(value, dict):
            out: dict[str, str] = {}
            url = value.get("url") or value.get("image_url")
            file_id = value.get("file_id")
            in_type = value.get("type")
            type_str = str(in_type).strip() if in_type is not None else ""

            if url:
                out["url"] = str(url).strip()
                out["type"] = type_str or "image_url"
            if file_id:
                out["file_id"] = str(file_id).strip()
                out["type"] = type_str or "image_file"

            if out.get("url") and out.get("file_id"):
                raise ValueError(f"{name} must include either url or file_id, not both")
            if not out:
                raise ValueError(f"{name} must include url or file_id")
            return out

        name_attr = getattr(value, "name", None)
        if isinstance(name_attr, str) and name_attr and filename_hint is None:
            filename_hint = name_attr

        read = getattr(value, "read", None)
        if callable(read):
            raw = cls._read_stream_bytes(value)
            if not raw:
                raise ValueError(f"{name}: empty file or stream")
            return cls._bytes_to_image_url_field(raw, filename_hint)

        raise ValueError(
            f"{name} must be a url/data-uri string, path, bytes, file-like object, "
            f"OpenAI (filename, file) tuple, or dict with url/file_id"
        )

    @classmethod
    def _normalize_image_inputs(cls, name: str, value: Any) -> list[dict]:
        values = value if isinstance(value, list) else [value]
        if not 1 <= len(values) <= 3:
            raise ValueError(f"{name} supports one to three images")
        return [cls._normalize_image_object(f"{name}[{index}]", item) for index, item in enumerate(values)]

    @classmethod
    def _image_response_from_http_body(cls, body: dict, request_payload: dict[str, Any]) -> ImageResponse:
        data = body.get("data") or []
        if not data:
            raise GrokImageException(normalize_error(body.get("error", {}).get("message", body)))

        out = []
        passthrough: list[dict[str, Any]] = []
        for item in data:
            item = item or {}
            values = {
                key: item.get(key)
                for key in ("url", "b64_json", "revised_prompt")
                if item.get(key) is not None
            }
            if values.get("url") or values.get("b64_json"):
                try:
                    image = ImageObject(**values)
                except (TypeError, ValueError):
                    image = ImageObject(url=values.get("url"))
                for key in ("b64_json", "revised_prompt", "mime_type", "file_output", "public_url"):
                    if item.get(key) is not None:
                        try:
                            setattr(image, key, item[key])
                        except (AttributeError, TypeError, ValueError):
                            pass
                out.append(image)
                passthrough.append(
                    {
                        key: item[key]
                        for key in ("mime_type", "file_output", "public_url")
                        if key in item
                    }
                )

        if not out:
            raise GrokImageException("xAI image response did not include any output url")

        response = ImageResponse(created=int(time.time()), data=out)
        hidden = getattr(response, "_hidden_params", None)
        if not isinstance(hidden, dict):
            hidden = {}
            response._hidden_params = hidden
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        cost = None
        if usage and usage.get("cost_in_usd_ticks") is not None:
            try:
                cost = int(usage["cost_in_usd_ticks"]) / cls.USD_TICKS_PER_DOLLAR
            except (TypeError, ValueError):
                pass
        if cost is None:
            input_count = len(request_payload.get("images") or ([] if not request_payload.get("image") else [1]))
            output_count = len(data) or int(request_payload.get("n") or 1)
            resolution = str(request_payload.get("resolution") or request_payload.get("size") or "1K").upper()
            output_price = cls.OUTPUT_IMAGE_PRICE_2K if resolution == "2K" else cls.OUTPUT_IMAGE_PRICE_1K
            cost = input_count * cls.INPUT_IMAGE_PRICE + output_count * output_price
        hidden["response_cost"] = float(cost)
        if usage:
            hidden["xai_usage"] = usage
        if any(passthrough):
            hidden["xai_image_outputs"] = passthrough
        return response

    def _prepare_xai_image_request_parts(
        self,
        model: str,
        prompt: str,
        optional_params: dict[str, Any],
        *,
        require_image: bool,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """
        Returns (full_url, json_payload, headers). Mutates ``optional_params`` via pop().
        """
        optional_params = dict(optional_params or {})

        api_key = os.environ.get("GROK_API_KEY")
        if not api_key:
            raise ValueError("GROK_API_KEY is required for grok-image")

        upstream_model = (
            optional_params.pop("xai_model", None)
            or optional_params.pop("upstream_model", None)
            or self._resolve_upstream_model(model)
        )

        if "image_url" in optional_params and "image" not in optional_params:
            optional_params["image"] = optional_params.pop("image_url")

        if "image_file_id" in optional_params and "image" not in optional_params:
            optional_params["image"] = {"file_id": optional_params.pop("image_file_id")}

        if "image_urls" in optional_params and "images" not in optional_params:
            optional_params["images"] = optional_params.pop("image_urls")
        if "image_file_ids" in optional_params and "images" not in optional_params:
            raw_ids = optional_params.pop("image_file_ids")
            if not isinstance(raw_ids, list):
                raise ValueError("image_file_ids must be a list")
            optional_params["images"] = [{"file_id": item} for item in raw_ids]

        prompt_text = (prompt or "").strip()
        image_input = optional_params.pop("image", None)
        images_input = optional_params.pop("images", None)
        if image_input is not None and images_input is not None:
            raise ValueError("use image or images, not both")
        normalized_images = (
            self._normalize_image_inputs("images", images_input if images_input is not None else image_input)
            if images_input is not None or image_input is not None
            else []
        )

        if require_image and not normalized_images:
            raise ValueError("image is required for /v1/images/edits")

        endpoint = "/images/edits" if normalized_images else "/images/generations"

        if not prompt_text:
            if endpoint == "/images/edits":
                raise ValueError("prompt is required for /v1/images/edits")
            raise ValueError("prompt is required for /v1/images/generations")

        payload: dict[str, Any] = {
            "model": upstream_model,
            "prompt": prompt_text,
        }
        if len(normalized_images) == 1:
            payload["image"] = normalized_images[0]
        elif normalized_images:
            payload["images"] = normalized_images

        for key in (
            "n",
            "size",
            "quality",
            "response_format",
            "style",
            "background",
            "aspect_ratio",
            "resolution",
            "output_format",
            "storage_options",
            "user",
        ):
            if key in optional_params:
                payload[key] = optional_params.pop(key)
        if str(payload.get("size") or "").upper() in {"1K", "2K"}:
            payload["resolution"] = str(payload.pop("size")).upper()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        return f"{self.XAI_BASE}{endpoint}", payload, headers

    async def _image_request(
        self,
        model: str,
        prompt: str,
        optional_params: dict,
        timeout: Optional[Union[float, httpx.Timeout]],
        *,
        require_image: bool,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_xai_image_request_parts(
            model, prompt, optional_params, require_image=require_image
        )

        async with httpx.AsyncClient(timeout=timeout or 120) as http:
            response = await http.post(
                url,
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = e.response.text
                raise GrokImageException(normalize_error(detail)) from e

            return self._image_response_from_http_body(response.json(), payload)

    def _image_request_sync(
        self,
        model: str,
        prompt: str,
        optional_params: dict,
        timeout: Optional[Union[float, httpx.Timeout]],
        *,
        require_image: bool,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_xai_image_request_parts(
            model, prompt, dict(optional_params or {}), require_image=require_image
        )

        with httpx.Client(timeout=timeout or 120) as http:
            response = http.post(
                url,
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = e.response.text
                raise GrokImageException(normalize_error(detail)) from e

            return self._image_response_from_http_body(response.json(), payload)

    def image_generation(
        self,
        model: str,
        prompt: str,
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[HTTPHandler] = None,
        **kwargs: Any,
    ) -> ImageResponse:
        return self._image_request_sync(
            model, prompt, optional_params, timeout, require_image=False
        )

    async def aimage_generation(
        self,
        model: str,
        prompt: str,
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[AsyncHTTPHandler] = None,
        **kwargs: Any,
    ) -> ImageResponse:
        return await self._image_request(
            model,
            prompt,
            optional_params,
            timeout,
            require_image=False,
        )

    def image_edit(
        self,
        model: str,
        image: Any,
        prompt: Optional[str],
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[HTTPHandler] = None,
    ) -> ImageResponse:
        params = dict(optional_params or {})
        if image is not None and "image" not in params and "images" not in params and "image_url" not in params:
            params["images" if isinstance(image, list) else "image"] = image
        return self._image_request_sync(
            model,
            prompt or "",
            params,
            timeout,
            require_image=True,
        )

    async def aimage_edit(
        self,
        model: str,
        image: Any,
        prompt: Optional[str],
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[AsyncHTTPHandler] = None,
        **kwargs: Any,
    ) -> ImageResponse:
        params = dict(optional_params or {})
        if image is not None and "image" not in params and "images" not in params and "image_url" not in params:
            params["images" if isinstance(image, list) else "image"] = image
        return await self._image_request(
            model,
            prompt or "",
            params,
            timeout,
            require_image=True,
        )


grok_video = GrokVideoLLM()
grok_image = GrokImageLLM()
