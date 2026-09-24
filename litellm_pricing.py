"""Use LiteLLM's calculators with an immutable per-attempt price snapshot.

Private cost-map keys keep historical calculations stable without swapping the
process-wide map or changing the model sent to a provider.
"""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pricing_registry import PricingError, decimal

ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / "pricing/litellm_catalog.json").read_text())


def install_catalog():
    import litellm
    # Only documented catalog entries/explicit corrections are installed. This
    # also updates SDK response-cost calculations in an older pinned SDK image.
    litellm.register_model(copy.deepcopy(catalog()["entries"]))


def profile_for(alias, model):
    data = catalog()
    mapping = data["models"][alias]
    info = copy.deepcopy(data["entries"][mapping["catalog_key"]])
    digest = hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()[:20]
    mode = info["mode"]
    routes = (["completion", "responses"] if mode == "chat" else
              ["generation_job", "image_generation"] if mode == "video_generation" else
              ["image_generation", "image_edit"] if mode == "image_generation" else ["speech"])
    # Gemini image generation uses chat completions to preserve imageConfig.
    gemini_image_chat = mode == "image_generation" and mapping["provider"] in {"vertex_ai", "gemini"} and mapping["resolver_model"].split("/", 1)[-1].startswith("gemini-")
    if gemini_image_chat:
        routes.append("completion")
    version = f"litellm:{mapping['resolver_model']}@{digest}" + ("-chat-route-v1" if gemini_image_chat else "")
    result = {"version": version,
        "engine": "litellm", "status": "catalog", "enabled": True,
        "vendor": model["vendor"], "upstream_model": model["upstream_model"],
        "deployment_model": model.get("deployment_model", model["upstream_model"]),
        "served_models": [model["upstream_model"].split("/", 1)[-1]],
        "currency": "USD", "rate_source": "litellm_catalog", "extractor": "components",
        "effective_from": data["retrieved_at"], "routes": routes,
        "options": {}, "defaults": {}, "components": [],
        "evidence": ["pricing/litellm_catalog.json"], "acceptance_evidence": [],
        "catalog_key": mapping["catalog_key"], "catalog_source": data["source"],
        "catalog_sha256": data["source_sha256"], "model_info": info,
        "cost_model": mapping["resolver_model"], "cost_provider": mapping["provider"],
        "zero_reasons": ["gateway_response_cache", "provider_reported_zero", "provider_filtered_outputs"],
        "calculation_version": "litellm-1.95.0-v1"}
    if mapping["provider"] == "xai":
        result["reported_charge"] = {"field": "cost_in_usd_ticks", "units_per_usd": "10000000000"}
    if info.get("gateway_rate_evidence"):
        result["rate_source"] = "posted_api_rate"
        result["evidence"].append(info["gateway_rate_evidence"])
    return result


@lru_cache(maxsize=256)
def _register_snapshot(provider, cost_model, serialized_info):
    import litellm
    digest = hashlib.sha256(serialized_info.encode()).hexdigest()[:20]
    name = cost_model.split("/", 1)[-1] + "-gateway-price-" + digest
    info = json.loads(serialized_info)
    # Explicit zeros prevent register_model from borrowing cache fields from a
    # different model. They do not invent missing usage or mark a request free.
    info.setdefault("cache_creation_input_token_cost", 0)
    info.setdefault("cache_read_input_token_cost", 0)
    litellm.register_model({name: info, provider + "/" + name: info})
    return name


def _native_numbers(value):
    if isinstance(value, dict):
        return {k: _native_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_native_numbers(v) for v in value]
    if isinstance(value, str):
        try:
            number = Decimal(value)
            return int(number) if number == number.to_integral_value() else float(number)
        except Exception:
            return value
    return value


