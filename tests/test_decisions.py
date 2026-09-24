"""Native typed decisions, auth, single dispatch, and cost capture without paid calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import httpx
from fastapi import FastAPI
from litellm.proxy._types import UserAPIKeyAuth

from accounting_usage import numbers
from decision_contract import DecisionError, MODEL, PROVIDER_URL, validate_request, validate_response
import decision_routes
import gateway_accounting
from pricing_registry import PricingError, PricingRegistry

ROOT = Path(__file__).resolve().parents[1]
HTTP_CLIENT = httpx.AsyncClient


def request_body():
    return {"model": MODEL, "state": {"text": "private request", "selected": ["node-1"]},
            "questions": {"target": {"type": "choice", "instructions": "Select the matching allowed tool.",
                                      "criteria": {"draw": "Image editor", "none": None}}}}


def response_body():
    return {"model": MODEL, "answers": {"target": {"type": "choice", "choice": "draw",
            "confidence": 0.98, "probabilities": {"draw": 0.99, "none": 0.01}}},
            "usage": {"input_tokens": 1000, "output_tokens": 20}}


def enabled_registry():
    document = json.loads((ROOT / "pricing/registry.json").read_text())
    document["models"][MODEL]["disabled"] = False
    for version in document["models"][MODEL]["profiles"]:
        document["profiles"][version]["enabled"] = True
    return PricingRegistry(document)


class DecisionContractTests(unittest.TestCase):
    def test_request_and_all_answer_types(self):
        payload, response = request_body(), response_body()
        payload["questions"].update(ready={"type": "noul", "instructions": {"question": "Ready?"},
            "criteria": {"true": "All inputs present", "false": ["missing input"]}},
            rank={"type": "score", "instructions": ["Rate readiness"], "criteria": ["missing", "ready"]})
        response["answers"].update(ready={"type": "noul", "noul": .8},
            rank={"type": "score", "score": .75, "legend": {"0": "missing", "1": "ready"},
                  "probabilities": {"0": .25, "1": .75}, "confidence": .5})
        self.assertEqual(validate_response(response, validate_request(payload)), response)

    def test_invalid_requests_fail_before_provider(self):
        for mutate in (
            lambda p: p.update(api_key="injected"), lambda p: p.update(model="jev-latest"),
            lambda p: p.update(state=True), lambda p: p.update(state={"value": float("nan")}),
            lambda p: p.update(questions={}),
            lambda p: p["questions"]["target"].update(criteria={str(i): None for i in range(256)}),
            lambda p: p["questions"]["target"].update(criteria={"one": 1}),
            lambda p: p["questions"]["target"].update(type="score", criteria=["single"]),
            lambda p: p["questions"]["target"].update(type="noul", criteria={"maybe": "unclear"}),
            lambda p: p["questions"]["target"].update(type="text"),
        ):
            payload = request_body()
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(DecisionError):
                validate_request(payload)

    def test_response_must_match_requested_candidates_and_pinned_model(self):
        for mutate in (
            lambda p: p.update(model="jev-2"), lambda p: p.update(answers={}),
            lambda p: p["answers"]["target"].update(choice="other"),
            lambda p: p["answers"]["target"].update(choice="none"),
            lambda p: p["answers"]["target"].update(type="noul"),
            lambda p: p["answers"]["target"].update(confidence=float("nan")),
            lambda p: p["answers"]["target"].update(confidence=True),
            lambda p: p["answers"]["target"].update(probabilities={"draw": .2, "none": .2}),
            lambda p: p["answers"]["target"].update(probabilities={"draw": 1}),
            lambda p: p["usage"].update(input_tokens=-1),
            lambda p: p["usage"].update(output_tokens=True),
        ):
            response = response_body()
            mutate(response)
            with self.subTest(response=response), self.assertRaises(DecisionError):
                validate_response(response, request_body())

    def test_rounded_native_distributions_remain_usable_without_rewriting_confidence(self):
        request, response = request_body(), response_body()
        criteria = {f"option_{index}": None for index in range(65)}
        probabilities = {key: 0 for key in criteria}
        probabilities.update(option_0=.81, option_1=.12, option_2=.03, option_3=.03)
        request["questions"]["target"]["criteria"] = criteria
        response["answers"]["target"].update(choice="option_0", probabilities=probabilities, confidence=.8)
        self.assertEqual(validate_response(response, request), response)
        for distribution in ({key: 0 for key in criteria}, {key: .04 for key in criteria}):
            response["answers"]["target"]["probabilities"] = distribution
            with self.assertRaises(DecisionError):
                validate_response(response, request)

    def test_score_consistency_accounts_for_native_rounding_but_rejects_wrong_score(self):
        request = {"model": MODEL, "state": "test", "questions": {"score": {
            "type": "score", "instructions": "Rate it", "criteria": ["low", "mid", "high"]}}}
        answer = {"type": "score", "score": 1.0, "confidence": 0, "legend": {"0": "low", "1": "mid", "2": "high"}, "probabilities": {"0": .33, "1": .33, "2": .33}}
        response = {"model": MODEL, "answers": {"score": answer}, "usage": {"input_tokens": 1, "output_tokens": 1}}
        self.assertEqual(validate_response(response, request), response)
        answer["score"] = 1.5
        with self.assertRaises(DecisionError):
            validate_response(response, request)

    def test_extra_provider_prose_is_not_returned(self):
        response = response_body()
        response["reasoning"] = "private text"
        response["answers"]["target"]["explanation"] = "private text"
        self.assertEqual(validate_response(response, request_body()), response_body())

    def test_published_price_is_disabled_until_activation_and_output_is_free(self):
        prices = PricingRegistry()
        with self.assertRaises(PricingError):
            prices.select(MODEL, "decisions")
        prices = enabled_registry()
        profile = prices.select(MODEL, "decisions")
        cost = prices.calculate(profile, {"input_tokens": "1000", "output_tokens": "20"}, served_model=MODEL)
        self.assertEqual(cost["cost_usd"], "0.000042")
        self.assertEqual(cost["breakdown"][1]["cost_usd"], "0")
        with self.assertRaises(PricingError):
            prices.select(MODEL, "completion")
        with self.assertRaises(PricingError):
            prices.calculate(profile, {}, served_model=MODEL)

    def test_decision_routes_are_classified_for_virtual_keys(self):
        from gateway_server import register_generation_job_llm_routes
        from litellm.proxy.auth.route_checks import RouteChecks
        register_generation_job_llm_routes()
        user = UserAPIKeyAuth(api_key="test", allowed_routes=["llm_api_routes"])
        for path in ("/v1/decisions", "/v1/decisions/models"):
            self.assertTrue(RouteChecks.is_llm_api_route(path))
            self.assertTrue(RouteChecks.is_virtual_key_allowed_to_call_route(path, user))


class DecisionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.calls = []
        self.result = response_body()
        self.status = 200
        self.transport_error = False
        self.transport_cancelled = False
        self.user = UserAPIKeyAuth(api_key="test-gateway-key", models=[MODEL])
        self.prices = enabled_registry()
        self.profile = self.prices.select(MODEL, "decisions")
        self.receipt = {"accounting_id": "cost_test", "cost_status": "pending", "cost_usd": None}

        async def begin(**kwargs):
            self.events.append(("begin", kwargs))
            return "cost_test"

        async def attempt(accounting_id, profile):
            self.events.append(("attempt", accounting_id))
            return "attempt_test"

        async def observe(attempt_id, **evidence):
            evidence["raw_usage"] = numbers(evidence["raw_usage"])
            self.events.append(("observe", evidence))
            try:
                self.receipt.update(self.prices.calculate(self.profile, evidence["raw_usage"], served_model=evidence["served_model"]))
            except PricingError:
                self.receipt.update(cost_status="unresolved", cost_usd=None)

        self.accounting = SimpleNamespace(check_budgets=AsyncMock(), begin=begin, attempt=attempt,
            observe=observe, finish=AsyncMock(), get=AsyncMock(side_effect=lambda _: self.receipt))

        def provider(request):
            self.events.append(("provider", str(request.url)))
            self.calls.append(request)
            self.assertEqual(str(request.url), PROVIDER_URL)
            self.assertEqual(request.headers["authorization"], "Bearer server-secret")
            self.assertEqual(json.loads(request.content), request_body())
            if self.transport_cancelled:
                raise asyncio.CancelledError()
            if self.transport_error:
                raise httpx.ReadTimeout("private upstream text", request=request)
            return httpx.Response(self.status, json=self.result, headers={"request-id": "req_test"})

        self.patches = [
            patch.dict(os.environ, {"JEV_API_KEY": "server-secret"}),
            patch.object(decision_routes, "registry", return_value=self.prices),
            patch.object(decision_routes, "accounting", self.accounting),
            patch.object(gateway_accounting, "accounting", self.accounting),
            patch.object(decision_routes, "invalidate_caches", new=AsyncMock()),
            patch.object(decision_routes.httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(provider), **kw)),
        ]
        for item in self.patches:
            item.start()
        self.token = gateway_accounting.state.set({})
        self.app = FastAPI()
        self.app.include_router(decision_routes.router)
        self.app.add_middleware(gateway_accounting.AccountingMiddleware)
        self.app.dependency_overrides[decision_routes.user_api_key_auth] = lambda: self.user
        self.client = HTTP_CLIENT(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost")

    async def asyncTearDown(self):
        await self.client.aclose()
        gateway_accounting.state.reset(self.token)
        for item in reversed(self.patches):
            item.stop()

    async def test_single_dispatch_follows_durable_intent_and_returns_native_answers(self):
        response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([e[0] for e in self.events], ["begin", "attempt", "provider", "observe"])
        body = response.json()
        self.assertEqual(body["answers"], self.result["answers"])
        self.assertEqual(body["usage"], self.result["usage"])
        self.assertEqual(body["provider_requests"], 1)
        self.assertEqual(body["cost_usd"], "0.000042")
        self.assertEqual(body["provider_request_id"], "req_test")
        self.assertEqual(response.headers["x-gateway-accounting-id"], "cost_test")
        self.assertEqual(response.headers["x-litellm-response-cost"], "0.000042")
        self.assertNotIn("private", json.dumps(self.events))
        self.assertNotIn("server-secret", response.text)
        decision_routes.invalidate_caches.assert_awaited_once()

    async def test_charged_malformed_answers_preserve_usage_and_do_not_retry(self):
        self.result["answers"]["target"]["choice"] = "invented"
        response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(response.json()["cost_usd"], "0.000042")
        self.assertEqual(self.events[-1][1]["raw_usage"], {"input_tokens": "1000", "output_tokens": "20"})
        self.assertNotIn("answers", response.json())

    async def test_provider_errors_and_ambiguous_timeout_are_never_retried(self):
        self.status = 429
        self.result = {"error": "private provider details"}
        response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["provider_requests"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("private", response.text)
        self.transport_error = True
        response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["cost_usd"], None)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.events[-1][1]["outcome"], "unknown")

    async def test_provider_auth_failure_is_a_submitted_request(self):
        for status in (401, 403):
            self.status = status
            self.result = {"error": "private provider authentication failure"}
            response = await self.client.post("/v1/decisions", json=request_body())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["provider_requests"], 1)
            self.assertNotIn("private", response.text)
        self.assertEqual(len(self.calls), 2)

    async def test_cancellation_retains_unknown_attempt_without_retry(self):
        self.transport_cancelled = True
        with self.assertRaises(asyncio.CancelledError):
            await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events[-1][1]["outcome"], "unknown")
        self.accounting.finish.assert_awaited_once_with("cost_test")

    async def test_invalid_request_missing_key_and_missing_intent_make_no_provider_call(self):
        response = await self.client.post("/v1/decisions", json={**request_body(), "api_key": "injected"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["provider_requests"], 0)
        with patch.dict(os.environ, {"JEV_API_KEY": ""}):
            response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 503)
        with patch.object(self.accounting, "attempt", new=AsyncMock(side_effect=RuntimeError("storage unavailable"))):
            response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.calls), 0)

    async def test_authorization_and_discovery_are_separate_from_generation(self):
        self.user.models = ["another-model"]
        response = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["provider_requests"], 0)
        self.assertEqual((await self.client.get("/v1/decisions/models")).json()["data"], [])
        self.user.models = [MODEL]
        row = (await self.client.get("/v1/decisions/models")).json()["data"][0]
        self.assertEqual(row["node_type"], "Decision")
        self.assertEqual(row["surfaces"], ["assistant"])
        self.assertTrue(row["available"])
        self.user.api_key = None
        self.assertEqual((await self.client.get("/v1/decisions/models")).status_code, 401)
        refused = await self.client.post("/v1/decisions", json=request_body())
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["provider_requests"], 0)
        self.assertEqual(len(self.calls), 0)

    async def test_real_gateway_auth_dependency_rejects_missing_key(self):
        from litellm.proxy import proxy_server
        from gateway_server import app
        with patch.object(proxy_server, "master_key", "test-master"):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                response = await client.post("/v1/decisions", json=request_body())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["provider_requests"], 0)
        self.assertEqual(len(self.calls), 0)


if __name__ == "__main__":
    unittest.main()
