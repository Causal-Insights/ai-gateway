import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import test_cost_accounting_db as db_fixtures


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires pinned LiteLLM and isolated PostgreSQL")
class RuntimeTests(db_fixtures.AccountingDatabaseTests):
    async def test_pinned_native_hooks_persist_before_proxy_response(self):
        import litellm
        import gateway_accounting as integration
        from litellm.proxy._types import UserAPIKeyAuth
        user = UserAPIKeyAuth(api_key=self.key, user_id=self.uid, team_id=self.uid)
        callback = integration.GatewayAccounting()
        current = {}
        token = integration.state.set(current)
        old_callbacks = list(litellm.callbacks)
        litellm.callbacks = [callback]
        try:
            with patch.object(integration, "accounting", self.ledger), patch.object(integration, "registry", return_value=self.prices):
                data = await callback.async_pre_call_hook(user, None, {"model": "test-model"}, "completion")
                response = await litellm.acompletion(model="xai/test-model", messages=[{"role": "user", "content": "fixture"}], mock_response="fixture")
                self.assertEqual(len(current["attempts"]), 1)
                attempt = await self.pool.fetchrow('select * from gateway_cost_attempts where attempt_id=$1', current["attempts"][0])
                self.assertIsNotNone(attempt["observed_at"])
                # Mock usage has no provider charge; the strict profile uses the
                # model's actual usage fields, not LiteLLM's synthetic cost map.
                await callback.async_post_call_success_hook(data, user, response)
                result = await self.ledger.get(current["accounting_id"])
                self.assertIn(result["cost_status"], {"priced", "unresolved"})
        finally:
            litellm.callbacks = old_callbacks
            integration.state.reset(token)
            if current.get("accounting_id"):
                await self.pool.execute('delete from gateway_cost_cache_outbox where attempt_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)', current["accounting_id"])
                await self.pool.execute('delete from gateway_cost_attempts where accounting_id=$1', current["accounting_id"])
                await self.pool.execute('delete from gateway_cost_requests where accounting_id=$1', current["accounting_id"])

    async def test_unpriced_admission_does_not_create_provider_attempt(self):
        import gateway_accounting as integration
        from litellm.proxy._types import UserAPIKeyAuth
        from fastapi import HTTPException
        callback = integration.GatewayAccounting()
        with patch.object(integration, "registry", return_value=self.prices):
            with self.assertRaises(HTTPException) as error:
                await callback.async_pre_call_hook(UserAPIKeyAuth(api_key=self.key), None, {"model": "unverified-model"}, "completion")
        self.assertEqual(error.exception.status_code, 503)

    async def test_stream_terminal_usage_and_disconnect_are_distinct(self):
        import gateway_accounting as integration
        callback = integration.GatewayAccounting()
        current = {"accounting_id": self.request_id, "attempts": [self.attempt_id], "reservation": None}
        token = integration.state.set(current)
        async def stream():
            yield {"choices": [{"delta": {"content": "content is not retained"}}], "usage": {"cost_in_usd_ticks": 100}}
            yield {"choices": [], "usage": {"cost_in_usd_ticks": 1000000000}, "model": "test-model"}
        try:
            with patch.object(integration, "accounting", self.ledger):
                chunks = [item async for item in callback.async_post_call_streaming_iterator_hook(None, stream(), {})]
                self.assertEqual(len(chunks), 2)
                result = await self.ledger.get(self.request_id)
                self.assertEqual(result["cost_usd"], "0.1")
                self.assertTrue(result["billing_eligible"])
        finally:
            integration.state.reset(token)

    async def test_stream_without_final_usage_stays_unresolved(self):
        import gateway_accounting as integration
        callback = integration.GatewayAccounting()
        current = {"accounting_id": self.request_id, "attempts": [self.attempt_id], "reservation": None}
        token = integration.state.set(current)
        async def stream():
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise ConnectionError("provider disconnected")
        try:
            with patch.object(integration, "accounting", self.ledger):
                with self.assertRaises(ConnectionError):
                    _ = [item async for item in callback.async_post_call_streaming_iterator_hook(None, stream(), {})]
                result = await self.ledger.get(self.request_id)
                self.assertEqual(result["cost_status"], "unresolved")
                self.assertIsNone(result["cost_usd"])
        finally:
            integration.state.reset(token)

    async def test_real_litellm_stream_wrapper_preserves_final_account_charge(self):
        import httpx
        import json
        import litellm
        import gateway_accounting as integration
        integration.install_stream_evidence_hooks()
        callback = integration.GatewayAccounting()
        await self.pool.execute('delete from gateway_cost_attempts where attempt_id=$1', self.attempt_id)
        current = {"accounting_id": self.request_id, "attempts": [], "attempt_map": {}, "reservation": None,
                   "profile": self.prices.profiles["test-v1"], "alias": "test-model", "options": {}}
        token = integration.state.set(current)
        seen = []
        async def send(client, request, **kwargs):
            seen.append(json.loads(await request.aread()))
            base = {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1, "model": "test-model"}
            chunks = [
                {**base, "choices": [{"index": 0, "delta": {"content": "fixture"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {**base, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                    "total_tokens": 2, "cost_in_usd_ticks": 1000000000}},
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode(), request=request)
        try:
            with patch.object(integration, "accounting", self.ledger), patch.object(integration, "registry", return_value=self.prices), \
                 patch.object(litellm, "callbacks", [callback]), patch("httpx.AsyncClient.send", new=send), \
                 patch.object(integration, "finalize_reservation", new=AsyncMock()):
                stream = await litellm.acompletion(model="xai/test-model", messages=[{"role":"user","content":"fixture"}],
                    api_key="offline-test", stream=True, stream_options={"include_usage":True}, num_retries=0, max_retries=0)
                _ = [chunk async for chunk in callback.async_post_call_streaming_iterator_hook(None, stream, {})]
                self.assertEqual(len(seen), 1)
                self.assertTrue(seen[0]["stream_options"]["include_usage"])
                self.assertEqual(stream._gateway_terminal_evidence["usage"]["cost_in_usd_ticks"], "1000000000")
                result = await self.ledger.get(self.request_id)
                self.assertEqual(result["cost_status"], "priced", result)
                self.assertEqual(result["cost_usd"], "0.1")
        finally:
            integration.state.reset(token)

    async def test_http_cost_headers_and_owner_scoped_lookup(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        import gateway_accounting as integration
        from litellm.proxy._types import UserAPIKeyAuth
        user = UserAPIKeyAuth(api_key=self.key, user_id=self.uid, team_id=self.uid)
        app = FastAPI()
        app.include_router(integration.router)
        app.add_middleware(integration.AccountingMiddleware)
        app.dependency_overrides[integration.user_api_key_auth] = lambda: user

        @app.get("/fixture-response")
        async def fixture():
            integration.state.get()["accounting_id"] = self.request_id
            return JSONResponse({"fixture": True}, headers={"x-litellm-response-cost": "0"})

        with patch.object(integration, "accounting", self.ledger):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
                pending = await client.get("/fixture-response")
                self.assertEqual(pending.headers["x-gateway-accounting-id"], self.request_id)
                self.assertEqual(pending.headers["x-gateway-cost-status"], "pending")
                self.assertNotIn("x-litellm-response-cost", pending.headers)
                await self.ledger.observe(self.attempt_id, raw_usage={"cost_in_usd_ticks":1000000000})
                await self.ledger.finish(self.request_id)
                priced = await client.get("/fixture-response")
                self.assertEqual(priced.headers["x-litellm-response-cost"], "0.1")
                cost = await client.get("/v1/costs/"+self.request_id)
                self.assertEqual(cost.status_code,200)
                self.assertTrue(cost.json()["billing_eligible"])
                user.api_key = "another-owner"
                self.assertEqual((await client.get("/v1/costs/"+self.request_id)).status_code,404)
                user.user_role = "internal_user"
                self.assertEqual((await client.get("/v1/costs/reconciliation/report")).status_code,403)

    async def test_cached_native_response_has_an_explicit_zero_receipt(self):
        from litellm.types.utils import ModelResponse
        import gateway_accounting as integration
        profile = dict(self.prices.profiles["test-v1"], zero_reasons=["gateway_response_cache"])
        current = {"accounting_id":self.request_id, "attempts":[], "profile":profile, "reservation":None}
        token = integration.state.set(current)
        response = ModelResponse(model="test-model",choices=[])
        response._hidden_params["cache_hit"] = True
        try:
            with patch.object(integration,"accounting",self.ledger):
                await integration.GatewayAccounting().async_post_call_success_hook({},None,response)
            attempt = await self.pool.fetchrow("select * from gateway_cost_attempts where attempt_id=$1",current["attempts"][0])
            self.assertEqual(attempt["cost_status"],"priced")
            self.assertEqual(attempt["cost_usd"],0)
            self.assertEqual(attempt["zero_reason"],"gateway_response_cache")
        finally:
            integration.state.reset(token)

    async def test_charged_wrapped_provider_failure_keeps_cost(self):
        import httpx
        import gateway_accounting as integration
        from custom_handler_seedance import SeedanceException
        response = httpx.Response(500, request=httpx.Request('POST','https://fixture.invalid'),
                                  json={'usage': {'cost_in_usd_ticks': 7100000000}, 'model': 'test-model'})
        error = SeedanceException('provider rejected output')
        error.__cause__ = httpx.HTTPStatusError('provider failed', request=response.request, response=response)
        current = {'accounting_id': self.request_id, 'attempts': [self.attempt_id], 'profile': self.prices.profiles['test-v1']}
        token = integration.state.set(current)
        try:
            with patch.object(integration, 'accounting', self.ledger), patch.object(integration, 'finalize_reservation', new=AsyncMock()):
                await integration.GatewayAccounting().async_post_call_failure_hook({}, error, None)
            self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id), .71)
        finally:
            integration.state.reset(token)

    async def test_unmapped_execution_is_recovered_once(self):
        import gateway_accounting as integration
        current = {'accounting_id': self.request_id, 'attempts': [], 'profile': self.prices.profiles['test-v1'], 'alias': 'test-model'}
        token = integration.state.set(current)
        try:
            with patch.object(integration, 'accounting', self.ledger):
                for _ in range(2):
                    attempt = await integration.recover_unmapped({'litellm_call_id': self.uid},
                        {'id': 'provider-fixture', 'model': 'test-model', 'usage': {'cost_in_usd_ticks': 7100000000}})
            self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', attempt), .71)
            self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .71)
        finally:
            integration.state.reset(token)

    async def test_sdk_boundary_logs_even_when_optional_callbacks_are_disabled(self):
        import gateway_accounting as integration
        import litellm
        from litellm.types.utils import ModelResponse
        current = {'accounting_id': self.request_id, 'attempts': [], 'profile': self.prices.profiles['test-v1'], 'alias': 'test-model'}
        token = integration.state.set(current)
        original = litellm.acompletion
        # Simulate an SDK route that bypasses optional logging callbacks but
        # submitted using its mandatory durable intent.
        async def provider(*args, **kwargs):
            current['attempts'].append(self.attempt_id)
            result = ModelResponse(model='test-model', choices=[])
            result._hidden_params['gateway_usage'] = {'cost_in_usd_ticks': 7100000000}
            return result
        from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
        had = getattr(_ProxyDBLogger, '_gateway_writer', False)
        try:
            with patch.object(integration, 'accounting', self.ledger), patch.object(litellm, 'acompletion', new=provider), \
                 patch.object(_ProxyDBLogger, '_gateway_writer', False, create=True), \
                 patch.object(litellm, 'callbacks', []):
                integration.install()
                litellm.callbacks = []
                await litellm.acompletion(model='test-model', disable_logging=True)
            self.assertEqual(await self.pool.fetchval('select spend from "LiteLLM_SpendLogs" where request_id=$1', self.attempt_id), .71)
        finally:
            litellm.acompletion = original
            _ProxyDBLogger._gateway_writer = had
            integration.state.reset(token)
