from datetime import date, datetime, timezone

import pytest

from dashboard import collector, internal
from dashboard.model import Batch, ReportingError, row
from dashboard.report import build_report
from dashboard.store import MemoryStore

DAY = date(2026, 9, 20)


@pytest.mark.asyncio
async def test_provider_failure_does_not_interrupt_other_sources(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(collector, "STREAMS", {"openai": ["costs"], "xai": ["costs"]})
    async def fail(*args):
        raise ReportingError("HTTP 403")
    async def succeed(*args):
        batch = Batch("xai", "costs", DAY, DAY)
        batch.rows = [row(DAY, cost="1.25")]
        batch.covered = {str(DAY): ["cost"]}
        return batch
    async def no_internal(*args):
        return []
    monkeypatch.setattr(collector.providers, "openai", fail)
    monkeypatch.setattr(collector.providers, "xai", succeed)
    monkeypatch.setattr(collector, "collect_internal", no_internal)
    result = await collector.collect_range(store, {}, DAY, DAY, ["openai", "xai"])
    assert [r["ok"] for r in result] == [False, True, True, True, True]
    assert store.status()["sources"]["provider:openai:costs"]["state"] == "error"
    assert store.read(DAY, DAY)["days"][0]["rows"][0]["cost"] == "1.25"


@pytest.mark.asyncio
async def test_explicit_backfill_splits_months_and_releases_lock_on_failure(monkeypatch):
    store = MemoryStore(); windows = []
    async def collect(store, env, start, end, selected):
        windows.append((start, end))
        return []
    monkeypatch.setattr(collector, "collect_range", collect)
    result = await collector.run(store, {}, date(2026, 8, 30), date(2026, 9, 2))
    assert result["state"] == "complete"
    assert windows == [(date(2026, 8, 30), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 2))]
    async def fail(*args):
        raise RuntimeError("cache unavailable")
    monkeypatch.setattr(collector, "collect_range", fail)
    with pytest.raises(RuntimeError):
        await collector.run(store, {}, DAY, DAY)
    assert store.status()["collection"]["state"] == "idle"


@pytest.mark.asyncio
async def test_hourly_window_daily_corrections_and_backfill(monkeypatch):
    store = MemoryStore(); windows = []
    async def collect(store, env, start, end, selected):
        windows.append((start, end)); return []
    monkeypatch.setattr(collector, "collect_range", collect)
    today = datetime.now(timezone.utc).date()
    await collector.run(store, {})
    assert windows[0] == (today.replace(day=1), today)
    windows.clear()
    await collector.run(store, {})
    assert windows[-1][1] < today.replace(day=1)  # another month backfilled
    from datetime import timedelta
    assert windows[0][0] == today - timedelta(days=6)
    store.meta("daily_refresh", "2000-01-01"); windows.clear()
    await collector.run(store, {})
    assert windows[0][0] < today.replace(day=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["gateway", "litellm", "magiclens"])
async def test_internal_connections_are_bounded_readonly_and_closed(monkeypatch, source):
    class Transaction:
        async def __aenter__(self): pass
        async def __aexit__(self, *args): pass
    class Connection:
        closed = False
        def transaction(self, *, readonly):
            assert readonly; return Transaction()
        async def fetch(self, sql, start, end):
            assert sql.lstrip().startswith("SELECT") and "GROUP BY" in sql
            assert start.tzinfo is None if source == "litellm" else start.tzinfo is not None
            return [{"day": DAY, "provider": "openai", "model": "historical-model-v1", "cost": "0.123456789", "requests": 3, "pending": 1}]
        async def close(self, *, timeout):
            assert timeout == 5; self.closed = True
    connection = Connection()
    async def connect(url, **kwargs):
        assert url == "dedicated-readonly"
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        assert kwargs["server_settings"]["statement_timeout"] == "5000"
        assert kwargs["timeout"] == kwargs["command_timeout"] == 5
        return connection
    monkeypatch.setattr(internal.asyncpg, "connect", connect)
    batches = await internal.collect_internal({"GATEWAY_REPORTING_DATABASE_URL": "dedicated-readonly", "MAGICLENS_REPORTING_DATABASE_URL": "dedicated-readonly"}, source, DAY, DAY)
    assert connection.closed
    item = next(b for b in batches if b.provider == "openai").rows[0]
    assert item["cost"] == "0.123456789" and item["pending"] == 1
    assert item["model"] == "historical-model-v1"


def test_unknown_history_does_not_erase_known_data():
    store = MemoryStore(); batch = Batch("openai", "costs", DAY, DAY)
    batch.rows = [row(DAY, cost="5")]; batch.covered = {str(DAY): ["cost"]}; store.save(batch)
    batch.rows = []; batch.covered = {}; store.save(batch)
    assert store.read(DAY, DAY)["days"][0]["rows"][0]["cost"] == "5"
    batch.covered = {str(DAY): ["cost"]}; store.save(batch)
    assert store.read(DAY, DAY)["days"][0]["rows"] == []


def test_today_and_daily_models_preserve_byteplus_reporting_zone():
    store = MemoryStore(); batch = Batch("byteplus", "costs", DAY, DAY, time_zone="Asia/Shanghai")
    batch.rows = [row(DAY, model="model-v1", cost="5")]; batch.covered = {str(DAY): ["cost"]}; store.save(batch)
    now = datetime(2026, 9, 19, 20, tzinfo=timezone.utc)
    result = build_report(store.read(DAY, DAY), DAY, DAY, ["byteplus"], now)
    assert result["today_summary"]["cost"]["USD"] == "5"
    assert result["daily"][0]["models"]["byteplus"]["model-v1"] == "5"
