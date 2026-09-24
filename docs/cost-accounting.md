# Gateway cost accounting

> **Non-negotiable: if a provider API call incurs a cost, the execution, usage,
> and cost must be recorded. We can have unattributed costs; we can never hide
> or drop real costs because attribution, pricing validation, budgets, feature
> flags, or downstream accounting failed.**

Provider execution logging and billing attribution are independent concerns.
`Incomplete billing attribution` is metadata on a visible run, never a reason to
withhold its LiteLLM Logs entry. See the [suppression-path audit](execution-logging-invariant.md).

The gateway owns provider-cost evidence. MagicLens continues to own quotes,
markups and credit settlement. A provider cost is not a customer retail price.

## Coverage

All 56 configured aliases have enabled pricing profiles; see the generated
[coverage table](pricing-coverage.md). LiteLLM catalog models are admitted without
another manual verification gate. Custom media profiles use documented API rates
and measured usage. Seedance 2.5 uses ModelArk and the shared BYTEDANCE_API_KEY.
Seedream web-search requests remain restricted because their separate surcharge
is not established; ordinary image generation and edits remain enabled.

## Price authority

LiteLLM remains a useful maintained pricing source. See
[how catalog verification differs from logging verification](litellm-pricing-verification.md).

`pricing/registry.json` is the gateway's versioned price authority. Each attempt
stores the complete selected profile, including evidence references and calculation
version. New registry versions do not silently reprice in-flight or historical work.
LiteLLM's maintained catalog and calculators are the default pricing engine.
The captured snapshot prevents later catalog changes from repricing prior usage. Unversioned `model_info` prices have been removed. Environment-variable prices are not
accepted as billing evidence.

Prefer an explicit provider-reported monetary usage charge, then verified account
rates. For subscription/credit products, use verified posted API usage rates without
allocating subscription fees. Keep the source visible. Missing invoice access, a review reminder or absent
per-alias acceptance paperwork does not disable a supported catalog model.

Custom calculations use decimal arithmetic and disjoint measured quantities.
Native calculations use the pinned LiteLLM SDK; its numeric result is retained as
a decimal string. Documented cache-calculation corrections use decimal arithmetic. Decimal
amounts are strings in the cost lookup response and PostgreSQL `numeric` in the
journal. LiteLLM's Float columns and the existing generation-job `cost_usd` number
are compatibility projections. Internal comparisons allow only documented float
representation tolerance, not unexplained billing differences.

Do not infer that absent usage means zero. A priced zero needs an explicit reason
allowed by the profile. A reported inclusive charge is never added to calculated
token/tool charges; both values are retained separately for comparison.

## Lifecycle and persistence

1. Authenticated admission resolves alias, upstream, route, billing options and
   current verified profile. Unknown/runtime-added profiles fail with HTTP 503 and
   `PRICING_UNVERIFIED` before provider submission.
2. Persist a request intent and a visible LiteLLM execution row **before** each
   provider attempt. Its unknown spend is SQL/JSON null, never zero. If this write
   fails, no provider call is allowed. Distinct billable retries get distinct IDs;
   duplicate callbacks and status polls reuse the same attempt.
3. Await native SDK/deployment/terminal-stream hooks or adapter completion capture.
   Append numeric receipts before calculating cost. Preserve charged failures,
   partial outcomes, actual model identity and provider IDs, without request text.
   SDK wrappers and recovery hooks also capture calls when a callback or mapping
   is missing. Logging flags cannot disable the mandatory execution writer.
4. Commit the execution cost and journal together, independently of daily totals,
   budgets, cache invalidation, attribution or customer settlement. Known costs
   remain visible even when validation fails; record the issue alongside them.
   A later empty or conflicting receipt cannot erase an existing cost.
5. Apply financial projections in a separate, idempotent transaction. Its failure
   leaves the execution intact and a retryable projection status. A canonical key
   without user/team membership still receives its key spend and an unattributed
   daily bucket; no fictional user or team is assigned.
6. Reconciliation repairs missing execution rows and retries financial projections
   without resubmitting providers or incrementing budgets twice. A database outage
   leaves the pre-existing run visible and emits a numeric Cloud Logging recovery
   receipt. Export/replay it with `scripts/replay_execution_receipts.py`; the default
   is a dry run and `--apply` replays saved evidence, never a provider call. Recover
   receipts before the deployment's Cloud Logging retention expires, then retain
   them in the journal for at least 13 months. No software can guarantee a write
   through simultaneous loss of every durable storage channel; alert on pending
   records and failed receipt persistence instead of reporting success as free.

