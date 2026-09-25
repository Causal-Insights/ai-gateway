"""Successor video adapters. Existing stored routes continue using their adapters."""
import base64
import asyncio
import os

from generation_job_adapters import (
    BytePlusAdapter, XAIAdapter, VertexAdapter, VertexVeoDirectAdapter,
    ProviderAdapterError, ProviderSubmission, ProviderStatus, ContentSource,
    _json_request, _download_media, probe_media_bytes,
)
from generation_job_models import v2_to_v1
from video_capabilities import MODELS, settings_for, validate
from decimal import Decimal


def checked(request):
    try:
        validate(request)
    except ValueError as exc:
        raise ProviderAdapterError(str(exc), code="INVALID_VIDEO_CONTRACT") from exc


def media_url(media, uploads):
    if media.url:
        return media.url
    if not uploads or media.upload_field not in uploads:
        raise ProviderAdapterError("The referenced upload is missing.", code="INVALID_MEDIA_INPUT")
    _, raw, mime = uploads[media.upload_field]
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class ExpandedSeedance(BytePlusAdapter):
    async def submit(self, request, *, job_id, callback_url, upload_bytes=None):
        checked(request)
        settings = request.settings
        is25 = request.model == "seedance-2.5"
        content = []
        if request.prompt:
            content.append({"type": "text", "text": request.prompt})
        for item in request.media:
            kind = item.kind + "_url"
            role = item.role if item.role in {"first_frame", "last_frame"} else "reference_" + item.kind
            content.append({"type": kind, kind: {"url": media_url(item, upload_bytes)}, "role": role})
        if request._previous_provider_id:
            content = [{"type": "draft_task", "draft_task": {"id": request._previous_provider_id}}]
        body = {"model": MODELS[request.model], "content": content}
        mapping = {"duration": "duration", "resolution": "resolution", "aspectRatio": "ratio",
                   "aspect_ratio": "ratio", "generateAudio": "generate_audio", "generate_audio": "generate_audio"}
        for key, target in mapping.items():
            if key in settings:
                body[target] = settings[key]
        controls = {"seed", "camera_fixed", "return_last_frame", "service_tier", "execution_expires_after", "priority"}
        if is25:
            controls |= {"draft", "output_format", "omni_reference_task_type"}
        for key in controls:
            if key in settings:
                body[key] = settings[key]
        if request.profile_id == "generate.draft":
            body["draft"] = True
            body.setdefault("resolution", "480p")
        if request._previous_provider_id:
            body.setdefault("resolution", "1080p")
        else:
            body.setdefault("duration", -1 if is25 else 5)
            body.setdefault("ratio", "adaptive")
            body.setdefault("resolution", "720p" if is25 else "480p")
        if is25 and request.operation in {"edit", "extend"}:
            body["omni_reference_task_type"] = request.operation
            if request.operation == "edit":
                body["duration"] = -1
        if callback_url:
            body["callback_url"] = callback_url
        data = await _json_request("POST", f"{self._base(request.model)}/contents/generations/tasks",
                                   headers=self._headers(request.model), body=body, submission=True)
        if not data.get("id"):
            raise ProviderAdapterError("Seedance did not return its task identity.", code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True)
        return ProviderSubmission(provider_request_id=str(data["id"]), provider_status=str(data.get("status") or "queued"),
                                  request_metadata={"upstream_model": body["model"], "duration_seconds": body.get("duration"),
                                                    "resolution": body.get("resolution"), "draft": bool(body.get("draft")),
                                                    "has_input_video": bool(request._previous_metadata.get("has_input_video")) if request._previous_provider_id else any(m.kind == "video" for m in request.media),
                                                    "operation": request.operation, "output_format": body.get("output_format", "mp4")})

    async def retrieve(self, job):
        model = (job.get("request_metadata") or {}).get("upstream_model") or MODELS[job["model"]]
        data = await _json_request("GET", f"{self._base(model)}/contents/generations/tasks/{job['provider_request_id']}", headers=self._headers(model))
        raw = str(data.get("status") or "running").lower()
        usage = data.get("usage") or None
        content = data.get("content") or {}
        if raw == "succeeded" and content.get("video_url"):
            outputs = [{"url": content["video_url"], "mime_type": "video/quicktime" if (data.get("output_format") or (job.get("request_metadata") or {}).get("output_format") or ("mov" if content["video_url"].split("?",1)[0].lower().endswith(".mov") else "mp4")) == "mov" else "video/mp4", "role": "video"}]
            if content.get("last_frame_url"):
                outputs.append({"url": content["last_frame_url"], "mime_type": "image/jpeg", "role": "last_frame"})
            return ProviderStatus(status="completed", provider_status=raw, progress=100, result_url=content["video_url"],
                                  result_mime_type=outputs[0]["mime_type"], usage=usage, served_model=data.get("model"),
                                  result_metadata={"outputs": outputs, "provider_duration_seconds": data.get("duration")})
        if raw in {"failed", "expired", "cancelled"}:
            error = data.get("error") or {}
            return ProviderStatus(status=raw, provider_status=raw, usage=usage, error_code=str(error.get("code") or "BYTEPLUS_FAILED"), error_message=str(error.get("message") or raw))
        if raw == "succeeded":
            return ProviderStatus(status="failed", provider_status=raw, error_code="PROVIDER_MALFORMED_RESULT", error_message="Seedance returned no video.", usage=usage)
        return ProviderStatus(status="queued" if raw == "queued" else "in_progress", provider_status=raw)


