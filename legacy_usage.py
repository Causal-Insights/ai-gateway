"""Structured deprecation telemetry for legacy blocking video calls."""

import json
import logging
from datetime import datetime, timezone


logger = logging.getLogger("ai_gateway.legacy_video")


def log_legacy_video_usage(*, provider: str, model: str, operation: str) -> None:
    logger.warning(
        json.dumps(
            {
                "event": "legacy_blocking_video_endpoint_used",
                "provider": provider,
                "model": model,
                "operation": operation,
                "deprecated": True,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
    )