def cost_using_response(profile, response, *, served_options=None):
    """Let the SDK price complete native responses, including hosted tools."""
    import litellm
    if getattr(response, "type", None) in {"response.completed", "response.failed", "response.incomplete"}:
        response = getattr(response, "response", None)
    if isinstance(response, dict) and "output" in response:
        from litellm.types.llms.openai import ResponsesAPIResponse
        response = ResponsesAPIResponse(**response)
    name = _register_snapshot(profile["cost_provider"], profile["cost_model"],
                              json.dumps(profile["model_info"], sort_keys=True))
    options = {**profile.get("request_options", {}), **(served_options or {})}
    if not hasattr(response, "model_copy"):
        return None
    mode = profile["model_info"]["mode"]
    if mode not in {"chat", "image_generation"}:
        return None
    call_type = "responses" if hasattr(response, "output") else "image_generation" if mode == "image_generation" else "completion"
    duplicate = response.model_copy(update={"model": name})
    # No requested output count: the provider response supplies actual outputs.
    value = litellm.completion_cost(completion_response=duplicate, model=name,
        custom_llm_provider=profile["cost_provider"], call_type=call_type,
        service_tier=options.get("service_tier"), size=options.get("size"), quality=options.get("quality"))
    if value is not None and profile.get("usage_corrections"):
        from accounting_usage import as_dict
        raw = as_dict(getattr(response, "usage", None))
        if mode == "image_generation":
            raw["output_images"] = len(getattr(response, "data", []) or [])
        base, _ = _calculate_sdk(profile, raw, served_options=served_options)
        corrected, _ = calculate(profile, raw, served_options=served_options)
        value = decimal(value) - base + corrected
    return None if value is None else str(decimal(value))


def matches_served_model(profile, served_model):
    """Accept provider-qualified IDs and date snapshots of the selected alias."""
    import re
    expected = profile["upstream_model"].split("/", 1)[-1]
    served = served_model.split("/", 1)[-1]
    if served == expected:
        return True
    # Providers resolve unversioned aliases to date snapshots. This identity
    # relationship does not permit a different model family or arbitrary suffix.
    if not re.fullmatch(re.escape(expected) + r"-\d{4}-\d{2}-\d{2}", served):
        return False
    import litellm
    info = litellm.model_cost.get(profile["cost_provider"] + "/" + served) or litellm.model_cost.get(served)
    if info:
        expected_rates = {k:v for k,v in profile["model_info"].items() if "cost" in k}
        served_rates = {k:v for k,v in info.items() if "cost" in k}
        return expected_rates == served_rates
    return True  # Dated identity of the submitted alias, priced at its pinned alias rate.


def calculate(profile, usage, *, served_options=None):
    options = {**profile.get("request_options", {}), **(served_options or {})}
    if usage.get("gateway_native_cost_usd") is None:
        for correction in profile.get("usage_corrections", []):
            if options.get("service_tier", "default") not in correction["options"].get("service_tier", []):
                continue
            from accounting_usage import extract
            from pricing_registry import registry
            raw = copy.deepcopy(usage)
            if correction["extractor"] == "openai_text":
                details = raw.setdefault("input_tokens_details", raw.get("prompt_tokens_details") or {})
                details.setdefault("cached_tokens", 0)
                details.setdefault("cache_write_tokens", 0)
            result = registry().calculate(correction, extract(correction["extractor"], raw))
            return decimal(result["cost_usd"]), result["breakdown"]
    return _calculate_sdk(profile, usage, served_options=served_options)


