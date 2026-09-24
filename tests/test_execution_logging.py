"""Non-negotiable: secondary accounting failures must never hide executions."""
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from cost_accounting import CostAccounting, encode
from test_cost_accounting_db import AccountingDatabaseTests


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
class ExecutionLoggingTests(AccountingDatabaseTests):
    async def log_row(self):
        row = dict(await self.pool.fetchrow('select * from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id))
        row["metadata"] = json.loads(row["metadata"])
        return row

    async def test_attempt_is_visible_before_submission_with_unknown_not_zero_cost(self):
        row = await self.log_row()
        self.assertIsNone(row["spend"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["metadata"]["cost_status"], "pending")

    async def test_standalone_key_cost_is_logged_with_unresolved_attribution(self):
        await self.pool.execute('update "LiteLLM_VerificationToken" set user_id=null,team_id=null where token=$1', self.key)
        await self.pool.execute("update gateway_cost_requests set identity=$2::jsonb where accounting_id=$1",
                                self.request_id, encode({"api_key_hash": self.key}))
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        await self.ledger.finish(self.request_id)
        row = await self.log_row()
        self.assertEqual(row["spend"], .71)
        self.assertEqual(row["api_key"], self.key)
        self.assertEqual(row["metadata"]["attribution_status"], "unresolved")
        cost = await self.ledger.get(self.request_id)
        self.assertEqual(cost["cost_usd"], "0.71")
        self.assertFalse(cost["billing_eligible"])
        daily = await self.pool.fetchrow('select * from "LiteLLM_DailyUserSpend" where api_key=$1', self.key)
        self.assertEqual(daily["user_id"], "")
        self.assertEqual(daily["api_requests"], 1)
        self.assertAlmostEqual(daily["spend"], .71)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_UserTable" where user_id=$1', self.uid), 0)

    async def test_lost_attribution_after_submission_cannot_hide_known_cost(self):
        await self.pool.execute("update gateway_cost_requests set identity='{}'::jsonb where accounting_id=$1", self.request_id)
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        row = await self.log_row()
        self.assertEqual(row["spend"], .71)
        # Preserve the authenticated key already recorded before submission.
        self.assertEqual(row["api_key"], self.key)
        self.assertEqual(row["metadata"]["attribution_status"], "unresolved")
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), 0)
        await self.pool.execute('delete from "LiteLLM_DailyUserSpend" where id in (select value from gateway_cost_attempts a cross join lateral jsonb_each_text(a.daily_buckets) where a.attempt_id=$1)', self.attempt_id)

    async def test_model_verification_failure_preserves_provider_reported_cost(self):
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000}, served_model="unexpected-model")
        row = await self.log_row()
        self.assertEqual(row["spend"], .71)
        self.assertEqual(row["model"], "unexpected-model")
        self.assertIn("Served model", row["metadata"]["pricing_issue"])
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), 0)

    async def test_calculator_exception_keeps_already_calculated_cost(self):
        with patch.object(self.prices, "calculate", side_effect=RuntimeError("calculator unavailable")):
            await self.ledger.observe(self.attempt_id, raw_usage={"gateway_native_cost_usd": "0.00039696"})
        row = await self.log_row()
        self.assertEqual(row["spend"], .00039696)
        self.assertEqual(row["metadata"]["cost_source"], "litellm")
        self.assertIn("calculator unavailable", row["metadata"]["pricing_issue"])

    async def test_late_usage_upgrades_visible_unknown_execution(self):
        await self.ledger.observe(self.attempt_id, raw_usage={}, outcome="incomplete")
        self.assertIsNone((await self.log_row())["spend"])
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        self.assertEqual((await self.log_row())["spend"], .71)
        self.assertEqual(await self.pool.fetchval("select count(*) from gateway_cost_observations where attempt_id=$1", self.attempt_id), 2)

    async def test_conflicting_receipt_never_erases_prior_cost_or_double_charges(self):
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 8000000000})
        row = await self.log_row()
        self.assertEqual(row["spend"], .71)
        self.assertIn("Conflicting provider receipts", row["metadata"]["pricing_issue"])
        self.assertEqual(await self.pool.fetchval("select count(*) from gateway_cost_observations where attempt_id=$1", self.attempt_id), 2)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)

    async def test_deleted_detail_is_recreated_without_reapplying_budgets(self):
        await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        await self.pool.execute('delete from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id)
        await self.ledger.reconcile()
        self.assertEqual((await self.log_row())["spend"], .71)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)

    async def test_pre_fix_unresolved_attribution_replays_once(self):
        await self.pool.execute("update gateway_cost_requests set identity=$2::jsonb where accounting_id=$1",
                                self.request_id, encode({"api_key_hash": self.key}))
        await self.pool.execute("""update gateway_cost_attempts set raw_usage=$2::jsonb,
            observed_at=now(),outcome='success',cost_status='unresolved',unresolved_reason='Incomplete billing attribution'
            where attempt_id=$1""", self.attempt_id, encode({"cost_in_usd_ticks": 7100000000}))
        await self.pool.execute('delete from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id)
        await self.ledger.reconcile()
        self.assertEqual((await self.log_row())["spend"], .71)
        self.assertEqual((await self.ledger.reconcile())["committed"], 0)
        self.assertEqual(await self.pool.fetchval("select count(*) from gateway_cost_corrections where accounting_id=$1", self.request_id), 1)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)

    async def test_database_receipt_failure_leaves_visible_intent_and_replayable_numeric_receipt(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(self.ledger, "pool", side_effect=ConnectionError("database down")), redirect_stdout(output):
            with self.assertRaises(ConnectionError):
                await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks": 7100000000})
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["event"], "gateway_execution_receipt")
        self.assertIsNone((await self.log_row())["spend"])
        await self.ledger.observe(receipt["attempt_id"], **receipt["evidence"])
        self.assertEqual((await self.log_row())["spend"], .71)

    async def test_generated_prisma_client_reads_unknown_spend_as_null(self):
        from prisma import Prisma
        client = Prisma(datasource={"url": os.environ["GATEWAY_DATABASE_URL"]})
        try:
            await client.connect()
            row = await client.litellm_spendlogs.find_unique(where={"request_id": self.attempt_id})
            self.assertIsNotNone(row)
            self.assertIsNone(row.spend)
        finally:
            await client.disconnect()

    async def test_retained_job_without_intent_is_visible_without_repricing_history(self):
        from datetime import datetime, timedelta, timezone
        from generation_job_models import ProviderStatus
        job_id = 'gen_retained_' + self.uid
        await self.repository.create_or_get(job_id=job_id, owner_key_hash=self.key,
            owner_context={'api_key_hash': self.key}, idempotency_key=self.uid,
            request_hash='fixture', modality='video', model='test-model', provider='xai',
            provider_route='grok_video', adapter_revision='test', request_metadata={},
            deadline_at=datetime.now(timezone.utc)+timedelta(hours=1), callback_token_hash=None)
        await self.repository.apply_provider_status(job_id, ProviderStatus(status='completed',
            provider_status='done', usage={'cost_in_usd_ticks': 7100000000}, cost_usd=.71))
        # The original job predates recovery. Both log timestamps must follow
        # the original execution, not the temporary row's insertion time.
        start = datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=31, milliseconds=35)
        await self.pool.execute('update gateway_generation_jobs set created_at=$2,submitted_at=$2,completed_at=$3 where id=$1',
                                job_id, start, end)
        try:
            job = await self.repository.get(job_id)
            first = await self.ledger.recover_job(job)
            await self.ledger.recover_job(job)
            row = await self.pool.fetchrow('select * from "LiteLLM_SpendLogs" where request_id=$1', first)
            self.assertEqual(row['spend'], .71)
            self.assertEqual(row['startTime'], start.replace(tzinfo=None))
            self.assertEqual(row['endTime'], end.replace(tzinfo=None))
            self.assertEqual((row['endTime'] - row['startTime']).total_seconds(), 31.035)
            metadata = json.loads(row['metadata'])
            self.assertEqual(metadata['cost_source'], 'legacy_gateway_evidence')
            self.assertFalse(metadata['billing_eligible'])
            self.assertIsNotNone(metadata['pricing_issue'])
            self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), 0)
            self.assertEqual(await self.pool.fetchval('select count(*) from gateway_cost_attempts where accounting_id=$1', job_id), 1)
        finally:
            await self.pool.execute('delete from gateway_generation_jobs where id=$1', job_id)
            await self.pool.execute('delete from gateway_cost_attempts where accounting_id=$1', job_id)
            await self.pool.execute('delete from gateway_cost_requests where accounting_id=$1', job_id)

    async def test_disabled_profile_after_submission_keeps_explicit_charge(self):
        profile = {**self.prices.profiles['test-v1'], 'enabled': False, 'status': 'unverified'}
        await self.pool.execute('update gateway_cost_attempts set profile=$2::jsonb where attempt_id=$1', self.attempt_id, encode(profile))
        await self.ledger.observe(self.attempt_id, raw_usage={'cost_in_usd_ticks': 7100000000})
        self.assertEqual((await self.log_row())['spend'], .71)

    async def test_media_job_bookkeeping_failure_cannot_roll_back_execution(self):
        real_pool = self.pool
        class FailingJobMarkerPool:
            def __getattr__(self, name):
                return getattr(real_pool, name)
            async def execute(self, query, *args):
                if 'update gateway_generation_jobs set spend_logged_at' in query:
                    raise RuntimeError('media bookkeeping unavailable')
                return await real_pool.execute(query, *args)
        with patch.object(self.ledger, 'pool', return_value=FailingJobMarkerPool()):
            await self.ledger.observe(self.attempt_id, raw_usage={'cost_in_usd_ticks': 7100000000})
        self.assertEqual((await self.log_row())['spend'], .71)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)

    async def test_malformed_secondary_usage_field_does_not_hide_reported_charge(self):
        await self.ledger.observe(self.attempt_id, raw_usage={'cost_in_usd_ticks': 7100000000, 'prompt_tokens_details': 17})
        self.assertEqual((await self.log_row())['spend'], .71)

    async def test_timestamp_repair_is_idempotent_and_preserves_costs_and_budgets(self):
        from datetime import datetime, timedelta, timezone
        from scripts.repair_execution_timestamps import repair
        await self.ledger.observe(self.attempt_id, raw_usage={'cost_in_usd_ticks': 7100000000})
        start = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=31)
        await self.pool.execute("""update gateway_cost_attempts set
            profile=jsonb_set(profile,'{version}','"retained-execution-v1"'::jsonb),
            created_at=$2,observed_at=$3 where attempt_id=$1""", self.attempt_id, start, end)
        await self.pool.execute('update "LiteLLM_SpendLogs" set "endTime"=$2 where request_id=$1',
                                self.attempt_id, end.replace(tzinfo=None))
        before_budget = await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key)
        async with self.pool.acquire() as conn:
            self.assertEqual((await repair(conn))['matching_records'], 1)
            self.assertGreater((await self.log_row())['startTime'], end.replace(tzinfo=None))
            self.assertEqual((await repair(conn, apply=True))['matching_records'], 1)
            self.assertEqual((await repair(conn, apply=True))['matching_records'], 0)
        row = await self.log_row()
        self.assertEqual(row['startTime'], start.replace(tzinfo=None))
        self.assertEqual((row['endTime'] - row['startTime']).total_seconds(), 31)
        self.assertEqual(row['spend'], .71)
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), before_budget)
        self.assertEqual(await self.pool.fetchval('select count(*) from gateway_cost_corrections where correction_id=$1',
                                                 self.attempt_id+':execution-timestamps-v1'), 1)
