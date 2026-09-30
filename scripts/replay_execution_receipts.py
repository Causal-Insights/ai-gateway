#!/usr/bin/env python3
"""Recover numeric Cloud Logging receipts after a DB outage; never call providers."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cost_accounting import CostAccounting
from generation_job_repository import GenerationJobRepository

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path, help="JSON array or JSONL Cloud Logging export")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    text = args.receipts.read_text().strip()
    entries = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    repo = GenerationJobRepository(read_only=not args.apply)
    ledger = CostAccounting(repo)
    results = []
    try:
        pool = await repo.pool()
        for entry in entries:
            receipt = entry.get("jsonPayload", entry)
            if entry.get("textPayload"):
                try:
                    receipt = json.loads(entry["textPayload"])
                except ValueError:
                    continue
            if receipt.get("event") != "gateway_execution_receipt":
                continue
            attempt_id = receipt["attempt_id"]
            if not await pool.fetchval("select 1 from gateway_cost_attempts where attempt_id=$1", attempt_id):
                results.append({"attempt_id": attempt_id, "status": "missing_intent_requires_review"})
                continue
            if args.apply:
                await ledger.observe(attempt_id, **receipt["evidence"])
            results.append({"attempt_id": attempt_id, "applied": args.apply})
    finally:
        await repo.close()
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
