"""Exact MagicLens Grok 1.5 V2 contract; validate before persistence or provider work."""

CONTRACT_REVISION = "video-contract-v2-2026-09-03"
ADAPTER_REVISION = "xai_grok15_v2@2026-09-10"
PROFILES = {
    "generate.text": None,
    "generate.first_frame": ("firstFrame", "first_frame", 1, 1),
    "generate.references": ("referenceImage", "reference", 1, 7),
}
ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}


def validate_grok_video_v2(request):
    if request.model != "grok-video-1.5":
        raise ValueError("This adapter requires the exact Grok Video 1.5 model.")
    if request.contract_revision != CONTRACT_REVISION:
        raise ValueError("Unknown Grok Video 1.5 contract revision.")
    if request.operation != "generate" or request.profile_id not in PROFILES:
        raise ValueError("Unknown Grok Video 1.5 generation profile.")
    if request.previous_job_id:
        raise ValueError("Grok Video 1.5 does not accept a previous job.")
    spec = PROFILES[request.profile_id]
    if not spec and request.media:
        raise ValueError("The text profile does not accept media.")
    if spec:
        slot, role, minimum, maximum = spec
        if not minimum <= len(request.media) <= maximum:
            raise ValueError("Invalid image count for the selected Grok profile.")
        for index, item in enumerate(request.media):
            if (item.slot_id, item.kind, item.role, item.index) != (slot, "image", role, index):
                raise ValueError("Image slot, role, and order must match the selected Grok profile.")
    voices = request.voice_ids
    if len(voices) > 3 or len(set(voices)) != len(voices) or any(v not in {"eve", "leo"} for v in voices):
        raise ValueError("Choose unique preset voices declared by the Grok contract.")
    settings = request.settings
    allowed = {"duration", "resolution", "generateAudio", "outputCount"}
    if request.profile_id != "generate.first_frame":
        allowed.add("aspectRatio")
    if set(settings) - allowed:
        raise ValueError("A setting is not declared by the selected Grok profile.")
    duration = settings.get("duration")
    if type(duration) is not int or not 1 <= duration <= 15:
        raise ValueError("Grok duration must be an integer from 1 to 15 seconds.")
    resolutions = {"480p", "720p"} if request.profile_id == "generate.references" or voices else {"480p", "720p", "1080p"}
    if settings.get("resolution") not in resolutions:
        raise ValueError("Unsupported resolution for the selected Grok inputs.")
    if request.profile_id != "generate.first_frame" and settings.get("aspectRatio") not in ASPECT_RATIOS:
        raise ValueError("Choose a supported Grok aspect ratio.")
    if settings.get("generateAudio") is not True:
        raise ValueError("This Grok contract includes generated audio.")
    if type(settings.get("outputCount", 1)) is not int or settings.get("outputCount", 1) != 1:
        raise ValueError("Grok produces one video per request.")
    if not request.media and not request.prompt.strip():
        raise ValueError("Describe the video before generating.")
