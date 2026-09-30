"""Compatibility cost helpers backed by the versioned registry.

The gateway journal owns billing. These helpers return unknown for unverified
parent/tool combinations and never consult mutable LiteLLM fallback rates.
"""
from openai_model_contracts import ASTRA_MODELS, image_model



def as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, dict) else {}


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def _registry_cost(alias, route, usage, *, service_tier="default"):
    from accounting_usage import extract
    from pricing_registry import registry, PricingError
    try:
        prices = registry()
        profile = prices.select(alias, route, {"service_tier": service_tier}, admission=False)
        result = prices.calculate(profile, extract(profile["extractor"], usage),
                                  served_options={"service_tier": service_tier})
        return float(result["cost_usd"])
    except (PricingError, KeyError, TypeError):
        return None


def astra_cost(usage, service_tier="default"):
    return _registry_cost("gpt-6-astra", "responses", usage, service_tier=service_tier)


def image_cost(usage):
    return _registry_cost("gpt-image-2.5-sunburst", "image_generation", usage)


def response_cost(model, result, optional_params=None):
    response = as_dict(result)
    if response.get("type") == "response.completed":
        response = as_dict(response.get("response"))
    params = optional_params or {}
    # These compatibility helpers are never a billing authority. Hosted tool
    # attribution requires a verified parent/tool contract; do not assume that
    # independently adding a tool cost avoids double counting.
    if params.get("tools") or any(as_dict(x).get("type", "").endswith("_call") for x in response.get("output", [])):
        return None
    alias = model.removeprefix("openai/").removesuffix("-2026-09-08")
    return _registry_cost(alias, "image_generation" if image_model(model) else "responses", response.get("usage"),
                          service_tier=response.get("service_tier", params.get("service_tier", "default")))


def install_openai_accounting():
    """Use LiteLLM's existing spend/header/log pipeline, scoped to exact IDs."""
    from litellm.litellm_core_utils.litellm_logging import Logging
    original = Logging._response_cost_calculator
    if getattr(original, "_openai_september_contract", False):
        return

    def calculate(self, result, cache_hit=None, litellm_model_name=None, router_model_id=None):
        candidates = (litellm_model_name, self.model, as_dict(result).get("model"))
        model = next((value for value in candidates if value in ASTRA_MODELS or image_model(value)), None)
        if model in ASTRA_MODELS or image_model(model):
            if cache_hit is True or (cache_hit is None and self.model_call_details.get("cache_hit") is True):
                return 0.0
            return response_cost(model, result, self.optional_params)
        return original(self, result, cache_hit, litellm_model_name, router_model_id)

    calculate._openai_september_contract = True
    Logging._response_cost_calculator = calculate