class ExpandedGrok(XAIAdapter):
    async def submit(self, request, *, job_id, callback_url, upload_bytes=None):
        checked(request)
        key = os.environ.get("GROK_API_KEY")
        if not key:
            raise ProviderAdapterError("GROK_API_KEY is not configured.", code="PROVIDER_NOT_CONFIGURED")
        body = {"model": MODELS[request.model]}
        if request.prompt:
            body["prompt"] = request.prompt
        for setting, field in (("duration", "duration"), ("resolution", "resolution"),
                               ("aspectRatio", "aspect_ratio"), ("aspect_ratio", "aspect_ratio"),
                               ("generateAudio", "generate_audio"), ("generate_audio", "generate_audio")):
            if setting in request.settings:
                body[field] = request.settings[setting]
        for item in request.media:
            value = self._media_value(item, upload_bytes)
            if item.role == "first_frame":
                body["image"] = await self._upload_file(item, upload_bytes, key)
            elif item.role == "last_frame":
                body["last_frame"] = value
            elif item.role == "keyframe":
                body.setdefault("keyframes", []).append({"image": value, "timestamp_s": item.timestamp_seconds})
            else:
                body.setdefault("reference_images", []).append(value)
        if request.voice_ids:
            body["reference_audios"] = [{"voice_id": v} for v in request.voice_ids]
        data = await _json_request("POST", f"{self.base_url}/videos/generations",
                                   headers={"Authorization": f"Bearer {key}"}, body=body, submission=True)
        identity = data.get("request_id") or data.get("id")
        if not identity:
            raise ProviderAdapterError("Grok did not return its task identity.", code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True)
        return ProviderSubmission(provider_request_id=str(identity), provider_status=str(data.get("status") or "pending"),
                                  request_metadata={"upstream_model": body["model"], "duration_seconds": body.get("duration"),
                                                    "resolution": body.get("resolution"), "operation": "generate"})


