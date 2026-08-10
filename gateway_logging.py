"""Small, opt-in logging adjustments for gateway-owned runtime environments."""

from __future__ import annotations

import logging
import os


_REGISTER_MODEL_CACHE_WARNING = (
    "register_model: model=",
    "not in built-in cost map and no prefix/region variant matched; cache cost fields will default to 0",
)


class _RegisterModelCacheWarningFilter(logging.Filter):
    """Hide one known registration warning without suppressing other LiteLLM warnings."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not all(fragment in message for fragment in _REGISTER_MODEL_CACHE_WARNING)


def configure_gateway_logging() -> None:
    enabled = os.environ.get("SUPPRESS_LITELLM_REGISTER_MODEL_CACHE_WARNINGS", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    logger = logging.getLogger("LiteLLM")
    if not any(isinstance(item, _RegisterModelCacheWarningFilter) for item in logger.filters):
        logger.addFilter(_RegisterModelCacheWarningFilter())
