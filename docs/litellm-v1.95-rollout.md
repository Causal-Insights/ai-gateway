# LiteLLM v1.95 rollout runbook

The application image is pinned to LiteLLM `v1.95.0` by immutable amd64 digest.
Roll out the runtime upgrade and the additive model changes as separate Cloud Run
revisions even when they originate from the same release branch.

## Database migration gate

1. Confirm production point-in-time recovery and restore the latest backup into
   an isolated clone. Never point local Docker Compose at the production URL.
2. Set `MIGRATION_AUDIT_DATABASE_URL` to the clone, then capture the baseline:

   ```bash
   python scripts/migration_id_snapshot.py --output /tmp/litellm-before.json
   ```

3. Start one no-traffic v1.95 revision against the clone with
   `RUN_LITELLM_MIGRATIONS=true`. The image invokes LiteLLM's migration CLI
   with migration checks, the safer v2 resolver, and fail-closed behavior before
   starting the gateway. The deployment helper forces this revision to one
   instance and refuses to combine migrations with a traffic deployment.
4. Compare the post-migration database:

   ```bash
   python scripts/migration_id_snapshot.py --compare /tmp/litellm-before.json
   ```

5. Require identical identifier counts and hashes, no unvalidated foreign keys,
   identical spend-history table row counts, successful virtual-key
   authentication, unchanged budgets/spend history, and
   retrieval of existing queued, completed, and failed generation jobs.

The custom `gateway_generation_jobs` migration is unchanged. Omni continues to
use provider `vertex` and modality `video`, so no custom database migration is
required.

## Revision sequence

1. Build an immutable application tag and deploy the v1.95 runtime revision with
   the prior model configuration at no traffic. Run the compatibility suite from
   `tests/test_litellm_compatibility.py` and the existing provider smoke tests.
   The deployment helper refuses an empty or `latest` tag and can stop after the
   no-traffic gateway candidate is created:

   ```bash
   ./deploy_cloud_run.sh --tag litellm-v1.95-runtime-<commit> --candidate-only --run-migrations
   ```

   After the database gate passes, create the serving candidate from the same
   image without `--run-migrations`; this prevents scale-out instances from
   racing the migration:

   ```bash
   ./deploy_cloud_run.sh --tag litellm-v1.95-runtime-<commit> --candidate-only
   ```

2. Promote the serving revision through 1%, 10%, 50%, and 100% traffic gates.
3. Deploy the additive model/adaptor revision at no traffic. Run the new model
   contract tests and the paid provider probes, then repeat the same traffic gates.
4. Keep the previous revision deployable for at least 48 hours.

At each gate, compare authentication errors, provider 4xx/5xx, latency, spend and
token totals, duplicate or missing spend records, durable-job queue age, poll and
callback failures, and content retrieval errors. Stop promotion on any regression.

## Paid provider probes

- Verify that `grok-video-1.5` returns exactly `grok-imagine-video-1.5` for text,
  image, reference-image, and preset-voice requests. Only after all four probes
  pass, set `GROK_VIDEO_15_CONTRACT_VERIFIED=true` on the candidate revision.
  Editing and extension have a separate fail-closed gate. Set
  `GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED=true` only after both operations pass
  equivalent exact-model and request/response contract probes. Leave either flag
  false to return a validation error without submitting a paid provider request.
- Verify Omni entitlement in the target Google Cloud project, then submit the
  cheapest text, first-frame, reference, source-video edit, and stateful edit jobs.
- Run cheapest-setting regression jobs for legacy Grok video, Grok Image Quality,
  Seedance, Seedream, and Veo before production promotion.
