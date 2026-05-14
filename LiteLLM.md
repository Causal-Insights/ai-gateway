---
name: litellm
description: >-
  Call models on the shared LiteLLM proxy: pick the HTTP endpoint, logical model
  alias, auth header, and optional request fields. Self-contained; uses
  LITELLM_BASE_URL for the proxy host.
---

# LiteLLM proxy — client guide

Clients call **only the LiteLLM proxy** over OpenAI-compatible HTTP routes. Provider API keys and upstream base URLs are configured on the proxy server, not in your application.

## Base URL and authentication

- **Base URL**: read from the environment variable **`LITELLM_BASE_URL`**. Use it without a trailing slash when appending paths (for example `"${LITELLM_BASE_URL}/v1/chat/completions"`).
- **Auth**: `Authorization: Bearer <token>`. The token is normally the proxy master key (or a LiteLLM virtual key if your operator issued one).
- **`model` field**: send the **logical alias** from the catalog below (for example `gpt-5.4-mini`, `grok-video`, `veo-3.1`). Do not send raw provider-internal model strings unless your operator confirms the proxy accepts them.

## Custom integrations (Grok video, Grok image, Seedance)

These aliases are routed through **custom LiteLLM handlers** on the proxy (not plain passthrough to a single vendor URL). The proxy still uses the same HTTP paths documented below; behavior and extra JSON fields are defined here.

| Aliases | Role | Credentials on the proxy (not sent by clients) |
|---------|------|--------------------------------------------------|
| `grok-video` | xAI Grok **video** (submit + poll server-side; response looks like images API) | `GROK_API_KEY` |
| `grok-image`, `grok-imagine-image-quality` | xAI Grok **image** generation and edits | `GROK_API_KEY` |
| `seedance-2.0`, `seedance-2.0-fast`, `dreamina-seedance-2-0-fast-260128` | BytePlus ARK **Seedance 2.0** video (async task + poll server-side) | `BYTEDANCE_API_KEY` |

---

## 1. `POST /v1/chat/completions`

Use for **text and multimodal chat**: JSON body with a `messages` array; optional tools; images inside message content when the upstream model supports vision.

| Group | Model aliases (`model` in JSON) | Upstream routing (for troubleshooting) |
|-------|-----------------------------------|------------------------------------------|
| Local MLX | `gemma-4-large` | OpenAI-compatible server at `MLX_VLM_API_BASE` with `MLX_VLM_API_KEY` on the proxy |
| OpenAI | `gpt-latest`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano` | OpenAI Chat Completions; `OPENAI_API_KEY` on the proxy |
| Vertex Gemini (text) | `gemini-latest`, `gemini-3.1-pro`, `gemini-3.1-pro-customtools`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview` | Vertex AI Gemini; `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and GCP credentials on the proxy |
| xAI Grok (text) | `grok-latest`, `grok-4.20-reasoning`, `grok-4.20` | xAI API; `XAI_API_KEY` on the proxy |

Example:

```bash
curl -sS "${LITELLM_BASE_URL}/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4-mini","messages":[{"role":"user","content":"Hello"}]}'
```

OpenAI-compatible SDKs: set `base_url` to `LITELLM_BASE_URL` and `api_key` to your proxy token; use `chat.completions.create` with the aliases above.

---

## 2. `POST /v1/images/generations`

Use for **image generation** and for **Grok video** and **Seedance video**, which return a **finished asset URL** in the same response shape as image generation (`data[0].url`).

### OpenAI image models

| Model aliases | Notes |
|---------------|--------|
| `gpt-image-1.5`, `gpt-image-2` | Standard OpenAI Images parameters (`prompt`, `n`, `size`, etc.). `OPENAI_API_KEY` on the proxy. |

### Vertex / Gemini image (“Nano Banana”)

| Model aliases | Vertex model id (on the proxy) |
|---------------|--------------------------------|
| `nano-banana`, `nano-banana-2` | `gemini-3.1-flash-image-preview` |
| `nano-banana-pro` | `gemini-3-pro-image-preview` |

Requires `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and Vertex-capable GCP credentials on the proxy.

