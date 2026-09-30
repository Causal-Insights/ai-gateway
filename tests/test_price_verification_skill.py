"""Behavioral invariants for evidence ingestion; prose quality also needs review."""
import copy
import json
from pathlib import Path
import unittest

from scripts.validate_price_evidence import validate


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads((Path(__file__).resolve().parents[1] / "pricing/evidence/xai-reported-charge-2026-09-16.json").read_text())

    def test_valid_numeric_provider_evidence(self):
        validate(self.report)

    def test_wrong_vendor_source_is_not_verified(self):
        self.report["identity"]["vendor"] = "openai"
        with self.assertRaises(ValueError):
            validate(self.report)

    def test_third_party_calculator_is_not_authority(self):
        self.report["sources"][0]["url"] = "https://prices.example.com/calculator"
        with self.assertRaises(ValueError):
            validate(self.report)

    def test_inaccessible_sources_stay_explicitly_unverified(self):
        self.report.update(status="unverified", sources=[], dimensions=[], calculation=None, rate_basis="unknown",
                           limitations=["Official source requires unavailable account access"])
        validate(self.report)
        self.report["status"] = "verified"
        with self.assertRaises(Exception):
            validate(self.report)

    def test_consumer_rates_do_not_verify_elevenapi(self):
        self.report["identity"].update(vendor="elevenlabs", api_product="ElevenAPI")
        self.report["sources"][0].update(url="https://elevenlabs.io/pricing", scope="pricing")
        with self.assertRaises(ValueError):
            validate(self.report)

    def test_developer_api_does_not_verify_vertex(self):
        self.report["identity"].update(vendor="google", api_product="Vertex AI")
        self.report["sources"][0].update(url="https://ai.google.dev/pricing", scope="pricing")
        with self.assertRaises(ValueError):
            validate(self.report)
