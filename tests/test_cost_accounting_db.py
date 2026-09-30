import asyncio
import os
import unittest
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from cost_accounting import CostAccounting, canonical_key, encode
from generation_job_repository import GenerationJobRepository
from pricing_registry import PricingRegistry
from test_cost_accounting import price_document


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
class AccountingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = GenerationJobRepository()
        self.prices = PricingRegistry(price_document())
        self.ledger = CostAccounting(self.repository, self.prices)
        self.uid = "accounting-test-" + uuid4().hex
        self.key = canonical_key("sk-" + self.uid)
        self.identity = {"api_key_hash": self.key, "user_id": self.uid, "team_id": self.uid}
        self.pool = await self.repository.pool()
        await self.pool.execute('insert into "LiteLLM_UserTable" (user_id,models) values($1,\'{}\')', self.uid)
        await self.pool.execute('insert into "LiteLLM_TeamTable" (team_id,admins,members,models,updated_at) values($1,\'{}\',\'{}\',\'{}\',now())', self.uid)
        await self.pool.execute('insert into "LiteLLM_VerificationToken" (token,user_id,team_id,models) values($1,$2,$2,\'{}\')', self.key, self.uid)
        self.request_id = await self.ledger.begin(model="test-model", route="completion", identity=self.identity)
        self.attempt_id = await self.ledger.attempt(self.request_id, self.prices.profiles["test-v1"])

    async def asyncTearDown(self):
        await self.pool.execute('delete from "LiteLLM_SpendLogs" where request_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)', self.request_id)
        await self.pool.execute('delete from gateway_cost_corrections where accounting_id=$1', self.request_id)
        await self.pool.execute('delete from "LiteLLM_SpendLogs" where api_key=$1', self.key)
        for table in ("DailyUserSpend", "DailyTeamSpend"):
            await self.pool.execute(f'delete from "LiteLLM_{table}" where api_key=$1', self.key)
        await self.pool.execute('delete from gateway_cost_cache_outbox where attempt_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)', self.request_id)
        await self.pool.execute('delete from gateway_cost_attempts where accounting_id=$1', self.request_id)
        await self.pool.execute('delete from gateway_cost_requests where accounting_id=$1', self.request_id)
        await self.pool.execute('delete from "LiteLLM_VerificationToken" where token=$1', self.key)
        await self.pool.execute('delete from "LiteLLM_UserTable" where user_id=$1', self.uid)
        await self.pool.execute('delete from "LiteLLM_TeamTable" where team_id=$1', self.uid)
        await self.repository.close()

    async def test_concurrent_duplicate_callbacks_are_one_atomic_cost(self):
        await asyncio.gather(*(self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000}, served_model="test-model") for _ in range(8)))
        await self.ledger.finish(self.request_id)
        result = await self.ledger.get(self.request_id, self.key)
        self.assertEqual(Decimal(result["cost_usd"]), Decimal("0.71"))
        self.assertTrue(result["billing_eligible"])
        self.assertIsNone(await self.ledger.get(self.request_id, canonical_key("someone-else")))
        for table in ("UserTable", "TeamTable", "VerificationToken"):
            field, value = ("token", self.key) if table == "VerificationToken" else ("team_id", self.uid) if table == "TeamTable" else ("user_id", self.uid)
            self.assertAlmostEqual(await self.pool.fetchval(f'select spend from "LiteLLM_{table}" where {field}=$1', value), .71)
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', self.key), 1)
        self.assertEqual(await self.pool.fetchval('select sum(api_requests) from "LiteLLM_DailyUserSpend" where api_key=$1', self.key), 1)
        self.assertEqual(await self.pool.fetchval('select sum(api_requests) from "LiteLLM_DailyTeamSpend" where api_key=$1', self.key), 1)

    async def test_all_attribution_projections_and_budget_admission(self):
        from pricing_registry import PricingError
        global_id = self.uid + "-proxy"
        identity = {**self.identity, "org_id": self.uid, "project_id": self.uid,
                    "end_user_id": self.uid, "agent_id": self.uid, "tags": [self.uid],
                    "proxy_budget_id": global_id}
        await self.pool.execute('''insert into "LiteLLM_BudgetTable" (budget_id,max_budget,created_by,updated_by)
            values($1,1,'test','test')''', self.uid)
        await self.pool.execute('''insert into "LiteLLM_OrganizationTable"
            (organization_id,organization_alias,budget_id,models,created_by,updated_by)
            values($1,$1,$1,'{}','test','test')''', self.uid)
        await self.pool.execute('''insert into "LiteLLM_ProjectTable" (project_id,team_id,budget_id,models,created_by,updated_by)
            values($1,$1,$1,'{}','test','test')''', self.uid)
        await self.pool.execute('insert into "LiteLLM_EndUserTable" (user_id,budget_id) values($1,$1)', self.uid)
        await self.pool.execute('insert into "LiteLLM_TagTable" (tag_name,models,budget_id) values($1,\'{}\',$1)', self.uid)
        await self.pool.execute('insert into "LiteLLM_TeamMembership" (user_id,team_id,budget_id) values($1,$1,$1)', self.uid)
        await self.pool.execute('insert into "LiteLLM_OrganizationMembership" (user_id,organization_id,budget_id) values($1,$1,$1)', self.uid)
        await self.pool.execute('insert into "LiteLLM_UserTable" (user_id,models,max_budget) values($1,\'{}\',1)', global_id)
        await self.pool.execute('update gateway_cost_requests set identity=$2::jsonb where accounting_id=$1', self.request_id, encode(identity))
        try:
            await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000,
                "input_tokens": 100, "output_tokens": 10, "cached_tokens": 20}, outcome="failure")
            for table, field in (("OrganizationTable", "organization_id"), ("ProjectTable", "project_id"),
                                 ("EndUserTable", "user_id"), ("TagTable", "tag_name"),
                                 ("TeamMembership", "user_id"), ("OrganizationMembership", "user_id")):
                self.assertAlmostEqual(await self.pool.fetchval(f'select spend from "LiteLLM_{table}" where {field}=$1', self.uid), .71)
            self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_UserTable" where user_id=$1', global_id), .71)
            for table in ("DailyOrganizationSpend", "DailyEndUserSpend", "DailyAgentSpend", "DailyTagSpend"):
                row = await self.pool.fetchrow(f'select * from "LiteLLM_{table}" where api_key=$1', self.key)
                self.assertAlmostEqual(row["spend"], .71)
                self.assertEqual(row["failed_requests"], 1)
                self.assertEqual(row["cache_read_input_tokens"], 20)
            await self.ledger.check_budgets(identity, "test-model")
            await self.pool.execute('update "LiteLLM_BudgetTable" set max_budget=0.5 where budget_id=$1', self.uid)
            with self.assertRaises(PricingError):
                await self.ledger.check_budgets(identity, "test-model")
            report = await self.ledger.report()
            bucket_ids = await self.pool.fetchval('select daily_buckets::text from gateway_cost_attempts where attempt_id=$1', self.attempt_id)
            self.assertFalse(any(d["bucket_id"] in bucket_ids for d in report["aggregate_divergence"]))
        finally:
            for table in ("DailyOrganizationSpend", "DailyEndUserSpend", "DailyAgentSpend", "DailyTagSpend"):
                await self.pool.execute(f'delete from "LiteLLM_{table}" where api_key=$1', self.key)
            for table, field in (("TeamMembership", "user_id"), ("OrganizationMembership", "user_id"),
                                 ("ProjectTable", "project_id"), ("OrganizationTable", "organization_id"),
                                 ("EndUserTable", "user_id"), ("TagTable", "tag_name"), ("BudgetTable", "budget_id")):
                await self.pool.execute(f'delete from "LiteLLM_{table}" where {field}=$1', self.uid)
            await self.pool.execute('delete from "LiteLLM_UserTable" where user_id=$1', global_id)

    async def test_transaction_failure_preserves_evidence_for_restart_retry(self):
        original = self.ledger._project_litellm
        async def fail_after_insert(*args):
            await original(*args)
            raise RuntimeError("simulated crash before commit")
        self.ledger._project_litellm = fail_after_insert
        await self.ledger.observe(self.attempt_id, raw_usage={"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', self.key), 1)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id), .000032)
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), 0)
        restarted = CostAccounting(self.repository, self.prices)
        self.assertEqual((await restarted.reconcile())["committed"], 1)
        self.assertEqual((await restarted.reconcile())["committed"], 0)

    async def test_unknown_failure_is_visible_and_never_free(self):
        await self.ledger.observe(self.attempt_id, raw_usage={}, outcome="failure")
        await self.ledger.finish(self.request_id)
        result = await self.ledger.get(self.request_id)
        self.assertEqual(result["cost_status"], "unresolved")
        self.assertIsNone(result["cost_usd"])
        self.assertFalse(result["billing_eligible"])
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', self.key), 1)
        self.assertIsNone(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id))

    async def test_distinct_retries_each_count_even_with_same_provider_request_id(self):
        second = await self.ledger.attempt(self.request_id, self.prices.profiles["test-v1"])
        for attempt_id, outcome in ((self.attempt_id, "failure"), (second, "success")):
            await self.ledger.observe(attempt_id, raw_usage={"cost_in_usd_ticks": 1000000000}, outcome=outcome, provider_request_id="same-external-id")
        await self.ledger.finish(self.request_id)
        result = await self.ledger.get(self.request_id)
        self.assertEqual(Decimal(result["cost_usd"]), Decimal("0.2"))
        self.assertEqual(len(result["breakdown"]), 2)

    async def test_fresh_budget_floor_blocks_after_commit(self):
        from pricing_registry import PricingError
        await self.pool.execute('update "LiteLLM_VerificationToken" set max_budget=0.05 where token=$1', self.key)
        await self.ledger.check_budgets(self.identity, "test-model")
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 1000000000})
        with self.assertRaises(PricingError):
            await self.ledger.check_budgets(self.identity, "test-model")

    async def test_daily_projection_divergence_is_detected(self):
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 1000000000})
        bucket = await self.pool.fetchval('select id from "LiteLLM_DailyUserSpend" where api_key=$1', self.key)
        await self.pool.execute('update "LiteLLM_DailyUserSpend" set spend=spend+1 where id=$1', bucket)
        report = await self.ledger.report()
        self.assertTrue(any(row["bucket_id"] == bucket for row in report["aggregate_divergence"]))

    async def test_retained_provider_usage_reproduces_enabled_upstream_amounts(self):
        import json
        from pathlib import Path
        from accounting_usage import extract
        prices = PricingRegistry()
        for name, expected in (("omni-usage.json", "0.5144755"), ("grok-video-1.5-usage.json", "0.71")):
            fixture = json.loads((Path(__file__).resolve().parents[1] / "pricing/evidence" / name).read_text())
            profile = prices.profiles[prices.models[fixture["model"]]["profiles"][0]]
            # Replay original provider quantities at today's verified rate for
            # acceptance, not as a historical correction or a fresh API call.
            attempt = await self.ledger.attempt(self.request_id, profile)
            await self.ledger.observe(attempt, raw_usage=fixture["usage"], served_model=fixture["upstream_model"])
            row = await self.pool.fetchrow("select cost_status,cost_usd from gateway_cost_attempts where attempt_id=$1", attempt)
            self.assertEqual(row["cost_status"], "priced")
            self.assertEqual(row["cost_usd"], Decimal(expected))
            spent = await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', attempt)
            self.assertAlmostEqual(spent, float(expected))

    async def test_read_only_repository_cannot_migrate_or_write(self):
        import asyncpg
        read_only = GenerationJobRepository(read_only=True)
        try:
            pool = await read_only.pool()
            self.assertEqual(await pool.fetchval("show default_transaction_read_only"), "on")
            with self.assertRaises(asyncpg.ReadOnlySQLTransactionError):
                await pool.execute("update gateway_cost_requests set finished_at=now() where accounting_id=$1", self.request_id)
        finally:
            await read_only.close()

    async def test_legacy_key_hash_and_historical_repair_are_idempotent(self):
        import hashlib
        import cost_repairs
        from datetime import datetime, timedelta, timezone
        from generation_job_models import ProviderStatus
        job_id = "gen_" + self.uid
        owner = hashlib.sha256(self.key.encode()).hexdigest()
        await self.repository.create_or_get(job_id=job_id,owner_key_hash=owner,
            owner_context={**self.identity, "api_key_hash": owner},idempotency_key=self.uid,
            request_hash="fixture",modality="video",model="test-model",provider="xai",
            provider_route="grok_video",adapter_revision="test-v1",
            request_metadata={"upstream_model": "test-model"},deadline_at=datetime.now(timezone.utc)+timedelta(hours=1),callback_token_hash=None)
        await self.repository.mark_submitted(job_id,provider_request_id=self.uid,provider_status="queued",progress=None,
            request_metadata={},next_poll_at=datetime.now(timezone.utc))
        await self.repository.apply_provider_status(job_id,ProviderStatus(status="completed",provider_status="done",
            usage={"cost_in_usd_ticks":1000000000},cost_usd=999))
        await self.pool.execute("update gateway_generation_jobs set spend_logged_at=now() where id=$1", job_id)
        try:
            with patch.object(cost_repairs, "accounting", self.ledger):
                preview = await cost_repairs.repair_missing_job(job_id,"test-v1")
                self.assertFalse(preview["applied"])
                first = await cost_repairs.repair_missing_job(job_id,"test-v1",apply=True)
                second = await cost_repairs.repair_missing_job(job_id,"test-v1",apply=True)
                self.assertTrue(first["applied"])
                self.assertTrue(second["already_applied"])
                cost = await self.ledger.get(job_id,owner)
                self.assertEqual(cost["cost_usd"], "0.1")
                self.assertFalse(cost["billing_eligible"])
                self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1',self.key),0)
                self.assertEqual(await self.pool.fetchval("select count(*) from gateway_cost_corrections where accounting_id=$1",job_id),1)
        finally:
            await self.pool.execute("delete from gateway_cost_corrections where accounting_id=$1",job_id)
            await self.pool.execute("delete from gateway_cost_cache_outbox where attempt_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)",job_id)
            await self.pool.execute("delete from gateway_cost_attempts where accounting_id=$1",job_id)
            await self.pool.execute("delete from gateway_cost_requests where accounting_id=$1",job_id)
            await self.pool.execute("delete from gateway_generation_jobs where id=$1",job_id)


if __name__ == "__main__":
    unittest.main()
