"""Run only against the disposable PostgreSQL service configured in dashboard CI."""
import os
from datetime import date

import asyncpg
import pytest

from dashboard.internal import collect_internal


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="Disposable PostgreSQL not configured")
async def test_real_sql_preserves_exact_costs_pending_attempts_and_historical_models():
    url = os.environ["TEST_POSTGRES_URL"]
    connection = await asyncpg.connect(url)
    if await connection.fetchval("SELECT current_database()") != "reporting_test":
        await connection.close()
        pytest.fail("TEST_POSTGRES_URL must name the disposable reporting_test database")
    try:
        # This is a fresh disposable database, never a production reporting URL.
        await connection.execute('''
          CREATE TABLE gateway_cost_requests (accounting_id text PRIMARY KEY, model text);
          CREATE TABLE gateway_cost_attempts (accounting_id text, created_at timestamptz, profile jsonb, served_model text, cost_usd numeric, status text);
          CREATE TABLE "LiteLLM_SpendLogs" ("startTime" timestamp, custom_llm_provider text, model text, metadata jsonb, spend double precision);
          CREATE SCHEMA magiclens;
          CREATE TABLE magiclens.financial_runs (id text PRIMARY KEY, model_name text, source text);
          CREATE TABLE magiclens.financial_attempts (financial_run_id text, started_at timestamptz, provider text, served_model_id text, model_name text, model_id text, cost_usd numeric, cost_status text);
          INSERT INTO gateway_cost_requests VALUES ('request-1','historical-alias');
          INSERT INTO gateway_cost_attempts VALUES
            ('request-1','2026-09-20T12:00:00Z','{"vendor":"openai"}','exact-version-2020',0.100000001,'failed'),
            ('request-1','2026-09-20T12:00:01Z','{"vendor":"openai"}','exact-version-2020',0.2,'succeeded'),
            ('request-1','2026-09-20T12:00:02Z','{"vendor":"openai"}','exact-version-2020',NULL,'pending'),
            ('request-1','2026-09-21T00:00:00Z','{"vendor":"openai"}','exact-version-2020',99,'succeeded');
          INSERT INTO "LiteLLM_SpendLogs" VALUES
            ('2026-09-20T12:00:00','openai','exact-version-2020','{"cost_usd_exact":"0.100000001"}',0.1),
            ('2026-09-20T12:00:01','openai','exact-version-2020','{"cost_usd_exact":"0.2"}',0.2);
          INSERT INTO magiclens.financial_runs VALUES ('studio','alias','studio'), ('runner','alias','runner');
          INSERT INTO magiclens.financial_attempts VALUES
            ('studio','2026-09-20T12:00:00Z','openai','exact-version-2020',NULL,NULL,0.100000001,'priced'),
            ('runner','2026-09-20T12:00:01Z','openai','exact-version-2020',NULL,NULL,0.2,'priced'),
            ('runner','2026-09-20T12:00:02Z','openai','exact-version-2020',NULL,NULL,NULL,'pending');
        ''')
        env = {"GATEWAY_REPORTING_DATABASE_URL": url, "MAGICLENS_REPORTING_DATABASE_URL": url}
        for source in ("gateway", "litellm", "magiclens"):
            batches = await collect_internal(env, source, date(2026, 9, 20), date(2026, 9, 20))
            rows = next(b.rows for b in batches if b.provider == "openai")
            assert len(rows) == 1
            assert rows[0]["cost"] == "0.300000001"
            assert rows[0]["model"] == "exact-version-2020"
            assert rows[0]["pending"] == (0 if source == "litellm" else 1)
            assert rows[0]["metrics"]["requests"] == ("2" if source == "litellm" else "3")
    finally:
        await connection.execute('DROP SCHEMA IF EXISTS magiclens CASCADE; DROP TABLE IF EXISTS gateway_cost_attempts, gateway_cost_requests, "LiteLLM_SpendLogs";')
        await connection.close()
