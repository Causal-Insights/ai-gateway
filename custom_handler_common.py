"""Shared helpers for custom LiteLLM handlers."""

from typing import Any


def normalize_error(message: Any) -> str:
    if message is None:
        return "unknown error"
    if isinstance(message, dict):
        msg = message.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        err = message.get("error")
        if isinstance(err, dict):
            inner = err.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return str(message).strip() or "unknown error"
    if isinstance(message, str):
        s = message.strip()
        return s if s else "unknown error"
    s = str(message).strip()
    return s if s else "unknown error"
