"""MiniMax-hosted H3 contract; no Hailuo, Max, or alternative serving routes."""
MODELS = {"minimax-h3": "MiniMax-H3", "MiniMax-H3": "MiniMax-H3"}
ROUTE = "minimax_h3_v2"
ADAPTER_REVISION = "minimax_h3_v2@2026-10-02"
PROFILES = ["generate.text", "generate.first_frame", "generate.last_frame",
            "generate.first_last_frames", "generate.references", "generate.multimodal_references"]
RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]


def settings_for(profile):
    frames = profile in {"generate.first_frame", "generate.last_frame", "generate.first_last_frames"}
    return {
        "duration": {"type": "integer", "min": 4, "max": 15, "default": 5},
        "resolution": {"values": ["768p", "2k"], "default": "768p"},
        "aspectRatio": {"values": ["adaptive"] if frames else RATIOS if profile == "generate.text" else ["adaptive", *RATIOS],
                        "default": "16:9" if profile == "generate.text" else "adaptive"},
        "outputCount": {"values": [1], "default": 1},
    }


def validate(request):
    if request.profile_id not in PROFILES or request.operation != "generate":
        raise ValueError("MiniMax H3 supports the declared generation profiles only.")
    if not request.prompt.strip() or len(request.prompt) > 7000:
        raise ValueError("MiniMax H3 requires a nonempty prompt of at most 7000 characters.")
    if request.previous_job_id or request.voice_ids:
        raise ValueError("MiniMax H3 accepts reference audio, not previous jobs or preset voice IDs.")
    declared = settings_for(request.profile_id)
    aliases = {"aspect_ratio": "aspectRatio", "output_count": "outputCount"}
    values = {}
    for key, value in request.settings.items():
        name = aliases.get(key, key)
        if name in values and values[name] != value:
            raise ValueError(f"Conflicting MiniMax H3 {name} settings.")
        values[name] = value
        if name not in declared:
            raise ValueError(f"{key} is not supported by MiniMax H3; audio is provider-managed.")
        rule = declared[name]
        if name in {"duration", "outputCount"} and type(value) is not int:
            raise ValueError(f"{key} must be an integer.")
        if "values" in rule and value not in rule["values"]:
            raise ValueError(f"Unsupported MiniMax H3 {key}.")
        if name == "duration" and not 4 <= value <= 15:
            raise ValueError("MiniMax H3 duration must be 4–15 seconds.")
    media = request.media
    for slot in {item.slot_id for item in media}:
        indices = [item.index for item in media if item.slot_id == slot]
        if indices != list(range(len(indices))):
            raise ValueError("Media indices must preserve order within each slot.")
    if any(item.timestamp_seconds is not None for item in media):
        raise ValueError("MiniMax H3 does not accept timestamped keyframes.")
    roles = [item.role for item in media]
    topology = request.profile_id.split(".", 1)[1]
    frames = {"first_frame": ["first_frame"], "last_frame": ["last_frame"],
              "first_last_frames": ["first_frame", "last_frame"]}
    if topology == "text":
        if media:
            raise ValueError("The text profile accepts no media.")
    elif topology in frames:
        if sorted(roles) != sorted(frames[topology]) or any(item.kind != "image" for item in media):
            raise ValueError("This frame profile requires exactly its declared image roles.")
    else:
        kinds = {kind: sum(item.kind == kind for item in media) for kind in ("image", "video", "audio")}
        mapping = {"image": "reference", "video": "reference_video", "audio": "reference_audio"}
        if not media or any(item.role != mapping[item.kind] for item in media):
            raise ValueError("H3 reference profiles require reference roles; frame and reference inputs cannot mix.")
        if topology == "references" and any(item.kind != "image" for item in media):
            raise ValueError("Use the multimodal reference profile for video or audio.")
        if len(media) > 12 or kinds["image"] > 9 or kinds["video"] > 3 or kinds["audio"] > 3:
            raise ValueError("H3 accepts at most 9 images, 3 videos, 3 audio clips, and 12 reference files total.")
    return {key: values.get(key, rule["default"]) for key, rule in declared.items()}
