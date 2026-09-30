"""BytePlus ModelArk Seedream 5 custom LiteLLM handler.

Sync OpenAI-compatible image generation at ModelArk::

    POST {ARK_BASE}/images/generations

ModelArk docs: https://docs.byteplus.com/en/docs/ModelArk/1541523
Pricing: https://docs.byteplus.com/en/docs/ModelArk/1544106

Env:
    BYTEDANCE_API_KEY                 ModelArk bearer token (required)
    SEEDREAM_ARK_BASE                 override ARK base (default BytePlus ap-southeast)
"""

from __future__ import annotations

import base64
import inspect
import logging
import mimetypes
import json
import os
import re
import time
from typing import Any, List, Optional, Union

import httpx
from litellm import CustomLLM
from litellm.types.utils import ImageObject, ImageResponse
from pydantic import SerializeAsAny

from custom_handler_common import normalize_error

DEFAULT_ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"

DEFAULT_MODEL_5_0 = "seedream-5-0-260128"
DEFAULT_MODEL_5_0_LITE = "seedream-5-0-lite-260128"
DEFAULT_MODEL_5_0_PRO = "dola-seedream-5-0-pro-260628"


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
    "layer_decomposition",
    "background",
)

_PRESERVED_PARAM_ALIASES = {
    "seedream_size": "size",
    "seedream_output_count": "n",
    "seedream_response_format": "response_format",
    "seedream_output_format": "output_format",
    "seedream_stream": "stream",
}

_LOGGER = logging.getLogger(__name__)


class SeedreamImageResponse(ImageResponse):
    # ImageResponse annotates data with OpenAI's narrower Image base class.
    # Preserve LiteLLM's provider metadata when FastAPI serializes each image.
    data: List[SerializeAsAny[ImageObject]]


class SeedreamException(Exception):
    """Raised when ModelArk Seedream image generation fails."""


def parse_image_events(text: str) -> dict:
    """Collect ModelArk SSE results without losing partial successes or usage."""
    body: dict[str, Any] = {"data": [], "errors": []}
    images: dict[int, dict] = {}
    completed = False
    # SSE permits comments, CRLF and multiline data. The last event need not
    # end with a blank line when the upstream connection closes.
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        event_type = event.get("type") or next((line[6:].strip() for line in block.splitlines() if line.startswith("event:")), "")
        for key in ("created", "model", "id"):
            if key in event:
                body[key] = event[key]
        if event_type == "image_generation.partial_succeeded":
            images[event.get("image_index", len(images))] = {
                key: value for key, value in event.items()
                if key not in {"type", "created", "model", "id", "usage"}
            }
        elif event_type == "image_generation.completed":
            completed = True
            body["usage"] = event.get("usage") or {}
        elif event_type in {"error", "image_generation.partial_failed"}:
            body["errors"].append(event)
            body["error"] = event.get("error") or event
    body["data"] = [images[index] for index in sorted(images)]
    body["stream_completed"] = completed
    return body


