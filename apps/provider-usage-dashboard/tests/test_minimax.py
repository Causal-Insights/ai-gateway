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


@pytest.mark.asyncio
async def test_estimates_follow_each_resolution_and_stay_separate():
    from dashboard.model import Batch, row
    from dashboard.report import build_report
    rows = [task('2k', resolution='2K', usage={'input_seconds': 0, 'output_seconds': 6, 'total_tokens': 312468}),
            task('reference', resolution='768P', usage={'input_seconds': 7, 'output_seconds': 6, 'total_tokens': 423137}),
            task('768', resolution='768P', usage={'input_seconds': 0, 'output_seconds': 6, 'total_tokens': 195294})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'items': rows, 'total': 3}))) as c:
        batch = await providers.minimax(c, ENV, START, END, 'usage')
    assert batch.rows[0]['estimated_cost'] == '2.30'
    assert batch.rows[0]['cost'] is None and batch.rows[0]['estimate_partial']
    assert batch.rows[0]['unpriced_tasks'] == 0
    assert '2026-10-02' in batch.rows[0]['estimate_basis']
    assert 'estimated_cost' in batch.covered[str(END)] and 'cost' not in batch.covered[str(END)]
    store = MemoryStore(); store.save(batch); store.save(batch)
    paid = Batch('xai', 'costs', END, END)
    paid.rows = [row(END, cost='10.10')]
    paid.covered = {str(END): ['cost']}
    paid.payments = [{'id': 'funding', 'day': str(END), 'kind': 'topup', 'amount': '100', 'currency': 'USD'}]
    store.save(paid)
    result = build_report(store.read(START, END), END, END, now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    assert result['summary']['cost'] == {'USD': '10.10'}
    assert result['summary']['estimated_cost'] == {'USD': '2.30'}
    assert result['summary']['spending'] == {'USD': '12.40'}
    assert result['today_summary']['spending'] == {'USD': '12.40'}
    assert result['payments_summary'] == {'USD': '100'}
    mini = next(p for p in result['providers'] if p['id'] == 'minimax')
    assert mini['spending'] == {'USD': '2.30'} and mini['estimate_partial']
    assert mini['payments'] == {} and mini['balance'] is None
    assert next(m for m in result['models'] if m['provider'] == 'minimax')['spending'] == {'USD': '2.30'}
    assert result['daily'][0]['providers']['minimax'] is None
    assert result['daily'][0]['spending_providers']['minimax'] == '2.30'
    assert result['daily'][0]['spending_models']['minimax']['MiniMax-H3'] == '2.30'
    comp = next(c for c in result['comparisons'] if c['provider'] == 'minimax')
    assert comp['sources']['provider']['cost'] == {}
    assert comp['differences']['provider_gateway']['absolute'] is None
    store.failure('provider:minimax:usage', 'HTTP 503')
    filtered = build_report(store.read(START, END), END, END, ['minimax'])
    assert filtered['summary']['spending'] == {'USD': '2.30'}
    assert filtered['providers'][0]['status']['state'] == 'partial'


def test_image_allowance_per_task_tokens_not_billed_twice_and_partial_failures():
    from decimal import Decimal
    first = task('one', resolution='768P', usage={'input_seconds': 3, 'output_seconds': 4, 'input_image_count': 7, 'total_tokens': 999999999, 'input_audio_seconds': 60})
    second = task('two', resolution='2K', usage={'input_seconds': 0, 'output_seconds': 6, 'input_image_count': 5})
    assert providers.minimax_estimate(first) == (Decimal('0.64'), False)
    assert providers.minimax_estimate(second) == (Decimal('0.78'), False)
    first['status'] = 'failed'
    assert providers.minimax_estimate(first) == (Decimal('0.64'), False)
    first['usage'] = {'input_seconds': 3}
    assert providers.minimax_estimate(first) == (Decimal('0.24'), True)
    first['usage'] = {}
    assert providers.minimax_estimate(first) == (None, True)
    first['usage'] = {'input_seconds': -1, 'output_seconds': 'NaN'}
    assert providers.minimax_estimate(first) == (None, True)


@pytest.mark.parametrize('fields', [{'resolution': '1080P'}, {'resolution': None}, {'model': 'MiniMax-H3-Max'}, {'task_type': 'regeneration'}, {'task_type': 'h3_context_ir'}])
def test_unknown_dimensions_never_receive_a_guessed_rate(fields):
    item = task('unknown', resolution='768P', usage={'input_seconds': 0, 'output_seconds': 6, 'input_image_count': 0})
    item.update(fields)
    assert providers.minimax_estimate(item) == (None, False)


def test_reported_amounts_override_estimates_without_currency_conversion():
    from dashboard.model import row
    from dashboard.report import summarize
    result = summarize([row(END, cost='1.00', estimated_cost='1.20'), row(END, estimated_cost='2.30'),
                        row(END, currency='EUR', estimated_cost='4.50'), row(END, unpriced_tasks=1)])
    assert result['cost'] == {'USD': '1.00'}
    assert result['estimated_cost'] == {'EUR': '4.50', 'USD': '2.30'}
    assert result['spending'] == {'EUR': '4.50', 'USD': '3.30'}
    assert result['unpriced_tasks'] == 1
