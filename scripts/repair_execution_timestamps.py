#!/usr/bin/env python3
"""Repair retained execution timestamps from original journal evidence, never costs."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generation_job_repository import _database_url

CANDIDATES = '''
select s.request_id,a.accounting_id,s."startTime" as before_start,s."endTime" as before_end,
 (a.created_at at time zone 'UTC')::timestamp(3) as original_start,
 (a.observed_at at time zone 'UTC')::timestamp(3) as original_end
from "LiteLLM_SpendLogs" s join gateway_cost_attempts a on a.attempt_id=s.request_id
where a.profile->>'version'='retained-execution-v1'
 and s.metadata->>'accounting_id'=a.accounting_id
 and a.observed_at is not null and a.observed_at >= a.created_at
 and (s."startTime" is distinct from (a.created_at at time zone 'UTC')::timestamp(3)
   or s."endTime" is distinct from (a.observed_at at time zone 'UTC')::timestamp(3))
order by s.request_id
'''

async def repair(conn, *, apply=False):
    async with conn.transaction(readonly=not apply):
        rows = await conn.fetch(CANDIDATES + (' for update of s' if apply else ''))
        for row in rows if apply else []:
            before = {'startTime': str(row['before_start']), 'endTime': str(row['before_end'])}
            after = {'startTime': str(row['original_start']), 'endTime': str(row['original_end'])}
            await conn.execute('''insert into gateway_cost_corrections
                (correction_id,accounting_id,evidence,before_value,after_value,reason)
                values($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6) on conflict do nothing''',
                row['request_id'] + ':execution-timestamps-v1', row['accounting_id'],
                json.dumps({'source': 'gateway_cost_attempts.created_at/observed_at', 'version': 'execution-timestamps-v1'}),
                json.dumps(before), json.dumps(after),
                'Restore original execution timestamps after historical backfill; costs and budgets unchanged')
            await conn.execute('''update "LiteLLM_SpendLogs" set "startTime"=$2,"endTime"=$3
                where request_id=$1''', row['request_id'], row['original_start'], row['original_end'])
        return {'apply': apply, 'matching_records': len(rows),
                'examples': [dict(row) for row in rows[:3]]}

async def main():
    import asyncpg
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply reviewed timestamp corrections; default is read-only')
    args = parser.parse_args()
    # No migrations or provider calls: this operation changes timestamps only.
    conn = await asyncpg.connect(_database_url(), statement_cache_size=0,
                                 server_settings={'default_transaction_read_only': 'off' if args.apply else 'on'})
    try:
        print(json.dumps(await repair(conn, apply=args.apply), default=str, indent=2))
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
