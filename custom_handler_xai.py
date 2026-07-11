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
    DEFAULT_PRICE_PER_SECOND_1080P_15 = 0.14
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

        raw_duration = optional_params.pop("duration", None)
        raw_seconds = optional_params.pop("seconds", None)
        duration = raw_duration if raw_duration is not None else raw_seconds
        if duration is not None:
            duration = self._coerce_int("duration", duration)

        image_input = optional_params.pop("image", None)
        video_input = optional_params.pop("video", None)

        raw_reference_images = optional_params.pop("reference_images", None)
        raw_reference_image_urls = optional_params.pop("reference_image_urls", None)
        if raw_reference_images is None and raw_reference_image_urls is not None:
            raw_reference_images = raw_reference_image_urls
        reference_images = self._normalize_reference_images(raw_reference_images)

        prompt_text = (prompt or "").strip()
        endpoint = "/videos/edits" if video_input is not None else "/videos/generations"
        resolution = optional_params.get("resolution")
        reference_image_count = len(reference_images)
        has_image_input = image_input is not None

        if endpoint == "/videos/edits":
            if not prompt_text:
                raise ValueError("prompt is required for /v1/videos/edits")
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
            if reference_images and duration is not None and duration > self.MAX_REFERENCE_DURATION:
                raise ValueError(
                    f"duration must be <= {self.MAX_REFERENCE_DURATION} when using reference_images"
                )
            payload = {
                "model": upstream_model,
            }
            if prompt_text:
                payload["prompt"] = prompt_text
            if image_obj is not None:
                payload["image"] = image_obj
            if reference_images:
                payload["reference_images"] = reference_images
            if duration is not None:
                payload["duration"] = duration
            for key in ("aspect_ratio", "resolution", "output", "storage_options", "user"):
                if key in optional_params:
                    payload[key] = optional_params.pop(key)

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

            direct_video_url = (data.get("video") or {}).get("url")
            if direct_video_url:
                cost = self._resolve_response_cost(
                    status_data=data,
                    requested_duration=duration,
                    resolution=resolution,
                    reference_image_count=reference_image_count,
                    has_image_input=has_image_input,
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

        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError(f"{name}: expected a single image, got {len(value)}")
            return cls._normalize_image_object(name, value[0])

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

    @staticmethod
    def _image_response_from_http_body(body: dict) -> ImageResponse:
        data = body.get("data") or []
        if not data:
            raise GrokImageException(normalize_error(body.get("error", {}).get("message", body)))

        out = []
        for item in data:
            url = (item or {}).get("url")
            if url:
                out.append(ImageObject(url=url))

        if not out:
            raise GrokImageException("xAI image response did not include any output url")

        return ImageResponse(created=int(time.time()), data=out)

    def _prepare_xai_image_request_parts(
        self,
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
            or os.environ.get("GROK_IMAGE_MODEL")
            or self.DEFAULT_XAI_MODEL
        )

        if "image_url" in optional_params and "image" not in optional_params:
            optional_params["image"] = optional_params.pop("image_url")

        if "image_file_id" in optional_params and "image" not in optional_params:
            optional_params["image"] = {"file_id": optional_params.pop("image_file_id")}

        prompt_text = (prompt or "").strip()
        image_input = optional_params.pop("image", None)
        image_obj = (
            self._normalize_image_object("image", image_input)
            if image_input is not None
            else None
        )

        if require_image and image_obj is None:
            raise ValueError("image is required for /v1/images/edits")

        endpoint = "/images/edits" if image_obj is not None else "/images/generations"

        if not prompt_text:
            if endpoint == "/images/edits":
                raise ValueError("prompt is required for /v1/images/edits")
            raise ValueError("prompt is required for /v1/images/generations")

        payload: dict[str, Any] = {
            "model": upstream_model,
            "prompt": prompt_text,
        }
        if image_obj is not None:
            payload["image"] = image_obj

        for key in (
            "n",
            "size",
            "quality",
            "response_format",
            "style",
            "background",
            "user",
        ):
            if key in optional_params:
                payload[key] = optional_params.pop(key)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        return f"{self.XAI_BASE}{endpoint}", payload, headers

    async def _image_request(
        self,
        prompt: str,
        optional_params: dict,
        timeout: Optional[Union[float, httpx.Timeout]],
        *,
        require_image: bool,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_xai_image_request_parts(
            prompt, optional_params, require_image=require_image
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

            return self._image_response_from_http_body(response.json())

    def _image_request_sync(
        self,
        prompt: str,
        optional_params: dict,
        timeout: Optional[Union[float, httpx.Timeout]],
        *,
        require_image: bool,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_xai_image_request_parts(
            prompt, dict(optional_params or {}), require_image=require_image
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

            return self._image_response_from_http_body(response.json())

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
        image_value = image[0] if isinstance(image, list) and image else image
        params = dict(optional_params or {})
        if image_value is not None and "image" not in params and "image_url" not in params:
            params["image"] = image_value
        return self._image_request_sync(
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
        # LiteLLM wraps the caller's image into a list before passing it here.
        # Unwrap to a single value so _image_request can normalise it.
        image_value = image[0] if isinstance(image, list) and image else image
        params = dict(optional_params or {})
        if image_value is not None and "image" not in params and "image_url" not in params:
            params["image"] = image_value
        return await self._image_request(
            prompt or "",
            params,
            timeout,
            require_image=True,
        )


grok_video = GrokVideoLLM()
grok_image = GrokImageLLM()
