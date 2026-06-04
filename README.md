## LiteLLM Proxy (ai-gateway)

This repo packages a LiteLLM Proxy plus custom handlers for:

- **Grok video** (`grok-video` / `grok-imagine-video`; **1.5** via `grok-video-1.5` → `grok-imagine-video-1.5-preview`)
- **Seedance 2.0** (BytePlus ModelArk video)
- **Seedream 5** (BytePlus ModelArk image)

It can run locally via `docker-compose` and in production on **Google Cloud Run**.

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
  --tag us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy:latest
```

### 2. Deploy to Cloud Run

```bash
gcloud run deploy ai-gateway-proxy \
  --image us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy:latest \
  --platform managed \
  --region us-central1 \
  --no-allow-unauthenticated \
  --port 8080
```

Then configure environment variables (via `gcloud run services update` or the console):

- `LITELLM_MASTER_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`
- `GROK_API_KEY`
- `BYTEDANCE_API_KEY` (Seedance 2.0 / Seedream 5 / BytePlus ModelArk)
- Optional Seedance tuning: `SEEDANCE_ARK_BASE`, `SEEDANCE_ARK_MODEL`, `SEEDANCE_POLL_INTERVAL_S`, `SEEDANCE_POLL_TIMEOUT_S` (default **1200s** / 20 min server-side poll — see `.env_example`). Ensure HTTP timeouts (e.g. Cloud Run `--timeout`) stay above that plus overhead.
- `ELEVENLABS_API_KEY`
- **Vertex (`vertex_ai/*` models in `litellm_config.yaml`)**: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (e.g. `us-central1`). On Cloud Run, attach a service account with Vertex permissions; local Docker may need `GOOGLE_APPLICATION_CREDENTIALS` (or `VERTEXAI_CREDENTIALS`) pointing at a key file.
- Any optional `VERTEXAI_*` / `ELEVENLABS_*` vars you still use elsewhere

Do **not** set `MLX_VLM_API_BASE` in Cloud Run if you are not running MLX in GCP.

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
  - `gpt-latest`, `gpt-5.5`, `gpt-5.4-mini`, `gemini-latest`, `gemini-3.5-flash`, `imagen-4.0`, `grok-video`, `grok-video-1.5`, `seedance-2.0`, `seedream-5.0`, `seedream-5.0-lite`, etc.

Clients **never** send provider API keys or upstream URLs; only the proxy holds those in its environment.