def _calculate_sdk(profile, usage, *, served_options=None):
    import litellm
    from litellm.types.utils import ImageResponse, ModelResponse, Usage

    raw = _native_numbers(usage)
    if raw.get("gateway_native_cost_usd") is not None:
        value = decimal(raw["gateway_native_cost_usd"])
        return value, [{"component": "litellm_response", "cost_usd": str(value),
                        "calculator": "litellm", "catalog_key": profile["catalog_key"]}]
    info = profile["model_info"]
    provider = profile["cost_provider"]
    name = _register_snapshot(provider, profile["cost_model"], json.dumps(info, sort_keys=True))
    mode = info["mode"]
    options = {**profile.get("request_options", {}), **(served_options or {})}
    parts = []
    try:
        if mode == "video_generation":
            from litellm.cost_calculator import default_video_cost_calculator
            seconds = decimal(raw.get("output_video_seconds"))
            if options.get("generate_audio") is False and info.get("gateway_video_silent_rates"):
                resolution = options.get("resolution") or "720p"
                rate = info["gateway_video_silent_rates"].get(resolution)
                if rate is None:
                    raise PricingError("No silent-video price for the served resolution")
                # The catalog's standard Veo fields describe video with audio.
                # Use the documented silent rate with the same SDK calculator.
                info = {**info, "output_cost_per_second": float(rate)}
                info.pop("output_cost_per_second_" + resolution, None)
            amount = default_video_cost_calculator(name, float(seconds), provider,
                model_info=info, video_resolution=options.get("resolution"))
            parts.append(("output_video", amount))
        elif mode == "image_generation":
            count = raw.get("output_images")
            if info.get("output_cost_per_image") is not None or provider == "xai":
                if count is None:
                    raise PricingError("Actual output image count is missing")
                rate = info.get("output_cost_per_image", info.get("input_cost_per_image"))
                if rate is None:
                    raise PricingError("LiteLLM image rate is missing")
                parts.append(("output_images", decimal(count) * decimal(rate)))
            else:
                if raw.get("input_tokens") is None or raw.get("output_tokens") is None:
                    raise PricingError("Actual image token usage is missing")
                image_usage = {k: v for k, v in raw.items() if k != "output_images"}
                image_usage.setdefault("total_tokens", raw["input_tokens"] + raw["output_tokens"])
                response = ImageResponse(data=[{} for _ in range(int(count or 0))], usage=image_usage)
                if provider == "openai":
                    from litellm.llms.openai.image_generation.cost_calculator import cost_calculator
                else:
                    from litellm.llms.vertex_ai.image_generation.cost_calculator import cost_calculator
                parts.append(("image_usage", cost_calculator(name, response)))
        elif mode == "audio_speech":
            characters = decimal(raw.get("billable_characters", raw.get("input_characters")))
            costs = litellm.cost_per_token(model=name, custom_llm_provider=provider,
                                          prompt_characters=int(characters), call_type="speech")
            parts.extend(zip(("input_characters", "audio_output"), costs))
        else:
            inputs = raw.get("prompt_tokens", raw.get("input_tokens"))
            outputs = raw.get("completion_tokens", raw.get("output_tokens"))
            decimal(inputs)
            decimal(outputs)
            native = {**raw, "prompt_tokens": inputs, "completion_tokens": outputs,
                      "total_tokens": raw.get("total_tokens", inputs + outputs)}
            if "input_tokens_details" in native:
                native["prompt_tokens_details"] = native.pop("input_tokens_details")
            if "output_tokens_details" in native:
                native["completion_tokens_details"] = native.pop("output_tokens_details")
            native.pop("gateway_tool_counts", None)
            usage_obj = Usage(**native)
            response = ModelResponse(model=name, choices=[], usage=usage_obj)
            amount = litellm.completion_cost(completion_response=response, model=name,
                custom_llm_provider=provider, call_type="completion",
                service_tier=options.get("service_tier"),
                standard_built_in_tools_params=raw.get("gateway_tool_counts"))
            if amount is None:
                raise PricingError("LiteLLM could not calculate this usage")
            parts.append(("litellm_usage", amount))
    except PricingError:
        raise
    except Exception as exc:
        raise PricingError(f"LiteLLM cost calculation failed: {type(exc).__name__}") from exc
    breakdown = [{"component": component, "cost_usd": str(decimal(amount)),
                  "calculator": "litellm", "catalog_key": profile["catalog_key"]}
                 for component, amount in parts]
    return sum((Decimal(part["cost_usd"]) for part in breakdown), Decimal(0)), breakdown
