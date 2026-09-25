import copy
import json
import os
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import grounded_pricing as grounding
from pricing_registry import PricingError, PricingRegistry
from litellm_pricing import calculate, cost_using_response


class GroundedPricing(unittest.TestCase):
    def setUp(self):
        self.registry = PricingRegistry()

    def profile(self, alias="grok-4.20", tools=None):
        return grounding.pin_contract(self.registry.select(alias, "responses" if alias.startswith("grok") else "completion"),
                                      {"tools": tools or [{"type": "web_search"}, {"type": "x_search"}]})

    def test_official_tariff_evidence_and_historical_profiles_retained(self):
        from scripts.validate_price_evidence import validate
        for vendor in ("google", "xai"):
            validate(json.loads((Path(__file__).parents[1]/f"pricing/evidence/{vendor}-grounding-2026-09-24.json").read_text()))
        profile = self.profile()
        original = self.registry.profiles[profile["version"].removesuffix("-grounding-20260924")]
        self.assertNotIn("grounding_tariff", original)
        self.assertEqual(profile["model_info"], original["model_info"])

    def test_plain_and_function_requests_keep_original_calculation_path(self):
        profile = self.registry.select("grok-4.20", "responses")
        for tools in ([], [{"type": "function", "name": "local"}]):
            self.assertIs(grounding.pin_contract(profile, {"tools": tools}), profile)
        excluded = self.registry.select("nano-banana-2-lite", "completion")
        self.assertIs(grounding.pin_contract(excluded, {"tools": [{"google_search": {}}]}), excluded)

    def test_x_posts_and_profiles_price_per_fetch_without_old_call_charge(self):
        profile = self.profile()
        response = {"usage": {"server_side_tool_usage_details": {"web_search_calls": 2, "x_search_calls": 99,
                     "x_posts_fetched": 44, "x_users_fetched": 3}}}
        raw = {**grounding.measured(profile, response), "gateway_grounding_parent_cost_usd": "0.001"}
        amount, lines = calculate(profile, raw)
        self.assertEqual(amount, Decimal("0.261"))
        self.assertNotIn("x_search_calls", {line["component"] for line in lines})
        self.assertEqual(grounding.measured(profile, {"type": "response.incomplete", "response": response}), grounding.measured(profile, response))

    def test_missing_or_fractional_counter_is_unresolved_not_zero(self):
        profile = self.profile()
        for counts in ({"web_search_calls": 1, "x_search_calls": 3}, {"web_search_calls": 0, "x_posts_fetched": 1.5, "x_users_fetched": 0}):
            with self.assertRaises(PricingError):
                calculate(profile, {"gateway_grounding_counts": counts, "gateway_grounding_parent_cost_usd": ".001"})

    def test_gemini_sdk_query_counters_are_estimates_not_authoritative_totals(self):
        profile = self.profile("gemini-3.8-flash", [{"google_search": {}}])
        for count in (0, 3):
            raw = grounding.measured(profile, {"usage": {"prompt_tokens_details": {"web_search_requests": count}}})
            raw["gateway_grounding_parent_cost_usd"] = ".001"
            self.assertEqual(raw["gateway_grounding_counts"]["google_search_queries"], count)
            self.assertEqual(raw["gateway_grounding_usage_source"], "sdk_query_count_estimate")
            with self.assertRaisesRegex(PricingError, "estimate"):
                calculate(profile, raw)
        with self.assertRaises(PricingError):
            calculate(profile, {"gateway_grounding_parent_cost_usd": ".001"})

    def test_actual_sdk_counter_is_not_charged_twice_and_parent_is_retained(self):
        from litellm import ModelResponse, Usage
        profile = self.profile("gemini-3.8-flash", [{"google_search": {}}])
        response = ModelResponse(model="gemini-3.8-flash", choices=[], usage=Usage(prompt_tokens=100, completion_tokens=10,
            total_tokens=110, prompt_tokens_details={"web_search_requests": 3}))
        raw = {**response.usage.model_dump(exclude_none=True), **grounding.measured(profile, response)}
        parent = grounding.parent_subtotal(profile, response)
        self.assertIsNotNone(parent)
        raw["gateway_grounding_parent_cost_usd"] = parent
        with self.assertRaisesRegex(PricingError, "estimate"):
            calculate(profile, raw)
        self.assertIsNone(cost_using_response(profile, response))
        self.assertEqual(response.usage.prompt_tokens_details.web_search_requests, 3)

    def test_actual_sdk_deduplicates_queries_within_and_across_candidates(self):
        from litellm.llms.vertex_ai.gemini.grounding_requests import calculate_grounding_requests
        measured = calculate_grounding_requests([
            {"webSearchQueries": ["query a", "query a", "query b", ""]},
            {"webSearchQueries": ["query a"]},
        ])
        self.assertEqual(measured.web_search_requests, 2)
        self.assertIsNone(calculate_grounding_requests([]).web_search_requests)

    def test_actual_sdk_preserves_x_counter_extras(self):
        from litellm.llms.xai.cost_calculator import apply_server_side_tool_usage_details_to_usage
        from litellm import Usage
        usage = Usage(prompt_tokens=10, completion_tokens=5)
        details = {"web_search_calls": 2, "x_posts_fetched": 44, "x_users_fetched": 3}
        apply_server_side_tool_usage_details_to_usage(usage, details)
        result = grounding.measured(self.profile(), {"usage": usage.model_dump(exclude_none=True)})
        self.assertEqual(result["gateway_grounding_counts"], details)


class GroundingLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_google_model_response_preserves_output_and_marks_estimate(self):
        from litellm import ModelResponse, Usage
        import gateway_accounting as gateway
        profile = grounding.pin_contract(PricingRegistry().select("gemini-3.8-flash", "completion"), {"tools": [{"google_search": {}}]})
        response = ModelResponse(model="gemini-3.8-flash", usage=Usage(prompt_tokens=10, completion_tokens=5,
            prompt_tokens_details={"web_search_requests": 2}))
        token = gateway.state.set({"profile": profile})
        try:
            with patch.object(gateway.accounting, "observe", new=AsyncMock()) as observe:
                await gateway.capture("attempt_fixture", response)
            self.assertEqual(response.gateway_grounding_usage, {"google_search_queries": 2})
            self.assertEqual(response.gateway_grounding_usage_source, "sdk_query_count_estimate")
            self.assertIn("gateway_grounding_parent_cost_usd", observe.await_args.kwargs["raw_usage"])
        finally:
            gateway.state.reset(token)

    async def test_callback_exposes_only_numeric_counts_and_preserves_output(self):
        import gateway_accounting as gateway
        profile = grounding.pin_contract(PricingRegistry().select("grok-4.20", "responses"), {"tools": [{"type": "x_search"}]})
        response = {"id": "resp_fixture", "model": "grok-4.20-non-reasoning-latest", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Saved answer", "annotations": [{"type": "url_citation", "url": "https://source.example"}]}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "server_side_tool_usage_details": {"x_posts_fetched": 44, "x_users_fetched": 3}}}
        output = copy.deepcopy(response["output"])
        token = gateway.state.set({"profile": profile, "route": "responses", "alias": "grok-4.20"})
        try:
            with patch.object(gateway.accounting, "observe", new=AsyncMock()) as observe:
                await gateway.capture("attempt_fixture", response)
            raw = observe.await_args.kwargs["raw_usage"]
            self.assertEqual(raw["gateway_grounding_counts"], {"x_posts_fetched": 44, "x_users_fetched": 3})
            self.assertNotIn("gateway_native_cost_usd", raw)
            self.assertEqual(response["gateway_grounding_usage"], {"x_posts_fetched": 44, "x_users_fetched": 3})
            self.assertEqual(response["output"], output)
        finally:
            gateway.state.reset(token)


@unittest.skipUnless(os.environ.get("RUN_COST_DB_TESTS") == "1", "requires isolated PostgreSQL")
class GroundingSettlement(unittest.IsolatedAsyncioTestCase):
    from test_cost_accounting_db import AccountingDatabaseTests as Fixtures
    asyncSetUp = Fixtures.asyncSetUp
    asyncTearDown = Fixtures.asyncTearDown

    async def pin(self, alias, tools):
        from cost_accounting import encode
        profile = grounding.pin_contract(PricingRegistry().select(alias, "responses" if alias.startswith("grok") else "completion"), {"tools": tools})
        await self.pool.execute("update gateway_cost_attempts set profile=$2::jsonb where attempt_id=$1", self.attempt_id, encode(profile))

    async def test_x_provider_counters_settle_once(self):
        await self.pin("grok-4.20", [{"type": "x_search"}])
        raw = {"gateway_grounding_counts": {"x_posts_fetched": 44, "x_users_fetched": 3},
               "gateway_grounding_usage_source": "provider_usage_counts", "gateway_grounding_parent_cost_usd": ".001"}
        for _ in range(3):
            await self.ledger.observe(self.attempt_id, raw_usage=raw)
        await self.ledger.finish(self.request_id)
        receipt = await self.ledger.get(self.request_id)
        self.assertEqual(receipt["cost_status"], "priced", receipt)
        self.assertEqual(Decimal(receipt["cost_usd"]), Decimal(".251"))
        self.assertEqual(await self.pool.fetchval('select count(*) from "LiteLLM_SpendLogs" where api_key=$1', self.key), 1)
        self.assertAlmostEqual(await self.pool.fetchval('select spend from "LiteLLM_VerificationToken" where token=$1', self.key), .251)

    async def test_google_estimate_keeps_total_unresolved_and_parent_evidence(self):
        await self.pin("gemini-3.8-flash", [{"google_search": {}}])
        raw = {"gateway_grounding_counts": {"google_search_queries": 3},
               "gateway_grounding_usage_source": "sdk_query_count_estimate", "gateway_grounding_parent_cost_usd": ".001"}
        await self.ledger.observe(self.attempt_id, raw_usage=raw)
        await self.ledger.finish(self.request_id)
        receipt = await self.ledger.get(self.request_id)
        self.assertEqual(receipt["cost_status"], "unresolved", receipt)
        self.assertIsNone(receipt["cost_usd"])
        stored = await self.pool.fetchval("select raw_usage->>'gateway_grounding_parent_cost_usd' from gateway_cost_attempts where attempt_id=$1", self.attempt_id)
        self.assertEqual(Decimal(stored), Decimal(".001"))


if __name__ == "__main__":
    unittest.main()
