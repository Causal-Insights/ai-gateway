"""Pinned LiteLLM accounting integration with durable execution receipts."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from contextvars import ContextVar
from importlib.metadata import version

from fastapi import APIRouter, Depends, HTTPException
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import ImageResponse
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

from accounting_usage import as_dict, numbers
from cost_accounting import accounting, canonical_key, identity_for
from pricing_registry import PricingError, registry

log = logging.getLogger("ai_gateway.accounting")
state = ContextVar("gateway_accounting", default=None)
router = APIRouter(tags=["costs"])
CALL_TYPES = {"acompletion": "completion", "completion": "completion", "text_completion": "completion",
              "aresponses": "responses", "responses": "responses", "image_generation": "image_generation",
              "aimage_generation": "image_generation", "image_edit": "image_edit", "aimage_edit": "image_edit",
              "aspeech": "speech", "speech": "speech"}
BILLING_OPTIONS = {"service_tier", "resolution", "quality", "generate_audio", "duration_seconds", "size",
                   "profile_id", "tools", "modalities", "background", "audio", "web_search_options",
                   "n", "output_count", "input_video", "operation", "render_quality", "frame_rate",
                   "cache_control", "prompt_cache_retention", "reasoning_effort", "layer_decomposition"}


def options_for(data):
    data = {**(data.get("extra_body") or {}), **(data.get("optional_params") or {}), **data}
    options = {key: data[key] for key in BILLING_OPTIONS if key in data}
    # Tools/content are never persisted. Their presence selects a billing profile.
    # Client function declarations have no separate provider tool charge.
    tools = options.get("tools") or []
    options["tools"] = bool(options.get("web_search_options") or tools is True or any(
        isinstance(item, dict) and item.get("type") not in {None, "function", "custom"}
        for item in (tools if isinstance(tools, list) else [])))
    options["audio"] = bool(options.get("audio") or "audio" in (options.get("modalities") or []))
    for key, value in (data.get("settings") or {}).items():
        normalized = {"duration": "duration_seconds", "generateAudio": "generate_audio", "renderQuality": "render_quality",
                      "render_quality": "render_quality", "frameRate": "frame_rate", "outputCount": "output_count"}.get(key, key)
        if normalized in BILLING_OPTIONS:
            options[normalized] = value
    if "media_inputs" in data or "media" in data:
        options["input_video"] = any(item.get("type", item.get("kind")) == "video" for item in (data.get("media_inputs") or data.get("media") or []))
    return options


def check_context(profile, deployment=None):
    deployment = deployment or {}
    context = {"project": deployment.get("vertex_project") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
               "location": deployment.get("vertex_location") or ("global" if profile.get("extractor") == "omni" else os.environ.get("GOOGLE_CLOUD_LOCATION")),
               "api_base": (deployment.get("api_base") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")}
    registry().check_context(profile, context)


def route_for_call(call_type):
    name = getattr(call_type, "value", call_type)
    if name not in CALL_TYPES:
        raise PricingError(f"No accounting lifecycle for route {name!r}")
    return CALL_TYPES[name]


def response_evidence(response):
    try:
        data = as_dict(response)
    except Exception:
        # A broken response serializer is not allowed to discard usage.
        data = {name: getattr(response, name, None) for name in ("usage", "model", "id")}
    if isinstance(response, Exception):
        seen = set()
        error = response
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            candidate = as_dict(getattr(error, "body", None))
            provider_response = getattr(error, "response", None)
            if provider_response is not None:
                try:
                    candidate = as_dict(provider_response.json())
                except (ValueError, AttributeError):
                    pass
            if candidate.get("usage"):
                data = candidate
                break
            error = error.__cause__ or error.__context__
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
        data = as_dict(data.get("response"))
    hidden = getattr(response, "_hidden_params", {}) or {}
    evidence = dict(as_dict(hidden.get("gateway_usage") or data.get("usage")))
    if hidden.get("response_cost") is not None:
        evidence["gateway_native_cost_usd"] = hidden["response_cost"]
    if isinstance(data.get("data"), list):
        evidence["output_images"] = sum(bool(item.get("url") or item.get("b64_json"))
                                       for item in data["data"] if isinstance(item, dict))
    provider_response = getattr(response, "response", None)
    headers = getattr(provider_response, "headers", None) or getattr(response, "headers", {}) or {}
    if headers.get("character-cost") is not None:
        evidence["billable_characters"] = headers["character-cost"]
    current = state.get() or {}
    if current.get("route") == "speech" and not isinstance(response, Exception):
        evidence.setdefault("billable_characters", current.get("billable_characters"))
    served_options = {key: data[key] for key in ("service_tier", "resolution", "quality", "generate_audio")
                      if key in data and isinstance(data[key], (str, bool, int, float))}
    served_options.update(hidden.get("gateway_served_options") or {})
    model = hidden.get("gateway_served_model") or data.get("model")
    if model == current.get("alias") and not hidden.get("gateway_served_model"):
        # The proxy rewrites the public response model to the configured alias.
        # That is not new evidence that a different upstream served the request.
        model = None
    return evidence, model, hidden.get("gateway_provider_request_id") or data.get("id") or headers.get("request-id"), served_options or None


def attempt_for(data):
    current = state.get() or {}
    metadata = data.get("metadata") or (data.get("litellm_params") or {}).get("metadata") or {}
    attempt_id = metadata.get("gateway_accounting_attempt_id")
    if attempt_id in current.get("attempts", []):
        return attempt_id
    mapped = current.get("attempt_map", {}).get(data.get("litellm_call_id"))
    if mapped:
        return mapped
    if len(current.get("attempts", [])) == 1:
        return current["attempts"][0]
    # Metadata here has crossed the authenticated admission hook, which removes
    # all client-supplied gateway_accounting fields before assigning this ID.
    if not current and isinstance(attempt_id, str):
        return attempt_id
    return None


def terminal_usage(data):
    if data.get("type") in {"response.completed", "response.failed", "response.incomplete",
                            "image_generation.completed", "image_edit.completed"}:
        return True
    # REST chat usage is authoritative in the final empty-choices chunk. Running
    # totals must not be committed or summed as independent charges.
    return bool(data.get("usage") and data.get("choices") == [])


async def capture(attempt_id, response, *, outcome="success"):
    try:
        raw, model, provider_id, served_options = response_evidence(response)
        current = state.get() or {}
        if (getattr(response, "_hidden_params", {}) or {}).get("gateway_pending") and not raw:
            return
        if current.get("legacy_poll"):
            # Poll requests do not carry the original media inputs/options.
            served_options = {**current.get("legacy_served_options", {}), **{k:v for k,v in (served_options or {}).items() if v is not None and k != "input_video"}}
        profile = current.get("profile") or {}
        if not profile:
            from cost_accounting import decode
            try:
                pool = await accounting.pool()
                stored = decode(await pool.fetchrow("select profile from gateway_cost_attempts where attempt_id=$1", attempt_id))
                profile = (stored or {}).get("profile") or {}
            except Exception:
                # observe emits the numeric recovery receipt before touching DB.
                # A failed profile lookup must not bypass that receipt.
                pass
        if profile.get("hosted_contract"):
            from hosted_tools import measured_tools, parent_subtotal
            raw.update(measured_tools(response))
            subtotal = parent_subtotal(profile, raw, served_options)
            if subtotal is not None:
                raw["gateway_hosted_parent_cost_usd"] = subtotal
        if profile.get("grounding_contract"):
            from grounded_pricing import measured, parent_subtotal
            observed = measured(profile, response)
            raw.update(observed)
            subtotal = parent_subtotal(profile, response)
            if subtotal is not None:
                raw["gateway_grounding_parent_cost_usd"] = subtotal
            counts = {}
            for name, value in observed["gateway_grounding_counts"].items():
                try:
                    from pricing_registry import decimal
                    number = decimal(value)
                    if number == number.to_integral_value():
                        counts[name] = int(number)
                except PricingError:
                    pass
            target = getattr(response, "response", None) or response
            if isinstance(target, dict) and target.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                target = target.get("response") or target
            if isinstance(target, dict):
                target["gateway_grounding_usage"] = counts
                target["gateway_grounding_usage_source"] = observed["gateway_grounding_usage_source"]
            elif hasattr(target, "model_dump"):
                target.gateway_grounding_usage = counts
                target.gateway_grounding_usage_source = observed["gateway_grounding_usage_source"]
        if profile.get("hosted_contract") or profile.get("batch_contract") or profile.get("grounding_contract"):
            raw.pop("gateway_native_cost_usd", None)
        if current.get("resource_owner"):
            from hosted_tools import retain_resources
            try:
                await retain_resources(response, current)
            except Exception:
                log.exception("hosted_resource_retention_pending")
        if as_dict(response).get("status") in {"queued", "in_progress"} and current.get("route") == "responses":
            return
        if profile.get("engine") == "litellm" and raw and not isinstance(response, Exception):
            from litellm_pricing import cost_using_response
            try:
                amount = cost_using_response(profile, response, served_options=served_options)
                if amount is not None:
                    raw["gateway_native_cost_usd"] = amount
            except Exception:
                # Streaming/partial shapes can still be priced from numeric usage.
                log.debug("Native response calculator unavailable; using usage", exc_info=True)
        async def persist():
            for retry in range(3):
                try:
                    return await accounting.observe(attempt_id, raw_usage=raw, served_model=model,
                        provider_request_id=provider_id, outcome=outcome, served_options=served_options,
                        zero_reason="gateway_response_cache" if (getattr(response, "_hidden_params", {}) or {}).get("cache_hit") is True else None)
                except Exception:
                    if retry == 2:
                        raise
                    await asyncio.sleep(.1 * (retry + 1))
        await asyncio.shield(persist())
    except Exception:
        # Do not turn a successfully billed response into a retry at the provider.
        # The persisted pending intent is visible; stored evidence can be retried.
        log.exception("cost_capture_failed", extra={"attempt_id": attempt_id})


async def bind_legacy_task(task_id, payload):
    """Save the accepted provider ID immediately, before any long polling."""
    current = state.get() or {}
    if not current.get("attempts"):
        return
    pool = await accounting.pool()
    options = {"resolution": payload.get("resolution", "480p"),
               "input_video": any(x.get("type") == "video_url" for x in payload.get("content", []))}
    from cost_accounting import encode
    await pool.execute("""update gateway_cost_attempts set provider_request_id=$2,served_options=$3::jsonb
        where attempt_id=$1 and observed_at is null""", current["attempts"][-1], task_id, encode(options))


async def recover_unmapped(request_data, response, *, outcome="success"):
    """Last-resort execution capture; a lost hook mapping is not a logging gate."""
    import hashlib
    from uuid import uuid4
    from cost_accounting import encode
    from accounting_usage import numbers
    current = state.get() or {}
    raw, model, provider_id, options = response_evidence(response)
    call_id = request_data.get("litellm_call_id") or request_data.get("id") or provider_id or uuid4().hex
    suffix = hashlib.sha256(str(call_id).encode()).hexdigest()
    accounting_id = current.get("accounting_id") or "cost_recovered_" + suffix
    profile = current.get("profile")
    alias = current.get("alias") or request_data.get("model") or model or "unknown"
    if not profile:
        try:
            profile = registry().select(alias, "completion")
        except Exception:
            profile = {"version": "execution-recovery-v1", "upstream_model": model or alias,
                       "status": "unverified", "extractor": "components", "zero_reasons": []}
    attempt_id = "attempt_recovered_" + suffix
    # Even if mapping AND database access are lost, retain the numeric response.
    # Recovery must never turn a completed generation into a client retry.
    print(encode({"event": "gateway_execution_receipt", "attempt_id": attempt_id,
        "accounting_id": accounting_id, "model": alias,
        "evidence": {"raw_usage": numbers(raw) or {}, "served_model": model,
                     "provider_request_id": provider_id, "served_options": options, "outcome": outcome}}), flush=True)
    try:
        pool = await accounting.pool()
        await pool.execute("""insert into gateway_cost_requests(accounting_id,owner_key_hash,identity,model,route)
            values($1,'','{}'::jsonb,$2,'completion') on conflict do nothing""", accounting_id, alias)
        await accounting.attempt(accounting_id, profile, attempt_id=attempt_id)
        if current.get("accounting_id") and attempt_id not in current.setdefault("attempts", []):
            current["attempts"].append(attempt_id)
        await capture(attempt_id, response, outcome=outcome)
        await accounting.finish(accounting_id)
    except Exception:
        log.exception("cost_unmapped_recovery_pending", extra={"attempt_id": attempt_id})
    return attempt_id


class GatewayAccounting(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        resource_owner = None
        try:
            route = route_for_call(call_type)
            if any(data.get(key) for key in ("api_base", "api_key", "vertex_project", "vertex_location", "vertex_credentials")):
                raise PricingError("Request-level provider/account overrides require a separate verified deployment")
            profile = registry().select(data.get("model"), route, options_for(data))
            if profile.get("grounding_tariff"):
                from grounded_pricing import pin_contract as pin_grounding
                profile = pin_grounding(profile, data)
            if route == "responses" and profile.get("vendor") == "openai":
                from hosted_tools import pin_contract, validate_resources
                resource_owner = await validate_resources(user_api_key_dict, data)
                profile = pin_contract(profile, data, registry().document)
            check_context(profile)
            identity = identity_for(user_api_key_dict, (data.get("metadata") or {}).get("tags"))
            resumed = None
            if str(data.get("model", "")).startswith("seedance-"):
                from custom_handler_seedance import SeedanceLLM
                task_id = SeedanceLLM()._extract_poll_task_id({**(data.get("extra_body") or {}), **data}, data.get("prompt", ""))
                if task_id:
                    pool = await accounting.pool()
                    from cost_accounting import decode
                    resumed = decode(await pool.fetchrow("""select a.*,r.accounting_id from gateway_cost_attempts a
                        join gateway_cost_requests r using(accounting_id)
                        where a.provider_request_id=$1 and r.owner_key_hash=$2 and r.model=$3
                        order by a.created_at limit 1""", task_id, identity["api_key_hash"], data["model"]))
            await accounting.check_budgets(identity, data["model"])
            accounting_id = resumed["accounting_id"] if resumed else await accounting.begin(model=data["model"], route=route, identity=identity)
            if resumed:
                profile = resumed["profile"]
        except PricingError as exc:
            raise HTTPException(503, detail={"code": exc.code, "message": str(exc)}) from exc
        current = state.get()
        if current is None:
            current = {}
            state.set(current)
        current.update(accounting_id=accounting_id, profile=profile, alias=data["model"], route=route,
                       options=options_for(data), identity=identity, attempts=[], attempt_map={}, reservation=user_api_key_dict.budget_reservation)
        if resource_owner:
            current["resource_owner"] = resource_owner
        if resumed:
            current.update(legacy_poll=True, attempts=[resumed["attempt_id"]], legacy_served_options=resumed.get("served_options") or {})
        if route == "speech" and isinstance(data.get("input"), str):
            # ElevenLabs explicitly bills submitted text characters. Prefer its
            # character-cost response header when provided (e.g. paid voices).
            current["billable_characters"] = len(data["input"])
        # Client metadata must never select an accounting record or billing key.
        metadata = dict(data.get("metadata") or {})
        for name in list(metadata):
            if name.startswith("gateway_accounting"):
                del metadata[name]
        data["metadata"] = metadata
        # Hidden SDK retries cannot supply a separate durable intent. Unknown
        # outcomes must be reconciled; clients may explicitly submit a new request.
        data["num_retries"] = 0
        data["max_retries"] = 0
        if data.get("stream") and route == "completion":
            data["stream_options"] = {**(data.get("stream_options") or {}), "include_usage": True}
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        if getattr(call_type, "value", call_type) in {"avideo_status", "avideo_content", "video_status", "video_content"}:
            return kwargs
        current = state.get()
        if current and current.get("durable_job"):
            # Durable adapters already hold a priced, persisted attempt. The
            # selected upstream must still match; status/content calls are reads.
            if getattr(call_type, "value", call_type) in {"avideo_status", "avideo_content"}:
                return kwargs
            expected = current["profile"].get("deployment_model", current["profile"]["upstream_model"])
            model = kwargs.get("model", "")
            if model not in {expected, expected.split("/", 1)[-1], current["alias"]}:
                raise PricingError("Durable deployment model changed")
            return kwargs
        if not current or not current.get("accounting_id"):
            raise PricingError("Provider submission requires an authenticated durable cost intent")
        route = route_for_call(call_type)
        expected = current["profile"].get("deployment_model", current["profile"]["upstream_model"])
        model = kwargs.get("model", "")
        if model not in {expected, expected.split("/", 1)[-1]}:
            raise PricingError("Fallback deployment has no verified pricing contract")
        if current.get("legacy_poll"):
            kwargs["metadata"] = {**(kwargs.get("metadata") or {}), "gateway_accounting_attempt_id": current["attempts"][0]}
            return kwargs
        registry().select(current["alias"], route, current["options"], upstream=expected)
        check_context(current["profile"], kwargs)
        attempt_id = await accounting.attempt(current["accounting_id"], current["profile"])
        current["attempts"].append(attempt_id)
        current.setdefault("attempt_map", {})[kwargs.get("litellm_call_id")] = attempt_id
        kwargs["metadata"] = {**(kwargs.get("metadata") or {}), "gateway_accounting_attempt_id": attempt_id}
        kwargs["num_retries"] = 0
        kwargs["max_retries"] = 0
        return kwargs

    async def async_post_call_success_deployment_hook(self, request_data, response, call_type):
        attempt_id = attempt_for(request_data)
        if attempt_id and (not request_data.get("stream") or isinstance(response, ImageResponse)):
            await capture(attempt_id, response)
        return response

    async def async_post_call_streaming_deployment_hook(self, request_data, response_chunk, call_type):
        attempt_id = attempt_for(request_data)
        data = as_dict(response_chunk)
        if attempt_id and terminal_usage(data):
            await capture(attempt_id, response_chunk, outcome="failure" if data.get("type") in {"response.failed", "response.incomplete"} else "success")
        return response_chunk

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        current = state.get()
        # Custom image adapters may collect provider SSE into a concrete result.
        # Such results never run the streaming iterator finalizer.
        buffered = not data.get("stream") or isinstance(response, ImageResponse)
        if current and current.get("accounting_id") and buffered:
            try:
                # A provider cache hit can precede the deployment hook entirely.
                if not current.get("attempts") and (getattr(response, "_hidden_params", {}) or {}).get("cache_hit") is True:
                    current["attempts"].append(await accounting.attempt(current["accounting_id"], current["profile"]))
                if not current.get("attempts"):
                    await recover_unmapped(data, response)
                if current.get("attempts"):
                    last = current["attempts"][-1]
                    await capture(last, response)
                await accounting.finish(current["accounting_id"])
                await finalize_reservation(current)
            except Exception:
                log.exception("cost_request_finalization_pending", extra={"accounting_id": current["accounting_id"]})
        elif buffered:
            await recover_unmapped(data, response)
        return response

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        current = state.get()
        if current and current.get("accounting_id"):
            try:
                # A failed HTTP/provider call can still report billable usage.
                # An outer failure belongs to the last attempted call; applying
                # its usage to earlier retries would duplicate the charge.
                for attempt_id in current.get("attempts", [])[-1:]:
                    await capture(attempt_id, original_exception, outcome="failure")
                await accounting.finish(current["accounting_id"])
                await finalize_reservation(current)
            except Exception:
                log.exception("cost_request_finalization_pending", extra={"accounting_id": current["accounting_id"]})
        elif response_evidence(original_exception)[0]:
            await recover_unmapped(request_data, original_exception, outcome="failure")

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        current = state.get()
        try:
            async for chunk in response:
                # Responses streaming does not always use CustomStreamWrapper.
                if current and current.get("attempts"):
                    data = as_dict(chunk)
                    if terminal_usage(data) and not (getattr(chunk, "_hidden_params", {}) or {}).get("gateway_usage_unverified"):
                        await capture(current["attempts"][-1], chunk,
                                      outcome="failure" if data.get("type") in {"response.failed", "response.incomplete"} else "success")
                yield chunk
        finally:
            if current and current.get("accounting_id"):
                # Shield final bookkeeping from client disconnect cancellation.
                async def finish():
                    try:
                        await mark_incomplete(current, "incomplete")
                        await accounting.finish(current["accounting_id"])
                        await finalize_reservation(current)
                    except Exception:
                        log.exception("cost_request_finalization_pending", extra={"accounting_id": current["accounting_id"]})
                await asyncio.shield(finish())


async def mark_incomplete(current, outcome):
    pool = await accounting.pool()
    for attempt_id in current.get("attempts", []):
        row = await pool.fetchrow("select observed_at from gateway_cost_attempts where attempt_id=$1", attempt_id)
        if row and row["observed_at"] is None:
            await capture(attempt_id, {}, outcome=outcome)


async def finalize_reservation(current):
    from litellm.proxy.hooks.proxy_track_cost_callback import _release_budget_reservation
    await _release_budget_reservation(current.get("reservation"))
    await invalidate_caches()


async def invalidate_caches():
    """Retryable after-commit invalidation; never apply a monetary increment twice."""
    from litellm.proxy import proxy_server
    pool = await accounting.pool()
    rows = await pool.fetch("select attempt_id,identity from gateway_cost_cache_outbox order by created_at limit 100")
    from cost_accounting import decode
    for raw in rows:
        row = decode(raw)
        identity = row["identity"]
        keys = [identity.get("api_key_hash"), identity.get("user_id"), identity.get("proxy_budget_id")]
        keys += [f"tag:{tag}" for tag in identity.get("tags", [])]
        keys += [f"{name}:{identity[field]}" for name, field in
                 (("team_id", "team_id"), ("org_id", "org_id"), ("end_user_id", "end_user_id"), ("project_id", "project_id")) if identity.get(field)]
        for key in filter(None, keys):
            await proxy_server.user_api_key_cache.async_delete_cache(key=key)
        # Spend counters are read authoritatively by the installed getter below.
        await pool.execute("delete from gateway_cost_cache_outbox where attempt_id=$1", row["attempt_id"])


@router.get("/v1/costs/{accounting_id}")
async def get_cost(accounting_id: str, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    key = canonical_key(user.api_key)
    result = await accounting.get(accounting_id, key)
    if result is None:
        # Legacy generation ownership intentionally remains double-hashed.
        import hashlib
        result = await accounting.get(accounting_id, hashlib.sha256(str(user.api_key).encode()).hexdigest())
    if result is None:
        raise HTTPException(404, "Cost record not found")
    return result


@router.get("/v1/costs/reconciliation/report")
async def reconciliation_report(user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    role = getattr(user.user_role, "value", user.user_role)
    if role not in {"proxy_admin"}:
        raise HTTPException(403, "Proxy administrator required")
    return await accounting.report()


class AccountingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        current = {}
        token = state.set(current)

        async def cost_send(message):
            if message["type"] == "http.response.start" and current.get("accounting_id"):
                message = dict(message)
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in
                           {b"x-litellm-response-cost", b"x-gateway-accounting-id", b"x-gateway-cost-status"}]
                try:
                    cost = await accounting.get(current["accounting_id"])
                except Exception:
                    log.exception("cost_header_lookup_pending", extra={"accounting_id": current["accounting_id"]})
                    cost = None
                headers.extend([(b"x-gateway-accounting-id", current["accounting_id"].encode()),
                                (b"x-gateway-cost-status", (cost or {}).get("cost_status", "pending").encode())])
                if cost and cost["cost_usd"] is not None:
                    headers.append((b"x-litellm-response-cost", cost["cost_usd"].encode()))
                message["headers"] = headers
            await send(message)
        try:
            await self.app(scope, receive, cost_send)
        finally:
            state.reset(token)


def install_stream_evidence_hooks():
    """Retain terminal provider usage before LiteLLM normalizes/synthesizes it.

    LiteLLM's completed stream can rebuild Usage and omit provider-specific
    charge fields. Its synthesized token estimates must never attest to cost.
    Capture numeric terminal evidence synchronously, then persist it in the
    awaited finalizer. Responses/image streams use their native terminal events.
    """
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    if getattr(CustomStreamWrapper, "_gateway_usage_capture", False):
        return
    create = CustomStreamWrapper.chunk_creator
    finalize = CustomStreamWrapper._finalize_completed_stream

    @functools.wraps(create)
    def chunk_creator(self, chunk, *args, **kwargs):
        raw = as_dict(chunk)
        choices = raw.get("choices")
        has_content = bool(raw.get("text")) or any(
            any((choice.get("delta") or {}).get(key) for key in
                ("content", "tool_calls", "function_call", "reasoning_content", "reasoning", "audio"))
            for choice in (choices or []))
        # The HTTP iterator can insert an empty synthetic choice while converting
        # the provider's usage-only chunk. Its Usage still contains the original
        # numeric fields at this boundary. Any later content invalidates it as a
        # terminal total; a later usage-only chunk may establish a new total.
        if has_content:
            self._gateway_terminal_evidence = None
        is_google = self.custom_llm_provider in {"vertex_ai", "gemini", "vertex_ai_beta"}
        if (state.get() and self.custom_llm_provider in {"openai", "xai", "vertex_ai", "gemini", "vertex_ai_beta"}
                and isinstance(choices, list) and (is_google or not has_content) and raw.get("usage")):
            # Keep the latest total once, never sum repeated/running totals.
            self._gateway_terminal_evidence = {"usage": numbers(raw["usage"]),
                "model": raw.get("model"), "id": raw.get("id")}
        return create(self, chunk, *args, **kwargs)

    @functools.wraps(finalize)
    async def finalizer(self, *args, **kwargs):
        evidence = getattr(self, "_gateway_terminal_evidence", None)
        attempt_id = attempt_for(self.logging_obj.model_call_details) if self.logging_obj else None
        current = state.get() or {}
        # Some native providers copy logging metadata before deployment hooks.
        # With retries disabled a single intent is unambiguous. Multiple intents
        # still require the explicit mapping; never guess which attempt to bill.
        if not attempt_id and len(current.get("attempts", [])) == 1:
            attempt_id = current["attempts"][0]
        if evidence and attempt_id and not getattr(self, "_gateway_evidence_observed", False):
            await capture(attempt_id, evidence)
            self._gateway_evidence_observed = True
        result = await finalize(self, *args, **kwargs)
        if state.get():
            hidden = getattr(result, "_hidden_params", {})
            if evidence:
                hidden.update(gateway_usage=evidence["usage"], gateway_served_model=evidence["model"],
                              gateway_provider_request_id=evidence["id"])
            else:
                hidden["gateway_usage_unverified"] = True
        return result

    CustomStreamWrapper.chunk_creator = chunk_creator
    CustomStreamWrapper._finalize_completed_stream = finalizer
    CustomStreamWrapper._gateway_usage_capture = True


def install():
    if version("litellm") != "1.102.1":
        raise RuntimeError("Accounting integration must be revalidated before upgrading LiteLLM")
    import litellm
    from litellm_pricing import install_catalog
    install_catalog()
    litellm.num_retries = 0
    litellm.DEFAULT_MAX_RETRIES = 0
    install_stream_evidence_hooks()
    from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger, _release_budget_reservation
    if not any(isinstance(item, GatewayAccounting) for item in litellm.callbacks):
        litellm.callbacks.append(callback)
    if getattr(_ProxyDBLogger, "_gateway_writer", False):
        return
    async def execution_success(self, kwargs, response_obj, start_time, end_time):
        attempt_id = attempt_for(kwargs)
        if attempt_id:
            # Backup for routes where an awaited SDK hook was bypassed. Reuse
            # the same execution ID; duplicate receipts cannot increment twice.
            await capture(attempt_id, response_obj)
            return
        if (state.get() or {}).get("accounting_id"):
            log.error("execution_attempt_mapping_missing")
            return await recover_unmapped(kwargs, response_obj)
        # Unmapped traffic is still an execution, not a reason to suppress logs.
        return await recover_unmapped(kwargs, response_obj)
    async def execution_track(self, *args, **kwargs):
        data = args[0] if args else kwargs.get("kwargs", {})
        response = args[1] if len(args) > 1 else kwargs.get("completion_response")
        return await execution_success(self, data, response, None, None)
    async def execution_failure(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        attempt_id = attempt_for(request_data)
        if attempt_id:
            await capture(attempt_id, original_exception, outcome="failure")
        elif response_evidence(original_exception)[0]:
            await recover_unmapped(request_data, original_exception, outcome="failure")
        await _release_budget_reservation(user_api_key_dict.budget_reservation)
    _ProxyDBLogger.async_log_success_event = execution_success
    _ProxyDBLogger._PROXY_track_cost_callback = execution_track
    _ProxyDBLogger.async_post_call_failure_hook = execution_failure
    _ProxyDBLogger._gateway_writer = True
    from litellm.proxy import proxy_server
    async def fresh_floor(counter_key, window_entity_type=None, window_entity_id=None,
                          window_duration=None, window_start=None):
        if counter_key.startswith(proxy_server.END_USER_COUNTER_PREFIX):
            return await proxy_server.SpendCounterReseed.end_user_from_db(
                prisma_client=proxy_server.prisma_client, counter_key=counter_key)
        amount = await proxy_server.SpendCounterReseed.from_db(
            prisma_client=proxy_server.prisma_client, counter_key=counter_key)
        if amount is None and window_entity_type and window_entity_id and window_start:
            # Gateway commits spend logs itself; LiteLLM's separate window table
            # is not maintained by this writer. Read the committed journal.
            amount = await proxy_server.SpendCounterReseed.window_from_spend_logs(
                prisma_client=proxy_server.prisma_client, entity_type=window_entity_type,
                entity_id=window_entity_id, window_start=window_start)
        return amount
    # Preserve reservation-aware counters, but always compare them with committed
    # database totals. This also works across workers without Redis invalidation.
    proxy_server._authoritative_floor_spend = fresh_floor
    proxy_server._fail_closed_budget_enforcement = lambda: True
    # Each outer provider call can fail before the proxy success hook, then be
    # retried by Router. Record the failed attempt before control reaches Router.
    for name in ("acompletion", "aresponses", "aimage_generation", "aimage_edit", "aspeech"):
        original = getattr(litellm, name, None)
        if original is None:
            continue
        def wrap(function):
            @functools.wraps(function)
            async def call(*args, **kwargs):
                current = state.get()
                before = len(current.get("attempts", [])) if current else 0
                try:
                    response = await function(*args, **kwargs)
                    # This awaited boundary is independent of LiteLLM logging
                    # flags and callback scheduling. Never trust a fire-and-forget
                    # callback as the only record of a completed provider call.
                    if current and not kwargs.get("stream"):
                        attempts = current.get("attempts", [])[before:]
                        if not attempts and current.get("legacy_poll"):
                            attempts = current.get("attempts", [])
                        if len(attempts) == 1:
                            await capture(attempts[0], response)
                        elif not current.get("attempts") and not current.get("durable_job"):
                            await recover_unmapped(kwargs, response)
                    elif not current and not kwargs.get("stream") and response_evidence(response)[0]:
                        await recover_unmapped(kwargs, response)
                    return response
                except BaseException as exc:
                    if current:
                        try:
                            for attempt_id in current.get("attempts", [])[before:]:
                                await asyncio.shield(capture(attempt_id, exc, outcome="failure"))
                        except Exception:
                            log.exception("cost_capture_failed", extra={"accounting_id": current.get("accounting_id")})
                    raise
            return call
        setattr(litellm, name, wrap(original))


callback = GatewayAccounting()
