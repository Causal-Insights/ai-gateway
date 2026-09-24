"""Durable request intents, numeric evidence, and atomic LiteLLM projections.

The journal is authoritative. LiteLLM Float columns are compatibility projections;
public amounts and historical evidence retain their exact decimal representation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from accounting_usage import extract, numbers
from pricing_registry import PricingError, decimal, registry

log = logging.getLogger("ai_gateway.accounting")


def merge_evidence(old, new):
    """Keep every known value; conflicting receipts are retained for review."""
    result, conflict = dict(old or {}), False
    for key, value in (new or {}).items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key], different = merge_evidence(result[key], value)
            conflict |= different
        elif result[key] != value:
            conflict = True
    return result, conflict


def encode(value):
    return json.dumps(value, separators=(",", ":"), default=str)


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for field in ("identity", "profile", "usage", "raw_usage", "breakdown", "served_options"):
        if isinstance(result.get(field), str):
            result[field] = json.loads(result[field])
    return result


def canonical_key(value):
    """LiteLLM auth already returns a SHA256 hash; raw keys require exactly one hash."""
    if not value:
        raise PricingError("Accounting requires an authenticated key")
    value = str(value)
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()


def identity_for(user, request_tags=None):
    identity = {key: getattr(user, key, None) for key in
                ("user_id", "team_id", "org_id", "project_id", "end_user_id", "agent_id")}
    identity["api_key_hash"] = canonical_key(getattr(user, "api_key", None))
    tags = ((getattr(user, "metadata", None) or {}).get("tags") or [])
    identity["tags"] = sorted({tag for tag in [*tags, *(request_tags or [])]
                               if isinstance(tag, str) and 0 < len(tag) <= 256})
    litellm = sys.modules.get("litellm")
    proxy = sys.modules.get("litellm.proxy.proxy_server")
    if litellm and getattr(litellm, "max_budget", 0) > 0 and proxy:
        identity["proxy_budget_id"] = proxy.litellm_proxy_budget_name
    return {key: value for key, value in identity.items() if value is not None}


class CostAccounting:
    def __init__(self, repository=None, price_registry=None):
        self._repository = repository
        self._registry = price_registry

    @property
    def prices(self):
        return self._registry or registry()

    async def pool(self):
        if self._repository is None:
            from generation_job_repository import repository
            self._repository = repository
        return await self._repository.pool()

    async def begin(self, *, model, route, identity, owner_hash=None, accounting_id=None, historical=False):
        key = canonical_key(identity.get("api_key_hash"))
        if key != identity.get("api_key_hash"):
            raise PricingError("Only canonical key hashes may be persisted")
        accounting_id = accounting_id or "cost_" + uuid4().hex
        pool = await self.pool()
        await pool.execute("""insert into gateway_cost_requests
            (accounting_id,owner_key_hash,identity,model,route,historical)
            values($1,$2,$3::jsonb,$4,$5,$6) on conflict do nothing""",
            accounting_id, owner_hash or key, encode(identity), model, route, historical)
        row = await pool.fetchrow("select * from gateway_cost_requests where accounting_id=$1", accounting_id)
        if (row["owner_key_hash"] != (owner_hash or key) or row["model"] != model or row["route"] != route
                or decode(row)["identity"] != identity or row["historical"] != historical):
            raise PricingError("Accounting identity conflict")
        return accounting_id

    async def check_budgets(self, identity, model):
        """Fresh admission floor; LiteLLM still handles in-flight reservations."""
        pool = await self.pool()
        for table, column, value in (("VerificationToken", "token", identity.get("api_key_hash")),
                ("UserTable", "user_id", identity.get("user_id")), ("TeamTable", "team_id", identity.get("team_id"))):
            if not value:
                continue
            row = await pool.fetchrow(f'''select spend,max_budget,model_spend,model_max_budget
                from "LiteLLM_{table}" where "{column}"=$1''', value)
            if row is None:
                continue
            if row["max_budget"] is not None and row["spend"] >= row["max_budget"]:
                raise PricingError("Committed account spend has reached its budget")
            spent = json.loads(row["model_spend"]) if isinstance(row["model_spend"], str) else row["model_spend"]
            limits = json.loads(row["model_max_budget"]) if isinstance(row["model_max_budget"], str) else row["model_max_budget"]
            if model in (limits or {}) and Decimal(str((spent or {}).get(model, 0))) >= Decimal(str(limits[model])):
                raise PricingError("Committed model spend has reached its budget")
        for table, column, value in (
                ("OrganizationTable", "organization_id", identity.get("org_id")),
                ("ProjectTable", "project_id", identity.get("project_id")),
                ("EndUserTable", "user_id", identity.get("end_user_id")),
                *(("TagTable", "tag_name", tag) for tag in identity.get("tags", []))):
            if value:
                row = await pool.fetchrow(f'''select e.spend,b.max_budget from "LiteLLM_{table}" e
                    join "LiteLLM_BudgetTable" b on b.budget_id=e.budget_id where e."{column}"=$1''', value)
                if row and row["max_budget"] is not None and row["spend"] >= row["max_budget"]:
                    raise PricingError("Committed account spend has reached its budget")
        if identity.get("proxy_budget_id"):
            row = await pool.fetchrow('select spend,max_budget from "LiteLLM_UserTable" where user_id=$1', identity["proxy_budget_id"])
            if row and row["max_budget"] is not None and row["spend"] >= row["max_budget"]:
                raise PricingError("Committed proxy spend has reached its budget")
        for table, field, value in (("TeamMembership", "team_id", identity.get("team_id")),
                                    ("OrganizationMembership", "organization_id", identity.get("org_id"))):
            if value and identity.get("user_id"):
                row = await pool.fetchrow(f'''select coalesce(e.spend,0) as spend,b.max_budget
                    from "LiteLLM_{table}" e join "LiteLLM_BudgetTable" b on b.budget_id=e.budget_id
                    where e.user_id=$1 and e."{field}"=$2''', identity["user_id"], value)
                if row and row["max_budget"] is not None and row["spend"] >= row["max_budget"]:
                    raise PricingError("Committed membership spend has reached its budget")

    async def attempt(self, accounting_id, profile, *, attempt_id=None):
        attempt_id = attempt_id or "attempt_" + uuid4().hex
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""insert into gateway_cost_attempts(attempt_id,accounting_id,profile)
                    values($1,$2,$3::jsonb) on conflict do nothing""", attempt_id, accounting_id, encode(profile))
                row = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1 for update", attempt_id))
                if row["accounting_id"] != accounting_id or row["profile"] != profile:
                    raise PricingError("Accounting attempt conflict")
                request = decode(await conn.fetchrow("select * from gateway_cost_requests where accounting_id=$1", accounting_id))
                # A visible execution intent exists BEFORE provider submission.
                await self._write_execution(conn, request, row)
        return attempt_id

    async def observe(self, attempt_id, *, raw_usage=None, served_model=None, provider_request_id=None,
                      outcome="success", zero_reason=None, served_options=None):
        raw = numbers(raw_usage or {}) or {}
        evidence = dict(raw_usage=raw, served_model=served_model, provider_request_id=provider_request_id,
                        outcome=outcome, zero_reason=zero_reason, served_options=served_options)
        observation_id = hashlib.sha256(encode([attempt_id, evidence]).encode()).hexdigest()
        # Numeric recovery receipt is also emitted to Cloud Logging. It contains
        # neither prompts nor credentials and survives a database write outage.
        print(encode({"event": "gateway_execution_receipt", "attempt_id": attempt_id,
                      "observation_id": observation_id, "evidence": evidence}), flush=True)
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1 for update", attempt_id))
                if not current:
                    raise PricingError("No durable intent for provider result")
                await conn.execute("""insert into gateway_cost_observations(observation_id,attempt_id,evidence)
                    values($1,$2,$3::jsonb) on conflict do nothing""", observation_id, attempt_id, encode(evidence))
                merged, conflict = merge_evidence(current["raw_usage"], raw)
                options, options_conflict = merge_evidence(current["served_options"], served_options)
                conflict |= options_conflict or any(current[field] and value and current[field] != value
                    for field, value in (("served_model", served_model), ("provider_request_id", provider_request_id)))
                # Empty disconnect/failure receipts cannot replace a real response.
                effective_outcome = outcome if raw or not current["raw_usage"] else current["outcome"]
                rule = current["profile"].get("reported_charge") or {}
                if raw.get(rule.get("field")) == "0" and "provider_reported_zero" in current["profile"].get("zero_reasons", []):
                    zero_reason = "provider_reported_zero"
                await conn.execute("""update gateway_cost_attempts set raw_usage=$2::jsonb,
                    served_model=coalesce(served_model,$3),provider_request_id=coalesce(provider_request_id,$4),
                    outcome=$5,zero_reason=coalesce(zero_reason,$6),observed_at=coalesce(observed_at,now()),
                    served_options=$7::jsonb,evidence_conflict=evidence_conflict or $8 where attempt_id=$1""",
                    attempt_id, encode(merged), served_model, provider_request_id, effective_outcome, zero_reason,
                    encode(options) if options else None, conflict)
        return await self.project(attempt_id)

    def _price_execution(self, row):
        """Admission/validation failures never erase observed monetary evidence."""
        profile, raw = row["profile"], row["raw_usage"] or {}
        usage, reported, issue = {}, None, None
        result = {"cost_usd": None, "calculated_cost_usd": None, "reported_cost_usd": None,
                  "cost_source": None, "breakdown": []}
        try:
            rule = profile.get("reported_charge") or {}
            if rule.get("field") in raw and row["zero_reason"] != "gateway_response_cache":
                reported = decimal(raw[rule["field"]])
            usage = {} if row["zero_reason"] == "gateway_response_cache" else extract(profile["extractor"], raw)
            result = self.prices.calculate(profile, usage, served_model=row["served_model"],
                served_options=row["served_options"], reported_charge=reported, zero_reason=row["zero_reason"])
        except Exception as exc:
            issue = str(exc)
            # Provider charges and already-calculated SDK costs are evidence even
            # if model/options/extractor verification needs reconciliation.
            try:
                if reported is not None:
                    amount = reported / decimal(profile["reported_charge"]["units_per_usd"])
                    result.update(cost_usd=str(amount), reported_cost_usd=str(amount), cost_source="provider_reported")
                elif raw.get("gateway_native_cost_usd") is not None:
                    amount = decimal(raw["gateway_native_cost_usd"])
                    result.update(cost_usd=str(amount), calculated_cost_usd=str(amount), cost_source="litellm")
                elif raw.get("gateway_retained_cost_usd") is not None:
                    amount = decimal(raw["gateway_retained_cost_usd"])
                    result.update(cost_usd=str(amount), calculated_cost_usd=str(amount), cost_source="legacy_gateway_evidence")
                if result["cost_usd"] == "0" and not row["zero_reason"]:
                    result.update(cost_usd=None, calculated_cost_usd=None, reported_cost_usd=None, cost_source=None)
            except Exception:
                pass
        if row["evidence_conflict"]:
            issue = "Conflicting provider receipts; all observations retained"
        # A later error or partial receipt cannot erase a cost already recorded.
        if row["cost_usd"] is not None:
            if result["cost_usd"] is not None and Decimal(result["cost_usd"]) != row["cost_usd"]:
                issue = "Cost correction requires reconciliation; prior cost retained"
            result.update(cost_usd=str(row["cost_usd"]), cost_source=row["cost_source"],
                          calculated_cost_usd=str(row["calculated_cost_usd"]) if row["calculated_cost_usd"] is not None else None,
                          reported_cost_usd=str(row["reported_cost_usd"]) if row["reported_cost_usd"] is not None else None,
                          breakdown=row["breakdown"])
        return usage, reported, result, issue

    async def project(self, attempt_id):
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1 for update", attempt_id))
                if not row:
                    return False
                request = decode(await conn.fetchrow("select * from gateway_cost_requests where accounting_id=$1", row["accounting_id"]))
                if row["observed_at"]:
                    usage, reported, result, issue = self._price_execution(row)
                    identity = request["identity"] or {}
                    reason = None if identity.get("api_key_hash") and (identity.get("user_id") or identity.get("team_id")) else "Incomplete billing attribution"
                    amount = Decimal(result["cost_usd"]) if result["cost_usd"] is not None else None
                    await conn.execute("""update gateway_cost_attempts set usage=$2::jsonb,reported_charge=$3,
                        cost_status=$4,cost_usd=$5,calculated_cost_usd=$6,reported_cost_usd=$7,
                        cost_source=$8,breakdown=$9::jsonb,
                        committed_at=case when $5::numeric is not null then coalesce(committed_at,now()) else null end,
                        unresolved_reason=$10,pricing_issue=$10,attribution_status=$11,attribution_reason=$12
                        where attempt_id=$1""", attempt_id, encode(usage), reported,
                        "priced" if amount is not None else "unresolved", amount,
                        Decimal(result["calculated_cost_usd"]) if result["calculated_cost_usd"] is not None else None,
                        Decimal(result["reported_cost_usd"]) if result["reported_cost_usd"] is not None else None,
                        result["cost_source"], encode(result["breakdown"]), issue,
                        "unresolved" if reason else "complete", reason)
                    if row["unresolved_reason"] == "Incomplete billing attribution" and amount is not None:
                        await conn.execute("""insert into gateway_cost_corrections
                            (correction_id,accounting_id,evidence,before_value,after_value,reason)
                            values($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6) on conflict do nothing""",
                            attempt_id + ":execution-log-v2", row["accounting_id"],
                            encode({"usage": row["raw_usage"], "pricing_version": row["profile"].get("version")}),
                            encode({"cost_status": row["cost_status"], "cost_usd": row["cost_usd"], "reason": row["unresolved_reason"]}),
                            encode(result), "Recover execution previously hidden by attribution; no settled customer charge change")
                row = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1", attempt_id))
                # Only the execution row and its journal receipt share this
                # transaction. No billing/aggregate/cache work can roll it back.
                await self._write_execution(conn, request, row)
        try:
            # Media-job bookkeeping is secondary too. A lock/constraint/failure
            # in that table must not roll back the execution's cost record.
            await pool.execute("""update gateway_generation_jobs set spend_logged_at=now(),updated_at=now()
                where accounting_id=$1 and not exists
                (select 1 from gateway_cost_attempts where accounting_id=$1 and log_recorded_at is null)""", row["accounting_id"])
        except Exception:
            log.exception("cost_job_marker_retry_failed", extra={"attempt_id": attempt_id})
        try:
            await self._project_billing(attempt_id)
        except Exception as exc:
            log.exception("cost_projection_retry_failed", extra={"attempt_id": attempt_id})
            await pool.execute("""update gateway_cost_attempts set projection_status='pending',projection_error=$2
                where attempt_id=$1 and projected_at is null""", attempt_id, str(exc)[:1000])
        return row["cost_usd"] is not None

    async def recover_job(self, job):
        """Expose legacy/missing-intent executions without inventing old rates."""
        pool = await self.pool()
        existing = await pool.fetchval('''select request_id from "LiteLLM_SpendLogs"
            where request_id=$1 or metadata->>'generation_job_id'=$1 limit 1''', job["id"])
        if existing and not job.get("accounting_id"):
            return None  # Already visible; do not create a second execution.
        identity = dict(job.get("owner_context") or {})
        matches = [r["token"] for r in await pool.fetch('select token from "LiteLLM_VerificationToken"')
                   if job.get("owner_key_hash") in {r["token"], hashlib.sha256(r["token"].encode()).hexdigest()}]
        if len(matches) == 1:
            identity["api_key_hash"] = matches[0]
        else:
            identity.pop("api_key_hash", None)
        metadata = job.get("request_metadata") or {}
        profile = {"version": "retained-execution-v1", "upstream_model": metadata.get("upstream_model") or job["model"],
                   "vendor": job.get("provider"), "status": "unverified", "extractor": "components", "zero_reasons": []}
        await pool.execute("""insert into gateway_cost_requests
            (accounting_id,owner_key_hash,identity,model,route,created_at,historical)
            values($1,$2,$3::jsonb,$4,'generation_job',$5,true) on conflict do nothing""",
            job["id"], job.get("owner_key_hash") or "", encode(identity), job["model"], job["created_at"])
        attempt_id = await self.attempt(job["id"], profile, attempt_id=job["id"] + ":retained:1")
        await pool.execute("update gateway_generation_jobs set accounting_id=$2 where id=$1", job["id"], job["id"])
        await pool.execute("""update gateway_cost_attempts set created_at=$2,observed_at=$3 where attempt_id=$1
            and observed_at is null""", attempt_id, job.get("submitted_at") or job["created_at"], job.get("completed_at"))
        raw = dict(job.get("usage") or {})
        if job.get("response_cost_usd") is not None:
            raw["gateway_retained_cost_usd"] = str(job["response_cost_usd"])
        await self.observe(attempt_id, raw_usage=raw, served_model=metadata.get("upstream_model"),
            provider_request_id=job.get("provider_request_id"), outcome="success" if job["status"] == "completed" else "failure")
        await self.finish(job["id"])
        return attempt_id

    @staticmethod
    def _tokens(attempt):
        usage = attempt.get("usage") or attempt.get("raw_usage") or {}
        def count(value):
            try:
                return min(2147483647, int(decimal(value)))
            except Exception:
                return 0
        prompt = count(usage.get("total_input_tokens", usage.get("input_tokens", usage.get("prompt_tokens", 0))))
        if any(key in usage for key in ("text_input_tokens", "image_input_tokens")):
            prompt = sum(count(usage.get(key, 0)) for key in
                         ("text_input_tokens", "image_input_tokens", "cached_text_tokens", "cached_image_tokens"))
        completion = count(usage.get("output_tokens", usage.get("completion_tokens", 0)))
        if "video_tokens" in usage:
            completion = count(usage["video_tokens"]) + count(usage.get("text_output_tokens", 0))
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached = count(usage.get("cached_tokens", details.get("cached_tokens", 0)))
        if "cached_image_tokens" in usage:
            cached += count(usage.get("cached_image_tokens", 0)) + count(usage.get("cached_text_tokens", 0))
        return min(prompt, 2147483647), min(completion, 2147483647), cached, count(usage.get("cache_write_tokens", 0))

    async def _write_execution(self, conn, request, attempt):
        identity = request["identity"] or {}
        key = identity.get("api_key_hash") or ""
        model = attempt.get("served_model") or attempt["profile"].get("upstream_model") or request["model"]
        provider = attempt["profile"].get("vendor") or attempt["profile"].get("upstream_model", model).split("/", 1)[0]
        prompt, completion, _, _ = self._tokens(attempt)
        start = attempt["created_at"].astimezone(timezone.utc).replace(tzinfo=None)
        end = (attempt["observed_at"] or attempt["created_at"]).astimezone(timezone.utc).replace(tzinfo=None)
        known = attempt["cost_usd"] is not None
        metadata = {"accounting_id": request["accounting_id"], "pricing_version": attempt["profile"].get("version"),
                    "provider_request_id": attempt["provider_request_id"], "cost_source": attempt["cost_source"],
                    "cost_usd_exact": str(attempt["cost_usd"]) if known else None, "cost_status": attempt["cost_status"],
                    "usage": attempt["usage"] or attempt["raw_usage"] or {}, "attribution_status": attempt["attribution_status"],
                    "attribution_reason": attempt["attribution_reason"], "pricing_issue": attempt["pricing_issue"],
                    "projection_status": attempt["projection_status"], "projection_error": attempt["projection_error"],
                    "billing_eligible": known and attempt["attribution_status"] == "complete" and not attempt["pricing_issue"]
                        and attempt["projection_status"] == "applied" and not request["historical"],
                    "project_id": identity.get("project_id"),
                    "generation_job_id": request["accounting_id"] if request["route"] == "generation_job" else None}
        call_type = {"completion": "acompletion", "responses": "aresponses", "speech": "aspeech",
                     "image_generation": "aimage_generation", "image_edit": "aimage_edit"}.get(request["route"], request["route"])
        prior = await conn.fetchval('select metadata from "LiteLLM_SpendLogs" where request_id=$1', attempt["attempt_id"])
        if prior is not None:
            prior = json.loads(prior) if isinstance(prior, str) else prior
            if prior.get("accounting_id") != request["accounting_id"]:
                raise RuntimeError("Execution ID collision; existing record retained")
        await conn.execute("""insert into "LiteLLM_SpendLogs"
            (request_id,call_type,api_key,spend,total_tokens,prompt_tokens,completion_tokens,
             "startTime","endTime",model,model_group,custom_llm_provider,"user",team_id,
             organization_id,end_user,metadata,status,agent_id,request_tags,cache_hit)
            values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,$18,$19,$20::jsonb,$21)
            on conflict(request_id) do update set spend=excluded.spend,total_tokens=excluded.total_tokens,
            prompt_tokens=excluded.prompt_tokens,completion_tokens=excluded.completion_tokens,
            "startTime"=excluded."startTime","endTime"=excluded."endTime",
            model=excluded.model,metadata=excluded.metadata,status=excluded.status,
            "user"=excluded."user",team_id=excluded.team_id,organization_id=excluded.organization_id,
            end_user=excluded.end_user""",
            attempt["attempt_id"], call_type, key, float(attempt["cost_usd"]) if known else None,
            min(prompt+completion,2147483647), prompt, completion, start, end, model, request["model"], provider,
            identity.get("user_id"), identity.get("team_id"), identity.get("org_id"), identity.get("end_user_id"),
            encode(metadata), attempt["outcome"] or "pending", identity.get("agent_id"), encode(identity.get("tags", [])),
            "True" if attempt["zero_reason"] == "gateway_response_cache" else "False")
        await conn.execute("update gateway_cost_attempts set log_recorded_at=coalesce(log_recorded_at,now()) where attempt_id=$1", attempt["attempt_id"])

    async def _project_billing(self, attempt_id):
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1 for update", attempt_id))
                request = decode(await conn.fetchrow("select * from gateway_cost_requests where accounting_id=$1", row["accounting_id"]))
                if row["projected_at"]:
                    return
                if row["cost_usd"] is None or row["pricing_issue"] or row["evidence_conflict"]:
                    await conn.execute("update gateway_cost_attempts set projection_status='unresolved' where attempt_id=$1", attempt_id)
                    row["projection_status"] = "unresolved"
                    await self._write_execution(conn, request, row)
                    return
                await self._project_litellm(conn, request, row, {"cost_usd": str(row["cost_usd"])})
                await conn.execute("""update gateway_cost_attempts set projected_at=now(),
                    projection_status='applied',projection_error=null where attempt_id=$1""", attempt_id)
                if request["historical"]:
                    await conn.execute("""insert into gateway_cost_corrections
                        (correction_id,accounting_id,evidence,before_value,after_value,reason)
                        values($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6) on conflict do nothing""",
                        attempt_id + ":import-v1", row["accounting_id"], encode({"profile": row["profile"], "usage": row["raw_usage"]}),
                        encode({"detailed_spend": "missing"}), encode({"cost_usd": str(row["cost_usd"])}),
                        "Verified historical provider-cost import; no settled customer charge change")
                await conn.execute("""insert into gateway_cost_cache_outbox(attempt_id,identity)
                    values($1,$2::jsonb) on conflict do nothing""", attempt_id, encode(request["identity"]))
                row = decode(await conn.fetchrow("select * from gateway_cost_attempts where attempt_id=$1", attempt_id))
                await self._write_execution(conn, request, row)

    async def _project_litellm(self, conn, request, attempt, result):
        identity = request["identity"] or {}
        key = identity.get("api_key_hash") or ""
        model = attempt["profile"].get("upstream_model") or request["model"]
        provider = model.split("/", 1)[0]
        amount = float(Decimal(result["cost_usd"]))
        prompt, completion, cached, written = self._tokens(attempt)
        start = attempt["created_at"].astimezone(timezone.utc).replace(tzinfo=None)
        daily_buckets = {}
        for table, field, value in (
            ("DailyUserSpend", "user_id", identity.get("user_id") or ""),
            ("DailyTeamSpend", "team_id", identity.get("team_id")),
            ("DailyOrganizationSpend", "organization_id", identity.get("org_id")),
            ("DailyEndUserSpend", "end_user_id", identity.get("end_user_id")),
            ("DailyAgentSpend", "agent_id", identity.get("agent_id")),
            *(("DailyTagSpend", "tag", tag) for tag in identity.get("tags", [])),
        ):
            if not value and table != "DailyUserSpend":
                continue
            # Deterministic bucket IDs avoid PostgreSQL NULL-distinct unique keys.
            bucket = encode([table, value, start.date().isoformat(), key, model, provider, request["route"]])
            bucket_id = "gateway_" + hashlib.sha256(bucket.encode()).hexdigest()
            projection = table + (":" + hashlib.sha256(value.encode()).hexdigest() if table == "DailyTagSpend" else "")
            daily_buckets[projection] = bucket_id
            await conn.execute(f'''insert into "LiteLLM_{table}"
                (id,"{field}",date,api_key,model,model_group,custom_llm_provider,endpoint,
                 mcp_namespaced_tool_name,spend,prompt_tokens,completion_tokens,api_requests,
                 successful_requests,failed_requests,updated_at,cache_read_input_tokens,cache_creation_input_tokens)
                values($1,$2,$3,$4,$5,$6,$7,$8,'',$9,$10,$11,1,$12,$13,now(),$14,$15)
                on conflict (id) do update set spend="LiteLLM_{table}".spend+excluded.spend,
                prompt_tokens="LiteLLM_{table}".prompt_tokens+excluded.prompt_tokens,
                completion_tokens="LiteLLM_{table}".completion_tokens+excluded.completion_tokens,
                api_requests="LiteLLM_{table}".api_requests+1,
                successful_requests="LiteLLM_{table}".successful_requests+excluded.successful_requests,
                failed_requests="LiteLLM_{table}".failed_requests+excluded.failed_requests,
                cache_read_input_tokens="LiteLLM_{table}".cache_read_input_tokens+excluded.cache_read_input_tokens,
                cache_creation_input_tokens="LiteLLM_{table}".cache_creation_input_tokens+excluded.cache_creation_input_tokens,updated_at=now()''',
                bucket_id, value, start.date().isoformat(), key, model, request["model"], provider,
                request["route"], amount, prompt, completion, int(attempt["outcome"] == "success"), int(attempt["outcome"] != "success"), cached, written)
        await conn.execute("update gateway_cost_attempts set daily_buckets=$2::jsonb where attempt_id=$1",
                           attempt["attempt_id"], encode(daily_buckets))
        # Historical corrections affect accounting reports, not current budget
        # windows or settled retail charges. They use the explicit repair path.
        if request["historical"]:
            return
        for table, field, value in (
            ("VerificationToken", "token", key), ("UserTable", "user_id", identity.get("user_id")),
            ("TeamTable", "team_id", identity.get("team_id")),
            ("OrganizationTable", "organization_id", identity.get("org_id")),
            ("ProjectTable", "project_id", identity.get("project_id")),
            ("EndUserTable", "user_id", identity.get("end_user_id")),
        ):
            if value:
                await conn.execute(f'update "LiteLLM_{table}" set spend=spend+$2 where "{field}"=$1', value, amount)
                if table != "EndUserTable":
                    await conn.execute(f'''update "LiteLLM_{table}" set model_spend=jsonb_set(
                        coalesce(model_spend,'{{}}'::jsonb),array[$2],
                        to_jsonb(coalesce((model_spend->>$2)::numeric,0)+$3::numeric)) where "{field}"=$1''',
                        value, request["model"], Decimal(result["cost_usd"]))
        if identity.get("user_id") and identity.get("team_id"):
            await conn.execute('''update "LiteLLM_TeamMembership" set spend=spend+$3,total_spend=total_spend+$3
                where user_id=$1 and team_id=$2''', identity["user_id"], identity["team_id"], amount)
        if identity.get("user_id") and identity.get("org_id"):
            await conn.execute('''update "LiteLLM_OrganizationMembership" set spend=coalesce(spend,0)+$3
                where user_id=$1 and organization_id=$2''', identity["user_id"], identity["org_id"], amount)
        for tag in identity.get("tags", []):
            await conn.execute('update "LiteLLM_TagTable" set spend=spend+$2 where tag_name=$1', tag, amount)
        if identity.get("proxy_budget_id") and identity["proxy_budget_id"] != identity.get("user_id"):
            await conn.execute('update "LiteLLM_UserTable" set spend=spend+$2 where user_id=$1', identity["proxy_budget_id"], amount)

    async def finish(self, accounting_id):
        pool = await self.pool()
        await pool.execute("update gateway_cost_requests set finished_at=coalesce(finished_at,now()) where accounting_id=$1", accounting_id)

    async def get(self, accounting_id, owner_hash=None):
        pool = await self.pool()
        request = decode(await pool.fetchrow("select * from gateway_cost_requests where accounting_id=$1", accounting_id))
        if not request or (owner_hash and request["owner_key_hash"] != owner_hash):
            return None
        attempts = [decode(r) for r in await pool.fetch("select * from gateway_cost_attempts where accounting_id=$1 order by created_at,attempt_id", accounting_id)]
        status = "pending"
        if any(a["cost_status"] == "unresolved" for a in attempts):
            status = "unresolved"
        elif attempts and all(a["cost_status"] == "priced" for a in attempts):
            status = "priced"
        amount = str(sum((a["cost_usd"] for a in attempts), Decimal(0))) if status == "priced" else None
        versions = sorted({a["profile"]["version"] for a in attempts})
        sources = sorted({a["cost_source"] for a in attempts if a["cost_source"]})
        return {"accounting_id": accounting_id, "cost_status": status, "cost_usd": amount,
                "cost_source": sources[0] if len(sources) == 1 else ("mixed" if sources else None),
                "pricing_version": versions[0] if len(versions) == 1 else versions,
                "usage": {a["attempt_id"]: a["usage"] for a in attempts},
                "breakdown": [{"attempt_id": a["attempt_id"], "cost_status": a["cost_status"],
                    "cost_usd": str(a["cost_usd"]) if a["cost_usd"] is not None else None,
                    "reported_cost_usd": str(a["reported_cost_usd"]) if a["reported_cost_usd"] is not None else None,
                    "calculated_cost_usd": str(a["calculated_cost_usd"]) if a["calculated_cost_usd"] is not None else None,
                    "cost_source": a["cost_source"], "pricing_version": a["profile"]["version"],
                    "components": a["breakdown"], "reason": a["unresolved_reason"],
                    "attribution_status": a["attribution_status"], "attribution_reason": a["attribution_reason"],
                    "pricing_issue": a["pricing_issue"], "projection_status": a["projection_status"],
                    "projection_error": a["projection_error"]} for a in attempts],
                "known_cost_usd": str(sum((a["cost_usd"] for a in attempts if a["cost_usd"] is not None), Decimal(0))),
                "attribution_status": "complete" if attempts and all(a["attribution_status"] == "complete" for a in attempts) else "unresolved",
                "projection_status": "applied" if attempts and all(a["projection_status"] == "applied" for a in attempts) else "pending",
                "billing_eligible": status == "priced" and bool(request["finished_at"]) and not request["historical"]
                    and all(a["attribution_status"] == "complete" and a["projection_status"] == "applied" and not a["pricing_issue"] for a in attempts),
                "invoice_reconciliation_status": "not_reconciled"}

    async def reconcile(self, limit=100):
        pool = await self.pool()
        rows = await pool.fetch("""select a.attempt_id from gateway_cost_attempts a
            left join "LiteLLM_SpendLogs" s on s.request_id=a.attempt_id
            where s.request_id is null or a.log_recorded_at is null
              or (a.observed_at is not null and (a.cost_status='pending' or a.projection_status='pending'
                  or a.unresolved_reason='Incomplete billing attribution'))
            order by a.created_at limit $1""", limit)
        committed = 0
        for row in rows:
            try:
                committed += int(await self.project(row["attempt_id"]))
            except Exception:
                log.exception("cost_projection_retry_failed", extra={"attempt_id": row["attempt_id"]})
        return {"retried": len(rows), "committed": committed}

    async def report(self):
        pool = await self.pool()
        rows = await pool.fetch("""select a.attempt_id,a.accounting_id,a.cost_status,a.unresolved_reason,
            a.attribution_status,a.attribution_reason,a.projection_status,a.projection_error,a.pricing_issue,
            a.cost_usd::text, a.created_at, s.request_id is null as missing_spend
            from gateway_cost_attempts a left join "LiteLLM_SpendLogs" s on s.request_id=a.attempt_id
            where s.request_id is null
               or ((a.cost_status <> 'priced' or a.attribution_status='unresolved' or a.projection_status <> 'applied'
                    or a.pricing_issue is not null) and a.created_at < now()-interval '5 minutes')
               or (a.cost_status='priced' and (s.spend is null or abs(s.spend::numeric-a.cost_usd)>0.000000001))
               or (a.cost_usd is null and s.spend is not null)
            order by a.created_at limit 1000""")
        unfinished = await pool.fetch("""select r.accounting_id,r.created_at from gateway_cost_requests r
            where r.finished_at is null and r.created_at < now()-interval '5 minutes'
            and not exists (select 1 from gateway_generation_jobs j where j.accounting_id=r.accounting_id
                and j.status in ('submitting','queued','in_progress')) order by r.created_at limit 1000""")
        aggregates = []
        for table in ("DailyUserSpend", "DailyTeamSpend", "DailyOrganizationSpend", "DailyEndUserSpend", "DailyAgentSpend", "DailyTagSpend"):
            differences = await pool.fetch(f'''with expected as (
                select b.value as bucket,sum(cost_usd) as spend,count(*) as requests
                from gateway_cost_attempts a cross join lateral jsonb_each_text(a.daily_buckets) b
                where projected_at is not null and split_part(b.key,':',1)=$1
                group by 1
            ) select $1::text as projection,coalesce(e.bucket,d.id) as bucket_id,e.spend::text as journal_spend,
                d.spend as aggregate_spend,e.requests as journal_requests,d.api_requests as aggregate_requests
                from expected e full join "LiteLLM_{table}" d on d.id=e.bucket
                where (e.bucket is not null or d.id like 'gateway_%') and
                    (e.bucket is null or d.id is null or abs(e.spend-d.spend::numeric)>0.000000001 or e.requests<>d.api_requests)
                limit 1000''', table)
            aggregates.extend(dict(row) for row in differences)
        expired = [p["version"] for p in self.prices.profiles.values() if p.get("enabled") and p.get("verify_by")
                   and __import__("pricing_registry").timestamp(p["verify_by"]) <= datetime.now(timezone.utc)]
        return {"issues": [dict(row) for row in rows], "unfinished_requests": [dict(row) for row in unfinished],
                "aggregate_divergence": aggregates, "expired_pricing": expired,
                "truncated": len(rows) == 1000 or len(unfinished) == 1000 or len(aggregates) >= 1000}


accounting = CostAccounting()
