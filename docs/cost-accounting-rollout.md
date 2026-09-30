# Cost accounting release runbook

Standard catalog prices are accepted without duplicate manual verification.
All configured models must pass `scripts/pricing_coverage.py --release`. Validate
shared accounting paths and concrete custom-provider changes before promotion;
missing per-alias paid fixtures or invoice access are not availability blockers.

The [validation record](cost-accounting-validation.md) distinguishes completed
local checks from outstanding production acceptance.

## Execution-logging invariant

**Unattributed costs are permitted; unrecorded provider executions are not.**
Validate the real LiteLLM Logs API with a standalone key, known cost and null
user/team attribution. Also query a pending execution with null spend. Test budget
transaction failure, duplicate receipts, late usage, missing mappings, flags,
charged failures, streams, retained jobs and restart repair.

Migration `004_execution_logging.sql` makes spend nullable and separates execution
receipts from financial projection markers. The image must regenerate Prisma with
`scripts/patch_execution_log_schema.py`; changing SQL alone breaks Logs reads.
Once null-spend rows exist, rollback must retain a nullable-compatible ORM and the
mandatory execution writer. Never roll back to an image that hides executions.

## Validation before traffic

- Run registry coverage, evidence schema validation and skill validation. Confirm
  the personal skill matches the canonical repository copy.
- Run the complete gateway suite in the Dockerfile's pinned LiteLLM image.
  Set `RUN_COST_DB_TESTS=1` only for isolated PostgreSQL. Supply
  `GATEWAY_DATABASE_URL` pointing to that database, never production for tests.
- Restore a recent production backup into a separate clone. Record identity/count
  hashes and run additive migration `003_cost_accounting.sql`. Verify no key, user,
  job, existing spend or budget identity changes. The September 16 release restored
  the production gateway public schema and data into isolated PostgreSQL 17 and
  verified identity/count hashes after migration; see the validation record.
- Run `scripts/historical_cost_audit.py` read-only. Compare its original-date
  evidence against provider billing. Investigate daily/detail mismatches before
  deciding whether a detailed row, an aggregate, or neither needs correction.
- Verify real provider usage fixtures and each required profile. A media test that checked only `job.response_cost_usd` does not prove ledger writes.

## Cutover

Check database connection headroom before starting overlapping revisions. Production
uses a Supabase session pool with a 15-client limit. The `DATABASE_URL` secret must
include `connection_limit=3&pool_timeout=30` for Prisma; the gateway starts through
Uvicorn, so LiteLLM CLI connection-pool defaults do not apply. Set
`GENERATION_DB_POOL_SIZE=2` for the gateway and callback service. Account for both
pools, autoscaling, and retained tagged revisions when sizing database capacity.

Build an immutable image and start a no-traffic candidate against the clone first.
Verify the stock writer replacement, authenticated lookup, native headers, final
stream usage and background-job recovery. Then prepare a production no-traffic
candidate and run migrations once with traffic still on the prior revision.

Record the cutover time and old revision. Drain in-flight native requests and the
old spend queue before moving traffic. Do not run old and new accounting writers
against the same migrated request. Preserve existing job ownership/idempotency;
legacy executions are logged immediately as retained evidence; verified pricing
and financial corrections remain separate reviewed operations. Never resubmit an outcome-unknown job.

Promote only after all required profiles pass the release gate and clone/candidate
checks are recorded. Validate a bounded sample for cost contract, spend row, daily
aggregate and budget parity. Keep the journal intact during rollback. Route unsafe
or unverified models to the pricing rejection path; rolling back to silent zeros
is not an acceptable accounting rollback.

## Operations

Call the authenticated generation reconciler on the existing schedule. It retries
stored accounting evidence and logs `accounting_reconciliation_alert` when stale
unresolved/pending records or detailed-log differences exist. Monitor also
`cost_unresolved`, `cost_capture_failed` and `cost_projection_retry_failed`.
Wire these structured errors into the deployment's alerting destination before
promotion. The admin report provides a bounded issue list for investigation.

Compare detailed logs and journal totals continuously, daily aggregates at complete
UTC buckets, and provider bills daily at supported account/project/SKU/time scope.
Track discounts, credits, tax and invoice adjustments separately. Do not distribute
an aggregate invoice delta among requests without supporting allocation evidence.

Reopen official pricing sources before `verify_by` dates and on vendor notices.
Unexplained zero amounts, duplicates, missing records, aggregate divergence,
registry/upstream drift and records unresolved over five minutes require attention.
Keep numeric journal records at least 13 months and retain unresolved records
indefinitely until reviewed. Media cleanup is independent.

Outstanding release evidence must remain explicit; do not mark a blocked profile,
production restore, paid acceptance or production rollout as completed by inference
from local tests.