### Vertex Imagen 4

| Model aliases | Vertex model id |
|---------------|-----------------|
| `imagen-4.0` | `imagen-4.0-generate-001` |
| `imagen-4.0-fast` | `imagen-4.0-fast-generate-001` |
| `imagen-4.0-ultra` | `imagen-4.0-ultra-generate-001` |

### xAI Grok image (custom handler)

| Model aliases | Notes |
|---------------|--------|
| `grok-image`, `grok-imagine-image-quality` | Same behavior; send `prompt` for generation. Uses `GROK_API_KEY` on the proxy. |

### xAI Grok video (custom handler)

| Model aliases | Notes |
|---------------|--------|
| `grok-video` | Call **`/v1/images/generations`** with JSON. Successful response includes **`data[0].url`** pointing at an **MP4**. The proxy polls xAI until the job completes. |

**Common JSON fields for `grok-video`**

- `prompt` (string): scene description.
- `duration` or `seconds` (integer): clip length in seconds.
- `xai_model` or `upstream_model` (string, optional): override the upstream xAI model name (default is operator-configurable, often a `grok-imagine-video`-style id).
- `reference_images` (optional): non-empty list of objects or URL strings for image-conditioned video; each object may use `url` or `file_id` (not both). When references are used, `prompt` is required; duration with references is capped (commonly **≤ 10** seconds). Up to **7** reference images.
- **Video edit path** (optional): supply `image` / `image_url` and `video` / `video_url` (or `file_id` variants) for edit-style calls; reference images are not combined with that path.

**Example (text-to-video)**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-video","prompt":"A red ball bouncing once","duration":3}'
```

Use a long HTTP client timeout (many minutes) if your stack defaults short.

### BytePlus Seedance 2.0 (custom handler)

| Model aliases | Notes |
|---------------|--------|
| `seedance-2.0`, `seedance-2.0-fast`, `dreamina-seedance-2-0-fast-260128` | Same route: **`POST /v1/images/generations`**. Response **`data[0].url`** is the **MP4**. The proxy submits an ARK task and polls until completion. |

**Common JSON fields for Seedance**

- `prompt` (string).
- `duration` (integer): length in seconds.
- `resolution` (string, e.g. `480p`), `ratio` (string, e.g. `1:1`).
- `generate_audio` or `generateAudio` (boolean).
- `watermark` (boolean).
- **Image / reference inputs** (optional): `image` or `image_url` (single URL string), or `images` (list of `https://` or `data:image/...` URLs). For **reference-only** batches, use `reference_image_urls` as a non-empty list of the same URL forms; do **not** combine `reference_image_urls` with `image` / `image_url` / `video_url` style fields. Up to **7** reference images.

