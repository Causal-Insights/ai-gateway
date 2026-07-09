"""Compatibility exports for split custom provider handlers.

LiteLLM config references this module path:
  - custom_handler.audio_studio
  - custom_handler.grok_video
  - custom_handler.seedance
  - custom_handler.seedream

Provider-specific implementations live in dedicated modules.
"""

from custom_handler_common import normalize_error
from custom_handler_audio import AudioStudioException, AudioStudioLLM, audio_studio
from custom_handler_seedance import (
    DEFAULT_ARK_BASE,
    DEFAULT_ARK_MODEL,
    SeedanceException,
    SeedanceLLM,
    seedance,
)
from custom_handler_seedream import (
    SeedreamException,
    SeedreamLLM,
    seedream,
)
from custom_handler_xai import (
    GrokImageException,
    GrokImageLLM,
    GrokVideoException,
    GrokVideoLLM,
    grok_image,
    grok_video,
)

__all__ = [
    "AudioStudioException",
    "AudioStudioLLM",
    "audio_studio",
    "DEFAULT_ARK_BASE",
    "DEFAULT_ARK_MODEL",
    "GrokImageException",
    "GrokImageLLM",
    "GrokVideoException",
    "GrokVideoLLM",
    "grok_image",
    "SeedanceException",
    "SeedanceLLM",
    "SeedreamException",
    "SeedreamLLM",
    "grok_video",
    "normalize_error",
    "seedance",
    "seedream",
]
