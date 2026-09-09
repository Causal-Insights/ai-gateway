"""Dated Standard token rates for the exact September 2026 OpenAI models.

Sources (accessed 2026-09-09):
https://developers.openai.com/api/docs/pricing
https://developers.openai.com/api/docs/guides/prompt-caching
https://developers.openai.com/api/docs/guides/image-generation#cost-and-latency

Unknown or incomplete usage remains unknown; it is never a zero-dollar guess.
"""
from openai_model_contracts import ASTRA_MODELS, image_model

ASTRA_RATES = {"input": 10, "cache_read": 1, "cache_write": 12.5, "output": 50}
IMAGE_RATES = {"text_input": 5, "cached_text_input": 1.25, "image_input": 8, "cached_image_input": 2, "output": 30}


def as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, dict) else {}


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def astra_cost(usage, service_tier="default"):
    usage = as_dict(usage)
    details = as_dict(usage.get("input_tokens_details", usage.get("prompt_tokens_details")))
    input_tokens = token_count(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = token_count(usage.get("output_tokens", usage.get("completion_tokens")))
    cached = token_count(details.get("cached_tokens"))
    written = token_count(details.get("cache_write_tokens"))
    tier = {"default": 1, "flex": .5, "priority": 2, "fast": 2}.get(service_tier)
    if None in (input_tokens, output_tokens, cached, written, tier) or cached + written > input_tokens:
        return None
    long_context = input_tokens > 272000
    input_cost = ((input_tokens - cached - written) * ASTRA_RATES["input"]
                  + cached * ASTRA_RATES["cache_read"] + written * ASTRA_RATES["cache_write"])
    # Reasoning is already included in output_tokens.
    output_cost = output_tokens * ASTRA_RATES["output"]
    return (input_cost * (2 if long_context else 1) + output_cost * (1.5 if long_context else 1)) * tier / 1_000_000


def image_cost(usage):
    usage = as_dict(usage)
    details = as_dict(usage.get("input_tokens_details"))
    text = token_count(details.get("text_tokens"))
    images = token_count(details.get("image_tokens"))
    output = token_count(usage.get("output_tokens"))
    total_input = token_count(usage.get("input_tokens"))
    if None in (text, images, output, total_input) or text + images != total_input:
        return None
    cached_total = token_count(details.get("cached_tokens", usage.get("cached_tokens", 0)))
    cached_details = as_dict(details.get("cached_tokens_details", usage.get("cached_tokens_details")))
    cached_text = token_count(cached_details.get("text_tokens", 0 if cached_total == 0 else None))
    cached_images = token_count(cached_details.get("image_tokens", 0 if cached_total == 0 else None))
    if None in (cached_total, cached_text, cached_images) or cached_text + cached_images != cached_total:
        return None
    if cached_text > text or cached_images > images:
        return None
    # The provider's output total includes paid partial frames. Never add them again.
    return ((text - cached_text) * IMAGE_RATES["text_input"] + cached_text * IMAGE_RATES["cached_text_input"]
            + (images - cached_images) * IMAGE_RATES["image_input"] + cached_images * IMAGE_RATES["cached_image_input"]
            + output * IMAGE_RATES["output"]) / 1_000_000


def response_cost(model, result, optional_params=None):
    response = as_dict(result)
    # Completed streaming events wrap the same final response.
    if response.get("type") == "response.completed":
        response = as_dict(response.get("response"))
    if image_model(model):
        return image_cost(response.get("usage"))
    params = optional_params or {}
    cost = astra_cost(response.get("usage"), response.get("service_tier", params.get("service_tier", "default")))
    if cost is None:
        return None
    tools = [as_dict(tool) for tool in params.get("tools", [])]
    image_tools = [tool for tool in tools if tool.get("type") == "image_generation"]
    for item in response.get("output", []):
        item = as_dict(item)
        if item.get("type") != "image_generation_call":
            # Other hosted tools have their own prices. Leave the full request
            # cost unknown instead of silently charging only parent tokens.
            if item.get("type", "").endswith("_call"):
                return None
            continue
        selected = item.get("model") or (image_tools[0].get("model") if len(image_tools) == 1 else None)
        if not image_model(selected):
            return None
        tool_cost = image_cost(item.get("usage"))
        if tool_cost is None:
            return None
        cost += tool_cost
    return cost


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
