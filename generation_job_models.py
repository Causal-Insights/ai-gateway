"""Provider-neutral request and response models for durable generation jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


def safe_client_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Persist operational correlation only; never arbitrary prompt-like client data."""
    allowed = {"source", "run_id", "step_id", "attempt", "workflow_run_id"}
    return {key: value for key, value in metadata.items() if key in allowed}


class MediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image", "video"]
    role: Literal["first_frame", "last_frame", "reference", "source"] = "reference"
    url: Optional[str] = None
    upload_field: Optional[str] = None

    @model_validator(mode="after")
    def validate_source(self) -> "MediaInput":
        if bool(self.url) == bool(self.upload_field):
            raise ValueError("media input requires exactly one of url or upload_field")
        if self.url and not self.url.startswith("https://"):
            raise ValueError("media input url must use https://; use multipart for inline media")
        return self


class GenerationJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    modality: Literal["video"] = "video"
    operation: Literal["auto", "generate", "edit", "extend"] = "auto"
    previous_job_id: Optional[str] = Field(default=None, min_length=5, max_length=200)
    reference_voice_ids: list[str] = Field(default_factory=list, max_length=3)
    prompt: str = Field(default="", max_length=100_000)
    duration_seconds: Optional[int] = Field(default=None, ge=1, le=60)
    resolution: Optional[str] = Field(default=None, max_length=32)
    aspect_ratio: Optional[str] = Field(default=None, max_length=32)
    generate_audio: bool = False
    # Provider adapters enforce their model-specific limits. Seedance 2.5 can
    # accept up to 30 image references; existing adapters retain lower caps.
    media_inputs: list[MediaInput] = Field(default_factory=list, max_length=30)
    metadata: dict[str, str] = Field(default_factory=dict)
    _previous_interaction_id: Optional[str] = PrivateAttr(default=None)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("reference_voice_ids")
    @classmethod
    def validate_reference_voice_ids(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip().lower() for item in value]
        if any(not item or len(item) > 100 for item in normalized):
            raise ValueError("reference voice IDs must contain 1 to 100 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("reference voice IDs must be unique")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("metadata supports at most 20 entries")
        for key, item in value.items():
            if len(key) > 100 or len(str(item)) > 500:
                raise ValueError("metadata keys and values are too long")
        return {str(key): str(item) for key, item in value.items()}


class MediaInputV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=100)
    index: int = Field(default=0, ge=0, le=30)
    kind: Literal["image", "video", "audio"]
    role: Literal["first_frame", "last_frame", "reference", "source", "reference_video", "reference_audio"] = "reference"
    url: Optional[str] = None
    upload_field: Optional[str] = None

    @model_validator(mode="after")
    def validate_source(self) -> "MediaInputV2":
        if bool(self.url) == bool(self.upload_field):
            raise ValueError("media input requires exactly one of url or upload_field")
        if self.url and not self.url.startswith("https://"):
            raise ValueError("media input url must use https://; use multipart for inline media")
        return self


class GenerationJobCreateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_schema_version: Literal[2]
    model: str = Field(min_length=1, max_length=200)
    contract_revision: str = Field(min_length=1, max_length=200)
    profile_id: str = Field(min_length=1, max_length=200)
    operation: Literal["generate", "edit", "extend"]
    prompt: str = Field(default="", max_length=100_000)
    settings: dict[str, Any] = Field(default_factory=dict)
    media: list[MediaInputV2] = Field(default_factory=list, max_length=30)
    voice_ids: list[str] = Field(default_factory=list, max_length=3)
    previous_job_id: Optional[str] = Field(default=None, min_length=5, max_length=200)
    metadata: dict[str, str] = Field(default_factory=dict)
    _previous_interaction_id: Optional[str] = PrivateAttr(default=None)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        normalized = value.strip()
        if "." not in normalized:
            raise ValueError("profile_id must be operation.topology")
        return normalized

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "resolution", "duration", "aspectRatio", "aspect_ratio", "generateAudio",
            "generate_audio", "renderQuality", "render_quality", "frameRate", "frame_rate",
            "outputCount", "output_count"
        }
        extra = [key for key in value if key not in allowed]
        if extra:
            raise ValueError(f"unsupported V2 settings: {', '.join(extra)}")
        if len(value) > 20:
            raise ValueError("settings supports at most 20 entries")
        return value

    @field_validator("voice_ids")
    @classmethod
    def validate_voice_ids(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip().lower() for item in value]
        if any(not item or len(item) > 100 for item in normalized):
            raise ValueError("voice IDs must contain 1 to 100 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("voice IDs must be unique")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("metadata supports at most 20 entries")
        for key, item in value.items():
            if len(key) > 100 or len(str(item)) > 500:
                raise ValueError("metadata keys and values are too long")
        return {str(key): str(item) for key, item in value.items()}


def v2_to_v1(payload: GenerationJobCreateV2) -> GenerationJobCreate:
    """Translate a V2 body onto the frozen V1 model for adapters that still speak V1."""
    media = []
    for item in payload.media:
        kind = item.kind if item.kind in {"image", "video"} else "video"
        role = item.role if item.role in {"first_frame", "last_frame", "reference", "source"} else "reference"
        media.append(MediaInput(type=kind, role=role, url=item.url, upload_field=item.upload_field))
    settings = payload.settings or {}
    duration = settings.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) and duration else None
    generate_audio = settings.get("generateAudio", settings.get("generate_audio", False))
    return GenerationJobCreate(
        model=payload.model,
        operation=payload.operation,
        previous_job_id=payload.previous_job_id,
        reference_voice_ids=payload.voice_ids,
        prompt=payload.prompt,
        duration_seconds=duration_seconds,
        resolution=str(settings["resolution"]) if settings.get("resolution") else None,
        aspect_ratio=str(settings.get("aspectRatio") or settings.get("aspect_ratio") or "") or None,
        generate_audio=bool(generate_audio),
        media_inputs=media,
        metadata=payload.metadata,
    )


class JobError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class JobResult(BaseModel):
    content_url: str
    mime_type: str = "video/mp4"


class GenerationJobResponse(BaseModel):
    id: str
    object: Literal["generation.job"] = "generation.job"
    modality: Literal["video"]
    model: str
    status: Literal["queued", "in_progress", "completed", "failed", "expired", "cancelled"]
    progress: Optional[float] = None
    provider_request_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    poll_after_ms: Optional[int] = None
    result: Optional[JobResult] = None
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    error: Optional[JobError] = None


class ProviderStatus(BaseModel):
    status: Literal["queued", "in_progress", "completed", "failed", "expired", "cancelled"]
    provider_status: str
    progress: Optional[float] = None
    result_url: Optional[str] = None
    result_mime_type: str = "video/mp4"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_retryable: bool = False
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None


class ProviderSubmission(BaseModel):
    provider_request_id: str
    provider_status: str = "queued"
    progress: Optional[float] = None
    request_metadata: dict[str, Any] = Field(default_factory=dict)
