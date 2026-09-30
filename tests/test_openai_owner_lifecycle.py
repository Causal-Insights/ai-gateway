"""Owned account cleanup uses no generation calls and retains numeric receipts."""
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
import httpx
import owned_openai_resources as resources
import test_owned_openai_resources as resource_fixtures


@unittest.skipUnless(os.environ.get('RUN_COST_DB_TESTS')=='1','requires isolated PostgreSQL')
class OwnerLifecycle(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=resource_fixtures.OwnedResourceTests.asyncSetUp
    asyncTearDown=resource_fixtures.OwnedResourceTests.asyncTearDown
    upload=resource_fixtures.OwnedResourceTests.upload
    make_batch=resource_fixtures.OwnedResourceTests.make_batch

    async def test_native_auth_allows_cleanup_on_exhausted_key_but_preserves_paid_budget(self):
        import importlib
        import openai_owner_lifecycle as lifecycle
        from litellm.proxy import proxy_server
        from litellm.proxy._types import LiteLLMRoutes
        native=importlib.import_module('litellm.proxy.auth.user_api_key_auth')
        original=native._should_skip_budget_checks
        original_key=native._virtual_key_max_budget_check
        lifecycle.install_budget_exemption()
        self.addCleanup(setattr,native,'_should_skip_budget_checks',original)
        self.addCleanup(setattr,native,'_virtual_key_max_budget_check',original_key)
        for routes in (LiteLLMRoutes.openai_routes.value,LiteLLMRoutes.llm_api_routes.value):
            if '/v1/resources/owner-data' not in routes:
                routes.append('/v1/resources/owner-data');self.addCleanup(routes.remove,'/v1/resources/owner-data')
        resources.resource_app.dependency_overrides.clear()
        self.user.max_budget=1;self.user.spend=2
        with patch.object(proxy_server,'master_key','sk-offline-master'),patch.object(proxy_server,'prisma_client',MagicMock()), \
             patch.object(proxy_server,'get_current_spend',new=AsyncMock(return_value=2)), \
             patch.object(native.IdentityStore,'resolve',new=AsyncMock(return_value=object())), \
             patch.object(native.IdentityStore,'key_from_principal',return_value=self.user):
            response=await self.client.delete('/v1/resources/owner-data',headers={'Authorization':'Bearer sk-offline-user'})
            self.assertEqual(response.status_code,200,response.text)
            paid=await self.client.get('/v1/files',headers={'Authorization':'Bearer sk-offline-user'})
            self.assertEqual(paid.status_code,429,paid.text)
        self.provider.assert_not_awaited()

    async def test_export_is_owner_metadata_only_and_purge_revokes_all_routes(self):
        file=(await self.upload(b'private bytes')).json()
        exported=(await self.client.get('/v1/resources/owner-data')).json()
        self.assertEqual(len(exported['resources']),1)
        self.assertNotIn('private bytes',json.dumps(exported))
        foreign=(await self.client.get('/v1/resources/owner-data',headers={'X-Gateway-Resource-Owner':'bob'})).json()
        self.assertEqual(foreign['resources'],[])
        result=await self.client.delete('/v1/resources/owner-data')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['cleanup_status'],'complete')
        calls=len(self.calls)
        self.assertEqual((await self.client.get('/v1/files/'+file['id']+'/content')).status_code,410)
        self.assertEqual((await self.upload(b'new','user_data','new')).status_code,410)
        self.assertEqual(len(self.calls),calls)
        self.assertEqual((await self.client.delete('/v1/resources/owner-data')).json()['cleanup_status'],'complete')

    async def test_cancel_pending_preserves_files_then_retries_settle_before_delete(self):
        batch,_=await self.make_batch(2)
        original=self.provider.side_effect
        async def cancelling(method,path,**kwargs):
            if path.endswith('/cancel'):
                self.calls.append((method,path,kwargs))
                self.batches[batch['id']]['status']='cancelling'
                return httpx.Response(200,json=self.batches[batch['id']])
            return await original(method,path,**kwargs)
        self.provider.side_effect=cancelling
        first=(await self.client.delete('/v1/resources/owner-data')).json()
        self.assertTrue(first['revoked']);self.assertEqual(first['cleanup_status'],'pending')
        self.assertFalse(any(method=='DELETE' for method,_,_ in self.calls))
        output='file_result'
        body={'id':'resp_fixture','model':'gpt-6-astra','usage':{'input_tokens':10,'output_tokens':5,
              'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}},'output':[]}
        self.files[output]=(json.dumps({'custom_id':'0','response':{'status_code':200,'body':body}})+'\n').encode()
        self.batches[batch['id']].update(status='cancelled',output_file_id=output)
        self.provider.side_effect=original
        second=(await self.client.delete('/v1/resources/owner-data')).json()
        self.assertEqual(second['cleanup_status'],'complete',second)
        receipt=await self.ledger.get(second['receipts'][0]['accounting_id'])
        self.assertEqual(Decimal(receipt['cost_usd']),Decimal('.000175'))
        self.assertIsNone((await self.ledger.get(second['receipts'][1]['accounting_id']))['cost_usd'])
        actions=[(method,path) for method,path,_ in self.calls]
        self.assertLess(actions.index(('GET','/files/'+output+'/content')),actions.index(('DELETE','/files/'+output)))
        self.assertFalse(any(method=='DELETE' and path.startswith('/batches/') for method,path in actions))
        self.assertEqual((await self.client.post('/v1/batches/'+batch['id']+'/cancel')).status_code,410)

    async def test_response_cancels_and_retains_receipt_before_removing_resources(self):
        owner=(self.app_id,'alice')
        await resources.remember('response','resp_pending',owner,{'id':'resp_pending','gateway_accounting_id':self.request_id,'gateway_attempt_id':self.attempt_id})
        self.responses['resp_pending']={'id':'resp_pending','model':'test-model','status':'in_progress','output':[]}
        original=self.provider.side_effect
        async def upstream(method,path,**kwargs):
            if path=='/responses/resp_pending/cancel':
                self.calls.append((method,path,kwargs));self.responses['resp_pending'].update(status='cancelled',usage={'cost_in_usd_ticks':200000000})
                return httpx.Response(200,json=self.responses['resp_pending'])
            return await original(method,path,**kwargs)
        self.provider.side_effect=upstream
        result=(await self.client.delete('/v1/resources/owner-data')).json()
        self.assertEqual(result['cleanup_status'],'complete',result)
        self.assertEqual(Decimal((await self.ledger.get(self.request_id))['cost_usd']),Decimal('.02'))
        self.assertIn(('DELETE','/responses/resp_pending'),[(method,path) for method,path,_ in self.calls])
        row=await self.pool.fetchrow("select data from gateway_openai_resources where provider_id='resp_pending' and app_id=$1",self.app_id)
        self.assertEqual(json.loads(row['data'])['gateway_accounting_id'],self.request_id)

    async def test_unknown_create_remains_pending_and_late_file_is_deleted(self):
        owner=(self.app_id,'alice')
        row,_=await resources.intent('file',owner,'lost',b'original')
        await resources.save(row,'outcome_unknown')
        result=(await self.client.delete('/v1/resources/owner-data')).json()
        self.assertEqual(result['pending'][0]['reason'],'provider_identity_unknown')
        before=len(self.calls)
        await resources.remember('file','file_late',owner,{'id':'file_late'})
        self.assertIn(('DELETE','/files/file_late'),[(method,path) for method,path,_ in self.calls[before:]])
        self.assertFalse(any(method=='POST' for method,_,_ in self.calls))
        self.assertEqual((await self.client.get('/v1/files/file_late')).status_code,410)
