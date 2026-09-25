"""Finite OpenAI hosted-tool attribution, pinned at authenticated admission.

Tool declarations are not usage. Resource duration/storage and image-tool
attribution remain unknown when the provider does not return billable evidence.
"""
from copy import deepcopy

from accounting_usage import as_dict
from pricing_registry import PricingError, decimal

HOSTED_TYPES = {"web_search", "file_search", "code_interpreter", "image_generation"}
PARENT_USAGE_FIELDS = {"input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens",
                       "input_tokens_details", "prompt_tokens_details", "output_tokens_details", "completion_tokens_details"}


def discovery_contract(alias, document):
    if alias != "gpt-6-astra":
        return None
    contract = deepcopy(document.get("hosted_tools") or {})
    operations = [{"id": "responses.create", "endpoint": "/v1/responses", "execution_modes": ["sync", "stream", "background"],
        "hosted_tools": [{"type": kind, "accounting": "measured_calls" if kind in {"web_search", "file_search"} else "provider_usage_required"}
                         for kind in sorted(HOSTED_TYPES)]}]
    if alias in contract.get("batch_models", []):
        operations.append({"id": "batch.create", "endpoint": "/v1/batches", "execution_modes": ["batch"],
                           "item_endpoints": sorted(BATCH_ROUTES), "tools": False, "stream": False})
    return {"operations": operations, "hosted_pricing": contract,
            "resource_endpoints": ["/v1/files", "/v1/vector_stores", "/v1/containers"],
            "owner_data_endpoint": "/v1/resources/owner-data",
            "resource_owner_header": "X-Gateway-Resource-Owner",
            "limitations": ["Container duration, storage allocation, and image-tool attribution may leave total cost unresolved; valid outputs are retained."]}


BATCH_ROUTES = {"/v1/responses", "/v1/chat/completions"}


def pin_contract(profile, data, document):
    effective = {**(data.get("extra_body") or {}), **(data.get("optional_params") or {}), **data}
    types = sorted({tool.get("type") for tool in (effective.get("tools") or [])
                    if isinstance(tool, dict) and tool.get("type") in HOSTED_TYPES})
    if profile.get("vendor") != "openai" or not types:
        return profile
    result = deepcopy(profile)
    result["hosted_contract"] = {**deepcopy(document.get("hosted_tools") or {}), "types": types}
    return result


def measured_tools(response):
    data = as_dict(response)
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
        data = as_dict(data.get("response"))
    if data.get("status") in {"queued", "in_progress"} or not isinstance(data.get("output"), list):
        return {}  # No terminal counts yet; a later owned retrieval can settle.
    counts = {"web_search_calls": 0, "file_search_calls": 0}
    image_usage = []
    unresolved = False
    for value in data["output"]:
        item = as_dict(value)
        kind = item.get("type")
        if kind == "web_search_call":
            action = as_dict(item.get("action")).get("type")
            if item.get("status") != "completed" or action not in {"search", "open_page", "find_in_page"}:
                unresolved = True
            elif action == "search":
                counts["web_search_calls"] += 1
        elif kind == "file_search_call":
            if item.get("status") == "completed":
                counts["file_search_calls"] += 1
            else:
                unresolved = True
        elif kind in {"code_interpreter_call", "image_generation_call"}:
            # Container IDs do not establish billable session duration. Image
            # result bytes do not establish independent parent/tool token usage.
            unresolved = True
            if kind == "image_generation_call" and item.get("usage"):
                image_usage.append(as_dict(item["usage"]))
    return {"gateway_hosted_counts": counts, **({"gateway_hosted_unresolved": 1} if unresolved else {}),
            **({"gateway_image_tool_usage": image_usage} if image_usage else {})}


def parent_subtotal(profile, usage, served_options=None):
    from litellm_pricing import calculate as native_calculate
    base = {key: value for key, value in profile.items() if key != "hosted_contract"}
    raw = {key: value for key, value in usage.items() if key in PARENT_USAGE_FIELDS}
    try:
        amount, _ = native_calculate(base, raw, served_options=served_options)
        return str(amount)
    except (PricingError, KeyError, TypeError, ValueError):
        return None


def calculate(profile, usage, base_calculator, served_options=None):
    contract = profile["hosted_contract"]
    if usage.get("gateway_hosted_unresolved") or not isinstance(usage.get("gateway_hosted_counts"), dict):
        raise PricingError("Hosted resource/tool billable usage is incomplete")
    base = {key: value for key, value in profile.items() if key != "hosted_contract"}
    # The SDK can also recognize tool counters in usage. Price parent tokens
    # alone here; measured hosted calls below must not be charged twice.
    raw = {key: value for key, value in usage.items() if key in PARENT_USAGE_FIELDS}
    amount, breakdown = base_calculator(base, raw, served_options=served_options)
    for field, rate_field in (("web_search_calls", "web_search_call_usd"), ("file_search_calls", "file_search_call_usd")):
        count = decimal(usage["gateway_hosted_counts"].get(field))
        if not count:
            continue
        rate = decimal(contract.get(rate_field))
        charge = count * rate
        amount += charge
        breakdown.append({"component": field, "quantity": str(count), "rate": str(rate),
                          "cost_usd": str(charge), "pricing_version": contract.get("version")})
    return amount, breakdown


async def validate_resources(user, data):
    """Resolve only documented resource references, never arbitrary strings."""
    from owned_openai_resources import owned, resource_owner
    headers = (data.get("proxy_server_request") or {}).get("headers") or {}
    references = []
    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if value.get("file_id"):
                references.append(("file", value["file_id"]))
            for key, child in value.items():
                if key in {"file_ids", "vector_store_ids"} and isinstance(child, list):
                    references.extend(("vector_store" if key == "vector_store_ids" else "file", item) for item in child)
                elif key == "container" and isinstance(child, str):
                    references.append(("container", child))
                elif key == "previous_response_id" and child:
                    references.append(("response", child))
                elif key in {"input", "content", "tools", "container", "image", "mask", "extra_body", "optional_params"}:
                    visit(child)
    visit(data)
    owner = None
    if references:
        owner = resource_owner(user, headers)
        for kind, identifier in references:
            await owned(kind, identifier, owner)
    elif any(isinstance(t, dict) and t.get("type") in {"file_search", "code_interpreter"}
             for source in (data, data.get("extra_body") or {}, data.get("optional_params") or {})
             for t in (source.get("tools") or [])):
        owner = resource_owner(user, headers)
    elif any(name.lower() == "x-gateway-resource-owner" for name in headers):
        owner = resource_owner(user, headers)
    if owner:
        from openai_owner_lifecycle import ensure_active
        await ensure_active(owner)
        # Native response cache keys need not include the delegated owner. A
        # cached container/response ID must never be reassigned to another user.
        data["cache"] = {**(data.get("cache") or {}), "no-cache": True, "no-store": True}
    return owner


async def retain_resources(response, current):
    owner = current.get("resource_owner")
    if not owner:
        return
    from owned_openai_resources import remember
    data = as_dict(response)
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
        data = as_dict(data.get("response"))
    if data.get("id") and isinstance(data.get("output"), list):
        await remember("response", data["id"], owner, {"id": data["id"], "model": data.get("model"),
            "gateway_accounting_id": current.get("accounting_id"),
            "gateway_attempt_id": (current.get("attempts") or [None])[-1]})
    for item in data.get("output", []):
        item = as_dict(item)
        if item.get("type") == "code_interpreter_call" and item.get("container_id"):
            await remember("container", item["container_id"], owner,
                           {"id": item["container_id"], "cost_status": "unresolved"})
