## LiteLLM Proxy (ai-gateway)

This repo packages a LiteLLM Proxy plus custom handlers for:

- **Grok video** (`grok-video` / `grok-imagine-video`; **1.5** via `grok-video-1.5` → `grok-imagine-video-1.5`)
- **Gemini Omni Flash 1.1** durable audiovisual video generation, editing, interpolation, and extension
- **Grok Imagine Image Quality** generation and up-to-three-image editing
- **Seedance 2.0** plus disabled durable **Seedance 2.5** onboarding (BytePlus video)
- **Seedream 5** including **Seedream 5.0 Pro** (BytePlus ModelArk image)

It can run locally via `docker-compose` and in production on **Google Cloud Run**.

Long-running video models also expose the durable job API: submit once with
`POST /v1/generation-jobs`, check `GET /v1/generation-jobs/{id}`, and stream the completed
asset from `/content`. See [docs/durable-generation-jobs.md](docs/durable-generation-jobs.md)
and [ADR-001](docs/ADR-001-durable-async-generation-jobs.md). The older blocking video calls
remain available during migration but are deprecated and emit structured usage events.
Use the [LiteLLM v1.95 rollout runbook](docs/litellm-v1.95-rollout.md) for the database-integrity
gate, compatibility checks, provider probes, and staged Cloud Run traffic promotion.

---

## Local development

Local LiteLLM uses the `postgres` service in `docker-compose.yml`.

```bash
cp .env_example .env
./run_gateway.sh
```

`run_gateway.sh` builds the image, runs checked LiteLLM migrations against the
local Compose database, and waits for an authenticated liveliness check before
reporting success. It does not modify `.env`. Use `./run_gateway.sh --no-follow`
for a non-interactive startup; otherwise it follows the gateway logs after the
health check passes. The local gateway explicitly runs the production-pinned
amd64 image under Docker emulation on Apple Silicon.

Local Vertex requests use the host gcloud Application Default Credentials file
at `/Users/jason/.config/gcloud/application_default_credentials.json`, mounted
read-only into the container. If the file moves, set `LOCAL_GOOGLE_ADC_PATH` to
its host path. Create or refresh it with `gcloud auth application-default login`.

Do not put the production database URL in `.env`. If you need to override the
local database, set `LOCAL_DATABASE_URL`; `docker-compose` intentionally ignores
`DATABASE_URL` for the LiteLLM container.

Reset local DB state without touching production:

```bash
docker compose down -v
```

After the local Gateway is healthy, the Seedream Pro acceptance smoke must be
explicitly acknowledged and only targets a loopback URL:

```bash
python scripts/smoke_seedream_5_pro.py \
  --confirm-paid \
  --api-base http://127.0.0.1:4000
```

The script refuses non-loopback hosts, submits one image, downloads it, and
verifies the exact requested dimensions and PNG/JPEG encoding.

---

## Cloud Run deployment (GCP project ID `ai-gateway-495414`, region `us-central1`)

### Quick redeploy (script)

From the repo root:

```bash
./deploy_cloud_run.sh
```

Common overrides:

```bash
PROJECT_ID=ai-gateway-495414 REGION=us-central1 ./deploy_cloud_run.sh --memory 2Gi --cpu 2
```

### 1. Build and push the image

Assuming:

- GCP project ID: `ai-gateway-495414`
- Region: `us-central1`
- Artifact Registry repo: `us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway`

```bash
gcloud config set project ai-gateway-495414
gcloud services enable run.googleapis.com artifactregistry.googleapis.com

gcloud builds submit \
  --tag us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy:<immutable-release-tag>
```

### 2. Deploy to Cloud Run

```bash
gcloud run deploy ai-gateway-proxy \
  --image us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy:<immutable-release-tag> \
  --platform managed \
  --region us-central1 \
  --no-traffic \
  --no-allow-unauthenticated \
  --port 8080
```

Then configure environment variables (via `gcloud run services update` or the console):

