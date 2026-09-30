import copy
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from accounting_usage import extract, numbers
from pricing_registry import PricingError, PricingRegistry


def price_document():
    return {"vendors": {"xai": {}}, "models": {"test-model": {"upstream_model": "xai/test-model", "profiles": ["test-v1"]}},
        "profiles": {"test-v1": {"version": "test-v1", "upstream_model": "xai/test-model", "vendor": "xai",
            "currency": "USD", "effective_from": "2020-01-01T00:00:00Z", "status": "verified",
            "enabled": True, "evidence": ["fixture"], "acceptance_evidence": ["fixture"], "verified_at": "2026-09-16T00:00:00Z",
            "routes": ["completion", "generation_job"], "options": {"tools": [False]}, "defaults": {"tools": False},
            "extractor": "components", "rate_source": "account_rate", "components": [
                {"usage": "input_tokens", "rate": "0.2", "per": "1000000", "unit": "token"},
                {"usage": "output_tokens", "rate": "1.2", "per": "1000000", "unit": "token"}],
            "reported_charge": {"field": "cost_in_usd_ticks", "units_per_usd": "10000000000"},
            "zero_reasons": ["provider_reported_zero"]}}}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.prices = PricingRegistry(price_document())
        self.profile = self.prices.select("test-model", "completion")

    def test_decimal_and_reported_account_cost_take_precedence(self):
        cost = self.prices.calculate(self.profile, {"input_tokens": 199, "output_tokens": 1}, reported_charge=158500)
        self.assertEqual(Decimal(cost["cost_usd"]), Decimal("0.00001585"))
        self.assertEqual(Decimal(cost["calculated_cost_usd"]), Decimal("0.000041"))
        self.assertEqual(cost["cost_source"], "provider_reported")

    def test_exact_units_not_thousand_for_million(self):
        cost = self.prices.calculate(self.profile, {"input_tokens": 1000, "output_tokens": 0})
        self.assertEqual(Decimal(cost["cost_usd"]), Decimal("0.0002"))

    def test_missing_negative_nonfinite_boolean_and_zero_not_free(self):
        for bad in ({}, {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": "NaN", "output_tokens": 1},
                    {"input_tokens": True, "output_tokens": 1}, {"input_tokens": 0, "output_tokens": 0}):
            with self.subTest(bad=bad), self.assertRaises(PricingError):
                self.prices.calculate(self.profile, bad)
        result = self.prices.calculate(self.profile, {}, reported_charge=0, zero_reason="provider_reported_zero")
        self.assertEqual(result["cost_usd"], "0")

    def test_unknown_model_route_options_and_historical_date_blocked(self):
        for args, kwargs in [(("missing", "completion"), {}), (("test-model", "speech"), {}),
            (("test-model", "completion", {"tools": True}), {}),
            (("test-model", "completion", {"service_tier": "unknown-new-tier"}), {}),
            (("test-model", "completion"), {"at": datetime(2019, 1, 1, tzinfo=timezone.utc)}),
            (("test-model", "completion"), {"upstream": "different/test-model"})]:
            with self.subTest(args=args), self.assertRaises(PricingError):
                self.prices.select(*args, **kwargs)

    def test_duplicate_active_prices_block_instead_of_selecting_arbitrarily(self):
        document = price_document()
        document["profiles"]["test-v2"] = {**document["profiles"]["test-v1"], "version": "test-v2"}
        document["models"]["test-model"]["profiles"].append("test-v2")
        with self.assertRaises(PricingError):
            PricingRegistry(document).select("test-model", "completion")

    def test_unverified_and_unaccepted_profiles_never_admitted(self):
        for status in ("unverified", "verified"):
            document = price_document()
            document["profiles"]["test-v1"].update(status=status, enabled=False)
            with self.assertRaises(PricingError):
                PricingRegistry(document).select("test-model", "completion")

    def test_served_model_is_checked(self):
        with self.assertRaises(PricingError):
            self.prices.calculate(self.profile, {"input_tokens": 1, "output_tokens": 1}, served_model="other-model")

    def test_account_and_served_options_cannot_silently_change(self):
        profile = copy.deepcopy(self.profile)
        profile["deployment_context"] = {"project": "verified-project"}
        with self.assertRaises(PricingError):
            self.prices.check_context(profile, {"project": "other-project"})
        profile["require_served_options"] = ["service_tier"]
        profile["options"]["service_tier"] = ["default"]
        with self.assertRaises(PricingError):
            self.prices.calculate(profile, {}, reported_charge=100, served_options={"service_tier": "fast"})

    def test_changed_registry_does_not_change_pinned_profile(self):
        pinned = copy.deepcopy(self.profile)
        self.profile["components"][0]["rate"] = "20"
        cost = self.prices.calculate(pinned, {"input_tokens": 1000, "output_tokens": 0})
        self.assertEqual(cost["cost_usd"], "0.0002")

    def test_registry_cannot_claim_evidence_for_a_different_rate(self):
        prices = PricingRegistry()
        profile = next(p for p in prices.profiles.values() if p["status"] == "verified" and p.get("components"))
        profile["components"][0]["rate"] = "99999"
        with self.assertRaises(PricingError):
            prices.validate()

    def test_catalog_seedance_promotional_profiles_and_expiry(self):
        prices = PricingRegistry()
        from datetime import datetime, timezone
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        for video, expected in ((False,"4.2"), (True,"2.475")):
            options = {"resolution":"720p", "input_video":video}
            profile = prices.select("seedance-2.0-fast", "generation_job", options, at=now, admission=False)
            cost = prices.calculate(profile, {"completion_tokens":1000000}, served_options=options)
            self.assertEqual(cost["cost_usd"], expected)
        successor = prices.select("seedance-2.0-fast", "generation_job", options,
                          at=datetime(2026,10,7,6,tzinfo=timezone.utc), admission=False)
        self.assertEqual(successor["components"][0]["rate"], "3.3")

    def test_omni_counts_thoughts_separately_and_requires_modality(self):
        usage = {"total_input_tokens": 2405, "total_output_tokens": 28960, "total_thought_tokens": 452,
                 "output_tokens_by_modality": [{"modality": "video", "tokens": 28960}]}
        result = extract("omni", usage)
        self.assertEqual(result, {"input_tokens": "2405", "video_tokens": "28960", "text_output_tokens": "452"})
        usage["output_tokens_by_modality"][0]["tokens"] = 28959
        with self.assertRaises(PricingError):
            extract("omni", usage)

    def test_omni_explicit_audio_uses_successor_without_repricing_history(self):
        prices = PricingRegistry()
        current = prices.select("gemini-omni-1.1-flash", "generation_job", {"generate_audio": True})
        historic = prices.select("gemini-omni-1.1-flash", "generation_job", at=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertNotEqual(current["version"], historic["version"])
        self.assertEqual(current["components"], historic["components"])
        self.assertEqual(current["extractor"], "omni")

    def test_image_cached_modalities_are_disjoint(self):
        result = extract("openai_image", {"input_tokens": 150, "output_tokens": 20,
            "input_tokens_details": {"text_tokens": 50, "image_tokens": 100, "cached_tokens": 30,
                "cached_tokens_details": {"text_tokens": 10, "image_tokens": 20}}})
        self.assertEqual(result["image_input_tokens"], "80")
        self.assertEqual(result["text_input_tokens"], "40")

    def test_usage_redaction_drops_content_and_invalid_values(self):
        self.assertEqual(numbers({"prompt": "private words", "url": "https://example.com", "tokens": 3, "bad": -1}), {"tokens": "3"})

    def test_gateway_cache_hit_does_not_rebill_original_provider_charge(self):
        profile = copy.deepcopy(self.profile)
        profile["zero_reasons"].append("gateway_response_cache")
        result = self.prices.calculate(profile, {}, reported_charge=7100000000, zero_reason="gateway_response_cache")
        self.assertEqual(result["cost_usd"], "0")
        self.assertIsNone(result["reported_cost_usd"])


if __name__ == "__main__":
    unittest.main()
