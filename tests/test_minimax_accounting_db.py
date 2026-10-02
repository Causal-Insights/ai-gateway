"""H3 provider migration and journal integration, against disposable PostgreSQL."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from cost_accounting import CostAccounting, canonical_key
from generation_job_models import ProviderStatus
from generation_job_repository import GenerationJobRepository
from pricing_registry import PricingRegistry


@unittest.skipUnless(os.environ.get('RUN_COST_DB_TESTS') == '1', 'requires isolated PostgreSQL')
class MiniMaxDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository=GenerationJobRepository()
        self.pool=await self.repository.pool()
        self.prices=PricingRegistry()
        self.ledger=CostAccounting(self.repository,self.prices)
        self.uid='h3-test-'+uuid4().hex
        self.key=canonical_key('sk-'+self.uid)
        await self.pool.execute('insert into "LiteLLM_UserTable" (user_id,models) values($1,\'{}\')',self.uid)
        await self.pool.execute('insert into "LiteLLM_VerificationToken" (token,user_id,models) values($1,$2,\'{}\')',self.key,self.uid)
        self.identity={'api_key_hash':self.key,'user_id':self.uid}
        self.account=await self.ledger.begin(model='minimax-h3',route='generation_job',identity=self.identity,owner_hash=self.key)
        self.profile=self.prices.select('minimax-h3','generation_job')
        self.attempt=await self.ledger.attempt(self.account,self.profile)

    async def asyncTearDown(self):
        await self.pool.execute('delete from gateway_generation_jobs where owner_key_hash=$1',self.key)
        await self.pool.execute('delete from "LiteLLM_SpendLogs" where api_key=$1',self.key)
        await self.pool.execute('delete from "LiteLLM_DailyUserSpend" where api_key=$1',self.key)
        await self.pool.execute('delete from gateway_cost_cache_outbox where attempt_id=$1',self.attempt)
        await self.pool.execute('delete from gateway_cost_attempts where accounting_id=$1',self.account)
        await self.pool.execute('delete from gateway_cost_requests where accounting_id=$1',self.account)
        await self.pool.execute('delete from "LiteLLM_VerificationToken" where token=$1',self.key)
        await self.pool.execute('delete from "LiteLLM_UserTable" where user_id=$1',self.uid)
        await self.repository.close()

    async def test_charged_failure_duplicate_receipts_and_pinned_price(self):
        before=await self.pool.fetchrow('select spend from "LiteLLM_SpendLogs" where request_id=$1',self.attempt)
        self.assertIsNotNone(before);self.assertIsNone(before['spend'])
        raw={'output_seconds':4,'input_seconds':3,'input_image_count':7,'input_audio_seconds':10,'total_tokens':123456}
        self.prices.profiles[self.profile['version']]['components'][0]['rate']='99'
        await asyncio.gather(*(self.ledger.observe(self.attempt,raw_usage=raw,served_model='MiniMax-H3',
            served_options={'resolution':'768p'},provider_request_id='task-'+self.uid,outcome='failure') for _ in range(3)))
        await self.ledger.finish(self.account)
        result=await self.ledger.get(self.account,self.key)
        self.assertEqual(Decimal(result['cost_usd']),Decimal('.64'))
        self.assertTrue(result['billing_eligible'])
        self.assertIsNone(await self.ledger.get(self.account,canonical_key('another-owner')))
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1',self.key),1)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1',self.key),.64)
        self.assertEqual(await self.pool.fetchval('select sum(failed_requests) from "LiteLLM_DailyUserSpend" where api_key=$1',self.key),1)
        await self.ledger.observe(self.attempt,raw_usage={},outcome='failure')
        self.assertEqual(Decimal((await self.ledger.get(self.account,self.key))['cost_usd']),Decimal('.64'))

    async def test_partial_usage_stays_visible_then_later_evidence_prices(self):
        await self.ledger.observe(self.attempt,raw_usage={'output_seconds':4},served_model='MiniMax-H3',
            served_options={'resolution':'768p'},outcome='failure')
        result=await self.ledger.get(self.account,self.key)
        self.assertIsNone(result['cost_usd']);self.assertEqual(result['cost_status'],'unresolved')
        self.assertFalse(result['billing_eligible'])
        self.assertIsNone(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1',self.attempt))
        await self.ledger.observe(self.attempt,raw_usage={'input_seconds':0,'input_image_count':0},outcome='failure')
        self.assertEqual(Decimal((await self.ledger.get(self.account,self.key))['cost_usd']),Decimal('.32'))

    async def test_migration_repeatability_duplicate_jobs_and_terminal_identity(self):
        await self.repository._migrate()
        args=dict(owner_key_hash=self.key,owner_context=self.identity,idempotency_key=self.uid,request_hash='gj2:h3',
            modality='video',model='MiniMax-H3',provider='minimax',request_metadata={},
            deadline_at=datetime.now(timezone.utc)+timedelta(hours=2),callback_token_hash=None,
            request_schema_version=2,provider_route='minimax_h3_v2',adapter_revision='minimax_h3_v2@2026-10-02')
        results=await asyncio.gather(*(self.repository.create_or_get(job_id='gen_'+uuid4().hex,**args) for _ in range(2)))
        self.assertEqual(sum(int(r[1]) for r in results),1)
        row=results[0][0]
        await self.repository.mark_submitted(row['id'],provider_request_id='task-'+self.uid,provider_status='queued',progress=None,request_metadata={},next_poll_at=datetime.now(timezone.utc))
        row=await self.repository.apply_provider_status(row['id'],ProviderStatus(status='completed',provider_status='succeeded',
            result_url='https://cdn.example/h3.mp4',served_model='MiniMax-H3',usage={'output_seconds':4,'input_seconds':0,'input_image_count':0}))
        await self.repository._migrate()
        row=await self.repository.apply_provider_status(row['id'],ProviderStatus(status='failed',provider_status='failed'))
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['model'],'MiniMax-H3')
        self.assertEqual(row['provider_route'],'minimax_h3_v2')
        self.assertEqual(row['adapter_revision'],'minimax_h3_v2@2026-10-02')
