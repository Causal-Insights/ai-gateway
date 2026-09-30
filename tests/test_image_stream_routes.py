"""Hermetic Images SSE ASGI contracts; no provider calls or database required."""
import asyncio
import importlib
import json
import os
import unittest
from contextlib import ExitStack
from email.parser import BytesParser
from email.policy import default
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from litellm.proxy._types import UserAPIKeyAuth
import image_stream_routes as routes
import gateway_accounting as integration
from gateway_request_policy import GatewayRequestPolicyMiddleware

RealClient = httpx.AsyncClient
RealCapture = integration.capture
USAGE = {"input_tokens": 5, "input_tokens_details": {"text_tokens": 3, "image_tokens": 2},
         "output_tokens": 10, "total_tokens": 15}


def event(kind="completed", **fields):
    return {"type": "image_generation." + kind, "b64_json": "fixture", **fields}


def sse(*events, trailing=True):
    text = "\n\n".join("data: " + json.dumps(item) for item in events)
    return (text + ("\n\n" if trailing else "")).encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, body, error=None):
        self.body, self.error, self.closed = body, error, False

    async def __aiter__(self):
        yield self.body
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


class ImageStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, OPENAI_API_KEY="offline-provider"))
        self.sent = []
        self.stream = Stream(sse(event(usage=USAGE)))
        self.status = 200
        self.clients = []

        async def provider(request):
            self.sent.append((request, await request.aread()))
            return httpx.Response(self.status, stream=self.stream, headers={"x-request-id": "req-fixture"})

        def client(**kwargs):
            instance = RealClient(transport=httpx.MockTransport(provider), **kwargs)
            self.clients.append(instance)
            return instance

        self.stack.enter_context(patch.object(routes, "httpx", SimpleNamespace(AsyncClient=client, Timeout=httpx.Timeout)))
        self.ledger = MagicMock()
        self.ledger.begin = AsyncMock(return_value="request-fixture")
        self.ledger.attempt = AsyncMock(return_value="attempt-fixture")
        self.ledger.check_budgets = AsyncMock()
        self.ledger.finish = AsyncMock()
        self.ledger.get = AsyncMock(return_value={"cost_status": "unresolved", "cost_usd": None})
        self.stack.enter_context(patch.object(routes, "accounting", self.ledger))
        self.stack.enter_context(patch.object(integration, "accounting", self.ledger))
        self.capture = self.stack.enter_context(patch.object(routes, "capture", new=AsyncMock()))
        self.release = self.stack.enter_context(patch.object(routes, "finalize_reservation", new=AsyncMock()))
        # Exercise the real accounting admission hook with deterministic price/storage boundaries.
        prices = MagicMock()
        prices.select.side_effect = lambda alias, route, options: {"upstream_model": "openai/" + alias}
        self.stack.enter_context(patch.object(integration, "registry", return_value=prices))
        self.stack.enter_context(patch.object(integration, "check_context"))
        self.user = UserAPIKeyAuth(api_key="sk-fixture", token="fixture", models=["gpt-image-1.5", "gpt-image-2", "gpt-image-2.5-flare"])
        async def authenticated():
            return self.user
        routes.stream_app.dependency_overrides[routes.user_api_key_auth] = authenticated
        self.addCleanup(routes.stream_app.dependency_overrides.clear)
        self.app = integration.AccountingMiddleware(GatewayRequestPolicyMiddleware(FastAPI()))
        self.http = RealClient(transport=httpx.ASGITransport(app=self.app), base_url="http://fixture")
        self.addAsyncCleanup(self.http.aclose)

    async def post(self, **body):
        return await self.http.post("/v1/images/generations", json={"model": "gpt-image-1.5", "prompt": "fixture", "stream": True, **body}, headers={"Authorization": "Bearer sk-fixture"})

    async def test_final_event_without_blank_line_and_partials_do_not_bill(self):
        self.stream = Stream(sse(event("partial_image", usage={"output_tokens": 999}), event(usage=USAGE), trailing=False))
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("partial_image", response.text)
        self.assertTrue(response.text.endswith("\n\n"))
        self.capture.assert_awaited_once_with("attempt-fixture", {"usage": USAGE, "model": "gpt-image-1.5", "id": "req-fixture"}, outcome="success")
        self.assertTrue(self.stream.closed)
        self.assertTrue(all(c.is_closed for c in self.clients))
        self.release.assert_awaited_once()

    async def test_exact_models_and_controls_survive_policy_and_admission(self):
        for model in ("gpt-image-1.5", "gpt-image-2", "gpt-image-2.5-flare"):
            with self.subTest(model=model):
                self.stream = Stream(sse(event(usage=USAGE)))
                response = await self.post(model=model, quality="low", output_format="webp", output_compression=23, partial_images=0, background="opaque")
                self.assertEqual(response.status_code, 200, response.text)
                payload = json.loads(self.sent[-1][1])
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["output_compression"], 23)
                self.assertEqual(payload["partial_images"], 0)
                self.assertEqual(payload["background"], "opaque")
                self.assertTrue(payload["stream"])
        self.assertEqual(len(self.sent), 3)

    async def test_multipart_edit_preserves_order_bytes_mask_and_scalars(self):
        response = await self.http.post("/v1/images/edits", data={"model": "gpt-image-1.5", "prompt": "fixture", "stream": "true", "input_fidelity": "high", "partial_images": "0"}, files=[("image[]", ("one.png", b"\x00one\xff", "image/png")), ("image[]", ("two.png", b"two\x00", "image/png")), ("mask", ("mask.png", b"mask\xff", "image/png"))])
        self.assertEqual(response.status_code, 200, response.text)
        request, body = self.sent[0]
        message = BytesParser(policy=default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + body)
        parts = list(message.iter_parts())
        files = [(p.get_param("name", header="content-disposition"), p.get_filename(), p.get_payload(decode=True)) for p in parts if p.get_filename()]
        self.assertEqual(files, [("image[]", "one.png", b"\x00one\xff"), ("image[]", "two.png", b"two\x00"), ("mask", "mask.png", b"mask\xff")])
        fields = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in parts if not p.get_filename()}
        self.assertEqual(fields["input_fidelity"], b"high")
        self.assertEqual(fields["partial_images"], b"0")

    async def test_multiple_outputs_capture_terminal_usage_once(self):
        first, second = event(usage=USAGE), event(usage=USAGE, b64_json="second")
        self.stream = Stream(sse(first, second))
        response = await self.post(n=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("image_generation.completed"), 2)
        self.capture.assert_awaited_once()
        self.assertEqual(self.capture.call_args.args[1]["usage"]["output_tokens"], 20)
        self.assertEqual(self.capture.call_args.args[1]["usage"]["input_tokens_details"]["image_tokens"], 4)

    async def test_unmetered_completed_image_keeps_subtotal_unresolved(self):
        self.stream = Stream(sse(event(usage=USAGE), event(b64_json="unmetered")))
        response = await self.post(n=2)
        self.assertEqual(response.status_code, 200)
        self.assertIn("unmetered", response.text)
        self.capture.assert_awaited_once()
        self.assertEqual(self.capture.call_args.args[1]["usage"], {"gateway_image_partial_usage": USAGE})

    async def test_partial_only_remains_unknown(self):
        self.stream = Stream(sse(event("partial_image", usage=USAGE)))
        self.assertEqual((await self.post()).status_code, 200)
        self.capture.assert_awaited_once_with("attempt-fixture", {}, outcome="incomplete")
        self.release.assert_awaited_once()

    async def test_missing_credentials_release_admission_without_attempt(self):
        self.stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        response = await self.post()
        self.assertEqual(response.status_code, 503)
        self.ledger.attempt.assert_not_awaited()
        self.release.assert_awaited_once()
        self.assertEqual(self.sent, [])

    async def test_budget_admission_failure_never_submits(self):
        from pricing_registry import PricingError
        self.ledger.check_budgets.side_effect = PricingError("fixture budget exhausted")
        response = await self.post()
        self.assertEqual(response.status_code, 503, response.text)
        self.ledger.attempt.assert_not_awaited()
        self.release.assert_awaited_once()
        self.assertEqual(self.sent, [])

    async def test_provider_error_closes_once_and_never_retries(self):
        self.status = 429
        self.stream = Stream(b'{"error":{"message":"fixture rate limit"}}')
        response = await self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.sent), 1)
        self.capture.assert_awaited_once()
        self.assertEqual(self.capture.call_args.kwargs["outcome"], "failure")
        self.assertTrue(self.stream.closed)
        self.release.assert_awaited_once()

    async def test_client_disconnect_releases_and_closes_unresolved_attempt(self):
        waiting = asyncio.Event()
        class SlowStream(Stream):
            async def __aiter__(self):
                yield sse(event("partial_image"))
                await waiting.wait()
        self.stream = SlowStream(b"")
        disconnected = asyncio.Event()
        raw = json.dumps({"model": "gpt-image-1.5", "prompt": "fixture", "stream": True}).encode()
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and b"partial_image" in message.get("body", b""):
                disconnected.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "method": "POST",
                 "path": "/v1/images/generations", "raw_path": b"/v1/images/generations", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")], "scheme": "http", "server": ("fixture", 80), "client": ("fixture", 1)}
        await asyncio.wait_for(self.app(scope, receive, send), 3)
        self.capture.assert_awaited_once_with("attempt-fixture", {}, outcome="incomplete")
        self.release.assert_awaited_once()
        self.assertTrue(self.stream.closed)
        self.assertTrue(all(client.is_closed for client in self.clients))
        self.assertEqual(len(self.sent), 1)

    async def test_provider_disconnect_does_not_retry_or_price_partial(self):
        self.stream = Stream(sse(event("partial_image")), httpx.ReadError("fixture disconnected"))
        with self.assertRaises((httpx.ReadError, ExceptionGroup)):
            await self.post()
        self.capture.assert_awaited_once_with("attempt-fixture", {}, outcome="incomplete")
        self.release.assert_awaited_once()
        self.assertTrue(self.stream.closed)
        self.assertEqual(len(self.sent), 1)

    def native_auth(self):
        routes.stream_app.dependency_overrides.clear()
        native = importlib.import_module("litellm.proxy.auth.user_api_key_auth")
        from litellm.proxy import proxy_server as proxy
        self.stack.enter_context(patch.object(proxy, "master_key", "sk-offline-master"))
        self.stack.enter_context(patch.object(proxy, "prisma_client", MagicMock()))
        self.stack.enter_context(patch.object(native.IdentityStore, "resolve", new=AsyncMock(return_value=object())))
        self.stack.enter_context(patch.object(native.IdentityStore, "key_from_principal", return_value=self.user))

    async def test_native_auth_rejects_model_key_before_provider(self):
        self.native_auth()
        self.user.models = ["other-model"]
        response = await self.post()
        self.assertIn(response.status_code, (401, 403), response.text)
        self.assertIn("model", response.text.lower())
        self.ledger.attempt.assert_not_awaited()
        self.assertEqual(self.sent, [])

    async def test_native_auth_rejects_exhausted_virtual_key_budget(self):
        self.native_auth()
        from litellm.proxy import proxy_server as proxy
        self.user.max_budget = 1
        self.user.spend = 2
        self.stack.enter_context(patch.object(proxy, "get_current_spend", new=AsyncMock(return_value=2)))
        response = await self.post()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("budget", response.text.lower())
        self.ledger.attempt.assert_not_awaited()
        self.assertEqual(self.sent, [])

    @unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
    async def test_actual_settlement_once_and_partial_null_on_isolated_database(self):
        from decimal import Decimal
        from uuid import uuid4
        from cost_accounting import CostAccounting, canonical_key
        from generation_job_repository import GenerationJobRepository
        from pricing_registry import PricingRegistry
        repository = GenerationJobRepository()
        prices = PricingRegistry()
        ledger = CostAccounting(repository, prices)
        pool = await repository.pool()
        key = canonical_key("sk-image-stream-fixture-" + uuid4().hex)
        self.user.api_key = key
        self.user.token = key
        await pool.execute('insert into "LiteLLM_VerificationToken" (token,models) values($1,$2)', key, [])
        request_ids = []
        try:
            with patch.object(routes, "accounting", ledger), patch.object(integration, "accounting", ledger), \
                 patch.object(integration, "registry", return_value=prices), patch.object(routes, "capture", new=RealCapture):
                final = event(usage=USAGE)
                self.stream = Stream(sse(event("partial_image"), final))
                response = await self.post()
                self.assertEqual(response.status_code, 200, response.text)
                request_id = response.headers["x-gateway-accounting-id"]
                request_ids.append(request_id)
                cost = await ledger.get(request_id)
                self.assertEqual(cost["cost_status"], "priced", cost)
                self.assertAlmostEqual(Decimal(cost["cost_usd"]), Decimal("0.000351"), places=15)
                self.assertEqual(await pool.fetchval('select count(*) from gateway_cost_attempts where accounting_id=$1', request_id), 1)
                self.assertEqual(await pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', key), 1)
                self.assertAlmostEqual(await pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', key), .000351)
                self.stream = Stream(sse(event("partial_image")))
                response = await self.post()
                request_id = response.headers["x-gateway-accounting-id"]
                request_ids.append(request_id)
                cost = await ledger.get(request_id)
                self.assertEqual(cost["cost_status"], "unresolved")
                self.assertIsNone(cost["cost_usd"])
                self.assertAlmostEqual(await pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', key), .000351)
        finally:
            await pool.execute('delete from "LiteLLM_SpendLogs" where api_key=$1', key)
            for table in ("DailyUserSpend", "DailyTeamSpend"):
                await pool.execute(f'delete from "LiteLLM_{table}" where api_key=$1', key)
            for request_id in request_ids:
                await pool.execute('delete from gateway_cost_cache_outbox where attempt_id in (select attempt_id from gateway_cost_attempts where accounting_id=$1)', request_id)
                await pool.execute('delete from gateway_cost_attempts where accounting_id=$1', request_id)
                await pool.execute('delete from gateway_cost_requests where accounting_id=$1', request_id)
            await pool.execute('delete from "LiteLLM_VerificationToken" where token=$1', key)
            await repository.close()


if __name__ == "__main__":
    unittest.main()
