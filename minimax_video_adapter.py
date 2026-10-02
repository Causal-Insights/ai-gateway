"""Direct MiniMax H3 hosted API, using the shared durable job lifecycle."""
import asyncio
import base64
import json
import os
from fractions import Fraction
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from generation_job_adapters import BaseAdapter, ProviderAdapterError, _download_media, _json_request, probe_media_bytes
from generation_job_models import ProviderStatus, ProviderSubmission
from minimax_video_contract import MODELS, ROUTE, ADAPTER_REVISION, validate


def _invalid(message):
    return ProviderAdapterError(message, code="INVALID_MEDIA_INPUT")


def inspect_media(kind, content, filename):
    """Inspect the original bytes; never transcode or silently reduce references."""
    maximum = {"image": 30, "video": 50, "audio": 15}[kind] * 1_000_000
    if not content or len(content) > maximum:
        raise _invalid(f"H3 {kind} must be nonempty and at most {maximum // 1_000_000} MB.")
    probe = probe_media_bytes(content, suffix=PurePosixPath(filename).suffix or ".bin")
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and
                  (kind == "image" or not (s.get("disposition") or {}).get("attached_pic"))), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    formats = set((probe.get("format", {}).get("format_name") or "").split(","))
    if kind in {"image", "video"}:
        if not video:
            raise _invalid("Media dimensions could not be inspected; provide a readable supported image or video.")
        width, height = video.get("width", 0), video.get("height", 0)
        if not (256 <= width <= 5760 and 256 <= height <= 5760 and .4 <= width / height <= 2.5):
            raise _invalid("H3 image/video dimensions must be 256–5760 pixels with aspect ratio 0.4–2.5.")
    if kind == "image":
        if content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            mime = "image/webp"
        elif content[4:8] == b"ftyp" and any(brand in content[8:40] for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")):
            mime = "image/heif"
        else:
            raise _invalid("H3 images must be JPEG, PNG, WebP, HEIC, or HEIF.")
        return mime, 0
    if kind == "video":
        if not formats.intersection({"mov", "mp4"}) or video.get("codec_name") not in {"h264", "hevc"}:
            raise _invalid("H3 videos must be MP4/MOV with H.264 or H.265 video.")
        if any(s.get("codec_name") not in {"aac", "mp3"} for s in audio):
            raise _invalid("H3 reference-video audio must be AAC or MP3.")
        try:
            fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
        except (ValueError, ZeroDivisionError):
            fps = 0
        if not 23.976 <= fps <= 60:
            raise _invalid("H3 reference-video frame rate must be 23.976–60 FPS.")
        mime = "video/quicktime" if content[8:12] == b"qt  " else "video/mp4"
    else:
        if not audio or not formats.intersection({"wav", "mp3"}):
            raise _invalid("H3 reference audio must be WAV or MP3.")
        mime = "audio/wav" if "wav" in formats else "audio/mp3"
    try:
        duration = float(probe.get("format", {}).get("duration", "nan"))
    except (ValueError, TypeError):
        duration = float("nan")
    if not 2 <= duration <= 15:
        raise _invalid("Each H3 reference video/audio clip must be 2–15 seconds.")
    return mime, duration


class MiniMaxAdapter(BaseAdapter):
    provider = "minimax"
    provider_route = ROUTE
    adapter_revision = ADAPTER_REVISION
    base_url = "https://api.minimax.io/v2"

    @staticmethod
    def _headers():
        key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not key:
            raise ProviderAdapterError("Configure the pay-as-you-go MINIMAX_API_KEY.", code="PROVIDER_NOT_CONFIGURED")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _request(self, method, path, *, body=None):
        try:
            return await _json_request(method, self.base_url + path, headers=self._headers(),
                                       body=body, submission=method == "POST")
        except ProviderAdapterError as exc:
            # MiniMax also returns 529; any server error after POST is ambiguous.
            if exc.status_code and 500 <= exc.status_code < 600:
                raise ProviderAdapterError("MiniMax temporarily failed to respond.",
                    code="SUBMISSION_OUTCOME_UNKNOWN" if method == "POST" else "STATUS_RETRIEVAL_TRANSIENT",
                    outcome_unknown=method == "POST", retryable=method != "POST",
                    status_code=exc.status_code, usage=exc.usage) from exc
            raise

    async def submit(self, request, *, job_id, callback_url, upload_bytes=None):
        settings = validate(request)
        self._headers()
        content = [{"type": "text", "text": request.prompt}]
        totals = {"video": 0, "audio": 0}
        for media in request.media:
            if media.upload_field:
                upload = (upload_bytes or {}).get(media.upload_field)
                if upload is None:
                    raise _invalid("A declared H3 multipart file is missing.")
            else:
                upload = await _download_media(media.url)
            filename, data, _mime = upload
            # ffprobe is a blocking subprocess; keep durable polling responsive.
            mime, duration = await asyncio.to_thread(inspect_media, media.kind, data, filename)
            if media.kind in totals:
                totals[media.kind] += duration
                if totals[media.kind] > 15:
                    raise _invalid(f"H3 total reference {media.kind} duration must not exceed 15 seconds.")
            if media.upload_field:
                if mime == "video/quicktime":
                    raise _invalid("Use an HTTPS URL for MOV references; H3 documents inline MP4 only.")
                url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            else:
                url = media.url
            field = media.kind + "_url"
            content.append({"type": field, field: {"url": url},
                            "role": "reference_image" if media.role == "reference" else media.role})
        body = {"model": MODELS[request.model], "content": content,
                "resolution": settings["resolution"].upper(), "duration": settings["duration"],
                "ratio": settings["aspectRatio"]}
        if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > 64 * 1_000_000:
            raise _invalid("H3 request body exceeds 64 MB after Base64 encoding; use HTTPS media URLs.")
        data = await self._request("POST", "/video_generation", body=body)
        if not isinstance(data.get("task_id"), str) or not data["task_id"]:
            raise ProviderAdapterError("MiniMax did not return a task ID; reconcile before resubmitting.",
                code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True, usage=data.get("usage"))
        return ProviderSubmission(provider_request_id=data["task_id"], request_metadata={
            "upstream_model": "MiniMax-H3", "resolution": settings["resolution"],
            "duration_seconds": settings["duration"], "audio_mode": "provider_managed",
            "has_input_video": any(m.kind == "video" for m in request.media)})

    async def retrieve(self, job):
        from urllib.parse import quote
        data = await self._request("GET", "/query/video_generation/" + quote(job["provider_request_id"], safe=""))
        task = data.get("task")
        if not isinstance(task, dict):
            raise ProviderAdapterError("MiniMax returned no task object.", code="PROVIDER_STATUS_ERROR", usage=data.get("usage"))
        usage = task.get("usage") if isinstance(task.get("usage"), dict) else None
        states = {"queued": "queued", "running": "in_progress", "succeeded": "completed",
                  "failed": "failed", "cancelled": "cancelled"}
        status = states.get(task.get("status")) if isinstance(task.get("status"), str) else None
        if status is None:
            raise ProviderAdapterError("MiniMax returned an unknown task state.", code="PROVIDER_STATUS_ERROR", usage=usage)
        # Missing served resolution cannot be replaced by the requested tier for billing.
        metadata = {"audio_mode": "provider_managed",
                    "resolution": str(task["resolution"]).lower() if task.get("resolution") is not None else None}
        if task.get("duration") is not None:
            metadata["actual_duration_seconds"] = task["duration"]
        if isinstance(task.get("ratio"), str):
            metadata["aspect_ratio"] = task["ratio"]
        error = task.get("error") if isinstance(task.get("error"), dict) else {}
        result = ProviderStatus(status=status, provider_status=task["status"], usage=usage,
            served_model=str(task["model"]) if task.get("model") is not None else None, result_metadata=metadata,
            error_code=str(error.get("code") or "PROVIDER_GENERATION_FAILED") if status == "failed" else None,
            error_message=str(error.get("message") or "MiniMax generation failed.")[:4000] if status == "failed" else None)
        if status == "completed":
            output = task.get("content") if isinstance(task.get("content"), dict) else {}
            url = output.get("url")
            try:
                valid_url = isinstance(url, str) and urlsplit(url).scheme == "https" and bool(urlsplit(url).hostname)
            except ValueError:
                valid_url = False
            if not valid_url:
                result.status = "failed"
                result.error_code = "PROVIDER_RESULT_INVALID"
                result.error_message = "MiniMax completed generation without a usable video URL. Usage was retained."
            else:
                result.result_url = url
                result.result_metadata["outputs"] = [{"url": url, "mime_type": "video/mp4", "role": "video"}]
        return result
