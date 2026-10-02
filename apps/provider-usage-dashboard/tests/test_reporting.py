from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.demo import demo_store
from dashboard.model import Batch, decimal, months_before, row, safe_error, total
from dashboard.report import build_report, compare
from dashboard.store import FileStore, MemoryStore

DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def report(store, start=DAY, end=DAY, providers=None):
    return build_report(store.read(start - timedelta(days=31), NOW.date()), start, end, providers, NOW)


def batch(provider="xai", cost="20", **kwargs):
    result = Batch(provider, "costs", DAY, DAY, **kwargs)
    result.rows = [row(DAY, model="example-model-v1", cost=cost, metrics={"requests": "10"})]
    result.covered = {str(DAY): ["cost", "requests"]}
    return result


def test_funding_is_not_spending_and_refresh_is_idempotent():
    store = MemoryStore()
    b = batch()
    b.payments = [{"id": "payment-1", "day": str(DAY), "kind": "topup", "amount": "100", "currency": "USD"}]
    b.balance = {"amount": "80", "unit": "USD"}
    store.save(b); store.save(b)
    result = report(store)
    assert result["summary"]["cost"] == {"USD": "20"}
    assert result["payments_summary"] == {"USD": "100"}
    assert len(result["payments"]) == 1
    assert next(p for p in result["providers"] if p["id"] == "xai")["balance"]["amount"] == "80"


def test_missing_is_not_zero_and_known_subtotals_survive():
    store = MemoryStore(); b = batch()
    b.rows.append(row(DAY, model="unknown", pending=1))
    store.save(b)
    result = report(store)
    assert result["summary"]["cost"]["USD"] == "20"
    assert result["summary"]["pending"] == 1
    assert next(p for p in result["providers"] if p["id"] == "openai")["cost"] == {}
    store.save(batch(cost="0"))
    assert report(store)["summary"]["cost"]["USD"] == "0"


def test_currencies_and_adjustments_are_not_combined():
    store = MemoryStore(); b = batch()
    b.rows = [row(DAY, cost="1.1", currency="USD", adjustments={"credit": "-0.1"}),
              row(DAY, cost="2.2", currency="EUR", adjustments={"credit": "-0.2"})]
    store.save(b)
    result = report(store)
    assert result["summary"]["cost"] == {"EUR": "2.2", "USD": "1.1"}
    assert result["summary"]["adjustments"]["credit"] == {"EUR": "-0.2", "USD": "-0.1"}


def test_partial_refresh_retains_old_data_and_status():
    store = MemoryStore(); store.save(batch())
    store.failure("provider:xai:costs", "HTTP 503")
    result = report(store)
    assert result["summary"]["cost"] == {"USD": "20"}
    state = next(p for p in result["providers"] if p["id"] == "xai")["status"]
    assert state["state"] == "partial"
    assert state["last_success"]


def test_historical_selection_does_not_backdate_current_balance():
    store = MemoryStore(); b = batch(); b.balance = {"amount": "80", "unit": "USD"}; store.save(b)
    before = DAY - timedelta(days=10)
    result = report(store, before, before)
    assert result["summary"]["cost"] == {}
    assert next(p for p in result["providers"] if p["id"] == "xai")["balance"]["as_of"] > str(NOW.date())


def test_provider_filter_applies_to_totals_models_payments_and_comparisons():
    store = MemoryStore(); store.save(batch()); store.save(batch("openai", "5"))
    result = report(store, providers=["openai"])
    assert result["summary"]["cost"]["USD"] == "5"
    assert {m["provider"] for m in result["models"]} == {"openai"}
    assert [c["provider"] for c in result["comparisons"]] == ["openai"]


def test_source_totals_are_never_added_and_scopes_are_visible():
    store = MemoryStore(); store.save(batch(scope="Provider account"))
    for source in ("gateway", "litellm", "magiclens"):
        store.save(batch(cost="10", source=source, scope="Gateway activity" if source != "magiclens" else "Magic Lens activity"))
    result = report(store)
    assert result["summary"]["cost"]["USD"] == "20"
    c = next(c for c in result["comparisons"] if c["provider"] == "xai")
    assert c["differences"]["provider_gateway"]["absolute"] == "10"
    assert not c["differences"]["provider_gateway"]["comparable"]
    assert c["differences"]["gateway_litellm"]["comparable"]


def test_unknown_models_and_unassigned_charges_remain_visible():
    store = MemoryStore(); b = batch()
    b.rows += [row(DAY, model="never-in-catalog-v2099", cost="3"), row(DAY, service="Storage", cost="2")]
    store.save(b)
    result = report(store)
    assert {m["model"] for m in result["models"]} == {"example-model-v1", "never-in-catalog-v2099", "Unassigned"}
    assert result["summary"]["cost"]["USD"] == "25"


