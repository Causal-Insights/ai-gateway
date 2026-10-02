"""Bounded aggregate reads. Never import Gateway/Studio runtime or reconciliation code."""
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import asyncpg

from .model import Batch, PROVIDERS, row
from .providers import require

QUERIES = {
    "gateway": """SELECT (a.created_at AT TIME ZONE 'UTC')::date AS "day",
        a.profile->>'vendor' provider, coalesce(a.served_model, a.profile->>'upstream_model', r.model) model,
        sum(a.cost_usd)::text cost, count(*) requests,
        count(*) FILTER (WHERE a.cost_usd IS NULL) pending
        FROM gateway_cost_attempts a JOIN gateway_cost_requests r USING (accounting_id)
        WHERE a.created_at >= $1 AND a.created_at < $2 GROUP BY 1,2,3""",
    "litellm": """SELECT "startTime"::date AS "day", custom_llm_provider provider, model,
        sum(coalesce((metadata->>'cost_usd_exact')::numeric, spend::numeric))::text cost,
        count(*) requests, count(*) FILTER (WHERE spend IS NULL AND metadata->>'cost_usd_exact' IS NULL) pending
        FROM "LiteLLM_SpendLogs" WHERE "startTime" >= $1 AND "startTime" < $2 GROUP BY 1,2,3""",
    "magiclens": """SELECT (a.started_at AT TIME ZONE 'UTC')::date AS "day", a.provider,
        coalesce(a.served_model_id, a.model_name, r.model_name, a.model_id) model,
        sum(a.cost_usd)::text cost, count(*) requests,
        count(*) FILTER (WHERE a.cost_status <> 'priced') pending
        FROM magiclens.financial_attempts a JOIN magiclens.financial_runs r ON r.id=a.financial_run_id
        WHERE a.started_at >= $1 AND a.started_at < $2 GROUP BY 1,2,3""",
}


def model_catalog():
    return json.loads(Path(__file__).with_name("model_aliases.json").read_text())


def resolve_provider(provider, model, catalog):
    aliases = {"vertex_ai": "google", "vertex": "google", "gcp": "google", "grok": "xai",
               "bytedance": "byteplus", "ark": "byteplus"}
    provider = aliases.get(provider, provider)
    if provider in PROVIDERS:
        return provider
    identity = catalog.get(model or "")
    if identity:
        return identity["provider"]
    return aliases.get((model or "").split("/")[0], (model or "").split("/")[0])


async def collect_internal(env, source, start, end):
    name = "MAGICLENS_REPORTING_DATABASE_URL" if source == "magiclens" else "GATEWAY_REPORTING_DATABASE_URL"
    url = require(env, name)
    connection = await asyncpg.connect(url, timeout=5, command_timeout=5,
        server_settings={"application_name": "provider-usage-dashboard", "default_transaction_read_only": "on",
                         "statement_timeout": "5000", "lock_timeout": "1000"})
    begin = datetime.combine(start, time(), timezone.utc)
    finish = datetime.combine(end + timedelta(days=1), time(), timezone.utc)
    if source == "litellm":
        begin, finish = begin.replace(tzinfo=None), finish.replace(tzinfo=None)
    try:
        async with connection.transaction(readonly=True):
            results = await connection.fetch(QUERIES[source], begin, finish)
    finally:
        await connection.close(timeout=5)
    catalog = model_catalog()
    batches = {p: Batch(p, "costs", start, end, source=source,
        scope=env.get(f"{source.upper()}_REPORTING_SCOPE", "Magic Lens activity" if source == "magiclens" else "Gateway activity"),
        basis="Recorded supplier costs; pending amounts remain unknown") for p in PROVIDERS}
    for item in results:
        provider = resolve_provider(item["provider"], item["model"], catalog)
        if provider not in batches:
            # A source may contain services outside these integrations. Their
            # supplier identity is not guessed into one of the supported providers.
            continue
        batch = batches[provider]
        batch.rows.append(row(item["day"], model=item["model"], cost=item["cost"],
                              metrics={"requests": item["requests"]}, pending=item["pending"]))
        batch.covered[str(item["day"])] = ["cost", "requests"]
    return list(batches.values())
