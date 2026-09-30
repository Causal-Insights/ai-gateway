"""Actual ASGI + isolated PostgreSQL owner and batch-accounting regressions."""
import asyncio
import copy
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth

import owned_openai_resources as resources
import test_cost_accounting_db as fixtures
from pricing_registry import PricingRegistry
from test_hosted_tools import CONTRACT


class NativeBatchAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_model_permissions_reject_forbidden_batch_item(self):
        from litellm.proxy import proxy_server
        from litellm.proxy._types import ProxyException
        user = UserAPIKeyAuth(api_key="fixture", models=["gpt-5.4-mini"])
        with patch.object(proxy_server, "prisma_client", None), patch.object(proxy_server, "llm_router", None):
            with self.assertRaises(ProxyException):
                await resources.authorize_model(user, "gpt-6-astra")
            for models in (["*"], ["all-proxy-models"]):
                await resources.authorize_model(UserAPIKeyAuth(api_key="fixture", models=models), "gpt-6-astra")


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
class OwnedResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.AccountingDatabaseTests.asyncSetUp(self)
        await self.pool.execute((Path(__file__).parents[1]/"migrations/007_owned_openai_resources.sql").read_text())
        self.app_id = uuid4().hex
        self.user = UserAPIKeyAuth(api_key=self.key, metadata={"gateway_app_id": self.app_id, "gateway_resource_delegate": True})
        resources.resource_app.dependency_overrides[resources.user_api_key_auth] = lambda: self.user
        self.addCleanup(resources.resource_app.dependency_overrides.clear)
        prices = PricingRegistry()
        prices.document["hosted_tools"] = copy.deepcopy(CONTRACT)
        self.native_prices = prices
        self.patches = [patch.object(resources, "repository", self.repository), patch.object(resources, "accounting", self.ledger),
                        patch.object(resources, "registry", lambda: prices), patch.object(resources, "authorize_model", new=AsyncMock())]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.files, self.batches, self.responses, self.calls = {}, {}, {}, []
        self.lose_batch = False
        async def upstream(method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if path == "/files" and method == "POST":
                identifier = "file_" + uuid4().hex
                self.files[identifier] = kwargs["files"]["file"][1]
                return httpx.Response(200, json={"id": identifier, "purpose": kwargs["data"]["purpose"], "bytes": len(self.files[identifier])})
            if path.startswith("/files/") and path.endswith("/content"):
                return httpx.Response(200, content=self.files[path.split("/")[2]])
            if path.startswith("/files/") and method == "DELETE":
                return httpx.Response(200, json={"id": path.split("/")[2], "deleted": True})
            if path == "/batches" and method == "POST":
                identifier = "batch_" + uuid4().hex
                self.batches[identifier] = {"id": identifier, "status": "in_progress", **kwargs["json"]}
                if self.lose_batch:
                    raise httpx.ReadTimeout("fixture lost response")
                return httpx.Response(200, json=self.batches[identifier])
            if path.startswith("/batches/"):
                body = self.batches[path.split("/")[2]]
                if path.endswith("/cancel"):
                    body["status"] = "cancelled"
                return httpx.Response(200, json=body)
            if path.startswith("/responses/"):
                return httpx.Response(200, json=self.responses[path.split("/")[2]])
            raise AssertionError((method,path))
        self.provider = AsyncMock(side_effect=upstream)
        item = patch.object(resources, "provider", self.provider)
        item.start(); self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=resources.resource_app), base_url="http://gateway",
            headers={"X-Gateway-Resource-Owner": "alice"})
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        ids = [row["accounting_id"] for row in await self.pool.fetch("""select i.accounting_id from gateway_openai_batch_items i
            join gateway_openai_resources r on r.id=i.batch_id where r.app_id=$1""", self.app_id)]
        await self.pool.execute("delete from gateway_openai_batch_items where batch_id in (select id from gateway_openai_resources where app_id=$1)",self.app_id)
        await self.pool.execute("delete from gateway_openai_resources where app_id=$1",self.app_id)
        await self.pool.execute("delete from gateway_openai_owner_cleanup where app_id=$1",self.app_id)
        for identifier in ids:
            await self.pool.execute('delete from "LiteLLM_SpendLogs" where request_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)',identifier)
            await self.pool.execute("delete from gateway_cost_cache_outbox where attempt_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)",identifier)
            await self.pool.execute("delete from gateway_cost_attempts where accounting_id=$1",identifier)
            await self.pool.execute("delete from gateway_cost_requests where accounting_id=$1",identifier)
        await fixtures.AccountingDatabaseTests.asyncTearDown(self)

    async def upload(self, content=b"fixture", purpose="user_data", request_id="upload"):
        return await self.client.post("/v1/files", data={"purpose": purpose}, files={"file": ("fixture.jsonl", content, "application/jsonl")},
                                      headers={"Idempotency-Key": request_id})

    async def make_batch(self, count=1):
        lines = [{"custom_id": str(i), "method": "POST", "url": "/v1/responses",
                  "body": {"model": "gpt-6-astra", "input": "fixture", "max_output_tokens": 16}} for i in range(count)]
        uploaded = await self.upload(("\n".join(json.dumps(x) for x in lines)+"\n").encode(), "batch")
        self.assertEqual(uploaded.status_code,200,uploaded.text)
        sent = json.loads(self.files[uploaded.json()["id"]].splitlines()[0])
        self.assertNotIn("allowed_openai_params",sent["body"])
        body = {"input_file_id": uploaded.json()["id"], "endpoint": "/v1/responses", "completion_window": "24h"}
        result = await self.client.post("/v1/batches",json=body,headers={"Idempotency-Key":"batch"})
        self.assertEqual(result.status_code,200,result.text)
        return result.json(), body

    async def test_files_owner_list_download_delete_and_duplicate_upload(self):
        first = await self.upload(b"exact\0bytes")
        same = await self.upload(b"exact\0bytes")
        self.assertEqual(first.json()["id"],same.json()["id"])
        identifier=first.json()["id"]
        denied=await self.client.get("/v1/files/"+identifier+"/content",headers={"X-Gateway-Resource-Owner":"bob"})
        self.assertEqual(denied.status_code,404)
        self.assertEqual((await self.client.get("/v1/files",headers={"X-Gateway-Resource-Owner":"bob"})).json()["data"],[])
        self.assertEqual((await self.client.get("/v1/files/"+identifier+"/content")).content,b"exact\0bytes")
        self.assertEqual((await self.upload(b"different")).status_code,409)
        self.assertEqual((await self.client.delete("/v1/files/"+identifier)).status_code,200)
        self.assertEqual((await self.client.get("/v1/files/"+identifier)).status_code,404)
        self.assertEqual(sum(m=="POST" and p=="/files" for m,p,_ in self.calls),1)

    async def test_batch_duplicate_poll_cancel_prices_each_result_once_missing_unknown(self):
        batch,_=await self.make_batch(2)
        output="file_output_"+uuid4().hex
        body={"id":"resp_fixture","model":"gpt-6-astra","usage":{"input_tokens":10,"output_tokens":5,
              "input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0}},"output":[]}
        self.files[output]=(json.dumps({"custom_id":"0","response":{"status_code":200,"body":body}})+"\n").encode()
        self.batches[batch["id"]].update(status="completed",output_file_id=output)
        first=await self.client.get("/v1/batches/"+batch["id"])
        second=await self.client.get("/v1/batches/"+batch["id"])
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()["gateway_items"],second.json()["gateway_items"])
        items=first.json()["gateway_items"]
        self.assertEqual(items[0]["cost"]["cost_status"],"priced",items)
        self.assertEqual(Decimal(items[0]["cost"]["cost_usd"]),Decimal("0.000175"))
        self.assertIsNone(items[1]["cost"]["cost_usd"])
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where request_id in (select attempt_id from gateway_openai_batch_items where batch_id=$1)',batch["gateway_resource_id"]),2)
        spent=await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1',self.key)
        await self.client.post("/v1/batches/"+batch["id"]+"/cancel")
        self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1',self.key),spent)
        self.assertGreater(spent,0)

    async def test_lost_create_never_replays_and_recovery_checks_owned_marker(self):
        self.lose_batch=True
        batch,body=await self.make_batch()
        self.assertEqual(batch["gateway_state"],"outcome_unknown")
        duplicate=await self.client.post("/v1/batches",json=body,headers={"Idempotency-Key":"batch"})
        self.assertEqual(duplicate.json()["id"],batch["id"])
        self.assertEqual(sum(m=="POST" and p=="/batches" for m,p,_ in self.calls),1)
        provider_id=next(iter(self.batches))
        self.batches["batch_foreign"]={"id":"batch_foreign","metadata":{"gateway_resource_id":"other"}}
        invalid=await self.client.post("/v1/batches/"+batch["id"]+"/recover",json={"provider_id":"batch_foreign"})
        self.assertEqual(invalid.status_code,404)
        recovered=await self.client.post("/v1/batches/"+batch["id"]+"/recover",json={"provider_id":provider_id})
        self.assertEqual(recovered.json()["id"],provider_id)

    async def test_foreign_batch_and_unbounded_input_rejected_before_provider(self):
        batch,_=await self.make_batch()
        calls=len(self.calls)
        response=await self.client.post("/v1/batches/"+batch["id"]+"/cancel",headers={"X-Gateway-Resource-Owner":"bob"})
        self.assertEqual(response.status_code,404)
        self.assertEqual(len(self.calls),calls)
        bad={"custom_id":"bad","method":"POST","url":"/v1/responses","body":{"model":"gpt-6-astra","input":"fixture","tools":[{"type":"web_search"}]}}
        rejected=await self.upload(json.dumps(bad).encode(),"batch","bad")
        self.assertEqual(rejected.status_code,422)
        self.assertEqual(len(self.calls),calls)

    async def test_hosted_callback_keeps_unknown_container_charge_null_despite_native_subtotal(self):
        import gateway_accounting as callbacks
        from hosted_tools import pin_contract
        from types import SimpleNamespace
        profile = pin_contract(self.native_prices.select("gpt-6-astra","responses"),
                               {"tools":[{"type":"code_interpreter"}]},self.native_prices.document)
        attempt = await self.ledger.attempt(self.request_id,profile)
        body = {"id":"resp_container","model":"gpt-6-astra","status":"completed",
                "usage":{"input_tokens":10,"output_tokens":5,"input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0}},
                "output":[{"type":"code_interpreter_call","status":"completed","container_id":"cntr_fixture"}]}
        response = SimpleNamespace(model_dump=lambda **kwargs: body,_hidden_params={"response_cost":9})
        token = callbacks.state.set({"profile":profile,"attempts":[attempt],"route":"responses"})
        try:
            with patch.object(callbacks,"accounting",self.ledger):
                await callbacks.capture(attempt,response)
            row = await self.pool.fetchrow("select cost_status,cost_usd from gateway_cost_attempts where attempt_id=$1",attempt)
            self.assertEqual(row["cost_status"],"unresolved")
            self.assertIsNone(row["cost_usd"])
        finally:
            callbacks.state.reset(token)

    async def test_exhausted_budget_blocks_batch_submission(self):
        lines = [{"custom_id":"one","method":"POST","url":"/v1/responses",
                  "body":{"model":"gpt-6-astra","input":"fixture"}}]
        uploaded = await self.upload(json.dumps(lines[0]).encode(),"batch")
        self.assertEqual(uploaded.status_code,200,uploaded.text)
        await self.pool.execute('update "LiteLLM_VerificationToken" set max_budget=0 where token=$1',self.key)
        response = await self.client.post("/v1/batches",json={"input_file_id":uploaded.json()["id"],"endpoint":"/v1/responses"},
                                          headers={"Idempotency-Key":"exhausted"})
        self.assertEqual(response.status_code,503,response.text)
        self.assertFalse(any(method=="POST" and path=="/batches" for method,path,_ in self.calls))

    async def test_owned_background_retrieval_settles_original_profile_once(self):
        from hosted_tools import pin_contract
        profile=pin_contract(self.native_prices.select("gpt-6-astra","responses"),
                             {"tools":[{"type":"web_search"}]},self.native_prices.document)
        attempt=await self.ledger.attempt(self.request_id,profile)
        identifier="resp_"+uuid4().hex
        await resources.remember("response",identifier,(self.app_id,"alice"),
            {"id":identifier,"gateway_accounting_id":self.request_id,"gateway_attempt_id":attempt})
        self.responses[identifier]={"id":identifier,"model":"gpt-6-astra","status":"in_progress","output":[]}
        self.assertEqual((await self.client.get("/v1/responses/"+identifier)).status_code,200)
        self.assertIsNone(await self.pool.fetchval("select observed_at from gateway_cost_attempts where attempt_id=$1",attempt))
        self.responses[identifier].update(status="completed",usage={"input_tokens":10,"output_tokens":5,
            "input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0}},
            output=[{"type":"web_search_call","status":"completed","action":{"type":"search"}}])
        for _ in range(2):
            response=await self.client.get("/v1/responses/"+identifier)
            self.assertEqual(response.status_code,200,response.text)
        row=await self.pool.fetchrow("select cost_usd,raw_usage from gateway_cost_attempts where attempt_id=$1",attempt)
        self.assertEqual(row["cost_usd"],Decimal(".01035"))
        self.assertEqual(Decimal(json.loads(row["raw_usage"])["gateway_hosted_parent_cost_usd"]),Decimal(".00035"))
        self.assertEqual((await self.client.get("/v1/responses/"+identifier,
            headers={"X-Gateway-Resource-Owner":"bob"})).status_code,404)

    async def test_unavailable_output_keeps_batch_visible_without_claiming_zero(self):
        batch,_=await self.make_batch()
        self.batches[batch["id"]].update(status="completed",output_file_id="file_unavailable")
        original=self.provider.side_effect
        async def missing(method,path,**kwargs):
            if path.endswith("/content"):
                return httpx.Response(503,json={"error":"fixture temporarily unavailable"})
            return await original(method,path,**kwargs)
        self.provider.side_effect=missing
        response=await self.client.get("/v1/batches/"+batch["id"])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["status"],"completed")
        self.assertEqual(response.json()["gateway_settlement_status"],"pending")
        item=await self.pool.fetchrow("select accounting_id from gateway_openai_batch_items where batch_id=$1",batch["gateway_resource_id"])
        self.assertIsNone((await self.ledger.get(item["accounting_id"]))["cost_usd"])

    async def test_actual_asgi_native_auth_budget_rejection_precedes_resource_access(self):
        import importlib
        native=importlib.import_module("litellm.proxy.auth.user_api_key_auth")
        from litellm.proxy import proxy_server
        resources.resource_app.dependency_overrides.clear()
        self.user.max_budget=1
        self.user.spend=2
        with patch.object(proxy_server,"master_key","sk-offline-master"), \
             patch.object(proxy_server,"prisma_client",MagicMock()), \
             patch.object(proxy_server,"get_current_spend",new=AsyncMock(return_value=2)), \
             patch.object(native.IdentityStore,"resolve",new=AsyncMock(return_value=object())), \
             patch.object(native.IdentityStore,"key_from_principal",return_value=self.user):
            response=await self.client.get("/v1/files",headers={"Authorization":"Bearer sk-offline-user"})
        self.assertEqual(response.status_code,429,response.text)
        self.provider.assert_not_awaited()
