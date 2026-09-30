"""Registered Seedance 2.0 V2 profiles and their provider translation."""

MODELS = {
    "seedance-2.0": "dreamina-seedance-2-0-260128",
    "seedance-2.0-fast": "dreamina-seedance-2-0-fast-260128",
}
CONTRACT_REVISION = "video-contract-v2-2026-09-03"
ADAPTER_REVISION = "byteplus_seedance20_v2@2026-09-10"
RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
PROFILES = {
    "generate.text": [],
    "generate.first_frame": [("firstFrame", "image", "first_frame", 1, 1)],
    "generate.references": [("referenceImage", "image", "reference", 1, 9)],
    "edit.source_video": [("sourceVideo", "video", "source", 1, 1), ("referenceImage", "image", "reference", 0, 9)],
}


def validate_seedance_video_v2(request):
    if request.model not in MODELS or request.contract_revision != CONTRACT_REVISION:
        raise ValueError("Unknown Seedance 2.0 model or contract revision.")
    if request.profile_id not in PROFILES or request.operation != request.profile_id.split(".")[0]:
        raise ValueError("Unknown Seedance 2.0 profile or operation.")
    if request.previous_job_id or request.voice_ids:
        raise ValueError("Seedance 2.0 does not accept prior jobs or preset voices.")
    if not request.prompt.strip():
        raise ValueError("Describe the video before generating.")
    remaining = list(request.media)
    for slot, kind, role, minimum, maximum in PROFILES[request.profile_id]:
        items = [item for item in request.media if item.slot_id == slot]
        if not minimum <= len(items) <= maximum:
            raise ValueError("Invalid media count for the selected Seedance profile.")
        for index, item in enumerate(items):
            if (item.kind, item.role, item.index) != (kind, role, index):
                raise ValueError("Media kind, role and order must match the selected Seedance profile.")
            if not item.url or item.upload_field or not item.url.startswith("https://"):
                raise ValueError("Seedance media must have a durable HTTPS URL.")
            remaining.remove(item)
    if remaining:
        raise ValueError("Media must match a declared Seedance slot.")
    settings = request.settings
    editing = request.operation == "edit"
    allowed = {"duration", "generateAudio", "outputCount"}
    if not editing:
        allowed |= {"resolution", "aspectRatio"}
    if set(settings) - allowed:
        raise ValueError("A setting is not declared by the selected Seedance profile.")
    if type(settings.get("duration")) is not int or not 4 <= settings["duration"] <= 15:
        raise ValueError("Seedance duration must be an integer from 4 to 15 seconds.")
    if type(settings.get("generateAudio")) is not bool:
        raise ValueError("Choose whether to generate audio.")
    if type(settings.get("outputCount", 1)) is not int or settings.get("outputCount", 1) != 1:
        raise ValueError("Seedance produces one video per request.")
    if not editing:
        resolutions = {"480p", "720p"}
        if request.model == "seedance-2.0" and request.profile_id != "generate.references":
            resolutions.add("1080p")
        if settings.get("resolution") not in resolutions or settings.get("aspectRatio") not in RATIOS:
            raise ValueError("Unsupported Seedance resolution or aspect ratio.")


# Provider output grids plus standard 16:9 frames, in either orientation. An
# unknown source tier must not silently become a different paid output tier.
SOURCE_GRIDS = {
    "480p": [(864, 496), (854, 480), (864, 480), (852, 480), (752, 560), (640, 640), (992, 432)],
    "720p": [(1280, 720), (1112, 834), (1120, 832), (960, 960), (1470, 630), (1504, 640)],
    "1080p": [(1920, 1080), (1920, 1088), (1664, 1248), (1440, 1440), (2206, 946), (2176, 928)],
}


def source_resolution(model, probe):
    streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise ValueError("The source must contain a readable video stream.")
    video = streams[0]
    try:
        duration = float(video.get("duration") or probe.get("format", {}).get("duration"))
        width, height = sorted((int(video["width"]), int(video["height"])), reverse=True)
    except (ValueError, TypeError, KeyError):
        raise ValueError("The source video duration and dimensions could not be verified.")
    if not 2 <= duration <= 15:
        raise ValueError("Seedance source videos must be between 2 and 15 seconds.")
    for tier, grids in SOURCE_GRIDS.items():
        if (width, height) in grids:
            if tier == "1080p" and model == "seedance-2.0-fast":
                raise ValueError("Seedance 2.0 Fast cannot preserve a 1080p source; use Seedance 2.0 or a 720p source.")
            return tier
    raise ValueError("Use a source video with a supported 480p, 720p or 1080p frame size to preserve its resolution.")


def seedance_provider_body(request, *, job_id, source_tier=None):
    validate_seedance_video_v2(request)
    import hashlib
    content = [{"type": "text", "text": request.prompt}]
    for item in request.media:
        provider_role = "reference_video" if item.kind == "video" else "reference_image" if item.role == "reference" else item.role
        content.append({"type": f"{item.kind}_url", f"{item.kind}_url": {"url": item.url}, "role": provider_role})
    settings = request.settings
    if request.operation == "edit" and source_tier not in SOURCE_GRIDS:
        raise ValueError("Verify the source resolution before editing.")
    return {
        "model": MODELS[request.model], "content": content,
        "resolution": source_tier if request.operation == "edit" else settings["resolution"],
        "ratio": "adaptive" if request.operation == "edit" else settings["aspectRatio"],
        "duration": settings["duration"], "generate_audio": settings["generateAudio"],
        "watermark": False, "safety_identifier": hashlib.sha256(job_id.encode()).hexdigest()[:32],
    }
