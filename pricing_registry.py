"""Versioned, fail-closed pricing. Decimal amounts are serialized as strings.

The registry is evidence, not a copy of LiteLLM's mutable cost map. Never infer a
missing usage component, silently select another profile, or rate old usage at
today's price. Profiles are selected at submission and pinned in the journal.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


class PricingError(ValueError):
    code = "PRICING_UNVERIFIED"


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PricingError("Missing or invalid billable quantity")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PricingError("Invalid decimal quantity") from None
    if not result.is_finite() or result < 0:
        raise PricingError("Billable quantities must be finite and nonnegative")
    return result


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PricingError("Pricing dates require a timezone")
    return result


class PricingRegistry:
    def __init__(self, document=None):
        self._check_evidence_files = document is None
        self.document = document or json.loads((ROOT / "pricing/registry.json").read_text())
        self.models = self.document["models"]
        self.profiles = self.document["profiles"]
        self.validate()

    def validate(self):
        for key, profile in self.profiles.items():
            if profile["version"] != key or profile["currency"] != "USD":
                raise PricingError("Invalid pricing version or unsupported currency")
            if profile["vendor"] not in self.document["vendors"]:
                raise PricingError("Vendor needs an official verification procedure")
            timestamp(profile["effective_from"])
            if profile.get("effective_to"):
                if timestamp(profile["effective_to"]) <= timestamp(profile["effective_from"]):
                    raise PricingError("Invalid pricing interval")
            for component in profile.get("components", []):
                decimal(component["rate"])
                if decimal(component["per"]) == 0:
                    raise PricingError("Zero pricing denominator")
                if component.get("long_context"):
                    decimal(component["long_context"]["above"])
                    decimal(component["long_context"]["rate"])
            if profile["status"] == "verified":
                if not profile.get("evidence") or not profile.get("verified_at"):
                    raise PricingError("Verified rates require evidence and retrieval date")
                if not profile.get("components") and not profile.get("reported_charge"):
                    raise PricingError("Verified profile has no billing rule")
                timestamp(profile["verified_at"])
                if not profile.get("verify_by") and self._check_evidence_files:
                    raise PricingError("Verified profiles require a review deadline")
                if self._check_evidence_files:
                    dimensions = []
                    for evidence in profile["evidence"]:
                        path = (ROOT / evidence).resolve()
                        if not path.is_relative_to(ROOT / "pricing/evidence"):
                            raise PricingError("Price evidence must be inside the evidence directory")
                        report = json.loads(path.read_text())
                        if report.get("status") not in {"verified", "mismatch"} or report.get("identity", {}).get("vendor") != profile["vendor"]:
                            raise PricingError("Profile is not supported by verified vendor evidence")
                        if report["identity"]["upstream_model"] not in {profile["upstream_model"], profile["upstream_model"].split("/", 1)[-1]}:
                            raise PricingError("Price evidence names a different upstream")
                        if report["currency"] != profile["currency"]:
                            raise PricingError("Evidence currency differs from the profile")
                        dimensions.extend(report["dimensions"])
                    for component in profile.get("components", []):
                        if not any(d["name"] == component["usage"] and d["unit"] == component["unit"]
                                   and decimal(d["rate"]) == decimal(component["rate"])
                                   and decimal(d["per"]) == decimal(component["per"])
                                   and d.get("conditions", {}).get("long_context") == component.get("long_context")
                                   for d in dimensions):
                            raise PricingError("Registry rate or context tier differs from its evidence")
                    charge = profile.get("reported_charge")
                    if charge and not any(d["name"] == "reported_charge" and decimal(d["rate"]) == 1
                                          and decimal(d["per"]) == decimal(charge["units_per_usd"]) for d in dimensions):
                        raise PricingError("Reported-charge conversion differs from its evidence")
            if profile.get("enabled") and profile["status"] not in {"verified", "catalog"}:
                raise PricingError("Enabled pricing requires a supported billing rule")
            if profile.get("engine") == "litellm":
                if not profile.get("model_info") or not profile.get("catalog_sha256"):
                    raise PricingError("LiteLLM pricing requires a captured catalog entry")
        for alias, model in self.models.items():
            for version in model["profiles"]:
                if version not in self.profiles or self.profiles[version]["upstream_model"] != model["upstream_model"]:
                    raise PricingError(f"Invalid upstream pricing mapping: {alias}")

    def select(self, alias, route, options=None, *, at=None, upstream=None, admission=True):
        model = self.models.get(alias)
        if not model or model.get("disabled"):
            raise PricingError(f"Model {alias!r} has no enabled pricing contract")
        if upstream is not None and upstream != model.get("deployment_model", model["upstream_model"]):
            raise PricingError("Selected deployment does not match the verified upstream")
        at = at or datetime.now(timezone.utc)
        options = options or {}
        matches = []
        for version in model["profiles"]:
            profile = self.profiles[version]
            if timestamp(profile["effective_from"]) > at or (profile.get("effective_to") and at >= timestamp(profile["effective_to"])):
                continue
            if route not in profile["routes"]:
                continue
            if profile.get("engine") == "litellm" and profile.get("enabled"):
                matches.append({**profile, "request_options": options})
                continue
            covered = set(profile.get("options", {})) | set(profile.get("metered_options", []))
            if any(value is not None and value is not False and key not in covered for key, value in options.items()):
                continue
            if any((options.get(key) if options.get(key) is not None else profile.get("defaults", {}).get(key)) not in values
                   for key, values in profile.get("options", {}).items()):
                continue
            if profile["status"] != "verified" or (admission and not profile.get("enabled")):
                continue
            # Review deadlines trigger monitoring, not a model outage. Actual
            # rate effective_to dates above still select the applicable price.
            matches.append(profile)
        if len(matches) != 1:
            raise PricingError(f"Exactly one verified price is required for {alias!r}, {route}, and its billing options")
        return matches[0]

    @staticmethod
    def check_context(profile, context):
        """Bind contract prices to the deployment, never client-supplied metadata."""
        for key, expected in profile.get("deployment_context", {}).items():
            if context.get(key) != expected:
                raise PricingError(f"Deployment {key} differs from the verified pricing context")

    def calculate(self, profile, usage, *, served_model=None, served_options=None, reported_charge=None, zero_reason=None):
        if profile["status"] not in {"verified", "catalog"}:
            raise PricingError("The pinned pricing profile is unverified")
        if zero_reason == "gateway_response_cache" and zero_reason in profile.get("zero_reasons", []):
            return {"cost_status": "priced", "cost_usd": "0", "cost_source": "gateway_response_cache",
                    "reported_cost_usd": None, "calculated_cost_usd": "0", "pricing_version": profile["version"],
                    "usage": {}, "breakdown": [], "zero_reason": zero_reason, "billing_eligible": True}
        if served_model and served_model not in profile.get("served_models", [profile["upstream_model"].split("/", 1)[-1]]):
            from litellm_pricing import matches_served_model
            if profile.get("engine") != "litellm" or not matches_served_model(profile, served_model):
                raise PricingError("Served model is outside the verified profile")
        for key in profile.get("require_served_options", []):
            if key not in (served_options or {}) or served_options[key] not in profile.get("options", {}).get(key, []):
                raise PricingError(f"Actual served {key} is missing or outside the verified profile")
        for key, limits in profile.get("usage_limits", {}).items():
            quantity = decimal(usage.get(key))
            if "max" in limits and quantity > decimal(limits["max"]):
                raise PricingError(f"Measured {key} exceeds the verified price coverage")
        breakdown = []
        calculated = None
        try:
            if profile.get("engine") == "litellm":
                from litellm_pricing import calculate
                calculated, breakdown = calculate(profile, usage, served_options=served_options)
            with localcontext() as context:
                context.prec = 40
                for component in profile.get("components", []):
                    quantity = decimal(usage.get(component["usage"]))
                    rate = component["rate"]
                    tier = component.get("long_context")
                    if tier and decimal(usage.get(tier["usage"])) > decimal(tier["above"]):
                        rate = tier["rate"]
                    amount = quantity * decimal(rate) / decimal(component["per"])
                    breakdown.append({"component": component["usage"], "quantity": str(quantity),
                                      "unit": component["unit"], "rate": rate,
                                      "per": component["per"], "cost_usd": str(amount)})
                if breakdown:
                    calculated = sum((Decimal(item["cost_usd"]) for item in breakdown), Decimal(0))
        except PricingError:
            if reported_charge is None:
                raise
            breakdown = []
        reported = None
        if reported_charge is not None:
            rule = profile.get("reported_charge")
            if not rule:
                raise PricingError("Provider charge units have not been verified")
            reported = decimal(reported_charge) / decimal(rule["units_per_usd"])
        amount = reported if reported is not None else calculated
        if amount is None:
            raise PricingError("No measured usage or verified provider charge")
        if amount == 0 and zero_reason not in profile.get("zero_reasons", []):
            raise PricingError("Zero cost needs an explicit verified reason")
        return {"cost_status": "priced", "cost_usd": str(amount),
                "cost_source": "provider_reported" if reported is not None else profile["rate_source"],
                "reported_cost_usd": str(reported) if reported is not None else None,
                "calculated_cost_usd": str(calculated) if calculated is not None else None,
                "pricing_version": profile["version"], "usage": usage, "breakdown": breakdown,
                "zero_reason": zero_reason, "billing_eligible": True}


_registry = None


def registry():
    global _registry
    if _registry is None:
        _registry = PricingRegistry()
    return _registry
