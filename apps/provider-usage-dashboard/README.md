# Provider Usage Dashboard

Independent internal reporting for OpenAI, xAI, BytePlus, ElevenLabs, Google Cloud, and MiniMax. The browser reads the reporting cache; only the collector contacts providers or source databases. Nothing imports the Gateway runtime, changes its schema, or sends inference requests.

## Run locally

Run these commands from `apps/provider-usage-dashboard`:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m dashboard demo --port 8765
```

Open `http://127.0.0.1:8765`. The demo clearly labels its illustrative data and makes no provider calls. Local mode binds to loopback. Production always requires IAP authentication and uses Firestore.

For real reports, copy `.env.example` to `.env`, populate the reporting credentials that are available, then run:

```sh
.venv/bin/python -m dashboard sync --env-file .env
.venv/bin/python -m dashboard serve --env-file .env --port 8765
```

The local cache is `.local/reporting.json`. The file and credentials are ignored by Git and excluded from builds. Missing configuration produces ×; it does not prevent the rest of the dashboard from working.

## Controls and interpretation

- Provider and model views have sortable headings. Missing values sort last in either direction. Monetary sorting uses USD; other currencies remain separately visible. Native quantities sort only in the selected unit.
- Today, last seven days (default), last 30 days, month to date, previous month, and custom dates apply to cached reporting. Provider selection applies throughout; model search changes only model rows.
- Expand a provider for exact model versions, supplementary Gateway aliases, adjustments, Google service subdivisions, and ElevenLabs allowance details. Models absent from the Gateway catalog remain visible. Unattributed costs remain Unassigned.
- Daily charts show USD cost by provider and requests or generations separately. A selected/expanded provider also shows daily costs for its ten largest attributed models.
- Known costs are retained when another provider or part of a period is unavailable. × means unavailable; an explicitly reported zero is zero. Balances always show their latest snapshot timestamp, independent of the date selection.
- Provider reporting dates are UTC except BytePlus's UTC+8 days. Those amounts are never redistributed across UTC days. Today's summary follows the source's own reporting day. Combined daily charts group these labeled reporting dates, not identical worldwide time intervals.
- Usage, payments, and adjustments are separate. Top-ups never increase usage cost. ElevenLabs allowance credits are not dollars. There is no currency conversion or allocation of subscription fees to daily/model usage.
- Comparisons display supplier costs from provider reporting, Gateway, LiteLLM, and Magic Lens. LiteLLM is populated by Gateway accounting and represents the same spending. Differences are left minus right, with percentages relative to the right-hand total. Different scope and partial coverage remain labeled. Neither independent activity nor missing history is called an accounting error.

## Official integrations

