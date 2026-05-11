"""
Custom LiteLLM handlers for providers not natively supported:
  - grok-video  (xAI video generation)
  - seedance    (ByteDance Seedance 2.0 video generation via BytePlus ARK)

Both are registered as custom providers and exposed through the LiteLLM proxy.
Routes:
  POST /v1/images/generations  ->  grok-video/<model>
  POST /v1/images/generations  ->  seedance/<model>
"""

import asyncio
import os
import time
from typing import Any, List, Optional, Tuple, Union

import httpx
from litellm import CustomLLM
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.types.utils import ImageObject, ImageResponse


def normalize_error(message: Any) -> str:
    if message is None:
        return "unknown error"
    if isinstance(message, dict):
        msg = message.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        err = message.get("error")
        if isinstance(err, dict):
            inner = err.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return str(message).strip() or "unknown error"
    if isinstance(message, str):
        s = message.strip()
        return s if s else "unknown error"
    s = str(message).strip()
    return s if s else "unknown error"


class SeedanceException(Exception):
    """Raised when BytePlus ARK Seedance task submission or polling fails."""

    pass


# ── Grok Video (xAI) ────────────────────────────────────────────────────────

class GrokVideoLLM(CustomLLM):
    """
    Wraps xAI's video generation endpoint.
    API docs: https://docs.x.ai/docs
    Endpoint: https://api.x.ai/v1/videos/generations
    Auth:     GROK_API_KEY
    """

    XAI_BASE = "https://api.x.ai/v1"
    POLL_INTERVAL = 3   # seconds between status checks
    POLL_TIMEOUT = 600  # maximum seconds to wait
    MAX_REFERENCE_IMAGES = 7
    MAX_REFERENCE_DURATION = 10
    DEFAULT_XAI_MODEL = "grok-imagine-video"

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
        # Always use the proxy's configured env var for upstream auth; do not
        # accept api_key from client-supplied parameters.
        api_key = os.environ.get("GROK_API_KEY")
        if not api_key:
            raise ValueError("GROK_API_KEY is required for grok-video")

        # LiteLLM routes through the synthetic grok-video model group, but xAI expects
        # the real upstream video model id.
        upstream_model = (
            optional_params.pop("xai_model", None)
            or optional_params.pop("upstream_model", None)
            or os.environ.get("GROK_VIDEO_MODEL")
            or self.DEFAULT_XAI_MODEL
        )

        # Accept both snake_case and camelCase for compatibility with upstream callers.
        if "referenceImageUrls" in optional_params and "reference_image_urls" not in optional_params:
            optional_params["reference_image_urls"] = optional_params.pop("referenceImageUrls")

        reference_images = optional_params.pop("reference_image_urls", None)
        if reference_images is not None:
            if not isinstance(reference_images, list) or len(reference_images) == 0:
                raise ValueError("reference_image_urls must be a non-empty list")
            if len(reference_images) > self.MAX_REFERENCE_IMAGES:
                raise ValueError(f"reference_image_urls supports up to {self.MAX_REFERENCE_IMAGES} images")
            if not prompt or not prompt.strip():
                raise ValueError("A non-empty prompt is required when using reference_image_urls")

            duration = optional_params.get("duration")
            if isinstance(duration, (int, float)) and duration > self.MAX_REFERENCE_DURATION:
                raise ValueError(
                    f"duration must be <= {self.MAX_REFERENCE_DURATION} when using reference_image_urls"
                )

            if any(k in optional_params for k in ("image", "image_url", "video_url")):
                raise ValueError(
                    "reference_image_urls cannot be combined with image-to-video or video editing fields"
                )

            for idx, image in enumerate(reference_images, start=1):
                if not isinstance(image, str) or not image:
                    raise ValueError(f"reference_image_urls[{idx}] must be a non-empty string")
                if not (image.startswith("https://") or image.startswith("data:image/")):
                    raise ValueError(
                        f"reference_image_urls[{idx}] must be an https URL or data:image/... URI"
                    )

            # xAI SDKs use reference_image_urls, but the REST API expects
            # reference_images: [{"url": "..."}].
            optional_params["reference_images"] = [{"url": image} for image in reference_images]

        payload = {
            "model": upstream_model,
            "prompt": prompt,
            **optional_params,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout or self.POLL_TIMEOUT + 30) as http:
            # 1. Submit generation request
            response = await http.post(
                f"{self.XAI_BASE}/videos/generations",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            request_id = data.get("request_id")

            # Fallback for any direct-complete responses.
            direct_video_url = data.get("video", {}).get("url")
            if direct_video_url:
                return ImageResponse(created=int(time.time()), data=[ImageObject(url=direct_video_url)])

            if not request_id:
                raise ValueError(f"Grok submit failed: {data}")

            # 2. Poll for completion
            deadline = time.monotonic() + self.POLL_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(self.POLL_INTERVAL)
                status_response = await http.get(
                    f"{self.XAI_BASE}/videos/{request_id}",
                    headers=headers,
                )
                status_response.raise_for_status()
                status_data = status_response.json()
                status = status_data.get("status")

                if status == "done":
                    video_url = status_data.get("video", {}).get("url")
                    if not video_url:
                        raise ValueError(f"Grok completion missing video url: {status_data}")
                    return ImageResponse(
                        created=int(time.time()),
                        data=[ImageObject(url=video_url)],
                    )
                if status in {"failed", "expired"}:
                    raise RuntimeError(f"Grok video request {request_id} ended with status={status}: {status_data}")
                # else: pending, keep polling

        raise TimeoutError(f"Grok request {request_id} did not complete within {self.POLL_TIMEOUT}s")


grok_video = GrokVideoLLM()


# ── Seedance 2.0 (BytePlus ARK) ─────────────────────────────────────────────

DEFAULT_ARK_MODEL = "dreamina-seedance-2-0-260128"
DEFAULT_ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"


def _as_image_response(url: str) -> ImageResponse:
    return ImageResponse(created=int(time.time()), data=[ImageObject(url=url)])


class SeedanceLLM(CustomLLM):
    """
    Wraps BytePlus ARK Seedance 2.0 async task API.

    Submit: POST {ARK_BASE}/contents/generations/tasks
    Poll:   GET  {ARK_BASE}/contents/generations/tasks/{id}

    Auth: BYTEDANCE_API_KEY (Bearer). Optional: SEEDANCE_ARK_BASE, SEEDANCE_ARK_MODEL.
    Polling: SEEDANCE_POLL_INTERVAL_S (default 10), SEEDANCE_POLL_TIMEOUT_S (default 1200).
    """

    MAX_REFERENCE_IMAGES = 7

    def _poll_interval_s(self) -> float:
        raw = os.environ.get("SEEDANCE_POLL_INTERVAL_S", "10")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 10.0

    def _poll_timeout_s(self) -> float:
        raw = os.environ.get("SEEDANCE_POLL_TIMEOUT_S", "1200")
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 1200.0

    def _ark_base(self) -> str:
        return (os.environ.get("SEEDANCE_ARK_BASE") or DEFAULT_ARK_BASE).rstrip("/")

    def _resolve_upstream_model(self, model: str) -> str:
        if model and model.strip():
            return model.strip()
        return os.environ.get("SEEDANCE_ARK_MODEL") or DEFAULT_ARK_MODEL

    def _collect_image_urls(self, optional_params: dict) -> Tuple[List[str], bool]:
        """Returns (ordered image URLs, reference_batch).

        ``reference_batch`` is True when URLs came from ``reference_image_urls``; ARK expects
        ``role=reference_image`` for those. Otherwise URLs use ``first_frame`` / ``last_frame``
        heuristics for image-to-video (see BytePlus Seedance content API).
        """
        urls: List[str] = []

        reference_images = optional_params.pop("reference_image_urls", None)
        if reference_images is not None:
            if any(k in optional_params for k in ("image", "image_url", "video_url")):
                raise ValueError(
                    "reference_image_urls cannot be combined with image-to-video or video editing fields"
                )
            if not isinstance(reference_images, list) or len(reference_images) == 0:
                raise ValueError("reference_image_urls must be a non-empty list")
            if len(reference_images) > self.MAX_REFERENCE_IMAGES:
                raise ValueError(f"reference_image_urls supports up to {self.MAX_REFERENCE_IMAGES} images")

            for idx, image in enumerate(reference_images, start=1):
                if not isinstance(image, str) or not image:
                    raise ValueError(f"reference_image_urls[{idx}] must be a non-empty string")
                if not (image.startswith("https://") or image.startswith("data:image/")):
                    raise ValueError(
                        f"reference_image_urls[{idx}] must be an https URL or data:image/... URI"
                    )
            return reference_images, True

        primary = optional_params.pop("image_url", None) or optional_params.pop("image", None)
        if primary:
            urls.append(primary)

        images = optional_params.pop("images", None)
        if images is not None:
            if not isinstance(images, list):
                raise ValueError("images must be a list of URL strings")
            urls.extend(str(u) for u in images if u)

        for idx, url in enumerate(urls, start=1):
            if not (url.startswith("https://") or url.startswith("data:image/")):
                raise ValueError(f"image URL #{idx} must be https:// or data:image/...")

        return urls, False

    def _ark_image_content_items(self, urls: List[str], *, reference_batch: bool) -> List[dict]:
        """Build ``content`` entries for images; BytePlus requires ``role`` on each image part."""
        items: List[dict] = []
        for i, url in enumerate(urls):
            item: dict = {"type": "image_url", "image_url": {"url": url}}
            if reference_batch:
                item["role"] = "reference_image"
            elif len(urls) == 1:
                item["role"] = "first_frame"
            elif len(urls) == 2:
                item["role"] = "first_frame" if i == 0 else "last_frame"
            else:
                item["role"] = "reference_image"
            items.append(item)
        return items

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
        if "referenceImageUrls" in optional_params and "reference_image_urls" not in optional_params:
            optional_params["reference_image_urls"] = optional_params.pop("referenceImageUrls")

        api_key = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key:
            raise ValueError("BYTEDANCE_API_KEY is required for seedance")

        ref_only = optional_params.get("reference_image_urls") is not None
        if ref_only and (not prompt or not prompt.strip()):
            raise ValueError("A non-empty prompt is required when using reference_image_urls")

        image_urls, reference_image_batch = self._collect_image_urls(optional_params)

        resolution = optional_params.pop("resolution", "480p")
        ratio = optional_params.pop("ratio", "1:1")
        duration = optional_params.pop("duration", 4)
        if isinstance(duration, float) and duration.is_integer():
            duration = int(duration)
        if not isinstance(duration, int):
            raise ValueError("duration must be an integer (seconds)")

        generate_audio = optional_params.pop("generate_audio", optional_params.pop("generateAudio", False))
        watermark = optional_params.pop("watermark", False)

        ark_model = self._resolve_upstream_model(model)
        ark_base = self._ark_base()
        poll_interval = self._poll_interval_s()
        poll_timeout = self._poll_timeout_s()

        content: List[dict] = [{"type": "text", "text": prompt}]
        content.extend(self._ark_image_content_items(image_urls, reference_batch=reference_image_batch))

        submit_body = {
            "model": ark_model,
            "content": content,
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "generate_audio": bool(generate_audio),
            "watermark": bool(watermark),
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        client_timeout = timeout or (poll_timeout + 30)

        async with httpx.AsyncClient(timeout=client_timeout) as http:
            submit_resp = await http.post(
                f"{ark_base}/contents/generations/tasks",
                headers=headers,
                json=submit_body,
            )
            try:
                submit_resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = e.response.text
                raise SeedanceException(normalize_error(detail)) from e

            submit_data = submit_resp.json()
            task_id = submit_data.get("id")
            if not task_id:
                raise SeedanceException(normalize_error(submit_data.get("error", {}).get("message", submit_data)))

            deadline = time.monotonic() + poll_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval)
                poll = await http.get(
                    f"{ark_base}/contents/generations/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                try:
                    poll.raise_for_status()
                except httpx.HTTPStatusError as e:
                    try:
                        detail = e.response.json()
                    except Exception:
                        detail = e.response.text
                    raise SeedanceException(normalize_error(detail)) from e

                body = poll.json()
                status = body.get("status")

                if status == "succeeded":
                    video_url = (body.get("content") or {}).get("video_url")
                    if not video_url:
                        raise SeedanceException(normalize_error("missing video_url in succeeded task"))
                    return _as_image_response(video_url)

                if status in ("failed", "expired"):
                    err = body.get("error") or {}
                    raise SeedanceException(normalize_error(err.get("message", status)))

        raise SeedanceException("Seedance task timed out before completion.")


seedance = SeedanceLLM()
