"""Compatibility exports for split custom provider handlers.

LiteLLM config references this module path:
  - custom_handler.grok_video
  - custom_handler.seedance

Provider-specific implementations live in dedicated modules.
"""

from custom_handler_common import normalize_error
from custom_handler_seedance import (
    DEFAULT_ARK_BASE,
    DEFAULT_ARK_MODEL,
    SeedanceException,
    SeedanceLLM,
    seedance,
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
    "DEFAULT_ARK_BASE",
    "DEFAULT_ARK_MODEL",
    "GrokImageException",
    "GrokImageLLM",
    "GrokVideoException",
    "GrokVideoLLM",
    "grok_image",
    "SeedanceException",
    "SeedanceLLM",
    "grok_video",
    "normalize_error",
    "seedance",
]
