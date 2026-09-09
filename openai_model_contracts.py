"""Exact OpenAI request contracts. No moving family-name capability inference."""

ASTRA_MODELS = {"gpt-6-astra", "openai/gpt-6-astra", "gpt-latest"}
IMAGE_MODELS = {
    name: f"{name}-2026-09-08"
    for name in ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare")
}
IMAGE_IDS = set(IMAGE_MODELS) | set(IMAGE_MODELS.values())
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
IMAGE_QUALITIES = {"auto", "low", "medium", "high", "xhigh", "max"}
IMAGE_PRESERVED_FIELDS = ("output_format", "output_compression", "background", "moderation")


def image_model(value):
    return str(value or "").removeprefix("openai/") in IMAGE_IDS


def validate_image_settings(body):
    """Return a public error before submission, including for nested tools."""
    if not isinstance(body.get("quality", "auto"), str) or body.get("quality", "auto") not in IMAGE_QUALITIES:
        return "Unsupported image quality."
    size = body.get("size", "auto")
    if size != "auto":
        import re
        match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", str(size))
        if not match:
            return "Image size must be auto or WIDTHxHEIGHT."
        width, height = map(int, match.groups())
        if (width % 16 or height % 16 or max(width, height) > 3840
                or max(width, height) > 3 * min(width, height)
                or not 655360 <= width * height <= 8294400):
            return "Image dimensions exceed this model's supported bounds."
    fmt = body.get("output_format", "png")
    if not isinstance(fmt, str) or fmt not in {"png", "jpeg", "webp"}:
        return "Unsupported image output format."
    if not isinstance(body.get("background", "auto"), str) or body.get("background", "auto") not in {"auto", "opaque", "transparent"}:
        return "Unsupported image background."
    if body.get("background") == "transparent" and fmt == "jpeg":
        return "Transparent output requires PNG or WebP."
    if "output_compression" in body:
        value = body["output_compression"]
        if fmt == "png" or type(value) is not int or not 0 <= value <= 100:
            return "Compression requires JPEG or WebP and an integer from 0 to 100."
    if "n" in body and (type(body["n"]) is not int or not 1 <= body["n"] <= 4):
        return "Image count must be an integer from 1 to 4."
    if "input_fidelity" in body:
        return "Input fidelity is not a verified control for this model."
    partials = body.get("partial_images", 0)
    if type(partials) is not int or not 0 <= partials <= 3:
        return "Partial image count must be an integer from 0 to 3."
    return None


def validate_astra(body, *, responses):
    unsupported = {"temperature", "top_p", "top_logprobs", "logprobs"}
    if unsupported.intersection(body):
        return "GPT-6 Astra does not support sampling or log-probability overrides."
    includes = body.get("include") or []
    if not isinstance(includes, list):
        return "Include must be an array."
    if "message.output_text.logprobs" in includes:
        return "GPT-6 Astra does not support log probabilities."
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        return "Reasoning must be an object."
    effort = reasoning.get("effort", body.get("reasoning_effort", "medium"))
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        return "Unsupported GPT-6 Astra reasoning effort."
    direct_effort = body.get("reasoning_effort")
    if direct_effort is not None and direct_effort != effort:
        return "Conflicting reasoning efforts."
    if responses:
        body["reasoning"] = {**reasoning, "effort": effort}
        body.pop("reasoning_effort", None)
    else:
        body["reasoning_effort"] = effort
        allowed = body.get("allowed_openai_params") or []
        if not isinstance(allowed, list) or any(not isinstance(value, str) for value in allowed):
            return "allowed_openai_params must be an array of names."
        body["allowed_openai_params"] = list(dict.fromkeys(allowed + ["reasoning_effort", "prompt_cache_options"]))
    tools = body.get("tools") or []
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        return "Tools must be an array of objects."
    if any(tool.get("type") == "image_generation" for tool in tools) and not responses:
        return "Image generation requires the Responses API."
    for tool in tools:
        if tool.get("type") == "image_generation" and image_model(tool.get("model")):
            if "n" in tool:
                return "Image tools produce one image per call; n belongs to the Images API."
            error = validate_image_settings(tool)
            if error:
                return error
            selected = tool["model"].removeprefix("openai/")
            tool["model"] = IMAGE_MODELS.get(selected, selected)
    return None


