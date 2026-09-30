"""Exact-model successor video contracts; legacy contracts remain in their modules."""
REVISION = "video-contract-v2-2026-09-24"
SEEDANCE = {
    "seedance-2.0": "dreamina-seedance-2-0-260128",
    "seedance-2.0-fast": "dreamina-seedance-2-0-fast-260128",
    "seedance-2.5": "dreamina-seedance-2-5-260628",
}
VEO = {
    "veo-3.1": "veo-3.1-generate-001",
    "veo-3.1-fast": "veo-3.1-fast-generate-001",
    "veo-3.1-lite": "veo-3.1-lite-generate-001",
}
OMNI = {name: "gemini-omni-1.1-flash-preview" for name in (
    "gemini-omni-flash", "gemini-omni-flash-preview",
    "gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview",
)}
GROK = {"grok-video-1.5": "grok-imagine-video-1.5",
        "grok-imagine-video-1.5-2026-05-30": "grok-imagine-video-1.5-2026-05-30"}
MODELS = {**SEEDANCE, **VEO, **OMNI, **GROK}


def settings_for(model, profile):
    """Public settings describe the exact submitted contract, including omissions."""
    base = {"outputCount": {"values": [1], "default": 1}}
    if model in SEEDANCE:
        resolutions = ["480p", "720p"]
        if model != "seedance-2.0-fast":
            resolutions += ["1080p"]
        if model == "seedance-2.0":
            resolutions += ["4k"]
        base.update(duration={"min": 4, "max": 30 if model == "seedance-2.5" else 15, "automatic": -1,
                              "default": -1 if model == "seedance-2.5" else 5},
                    resolution={"values": resolutions, "default": "720p" if model == "seedance-2.5" else "480p"},
                    aspectRatio={"values": ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"], "default": "adaptive"},
                    generateAudio={"type": "boolean", "default": True}, seed={"type": "integer"},
                    return_last_frame={"type": "boolean"}, service_tier={"values": ["default"]},
                    execution_expires_after={"type": "integer"}, priority={"type": "integer"})
        if model == "seedance-2.5":
            base.update(output_format={"values": ["mp4", "mov"], "default": "mp4"})
            if profile == "generate.draft":
                base["resolution"] = {"values": ["480p"], "default": "480p"}
            if profile == "generate.from_draft":
                for field in ("duration", "aspectRatio", "generateAudio", "seed"):
                    base.pop(field)
                base["resolution"] = {"values": ["1080p"], "default": "1080p"}
            elif profile.startswith(("edit.", "extend.")) or "frame" in profile:
                base["aspectRatio"] = {"values": ["adaptive"], "default": "adaptive"}
                if profile.startswith("edit."):
                    base["duration"] = {"values": [-1], "default": -1}
    elif model in VEO:
        base["outputCount"] = {"values": [1, 2, 3, 4], "default": 1}
        base.update(duration={"values": [8] if profile == "generate.references" else [4, 6, 8], "default": 8},
                    resolution={"values": ["720p", "1080p"] + (["4k"] if model == "veo-3.1" else []), "default": "720p"},
                    aspectRatio={"values": ["16:9", "9:16"], "default": "16:9"},
                    generateAudio={"type": "boolean", "default": True}, seed={"type": "integer"})
        if profile == "extend.source_video":
            base.pop("duration")  # Provider returns a fixed extension of the source.
    elif model in GROK:
        base.update(duration={"min": 1, "max": 15, "default": 6},
                    resolution={"values": ["480p", "720p"] + (["1080p"] if profile in {"generate.text", "generate.first_frame", "generate.last_frame", "generate.first_last_frames"} else []), "default": "480p"},
                    aspectRatio={"values": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"]},
                    generateAudio={"type": "boolean", "default": True})
    elif model in OMNI:
        base.update(duration={"min": 3, "max": 10},
                    resolution={"values": ["360p", "720p", "1080p", "4k"], "default": "720p"},
                    aspectRatio={"values": ["16:9", "9:16"], "default": "16:9"})
        if profile.startswith("edit."):
            base.pop("aspectRatio")
    return base


def profiles(model):
    result = ["generate.text", "generate.first_frame", "generate.first_last_frames"]
    if model != "veo-3.1-lite":
        result.append("generate.references")
    if model in SEEDANCE or model in OMNI:
        result += ["generate.multimodal_references", "edit.source_video", "extend.source_video"]
    if model in VEO:
        result += ["extend.source_video"]
    if model in GROK:
        result += ["generate.last_frame", "generate.first_frame_references", "generate.keyframes"]
    if model == "seedance-2.5":
        result += ["generate.draft", "generate.from_draft"]
    if model in OMNI:
        result += ["edit.previous_job", "extend.previous_job"]
    return result


def route(model):
    if model in SEEDANCE:
        return "byteplus_expanded_v2"
    if model in VEO:
        return "vertex_veo_expanded_v2"
    if model in OMNI:
        return "vertex_omni_expanded_v2"
    if model in GROK:
        return "xai_expanded_v2"
    raise ValueError("No successor video contract exists for this exact model.")


def validate(request):
    if request.contract_revision != REVISION or request.model not in MODELS:
        raise ValueError("Unknown video model or contract revision.")
    if request.profile_id not in profiles(request.model) or request.operation != request.profile_id.split('.')[0]:
        raise ValueError("This model does not support the selected video operation.")
    media = request.media
    kinds = {kind: [m for m in media if m.kind == kind] for kind in ("image", "video", "audio")}
    first = [m for m in media if m.role == "first_frame"]
    last = [m for m in media if m.role == "last_frame"]
    if len(first) > 1 or len(last) > 1 or any(m.kind != "image" for m in first + last):
        raise ValueError("First and last frames each require one image.")
    # Order is part of the submitted contract, not sorted or reconstructed here.
    for slot in {m.slot_id for m in media}:
        if [m.index for m in media if m.slot_id == slot] != list(range(sum(m.slot_id == slot for m in media))):
            raise ValueError("Media indices must preserve order within each slot.")
    topology = request.profile_id.split('.', 1)[1]
    if topology == "text" and media:
        raise ValueError("Use a media profile for a request containing media.")
    if topology == "first_frame" and (len(first) != 1 or len(media) != 1):
        raise ValueError("The first-frame profile requires one first frame.")
    if topology == "first_last_frames" and (len(first) != 1 or len(last) != 1 or len(media) != 2):
        raise ValueError("The first/last-frame profile requires both frames.")
    if topology == "source_video" and not kinds["video"]:
        raise ValueError("This operation needs a source video.")
    if topology in {"previous_job", "from_draft"} and not request.previous_job_id:
        raise ValueError("This operation needs an owned previous job.")
    if topology in {"references", "multimodal_references", "first_frame_references", "last_frame", "keyframes"} and not media:
        raise ValueError("This profile needs its reference or frame inputs.")
    if topology == "last_frame" and (len(last) != 1 or len(media) != 1):
        raise ValueError("The last-frame profile requires one last frame.")
    if topology == "references" and any(m.kind != "image" or m.role != "reference" for m in media):
        raise ValueError("This profile accepts image references.")
    if topology == "first_frame_references" and (len(first) != 1 or not any(m.role == "reference" for m in media)):
        raise ValueError("This profile requires a first frame and image references.")
    if topology == "keyframes" and not any(m.role == "keyframe" for m in media):
        raise ValueError("The keyframe profile needs a timestamped image.")
    if topology not in {"previous_job", "from_draft"} and request.previous_job_id:
        raise ValueError("Use a continuation profile for an existing job.")
    settings = request.settings
    canonical = {"aspect_ratio": "aspectRatio", "generate_audio": "generateAudio", "output_count": "outputCount"}
    declared = settings_for(request.model, request.profile_id)
    for key, value in settings.items():
        field = canonical.get(key, key)
        if field not in declared:
            raise ValueError(f"{key} is not supported by this exact model and operation.")
        rule = declared[field]
        if "values" in rule and value not in rule["values"]:
            raise ValueError(f"Unsupported {key} for this exact model and operation.")
        if rule.get("type") == "boolean" and type(value) is not bool:
            raise ValueError(f"{key} must be a boolean.")
        if rule.get("type") == "integer" and type(value) is not int:
            raise ValueError(f"{key} must be an integer.")
        if "min" in rule and value != rule.get("automatic") and (not isinstance(value, (int, float)) or not rule["min"] <= value <= rule["max"]):
            raise ValueError(f"Unsupported {key} for this exact model and operation.")
    if request.model not in VEO and settings.get("outputCount", settings.get("output_count", 1)) != 1:
        raise ValueError("Durable video jobs currently produce one output; submit distinct jobs for multiple outputs.")
    duration = settings.get("duration")
    if duration is not None and type(duration) is not int:
        raise ValueError("Video duration must be an integer or the supported automatic sentinel.")
    if request.model in VEO:
        if kinds["audio"] or request.voice_ids:
            raise ValueError("Veo does not accept reference audio or voice IDs.")
        if request.model == "veo-3.1-lite" and any(m.role == "reference" for m in media):
            raise ValueError("Veo Lite does not support reference images.")
        if len(kinds["image"]) > 3 or len(kinds["video"]) > 1:
            raise ValueError("Veo supports up to three asset references or one source video.")
        if request.operation != "extend" and duration not in (None, 4, 6, 8):
            raise ValueError("Veo generation supports 4, 6 or 8 seconds.")
    elif request.model in SEEDANCE:
        is25 = request.model == "seedance-2.5"
        limits = (30, 10, 10) if is25 else (9, 3, 3)
        if any(len(kinds[k]) > limit for k, limit in zip(("image", "video", "audio"), limits)):
            raise ValueError("Too many references for this exact Seedance model.")
        if (first or last) and len(media) != len(first + last):
            raise ValueError("Seedance frame inputs cannot be combined with omni references.")
        if kinds["audio"] and not is25 and not (kinds["image"] or kinds["video"]):
            raise ValueError("Seedance 2.0 audio references need an image or video reference.")
        if duration is not None and duration != -1 and not 4 <= duration <= (30 if is25 else 15):
            raise ValueError("Unsupported duration for this exact Seedance model.")
        if is25 and request.operation == "edit" and duration not in (None, -1):
            raise ValueError("Seedance 2.5 editing requires automatic duration (-1).")
        if settings.get("draft") and not is25:
            raise ValueError("This Seedance model does not support draft generation.")
        if topology == "from_draft" and (media or request.prompt):
            raise ValueError("Draft completion inherits prompt and media from the owned draft; omit them here.")
        if request.voice_ids:
            raise ValueError("Seedance accepts audio references, not preset voice IDs.")
        if any(m.kind == "video" and m.upload_field for m in media):
            raise ValueError("ModelArk requires an HTTPS video URL; upload the source to your media store first.")
    elif request.model in OMNI:
        if kinds["audio"] or request.voice_ids:
            raise ValueError("This Omni model does not accept standalone audio inputs or preset voices.")
        if len(kinds["image"]) > 10 or len(kinds["video"]) > 3:
            raise ValueError("Omni supports up to ten images and three videos.")
    elif request.model in GROK:
        if kinds["audio"] or kinds["video"]:
            raise ValueError("This Grok generation contract accepts images and preset voices.")
        if duration is not None and not 1 <= duration <= 15:
            raise ValueError("Grok Video 1.5 supports 1 to 15 seconds.")
        if request.voice_ids and settings.get("resolution") == "1080p":
            raise ValueError("Preset voice references support 480p or 720p output.")
        keys = [m for m in media if m.role == "keyframe"]
        if len(keys) > 4 or any(m.timestamp_seconds is None or abs(m.timestamp_seconds * 3 - round(m.timestamp_seconds * 3)) > 1e-6
                              or m.timestamp_seconds >= (duration or 6) for m in keys):
            raise ValueError("Grok accepts at most four keyframes on a 1/3-second grid inside the clip.")
