#!/usr/bin/env python3
"""Review/apply missing-job corrections with original-date verified price evidence."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cost_repairs
from cost_accounting import CostAccounting
from generation_job_repository import GenerationJobRepository


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON list of job_id and pricing_version pairs")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repository = GenerationJobRepository(read_only=not args.apply)
    cost_repairs.accounting = CostAccounting(repository)
    results = []
    try:
        for item in json.loads(args.manifest.read_text()):
            try:
                results.append(await cost_repairs.repair_missing_job(item["job_id"], item["pricing_version"], apply=args.apply))
            except ValueError as exc:
                results.append({"job_id": item["job_id"], "status": "unresolved", "reason": str(exc)})
    finally:
        await repository.close()
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
