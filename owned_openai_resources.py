"""Owned OpenAI files, finite resource operations, and per-item batch receipts.

Provider creates are sent once. A lost create response leaves a visible intent;
polling or recovery never blindly submits a second batch.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from litellm.proxy._types import ProxyException
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

from cost_accounting import accounting, encode, identity_for
from generation_job_repository import repository
from pricing_registry import PricingError, registry

resource_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@resource_app.middleware('http')
async def owner_cleanup_budget_scope(request, call_next):
    from openai_owner_lifecycle import _owner_data_budget
    token=_owner_data_budget.set(request.url.path == '/v1/resources/owner-data' and request.method in {'GET','DELETE'})
    try:return await call_next(request)
    finally:_owner_data_budget.reset(token)


BATCH_ENDPOINTS = {"/v1/responses": "responses", "/v1/chat/completions": "completion"}
BATCH_FIELDS = {
    "responses": {"model", "input", "instructions", "max_output_tokens", "reasoning", "text", "metadata",
                  "store", "temperature", "top_p", "truncation", "service_tier", "prompt_cache_key", "prompt_cache_options"},
    "completion": {"model", "messages", "max_tokens", "max_completion_tokens", "reasoning_effort", "response_format",
                   "metadata", "store", "temperature", "top_p", "stop", "presence_penalty", "frequency_penalty",
                   "service_tier", "prompt_cache_key", "prompt_cache_options"},
}
TERMINAL = {"completed", "failed", "expired", "cancelled"}
log = logging.getLogger(__name__)


def fail(status, code, message):
    raise HTTPException(status, detail={"code": code, "message": message})


def resource_owner(user, headers):
    metadata = user.metadata or {}
    owner = next((v for k, v in headers.items() if k.lower() == "x-gateway-resource-owner"), "")
    if metadata.get("gateway_resource_delegate") is not True or not metadata.get("gateway_app_id"):
        fail(403, "RESOURCE_DELEGATION_REQUIRED", "This key cannot manage delegated resources.")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
        fail(401, "RESOURCE_OWNER_REQUIRED", "An authenticated resource owner is required.")
    return metadata["gateway_app_id"], owner


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("data", "manifest"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


async def owned(kind, identifier, owner):
    from openai_owner_lifecycle import ensure_active
    await ensure_active(owner)
    pool = await repository.pool()
    row = decode(await pool.fetchrow("""select * from gateway_openai_resources
        where kind=$1 and (id=$2 or provider_id=$2) and app_id=$3 and owner_id=$4""", kind, identifier, *owner))
    if not row or row["state"] == "deleted":
        fail(404, "RESOURCE_NOT_FOUND", "Resource not found.")
    return row


async def remember(kind, provider_id, owner, data):
    pool = await repository.pool()
    await pool.execute("""insert into gateway_openai_resources(id,kind,provider_id,app_id,owner_id,state,data)
        values($1,$2,$3,$4,$5,'ready',$6::jsonb) on conflict(kind,provider_id) do nothing""",
        "resource_" + uuid4().hex, kind, provider_id, *owner, encode(data))
    row=decode(await pool.fetchrow('select * from gateway_openai_resources where kind=$1 and provider_id=$2 and app_id=$3 and owner_id=$4',kind,provider_id,*owner))
    if not row:fail(404, "RESOURCE_NOT_FOUND", "Resource not found.")
    from openai_owner_lifecycle import cleanup_late_resource
    await cleanup_late_resource(owner)
    return row


def public(row):
    return {**row["data"], "id": row.get("provider_id") or row["id"],
            "gateway_resource_id": row["id"], "gateway_state": row["state"]}


async def provider(method, path, **kwargs):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        fail(503, "OPENAI_NOT_CONFIGURED", "The provider is not configured.")
    # Fixed provider origin; clients cannot choose an account, origin or route.
    async with httpx.AsyncClient(timeout=120) as client:
        return await client.request(method, "https://api.openai.com/v1" + path,
                                    headers={"Authorization": "Bearer " + key}, **kwargs)


def upstream_ok(response):
    if response.is_error:
        fail(response.status_code, "PROVIDER_RESOURCE_ERROR", "The provider rejected the resource operation.")
    return response


async def authorize_model(user, model):
    from litellm.proxy.auth.auth_checks import can_key_call_resolved_model
    from litellm.proxy import proxy_server
    await can_key_call_resolved_model(model=model, llm_model_list=proxy_server.llm_model_list,
                                     valid_token=user, llm_router=proxy_server.llm_router)


async def batch_file(content, user, headers=None):
    try:
        lines = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError):
        fail(422, "BATCH_INPUT_INVALID", "Batch input must be UTF-8 JSONL.")
    if not 1 <= len(lines) <= 50000:
        fail(422, "BATCH_INPUT_INVALID", "A batch requires 1 to 50,000 requests.")
    manifest, seen, models = [], set(), set()
    from gateway_request_policy import apply_request_policy
    from gateway_accounting import options_for
    for line in lines:
        if not isinstance(line, dict) or line.get("method") != "POST" or line.get("url") not in BATCH_ENDPOINTS:
            fail(422, "BATCH_ENDPOINT_UNSUPPORTED", "This batch interface supports Responses and Chat Completions.")
        custom_id, body = line.get("custom_id"), line.get("body")
        if not isinstance(custom_id, str) or not custom_id or custom_id in seen or not isinstance(body, dict):
            fail(422, "BATCH_INPUT_INVALID", "Each item needs a unique custom_id and request body.")
        if body.get("stream") or body.get("tools") or body.get("background") or any(body.get(k) for k in ("api_key", "api_base", "extra_body")):
            fail(422, "BATCH_OPTION_UNSUPPORTED", "This batch interface supports finite text requests without streaming or tools.")
        if set(body) - BATCH_FIELDS[BATCH_ENDPOINTS[line["url"]]] or body.get("service_tier", "default") not in {"default", "auto"}:
            fail(422, "BATCH_OPTION_UNSUPPORTED", "Unsupported batch request field or processing tier.")
        alias = body.get("model")
        await authorize_model(user, alias)
        body, error = apply_request_policy(line["url"], body)
        if error:
            fail(422, error.code, error.message)
        body.pop("allowed_openai_params", None)  # SDK compatibility hints are not provider fields.
        body.pop("service_tier", None)  # The batch execution contract selects its processing tier.
        profile = registry().select(alias, BATCH_ENDPOINTS[line["url"]], options_for(body))
        contract = registry().document.get("hosted_tools") or {}
        if profile.get("vendor") != "openai" or alias not in contract.get("batch_models", []):
            fail(422, "BATCH_MODEL_UNVERIFIED", "This model has no verified batch contract.")
        # Body references are owned resources too; raw foreign file IDs are not
        # allowed to bypass the synchronous Responses ownership boundary.
        from hosted_tools import validate_resources
        await validate_resources(user, {**body, "proxy_server_request": {"headers": headers or {}}})
        upstream = profile["upstream_model"].removeprefix("openai/")
        manifest.append({"custom_id": custom_id, "model": alias, "route": BATCH_ENDPOINTS[line["url"]],
                         "options": options_for(body), "endpoint": line["url"]})
        body["model"] = upstream
        line["body"] = body
        seen.add(custom_id)
        models.add(upstream)
    if len(models) != 1 or len({item["endpoint"] for item in manifest}) != 1:
        fail(422, "BATCH_INPUT_INVALID", "A batch must use one model and endpoint.")
    return ("\n".join(encode(line) for line in lines) + "\n").encode(), manifest


async def intent(kind, owner, request_id, body, manifest=None):
    if not request_id or len(request_id) > 200:
        fail(422, "RESOURCE_REQUEST_ID_REQUIRED", "An idempotency-key is required for resource creation.")
    fingerprint = hashlib.sha256(body).hexdigest()
    pool = await repository.pool()
    from openai_owner_lifecycle import ensure_active
    await ensure_active(owner)
    row = await pool.fetchrow("""insert into gateway_openai_resources
        (id,kind,app_id,owner_id,request_id,request_hash,manifest) values($1,$2,$3,$4,$5,$6,$7::jsonb)
        on conflict(kind,app_id,owner_id,request_id) do nothing returning *""",
        "resource_" + uuid4().hex, kind, *owner, request_id, fingerprint, encode(manifest))
    if row:
        return decode(row), True
    row = decode(await pool.fetchrow("""select * from gateway_openai_resources
        where kind=$1 and app_id=$2 and owner_id=$3 and request_id=$4""", kind, *owner, request_id))
    if row["request_hash"] != fingerprint:
        fail(409, "RESOURCE_IDEMPOTENCY_CONFLICT", "The request ID already describes different input.")
    return row, False


async def save(row, state, data=None):
    pool = await repository.pool()
    data = data if data is not None else row["data"]
    return decode(await pool.fetchrow("""update gateway_openai_resources set state=$2,data=$3::jsonb,
        provider_id=coalesce($4,provider_id),updated_at=now() where id=$1 returning *""",
        row["id"], state, encode(data), data.get("id")))


async def create_once(row, operation):
    from openai_owner_lifecycle import ensure_active
    await ensure_active((row['app_id'],row['owner_id']))
    try:
        response = await operation()
        if response.is_error:
            await save(row, "failed" if response.status_code < 500 else "outcome_unknown")
            upstream_ok(response)
        row=await save(row, "ready", response.json())
        from openai_owner_lifecycle import cleanup_late_resource
        await cleanup_late_resource((row['app_id'],row['owner_id']))
        return row
    except (httpx.TransportError, ValueError):
        return await save(row, "outcome_unknown")


async def prepare_batch(row, file_row, user):
    pool = await repository.pool()
    identity = identity_for(user)
    for item in file_row["manifest"]:
        await authorize_model(user, item["model"])
        await accounting.check_budgets(identity, item["model"])
        profile = deepcopy(registry().select(item["model"], item["route"], item["options"]))
        contract = registry().document.get("hosted_tools") or {}
        if item["model"] not in contract.get("batch_models", []):
            fail(422, "BATCH_MODEL_UNVERIFIED", "This model has no verified batch contract.")
        profile["batch_contract"] = {"version": contract["version"], "multiplier": contract["batch_multiplier"]}
        suffix = hashlib.sha256((row["id"] + "\0" + item["custom_id"]).encode()).hexdigest()
        accounting_id = await accounting.begin(model=item["model"], route=item["route"], identity=identity,
                                               accounting_id="cost_batch_" + suffix)
        attempt = await accounting.attempt(accounting_id, profile, attempt_id="attempt_batch_" + suffix)
        await pool.execute("""insert into gateway_openai_batch_items(batch_id,custom_id,model,accounting_id,attempt_id)
            values($1,$2,$3,$4,$5) on conflict do nothing""", row["id"], item["custom_id"], item["model"], accounting_id, attempt)


async def settle_batch(row, owner):
    pool = await repository.pool()
    for field in ("output_file_id", "error_file_id"):
        provider_id = row["data"].get(field)
        if not provider_id:
            continue
        await remember("file", provider_id, owner, {"id": provider_id, "purpose": "batch_output"})
        response = upstream_ok(await provider("GET", "/files/" + quote(provider_id, safe="") + "/content"))
        for line in response.text.splitlines():
            result = json.loads(line)
            item = await pool.fetchrow("select * from gateway_openai_batch_items where batch_id=$1 and custom_id=$2",
                                       row["id"], result.get("custom_id"))
            if not item:
                continue  # A foreign output cannot select or create an accounting record.
            body = (result.get("response") or {}).get("body") or {}
            from gateway_accounting import response_evidence
            raw, model, provider_id, options = response_evidence(body)
            fingerprint = hashlib.sha256(encode(result).encode()).hexdigest()
            if item["result_hash"] == fingerprint:
                continue
            await accounting.observe(item["attempt_id"], raw_usage=raw, served_model=model,
                provider_request_id=provider_id, served_options=options,
                outcome="failure" if result.get("error") or (result.get("response") or {}).get("status_code", 200) >= 400 else "success")
            await accounting.finish(item["accounting_id"])
            await pool.execute("update gateway_openai_batch_items set result_hash=$3,state='observed' where batch_id=$1 and custom_id=$2",
                               row["id"], item["custom_id"], fingerprint)
    if row["data"].get("status") in TERMINAL:
        for item in await pool.fetch("select * from gateway_openai_batch_items where batch_id=$1 and state='pending'", row["id"]):
            # Cancelled/expired/missing outputs do not prove zero provider work.
            await accounting.finish(item["accounting_id"])
    return [{"custom_id": item["custom_id"], "accounting_id": item["accounting_id"],
             "cost": await accounting.get(item["accounting_id"])}
            for item in await pool.fetch("select * from gateway_openai_batch_items where batch_id=$1 order by custom_id", row["id"])]


async def settle_response(row, body, owner):
    """Recover a background response against its original pinned execution."""
    attempt = row["data"].get("gateway_attempt_id")
    accounting_id = row["data"].get("gateway_accounting_id")
    if not attempt or not accounting_id:
        return None
    if body.get("status") in {"queued", "in_progress"}:
        return await accounting.get(accounting_id)
    from cost_accounting import decode as decode_cost
    from gateway_accounting import response_evidence
    from hosted_tools import measured_tools, parent_subtotal, retain_resources
    pool = await repository.pool()
    stored = decode_cost(await pool.fetchrow("select profile from gateway_cost_attempts where attempt_id=$1", attempt))
    if not stored:
        return None
    raw, model, provider_id, options = response_evidence(body)
    if stored["profile"].get("hosted_contract"):
        raw.update(measured_tools(body))
        raw.pop("gateway_native_cost_usd", None)
        subtotal = parent_subtotal(stored["profile"], raw, options)
        if subtotal is not None:
            raw["gateway_hosted_parent_cost_usd"] = subtotal
    await accounting.observe(attempt, raw_usage=raw, served_model=model, provider_request_id=provider_id,
                             served_options=options, outcome="success" if body.get("status") == "completed" else "incomplete")
    if body.get("status") in {"completed", "failed", "incomplete", "cancelled"}:
        await accounting.finish(accounting_id)
    await retain_resources(body, {"resource_owner": owner, "accounting_id": accounting_id, "attempts": [attempt]})
    return await accounting.get(accounting_id)


async def batch_result(row, owner):
    result = public(row)
    try:
        result["gateway_items"] = await settle_batch(row, owner)
        result["gateway_settlement_status"] = "observed"
    except Exception:
        # A temporarily unavailable output file or journal must not discard the
        # accepted provider batch, nor encourage a second paid submission.
        log.exception("batch_settlement_pending", extra={"batch_id": row["id"]})
        result["gateway_settlement_status"] = "pending"
    return result


@resource_app.exception_handler(ProxyException)
async def auth_error(request, exc):
    from litellm.proxy.proxy_server import openai_exception_handler
    return await openai_exception_handler(request, exc)


@resource_app.exception_handler(PricingError)
async def pricing_error(request, exc):
    return JSONResponse(status_code=503, content={"error": {"code": exc.code, "message": str(exc)}})


@resource_app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def resources(path: str, request: Request, user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    path = path.removeprefix("v1/").strip("/")
    owner = resource_owner(user, request.headers)
    if path == 'resources/owner-data' and request.method in {'GET','DELETE'}:
        from openai_owner_lifecycle import owner_data
        return await owner_data(owner, purge=request.method=='DELETE')
    from openai_owner_lifecycle import ensure_active
    await ensure_active(owner)
    parts, method = path.split("/"), request.method
    collection = parts[0]
    kind = {"files": "file", "batches": "batch", "vector_stores": "vector_store", "containers": "container", "responses": "response"}.get(collection)
    if not kind:
        fail(404, "RESOURCE_ROUTE_UNSUPPORTED", "Resource route not supported.")
    pool = await repository.pool()
    if len(parts) == 1 and method == "GET" and kind in {"file", "batch", "vector_store", "container"}:
        rows = await pool.fetch("""select * from gateway_openai_resources where kind=$1 and app_id=$2 and owner_id=$3
            and state != 'deleted' order by created_at desc""", kind, *owner)
        return {"object": "list", "data": [public(decode(row)) for row in rows], "has_more": False}
    if len(parts) == 1 and method == "POST" and kind == "file":
        async with request.form(max_files=1, max_fields=5, max_part_size=512*1024*1024) as form:
            upload, purpose = form.get("file"), form.get("purpose")
            if purpose not in {"batch", "assistants", "user_data", "vision"} or not hasattr(upload, "read"):
                fail(422, "FILE_INPUT_INVALID", "Supply one file and a supported purpose.")
            limit = (200 if purpose == "batch" else 512) * 1024 * 1024
            content = await upload.read(limit + 1)
            if len(content) > limit:
                fail(413, "FILE_TOO_LARGE", "The file exceeds the provider's upload limit.")
            manifest = None
            if purpose == "batch":
                content, manifest = await batch_file(content, user, request.headers)
            row, fresh = await intent(kind, owner, request.headers.get("idempotency-key"), purpose.encode()+b"\0"+content, manifest)
            if fresh:
                row = await create_once(row, lambda: provider("POST", "/files", data={"purpose": purpose},
                    files={"file": (upload.filename or "input", content, upload.content_type or "application/octet-stream")}))
            return public(row)
    if len(parts) == 1 and method == "POST" and kind in {"batch", "vector_store"}:
        body = await request.json()
        if kind == "batch":
            if set(body) - {"input_file_id", "endpoint", "completion_window", "metadata"} or body.get("endpoint") not in BATCH_ENDPOINTS or body.get("completion_window", "24h") != "24h":
                fail(422, "BATCH_INPUT_INVALID", "Use a supported endpoint and the 24h completion window.")
            file_row = await owned("file", body.get("input_file_id"), owner)
            if not file_row.get("provider_id") or not file_row.get("manifest") or any(i["endpoint"] != body["endpoint"] for i in file_row["manifest"]):
                fail(422, "BATCH_INPUT_INVALID", "Choose an owned, validated batch input file for this endpoint.")
            row, fresh = await intent(kind, owner, request.headers.get("idempotency-key"), encode(body).encode())
            if fresh:
                try:
                    await prepare_batch(row, file_row, user)
                except (PricingError, HTTPException, ProxyException):
                    await save(row, "failed")
                    raise
                body = {**body, "input_file_id": file_row["provider_id"], "completion_window": "24h",
                        "metadata": {**(body.get("metadata") or {}), "gateway_resource_id": row["id"]}}
        else:
            if set(body) - {"name", "file_ids", "expires_after", "metadata"}:
                fail(422, "VECTOR_STORE_INPUT_INVALID", "Unsupported vector store creation fields.")
            for file_id in body.get("file_ids", []):
                await owned("file", file_id, owner)
            row, fresh = await intent(kind, owner, request.headers.get("idempotency-key"), encode(body).encode())
        if fresh:
            row = await create_once(row, lambda: provider("POST", "/"+collection, json=body))
        result = public(row)
        if kind == "vector_store":
            result["storage_cost_status"] = "unresolved"
        return result
    if len(parts) < 2:
        fail(404, "RESOURCE_ROUTE_UNSUPPORTED", "Resource route not supported.")
    row = await owned(kind, parts[1], owner)
    if kind == "batch" and method == "POST" and parts[2:] == ["recover"]:
        body = await request.json()
        provider_id = body.get("provider_id")
        if not isinstance(provider_id, str):
            fail(422, "BATCH_RECOVERY_INVALID", "Supply the provider batch ID from the original operation.")
        recovered = upstream_ok(await provider("GET", "/batches/" + quote(provider_id, safe=""))).json()
        if (recovered.get("metadata") or {}).get("gateway_resource_id") != row["id"]:
            fail(404, "RESOURCE_NOT_FOUND", "Resource not found.")
        row = await save(row, "ready", recovered)
        return await batch_result(row, owner)
    if not row.get("provider_id"):
        return public(row)
    base = "/" + collection + "/" + quote(row["provider_id"], safe="")
    if kind == "response" and (method == "GET" and len(parts) == 2 or method == "POST" and parts[2:] == ["cancel"]):
        body = upstream_ok(await provider(method, base + ("/cancel" if method == "POST" else ""))).json()
        try:
            cost = await settle_response(row, body, owner)
        except Exception:
            log.exception("response_resource_settlement_pending")
            cost = None
        return {**body, "gateway_cost": cost}
    if kind == "batch" and method == "POST" and parts[2:] == ["cancel"]:
        row = await save(row, "ready", upstream_ok(await provider("POST", base+"/cancel")).json())
        return await batch_result(row, owner)
    if len(parts) == 2 and method == "GET":
        row = await save(row, "ready", upstream_ok(await provider("GET", base)).json())
        result = public(row)
        if kind == "batch":
            return await batch_result(row, owner)
        if kind == "vector_store":
            result["storage_cost_status"] = "unresolved"
        return result
    if len(parts) == 2 and method == "DELETE" and kind in {"file", "vector_store", "container", "response"}:
        result = upstream_ok(await provider("DELETE", base)).json()
        await save(row, "deleted")
        return result
    if kind == "file" and method == "GET" and parts[2:] == ["content"]:
        response = upstream_ok(await provider("GET", base+"/content"))
        return Response(response.content, media_type=response.headers.get("content-type", "application/octet-stream"))
    if kind in {"vector_store", "container"} and parts[2:3] == ["files"]:
        if len(parts) == 3 and method == "GET":
            return upstream_ok(await provider("GET", base+"/files")).json()
        if kind == "vector_store" and len(parts) == 3 and method == "POST":
            body = await request.json()
            if set(body) - {"file_id", "attributes", "chunking_strategy"}:
                fail(422, "VECTOR_FILE_INPUT_INVALID", "Unsupported vector file fields.")
            await owned("file", body.get("file_id"), owner)
            return upstream_ok(await provider("POST", base+"/files", json=body)).json()
        if len(parts) in {4, 5} and method in {"GET", "DELETE"}:
            if len(parts) == 5 and (parts[4] != "content" or method != "GET"):
                fail(404, "RESOURCE_ROUTE_UNSUPPORTED", "Resource route not supported.")
            if kind == "vector_store":
                await owned("file", parts[3], owner)
            response = upstream_ok(await provider(method, base+"/files/"+quote(parts[3],safe="")+("/content" if len(parts)==5 else "")))
            return Response(response.content, media_type=response.headers.get("content-type", "application/json"))
    fail(404, "RESOURCE_ROUTE_UNSUPPORTED", "Resource route not supported.")


class OwnedOpenAIResourcesMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").removeprefix("/v1").strip("/")
        first = path.split("/", 1)[0]
        resource = path == 'resources/owner-data' or first in {"files", "batches", "vector_stores", "containers"} or (first == "responses" and "/" in path)
        if scope.get("type") == "http" and resource:
            await resource_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)