Stock LiteLLM success/failure spend writes are replaced in the gateway process to
avoid two writers. Authentication, guardrails and unrelated callbacks remain active.
Reservation release remains awaited. Budget enforcement reads a fresh database
floor while retaining reservation-aware counters. Commit failures never cause an
unchecked repeated cache increment.

Ownership hashes of existing media jobs remain unchanged. Billing uses LiteLLM's
canonical key hash, without rehashing it. Accounting tables do not reference media
job/key rows by foreign key: evidence must survive cleanup and key deletion.

## Public contract

`GET /v1/costs/{accounting_id}` requires the originating key. Missing and unauthorized
records both return 404. Generation-job responses add the same accounting fields.

```json
{
  "accounting_id": "cost_example",
  "cost_status": "priced",
  "cost_usd": "0.71",
  "cost_source": "provider_reported",
  "pricing_version": "grok-video--grok-imagine-video-1.5@2026-09-16",
  "usage": {"attempt_example": {"cost_in_usd_ticks": "7100000000"}},
  "breakdown": [{"attempt_id": "attempt_example", "cost_status": "priced", "cost_usd": "0.71", "components": [], "reason": null}],
  "billing_eligible": true,
  "invoice_reconciliation_status": "not_reconciled"
}
```

`cost_status` describes cost knowledge, not attribution: `pending` awaits usage,
`unresolved` lacks a supported amount, and `priced` has a recorded amount.
A known cost can be `priced` while `attribution_status` is `unresolved` and
`billing_eligible` is false. Per-attempt `pricing_issue`, `projection_status` and
`projection_error` describe review/retry work without hiding the amount.
For mixed known/unknown attempts, `cost_usd` is null while `known_cost_usd` and the
breakdown retain the known subtotal. Automatic settlement requires all attempts
priced, complete attribution, successful projection, and no pricing issue. The
provider-cost total includes charged failures and successful retries.

Native responses include `x-gateway-accounting-id` and `x-gateway-cost-status`.
`x-litellm-response-cost` is emitted only for a known journal amount. Stream headers
are sent before completion; retrieve final cost using the accounting ID. Consumer
code must not interpret a missing cost header as zero.

`GET /v1/costs/reconciliation/report` requires a proxy administrator. It reports
stale pending/unresolved attempts and detailed-log discrepancies. The generation
reconciler also retries pending accounting writes and emits structured error logs.
Provider invoice reconciliation is a separate dimension and does not delay eligible
request-time usage pricing.

## Historical evidence and retention

Run `python scripts/historical_cost_audit.py --output audit.json` with a read-only
database credential. It does not apply corrections or call model providers.
The checked-in September 16 audit identified 68 completed retained jobs missing
detailed spend rows. Reconciliation now makes retained executions visible using
`retained-execution-v1`; stored amounts are labeled `legacy_gateway_evidence`,
unverified and ineligible for settlement. These imports do not apply current
prices to history or update customer budgets. Verified historical corrections
remain a separate reviewed operation. Existing job amounts are unverified evidence. The audit also
lists historical zero successes and daily/detail discrepancies; legacy Seedance
poll responses must not all be interpreted as billable completed generations.

The earlier seven-request discrepancy covered August 18–September 15. Including
August 17 finds one additional Seedream discrepancy. The report does not establish
which side of any mismatch is wrong. Repair requires original-date price evidence,
canonical identity, verified actual usage and an explicit correction record.
Current rates cannot substitute for missing historical rates. Settled retail
charges remain unchanged.

`scripts/repair_historical_costs.py MANIFEST.json` defaults to a read-only proposal.
The manifest is a JSON list of `job_id` and `pricing_version` pairs. `--apply`
imports supported missing-job costs with an idempotent correction receipt; it
does not repair existing detailed rows or infer which side of an aggregate
mismatch is correct. Those corrections still require a reviewed plan and evidence.

`scripts/reconcile_provider_bill.py EXPORT.json` compares a reviewed provider
usage-cost bucket without writing. Required fields are `provider`,
`account_context`, `currency: "USD"`, `period_start`, `period_end`,
`accounting_cutover_at`, `upstream_models`, `invoice_cost_usd`, `evidence`, and
`exclusive_gateway_scope: true`. The bucket must exclude non-gateway traffic and
precede neither the journal cutover nor its verified scope. `--record` stores the
comparison, not customer adjustments. Schedule attributable export comparisons
daily during rollout; this repository does not provision that external feed.

Retain numeric accounting evidence at least 13 months, independently of 30-day
media cleanup. Unresolved records must never be automatically deleted. This release
does not delete journal records; retention can be extended without affecting media.
