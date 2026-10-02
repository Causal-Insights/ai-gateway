import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from dashboard import providers
from dashboard.model import ReportingError

D = date(2026, 9, 20)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_all_pages_and_exact_token_line_identity():
    seen = []
    def handler(req):
        seen.append(req)
        assert req.url.path == "/v1/organization/costs"
        assert req.url.params.get_list("group_by") == ["line_item", "project_id"]
        return httpx.Response(200, json={"data": [{"start_time": providers.epoch(D), "results": [
            {"amount": {"value": "0.100000000001", "currency": "usd"}, "line_item": "gpt-new-v2, input_tokens"}]}],
            "has_more": len(seen) == 1, "next_page": "next" if len(seen) == 1 else None})
    async with client(handler) as c:
        b = await providers.openai(c, {"OPENAI_USAGE_API_KEY": "fixture"}, D, D, "costs")
    assert len(b.rows) == 2 and b.rows[0]["cost"] == "0.100000000001"
    assert b.rows[0]["model"] == "gpt-new-v2"
    assert seen[1].url.params["page"] == "next"


@pytest.mark.asyncio
async def test_openai_explicit_empty_cost_bucket_is_zero_and_storage_unassigned():
    async with client(lambda req: httpx.Response(200, json={"data": [{"start_time": providers.epoch(D), "results": []}], "has_more": False})) as c:
        b = await providers.openai(c, {"OPENAI_USAGE_API_KEY": "fixture"}, D, D, "costs")
    assert b.rows[0]["cost"] == "0" and b.covered[str(D)] == ["cost"]


@pytest.mark.asyncio
async def test_openai_incomplete_pagination_does_not_return_partial_success():
    async with client(lambda req: httpx.Response(200, json={"data": [], "has_more": True, "next_page": None})) as c:
        with pytest.raises(ReportingError, match="pagination"):
            await providers.openai(c, {"OPENAI_USAGE_API_KEY": "fixture"}, D, D, "costs")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,endpoint,metric", [("web_searches", "web_search_calls", "web_search_calls"), ("file_searches", "file_search_calls", "file_search_calls")])
async def test_openai_tool_calls_use_official_endpoints(stream, endpoint, metric):
    def handler(req):
        assert req.url.path == f"/v1/organization/usage/{endpoint}"
        result = {"num_requests": 5}
        if stream == "web_searches":
            result.update(model="gpt-test", num_model_requests=3)
        else:
            assert req.url.params.get_list("group_by") == ["project_id"]
        return httpx.Response(200, json={"data": [{"start_time": providers.epoch(D), "results": [result]}], "has_more": False})
    async with client(handler) as c:
        b = await providers.openai(c, {"OPENAI_USAGE_API_KEY": "fixture"}, D, D, stream)
    assert b.rows[0]["metrics"] == {metric: "5"}


@pytest.mark.asyncio
async def test_openai_live_image_billing_labels_preserve_reported_model_names():
    labels = ["gpt-image-2.5-flare image, output", "gpt-image-2.5-sunburst text, cached input", "Vector storage"]
    payload = {"data": [{"start_time": providers.epoch(D), "results": [
        {"amount": {"value": "1.2345", "currency": "usd"}, "line_item": label} for label in labels]}], "has_more": False}
    async with client(lambda req: httpx.Response(200, json=payload)) as c:
        b = await providers.openai(c, {"OPENAI_USAGE_API_KEY": "fixture"}, D, D, "costs")
    assert [r["model"] for r in b.rows] == ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", None]
    assert sum(Decimal(r["cost"]) for r in b.rows) == Decimal("3.7035")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["PURCHASE", "AUTO_PURCHASE"])
