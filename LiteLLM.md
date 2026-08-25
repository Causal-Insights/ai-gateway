---
name: litellm
description: >-
  Call any model on the shared LiteLLM proxy: choose endpoint, logical alias,
  auth, and request fields. Covers chat, images, video (Grok, Seedance, Veo),
  and TTS. Use when invoking LITELLM_BASE_URL, LiteLLM, seedance, grok-video,
  grok-video-1.5, grok-image, Veo, Gemini, OpenAI, or model aliases from this gateway.
---

# LiteLLM proxy — integration skill

Agents and apps call **only this proxy** using OpenAI-compatible HTTP routes. Provider API keys live on the server (`litellm_config.yaml`); clients send a proxy bearer token and a **logical `model` alias**.

## Prerequisites

| Variable | Purpose |
|----------|---------|
| `LITELLM_BASE_URL` | Proxy host, no trailing slash (e.g. `http://localhost:4000` or Cloud Run URL) |
| `LITELLM_MASTER_KEY` | Bearer token for `Authorization: Bearer …` (or a LiteLLM virtual key) |
| `GROK_API_KEY` | xAI key — required on the **proxy**; also needed in **your shell** when uploading local images to `https://api.x.ai/v1/files` before `grok-video-1.5` requests |

Local setup: `cp .env_example .env` and fill in keys. Prefix `curl` examples with `set -a; source .env; set +a` so variables load in the same command.

Optional response headers (useful for billing/debug):

- `x-litellm-response-cost` — USD cost for the call when computed
- `x-litellm-call-id` — request id for logs

Discover live models: `GET ${LITELLM_BASE_URL}/v1/models` with the same auth. If that list disagrees with the tables below, trust the live response.

---

## Agent workflow: pick endpoint and handle async video

```text
Need text / tools / vision chat?     → POST /v1/chat/completions
Need a still image?                  → POST /v1/images/generations
Need Grok image edit (file in)?      → POST /v1/images/edits (multipart)
Need video?
  ├─ Grok 1.0 (xAI)                  → POST /v1/images/generations  model=grok-video
  ├─ Grok 1.5 (xAI, image required)  → POST /v1/images/generations  model=grok-video-1.5
  ├─ Seedance (BytePlus ARK)         → POST /v1/images/generations  model=seedance-2.0*
  └─ Vertex Veo                      → POST /videos + poll GET /v1/videos/{id}
Need speech?                         → POST /v1/audio/speech
```

**Video models differ:**

| Model | Route | Blocking behavior | Client must poll? |
|-------|-------|-------------------|-------------------|
| `grok-video` | `/v1/images/generations` | Proxy polls xAI up to **~600s** inside one request | No — wait for `data[0].url` MP4 |
| `grok-video-1.5`, `grok-imagine-video-1.5-2026-05-30` | `/v1/images/generations` | Same blocking poll as `grok-video`; **requires** `image` (image-to-video) | No |
| `seedance-2.0`, `seedance-2.0-fast` | `/v1/images/generations` | Proxy waits up to **240s** (`SEEDANCE_SYNC_WAIT_S`), then may return a task handle | **Yes**, if `data[0].url` starts with `seedance-task://` |
| `veo-3.1*` | `/videos` + GET | Async job API | Yes — poll video id |

