#!/usr/bin/env python3
"""Compare an attributable provider bill bucket with exact journal costs.

Input is a reviewed JSON export containing provider, account_context, period_start,
period_end, upstream_models, invoice_cost_usd, and evidence (source/units/scope).
No allocation to individual requests or customer billing is performed.
"""
import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cost_accounting import CostAccounting, accounting, encode
from pricing_registry import decimal, timestamp
from generation_job_repository import GenerationJobRepository


async def reconcile(bucket, *, record=False):
    models = bucket["upstream_models"]
    if not models or not bucket.get("evidence"):
        raise ValueError("An attributable upstream model scope and provider evidence are required")
    if bucket.get("currency") != "USD" or bucket.get("account_context") in {None, "", "default"}:
        raise ValueError("Reconciliation requires an identified account/project and a USD usage-cost bucket")
    if bucket.get("exclusive_gateway_scope") is not True:
        raise ValueError("Provider bucket must exclude non-gateway traffic; aggregate account totals are not sufficient")
    start, end = timestamp(bucket["period_start"]), timestamp(bucket["period_end"])
    if end <= start:
        raise ValueError("Invalid reconciliation period")
    if start < timestamp(bucket["accounting_cutover_at"]):
        raise ValueError("Bucket precedes the validated accounting cutover boundary")
    invoice = decimal(bucket["invoice_cost_usd"])
    pool = await accounting.pool()
    row = await pool.fetchrow("""select sum(cost_usd) as total,count(*) as attempts,
        count(*) filter(where cost_status<>'priced') as unresolved
        from gateway_cost_attempts where profile->>'vendor'=$1 and profile->>'account_context'=$2
        and profile->>'upstream_model'=any($3::text[]) and created_at >= $4 and created_at < $5""",
        bucket["provider"], bucket["account_context"], models, start, end)
    # No matching evidence is not a verified zero-bill bucket.
    complete = row["attempts"] > 0 and row["unresolved"] == 0
    total = row["total"] if complete else None
    result = {"status": "unresolved" if total is None else "agreement" if total == invoice else "discrepancy",
              "attempts": row["attempts"], "unresolved_attempts": row["unresolved"],
              "usage_cost_usd": str(total) if total is not None else None,
              "invoice_cost_usd": str(invoice), "difference_usd": str(invoice-total) if total is not None else None,
              "scope": {"models": models, "period_start": start.isoformat(), "period_end": end.isoformat()},
              "request_level_allocation": None}
    if record and complete:
        identity = hashlib.sha256(encode(bucket).encode()).hexdigest()
        await pool.execute("""insert into gateway_cost_invoice_checks
            (reconciliation_id,provider,account_context,period_start,period_end,dimensions,
             usage_cost_usd,invoice_cost_usd,evidence)
            values($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9::jsonb) on conflict do nothing""",
            identity, bucket["provider"], bucket["account_context"], start, end,
            encode({"upstream_models": models}), total, invoice, encode(bucket["evidence"]))
    return result


async def main():
    global accounting
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    repository = GenerationJobRepository(read_only=not args.record)
    accounting = CostAccounting(repository)
    try:
        print(json.dumps(await reconcile(json.loads(args.export.read_text()), record=args.record), indent=2))
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
