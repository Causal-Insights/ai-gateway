"""BytePlus ModelArk Seedream 5 custom LiteLLM handler.

Sync OpenAI-compatible image generation at ModelArk::

    POST {ARK_BASE}/images/generations

ModelArk docs: https://docs.byteplus.com/en/docs/ModelArk/1541523
Pricing: https://docs.byteplus.com/en/docs/ModelArk/1544106

Env:
    BYTEDANCE_API_KEY                 ModelArk bearer token (required)
    SEEDREAM_ARK_BASE                 override ARK base (default BytePlus ap-southeast)
    SEEDREAM_5_0_PRICE_PER_IMAGE      USD per output image for seedream-5.0 (ARK seedream-5-0-260128)
    SEEDREAM_5_0_LITE_PRICE_PER_IMAGE USD per output image for seedream-5.0-lite (ARK seedream-5-0-lite-260128)
    SEEDREAM_WEB_SEARCH_PRICE_PER_REQUEST  USD when tools includes web_search (default 0.0006)
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Union

import httpx
from litellm import CustomLLM
from litellm.types.utils import ImageObject, ImageResponse

from custom_handler_common import normalize_error

DEFAULT_ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"

DEFAULT_MODEL_5_0 = "seedream-5-0-260128"
DEFAULT_MODEL_5_0_LITE = "seedream-5-0-lite-260128"

# BytePlus ModelArk list price (Seedream 5.0 / 5.0 Lite, 2K & 3K, per generated image).
DEFAULT_PRICE_PER_IMAGE_5_0 = 0.035
DEFAULT_PRICE_PER_IMAGE_5_0_LITE = 0.035
# Optional web_search tool surcharge (per request when tools includes web_search).
DEFAULT_WEB_SEARCH_PRICE_PER_REQUEST = 0.0006

# OpenAI-shaped fields forwarded to ModelArk (everything else is dropped by the handler).
_ARK_PASSTHROUGH_KEYS = (
    "size",
    "n",
    "response_format",
    "output_format",
    "watermark",
    "stream",
    "seed",
    "sequential_image_generation",
    "sequential_image_generation_options",
    "tools",
    "optimize_prompt_options",
)


class SeedreamException(Exception):
    """Raised when ModelArk Seedream image generation fails."""


class SeedreamLLM(CustomLLM):
    """Wraps BytePlus ModelArk Seedream 5 image generation (sync, OpenAI-compatible)."""

    MAX_REFERENCE_IMAGES = 14

    @staticmethod
    def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return max(minimum, float(raw))
        except ValueError:
            return default

    def _ark_base(self) -> str:
        return (os.environ.get("SEEDREAM_ARK_BASE") or DEFAULT_ARK_BASE).rstrip("/")

    def _resolve_upstream_model(self, model: str) -> str:
        if model and model.strip():
            return model.strip()
        return DEFAULT_MODEL_5_0_LITE

    def _price_per_image(self, ark_model: str) -> float:
        lower = (ark_model or "").lower()
        if "lite" in lower:
            return self._env_float(
                "SEEDREAM_5_0_LITE_PRICE_PER_IMAGE", DEFAULT_PRICE_PER_IMAGE_5_0_LITE
            )
        return self._env_float("SEEDREAM_5_0_PRICE_PER_IMAGE", DEFAULT_PRICE_PER_IMAGE_5_0)

    @staticmethod
    def _web_search_price() -> float:
        return SeedreamLLM._env_float(
            "SEEDREAM_WEB_SEARCH_PRICE_PER_REQUEST", DEFAULT_WEB_SEARCH_PRICE_PER_REQUEST
        )

    @staticmethod
    def _uses_web_search(tools: Any) -> bool:
        if not isinstance(tools, list):
            return False
        for item in tools:
            if isinstance(item, dict) and str(item.get("type", "")).strip() == "web_search":
                return True
            if isinstance(item, str) and item.strip() == "web_search":
                return True
        return False

    def _collect_image_inputs(self, optional_params: dict) -> Optional[Union[str, List[str]]]:
        """Normalize OpenAI / gateway aliases into ModelArk ``image`` (string or list)."""
        if "image_urls" in optional_params:
            raw = optional_params.pop("image_urls")
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if u]
                if len(urls) > self.MAX_REFERENCE_IMAGES:
                    raise ValueError(
                        f"image_urls supports up to {self.MAX_REFERENCE_IMAGES} images"
                    )
                return urls
            raise ValueError("image_urls must be a string or list of URLs")

        if "images" in optional_params:
            raw = optional_params.pop("images")
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if u]
                if len(urls) > self.MAX_REFERENCE_IMAGES:
                    raise ValueError(f"images supports up to {self.MAX_REFERENCE_IMAGES} images")
                return urls
            raise ValueError("images must be a string or list")

        if "image" in optional_params:
            raw = optional_params.pop("image")
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if u]
                if len(urls) > self.MAX_REFERENCE_IMAGES:
                    raise ValueError(f"image supports up to {self.MAX_REFERENCE_IMAGES} images")
                return urls
            raise ValueError("image must be a string or list")

        reference = optional_params.pop("reference_image_urls", None)
        if reference is None:
            reference = optional_params.pop("referenceImageUrls", None)
        if reference is None:
            return None
        if not isinstance(reference, list) or len(reference) == 0:
            raise ValueError("reference_image_urls must be a non-empty list")
        if len(reference) > self.MAX_REFERENCE_IMAGES:
            raise ValueError(
                f"reference_image_urls supports up to {self.MAX_REFERENCE_IMAGES} images"
            )
        urls = []
        for idx, url in enumerate(reference, start=1):
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"reference_image_urls[{idx}] must be a non-empty string")
            urls.append(url.strip())
        return urls

    @staticmethod
    def _image_response_from_body(
        body: dict, *, response_cost: Optional[float] = None
    ) -> ImageResponse:
        data = body.get("data") or []
        if not data:
            err = body.get("error")
            if isinstance(err, dict):
                raise SeedreamException(normalize_error(err.get("message", err)))
            raise SeedreamException(normalize_error(body))

        out: List[ImageObject] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            b64 = item.get("b64_json")
            if url:
                out.append(ImageObject(url=url))
            elif b64:
                out.append(ImageObject(b64_json=b64))

        if not out:
            raise SeedreamException("ModelArk response did not include any image url or b64_json")

        resp = ImageResponse(created=int(body.get("created") or time.time()), data=out)
        if response_cost is not None:
            try:
                resp._hidden_params["response_cost"] = float(response_cost)
            except Exception:
                pass
        return resp

    def _compute_response_cost(
        self,
        *,
        ark_model: str,
        body: dict,
        tools: Any,
        requested_n: Optional[int],
    ) -> float:
        data = body.get("data") or []
        image_count = len([d for d in data if isinstance(d, dict)])
        if image_count <= 0:
            try:
                image_count = max(1, int(requested_n or 1))
            except (TypeError, ValueError):
                image_count = 1

        usage = body.get("usage")
        if isinstance(usage, dict):
            for key in ("generated_images", "output_images", "image_count", "total_images"):
                raw = usage.get(key)
                if raw is not None:
                    try:
                        image_count = max(image_count, int(raw))
                    except (TypeError, ValueError):
                        pass

        cost = image_count * self._price_per_image(ark_model)
        if self._uses_web_search(tools):
            cost += self._web_search_price()
        return cost

    def _prepare_request(
        self, prompt: str, model: str, optional_params: dict
    ) -> tuple[str, dict, dict]:
        optional_params = dict(optional_params or {})

        api_key = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key:
            raise ValueError("BYTEDANCE_API_KEY is required for seedream")

        ark_model = self._resolve_upstream_model(model)
        prompt_text = (prompt or "").strip()
        if not prompt_text:
            raise ValueError("prompt is required for Seedream image generation")

        image_input = self._collect_image_inputs(optional_params)

        payload: dict[str, Any] = {
            "model": ark_model,
            "prompt": prompt_text,
        }
        if image_input is not None:
            payload["image"] = image_input

        tools_value: Any = None
        for key in _ARK_PASSTHROUGH_KEYS:
            if key in optional_params:
                value = optional_params.pop(key)
                if key == "tools":
                    tools_value = value
                payload[key] = value

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._ark_base()}/images/generations"
        return url, payload, headers

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
        client: Any = None,
        **kwargs: Any,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_request(prompt, model, optional_params)
        tools = payload.get("tools")
        requested_n = payload.get("n")

        async with httpx.AsyncClient(timeout=timeout or 300) as http:
            response = await http.post(url, headers=headers, json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json()
                except Exception:
                    detail = e.response.text
                raise SeedreamException(normalize_error(detail)) from e

            body = response.json()
            cost = self._compute_response_cost(
                ark_model=payload["model"],
                body=body,
                tools=tools,
                requested_n=requested_n,
            )
            return self._image_response_from_body(body, response_cost=cost)


seedream = SeedreamLLM()