class ExpandedVeo(VertexVeoDirectAdapter):
    def _model_url(self, model, method):
        # These exact Veo models are served in us-central1, independently of
        # the global Gemini location configured on this mixed-model Gateway.
        project = self._project()
        return f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/publishers/google/models/{model}:{method}"

    async def submit(self, request, *, job_id, callback_url, upload_bytes=None):
        checked(request)
        # The direct API accepts inline media and returns inline video when no
        # storage URI is configured. A GCS bucket is not an input prerequisite.
        instance = {"prompt": request.prompt}
        for item in request.media:
            if item.role in {"first_frame", "last_frame"}:
                instance["image" if item.role == "first_frame" else "lastFrame"] = await self._inline_image(item, upload_bytes)
            elif item.kind == "video":
                if item.upload_field:
                    if not upload_bytes or item.upload_field not in upload_bytes:
                        raise ProviderAdapterError("Source video upload is missing.", code="INVALID_MEDIA_INPUT")
                    _, content, mime = upload_bytes[item.upload_field]
                else:
                    _, content, mime = await _download_media(item.url)
                instance["video"] = {"bytesBase64Encoded": base64.b64encode(content).decode("ascii"), "mimeType": mime}
            elif item.kind == "image":
                instance.setdefault("referenceImages", []).append({"image": await self._inline_image(item, upload_bytes), "referenceType": "asset"})
        params = {"sampleCount": request.settings.get("outputCount", request.settings.get("output_count", 1))}
        if os.environ.get("VEO_OUTPUT_GCS_PREFIX"):
            params["storageUri"] = self._storage_uri(job_id)
        for setting, field in (("duration", "durationSeconds"), ("resolution", "resolution"),
                               ("aspectRatio", "aspectRatio"), ("aspect_ratio", "aspectRatio"),
                               ("generateAudio", "generateAudio"), ("generate_audio", "generateAudio"), ("seed", "seed")):
            if setting in request.settings:
                params[field] = request.settings[setting]
        data = await _json_request("POST", self._model_url(MODELS[request.model], "predictLongRunning"),
                                   headers=await VertexAdapter._vertex_headers(),
                                   body={"instances": [instance], "parameters": params}, submission=True)
        if not data.get("name"):
            raise ProviderAdapterError("Veo did not return an operation identity.", code="SUBMISSION_OUTCOME_UNKNOWN", outcome_unknown=True)
        return ProviderSubmission(provider_request_id=data["name"], request_metadata={"upstream_model": MODELS[request.model],
                                  "resolution": params.get("resolution"), "generate_audio": params.get("generateAudio"),
                                  "duration_seconds": params.get("durationSeconds"), "operation": request.operation})

    async def _fetch(self, job):
        return await _json_request("POST", self._model_url((job.get("request_metadata") or {}).get("upstream_model") or MODELS[job["model"]], "fetchPredictOperation"),
                                   headers=await VertexAdapter._vertex_headers(), body={"operationName": job["provider_request_id"]})

    @staticmethod
    def _inline(data, index=0):
        body = data.get("response") or {}
        outputs = body.get("videos") or body.get("generatedSamples") or []
        if not outputs:
            return None
        video = outputs[index].get("video") or outputs[index]
        value = video.get("bytesBase64Encoded")
        return base64.b64decode(value, validate=True) if value else None

    async def retrieve(self, job):
        data = await self._fetch(job)
        if data.get("error"):
            return ProviderStatus(status="failed", provider_status="failed", error_code="VEO_FAILED",
                                  error_message=str((data["error"] or {}).get("message") or "Veo generation failed."))
        if not data.get("done"):
            return ProviderStatus(status="in_progress", provider_status="in_progress")
        body = data.get("response") or {}
        samples = body.get("videos") or body.get("generatedSamples") or []
        outputs = []
        billable = Decimal(0)
        metadata = job.get("request_metadata") or {}
        for index, sample in enumerate(samples):
            video = sample.get("video") or sample
            uri = video.get("gcsUri") or video.get("uri")
            raw = self._inline(data, index)
            if raw is None and uri:
                raw = (await super().content({**job, "result_url": uri})).content
            if raw is None:
                raise ProviderAdapterError("Veo output content is temporarily unavailable.", code="CONTENT_RETRIEVAL_FAILED", retryable=True)
            probe = await asyncio.to_thread(probe_media_bytes, raw, ".mp4")
            duration = (probe.get("format") or {}).get("duration")
            # Extension returns source+seven new seconds. Only the generated
            # segment is billable; retain the physical duration independently.
            quantity = "7" if metadata.get("operation") == "extend" else duration
            if quantity is None:
                billable = None
            elif billable is not None:
                billable += Decimal(str(quantity))
            outputs.append({"url": uri or f"gateway:veo-inline:{index}", "mime_type": "video/mp4",
                            "kind": "video", "index": index, "duration_seconds": duration})
        if not outputs:
            return ProviderStatus(status="failed", provider_status="done", error_code="PROVIDER_MALFORMED_RESULT", error_message="Veo returned no video.")
        return ProviderStatus(status="completed", provider_status="succeeded", progress=100,
                              result_url=outputs[0]["url"], usage={"output_video_seconds": str(billable)} if billable is not None else None,
                              served_model=metadata.get("upstream_model"), result_metadata={"outputs": outputs})

    async def content(self, job):
        value = job.get("result_url") or ""
        if not value.startswith("gateway:veo-inline"):
            return await super().content(job)
        index = int(value.rsplit(":", 1)[-1]) if value.count(":") > 1 else 0
        raw = self._inline(await self._fetch(job), index)
        if raw is None:
            raise ProviderAdapterError("Veo inline output is temporarily unavailable.", code="CONTENT_RETRIEVAL_FAILED", retryable=True)
        return ContentSource(content=raw, mime_type="video/mp4")



class ExpandedOmni(VertexAdapter):
    async def submit(self, request, *, job_id, callback_url, upload_bytes=None):
        checked(request)
        return await self._submit_omni(v2_to_v1(request), upload_bytes, expanded=True)


ADAPTERS = {
    "byteplus_expanded_v2": ExpandedSeedance(),
    "xai_expanded_v2": ExpandedGrok(),
    "vertex_veo_expanded_v2": ExpandedVeo(),
    "vertex_omni_expanded_v2": ExpandedOmni(),
}
