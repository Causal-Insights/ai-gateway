"""Buffered provider SSE must settle through concrete-image success callbacks."""
import os
import unittest
from unittest.mock import AsyncMock, patch

import gateway_accounting as integration
from custom_handler_seedream import SeedreamLLM, parse_image_events
import test_cost_accounting_db as db_fixtures


class BufferedImageCallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_real_iterator_is_left_for_streaming_finalizer(self):
        callback = integration.GatewayAccounting()
        current = {"accounting_id": "fixture-request", "attempts": ["fixture-attempt"]}
        token = integration.state.set(current)
        async def events():
            yield {"type": "image_generation.partial_image", "b64_json": "fixture"}
        response = events()
        try:
            with patch.object(integration, "capture", new=AsyncMock()) as capture, \
                 patch.object(integration, "finalize_reservation", new=AsyncMock()) as release:
                self.assertIs(await callback.async_post_call_success_deployment_hook({"stream": True}, response, "image_generation"), response)
                self.assertIs(await callback.async_post_call_success_hook({"stream": True}, None, response), response)
                capture.assert_not_awaited()
                release.assert_not_awaited()
        finally:
            await response.aclose()
            integration.state.reset(token)


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
class BufferedImageSettlement(unittest.IsolatedAsyncioTestCase):
    # Reuse setup/cleanup without inheriting and rerunning the entire fixture suite.
    asyncSetUp = db_fixtures.AccountingDatabaseTests.asyncSetUp
    asyncTearDown = db_fixtures.AccountingDatabaseTests.asyncTearDown

    async def test_seedream_collected_sse_success_hooks_settle_once(self):
        callback = integration.GatewayAccounting()
        response = SeedreamLLM._image_response_from_body(parse_image_events(
            'data: {"type":"image_generation.partial_succeeded","image_index":0,"url":"https://example.test/result","size":"2048x2048"}\n\n'
            'data: {"type":"image_generation.completed","usage":{"cost_in_usd_ticks":350000000}}\n\n'))
        current = {"accounting_id": self.request_id, "attempts": [self.attempt_id],
                   "profile": self.prices.profiles["test-v1"], "reservation": None}
        token = integration.state.set(current)
        try:
            with patch.object(integration, "accounting", self.ledger), \
                 patch.object(integration, "finalize_reservation", new=AsyncMock()) as release:
                data = {"stream": True, "metadata": {"gateway_accounting_attempt_id": self.attempt_id}}
                self.assertIs(await callback.async_post_call_success_deployment_hook(data, response, "image_generation"), response)
                self.assertIs(await callback.async_post_call_success_hook(data, None, response), response)
                # Repeated native observation is idempotent; only one provider attempt exists.
                await callback.async_post_call_success_deployment_hook(data, response, "image_generation")
                cost = await self.ledger.get(self.request_id)
                self.assertEqual(cost["cost_status"], "priced", cost)
                self.assertEqual(cost["cost_usd"], "0.035")
                self.assertIsNotNone(await self.pool.fetchval('select finished_at from gateway_cost_requests where accounting_id=$1', self.request_id))
                self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', self.key), 1)
                self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .035)
                release.assert_awaited_once_with(current)
        finally:
            integration.state.reset(token)
