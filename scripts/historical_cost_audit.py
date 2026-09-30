#!/usr/bin/env python3
"""Read-only historical audit. Never applies corrections or contacts a provider."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_SQL = '''
select jsonb_build_object(
 'retained_jobs', (select coalesce(jsonb_agg(x),'[]'::jsonb) from (
   select j.model,count(*) as completed,count(*) filter(where s.request_id is null) as missing_spend,
     sum(j.response_cost_usd)::text as legacy_cost_evidence,
     count(*) filter(where j.usage is not null) as with_usage
   from gateway_generation_jobs j left join "LiteLLM_SpendLogs" s
     on s.request_id=j.id or s.metadata->>'generation_job_id'=j.id
   where j.status='completed' group by j.model order by j.model) x),
 'zero_successes', (select coalesce(jsonb_agg(x),'[]'::jsonb) from (
   select model,count(*) as requests,min("startTime") as first_at,max("startTime") as last_at,
     count(*) filter(where total_tokens>0) as with_tokens
   from "LiteLLM_SpendLogs" where spend=0 and status='success' group by model order by model) x),
 'daily_differences', (select coalesce(jsonb_agg(x),'[]'::jsonb) from (
   with detailed as (select "startTime"::date::text as day,model,count(*) as requests,sum(spend) as spend
     from "LiteLLM_SpendLogs" where "startTime">=current_date-30 and "startTime"<current_date group by 1,2),
   daily as (select date as day,model,sum(api_requests) as requests,sum(spend) as spend
     from "LiteLLM_DailyUserSpend" where date>=(current_date-30)::text and date<current_date::text group by 1,2)
   select coalesce(d.day,s.day) as day,coalesce(d.model,s.model) as model,
     coalesce(s.requests,0)-coalesce(d.requests,0) as request_difference,
     coalesce(s.spend,0)-coalesce(d.spend,0) as spend_difference
   from detailed d full join daily s using(day,model)
   where coalesce(d.requests,0)<>coalesce(s.requests,0) or abs(coalesce(d.spend,0)-coalesce(s.spend,0))>0.000000001
   order by 1,2) x),
 'policy','Dry run only. Existing amounts are unverified evidence. Historical rates and identity must be verified; settled customer charges are unchanged.'
);
'''


async def main():
    import asyncpg
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from generation_job_repository import _database_url
    conn = await asyncpg.connect(_database_url())
    try:
        async with conn.transaction(readonly=True):
            report = json.loads(await conn.fetchval(AUDIT_SQL))
    finally:
        await conn.close()
    result = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(result)
    else:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