async def test_xai_paid_topups_only_and_cents_signs(origin):
    payload = {"total": {"val": "-8000"}, "changes": [
        {"changeOrigin": origin, "topupStatus": status, "amount": {"val": "-10000"}, "invoiceId": status, "createTime": "2026-09-20T15:00:00Z"}
        for status in ["SUCCEEDED", "TO_CHARGE"]] + [
        {"changeOrigin": "SPEND", "amount": {"val": "2000"}, "createTs": "2026-09-20T16:00:00Z"}]}
    async with client(lambda req: httpx.Response(200, json=payload)) as c:
        b = await providers.xai(c, {"XAI_USAGE_API_KEY": "fixture", "XAI_TEAM_ID": "team"}, D, D, "balance")
    assert b.balance["amount"] == "80"
    assert len(b.payments) == 1 and b.payments[0]["amount"] == "100"
    assert b.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("description,model", [("Chat grok-test-v1", "grok-test-v1"), ("API grok-imagine-video-1.5", "grok-imagine-video-1.5"), ("File Storage", None)])
async def test_xai_uses_documented_body_and_usd_not_cents_for_usage(description, model):
    def handler(req):
        body = json.loads(req.content)["analyticsRequest"]
        assert body["timeRange"]["timezone"] == "Etc/GMT"
        assert body["values"] == [{"name": "usd", "aggregation": "AGGREGATION_SUM"}]
        return httpx.Response(200, json={"limitReached": False, "timeSeries": [{"groupLabels": [description], "dataPoints": [{"timestamp": "2026-09-20T00:00:00Z", "values": ["0.75973725"]}]}]})
    async with client(handler) as c:
        b = await providers.xai(c, {"XAI_USAGE_API_KEY": "fixture", "XAI_TEAM_ID": "team"}, D, D, "costs")
    assert b.rows[0]["cost"] == "0.75973725" and b.rows[0]["model"] == model


@pytest.mark.asyncio
async def test_xai_team_scope_discovery():
    async with client(lambda req: httpx.Response(200, json={"scope": "SCOPE_TEAM", "scopeId": "team-1"})) as c:
        assert await providers.xai_team(c, {}, "fixture") == "team-1"


@pytest.mark.asyncio
async def test_xai_daily_truncation_is_not_accepted():
    async with client(lambda req: httpx.Response(200, json={"limitReached": True, "timeSeries": []})) as c:
        with pytest.raises(ReportingError, match="truncated"):
            await providers.xai(c, {"XAI_USAGE_API_KEY": "fixture", "XAI_TEAM_ID": "team"}, D, D, "costs")


@pytest.mark.asyncio
async def test_elevenlabs_credit_units_not_priced_and_milliseconds_preserved():
    def handler(req):
        body = json.loads(req.content)
        assert body["start_time"] == providers.epoch(D) * 1000 and body["interval_seconds"] == 86400
        return httpx.Response(200, json={"columns": ["time", "model", "product_type", "credit_usage", "unit_price"],
            "column_units": ["ms", None, None, "credits", "usd"], "rows": [[providers.epoch(D) * 1000, "music_v2_5", "music", 12345, "0.1"]]})
    async with client(handler) as c:
        b = await providers.elevenlabs(c, {"ELEVENLABS_USAGE_API_KEY": "fixture"}, D, D, "usage")
    assert b.rows[0]["cost"] is None
    assert b.rows[0]["metrics"] == {"credit_usage": "12345", "unit_price_USD": "0.1"}
    assert b.rows[0]["day"] == str(D)


