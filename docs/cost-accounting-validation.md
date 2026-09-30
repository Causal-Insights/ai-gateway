# Accounting validation — September 16, 2026

Jason confirmed successful local testing and explicitly approved redeployment on
September 16, 2026. Production promotion completed at 21:55:10 UTC after no-traffic
candidate checks. The earlier intermediate Cloud Build image predates the final
duplicate-hook fix and must not be promoted.

## Completed checks

- All 56 configured aliases have enabled pricing profiles; zero model-level
  pricing blocks. LiteLLM is the default source for supported catalog models.
- Pinned LiteLLM 1.95.0 offline suite: 214 tests passed, 30 database tests skipped.
  The additional Seedance multimedia regression passed in the focused six-test
  catalog suite; all configured aliases have representative nonzero calculations.
  Veo silent/audio rate regressions were added for all three configured variants.
- Isolated PostgreSQL accounting/native-hook suite: 42 tests passed. Durable job
  database suite: three tests passed. These cover transactions, duplicate events,
  budgets, ownership, rollback/retry, historical idempotency and stream evidence.
- A private production backup was restored into isolated PostgreSQL 17. The
  gateway public schema was restored; Supabase platform extensions are unrelated
  to gateway accounting. Additive migration preserved hashes/counts of 59 keys,
  one user, three teams, 3,496 detailed spend rows, 775 daily rows and 81 jobs.
- Local cloned-database candidate returns healthy readiness and all 56 model
  aliases. Full HTTP validation identified duplicate hook registration from both
  YAML and gateway startup; YAML registration was removed so startup owns it once.
  A complete mock-provider HTTP request then returned `priced`, $0.000165, one
  accounting attempt and exactly one matching detailed spend row.
- ModelArk read-only task listing authenticated successfully with the existing
  BYTEDANCE_API_KEY. Seedance 2.5 now uses ModelArk, not LAS, including reference
  video/audio inputs and token pricing. No paid generation test was performed.
- Fifty structured vendor price reports passed validation. Skill-creator
  validation passed; repository and personal Price Verification copies match.

## Specific option restriction

Seedream web-search requests remain restricted because an applicable separate
search surcharge has not been established. Standard image generation/editing is
priced. Other provider capability restrictions are independent of model pricing.

## Local handoff and subsequent rollout

The local gateway uses its existing local PostgreSQL database. A private backup
and `ai-gateway-litellm:before-pricing-20260916` preserve the prior local state.
Local Magic Lens testing and production deployment approval are complete. Build
the final source into a fresh immutable Cloud image; do not promote the earlier
intermediate build.

Historical provider-cost repair, settled customer charges and provider invoice
reconciliation are separate. No production financial records were corrected.

Final local image: `ai-gateway-litellm:pricing-20260916`, digest
`sha256:209e566a1e52bc87336f1ccde8df88f2546092daea95546e5ae36a88eaf25734`.
`http://localhost:4000/health/readiness` is healthy and authenticated `/v1/models`
returns all 56 aliases, including Seedance 2.5. All 91 runtime files in the release
source were compared with the running local image and matched exactly.

## Production release

- Cloud Build: `ab0c887d-1353-4974-bc3f-116778c30ec1` (successful).
- Image: `us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy:pricing-20260916-release`.
- Immutable digest: `sha256:b06becddb23c99637c9b98dc7c359b7ad6632146c68cb22479967e2b76ab0958`.
- Gateway: `ai-gateway-proxy-pricing-20260916-r3`, 100% traffic, Ready=True.
- Callbacks: `ai-gateway-callbacks-pricing-20260916`, 100% traffic, Ready=True.
- Previous serving revisions: `ai-gateway-proxy-video-fbf6dff` and
  `ai-gateway-callbacks-00005-dum`. Their revision records remain available.

The first two gateway candidates encountered the existing Supabase session-pool
limit. `DATABASE_URL` secret version 3 preserves the credentials and adds Prisma
`connection_limit=3&pool_timeout=30`; both services now set
`GENERATION_DB_POOL_SIZE=2`. The deployment helper preserves that pool setting.
Unused `omni11` and `openai-sep9` preview tags had no requests in the preceding
seven days and were retired. Old serving tags were removed during promotion so
retired revisions do not retain warm instances. The final gateway revision started
successfully with the same application image; failed candidates received no
production traffic.

There were zero active generation jobs immediately before cutover. Accounting
tables were created additively. Production checks before and after deployment
found 59 keys, 3,496 detailed spend rows and 81 retained jobs. Existing gateway
access rules, provider credentials and reconciliation schedules were preserved.
New requests use the journal writer; old requests remain owned by their original
revision until completion. No historical cost correction was applied.