| Provider | Source and limits |
| --- | --- |
| OpenAI | [Organization Usage](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage) and Costs APIs, using an organization admin key. Pagination completes before replacement. Costs and usage retain their different dimensions. Only explicitly identified cost line items receive a model; no token-price allocation. Prepaid balance and payments remain ×. |
| xAI | [Management billing](https://docs.x.ai/developers/rest-api-reference/management/billing). A team-scoped management key can supply its own team ID through validation; otherwise set `XAI_TEAM_ID`. Usage analytics USD is already dollars. Balance-change cent amounts use the documented balance sign; only successful purchases count as top-ups. Credit adjustments remain separate. Invoice balances are not assumed paid. |
| BytePlus | [ListBillDetail](https://docs.byteplus.com/en/docs/byteplus-platform/ListBillDetail) and [GetInferenceUsage](https://docs.byteplus.com/en/docs/modelark/get-inference-usage-api), signed with dedicated international access keys. Billing pagination and model/version columns are preserved. Usage dates are UTC+8. Consumption-bill `PaidAmount` is not a top-up. |
| ElevenLabs | [Workspace analytics](https://elevenlabs.io/docs/api-reference/analytics/workspace/usage) and [subscription](https://elevenlabs.io/docs/api-reference/user/subscription/get). Model/product usage uses returned column units. Subscription allowance, use, reset, and overage remain snapshot information. A missing dollar cost is ×. |
| Google Cloud | [Cloud Billing BigQuery export](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery). Configure `GOOGLE_BILLING_TABLE=project.dataset.table`. Standard/detailed export cost, service, SKU, project, usage units, explicit model labels, and credits are aggregated. Credits are summed without multiplying the cost rows. Regular usage charges are shown before credits; taxes and adjustments are separate. Export history is not assumed to cover dates before export was enabled. |
| MiniMax | [Official V2 task list](https://platform.minimax.io/docs/api-reference/video-generation-v2-list), using `MINIMAX_USAGE_API_KEY`. Reports visible video/Context-IR tasks from the latest seven days, exact models, completed video generations, and metered seconds/images/tokens. Daily aggregates use task creation time in UTC; the partially expired boundary day is excluded to preserve cached history. Task counts are not HTTP request counts. Provider-reported pay-as-you-go costs, balances and payments remain unavailable. H3 generation spending is estimated separately from metered input/output seconds, resolution, and input image counts at posted API rates. No subscription-quota substitution, prompts or media are stored. |

Generated credentials must still pass their reporting endpoints. A successful inference key is not proof of reporting access. Adapters make no test inference calls.

For OpenAI, create an **Organization → Admin keys** key with **Restricted → Usage API Scope → Read**, leaving the other scopes at None. Store it as `OPENAI_USAGE_API_KEY`; a project inference key cannot substitute for this credential. Publish the new value to the dashboard's Secret Manager secret before running the deployed collector. Search usage uses the official `web_search_calls` and `file_search_calls` endpoints. Billing model names are preserved as reported: an unversioned billing label is not silently assigned to a dated model from the Usage API.

xAI uses a management key in `XAI_USAGE_API_KEY`. Successful automatic purchases are top-ups, just like successful manual purchases; prepaid spend deductions are omitted from funding activity because usage is already counted in the usage report. ElevenLabs uses a regular restricted API key in `ELEVENLABS_USAGE_API_KEY`, with **User → Access**, **Workspace → Read**, and **Workspace Analytics → Access**. Use the full secret shown at creation, not the key's ID or a credential from another provider.

ElevenLabs analytics supports both millisecond timestamps and ISO `DateTime` columns. Its explicit `total_cost` field with a fiat unit is reported usage cost; unit prices are never treated as costs, and subscription fees are not allocated. Credits, minutes, and `usage_count` remain native quantities: usage events are not assumed to be HTTP requests or generations. An extra bucket at the next day's boundary is excluded from the selected interval.

### MiniMax estimated spending

As requested, MiniMax H3 **generation** rows include a separate posted-rate estimate. Rates verified on October 2, 2026 are USD 0.08 per input/output video second at 768p, USD 0.13 at 2K, and USD 0.04 per input image beyond five **per task**; input audio is free. Token-equivalent counters are never charged again. See [official pricing](https://platform.minimax.io/docs/guides/pricing-paygo) and the saved [768p](docs/minimax-estimate-768p.json) / [2K](docs/minimax-estimate-2k.json) numeric evidence.

The collector calculates decimal estimates from provider-metered quantities before daily aggregation and saves the amount with its rate-version basis. Cached history is not repriced during report reads. Missing components retain the calculable subtotal, labeled partial; missing resolution, unsupported models/task types, or entirely missing billable usage leave the task unpriced. Unknown quantities are never interpreted as free usage. Regeneration and Context-IR usage remain visible but are not estimated by this H3 generation formula.

The API keeps `cost` (reported), `estimated_cost`, and `spending` (reported plus estimates for rows lacking reported cost) separate, including currencies. Summary cards, provider/model tables, sorting, and daily/model charts use spending and identify estimates. Provider comparison amounts and financial discrepancy warnings continue to use reported costs only; estimates do not create balance, payment, or credit records. The latest seven-day source window still limits historical coverage. Account discounts, tax, and missing extra-image charges are not inferred.

## Storage and collection

One image has `serve` and `sync` entrypoints. Firestore contains `days`, `payments`, `balances`, `status`, and small `meta` coordination documents. Each daily document contains aggregate rows for one source/provider/stream/reporting date. No prompts, media, or customer-level traces are retained.

The first collection retrieves the current month and starts backfilling older months. Subsequent hourly collections refresh seven days and another month of history until the retained 13 months are covered. Once per UTC day, current and previous billing months are refreshed. Unsupported source history remains missing. A failed historical window can be retried with the bounded collector command below; current collection and other providers continue.

Every required API page is fetched before saving a stream. Daily documents replace prior revisions atomically. Payment identifiers are stable. Unknown historical coverage does not erase previously reported rows; explicitly reported corrections replace them. Cache write failures may leave different days at different refresh times, but cannot duplicate a day's revisions. Each successful stream records freshness; failures keep prior data. A transactional 25-minute lease excludes overlapping scheduled/manual collectors; jobs have a 20-minute timeout and the Python runner allows 19 minutes.

```sh
.venv/bin/python -m dashboard sync --env-file .env \
  --provider openai --start 2026-08-01 --end 2026-08-31
```

Dates are inclusive and restricted to retained history. Omit `--provider` to refresh all providers. This is an operator command, not another dashboard workflow.

Warnings stay in the dashboard: completed-day cost >2× the prior seven-day mean with a ≥$10 increase; volume >3× with ≥100 additional requests/generations; new model after 30 covered days; comparable discrepancies >$5 and >10%; failed/stale (>2 hours) sources. Missing history prevents a trend conclusion. Discrepancy warnings require a period ending at least 48 hours ago. Available amounts remain displayed regardless.

## Read-only comparisons

Configure only dedicated `GATEWAY_REPORTING_DATABASE_URL` and `MAGICLENS_REPORTING_DATABASE_URL` credentials with SELECT on the needed tables. The Gateway reporting connection supplies both Gateway and LiteLLM reads. The collector never falls back to `DATABASE_URL` or invokes reconciliation code.

- Gateway: `gateway_cost_attempts` joined to `gateway_cost_requests`; recorded costs and historical model/profile identity, including charged failures and retries.
- LiteLLM: `LiteLLM_SpendLogs`, preferring `metadata.cost_usd_exact` over the float `spend` column.
- Magic Lens: `magiclens.financial_attempts` joined to `financial_runs`; supplier cost, across recorded Studio/Runner/other activity, including unresolved amounts. No ledger/evidence join that could duplicate attempts; no customer credit charges or revenue.

Connections are sequential, short-lived, read-only transactions with five-second connect/statement/command timeouts. Every connection closes after its aggregate query. Missing roles leave comparisons unavailable. Model aliases are a display-only snapshot in `dashboard/model_aliases.json`; no current pricing is used to reprice history.

## Independent deployment

`deploy.sh` builds only this directory. It never invokes the repository's Gateway deployment. It creates:

- Cloud Run service `provider-usage-dashboard` and job `provider-usage-dashboard-sync`.
- Named Firestore database `provider-usage-dashboard`, protected from deletion.
- Dedicated web, collector, and scheduler service accounts. Database access is restricted to this named database. Only the collector receives provider secrets; the web identity can read the cache and invoke the job.
- Cloud Scheduler hourly at minute 17 UTC, invoking the job with OAuth.
- Direct Cloud Run IAP, an explicit user/group allowlist, and application verification of signed IAP JWTs. There is no public fallback.

Publish prepared reporting secrets from an explicit local environment file:

```sh
.venv/bin/python scripts/publish_secrets.py --help
.venv/bin/python scripts/publish_secrets.py --env-file .env --project ai-gateway-495414
IAP_MEMBER=user:your-email@example.com ./deploy.sh
```

Check the helper's arguments before running. It reads only the reporting variable allowlist and passes secret contents on stdin, never command arguments. New values receive new Secret Manager versions. Existing inference secrets are neither read nor changed.

Set `GOOGLE_CLOUD_PROJECT`, `REGION`, `FIRESTORE_DATABASE`, optional `XAI_TEAM_ID`, and `GOOGLE_BILLING_TABLE` in the deployment shell. Defaults use project `ai-gateway-495414`, region `us-central1`. Grant the collector service account BigQuery Data Viewer **on the billing dataset**, including when that dataset is in another project. The deploy script grants query-job execution in the query project; it does not broaden access to all BigQuery datasets. Local Google reporting uses Application Default Credentials; production uses attached identity.

Direct IAP may require configuring an OAuth consent screen/client for the project first. Keep the service private if that setup is incomplete. Configure Cloud Billing export through the billing account's official export settings; the dashboard cannot reconstruct missing historical export data.

To run a bounded cloud refresh:

```sh
gcloud run jobs execute provider-usage-dashboard-sync --project ai-gateway-495414 \
  --region us-central1 --args=sync,--start,2026-08-01,--end,2026-08-31 --wait
```

## Validation

```sh
.venv/bin/python -m pytest -q tests
node --test tests/controls.test.mjs
bash -n deploy.sh
docker build -t provider-usage-dashboard:test .
```

The independent GitHub Actions workflow runs these checks. Tests cover source response fixtures, paging, monetary units, missing/zero/partial/stale reporting, currency separation, exact history, repeat collection, lease ownership, time boundaries, read-only connection settings, warning thresholds, auth/refresh boundaries, filters, and sorting. Browser checks cover actual controls and charts. Fixture success does not certify live provider permissions or production database access.