@pytest.mark.asyncio
async def test_elevenlabs_datetime_cost_credits_and_exclusive_end_bucket():
    payload = {"columns": ["model", "product_type", "timestamp", "total_usage", "total_minutes", "total_cost", "usage_count"],
        "column_types": ["String", "String", "DateTime", "Int", "Float", "Float", "Int"],
        "column_units": [None, None, None, "credits", "min", "usd", None],
        "rows": [["music_v2_5", "Music", "2026-09-20T00:00:00Z", 90, "0.05", "0.015", 1],
                 ["eleven_multilingual_v2", "TTS", "2026-09-20T00:00:00Z", 0, 0, 0, 0],
                 ["music_v2_5", "Music", "2026-09-21T00:00:00Z", 90, "0.05", "0.015", 1]]}
    async with client(lambda req: httpx.Response(200, json=payload)) as c:
        b = await providers.elevenlabs(c, {"ELEVENLABS_USAGE_API_KEY": "fixture"}, D, D, "usage")
    assert len(b.rows) == 2
    assert b.rows[0]["cost"] == "0.015" and b.rows[0]["currency"] == "USD"
    assert b.rows[0]["metrics"] == {"credits": "90", "total_minutes_min": "0.05", "usage_count": "1"}
    assert b.rows[1]["cost"] == "0"
    assert "cost" in b.covered[str(D)] and len(b.covered) == 1


@pytest.mark.asyncio
async def test_elevenlabs_allowance_and_overage_are_snapshots():
    async with client(lambda req: httpx.Response(200, json={"character_count": 200, "character_limit": 1000, "current_overage": {"amount": "2.50", "currency": "usd"}})) as c:
        b = await providers.elevenlabs(c, {"ELEVENLABS_USAGE_API_KEY": "fixture"}, D, D, "subscription")
    assert b.balance["amount"] == "800" and b.balance["unit"] == "credits"
    assert b.balance["overage"]["amount"] == "2.50" and not b.rows


@pytest.mark.asyncio
async def test_byteplus_usage_fields_versions_and_local_day():
    def handler(req):
        assert req.url.host == "ark.ap-southeast-1.byteplusapi.com"
        assert 'ap-southeast-1/ark/request' in req.headers["authorization"]
        body = json.loads(req.content)
        assert body["StartTime"] == body["EndTime"] == str(D)
        assert body["QueryInterval"] == "Day"
        return httpx.Response(200, json={"Result": {"Fields": [{"Name": n} for n in ["Day", "ModelName", "ModelVersion", "ReqCnt", "InputTokens"]],
            "Data": [[str(D), "seedream", "version-1", "4", "1000"]], "DataCount": 1}})
    async with client(handler) as c:
        b = await providers.byteplus(c, {"BYTEPLUS_BILLING_ACCESS_KEY_ID": "id", "BYTEPLUS_BILLING_SECRET_ACCESS_KEY": "secret"}, D, D, "usage")
    assert b.time_zone == "Asia/Shanghai"
    assert b.rows[0]["model"] == "seedream / version-1"
    assert b.rows[0]["metrics"]["requests"] == "4"


@pytest.mark.asyncio
async def test_byteplus_billing_pagination_paid_amount_not_topup():
    calls = []
    def handler(req):
        payload = json.loads(req.content); calls.append(payload)
        assert req.url.host == "open.byteplusapi.com"
        return httpx.Response(200, json={"Result": {"Total": 2, "List": [{"BillDetailId": str(payload["Offset"]),
            "ExpenseDate": "2026/9/20", "BillCategory": "consume-use", "DiscountBillAmount": "1.25", "PaidAmount": "100",
            "Currency": "USD", "Product": "ark", "Count": "1000", "Unit": "tokens", "CouponAmount": "0.25"}]}})
    async with client(handler) as c:
        b = await providers.byteplus(c, {"BYTEPLUS_BILLING_ACCESS_KEY_ID": "id", "BYTEPLUS_BILLING_SECRET_ACCESS_KEY": "secret"}, D, D, "costs")
    assert [c["Offset"] for c in calls] == [0, 1]
    assert b.payments is None and b.balance is None and len(b.rows) == 2
    assert b.rows[0]["cost"] == "1.25"