- `DATABASE_URL` (Secret Manager; production only)
- `LITELLM_MASTER_KEY`
- `OPENAI_API_KEY`
- `GROK_API_KEY`
- `BYTEDANCE_API_KEY` (Seedance 2.0 / Seedream 5 / BytePlus ModelArk)
- `SEEDANCE_2_5_API_KEY` (separate disabled Seedance 2.5 LAS durable-job credential)
- Optional Seedance 2.5 settings: `SEEDANCE_2_5_BASE_URL`, `SEEDANCE_2_5_PRICE_PER_SECOND_480P`, `SEEDANCE_2_5_PRICE_PER_SECOND_720P`.
- Optional Seedance legacy tuning: `SEEDANCE_ARK_BASE`, `SEEDANCE_ARK_MODEL`, `SEEDANCE_POLL_INTERVAL_S`, `SEEDANCE_POLL_TIMEOUT_S`. These settings only support deprecated blocking calls; new consumers should use durable jobs.
- `ELEVENLABS_API_KEY`
- **Vertex (`vertex_ai/*` models in `litellm_config.yaml`)**: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (e.g. `us-central1`). On Cloud Run, attach a service account with Vertex permissions; local Docker may need `GOOGLE_APPLICATION_CREDENTIALS` (or `VERTEXAI_CREDENTIALS`) pointing at a key file.
- Any optional `VERTEXAI_*` / `ELEVENLABS_*` vars you still use elsewhere

`./deploy_cloud_run.sh` binds required secrets explicitly with `--set-secrets`
and fails before build/deploy if `.env` contains the same database URL as the
production `DATABASE_URL` secret.

The deployment script also provisions the rate-limited `ai-generation-polls` Cloud Tasks
queue, one-minute reconciliation and daily cleanup Scheduler jobs, a warm main instance, and
the warm public `ai-gateway-callbacks` receiver. Task and Scheduler delivery use a dedicated
OIDC service account; the main gateway remains private.

If deploy fails with “failed to start and listen on PORT”, first check logs:

```bash
gcloud run services logs read ai-gateway-proxy --region us-central1 --limit 50
```

Common causes:

- Missing required env vars referenced by `litellm_config.yaml` on first boot (set secrets **before** or during deploy).
- Container not binding `0.0.0.0:$PORT` (this image uses `litellm --host 0.0.0.0 --port $PORT`).

### 3. Health check

Once deployed, verify the service:

```bash
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -X POST "https://<cloud-run-url>/v1/chat/completions" \
  -d '{
    "model": "gpt-5.4-mini",
    "messages": [{"role": "user", "content": "Hello from Cloud Run"}]
  }'
```

Replace `<cloud-run-url>` with the HTTPS URL shown by `gcloud run deploy`.

---

## Client integration contract

- **api_base**: the Cloud Run URL, e.g. `https://ai-gateway-proxy-xxxxx-uc.a.run.app`
- **Authorization**: `Bearer <proxy_token>` where:
  - Initially, `<proxy_token>` can be the value of `LITELLM_MASTER_KEY`.
  - Later, you can move to per-client keys managed by LiteLLM.
- **Models**: use the logical model names from `litellm_config.yaml`, e.g.:
  - `gpt-latest`, `gpt-5.6-sol-medium`, `gpt-5.6-terra-medium`, `gpt-5.6-luna-medium`, `gpt-5.6-luna-high`
  - `gemini-latest`, `gemini-3.7-flash`, `gemini-3.5-flash-lite`, `gemini-omni-1.1-flash`
  - compatibility aliases `gemini-omni-flash`, `gemini-omni-flash-preview`, and
    `gemini-omni-1.1-flash-preview` resolve to the same Gemini Omni Flash 1.1 Vertex upstream
  - `imagen-4.0`, `grok-video`, `grok-video-1.5`, `grok-imagine-image-quality`, `seedance-2.0`, `seedream-5.0`, `seedream-5.0-lite`

Clients **never** send provider API keys or upstream URLs; only the proxy holds those in its environment.
