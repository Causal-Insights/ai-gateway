"""Strict numeric usage extraction. Payload text and media never enter the journal."""
from pricing_registry import PricingError, decimal


def as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, dict) else {}


def numbers(value):
    """Retain numeric usage trees, including modality labels, without request content."""
    if isinstance(value, dict):
        return {str(k): cleaned for k, v in value.items()
                if (cleaned := numbers(v)) is not None and cleaned != {}}
    if isinstance(value, list):
        return [cleaned for v in value if (cleaned := numbers(v)) is not None]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return str(decimal(value))
        except PricingError:
            return None
    if isinstance(value, str):
        if value in {"text", "image", "video", "audio", "TEXT", "IMAGE", "VIDEO", "AUDIO"}:
            return value
        try:
            return str(decimal(value))
        except PricingError:
            return None
    return None


def extract(extractor, raw):
    raw = as_dict(raw)
    if extractor == "components":
        return numbers(raw) or {}
    if extractor == "omni":
        # Interactions separates thought tokens from the modality output total.
        inputs = decimal(raw.get("total_input_tokens"))
        outputs = decimal(raw.get("total_output_tokens"))
        thoughts = decimal(raw.get("total_thought_tokens"))
        if raw.get("total_tool_use_tokens") is not None and decimal(raw["total_tool_use_tokens"]) != 0:
            raise PricingError("Omni tool usage needs a separately verified billing rule")
        if raw.get("total_tokens") is not None and decimal(raw["total_tokens"]) != inputs + outputs + thoughts:
            raise PricingError("Omni total usage disagrees with modality and thought counts")
        modalities = raw.get("output_tokens_by_modality")
        if not isinstance(modalities, list) or not modalities:
            raise PricingError("Omni output modality usage is missing")
        counts = {}
        for item in modalities:
            name = str(item.get("modality", "")).lower()
            if name not in {"video", "text"} or name in counts:
                raise PricingError("Unknown or duplicate Omni output modality")
            counts[name] = decimal(item.get("tokens"))
        if sum(counts.values()) != outputs:
            raise PricingError("Omni output modality totals disagree")
        return {"input_tokens": str(inputs), "video_tokens": str(counts.get("video", 0)),
                "text_output_tokens": str(counts.get("text", 0) + thoughts)}
    if extractor in {"openai_image", "openai_image_mixed"}:
        details = as_dict(raw.get("input_tokens_details"))
        text = decimal(details.get("text_tokens"))
        images = decimal(details.get("image_tokens"))
        if text + images != decimal(raw.get("input_tokens")):
            raise PricingError("Image input modality totals disagree")
        cached = decimal(details.get("cached_tokens", raw.get("cached_tokens", 0)))
        cache_details = as_dict(details.get("cached_tokens_details", raw.get("cached_tokens_details")))
        ct = decimal(cache_details.get("text_tokens", 0 if cached == 0 else None))
        ci = decimal(cache_details.get("image_tokens", 0 if cached == 0 else None))
        if ct + ci != cached or ct > text or ci > images:
            raise PricingError("Image cache modality totals disagree")
        result = {"text_input_tokens": str(text - ct), "image_input_tokens": str(images - ci),
                "cached_text_tokens": str(ct), "cached_image_tokens": str(ci),
                "output_tokens": str(decimal(raw.get("output_tokens")))}
        if extractor == "openai_image_mixed":
            output = as_dict(raw.get("output_tokens_details"))
            image_output = decimal(output.get("image_tokens"))
            text_output = decimal(output.get("text_tokens"))
            if image_output + text_output != decimal(raw.get("output_tokens")):
                raise PricingError("Image/text output totals disagree")
            result.update(image_output_tokens=str(image_output), text_output_tokens=str(text_output))
        return result
    if extractor in {"openai_text", "openai_text_legacy"}:
        inputs = decimal(raw.get("input_tokens", raw.get("prompt_tokens")))
        outputs = decimal(raw.get("output_tokens", raw.get("completion_tokens")))
        details = as_dict(raw.get("input_tokens_details", raw.get("prompt_tokens_details")))
        cached = decimal(details.get("cached_tokens"))
        written = decimal(details.get("cache_write_tokens", 0 if extractor == "openai_text_legacy" else None))
        if extractor == "openai_text_legacy" and written:
            raise PricingError("Unexpected cache-write billing for a profile without cache-write prices")
        if cached + written > inputs:
            raise PricingError("Cache usage exceeds total input")
        return {"input_tokens": str(inputs - cached - written), "cached_tokens": str(cached),
                "cache_write_tokens": str(written), "output_tokens": str(outputs),
                "total_input_tokens": str(inputs)}
    raise PricingError(f"No verified usage extractor for {extractor!r}")
