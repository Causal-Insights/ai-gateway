# Every provider execution is recorded

> **IF A PROVIDER API CALL INCURS A COST, THE RUN AND ITS COST MUST BE LOGGED. ALWAYS.**
>
> We can have unattributed costs. We cannot solve attribution or accounting
> problems by hiding or dropping a real API cost.

Execution evidence and customer billing decisions have separate lifecycles.
Authentication and pricing admission can reject a request **before submission**.
After submission, attribution, model validation, feature flags, budget tables,
cache invalidation and reconciliation cannot veto the execution record.

## Persistence boundaries

1. Before the provider call, atomically persist its journal attempt and LiteLLM
   execution row. Unknown spend is null. Failure stops submission.
2. Append numeric provider receipts before extraction/calculation. Duplicate
   receipts reuse the attempt; independent billable retries have separate IDs.
3. Commit known cost, usage and execution metadata independently of financial
   projections. Unknown costs remain visible. Retain explicit charges and SDK
   calculations even when served-model validation needs review.
4. Apply daily/budget projections once in a separate transaction. Retry failures
   from durable evidence. Incomplete attribution never rolls back step 3.
5. Emit numeric `gateway_execution_receipt` events independently of optional
   LiteLLM logging flags. A DB outage leaves a visible pending execution plus
   recovery evidence in Cloud Logging. Replay exported receipts with
   `scripts/replay_execution_receipts.py` (dry run by default; `--apply` writes).

`attribution_status`, `attribution_reason`, `pricing_issue`, `projection_status`
and `projection_error` express work remaining. `billing_eligible` controls
automatic settlement; it never controls visibility. Preserve existing key
ownership and known exact decimal amounts. Do not fabricate a user/team, a zero
cost, historical rates or invoice precision.

## September 16 suppression-path audit

| Path or condition | Enforced behavior |
|---|---|
| Canonical key has no user/team | Log known cost; mark attribution unresolved; update the known key's spend and an unattributed daily bucket. |
| Missing identity after submission | Keep execution and cost; preserve the key already recorded; flag attribution. |
| Pricing disabled/changed, unknown served model/options, extractor/calculator error | Keep usage, explicit provider charge or existing calculated amount; flag pricing review and hold unsafe financial projection. |
| Unknown/partial usage or stream disconnect | Keep visible null-spend execution, never misleading zero; accept later evidence. |
| Daily/budget transaction failure | Execution commit survives; retry projection idempotently. |
| Optional LiteLLM callbacks/logging flags bypassed | Awaited SDK boundary and mandatory deployment/stream hooks capture evidence; stock logger forwards to the same writer. |
| Lost callback/attempt mapping | Recover a stable execution ID from the gateway call/provider ID; keep it unattributed if necessary. |
| Wrapped provider exception or failed generation with usage | Preserve original numeric usage through exception chains and adapter failure results. Apply a final failure only to its actual attempt. |
| Malformed media result after billable completion | Preserve usage even if media URL/content validation fails. |
| Repeated polls, callbacks or recovery attempts | Upsert one execution; row locks and projection marker prevent duplicate budget increments. |
| Late/richer/conflicting receipts | Fill missing evidence; retain all observations and prior cost; conflicts become review metadata. |
| Background job lacks accounting ID or was falsely marked logged | Recover retained execution without applying today's rates; check actual detailed row, not a flag alone. |
| Detailed row was deleted | Recreate from journal without reapplying budget totals. |
| Media cleanup | Never delete unresolved accounting evidence; numeric journal is independent of media retention. |
| DB unavailable after response | Pending execution remains; emit/replay numeric receipt without resubmitting provider. |
| Reservation release/cache invalidation fails | Cost record already committed; cache outbox and fresh DB budget floors support recovery. |

Early returns for free status/content reads, nonterminal streaming chunks and
already-visible duplicate records do not discard billable executions. The original
attempt remains visible. Admission guards remain before provider submission.

## Required regression evidence

`tests/test_execution_logging.py`, `tests/test_cost_accounting_db.py` and
`tests/test_accounting_runtime.py` run in the pinned image with isolated PostgreSQL.
CI also runs the durable-job repository suite. Assertions cover log/journal
amounts, unknown nulls, standalone keys, retries, concurrency, projection failures,
late/conflicting receipts, missing logs, retained jobs, exception usage and callback
bypass. A generated Prisma-client check prevents SQL/ORM nullability drift.

Before deploying changes to these hooks, exercise the real `/spend/logs/ui` API:
one known-cost execution with no user/team attribution and one null-spend pending
execution must both be returned. A mocked provider response is sufficient to
verify this integration without buying another generation.

The confirmed Studio Pro regression had 3,030 prompt tokens (2,048 cached), 133
completion tokens and a calculated cost of **$0.00039696**. Its saved receipt can
be replayed without invoking the provider. The repair is idempotent and does not
change settled MagicLens charges.

Media-job completion markers are also written after the execution transaction.
Their failure cannot roll back cost persistence. CI explicitly simulates that
failure and malformed secondary usage fields while retaining a known charge.

## Original execution time

Historical recovery must copy **both** original start and completion timestamps
into the LiteLLM row, including on an upsert of a pending recovery row. The import
or reconciliation time is not the execution start time. Updating only the end
would make historical durations negative and place executions on the wrong date.
`tests/test_execution_logging.py` checks an old job's exact positive duration.

`python scripts/repair_execution_timestamps.py` previews timestamp discrepancies
for retained executions with valid journal evidence. `--apply` corrects those
fields with versioned before/after receipts; it never updates usage, costs,
aggregates or budgets. A repeated run must find zero corrections. Do not hide
invalid durations by clamping them to zero.
