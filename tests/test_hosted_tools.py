"""Hosted calls price measured output, never declarations or resource guesses."""
import copy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from hosted_tools import calculate, measured_tools, pin_contract, validate_resources
from pricing_registry import PricingError

CONTRACT = {"version": "fixture-hosted", "web_search_call_usd": ".01", "file_search_call_usd": ".0025",
            "batch_multiplier": ".5", "batch_models": ["gpt-6-astra"]}


class HostedToolTests(unittest.TestCase):
    def test_pinned_sdk_image_tool_output_does_not_add_image_charge(self):
        from pricing_registry import PricingRegistry
        from litellm_pricing import cost_using_response
        profile = PricingRegistry().select("gpt-6-astra", "responses")
        body = {"id":"resp_fixture","object":"response","created_at":1,"status":"completed","model":"gpt-6-astra",
                "parallel_tool_calls":False,"tool_choice":"auto","tools":[],"output":[],
                "usage":{"input_tokens":100,"output_tokens":50,"total_tokens":150,
                         "input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0}}}
        parent = cost_using_response(profile, body)
        body["output"] = [{"id":"ig_fixture","type":"image_generation_call","status":"completed","result":"YQ=="}]
        self.assertEqual(cost_using_response(profile, body), parent)
        self.assertIsNotNone(parent)

    def test_official_evidence_shape(self):
        import json
        from pathlib import Path
        from scripts.validate_price_evidence import validate
        validate(json.loads((Path(__file__).parents[1]/"pricing/evidence/openai-hosted-tools-2026-09-24.json").read_text()))

    def test_search_actions_and_file_calls_not_declarations_are_billable(self):
        result = {"output": [{"type": "web_search_call", "status": "completed", "action": {"type": action}}
                              for action in ("search", "open_page", "find_in_page", "search")]
                            + [{"type": "file_search_call", "status": "completed"}]}
        usage = measured_tools(result)
        usage["server_side_tool_usage_details"] = {"web_search_calls":2}
        self.assertEqual(usage["gateway_hosted_counts"], {"web_search_calls": 2, "file_search_calls": 1})
        def base(profile, raw, served_options=None):
            self.assertNotIn("server_side_tool_usage_details",raw)
            return Decimal(".0001"), []
        amount, breakdown = calculate({"hosted_contract": CONTRACT}, usage, base)
        self.assertEqual(amount, Decimal(".0226"))
        self.assertEqual(len(breakdown), 2)

    def test_container_images_missing_terminal_usage_are_unknown_not_free(self):
        for response in ({}, {"output": [{"type": "code_interpreter_call", "container_id": "cntr_1"}]},
                         {"output": [{"type": "image_generation_call", "result": "base64-bytes"}]},
                         {"output": [{"type": "web_search_call", "status": "failed"}]}):
            with self.subTest(response=response), self.assertRaises(PricingError):
                calculate({"hosted_contract": CONTRACT}, measured_tools(response), lambda *args, **kwargs: (Decimal(1), []))

    def test_unused_declared_tools_cost_no_calls_and_contract_is_pinned(self):
        document = {"hosted_tools": copy.deepcopy(CONTRACT)}
        source = {"vendor": "openai"}
        profile = pin_contract(source, {"tools": [{"type": "web_search"}, {"type": "function"}]}, document)
        document["hosted_tools"]["web_search_call_usd"] = "99"
        self.assertEqual(profile["hosted_contract"]["web_search_call_usd"], ".01")
        self.assertNotIn("hosted_contract", source)
        amount, _ = calculate(profile, measured_tools({"output": []}), lambda *args, **kwargs: (Decimal(".1"), []))
        self.assertEqual(amount, Decimal(".1"))
        self.assertIs(pin_contract(source,{"tools":None},document),source)


class HostedOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_documented_reference_is_checked_before_execution(self):
        user = SimpleNamespace(metadata={"gateway_app_id": "app", "gateway_resource_delegate": True})
        body = {"proxy_server_request": {"headers": {"x-gateway-resource-owner": "alice"}},
                "previous_response_id": "resp_owned", "input": [{"content": [{"type": "input_file", "file_id": "file_owned"}]}],
                "tools": [{"type": "file_search", "vector_store_ids": ["vs_owned"]},
                          {"type": "code_interpreter", "container": {"type": "auto", "file_ids": ["file_owned"]}}]}
        with patch("owned_openai_resources.owned", new=AsyncMock()) as owned, patch("openai_owner_lifecycle.ensure_active", new=AsyncMock()):
            self.assertEqual(await validate_resources(user, body), ("app", "alice"))
            self.assertEqual(body["cache"], {"no-cache":True,"no-store":True})
            self.assertEqual({call.args[:2] for call in owned.call_args_list},
                             {("response", "resp_owned"), ("file", "file_owned"), ("vector_store", "vs_owned")})
        with patch("owned_openai_resources.owned", new=AsyncMock(side_effect=HTTPException(404))):
            with self.assertRaises(HTTPException):
                await validate_resources(user, body)

    async def test_body_metadata_cannot_grant_resource_delegation(self):
        with self.assertRaises(HTTPException):
            await validate_resources(SimpleNamespace(metadata={}), {"metadata": {"gateway_resource_delegate": True},
                "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]})

    async def test_extra_body_references_cannot_bypass_owner_check(self):
        user=SimpleNamespace(metadata={"gateway_app_id":"app","gateway_resource_delegate":True})
        with patch("owned_openai_resources.owned",new=AsyncMock(side_effect=HTTPException(404))) as owned:
            with self.assertRaises(HTTPException):
                await validate_resources(user,{"proxy_server_request":{"headers":{"x-gateway-resource-owner":"alice"}},
                    "extra_body":{"tools":[{"type":"file_search","vector_store_ids":["vs_foreign"]}]}})
            owned.assert_awaited_once_with("vector_store","vs_foreign",("app","alice"))


if __name__ == "__main__":
    unittest.main()