**Rule for Seedance:** After submit, if `data[0].url` is `seedance-task://<id>`, poll with another POST (see [Seedance 2.0](#byteplus-seedance-20-custom-handler)). Do not download a `seedance-task://` URL.

**HTTP client timeouts:** Use **≥ 660s** for `grok-video` / `grok-video-1.5` (poll ceiling ~600s). Use **≥ 300s** for blocking Seedance (`async_submit: false`). For Seedance polling calls, **60s** per request is enough (`wait_seconds: 0`).

---

## Full model catalog

Logical alias → endpoint → upstream (for debugging). Credentials are on the proxy only.

### Chat — `POST /v1/chat/completions`

| Alias | Upstream | Proxy env |
|-------|----------|-----------|
| `gpt-latest` | `openai/gpt-5.5` | `OPENAI_API_KEY` |
| `gpt-5.5` | `openai/gpt-5.5` | `OPENAI_API_KEY` |
| `gpt-5.5-thinking` | `openai/gpt-5.5` + `reasoning_effort: high` | `OPENAI_API_KEY` |
| `gpt-5.4` | `openai/gpt-5.4` | `OPENAI_API_KEY` |
| `gpt-5.4-mini` | `openai/gpt-5.4-mini` | `OPENAI_API_KEY` |
| `gpt-5.4-nano` | `openai/gpt-5.4-nano` | `OPENAI_API_KEY` |
| `gemini-latest` | `vertex_ai/gemini-3.1-pro-preview` | `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, GCP creds |
| `gemini-3.1-pro` | `vertex_ai/gemini-3.1-pro-preview` | same |
| `gemini-3.1-pro-customtools` | `vertex_ai/gemini-3.1-pro-preview-customtools` | same |
| `gemini-3.5-flash` | `vertex_ai/gemini-3.5-flash` | same |
| `gemini-3.7-flash` | `vertex_ai/gemini-3.7-flash` (global) | same |
| `gemini-3.5-flash-lite` | `vertex_ai/gemini-3.5-flash-lite` (global) | same |
| `gemini-3-flash-preview` | `vertex_ai/gemini-3-flash-preview` | same |
| `gemini-3.1-flash-lite-preview` | `vertex_ai/gemini-3.1-flash-lite-preview` | same |
| `grok-latest` | `xai/grok-4.20-non-reasoning-latest` | `GROK_API_KEY` |
| `grok-4.20-reasoning` | `xai/grok-4.20-reasoning-latest` | `GROK_API_KEY` |
| `grok-4.20` | `xai/grok-4.20-non-reasoning-latest` | `GROK_API_KEY` |

**Example**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4-mini","messages":[{"role":"user","content":"Hello"}]}'
```

OpenAI SDKs: `base_url=LITELLM_BASE_URL`, `api_key=LITELLM_MASTER_KEY`, standard `chat.completions.create`.

---

### Images & proxy-hosted video — `POST /v1/images/generations`

| Alias | Type | Upstream / handler | Proxy env |
|-------|------|-------------------|-----------|
| `gpt-image-1.5` | Image | OpenAI | `OPENAI_API_KEY` |
| `gpt-image-2` | Image | OpenAI | `OPENAI_API_KEY` |
| `nano-banana`, `nano-banana-2` | Image | `vertex_ai/gemini-3.1-flash-image-preview` | GCP + Vertex |
| `nano-banana-pro` | Image | `vertex_ai/gemini-3-pro-image-preview` | GCP + Vertex |
| `imagen-4.0` | Image | `vertex_ai/imagen-4.0-generate-001` | GCP + Vertex |
| `imagen-4.0-fast` | Image | `vertex_ai/imagen-4.0-fast-generate-001` | GCP + Vertex |
| `imagen-4.0-ultra` | Image | `vertex_ai/imagen-4.0-ultra-generate-001` | GCP + Vertex |
| `grok-image` | Image | custom → `grok-imagine-image-quality` | `GROK_API_KEY` |
| `grok-imagine-image-quality` | Image | same as `grok-image` | `GROK_API_KEY` |
| `grok-imagine-image-2.0` | Image | custom → exact xAI Image 2.0 model | `GROK_API_KEY` |
| `grok-video` | **Video (MP4)** | custom → `grok-imagine-video`; text-to-video or image-to-video | `GROK_API_KEY` (proxy) |
| `grok-video-1.5` | **Video (MP4)** | custom → `grok-imagine-video-1.5-preview`; **image-to-video only** | `GROK_API_KEY` (proxy + client file upload) |
| `grok-imagine-video-1.5-2026-05-30` | **Video (MP4)** | same handler/pricing as `grok-video-1.5` (dated xAI model id) | same |

See [xAI Grok video](#xai-grok-video) for modes, file upload, and pricing.
| `seedance-2.0` | **Video (MP4)** | custom → ARK `dreamina-seedance-2-0-260128` | `BYTEDANCE_API_KEY` |
| `seedance-2.0-fast` | **Video (MP4)** | custom → ARK `dreamina-seedance-2-0-fast-260128` | `BYTEDANCE_API_KEY` |
| `seedance-2.5` | **Video (MP4, durable only; disabled in Magic Lens)** | LAS → `dreamina-seedance-2-5-260628` | `SEEDANCE_2_5_API_KEY` |
| `seedream-5.0` | Image | custom → ModelArk `seedream-5-0-260128` | `BYTEDANCE_API_KEY` |
| `seedream-5.0-lite` | Image | custom → ModelArk `seedream-5-0-lite-260128` | `BYTEDANCE_API_KEY` |
| `seedream-5.0-pro` | Image | custom → ModelArk `dola-seedream-5-0-pro-260628` | `BYTEDANCE_API_KEY` |

Standard image body: `prompt`, optional `n`, `size`, `quality`, etc. (provider-dependent).

**Imagen example**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"imagen-4.0","prompt":"A red bicycle on white","n":1,"size":"1024x1024"}'
```

---

### Grok image edits — `POST /v1/images/edits`

Multipart form: `model` = `grok-image` or `grok-imagine-image-quality`, `prompt`, `image` file part. Text-only Grok images use `/v1/images/generations` (JSON).

---

### Vertex Veo — async video API

| Alias | Vertex model id |
|-------|-----------------|
| `veo-3.1` | `veo-3.1-generate-001` |
| `veo-3.1-fast` | `veo-3.1-fast-generate-001` |
| `veo-3.1-lite` | `veo-3.1-lite-generate-001` |

1. `POST ${LITELLM_BASE_URL}/videos` — body: `model`, `prompt`, optional `seconds`, `size`, …
2. `GET ${LITELLM_BASE_URL}/v1/videos/{video_id}` — poll until completed
3. `GET ${LITELLM_BASE_URL}/v1/videos/{video_id}/content` — download bytes

See [LiteLLM Vertex Veo docs](https://docs.litellm.ai/docs/providers/vertex_ai/videos).

---

### Speech — `POST /v1/audio/speech`

| Alias | ElevenLabs model |
|-------|------------------|
| `elevenlabs-v3-tts` | `eleven_v3` |
| `tts-quality` | `eleven_multilingual_v2` |
| `tts-fast` | `eleven_flash_v2_5` |
| `tts-turbo` | `eleven_turbo_v2_5` |

Body: `model`, `input` (text), `voice` (OpenAI-style name like `alloy` or raw ElevenLabs voice id), optional `response_format` (`mp3`, `pcm`, `opus`). Proxy: `ELEVENLABS_API_KEY`.

```bash
curl -sS "${LITELLM_BASE_URL}/v1/audio/speech" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-quality","input":"Hello.","voice":"alloy","response_format":"mp3"}' \
  -o speech.mp3
```

Provider extras (e.g. `voice_settings`) often go in `extra_body` with OpenAI SDKs.

---

## Custom handlers (Grok & Seedance)

These use the same OpenAI **Images** path but implement vendor-specific async video (and Grok image) logic in `custom_handler_*.py`.

### xAI Grok video

- **Endpoint:** `POST /v1/images/generations`
- **Success:** `data[0].url` = HTTPS link to **MP4**
- **Behavior:** Submits to xAI (`/v1/videos/generations` or `/v1/videos/edits`), then **polls inside the proxy** (up to ~600s, 3s interval). One client request can block for the full generation.
- **Handler:** `custom_handler.grok_video` (same code path for all aliases below)
- **Override upstream:** `xai_model` or `upstream_model` in the JSON body, or env `GROK_VIDEO_MODEL` on the proxy

**Proxy aliases**

| Client `model` | xAI upstream | Input |
|----------------|--------------|--------|
| `grok-video` | `grok-imagine-video` | Text-to-video and/or `image` |
| `grok-video-1.5` | `grok-imagine-video-1.5-preview` | **Image-to-video only** — `image` required |
| `grok-imagine-video-1.5-2026-05-30` | `grok-imagine-video-1.5-2026-05-30` | Same as `grok-video-1.5` (dated xAI alias) |

**1.0 vs 1.5**

| | `grok-video` (1.0) | `grok-video-1.5` (1.5) |
|--|---------------------|-------------------------|
| Text-only generation | Supported | **Not supported** — xAI returns an error if `image` is omitted |
| Starting frame | Optional `image` | **Required** `image` (or `image_url` / `image_file_id`) |
| Storyboard | `reference_images` (optional) | `reference_images` (optional); use with `image` for base frame + storyboard |
| Video edit | `video` / `video_url` → `/v1/videos/edits` | Same |
| Typical resolutions | `480p`, `720p` | `480p`, `720p` (per xAI pricing tiers) |
| xAI list pricing (fallback) | $0.05/s @ 480p, $0.07/s @ 720p | $0.08/s @ 480p, $0.14/s @ 720p; input image $0.01 |

**Request fields (all Grok video aliases)**

| Field | Aliases | Notes |
|-------|---------|-------|
| `prompt` | — | Required for video edit; required with `reference_images`; for 1.5 required whenever you send images |
| `image`, `image_url`, `image_file_id` | — | Starting frame; **required for `grok-video-1.5`** |
| `reference_images`, `reference_image_urls` | — | Storyboard / style refs; max **7**; `prompt` required |
| `video`, `video_url`, `video_file_id` | — | Video edit path only |
| `duration`, `seconds` | — | Integer **1–15**; with `reference_images`, **≤ 10** |
| `resolution`, `aspect_ratio`, `output`, `storage_options`, `user` | — | Passed through to xAI |

**Local images → xAI `file_id`**

The proxy accepts `image: {"file_id": "…"}` and `reference_images: [{"file_id": "…"}]`. Upload bytes with your **`GROK_API_KEY`** (not the LiteLLM master key):

```bash
curl -sS "https://api.x.ai/v1/files" \
  -H "Authorization: Bearer ${GROK_API_KEY}" \
  -F "file=@path/to/frame.jpg;type=image/jpeg"
```

Use the returned `id` in the generation JSON. MIME types: `image/jpeg`, `image/png`, `image/webp`, etc.

#### `grok-video-1.5` example (base frame + storyboard)

From repo root; `cp .env_example .env`; HTTP `--max-time 660` or higher. Manual smoke assets belong under ignored `local-tests/`.

```bash
set -a; source .env; set +a
: "${GROK_API_KEY:?set GROK_API_KEY in .env}"

BASE_FILE_ID=$(curl -sS "https://api.x.ai/v1/files" \
  -H "Authorization: Bearer ${GROK_API_KEY}" \
  -F "file=@local-tests/toy_base_image.jpeg;type=image/jpeg" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

STORYBOARD_FILE_ID=$(curl -sS "https://api.x.ai/v1/files" \
  -H "Authorization: Bearer ${GROK_API_KEY}" \
  -F "file=@local-tests/toy_screenplay.webp;type=image/webp" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

curl -sS --max-time 660 "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(BASE_FILE_ID="${BASE_FILE_ID}" STORYBOARD_FILE_ID="${STORYBOARD_FILE_ID}" python3 -c 'import json, os; print(json.dumps({
    "model": "grok-video-1.5",
    "prompt": "local-tests/toy_base_image.jpeg is the starting frame: match its composition, lighting, and toy layout at t=0. local-tests/toy_screenplay.webp is the storyboard: follow its panels for motion, acting beats, and camera moves over 8 seconds. Animate from the base photo through the storyboard with warm tungsten stage light and playful stop-motion energy",
    "duration": 8,
    "resolution": "720p",
    "image": {"file_id": os.environ["BASE_FILE_ID"]},
    "reference_images": [{"file_id": os.environ["STORYBOARD_FILE_ID"]}],
  }))')"
```

#### `grok-video` (1.0) text-to-video example

```bash
set -a; source .env; set +a
curl -sS --max-time 660 "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-video","prompt":"A red ball bouncing once on white","duration":5}'
```

#### Pricing

The handler bills using xAI’s **`usage.cost_in_usd_ticks`** from `GET /v1/videos/{request_id}` when the job completes (1 USD = 10,000,000,000 ticks). That value is exposed as `x-litellm-response-cost`. If ticks are missing, cost is estimated from **`video.duration`** × per-second rate by **`resolution`** (rates depend on upstream model).

**`grok-imagine-video`** (`grok-video`):

| Resolution | USD / generated second (output) |
|------------|----------------------------------|
| `480p` (default) | $0.05 |
| `720p` | $0.07 |
| `1080p` | $0.07 (720p fallback) |

| Input | USD |
|-------|-----|
| Reference / input image (each) | $0.002 |
| Input video (edit path) | $0.01 / second |

Env: `GROK_VIDEO_PRICE_PER_SECOND_480P`, `GROK_VIDEO_PRICE_PER_SECOND_720P`, `GROK_VIDEO_PRICE_PER_SECOND_1080P`, `GROK_VIDEO_PRICE_PER_REFERENCE_IMAGE`.

**`grok-imagine-video-1.5-preview`** (`grok-video-1.5`, alias `grok-imagine-video-1.5-2026-05-30`):

| Resolution | USD / generated second (output) |
|------------|----------------------------------|
| `480p` (default) | $0.08 |
| `720p` | $0.14 |
| `1080p` | $0.14 (720p fallback) |

| Input | USD |
|-------|-----|
| Image (reference / input) | $0.01 |
| Input video (edit path) | $0.01 / second |

Env: `GROK_VIDEO_15_PRICE_PER_SECOND_480P`, `GROK_VIDEO_15_PRICE_PER_SECOND_720P`, `GROK_VIDEO_15_PRICE_PER_SECOND_1080P`, `GROK_VIDEO_15_PRICE_PER_REFERENCE_IMAGE`.

**Client timeout:** Prefer **≥ 660s** HTTP timeout (generation + poll ceiling ~600s).

---

### xAI Grok image (`grok-image`, `grok-imagine-image-quality`, `grok-imagine-image-2.0`)

- **Generate:** `POST /v1/images/generations` with `prompt`
- **Edit:** `POST /v1/images/edits` multipart with `image` + `prompt`
- **Proxy env:** `GROK_API_KEY`
- **Image 2.0:** 1K/2K, `quality=low|medium`, up to five input images.
- **Image 2.0 pricing:** $0.01/input image; $0.04 (1K low), $0.06 (2K low or 1K medium), $0.08 (2K medium) per output image.

---

### BytePlus Seedance 2.0 (custom handler)

#### Implementation checklist (read before using Seedance)

**You are not done until all of these are true.**

| # | Action | Why |
|---|--------|-----|
| 1 | **Redeploy** the LiteLLM proxy image (`./deploy_cloud_run.sh` or your CI) so Cloud Run runs `custom_handler_seedance.py` with handler version **`2026-05-24-poll-default-0`** (grep the file in the built image if unsure). | Fixes live in the handler file, not config alone. |
| 2 | Confirm Cloud Run env: `SEEDANCE_SYNC_WAIT_S=240`, `SEEDANCE_POLL_TIMEOUT_S=1200`, request timeout **1800s**. | Submit waits up to 4m; poll cap for blocking opt-in. |
| 3 | **Run the smoke test** below (submit + one poll). Poll must return in **&lt; 5 seconds** while status is `running`. | Proves poll-default fix is deployed. |
| 4 | **Calling code / agents:** implement the two-step flow (submit → poll). **Never** re-POST the original prompt on timeout — that starts a **new paid ARK job**. | Main cause of surprise credit burn. |
| 5 | On **poll**, always send `seedance_task_id` (or `prompt: "seedance-task://<id>"`). Do **not** send `duration`, `resolution`, or reference images again. | Avoid duplicate submits; cheaper polls. |
| 6 | Set `resolution` explicitly if you need 720p/1080p (`480p` is the handler default). | Omitted resolution previously fell through to slower ARK tiers. |
| 7 | Optional: use **`"async_submit": false`** on submit only if you want one HTTP call blocking up to 20m (old style). | No client poll loop required. |

**Smoke test (after deploy)**

```bash
# 1) Submit — may return MP4 or seedance-task://...
RESP=$(curl -sS -m 300 "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0-fast","prompt":"A red ball on white","duration":4,"resolution":"480p"}')
echo "$RESP"
URL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['url'])")

