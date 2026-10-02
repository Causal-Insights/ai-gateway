import json
from datetime import date, datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import providers
from dashboard.app import create_app
from dashboard.model import ReportingError
from dashboard.store import MemoryStore

START, END = date(2026, 9, 1), date(2026, 10, 2)
ENV = {"MINIMAX_USAGE_API_KEY": "reporting-only"}


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(providers, "datetime", Clock)


def task(identity, day="2026-10-02", **fields):
    return {"id": identity, "created_at": int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()),
            "model": "MiniMax-H3", "status": "succeeded", "task_type": "generation", **fields}


@pytest.mark.asyncio
async def test_minimax_pagination_aggregates_usage_without_costs_or_content():
    seen = []
    def handler(req):
        assert req.method == "GET" and req.url.path == "/v2/query/video_generation"
        assert req.headers["authorization"] == "Bearer reporting-only"
        page = int(req.url.params["page_num"]); seen.append(page)
        rows = [task("one", usage={"output_seconds": 6, "input_seconds": 0}, content={"url": "private-media"})] if page == 1 else [
            task("two", usage={"output_seconds": 6, "input_seconds": 7, "input_image_count": 1}),
            task("three", status="failed", usage={"input_seconds": 2}),
            task("four", task_type="h3_context_ir", usage={"prompt_tokens": 20, "completion_tokens": 10}, content={"prompt": "private-prompt"})]
        return httpx.Response(200, json={"items": rows, "total": 4})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        batch = await providers.minimax(c, ENV, START, END, "usage")
    assert seen == [1, 2]
    assert len(batch.rows) == 2
    generation = batch.rows[0]
    assert generation["metrics"] == {"tasks": "3", "generations": "2", "output_video_seconds": "12", "input_video_seconds": "9", "input_images": "1"}
    assert batch.rows[1]["metrics"] == {"tasks": "1", "input_tokens": "20", "output_tokens": "10"}
    assert all(r["cost"] is None and "requests" not in r["metrics"] for r in batch.rows)
    assert batch.balance is None and batch.payments is None
    assert batch.covered == {"2026-10-02": ["usage", "generations"]}
    store = MemoryStore(); store.save(batch); store.save(batch)
    persisted = json.dumps(store.data)
    assert "private-media" not in persisted and "private-prompt" not in persisted
    api = TestClient(create_app(store))
    report = api.get('/api/report?start=2026-09-01&end=2026-10-02&providers=minimax').json()
    assert report['providers'][0]['metrics']['generations'] == '2'
    assert report['providers'][0]['cost'] == {} and report['providers'][0]['notes']
    assert report['models'][0]['model'] == 'MiniMax-H3'
    assert 'MiniMax' in api.get('/').text


@pytest.mark.asyncio
async def test_minimax_expired_boundary_does_not_replace_cached_full_day():
    def handler(req):
        return httpx.Response(200, json={"items": [task("boundary", "2026-09-25"), task("complete", "2026-09-26"), task("today")], "total": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        batch = await providers.minimax(c, ENV, START, date(2026, 9, 30), "usage")
    assert [r['day'] for r in batch.rows] == ['2026-09-26']
    assert [s['day'] for s in batch.snapshots('now')] == ['2026-09-26']
    def no_request(req):
        pytest.fail('Expired ranges must not call the rolling-window API')
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as c:
        expired = await providers.minimax(c, ENV, START, date(2026, 9, 24), "usage")
    assert list(expired.snapshots('now')) == []


@pytest.mark.asyncio
async def test_minimax_incomplete_pages_and_auth_errors_preserve_unknowns():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"items": [task('same')], "total": 2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ReportingError, match='incomplete task pagination'):
            await providers.minimax(c, ENV, START, END, 'usage')
    assert len(calls) == 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={'error': 'private-secret'}))) as c:
        with pytest.raises(ReportingError, match='HTTP 401') as caught:
            await providers.minimax(c, ENV, START, END, 'usage')
    assert 'private-secret' not in str(caught.value)
