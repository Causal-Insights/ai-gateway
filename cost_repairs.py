"""Evidence-reviewed historical imports. Dry-run by default; never retail billing."""
from datetime import datetime, timezone
import hashlib
import json

from cost_accounting import accounting, decode
from pricing_registry import PricingError, timestamp


async def inspect_job(job_id, pricing_version):
    pool = await accounting.pool()
    job = decode(await pool.fetchrow("select * from gateway_generation_jobs where id=$1", job_id))
    if not job:
        raise PricingError("Historical job no longer retained; obtain original provider evidence")
    from generation_job_repository import _row
    job = _row(job)
    profile = accounting.prices.profiles.get(pricing_version)
    if not profile or profile["status"] != "verified":
        raise PricingError("Historical correction requires a verified pricing version")
    original_time = job.get("submitted_at") or job["created_at"]
    if original_time < timestamp(profile["effective_from"]) or (profile.get("effective_to") and original_time >= timestamp(profile["effective_to"])):
        raise PricingError("Price evidence does not cover the original request date")
    upstream = (job["request_metadata"] or {}).get("upstream_model")
    if upstream not in profile.get("served_models", [profile["upstream_model"].split("/", 1)[-1]]):
        raise PricingError("Historical upstream does not match the price evidence")
    prior = await pool.fetchval('''select request_id from "LiteLLM_SpendLogs" where
        request_id=$1 or metadata->>'generation_job_id'=$1 limit 1''', job_id)
    # Never guess whether the detail or aggregate side of a mismatch is correct.
    if prior and prior != job_id + ":historical:1":
        raise PricingError("Existing detailed spend requires a separately reviewed correction")
    identity = dict(job["owner_context"] or {})
    matches = []
    for token in await pool.fetch('select token,user_id,team_id,organization_id,project_id from "LiteLLM_VerificationToken"'):
        if hashlib.sha256(token["token"].encode()).hexdigest() == job["owner_key_hash"]:
            matches.append(token)
    if len(matches) != 1:
        raise PricingError("Historical canonical key attribution is missing or ambiguous")
    token = matches[0]
    # Deleted/reassigned keys cannot silently change old user/team attribution.
    for field in ("user_id", "team_id"):
        if identity.get(field) and identity[field] != token[field]:
            raise PricingError("Historical key attribution conflicts with retained ownership")
    identity.update(api_key_hash=token["token"])
    from accounting_usage import extract
    usage = extract(profile["extractor"], job["usage"] or {})
    rule = profile.get("reported_charge") or {}
    reported = (job["usage"] or {}).get(rule.get("field"))
    calculated = accounting.prices.calculate(profile, usage, served_model=upstream, reported_charge=reported)
    return job, identity, profile, calculated


async def repair_missing_job(job_id, pricing_version, *, apply=False):
    job, identity, profile, result = await inspect_job(job_id, pricing_version)
    proposal = {"job_id": job_id, "before": {"legacy_cost_evidence": str(job.get("response_cost_usd")),
                "spend_logged_at": str(job.get("spend_logged_at"))}, "after": result,
                "settled_customer_charges_changed": False, "applied": False}
    if not apply:
        return proposal
    accounting_id = job_id
    attempt_id = job_id + ":historical:1"
    await accounting.begin(model=job["model"], route="generation_job", identity=identity,
                           owner_hash=job["owner_key_hash"], accounting_id=accounting_id, historical=True)
    await accounting.attempt(accounting_id, profile, attempt_id=attempt_id)
    pool = await accounting.pool()
    await pool.execute("""update gateway_cost_attempts set created_at=$2
        where attempt_id=$1 and observed_at is null""", attempt_id, job.get("submitted_at") or job["created_at"])
    # A committed attempt is the idempotency receipt. Re-running changes nothing.
    if await pool.fetchval("select committed_at from gateway_cost_attempts where attempt_id=$1", attempt_id):
        proposal["already_applied"] = True
        return proposal
    await pool.execute("update gateway_generation_jobs set accounting_id=$2 where id=$1", job_id, accounting_id)
    committed = await accounting.observe(attempt_id, raw_usage=job["usage"],
        served_model=job["request_metadata"].get("upstream_model"), provider_request_id=job["provider_request_id"],
        outcome="success" if job["status"] == "completed" else "failure")
    await accounting.finish(accounting_id)
    proposal["applied"] = committed
    if not committed:
        proposal["status"] = "unresolved"
    return proposal