# 2) If pending, poll once — must finish in a few seconds, not minutes
if [[ "$URL" == seedance-task://* ]]; then
  TID="${URL#seedance-task://}"
  time curl -sS -m 60 "${LITELLM_BASE_URL}/v1/images/generations" \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"seedance-2.0-fast\",\"prompt\":\"seedance-task://${TID}\",\"seedance_task_id\":\"${TID}\"}"
fi
```

If step 2’s `time curl` shows **~240s**, the old handler is still running — redeploy.

---

| Alias | ARK model id | Notes |
|-------|--------------|-------|
| `seedance-2.0` | `dreamina-seedance-2-0-260128` | 480p / 720p / **1080p**; best quality |
| `seedance-2.0-fast` | `dreamina-seedance-2-0-fast-260128` | Same features, max **720p**, lower cost |

**Endpoint:** `POST /v1/images/generations`

**Response shapes**

```jsonc
// Done — download this URL
{ "data": [{ "url": "https://....mp4" }] }

// Still running — poll (do not treat url as downloadable)
{ "data": [{ "url": "seedance-task://cgt-...", "revised_prompt": "running" }] }
```

Status strings in `revised_prompt` include `submitted`, `running`, etc.

#### Submit (new job)

Required: `prompt` (non-empty), `model`, usually `duration` (4–15 seconds).

| Field | Description |
|-------|-------------|
| `duration` | Integer seconds, **4–15** |
| `resolution` | `480p`, `720p`, `1080p` (1080p: Pro only) |
| `ratio` | `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, or `adaptive` (with refs) |
| `generate_audio` | Boolean |
| `watermark` | Boolean |
| `wait_seconds` | How long this HTTP call blocks polling ARK (default **240**). `0` = return task URL immediately |
| `async_submit` | `true` → `wait_seconds=0`; `false` → block up to `SEEDANCE_POLL_TIMEOUT_S` (1200s) |

**Poll calls** (`seedance_task_id` set): default `wait_seconds` is **0** (one ARK GET, ~1s). Do not omit `seedance_task_id` on poll — only the submit body should create a new task. Optional `wait_seconds` &gt; 0 makes the proxy block and poll ARK until that cap.

**Image inputs**

- `image` / `image_url` — one URL (`https://` or `data:image/...`)
- `images` — list of image URLs
- `reference_image_urls` — list (max **7**) for reference-only / multimodal; **do not** combine with `image` / `video_url`

Roles sent to ARK: 1 image → `first_frame`; 2 → `first_frame` + `last_frame`; 3+ or reference batch → `reference_image`.

**Video inputs (edit / extend)**

- `video_url` or `videos` — up to **3** HTTPS URLs, role `reference_video`

#### Poll (existing job)

LiteLLM’s router requires a **`prompt`** field on every images request. Use any non-empty string; the task URL works:

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2.0",
    "prompt": "seedance-task://cgt-xyz",
    "seedance_task_id": "cgt-xyz",
    "wait_seconds": 0
  }'
```

Also accepts `poll_task_id` or `task_id` instead of `seedance_task_id`.

#### Recovery (you already paid — don’t lose the video)

ARK assigns a **task id** (`cgt-…`) at submit time. That id can be polled until the job is **succeeded**, **failed**, or **expired**. Completed outputs are kept for roughly **48 hours** (`execution_expires_after` on the ARK task).

**Polling an existing id does not start a new generation** (no new completion charge for the generation itself).

| Handle | Where it appears |
|--------|------------------|
| `seedance-task://cgt-…` | `data[0].url` while running |
| `cgt-…` | `data[0].revised_prompt` on **success** (added for recovery) |
| JSONL row | `SEEDANCE_TASK_LEDGER_PATH` on the proxy (written immediately after ARK accepts the task) |

**Fool-proof setup (recommended)**

1. In `.env` for local Docker:
   ```bash
   SEEDANCE_TASK_LEDGER_PATH=/app/seedance_task_ledger.jsonl
   ```
   (`run_gateway.sh` creates the host file; compose bind-mounts it.)

2. On every submit, your app **persists the task id** before waiting for the MP4:
   - From `data[0].url` if it starts with `seedance-task://`
   - From `data[0].revised_prompt` when status is `succeeded`
   - Or grep the ledger: `tail seedance_task_ledger.jsonl`

3. If LiteLLM or your app times out, **recover** (do not re-submit the prompt):
   ```bash
   python recover_seedance_task.py cgt-XXXXXXXX   # polls ARK directly
   python recover_seedance_task.py cgt-XXXXXXXX --via proxy --model seedance-2.0-fast
   python recover_seedance_task.py --list-recent
   python recover_seedance_task.py --recover-pending
   ```

4. Optional faster handoff: `"async_submit": true` on submit returns `seedance-task://…` in **&lt;1s** so you can save the id before any long wait.

**Never** re-POST the original prompt after a timeout unless you intend to pay for a **new** job.

#### Agent polling loop (pseudocode)

```python
TASK_PREFIX = "seedance-task://"

def is_pending(url: str) -> bool:
    return url and url.startswith(TASK_PREFIX)

def task_id_from_url(url: str) -> str:
    return url[len(TASK_PREFIX):]

# 1) submit
resp = post_images_generations(model="seedance-2.0", prompt="...", duration=8, ratio="16:9")
url = resp["data"][0]["url"]
if not is_pending(url):
    return url  # MP4 ready

tid = task_id_from_url(url)
# 2) poll every 10s
while True:
    resp = post_images_generations(
        model="seedance-2.0",
        prompt=f"{TASK_PREFIX}{tid}",
        seedance_task_id=tid,
        wait_seconds=0,
    )
    url = resp["data"][0]["url"]
    if not is_pending(url):
        return url
    sleep(10)
```

#### Why bounded wait?

Many clients and load balancers cut idle connections at **~300s**. The handler defaults to **240s** synchronous wait (`SEEDANCE_SYNC_WAIT_S`), then returns a task handle so polling uses short requests.

#### Pricing

Cost is computed from ARK `usage.completion_tokens` × rate (in `x-litellm-response-cost`). Re-polling a **completed** task does not double-charge.

| Alias | USD / 1M output tokens (no input video) | With input video |
|-------|----------------------------------------|------------------|
| `seedance-2.0` | $7.00 | $4.30 |
| `seedance-2.0-fast` | $5.60 | $3.30 |

Override on proxy: `SEEDANCE_PRICE_PER_MTOK`, `SEEDANCE_PRICE_PER_MTOK_VIDEO`, `SEEDANCE_PRICE_PER_MTOK_FAST`, `SEEDANCE_PRICE_PER_MTOK_FAST_VIDEO`.

---

### BytePlus Seedream 5 (ModelArk custom handler)

| Gateway alias | ModelArk model id | Notes |
|---------------|-------------------|-------|
| `seedream-5.0` | `seedream-5-0-260128` | Seedream 5.0 (2K / 3K) |
| `seedream-5.0-lite` | `seedream-5-0-lite-260128` | Seedream 5.0 Lite (2K / 3K, web search) |
| `seedream-5.0-pro` | `dola-seedream-5-0-pro-260628` | Seedream 5.0 Pro (1K / 2K, one output, up to 10 references) |

**Endpoint:** `POST /v1/images/generations` — **synchronous** (one request returns image URL(s); no task polling).

**ModelArk upstream:** `POST {ARK_BASE}/images/generations` (default `https://ark.ap-southeast.bytepluses.com/api/v3`).

**Proxy env:** `BYTEDANCE_API_KEY` (same ModelArk key as Seedance).

| Field | Description |
|-------|-------------|
| `prompt` | Required text prompt |
| `size` | `2K`, `3K`, or explicit `WxH` (see [ModelArk image API](https://docs.byteplus.com/en/docs/ModelArk/1541523)) |
| `image` / `images` / `image_urls` | Reference image(s) for edit / fusion (up to **14** URLs or base64) |
| `reference_image_urls` | Alias for multi-image input |
| `n` | Number of images (default 1) |
| `response_format` | `url` (default) or `b64_json` |
| `output_format` | `png` or `jpeg` (5.0 supports both) |
| `watermark` | Boolean (default upstream `true`; set `false` for clean output) |
| `sequential_image_generation` | `disabled` (default) or `auto` for related multi-image sets |
| `sequential_image_generation_options` | e.g. `{ "max_images": 4 }` when sequential mode is `auto` |
| `tools` | `[{ "type": "web_search" }]` — **5.0 Lite** real-time search (adds latency + surcharge) |
| `optimize_prompt_options` | e.g. `{ "mode": "standard" }` |
| `stream` | Stream partial results when supported |

Seedream Pro accepts only one output, PNG/JPEG, no streaming, and up to ten references. The Gateway request policy copies `size`, `n`, and format fields into model-scoped private fields so LiteLLM 1.95 cannot consume them before custom-provider dispatch; the handler restores the public ModelArk fields.

**Example (text-to-image)**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedream-5.0-lite",
    "prompt": "A red bicycle on white, studio product photo",
    "size": "2K",
    "watermark": false
  }'
```

**Example (web search — Lite)**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedream-5.0-lite",
    "prompt": "Shanghai 5-day weather forecast infographic, flat illustration",
    "size": "2K",
    "tools": [{"type": "web_search"}],
    "watermark": false
  }'
```

#### Pricing (BytePlus ModelArk, Seedream 5.0 family)

Handler sets `x-litellm-response-cost` from output image count × per-image rate (+ web search when `tools` includes `web_search`). See [ModelArk pricing](https://docs.byteplus.com/en/docs/ModelArk/1544106).

| Gateway alias | USD / generated image (2K & 3K) | Web search (per request) |
|---------------|----------------------------------|---------------------------|
| `seedream-5.0` | $0.035 | + $0.0006 when `tools: [{ "type": "web_search" }]` |
| `seedream-5.0-lite` | $0.035 | + $0.0006 when `tools: [{ "type": "web_search" }]` |
| `seedream-5.0-pro` | $0.045 (1K) / $0.09 (2K), plus $0.003 per input after the first | Not supported |

Override on proxy: `SEEDREAM_5_0_PRICE_PER_IMAGE`, `SEEDREAM_5_0_LITE_PRICE_PER_IMAGE`, `SEEDREAM_5_0_PRO_1K_PRICE_PER_IMAGE`, `SEEDREAM_5_0_PRO_2K_PRICE_PER_IMAGE`, `SEEDREAM_5_0_PRO_ADDITIONAL_INPUT_PRICE`, `SEEDREAM_WEB_SEARCH_PRICE_PER_REQUEST`, `SEEDREAM_ARK_BASE`.

**Client timeout:** **≥ 120s** for complex prompts / web search; default handler HTTP timeout is **300s**.

---

## Quick reference: alias → HTTP surface

| HTTP surface | Aliases |
|--------------|---------|
| `POST /v1/chat/completions` | `gpt-latest`, `gpt-5.5`, `gpt-5.5-thinking`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gemini-latest`, `gemini-3.1-pro`, `gemini-3.1-pro-customtools`, `gemini-3.5-flash`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`, `grok-latest`, `grok-4.20-reasoning`, `grok-4.20` |
| `POST /v1/images/generations` | `gpt-image-1.5`, `gpt-image-2`, `nano-banana`, `nano-banana-2`, `nano-banana-pro`, `imagen-4.0`, `imagen-4.0-fast`, `imagen-4.0-ultra`, `grok-image`, `grok-imagine-image-quality`, `grok-video`, `grok-video-1.5`, `grok-imagine-video-1.5-2026-05-30`, `seedance-2.0`, `seedance-2.0-fast`, `seedream-5.0`, `seedream-5.0-lite` |
| `POST /v1/images/edits` | `grok-image`, `grok-imagine-image-quality` |
| `POST /videos` + `GET /v1/videos/{id}` + `GET …/content` | `veo-3.1`, `veo-3.1-fast`, `veo-3.1-lite` |
| `POST /v1/audio/speech` | `elevenlabs-v3-tts`, `tts-quality`, `tts-fast`, `tts-turbo` |

---

## Proxy configuration notes (operators)

| Setting | Value | Effect |
|---------|-------|--------|
| `litellm_settings.request_timeout` | `1800` | Upper bound per proxy request |
| `seedance-*` `timeout` in model_list | `1800` | Per-model router timeout |
| `grok-video` / `grok-video-1.5` `timeout` | `1260` | Per-model router timeout |
| `SEEDANCE_SYNC_WAIT_S` (env) | default `240` | Max blocking wait before task URL |
| `SEEDANCE_POLL_TIMEOUT_S` (env) | default `1200` | Max wait when `async_submit: false` |

Custom handler modules: `custom_handler.grok_video`, `custom_handler.grok_image`, `custom_handler.seedance`, `custom_handler.seedream`.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Empty `data` on Seedance | Old handler bug (fixed) | Redeploy proxy; expect `seedance-task://` or MP4 URL |
| 504 / disconnect ~5 min on video | Long blocking request | Seedance: use default bounded wait + poll; Grok: increase client timeout to ≥660s |
| `missing prompt` on Seedance poll | LiteLLM router | Include `prompt` (e.g. task URL string) on poll requests |
| 404 unknown model | Alias typo or stale deploy | `GET /v1/models` |
| Grok 1.5 “text to image/video not supported” | `grok-video-1.5` without `image` | Use `grok-video-1.5` with `image` (+ optional `reference_images`); use `grok-video` for text-only |
| Grok missing `file_id` | Upload step skipped or wrong key | Upload with `GROK_API_KEY` to `https://api.x.ai/v1/files`, then pass `image` / `reference_images` |
| Seedance 401/403 | ARK key or prepaid pack | Check `BYTEDANCE_API_KEY` and BytePlus model activation |

---

## Source of truth

- **Model list:** `litellm_config.yaml` in this repo
- **Handler behavior:** `custom_handler_seedance.py`, `custom_handler_seedream.py`, `custom_handler_xai.py`
- **This file:** client/skill contract for agents calling `LITELLM_BASE_URL`

When adding models, update `litellm_config.yaml` and this document together.
