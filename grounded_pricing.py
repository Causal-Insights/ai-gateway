"""Exact search tariffs with immutable request selection and measured counters."""
from copy import deepcopy

from accounting_usage import as_dict
from pricing_registry import PricingError, decimal


def pin_contract(profile, data):
    tariff = profile.get("grounding_tariff")
    if not tariff:
        return profile
    effective = {**(data.get("extra_body") or {}), **(data.get("optional_params") or {}), **data}
    selected = set()
    for tool in effective.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if profile["vendor"] == "google" and ("google_search" in tool or "googleSearch" in tool):
            selected.add("google_search")
        if profile["vendor"] == "xai" and tool.get("type") in {"web_search", "x_search"}:
            selected.add(tool["type"])
    if not selected:
        return profile
    return {**deepcopy(profile), "grounding_contract": {**deepcopy(tariff), "tools": sorted(selected)}}


def clean_parent_usage(usage):
    raw = deepcopy(usage)
    for key in list(raw):
        if key.startswith("gateway_") or key in {"server_side_tool_usage_details", "cost", "cost_in_usd_ticks"}:
            raw.pop(key)
    for name in ("input_tokens_details", "prompt_tokens_details"):
        if isinstance(raw.get(name), dict):
            raw[name].pop("web_search_requests", None)
            raw[name].pop("google_maps_grounding_requests", None)
    return raw


def measured(profile, response):
    data = as_dict(response)
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
        data = as_dict(data.get("response"))
    usage = as_dict(data.get("usage"))
    tools = profile["grounding_contract"]["tools"]
    counters = {}
    if "google_search" in tools:
        details = as_dict(usage.get("prompt_tokens_details") or usage.get("input_tokens_details"))
        if details.get("web_search_requests") is not None:
            counters["google_search_queries"] = details["web_search_requests"]
    detail = as_dict(usage.get("server_side_tool_usage_details"))
    fields = (["web_search_calls"] if "web_search" in tools else []) + (["x_posts_fetched", "x_users_fetched"] if "x_search" in tools else [])
    counters.update({field: detail[field] for field in fields if field in detail})
    return {"gateway_grounding_counts": counters,
            "gateway_grounding_usage_source": "sdk_query_count_estimate" if "google_search" in tools else "provider_usage_counts"}


def parent_subtotal(profile, response):
    """Strip SDK tool counters before asking it to price the original modalities."""
    from litellm_pricing import cost_using_response
    base = {key: value for key, value in profile.items() if key != "grounding_contract"}
    data = deepcopy(as_dict(response))
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
        data = as_dict(data.get("response"))
    data["usage"] = clean_parent_usage(as_dict(data.get("usage")))
    if isinstance(data.get("output"), list):
        data["output"] = [item for item in data["output"] if as_dict(item).get("type") not in {"web_search_call", "x_search_call"}]
    data.pop("vertex_ai_grounding_metadata", None)
    data.pop("_hidden_params", None)
    try:
        if "choices" in data:
            from litellm import ModelResponse
            data = ModelResponse(**data)
        return cost_using_response(base, data)
    except Exception:
        return None


def calculate(profile, usage, base_calculator, served_options=None):
    contract = profile["grounding_contract"]
    # The pinned Gemini SDK deduplicates query strings across candidates. That
    # count cannot establish the provider's individually billed search queries.
    if "google_search" in contract["tools"] and usage.get("gateway_grounding_usage_source") != "provider_usage_counts":
        raise PricingError("Google grounding billable query count is unavailable; SDK query count is an estimate")
    counts = usage.get("gateway_grounding_counts") or {}
    required = (["google_search_queries"] if "google_search" in contract["tools"] else [])
    required += (["web_search_calls"] if "web_search" in contract["tools"] else [])
    required += (["x_posts_fetched", "x_users_fetched"] if "x_search" in contract["tools"] else [])
    quantities = {field: decimal(counts.get(field)) for field in required}
    if any(value != value.to_integral_value() for value in quantities.values()):
        raise PricingError("Search usage counters must be whole numbers")
    base = {key: value for key, value in profile.items() if key != "grounding_contract"}
    raw = clean_parent_usage(usage)
    if usage.get("gateway_grounding_parent_cost_usd") is not None:
        amount = decimal(usage["gateway_grounding_parent_cost_usd"])
        breakdown = [{"component": "parent_model", "cost_usd": str(amount)}]
    else:
        amount, breakdown = base_calculator(base, raw, served_options=served_options)
    for field, count in quantities.items():
        rate = decimal(contract["rates"][field])
        charge = count * rate
        amount += charge
        breakdown.append({"component": field, "quantity": str(count), "rate": str(rate),
                          "cost_usd": str(charge), "pricing_version": contract["version"]})
    return amount, breakdown