def test_daily_replacement_removes_obsolete_models():
    store = MemoryStore(); b = batch(); store.save(b)
    b.rows = [row(DAY, model="replacement", cost="4")]; store.save(b)
    assert [m["model"] for m in report(store)["models"]] == ["replacement"]
    assert report(store)["summary"]["cost"]["USD"] == "4"


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_collection_lease_prevents_overlap_and_preserves_owner(kind, tmp_path):
    store = MemoryStore() if kind == "memory" else FileStore(tmp_path / "cache.json")
    assert store.claim("one")
    assert not store.claim("two")
    store.finish("two")
    assert not store.claim("two")
    store.finish("one")
    assert store.claim("two")


def test_local_cache_shared_between_process_instances(tmp_path):
    first = FileStore(tmp_path / "report.json"); second = FileStore(tmp_path / "report.json")
    first.save(batch()); second.failure("provider:xai:costs", "Unavailable")
    assert report(first)["summary"]["cost"]["USD"] == "20"
    assert first.status()["sources"]["provider:xai:costs"]["state"] == "error"


def test_decimal_arithmetic_and_invalid_numbers():
    assert total(["0.1", "0.2"]) == "0.3"
    assert decimal("NaN") is None and decimal("Infinity") is None
    assert total([None, None]) is None
    assert "secret" not in safe_error(RuntimeError("password=secret"))
    assert compare("5", "0", True, "same")["percent"] is None


def test_retention_handles_calendar_boundaries():
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    store = MemoryStore(); store.save(batch()); store.prune(DAY + timedelta(days=1))
    assert report(store)["models"] == []


def test_alert_threshold_requires_history_and_real_increase():
    store = MemoryStore(); start = NOW.date() - timedelta(days=8); end = NOW.date() - timedelta(days=1)
    b = Batch("openai", "costs", start, end)
    for i in range(8):
        day = start + timedelta(days=i)
        b.rows.append(row(day, cost="40" if i == 7 else "10"))
        b.covered[str(day)] = ["cost"]
    store.save(b)
    assert any(w["kind"] == "increase" for w in report(store, start, end)["warnings"])
    b.rows = b.rows[-1:]; b.covered = {str(end): ["cost"]}; store = MemoryStore(); store.save(b)
    assert not any(w["kind"] == "increase" for w in report(store, start, end)["warnings"])


def test_app_demo_and_validation_are_cached_only(monkeypatch):
    monkeypatch.setenv("OPENAI_USAGE_API_KEY", "should-never-appear")
    with TestClient(create_app(demo_store(), mode="demo")) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        html = client.get("/")
        assert "sample data" in html.text and "should-never-appear" not in html.text
        response = client.get("/api/report")
        assert response.status_code == 200 and len(response.json()["providers"]) == 6
        assert "should-never-appear" not in response.text
        assert client.get("/api/report?providers=unknown").status_code == 400
        assert client.get("/api/report?start=2000-01-01&end=2000-01-02").status_code == 400
        assert client.post("/api/refresh").status_code == 403
        assert client.post("/api/refresh", headers={"x-dashboard-request": "1", "origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/refresh", headers={"x-dashboard-request": "1"}).json()["state"] == "demo"
        assert client.get("/static/dashboard.js").status_code == 200


def test_cloud_rejects_missing_and_forged_iap_assertions(monkeypatch):
    monkeypatch.setenv("IAP_AUDIENCE", "expected")
    with TestClient(create_app(MemoryStore(), mode="cloud")) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/report", headers={"x-goog-iap-authenticated-user-email": "accounts.google.com:fake@example.com"}).status_code == 401
        assert client.get("/healthz").status_code == 200


def test_cloud_accepts_verified_iap_and_refresh_stays_asynchronous(monkeypatch):
    from google.oauth2 import id_token
    monkeypatch.setenv("IAP_AUDIENCE", "expected")
    def verify(token, transport, audience, certs_url):
        assert token == "signed" and audience == "expected"
        return {"iss": "https://cloud.google.com/iap"}
    monkeypatch.setattr(id_token, "verify_token", verify)
    async def refresh():
        return {"state": "queued"}
    with TestClient(create_app(MemoryStore(), mode="cloud", refresh=refresh)) as client:
        assert client.get("/api/report", headers={"x-goog-iap-jwt-assertion": "signed"}).status_code == 200
        assert client.post("/api/refresh", headers={"x-goog-iap-jwt-assertion": "signed", "x-dashboard-request": "1"}).status_code == 202