def install_image_adapters():
    """Fill v1.95's image-edit field gap only for the two pinned models.

    Keep LiteLLM's inference auth, callbacks, request IDs and spend ledger.
    This hook is covered inside the pinned application image.
    """
    from litellm.utils import ProviderConfigManager
    from litellm.types.utils import LlmProviders
    from litellm.llms.openai.image_edit.transformation import OpenAIImageEditConfig

    original = ProviderConfigManager.get_provider_image_edit_config
    if getattr(original, "_image_25_contract", False):
        return

    class Image25EditConfig(OpenAIImageEditConfig):
        def get_supported_openai_params(self, model):
            return super().get_supported_openai_params(model) + [
                "output_format", "output_compression", "moderation"
            ]

    def select(model, provider):
        if provider == LlmProviders.OPENAI and image_model(model):
            return Image25EditConfig()
        return original(model=model, provider=provider)

    select._image_25_contract = True
    ProviderConfigManager.get_provider_image_edit_config = staticmethod(select)

    from litellm.images.main import _get_ImageEditRequestUtils
    utils = _get_ImageEditRequestUtils()
    original_extract = utils.get_requested_image_edit_optional_param

    def extract(params):
        selected = original_extract(params)
        if image_model(params.get("model")):
            for name in ("output_format", "output_compression", "moderation"):
                if params.get(name) is not None:
                    selected[name] = params[name]
        return selected

    utils.get_requested_image_edit_optional_param = staticmethod(extract)

    from litellm.llms.openai.image_generation.gpt_transformation import GPTImageGenerationConfig
    original_generation = ProviderConfigManager.get_provider_image_generation_config

    class Image25GenerationConfig(GPTImageGenerationConfig):
        def get_supported_openai_params(self, model):
            return super().get_supported_openai_params(model) + [
                "gateway_openai_image_" + name for name in IMAGE_PRESERVED_FIELDS
            ]

        def map_openai_params(self, non_default_params, optional_params, model, drop_params):
            mapped = super().map_openai_params(non_default_params, optional_params, model, drop_params)
            for name in IMAGE_PRESERVED_FIELDS:
                private = "gateway_openai_image_" + name
                if private in mapped:
                    mapped[name] = mapped.pop(private)
            return mapped

        def transform_image_generation_response(self, *args, **kwargs):
            response = super().transform_image_generation_response(*args, **kwargs)
            optional = kwargs.get("optional_params", args[5] if len(args) > 5 else {})
            response.output_format = optional.get("output_format", "png")
            return response

    def select_generation(model, provider):
        if provider == LlmProviders.OPENAI and image_model(model):
            return Image25GenerationConfig()
        return original_generation(model=model, provider=provider)

    ProviderConfigManager.get_provider_image_generation_config = staticmethod(select_generation)

    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
    from litellm.responses.utils import ResponsesAPIRequestUtils
    original_responses = ProviderConfigManager.get_provider_responses_api_config
    original_response_extract = ResponsesAPIRequestUtils.get_requested_response_api_optional_param

    class AstraResponsesConfig(OpenAIResponsesAPIConfig):
        def get_supported_openai_params(self, model):
            return super().get_supported_openai_params(model) + ["prompt_cache_options"]

        def should_fake_stream(self, model, stream, custom_llm_provider=None):
            # Astra supports native Responses SSE; the pinned cost map predates it.
            return False

    def select_responses(provider, model=None):
        if provider in (LlmProviders.OPENAI, "openai") and model in ASTRA_MODELS:
            return AstraResponsesConfig()
        return original_responses(provider=provider, model=model)

    def extract_responses(params):
        model = params.get("model")
        cache_options = params.get("prompt_cache_options", params.get("kwargs", {}).get("prompt_cache_options"))
        selected = original_response_extract(params)
        if model in ASTRA_MODELS and cache_options is not None:
            selected["prompt_cache_options"] = cache_options
        return selected

    ProviderConfigManager.get_provider_responses_api_config = staticmethod(select_responses)
    ResponsesAPIRequestUtils.get_requested_response_api_optional_param = staticmethod(extract_responses)

    # Image generation drops these kwargs before its provider-specific mapping.
    # Preserve them at the final optional-parameter boundary, including the SDK
    # retry count (the native OpenAI image handler otherwise defaults to two).
    import importlib
    import litellm.utils as litellm_utils
    images_main = importlib.import_module("litellm.images.main")
    original_generation_params = images_main.get_optional_params_image_gen

    def generation_params(*args, **kwargs):
        selected = original_generation_params(*args, **kwargs)
        model = kwargs.get("model", args[0] if args else None)
        if image_model(model):
            for name in IMAGE_PRESERVED_FIELDS:
                value = kwargs.get("gateway_openai_image_" + name, kwargs.get(name))
                if value is not None:
                    selected[name] = value
                selected.pop("gateway_openai_image_" + name, None)
            selected["max_retries"] = 0
        return selected

    images_main.get_optional_params_image_gen = generation_params
    litellm_utils.get_optional_params_image_gen = generation_params

    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
    original_chat = ProviderConfigManager.get_provider_chat_config

    class AstraChatConfig(OpenAIGPTConfig):
        def get_supported_openai_params(self, model):
            return super().get_supported_openai_params(model) + ["reasoning_effort", "prompt_cache_options"]

        def map_openai_params(self, non_default_params, optional_params, model, drop_params):
            mapped = super().map_openai_params(non_default_params, optional_params, model, drop_params)
            for name in ("reasoning_effort", "prompt_cache_options"):
                if name in non_default_params:
                    mapped[name] = non_default_params[name]
            return mapped

    def select_chat(model, provider, base_model=None):
        if provider == LlmProviders.OPENAI and model in ASTRA_MODELS:
            return AstraChatConfig()
        return original_chat(model=model, provider=provider, base_model=base_model)

    ProviderConfigManager.get_provider_chat_config = staticmethod(select_chat)
