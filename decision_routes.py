"""Authenticated, single-attempt decisions with the Gateway cost journal."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.auth.auth_checks import can_key_call_model
from litellm.proxy._types import ProxyException

from cost_accounting import accounting, identity_for
from decision_contract import DecisionError, MAX_REQUEST_BYTES, MODEL, PROVIDER_URL, ROUTE, validate_request, validate_response
from gateway_accounting import capture, invalidate_caches, state
from pricing_registry import PricingError, registry

class DecisionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def with_auth_receipt(request):
            try:
                return await handler(request)
            except (HTTPException, ProxyException) as exc:
                status = str(getattr(exc, "status_code", getattr(exc, "code", "")))
                # Dependency authentication runs before an accounting intent or provider dispatch.
                if status not in {"401", "403"} or (state.get() or {}).get("accounting_id"):
                    raise
                code = "GATEWAY_AUTHENTICATION_REQUIRED" if status == "401" else "MODEL_ACCESS_DENIED"
                message = "Gateway authentication is required." if status == "401" else "This key cannot use the decision model."
                return _error(code, message, int(status))
        return with_auth_receipt


router = APIRouter(tags=["decisions"], route_class=DecisionRoute)
log = logging.getLogger("ai_gateway.decisions")


async def decision_auth(user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    try:
        yield user
    finally:
        from litellm.proxy.hooks.proxy_track_cost_callback import _release_budget_reservation
        try:
            await _release_budget_reservation(getattr(user, "budget_reservation", None))
        except Exception:
            log.error("decision_budget_release_failed")


async def model_allowed(user):
    from litellm.proxy import proxy_server
    try:
        await can_key_call_model(model=MODEL, llm_model_list=proxy_server.llm_model_list,
                                 valid_token=user, llm_router=proxy_server.llm_router)
        return True
    except ProxyException:
        return False


def decision_profile():
    profile = registry().select(MODEL, ROUTE)
    if profile["upstream_model"] != MODEL or profile["vendor"] != "typesafe":
        raise PricingError("Decision pricing must name the pinned TypeSafe model")
    registry().check_context(profile, {"api_base": "https://api.typesafe.ai/v1"})
    return profile


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", value) else None


def _evidence(payload, headers):
    body = payload if isinstance(payload, dict) else {}
    return {"usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
            "model": _identifier(body.get("model")),
            "id": _identifier(headers.get("request-id") or headers.get("x-request-id") or body.get("id"))}


def _error(code, message, status, **extra):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}, "provider_requests": 0, **extra})


@router.get("/v1/decisions/models")
async def decision_models(user: UserAPIKeyAuth = Depends(decision_auth)):
    if not user.api_key:
        raise HTTPException(401, detail="Gateway authentication is required.")
    # This catalog is separate from generation discovery and never calls TypeSafe.
    if not await model_allowed(user):
        return {"object": "list", "data": []}
    available = bool(os.environ.get("JEV_API_KEY", "").strip())
    reason = None if available else "PROVIDER_NOT_CONFIGURED"
    try:
        decision_profile()
    except PricingError:
        available, reason = False, "PRICING_UNVERIFIED"
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "typesafe",
        "node_type": "Decision", "request_profile": "system_one", "surfaces": ["assistant"],
        "endpoint": "/v1/decisions", "question_types": ["noul", "choice", "score"],
        "available": available, "unavailable_reason": reason}]}


@router.post("/v1/decisions")
async def decisions(request: Request, user: UserAPIKeyAuth = Depends(decision_auth)):
    if not user.api_key:
        raise HTTPException(401, detail="Gateway authentication is required.")
    if not await model_allowed(user):
        raise HTTPException(403, detail={"code": "MODEL_ACCESS_DENIED", "message": "This key cannot use the decision model."})
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return _error("INVALID_CONTENT_TYPE", "Use application/json.", 415)
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return _error("DECISION_REQUEST_TOO_LARGE", "The decision request is too large.", 413)
        payload = validate_request(json.loads(body))
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, DecisionError):
            return _error(exc.code, str(exc), exc.status_code)
        return _error("INVALID_DECISION_REQUEST", "The request must be valid JSON.", 422)

    key = os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        return _error("PROVIDER_NOT_CONFIGURED", "The decision provider is not configured.", 503)
    try:
        profile = decision_profile()
        identity = identity_for(user)
        await accounting.check_budgets(identity, MODEL)
        accounting_id = await accounting.begin(model=MODEL, route=ROUTE, identity=identity)
        current = state.get()
        if current is None:
            current = {}
            state.set(current)
        # Native TypeSafe responses preserve model identity, unlike rewritten
        # OpenAI-compatible aliases. Leave alias absent so capture retains it.
        current.update(accounting_id=accounting_id, profile=profile, route=ROUTE, identity=identity,
                       reservation=getattr(user, "budget_reservation", None))
        attempt_id = await accounting.attempt(accounting_id, profile)
        current["attempts"] = [attempt_id]
    except PricingError:
        return _error("PRICING_UNVERIFIED", "Decision pricing or account admission is unavailable.", 503)
    except Exception:
        log.error("decision_accounting_admission_failed")
        return _error("ACCOUNTING_UNAVAILABLE", "Decision accounting is temporarily unavailable.", 503)

    output, error, evidence, captured_outcome = None, None, {}, None
    outcome = "error"
    try:
        # No redirects, retries, tools, or fallback model. One request = one hop.
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as client:
            response = await client.post(PROVIDER_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload)
        try:
            upstream = response.json()
        except ValueError:
            upstream = None
        evidence = _evidence(upstream, response.headers)
        captured_outcome = "success" if response.is_success else "error"
        await capture(attempt_id, evidence, outcome=captured_outcome)
        if response.is_success:
            try:
                output = validate_response(upstream, payload)
                outcome = "success"
            except DecisionError as exc:
                log.warning("decision_response_invalid: %s", getattr(exc, "validation_reason", "typed_contract"))
                error = (exc.code, str(exc), exc.status_code)
        else:
            status = response.status_code
            error = ("DECISION_PROVIDER_UNAVAILABLE", "The decision provider could not complete this request.", 503)
            if status == 429:
                error = ("DECISION_PROVIDER_RATE_LIMITED", "The decision provider is busy. Try again later.", 429)
            elif status == 422:
                error = ("DECISION_PROVIDER_REJECTED", "The decision provider rejected this request.", 422)
    except httpx.RequestError:
        outcome = "unknown"
        error = ("DECISION_OUTCOME_UNKNOWN", "The decision request was interrupted; it was not retried.", 502)
    except asyncio.CancelledError:
        outcome = "unknown"
        raise
    finally:
        # Retain metering even when the typed response cannot be acted on.
        if captured_outcome != outcome:
            await capture(attempt_id, evidence, outcome=outcome)
        try:
            await accounting.finish(accounting_id)
            await invalidate_caches()
        except Exception:
            log.error("decision_accounting_finalize_failed", extra={"accounting_id": accounting_id})
    receipt = {"accounting_id": accounting_id, "cost_status": "pending", "cost_usd": None, "provider_requests": 1}
    try:
        stored = await accounting.get(accounting_id)
        if stored:
            receipt.update({k: stored[k] for k in ("cost_status", "cost_usd", "cost_source", "pricing_version", "breakdown") if k in stored})
    except Exception:
        log.error("decision_receipt_read_failed", extra={"accounting_id": accounting_id})
    receipt["provider"] = "typesafe"
    if evidence.get("id"):
        receipt["provider_request_id"] = evidence["id"]
    if error:
        return _error(*error, **receipt)
    return {**output, **receipt}