Post-cutover checks passed for authenticated liveliness/readiness, database
connectivity, all 56 model aliases, the admin reconciliation report, and the
expected owner-scoped 404 for an absent cost record. The report returned no issues,
unfinished requests, aggregate divergence or expired pricing. No new inference
requests had entered the journal at the time of these checks; live provider-cost
acceptance was not inferred from an empty report. Local acceptance evidence remains
listed above.

The callback candidate passed startup and database-backed rejection of an unknown
job/token. Its external `/healthz` path is intercepted with a Google frontend 404;
this also occurred on the prior revision. Cloud Run documents
[reserved paths ending in z](https://docs.cloud.google.com/run/docs/known-issues#reserved-url-paths).
Callback validation used the actual callback route rather than that external path.

## Execution-logging regression repair — September 16

The previous release incorrectly treated missing user/team attribution as a gate
on the LiteLLM detailed row. The confirmed Studio Pro run already had numeric
usage and a calculated **$0.00039696** cost; neither was absent.

The repair separates mandatory execution persistence from attribution and
financial projections. See [the invariant and suppression audit](execution-logging-invariant.md).

Validation completed against the pinned LiteLLM image and isolated PostgreSQL:

- **256 tests passed**, including all database-backed accounting and durable-job
  tests. Release coverage checked **56 aliases / 121 profiles**, with zero errors.
- The actual local `/spend/logs/ui` API returned exactly one **$0.00039696** row
  per simulated Studio Pro call using a key with no user/team. Both ordinary calls
  and `disable_logging: true` calls were recorded; attribution remained unresolved.
- The same Logs API returned a pending execution with `spend: null`, using the
  regenerated nullable Prisma client. No provider API was called in these tests.
- Applying migration 004 to the restored production database clone left existing
  key/user/team, spend, daily aggregate and job fingerprints unchanged.
- Reproducing the missing-attribution repair on that clone restored the known cost
  and incremented the known key's gateway spend once. A second reconciliation
  changed no cost or budget total. Settled customer charges are separate.
- A fresh private production backup was taken before the no-traffic candidate.

Final image build: `a12865b5-61dd-4dc3-9265-75e863f77fd8`; image digest
`sha256:28ad6011c1141fcc877b6d8b4403402b39c8238c934bb91a2d675066d79d238d`.

Production rollout and recovery completed on the same image:

- Gateway: `ai-gateway-proxy-execution-20260916` (100% traffic).
- Callback receiver: `ai-gateway-callbacks-execution-20260916` (100% traffic).
- The actual production Logs API returns exactly one row for accounting ID
  `cost_48a228cd7bdd4633b46c5458594a8956`, cost **$0.00039696**, 3,030 prompt
  tokens and 133 completion tokens. Attribution remains unresolved metadata.
- All **81 retained jobs** now have execution records: 65 preserve existing
  unverified amounts and 16 retain unknown/null cost. None was repriced with
  current rates or made eligible for automatic settlement by this import.
- A second reconciliation left spend-row counts, key-budget fingerprints, daily
  aggregate fingerprints and the original correction receipt count unchanged.
- Production health and all 56 aliases passed; missing execution rows, retained
  jobs without logs, unfinished requests and aggregate divergence are all zero.
- One transient projection deadlock during overlapping revision startup was
  recovered. Its execution row remained committed; the historical row remains
  held for pricing review, with the last projection error retained as metadata.
  Review findings are not missing logs or unavailable models.

Machine-readable acceptance evidence: [execution-logging-validation.json](execution-logging-validation.json).

## Historical duration correction — September 16

The initial retained-job backfill inserted a pending log using recovery time, then
updated only its end time with the original completion time. This caused all 81
recovered jobs to show negative durations. Their journal and original job times
were intact; the Studio Pro run's 5.937-second duration was unaffected.

The writer now updates both timestamps from the journal on every upsert.
`scripts/repair_execution_timestamps.py` restored all 81 production records using
versioned before/after receipts. A second apply matched zero records; the database
contains zero negative durations. The actual Logs API reports 31,035 ms for the
example Grok job, with its $0.71 cost unchanged. Repair changes timestamps only.

Validation: **36 focused database tests passed**, including original historical
start/end times and repeat repair without cost or budget changes. Candidate health,
all 56 models and the real Logs API passed. Image build:
`f90a7ad8-a4c9-40e1-99f6-85302b42773a`; digest
`sha256:583643d1f094ca6fb8b37e425d63b71d135c12f3382fe91c147137b727da2414`.
The gateway now serves 100% of traffic on `ai-gateway-proxy-timestamps-20260916`.
The callback receiver is unchanged; it does not write execution log timestamps.
