from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

PROVIDERS = {"openai": "OpenAI", "xai": "xAI", "byteplus": "BytePlus / ModelArk",
             "elevenlabs": "ElevenLabs", "google": "Google Cloud"}
SOURCES = ("provider", "gateway", "litellm", "magiclens")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def total(values):
    numbers = [n for v in values if (n := decimal(v)) is not None]
    return str(sum(numbers, Decimal(0))) if numbers else None


def days(start: date, end: date):
    """Calendar dates, both endpoints inclusive."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def months_before(day: date, count: int):
    month = day.year * 12 + day.month - 1 - count
    year, month = divmod(month, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def identifier(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def row(day, *, model=None, service=None, cost=None, currency="USD", metrics=None,
        adjustments=None, scope="account", pending=0, **extra):
    return {"day": str(day), "model": model, "service": service,
            "cost": str(decimal(cost)) if decimal(cost) is not None else None,
            "currency": currency.upper() if currency else None,
            "metrics": {k: str(decimal(v)) for k, v in (metrics or {}).items() if decimal(v) is not None},
            "adjustments": {k: str(decimal(v)) for k, v in (adjustments or {}).items() if decimal(v) is not None},
            "scope": scope, "pending": pending, **extra}


@dataclass
class Batch:
    provider: str
    stream: str
    start: date
    end: date
    source: str = "provider"
    rows: list[dict] = field(default_factory=list)
    covered: dict[str, list[str]] = field(default_factory=dict)
    time_zone: str = "UTC"
    scope: str = "account"
    basis: str = "Provider reported charges"
    note: str = ""
    balance: dict | None = None
    payments: list[dict] | None = None

    @property
    def key(self):
        return f"{self.source}:{self.provider}:{self.stream}"

    def snapshots(self, refreshed_at):
        grouped = {str(d): [] for d in days(self.start, self.end)}
        for item in self.rows:
            if item["day"] in grouped:
                grouped[item["day"]].append(item)
        for day, items in grouped.items():
            # Missing history is not a correction to previously reported data.
            # Explicit empty/zero intervals have coverage and replace normally.
            if not items and not self.covered.get(day):
                continue
            yield {"id": identifier(self.key, day), "key": self.key, "day": day,
                   "provider": self.provider, "source": self.source, "stream": self.stream,
                   "rows": items, "covered": self.covered.get(day, []),
                   "time_zone": self.time_zone, "scope": self.scope, "basis": self.basis,
                   "refreshed_at": refreshed_at, "note": self.note}


class ReportingError(Exception):
    """Only deliberate, secret-free messages may be shown in the dashboard."""


class NotConfigured(ReportingError):
    pass


def safe_error(exc):
    if isinstance(exc, ReportingError):
        return str(exc)
    # HTTP exceptions and DB errors can contain URLs, credentials or response bodies.
    return f"Reporting request failed ({type(exc).__name__}). Previous data retained."