class SeedreamLLM(CustomLLM):
    """Wraps BytePlus ModelArk Seedream 5 image generation (sync, OpenAI-compatible)."""

    MAX_REFERENCE_IMAGES = 14
    MAX_REFERENCE_IMAGES_PRO = 10

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

    @staticmethod
    def _is_pro_model(ark_model: str) -> bool:
        return "seedream-5-0-pro" in (ark_model or "").lower()

    def _max_reference_images(self, ark_model: str) -> int:
        return self.MAX_REFERENCE_IMAGES_PRO if self._is_pro_model(ark_model) else self.MAX_REFERENCE_IMAGES


    @staticmethod
    def _restore_preserved_params(optional_params: dict) -> None:
        for private_key, public_key in _PRESERVED_PARAM_ALIASES.items():
            if private_key in optional_params:
                optional_params[public_key] = optional_params.pop(private_key)

    def _validate_pro_params(self, optional_params: dict, *, reference_count: int) -> None:
        if reference_count > self.MAX_REFERENCE_IMAGES_PRO:
            raise ValueError(
                f"seedream-5.0-pro supports up to {self.MAX_REFERENCE_IMAGES_PRO} reference images"
            )

        layers = optional_params.get("layer_decomposition") is True
        if layers and reference_count != 1:
            raise ValueError("seedream-5.0-pro layer decomposition requires exactly one reference image")
        raw_size = str(optional_params.get("size") or ("auto" if layers else "1K")).strip()
        normalized_size = raw_size.lower()
        if layers and normalized_size not in {"1k", "1.5k", "2k", "auto"}:
            raise ValueError("seedream-5.0-pro layer size must be 1K, 1.5K, 2K, or auto")
        if normalized_size not in {"1k", "1.5k", "2k"} and not (layers and normalized_size == "auto"):
            match = re.fullmatch(r"(\d+)x(\d+)", normalized_size)
            if not match:
                raise ValueError("seedream-5.0-pro size must be 1K, 1.5K, 2K, or valid pixel dimensions")
            width, height = (int(value) for value in match.groups())
            pixels = width * height
            ratio = width / height if height else 0
            if not (921_600 <= pixels <= 4_624_220 and 1 / 16 <= ratio <= 16):
                raise ValueError(
                    "seedream-5.0-pro pixel dimensions must satisfy its published pixel and aspect-ratio limits"
                )

        output_count = optional_params.get("n", 1)
        if isinstance(output_count, bool):
            raise ValueError("seedream-5.0-pro n must be 1")
        try:
            output_count = int(output_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("seedream-5.0-pro n must be 1") from exc
        if output_count != 1:
            raise ValueError("seedream-5.0-pro supports exactly one output image per request")

        output_format = str(optional_params.get("output_format") or "png").strip().lower()
        if output_format not in {"png", "jpeg", "jpg"}:
            raise ValueError("seedream-5.0-pro output_format must be png or jpeg")
        if optional_params.get("background") == "transparent":
            if reference_count != 1:
                raise ValueError("seedream-5.0-pro transparent editing requires exactly one alpha-channel reference image")
            if output_format != "png":
                raise ValueError("seedream-5.0-pro transparent output requires png")
        if optional_params.get("stream") is True:
            raise ValueError("seedream-5.0-pro does not support streaming output")
        sequential = optional_params.get("sequential_image_generation")
        if sequential is not None and sequential is not False and sequential != "disabled":
            raise ValueError("seedream-5.0-pro does not support sequential multi-image generation")


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

    def _collect_image_inputs(
        self, optional_params: dict, *, ark_model: str
    ) -> Optional[Union[str, List[str]]]:
        """Normalize OpenAI / gateway aliases into ModelArk ``image`` (string or list)."""
        max_reference_images = self._max_reference_images(ark_model)
        if "image_urls" in optional_params:
            raw = optional_params.pop("image_urls")
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if u]
                if len(urls) > max_reference_images:
                    raise ValueError(
                        f"image_urls supports up to {max_reference_images} images"
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
                if len(urls) > max_reference_images:
                    raise ValueError(f"images supports up to {max_reference_images} images")
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
                if len(urls) > max_reference_images:
                    raise ValueError(f"image supports up to {max_reference_images} images")
                return urls
            raise ValueError("image must be a string or list")

        reference = optional_params.pop("reference_image_urls", None)
        if reference is None:
            reference = optional_params.pop("referenceImageUrls", None)
        if reference is None:
            return None
        if not isinstance(reference, list) or len(reference) == 0:
            raise ValueError("reference_image_urls must be a non-empty list")
        if len(reference) > max_reference_images:
            raise ValueError(
                f"reference_image_urls supports up to {max_reference_images} images"
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
            if url or b64:
                details = {key: value for key, value in item.items()
                           if key not in {"url", "b64_json", "revised_prompt", "provider_specific_fields"}}
                details = {**(item.get("provider_specific_fields") or {}), **details}
                # Pinned ImageObject.__init__ discards arbitrary kwargs. Its
                # explicit provider_specific_fields survives ImageResponse's copy.
                out.append(ImageObject(url=url, b64_json=b64, revised_prompt=item.get("revised_prompt"),
                                       provider_specific_fields=details or None))

        if not out:
            raise SeedreamException("ModelArk response did not include any image url or b64_json")

        resp = SeedreamImageResponse(created=int(body.get("created") or time.time()), data=out)
        for item in resp.data:
            details = item.provider_specific_fields or {}
            for name in ("size", "output_format", "image_index", "z_index", "bounding_box", "name", "description"):
                if name in details:
                    setattr(item, name, details[name])
        if "stream_completed" in body:
            resp["stream_completed"] = body["stream_completed"]
        errors = list(body.get("errors") or [])
        errors.extend({"image_index": index, "error": item["error"]}
                      for index, item in enumerate(data) if isinstance(item, dict) and item.get("error"))
        if errors:
            resp["errors"] = errors
        usage = dict(body.get("usage") or {})
        usage["output_images"] = len(out)
        sizes = [item.get("size") for item in data if isinstance(item, dict) and (item.get("url") or item.get("b64_json"))]
        try:
            pixels = [int(size.lower().split("x")[0]) * int(size.lower().split("x")[1]) for size in sizes]
            small = sum(value <= 2610000 for value in pixels)
            large = sum(value > 2610000 for value in pixels)
            layers = body.get("_layer_decomposition") is True
            usage["output_images_small"] = 0 if layers else small
            usage["output_images_large"] = 0 if layers else large
            usage["layer_images_small"] = small if layers else 0
            usage["layer_images_large"] = large if layers else 0
        except (AttributeError, ValueError, IndexError):
            pass  # Missing actual dimensions remain unresolved for Pro pricing.
        reference_count = usage.get("input_images", body.get("_reference_count"))
        if reference_count is not None:
            usage["billable_reference_images"] = max(0, reference_count - 1)
        resp._hidden_params["gateway_usage"] = usage
        resp._hidden_params["gateway_served_model"] = body.get("model")
        resp._hidden_params["gateway_provider_request_id"] = body.get("id")
        if response_cost is not None:
            try:
                resp._hidden_params["response_cost"] = float(response_cost)
            except Exception:
                pass
        return resp

    def _compute_response_cost(self, *, ark_model, body, tools, requested_n):
        # Calculated centrally from measured usage and a pinned verified profile.
        # Enabling a search tool or requesting n images is not billable evidence.
        return None

    def _prepare_request(
        self, prompt: str, model: str, optional_params: dict
    ) -> tuple[str, dict, dict]:
        optional_params = dict(optional_params or {})
        self._restore_preserved_params(optional_params)

        api_key = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key:
            raise ValueError("BYTEDANCE_API_KEY is required for seedream")

        ark_model = self._resolve_upstream_model(model)
        prompt_text = (prompt or "").strip()
        layers = optional_params.get("layer_decomposition") is True
        if layers and not self._is_pro_model(ark_model):
            raise ValueError("layer decomposition is supported only by seedream-5.0-pro")
        if not prompt_text and not layers:
            raise ValueError("prompt is required for Seedream image generation")

        image_input = self._collect_image_inputs(optional_params, ark_model=ark_model)
        reference_count = len(image_input) if isinstance(image_input, list) else int(image_input is not None)
        if self._is_pro_model(ark_model):
            self._validate_pro_params(optional_params, reference_count=reference_count)

        payload: dict[str, Any] = {"model": ark_model}
        if prompt_text:
            payload["prompt"] = prompt_text
        if image_input is not None:
            payload["image"] = image_input

        tools_value: Any = None
        for key in _ARK_PASSTHROUGH_KEYS:
            if key in optional_params:
                value = optional_params.pop(key)
                if key == "tools":
                    tools_value = value
                payload[key] = value

        _LOGGER.info(
            "seedream.request model=%s requested_size=%s resolved_size=%s transmitted_size=%s reference_count=%d output_count=%s output_format=%s",
            ark_model,
            payload.get("size"),
            payload.get("size"),
            payload.get("size"),
            reference_count,
            payload.get("n", 1),
            payload.get("output_format"),
        )

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

            body = (parse_image_events(response.text)
                    if "text/event-stream" in response.headers.get("content-type", "")
                    or payload.get("stream") is True else response.json())
            cost_body = dict(body)
            cost_body["_requested_size"] = payload.get("size")
            cost_body["_layer_decomposition"] = payload.get("layer_decomposition") is True
            image_input = payload.get("image")
            cost_body["_reference_count"] = (
                len(image_input) if isinstance(image_input, list) else int(image_input is not None)
            )
            cost = self._compute_response_cost(
                ark_model=payload["model"],
                body=cost_body,
                tools=tools,
                requested_n=requested_n,
            )
            cost_body.setdefault("model", payload["model"])
            return self._image_response_from_body(cost_body, response_cost=cost)

    @staticmethod
    async def _edit_image(value: Any) -> str:
        """Convert uploaded edit files into ModelArk data URLs without re-encoding."""
        filename = getattr(value, "filename", None) or getattr(value, "name", None)
        mime = getattr(value, "content_type", None)
        if isinstance(value, tuple) and len(value) >= 2:
            filename, value, *rest = value
            mime = rest[0] if rest else mime
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            url = value.get("url") or value.get("image_url")
            if isinstance(url, str):
                return url
            raise ValueError("Seedream edit images require a URL or uploaded image bytes")
        if hasattr(value, "read"):
            value = value.read()
            if inspect.isawaitable(value):
                value = await value
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError("Seedream edit images require a URL or uploaded image bytes")
        mime = mime or mimetypes.guess_type(str(filename or ""))[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(bytes(value)).decode('ascii')}"

    async def aimage_edit(
        self, model: str, image: Any, prompt: Optional[str], model_response: ImageResponse,
        api_key: Optional[str], api_base: Optional[str], optional_params: dict, logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None, client: Any = None, **kwargs: Any,
    ) -> ImageResponse:
        params = dict(optional_params or {})
        if params.get("mask") is not None or kwargs.get("mask") is not None:
            raise ValueError("Seedream edits use prompt coordinates or reference markings; masks are unsupported")
        if image is None:
            image = self._collect_image_inputs(params, ark_model=model)
        if image is None:
            raise ValueError("image is required for Seedream image editing")
        sources = image if isinstance(image, list) else [image]
        if not sources:
            raise ValueError("image is required for Seedream image editing")
        # The explicit edit input owns ordering even when legacy aliases coexist.
        for key in ("image_urls", "images", "image", "reference_image_urls", "referenceImageUrls"):
            params.pop(key, None)
        params["image"] = [await self._edit_image(value) for value in sources]
        return await self.aimage_generation(model=model, prompt=prompt or "", model_response=model_response,
            api_key=api_key, api_base=api_base, optional_params=params, logging_obj=logging_obj,
            timeout=timeout, client=client, **kwargs)


seedream = SeedreamLLM()