@pytest.mark.asyncio
async def test_byteplus_live_categories_and_reused_daily_bill_ids():
    entries = [dict(BillDetailId="shared", ExpenseDate="2026-09-20", BillCategory="Consumption-usage",
                    DiscountBillAmount="0.045000", PayableAmount="0.04", PaidAmount="100", Currency="USD", RoundAmount=0.005,
                    Product="Smart_Drawing_T2I", ConfigName="Dola-Seedream-5.0-Pro", Element="image-output", Unit="Piece", Count="1"),
               dict(BillDetailId="shared", ExpenseDate="2026-09-21", BillCategory="Consumption-usage",
                    DiscountBillAmount="0", Currency="USD", Product="ModelArk_video_generation", ConfigName="Dreamina-Seedance-2.5"),
               dict(BillDetailId="shared", ExpenseDate="2026-09-21", BillCategory="refund-terminate",
                    PayableAmount="-1.2", Currency="USD", Product="other", ConfigName="not-a-model")]
    async with client(lambda req: httpx.Response(200, json={"Result": {"Total": 3, "List": entries}})) as c:
        b = await providers.byteplus(c, {"BYTEPLUS_BILLING_ACCESS_KEY_ID": "id", "BYTEPLUS_BILLING_SECRET_ACCESS_KEY": "secret"}, D, date(2026, 9, 21), "costs")
    assert len(b.rows) == 3 and b.payments is None
    assert b.rows[0]["cost"] == "0.045000" and b.rows[0]["model"] == "Dola-Seedream-5.0-Pro"
    assert b.rows[1]["cost"] == "0" and b.rows[1]["model"] == "Dreamina-Seedance-2.5"
    assert b.rows[2]["cost"] is None and b.rows[2]["model"] is None
    assert b.rows[2]["adjustments"]["refund-terminate"] == "-1.2"


def test_google_billing_no_currency_conversion_or_credit_explosion(monkeypatch):
    from google.cloud import bigquery
    class Job:
        def result(self, timeout):
            return [{"day": D, "project_id": "p", "service": "Vertex AI", "sku": "Model tokens", "model": "explicit-model-id",
                     "currency": "USD", "cost_type": "regular", "unit": "tokens", "quantity": Decimal("200"), "cost": Decimal("2.1"), "credits": Decimal("-0.3")}]
    class Client:
        def __init__(self, **kwargs): pass
        def query(self, sql, job_config):
            assert 'UNNEST(credits)' in sql and 'GROUP BY' in sql and '@start' in sql
            assert job_config.maximum_bytes_billed == 10000000000
            return Job()
        def close(self): pass
    monkeypatch.setattr(bigquery, "Client", Client)
    b = providers.google_billing({"GOOGLE_BILLING_TABLE": "p.d.t"}, D, D)
    assert b.rows[0]["cost"] == "2.1" and b.rows[0]["adjustments"] == {"credits": "-0.3"}
    assert b.rows[0]["model"] == "explicit-model-id"


@pytest.mark.parametrize("during_result", [False, True])
def test_google_export_pending_preserves_unavailable_status_and_closes_client(monkeypatch, during_result):
    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
    closed = []
    class Job:
        def result(self, timeout):
            raise NotFound("internal query details")
    class Client:
        def __init__(self, **kwargs): pass
        def query(self, sql, job_config):
            if not during_result:
                raise NotFound("internal query details")
            return Job()
        def close(self): closed.append(True)
    monkeypatch.setattr(bigquery, "Client", Client)
    with pytest.raises(ReportingError, match="Initial export can take up to five days") as caught:
        providers.google_billing({"GOOGLE_BILLING_TABLE": "p.d.t"}, D, D)
    assert "internal query details" not in str(caught.value)
    assert closed == [True]


@pytest.mark.asyncio
async def test_http_errors_do_not_echo_secrets():
    async with client(lambda req: httpx.Response(401, json={"error": "secret-value"})) as c:
        with pytest.raises(ReportingError) as caught:
            await providers.request(c, "GET", "https://api.example.test", headers={"Authorization": "Bearer secret-value"})
    assert "secret-value" not in str(caught.value)
