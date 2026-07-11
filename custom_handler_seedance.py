"""BytePlus Seedance 2.0 custom LiteLLM handler.

Design (long-poll with bounded wait):

* ``POST /v1/images/generations`` with ``prompt`` (no ``seedance_task_id``):
    1. Submit ARK task at ``{ARK_BASE}/contents/generations/tasks``.
    2. Poll for up to ``SEEDANCE_SYNC_WAIT_S`` seconds (default 240s, safely
       below the common 300s edge timeouts on Cloud Run LBs / clients).
    3. If the task completes in that window, return ``data[0].url`` = the MP4.
    4. Otherwise return ``data[0].url = "seedance-task://<task_id>"`` and
       ``data[0].revised_prompt = "running"`` so the OpenAI-shape response is
       still valid and the client can resume polling.

* ``POST /v1/images/generations`` with ``seedance_task_id=<id>``: single
  GET against ARK, returning either the running placeholder URL or the final
  MP4 URL. Light-weight, ~1s, no risk of timing out.

Key env vars:
    BYTEDANCE_API_KEY              ARK bearer token (required)
    SEEDANCE_ARK_BASE              override ARK base URL
    SEEDANCE_ARK_MODEL             override default model id
    SEEDANCE_SYNC_WAIT_S           bounded blocking window on submit (default 240)
    SEEDANCE_POLL_TIMEOUT_S        upper bound on synchronous poll loops (default 1200)
    SEEDANCE_POLL_INTERVAL_S       seconds between polls (default 10)
    SEEDANCE_BLOCKING_POLL=1       legacy: wait up to POLL_TIMEOUT_S synchronously
    SEEDANCE_PRICE_PER_MTOK        $/1M output tokens (text+image input); default 7.00
    SEEDANCE_PRICE_PER_MTOK_VIDEO  $/1M output tokens when an input video is sent; default 4.30
    SEEDANCE_PRICE_PER_MTOK_FAST          fast tier rate (no input video); default 5.60
    SEEDANCE_PRICE_PER_MTOK_FAST_VIDEO    fast tier rate (with input video); default 3.30
    SEEDANCE_TASK_LEDGER_PATH          append-only JSONL log of every submitted task id
                                       (recover videos after client/proxy failures)

Per-request overrides (passed in ``optional_params``):
    seedance_task_id        poll an existing task instead of submitting
    wait_seconds            override SEEDANCE_SYNC_WAIT_S for this request
    async_submit            ``True`` -> wait_seconds=0; ``False`` -> wait_seconds=POLL_TIMEOUT_S
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple, Union

import httpx
from litellm import CustomLLM
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.types.utils import ImageObject, ImageResponse

from custom_handler_common import normalize_error
from legacy_usage import log_legacy_video_usage


DEFAULT_ARK_MODEL = "dreamina-seedance-2-0-260128"
DEFAULT_ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"

TASK_URL_SCHEME = "seedance-task://"

# Per-process LRU of task_ids we have already attributed cost to. Re-polling a
# completed task should not double-bill the caller. The cap is intentionally
# generous: each entry is ~50 bytes so 50k tasks ≈ 2.5 MB.
_BILLED_TASKS_CAPACITY = 50_000

DEFAULT_SYNC_WAIT_S = 240.0  # bounded blocking window; safely below 300s edge timeouts
DEFAULT_POLL_INTERVAL_S = 10.0
DEFAULT_POLL_TIMEOUT_S = 1200.0  # absolute cap on synchronous wait when explicitly opted in

# Bump when deploying handler behavior changes (verify in Cloud Run logs / support).
SEEDANCE_HANDLER_VERSION = "2026-05-24-poll-default-0"

# BytePlus ARK official rates (USD per 1M output tokens) for Seedance 2.0 family at
# 480p/720p. 1080p is roughly +10% but we keep a single rate per model since the
# proxy bills off the upstream-returned completion_tokens.
DEFAULT_PRICE_PER_MTOK = 7.00
DEFAULT_PRICE_PER_MTOK_VIDEO = 4.30
DEFAULT_PRICE_PER_MTOK_FAST = 5.60
DEFAULT_PRICE_PER_MTOK_FAST_VIDEO = 3.30


def _task_url(task_id: str) -> str:
    return f"{TASK_URL_SCHEME}{task_id}"


def _append_task_ledger(entry: dict) -> None:
    """Best-effort audit log so task ids survive client/proxy timeouts."""
    path = (os.environ.get("SEEDANCE_TASK_LEDGER_PATH") or "").strip()
    if not path:
        return
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _running_response(task_id: str, status: str) -> ImageResponse:
    """OpenAI-shape response that round-trips through LiteLLM's serializer.

    Uses declared ImageObject fields (``url``, ``revised_prompt``) so they
    survive Pydantic round-tripping (ad-hoc kwargs on ImageResponse get
    dropped by the proxy at response time).
    """
    return ImageResponse(
        created=int(time.time()),
        data=[ImageObject(url=_task_url(task_id), revised_prompt=status)],
    )


def _final_response(
    video_url: str,
    *,
    response_cost: Optional[float] = None,
    task_id: Optional[str] = None,
) -> ImageResponse:
    # revised_prompt carries the ARK task id on success so callers can recover
    # later even if they only stored the final HTTP response.
    resp = ImageResponse(
        created=int(time.time()),
        data=[ImageObject(url=video_url, revised_prompt=task_id)],
    )
    if response_cost is not None:
        try:
            resp._hidden_params["response_cost"] = float(response_cost)
        except Exception:  # _hidden_params should always exist; be defensive
            pass
    return resp


class SeedanceException(Exception):
    """Raised when BytePlus ARK Seedance task submission or polling fails."""


class SeedanceLLM(CustomLLM):
    """Wraps the BytePlus ARK Seedance 2.0 async content generation API."""

    MAX_REFERENCE_IMAGES = 7

    def __init__(self) -> None:
        super().__init__()
        # OrderedDict acts as a tiny LRU set of billed task ids.
        self._billed_tasks: "OrderedDict[str, None]" = OrderedDict()

    def _mark_billed(self, task_id: str) -> bool:
        """Returns True the first time a task_id is seen, False thereafter."""
        if task_id in self._billed_tasks:
            self._billed_tasks.move_to_end(task_id)
            return False
        self._billed_tasks[task_id] = None
        while len(self._billed_tasks) > _BILLED_TASKS_CAPACITY:
            self._billed_tasks.popitem(last=False)
        return True

    # --- env / configuration helpers -----------------------------------------

    @staticmethod
    def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return max(minimum, float(raw))
        except ValueError:
            return default

    def _sync_wait_s(self) -> float:
        return self._env_float("SEEDANCE_SYNC_WAIT_S", DEFAULT_SYNC_WAIT_S)

    def _poll_interval_s(self) -> float:
        return self._env_float("SEEDANCE_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S)

    def _poll_timeout_s(self) -> float:
        return self._env_float("SEEDANCE_POLL_TIMEOUT_S", DEFAULT_POLL_TIMEOUT_S, minimum=1.0)

    def _legacy_blocking_default(self) -> bool:
        return os.environ.get("SEEDANCE_BLOCKING_POLL", "").strip().lower() in ("1", "true", "yes")

    def _ark_base(self) -> str:
        return (os.environ.get("SEEDANCE_ARK_BASE") or DEFAULT_ARK_BASE).rstrip("/")

    def _resolve_upstream_model(self, model: str) -> str:
        if model and model.strip():
            return model.strip()
        return os.environ.get("SEEDANCE_ARK_MODEL") or DEFAULT_ARK_MODEL

    def _is_fast_model(self, ark_model: str) -> bool:
        return "fast" in (ark_model or "").lower()

    def _price_per_mtok(self, ark_model: str, *, has_input_video: bool) -> float:
        if self._is_fast_model(ark_model):
            if has_input_video:
                return self._env_float(
                    "SEEDANCE_PRICE_PER_MTOK_FAST_VIDEO", DEFAULT_PRICE_PER_MTOK_FAST_VIDEO
                )
            return self._env_float("SEEDANCE_PRICE_PER_MTOK_FAST", DEFAULT_PRICE_PER_MTOK_FAST)
        if has_input_video:
            return self._env_float("SEEDANCE_PRICE_PER_MTOK_VIDEO", DEFAULT_PRICE_PER_MTOK_VIDEO)
        return self._env_float("SEEDANCE_PRICE_PER_MTOK", DEFAULT_PRICE_PER_MTOK)

    @staticmethod
    def _resolve_wait_seconds(
        optional_params: dict, *, default: float, hard_max: float
    ) -> float:
        explicit = optional_params.pop("wait_seconds", None)
        if explicit is not None:
            try:
                return max(0.0, min(float(explicit), hard_max))
            except (TypeError, ValueError):
                raise ValueError("wait_seconds must be a number")

        async_submit = optional_params.pop("async_submit", None)
        if async_submit is True:
            return 0.0
        if async_submit is False:
            return hard_max

        return min(default, hard_max)

    # --- payload helpers ------------------------------------------------------

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
                raise ValueError(
                    f"reference_image_urls supports up to {self.MAX_REFERENCE_IMAGES} images"
                )

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

    def _collect_video_urls(self, optional_params: dict) -> List[str]:
        """Returns 0..3 ordered https video URLs (for edit/extend flows)."""
        urls: List[str] = []
        primary = optional_params.pop("video_url", None) or optional_params.pop("video", None)
        if primary:
            urls.append(primary)
        many = optional_params.pop("videos", None) or optional_params.pop("video_urls", None)
        if many is not None:
            if not isinstance(many, list):
                raise ValueError("videos must be a list of URL strings")
            urls.extend(str(u) for u in many if u)
        for idx, url in enumerate(urls, start=1):
            if not (isinstance(url, str) and url.startswith("https://")):
                raise ValueError(f"video URL #{idx} must be an https:// URL")
        if len(urls) > 3:
            raise ValueError("Seedance supports at most 3 reference videos per request")
        return urls

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

    def _ark_video_content_items(self, urls: List[str]) -> List[dict]:
        return [
            {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
            for url in urls
        ]

    def _extract_poll_task_id(self, optional_params: dict, prompt: Optional[str]) -> Optional[str]:
        for key in ("seedance_task_id", "poll_task_id", "task_id"):
            raw = optional_params.pop(key, None)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        if isinstance(prompt, str) and prompt.startswith(TASK_URL_SCHEME):
            return prompt[len(TASK_URL_SCHEME) :].strip() or None
        return None

    # --- HTTP layer -----------------------------------------------------------

    @staticmethod
    def _client_timeout_floor(*, sync_wait: float, blocking: bool, poll_timeout: float) -> float:
        """httpx timeout must outlive any awaited poll loop."""
        if blocking:
            return max(60.0, poll_timeout + 30.0)
        return max(60.0, sync_wait + 30.0)

    @staticmethod
    def _resolve_client_timeout(
        timeout: Optional[Union[float, httpx.Timeout]], *, floor: float
    ) -> float:
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            return max(floor, float(timeout))
        return floor

    async def _submit_task(
        self,
        *,
        http: httpx.AsyncClient,
        ark_base: str,
        headers: dict,
        submit_body: dict,
    ) -> str:
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
            raise SeedanceException(
                normalize_error(submit_data.get("error", {}).get("message", submit_data))
            )
        return str(task_id)

    async def _get_task(
        self, *, http: httpx.AsyncClient, ark_base: str, headers: dict, task_id: str
    ) -> dict:
        poll = await http.get(
            f"{ark_base}/contents/generations/tasks/{task_id}",
            headers={"Authorization": headers["Authorization"]},
        )
        try:
            poll.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
            raise SeedanceException(normalize_error(detail)) from e
        return poll.json()

    def _interpret_task(
        self, body: dict, *, task_id: str, has_input_video: bool
    ) -> ImageResponse:
        """Map an ARK task GET body to an ImageResponse.

        Returns a running placeholder when not yet terminal so callers can
        decide whether to keep polling.
        """
        status = body.get("status") or "running"

        if status == "succeeded":
            video_url = (body.get("content") or {}).get("video_url")
            if not video_url:
                raise SeedanceException(normalize_error("missing video_url in succeeded task"))
            cost: Optional[float] = None
            if self._mark_billed(task_id):
                cost = self._compute_cost(
                    ark_model=body.get("model", ""),
                    usage=body.get("usage") or {},
                    has_input_video=has_input_video,
                )
            return _final_response(video_url, response_cost=cost, task_id=task_id)

        if status in ("failed", "expired"):
            err = body.get("error") or {}
            raise SeedanceException(normalize_error(err.get("message", status)))

        return _running_response(task_id, status)

    def _compute_cost(
        self, *, ark_model: str, usage: dict, has_input_video: bool
    ) -> Optional[float]:
        try:
            tokens = int(usage.get("completion_tokens") or usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= 0:
            return None
        rate_per_mtok = self._price_per_mtok(ark_model, has_input_video=has_input_video)
        return tokens * rate_per_mtok / 1_000_000.0

    async def _poll_until_done_or_deadline(
        self,
        *,
        http: httpx.AsyncClient,
        ark_base: str,
        headers: dict,
        task_id: str,
        deadline_monotonic: float,
        poll_interval: float,
        has_input_video: bool,
    ) -> ImageResponse:
        """Poll repeatedly until either the task is terminal or we hit deadline.

        Returns a final ImageResponse on success, or a running placeholder if
        the deadline elapses first.
        """
        while True:
            body = await self._get_task(
                http=http, ark_base=ark_base, headers=headers, task_id=task_id
            )
            status = body.get("status")
            if status in ("succeeded", "failed", "expired"):
                return self._interpret_task(
                    body, task_id=task_id, has_input_video=has_input_video
                )

            now = time.monotonic()
            if now >= deadline_monotonic:
                return _running_response(task_id, status or "running")

            await asyncio.sleep(min(poll_interval, max(0.0, deadline_monotonic - now)))

    # --- main entry -----------------------------------------------------------

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

        api_key_env = os.environ.get("BYTEDANCE_API_KEY")
        if not api_key_env:
            raise ValueError("BYTEDANCE_API_KEY is required for seedance")

        ark_base = self._ark_base()
        ark_model = self._resolve_upstream_model(model)
        poll_interval = self._poll_interval_s()
        poll_timeout = self._poll_timeout_s()
        headers = {
            "Authorization": f"Bearer {api_key_env}",
            "Content-Type": "application/json",
        }

        poll_task_id = self._extract_poll_task_id(optional_params, prompt)
        log_legacy_video_usage(
            provider="byteplus",
            model=model,
            operation="retrieve" if poll_task_id else "submit_and_poll",
        )

        legacy_blocking_default = self._legacy_blocking_default()
        default_sync_wait = poll_timeout if legacy_blocking_default else self._sync_wait_s()

        # ---- Poll-only branch -------------------------------------------------
        if poll_task_id:
            # Never inherit submit defaults (240s) or async_submit:false (1200s) here.
            optional_params.pop("async_submit", None)
            sync_wait_s = self._resolve_wait_seconds(
                optional_params,
                default=0.0,
                hard_max=poll_timeout,
            )
            client_timeout = self._resolve_client_timeout(
                timeout,
                floor=self._client_timeout_floor(
                    sync_wait=sync_wait_s, blocking=False, poll_timeout=poll_timeout
                ),
            )
            async with httpx.AsyncClient(timeout=client_timeout) as http:
                if sync_wait_s <= 0.0:
                    body = await self._get_task(
                        http=http, ark_base=ark_base, headers=headers, task_id=poll_task_id
                    )
                    return self._interpret_task(
                        body,
                        task_id=poll_task_id,
                        has_input_video=False,  # unknown on poll; cost still computed from tokens
                    )

                deadline = time.monotonic() + sync_wait_s
                return await self._poll_until_done_or_deadline(
                    http=http,
                    ark_base=ark_base,
                    headers=headers,
                    task_id=poll_task_id,
                    deadline_monotonic=deadline,
                    poll_interval=poll_interval,
                    has_input_video=False,
                )

        # ---- Submit branch ----------------------------------------------------
        sync_wait_s = self._resolve_wait_seconds(
            optional_params,
            default=default_sync_wait,
            hard_max=poll_timeout,
        )

        if not prompt or not prompt.strip():
            raise ValueError("A non-empty prompt is required to submit a Seedance task")

        image_urls, reference_image_batch = self._collect_image_urls(optional_params)
        video_urls = self._collect_video_urls(optional_params)
        has_input_video = bool(video_urls)

        # Default 480p / 1:1 / 4s matches the pre–bounded-wait handler so omitted
        # params do not fall through to a slower ARK default (e.g. 720p/1080p).
        resolution = optional_params.pop("resolution", "480p")
        ratio = optional_params.pop("ratio", "1:1")
        duration = optional_params.pop("duration", 4)
        if isinstance(duration, float) and duration.is_integer():
            duration = int(duration)
        if not isinstance(duration, int):
            raise ValueError("duration must be an integer (seconds)")

        generate_audio = optional_params.pop(
            "generate_audio", optional_params.pop("generateAudio", False)
        )
        watermark = optional_params.pop("watermark", False)

        content: List[dict] = [{"type": "text", "text": prompt}]
        content.extend(
            self._ark_image_content_items(image_urls, reference_batch=reference_image_batch)
        )
        content.extend(self._ark_video_content_items(video_urls))

        submit_body: dict = {
            "model": ark_model,
            "content": content,
            "ratio": ratio,
            "duration": duration,
            "generate_audio": bool(generate_audio),
            "watermark": bool(watermark),
        }
        submit_body["resolution"] = resolution

        client_timeout = self._resolve_client_timeout(
            timeout,
            floor=self._client_timeout_floor(
                sync_wait=sync_wait_s,
                blocking=legacy_blocking_default and sync_wait_s >= poll_timeout,
                poll_timeout=poll_timeout,
            ),
        )

        async with httpx.AsyncClient(timeout=client_timeout) as http:
            task_id = await self._submit_task(
                http=http, ark_base=ark_base, headers=headers, submit_body=submit_body
            )
            _append_task_ledger(
                {
                    "task_id": task_id,
                    "ark_model": ark_model,
                    "resolution": resolution,
                    "ratio": ratio,
                    "duration": duration,
                    "has_input_video": has_input_video,
                    "prompt_preview": (prompt or "")[:500],
                }
            )

            if sync_wait_s <= 0.0:
                return _running_response(task_id, "submitted")

            deadline = time.monotonic() + sync_wait_s
            return await self._poll_until_done_or_deadline(
                http=http,
                ark_base=ark_base,
                headers=headers,
                task_id=task_id,
                deadline_monotonic=deadline,
                poll_interval=poll_interval,
                has_input_video=has_input_video,
            )


seedance = SeedanceLLM()