**Example (text-to-video)**

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0","prompt":"A person walking through a forest at night","resolution":"480p","ratio":"1:1","duration":4,"generate_audio":false,"watermark":false}'
```

Server-side polling can run up to roughly **20 minutes**; ensure your HTTP client and any edge timeouts exceed that if you generate long clips.

### Generic image example (Vertex Imagen)

```bash
curl -sS "${LITELLM_BASE_URL}/v1/images/generations" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"imagen-4.0","prompt":"A red bicycle on a white background","n":1,"size":"1024x1024"}'
```

### `POST /v1/images/edits` (Grok image)

For **image edits** (input image + prompt), use the **OpenAI Images Edits** pattern: **multipart/form-data** with an `image` file part, `model` set to `grok-image` or `grok-imagine-image-quality`, and a `prompt`. Generation without an input image stays on **`/v1/images/generations`** with JSON.

---

## 3. Vertex Veo video (OpenAI-style video API)

Use for **Vertex Veo** models. LiteLLM uses a **create job → poll status → download content** flow. Official parameter mapping is described in the [LiteLLM Vertex Veo documentation](https://docs.litellm.ai/docs/providers/vertex_ai/videos).

| Model aliases | Vertex model id |
|---------------|-----------------|
| `veo-3.1` | `veo-3.1-generate-001` |
| `veo-3.1-fast` | `veo-3.1-fast-generate-001` |
| `veo-3.1-lite` | `veo-3.1-lite-generate-001` |

**Typical flow**

1. **`POST ${LITELLM_BASE_URL}/videos`** — JSON body includes `"model": "<alias>"`, `"prompt"`, optional `"seconds"`, `"size"`, etc.
2. **`GET ${LITELLM_BASE_URL}/v1/videos/{video_id}`** — poll until status is completed (or failed).
3. **`GET ${LITELLM_BASE_URL}/v1/videos/{video_id}/content`** — download video bytes.

Use the same `Authorization: Bearer` scheme as other routes (unless your operator documents a different proxy key header). The proxy needs `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and Vertex-capable GCP credentials.

---

## 4. `POST /v1/audio/speech` (ElevenLabs TTS)

| Model aliases | ElevenLabs model id (on the proxy) |
|---------------|-------------------------------------|
| `tts-quality` | `eleven_multilingual_v2` |
| `tts-fast` | `eleven_flash_v2_5` |
| `tts-turbo` | `eleven_turbo_v2_5` |

Request body (OpenAI-compatible): **`model`**, **`input`** (text to speak), **`voice`** (OpenAI-style names such as `alloy` map to ElevenLabs voices in LiteLLM; you may also pass a raw ElevenLabs voice id), optional **`response_format`** (`mp3`, `pcm`, `opus`). The proxy holds **`ELEVENLABS_API_KEY`**.

```bash
curl -sS "${LITELLM_BASE_URL}/v1/audio/speech" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-quality","input":"Hello from the proxy.","voice":"alloy","response_format":"mp3"}' \
  --output speech.mp3
```

Provider-specific options (for example `voice_settings`) are often passed under **`extra_body`** when using OpenAI SDKs against the proxy; see [LiteLLM ElevenLabs documentation](https://docs.litellm.ai/docs/providers/elevenlabs).

---

## Quick reference: alias → endpoint

| HTTP surface | Aliases |
|--------------|---------|
| `POST /v1/chat/completions` | `gemma-4-large`, `gpt-latest`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gemini-latest`, `gemini-3.1-pro`, `gemini-3.1-pro-customtools`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`, `grok-latest`, `grok-4.20-reasoning`, `grok-4.20` |
| `POST /v1/images/generations` | `gpt-image-1.5`, `gpt-image-2`, `nano-banana`, `nano-banana-2`, `nano-banana-pro`, `imagen-4.0`, `imagen-4.0-fast`, `imagen-4.0-ultra`, `grok-image`, `grok-imagine-image-quality`, `grok-video`, `seedance-2.0`, `seedance-2.0-fast`, `dreamina-seedance-2-0-fast-260128` |
| `POST /v1/images/edits` (multipart) | `grok-image`, `grok-imagine-image-quality` (edits only) |
| Veo: `POST /videos`, then `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content` | `veo-3.1`, `veo-3.1-fast`, `veo-3.1-lite` |
| `POST /v1/audio/speech` | `tts-quality`, `tts-fast`, `tts-turbo` |

---

## Discovery and drift

- **`GET ${LITELLM_BASE_URL}/v1/models`** (with the same proxy auth) usually returns the models the proxy currently exposes. If that list disagrees with the tables above, treat the **live response** as authoritative for your environment.
- If an alias returns **404 / unknown model**, the name may have been retired or renamed on the proxy; ask the operator or refresh from `GET /v1/models`.

The **tables in this document** are the intended catalog for this deployment; copy this page into another repository when you need an offline contract.
