"""
Custom LiteLLM handlers for providers not natively supported:
  - grok-video  (xAI video generation)
  - seedance    (ByteDance Seedance 2.0 video generation)

Both are registered as custom providers and exposed through the LiteLLM proxy.
Routes:
  POST /v1/images/generations  ->  grok-video/<model>
  POST /v1/images/generations  ->  seedance/<model>
"""

import asyncio
import os
import time
from typing import Any, Optional, Union

import httpx
from litellm import CustomLLM
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.types.utils import ImageObject, ImageResponse


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


# ── Seedance 2.0 (ByteDance / VolcEngine) ────────────────────────────────────

class SeedanceLLM(CustomLLM):
    """
    Wraps ByteDance VolcEngine Seedance 2.0 video generation API.
    API docs: https://www.volcengine.com/docs/6791/1330310
    Endpoint: https://visual.volcengineapi.com  (Action=CVProcess)

    The VolcEngine API is async: submit a task, poll for the result URL.
    We block here until the video is ready and return it as an image URL
    so it fits the ImageResponse envelope LiteLLM expects.
    """

    VOLC_BASE = "https://visual.volcengineapi.com"
    POLL_INTERVAL = 3   # seconds between status checks
    POLL_TIMEOUT = 300  # maximum seconds to wait
    MAX_REFERENCE_IMAGES = 7

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
        api_key = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key:
            raise ValueError("BYTEDANCE_API_KEY is required for seedance")

        # Normalize common reference-image keys used by SDKs/clients.
        if "referenceImageUrls" in optional_params and "reference_image_urls" not in optional_params:
            optional_params["reference_image_urls"] = optional_params.pop("referenceImageUrls")

        reference_images = optional_params.get("reference_image_urls")
        if reference_images is not None:
            if not isinstance(reference_images, list) or len(reference_images) == 0:
                raise ValueError("reference_image_urls must be a non-empty list")
            if len(reference_images) > self.MAX_REFERENCE_IMAGES:
                raise ValueError(f"reference_image_urls supports up to {self.MAX_REFERENCE_IMAGES} images")
            if not prompt or not prompt.strip():
                raise ValueError("A non-empty prompt is required when using reference_image_urls")

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

        # VolcEngine uses AK/SK or bearer token depending on auth method.
        # Set VOLC_API_KEY to a pre-signed bearer token, or extend this to
        # use HMAC signing with VOLC_ACCESS_KEY_ID + VOLC_SECRET_ACCESS_KEY.
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        submit_payload = {
            "req_key": model,   # e.g. "seedance_video_2_0"
            "prompt": prompt,
            **optional_params,
        }

        async with httpx.AsyncClient(timeout=timeout or self.POLL_TIMEOUT + 30) as http:
            # 1. Submit generation task
            submit_resp = await http.post(
                f"{self.VOLC_BASE}/?Action=CVProcess&Version=2022-08-31",
                headers=headers,
                json=submit_payload,
            )
            submit_resp.raise_for_status()
            submit_data = submit_resp.json()
            task_id = submit_data.get("data", {}).get("task_id")
            if not task_id:
                raise ValueError(f"Seedance submit failed: {submit_data}")

            # 2. Poll until the task completes
            deadline = time.monotonic() + self.POLL_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(self.POLL_INTERVAL)
                status_resp = await http.post(
                    f"{self.VOLC_BASE}/?Action=CVSync2AsyncGetResult&Version=2022-08-31",
                    headers=headers,
                    json={"task_id": task_id},
                )
                status_resp.raise_for_status()
                status_data = status_resp.json()
                status = status_data.get("data", {}).get("status")

                if status == "done":
                    video_url = status_data["data"]["resp_data"][0]["url"]
                    return ImageResponse(
                        created=int(time.time()),
                        data=[ImageObject(url=video_url)],
                    )
                elif status == "failed":
                    raise RuntimeError(f"Seedance generation failed: {status_data}")
                # else: still processing, keep polling

        raise TimeoutError(f"Seedance task {task_id} did not complete within {self.POLL_TIMEOUT}s")

seedance = SeedanceLLM()
