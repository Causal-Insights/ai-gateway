# Provider implementation reference

Implementation notes reviewed **2026-09-24**. Scope: every model configured in this
Gateway checkout, its serving provider, and where to verify implementation details.
The inventory reflects the local retirement changes, not a deployment record.
Consult this reference before capability reviews, model changes, pricing work, or
LiteLLM upgrades. It does not certify every native feature as exposed or tested.

## How to resolve a model correctly

Follow this chain: **public alias → configured deployment → exact provider model
ID → serving API and location → selected adapter → request/response contract →
pricing profile → consuming application**. Model branding and a provider-compatible
request shape do not identify the service that actually executes the request.

| Question | Source of truth |
| --- | --- |
| Which aliases and exact deployment IDs are configured? | [`litellm_config.yaml`](../litellm_config.yaml), including `custom_provider_map`, location and effort overrides |
| Which supplier and billing identity apply? | [`pricing/registry.json`](../pricing/registry.json), active `models` entries and their referenced profiles; supplier verification procedures linked below |
| Which code actually sends the request? | The provider sections below; for durable jobs, [`route_for`, `adapter_for_route`, and `adapter_for_job`](../generation_job_adapters.py). Persisted jobs retain their route identity. |
| Which fields survive Gateway validation and translation? | [`gateway_request_policy.py`](../gateway_request_policy.py), the selected handler/adapter, [`generation_job_models.py`](../generation_job_models.py), and the installed LiteLLM implementation |
| Which SDK version is relevant? | Digest-pinned [`Dockerfile`](../Dockerfile) and exact-version guard in [`gateway_accounting.py`](../gateway_accounting.py): **LiteLLM 1.102.1**. See the [upgrade evidence and remaining gaps](litellm-v1.102-upgrade.md). Latest online LiteLLM docs can describe a different version. |
| What does the provider support now? | Official docs for the exact serving product below, followed by account/location-specific verification where needed |
| Can Magic Lens author and execute it? | Both Studio and Runner plus shared registry/compiler contracts in the [integration review](magiclens-integration-2026-09-24.md#integration-points-and-contracts-to-preserve) |

Treat these as separate findings: provider documented, Gateway implemented,
credentials verified, and Magic Lens end-to-end verified. `/v1/models`, a pricing
profile, or a successful basic generation establishes only part of that chain.
`drop_params: true` makes inspection of actual translated payloads particularly
important. Do not infer first/last-frame support, reference roles, masks, streaming,
or output modalities from an alias or generic route name.

## Provider directory

These are the serving providers configured here; this is not a directory of every
provider supported by LiteLLM.

| Provider | Serving product | Model families here | Main implementation |
| --- | --- | --- | --- |
| [OpenAI](#provider-openai) | Direct OpenAI API | GPT text/reasoning; GPT Image | Native LiteLLM plus local OpenAI contracts |
| [Google](#provider-google) | Google Cloud Vertex AI; Omni uses Vertex Interactions | Gemini text, Nano Banana/Gemini images, Veo, Gemini Omni | Native Vertex translation plus durable `VertexAdapter` |
| [xAI](#provider-xai) | Direct xAI API | Grok text, Imagine Image, Imagine Video | Native text; custom image/video handlers and `XAIAdapter` |
| [BytePlus](#provider-byteplus) | ModelArk, Asia-Pacific endpoint | Seedance and Seedream | Custom handlers and `BytePlusAdapter` |
| [ElevenLabs](#provider-elevenlabs) | Direct ElevenAPI | Speech, sound effects, music | Native speech; custom Audio Studio handler |

### OpenAI

<a id="provider-openai"></a>

- **Endpoint/auth:** default `https://api.openai.com/v1`; `OPENAI_API_KEY` via configuration. No Azure deployment is configured. Resolve any runtime base-URL overrides before claiming a particular deployment was tested.
- **Implementation:** native `openai/` routing for chat, Responses and Images, plus [`openai_model_contracts.py`](../openai_model_contracts.py), [`openai_usage.py`](../openai_usage.py), and [`gateway_request_policy.py`](../gateway_request_policy.py). In the pinned SDK, start with `litellm/llms/openai/` and its chat, responses, image-generation and image-edit transformations.
- **Official information:** [model catalog](https://developers.openai.com/api/docs/models), [image generation and editing](https://developers.openai.com/api/docs/guides/image-generation), [image inputs](https://developers.openai.com/api/docs/guides/images-vision), [pricing](https://developers.openai.com/api/docs/pricing). Consult the individual model page matching the inventory's exact ID.
- **Gateway contracts/evidence:** [September OpenAI contract](openai-september-2026.md); [OpenAI pricing procedure](../skills/price-verification/references/openai.md). `gpt-latest` is explicitly mapped to Astra; GPT-5.6 effort aliases have fixed controls. The two GPT Image 2.5 aliases resolve to dated September 8 snapshots, not floating IDs.
- **Verification boundary:** [September 24 direct provider receipts](evidence/gpt-image-2.5-access-2026-09-24.json) establish basic Sunburst/Flare generation using the tested Gateway credential. They do not establish deployed Gateway/Magic Lens editing, streaming, or Responses image-tool success.
- **1.102.1 compatibility:** native image generation now preserves response format, so its local response override was removed. The two 2.5 models still need local edit-field preservation and generation parameter handling. Astra cache options, native Responses streaming and retry handling retain their existing hooks. Mocked SDK wire tests cover generation, multipart edits, Responses tools/streaming and Chat Completions.
- **Hosted tools/resources:** [`hosted_tools.py`](../hosted_tools.py) pins measured web/file-call prices alongside native parent usage; [`owned_openai_resources.py`](../owned_openai_resources.py) isolates Files, vector stores, containers, Responses retrieval, and finite Astra batches by delegated application/owner. Batch results settle per item without replay; missing costs stay unresolved. The pinned SDK does not price image-tool calls, so valid assets and the known parent subtotal are retained without claiming a complete total. See [implemented routes and accounting limits](hosted-tools-and-owned-resources.md).

### Google / Vertex AI

<a id="provider-google"></a>

- **Endpoint/auth:** `vertex_ai/`, Google Application Default Credentials with Cloud Platform scope; project `GOOGLE_CLOUD_PROJECT`. Location is either `GOOGLE_CLOUD_LOCATION` or explicit `global` as shown per alias. Regional Vertex endpoints use `{location}-aiplatform.googleapis.com`; global endpoints use `aiplatform.googleapis.com`. Cloud Run hosting region is not automatically the model's inference location.
- **API distinction:** these are Vertex-served models. Gemini Developer API / AI Studio examples using `gemini/` and an API key do not establish the Vertex request schema, availability, quota or price. Likewise, Nano Banana is a public alias for an exact Gemini image model, not a separate supplier.
- **Text/images:** native LiteLLM Vertex/Gemini translation. Inspect `litellm/llms/vertex_ai/` in the pinned image, especially Gemini request/response transformations. Magic Lens sends Gemini image references through chat; an OpenAI-compatible client interface does not mean the provider receives OpenAI Images requests.
- **Grounding accounting (2026-09-24):** exact supported Gemini text and image aliases have successor price profiles with the [published $0.014/query tariff](../pricing/evidence/google-grounding-2026-09-24.json). Google Search selection pins the tariff; ordinary requests retain token pricing. Pinned SDK 1.102.1 deduplicates `webSearchQueries`, so `gateway_grounding_usage.google_search_queries` carries `gateway_grounding_usage_source=sdk_query_count_estimate`; combined cost stays unresolved without authoritative billable quantity. Valid output, source metadata and known parent subtotal are retained. Free monthly allowance is not assumed. Flash-Lite Image/customtools are excluded pending exact support evidence.
- **1.102.1 thinking translation:** the native Vertex adapter recognizes Gemini 3.x Flash, including 3.8, and maps reasoning efforts to `thinkingLevel`. Absence from the Gateway's small special-policy set alone is not evidence that reasoning fails; do not add a new model gate to compensate for it.
- **Owned Omni text interactions — unverified pending provider clarification:** `/v1/interactions` implements owned text retention, usage accounting, streaming and terminal `incomplete` handling. Explicit request rejections become failed records; ambiguous transport outcomes are retained without automatic resubmission. `store:false` prevents upstream retrieval after interruption; returned content remains retained locally. Background execution requires storage. Two text acceptance attempts were rejected: the initial Gateway response was HTTP 500; the corrected request returned provider HTTP 400 with only “Request contains an invalid argument,” no field violation or provider request ID. The corrected body used exact `gemini-omni-1.1-flash-preview`, string input, `stream:false`, `background:false`, `store:true`, `response_format:{type:"text",mime_type:"text/plain"}` and `generation_config.max_output_tokens:128`. Campaign evidence is `r3-omni-text-response.json` and `r3-omni-text-corrected-response.json`. Magic Lens `gemini-omni-flash-text` remains disabled; working video acceptance is independent.
- **Omni text contract evidence (2026-09-24):** The [exact Vertex model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/omni-1-1-flash) documents text input/output. The current official Python SDK accepts the [text/plain format object](https://github.com/googleapis/python-genai/blob/main/google/genai/_gaos/types/interactions/textresponseformat.py), [object or list response formats](https://github.com/googleapis/python-genai/blob/main/google/genai/_gaos/types/interactions/createmodelinteraction.py), string inputs and integer token limits. Its [Vertex bridge](https://github.com/googleapis/python-genai/blob/main/google/genai/_gaos/google_genai.py) does not translate text output fields specially. The Vertex REST reference still contains legacy top-level `response_mime_type` prose; Google's [migration guide](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026) moves MIME type into `response_format` and says the API revision header is ignored after June 8. Neither legacy MIME fields nor a missing revision header is an established fix. No evidence-backed schema correction or confirmed provider exclusion was found; do not label standalone text unsupported or make further paid permutations without new provider evidence.
- **Veo:** [`VertexAdapter`](../generation_job_adapters.py) calls LiteLLM `avideo_generation`; both selected V1/V2 routes are `vertex_litellm_video`. `VertexVeoDirectAdapter` exists but is not selected for new catalog requests. Its presence must not be counted as exposed first/last-frame or extension support.
- **Omni:** the same module's `_submit_omni` calls `https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/interactions` directly with Google auth. Selected route is `vertex_omni_interactions`, currently V1 only. All four Omni aliases resolve to the same exact preview ID.
- **Official information:** [Vertex Gemini inference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/inference), [Gemini 3.1 Flash Image](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-image), [Veo 3.1](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate), [Omni 1.1](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/omni-1-1-flash), [Vertex Interactions API](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/interactions-api), [Vertex pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing).
- **Translation references:** [LiteLLM Vertex](https://docs.litellm.ai/docs/providers/vertex), [Veo](https://docs.litellm.ai/docs/providers/vertex_ai/videos), [images](https://docs.litellm.ai/docs/providers/vertex_image). Compare their examples to installed SDK code. Billing: [Google procedure](../skills/price-verification/references/google.md).

### xAI

<a id="provider-xai"></a>

- **Endpoint/auth:** `https://api.x.ai/v1`, bearer credential from **`GROK_API_KEY`** in this repository. Official examples may call the variable `XAI_API_KEY`; do not silently rename the Gateway's configuration. These Grok models use direct xAI, not the separately available Vertex-hosted product.
- **Text:** native LiteLLM `xai/` translation (`litellm/llms/xai/` in the installed SDK). The `grok-latest` alias is explicitly non-reasoning, not a dynamic choice of the newest Grok release.
- **Images:** [`GrokImageLLM`](../custom_handler_xai.py), exported as [`custom_handler.grok_image`](../custom_handler.py); direct `/images/generations` and `/images/edits` requests. Inspect model-specific input validation and normalization before assuming all image aliases accept the same options.
- **Video:** durable [`XAIAdapter`](../generation_job_adapters.py) uses xAI video generation/edit/extension endpoints and provider file handling where required. The deprecated blocking path is `custom_handler.grok_video` in [`custom_handler_xai.py`](../custom_handler_xai.py). Check [`grok_video_contract.py`](../grok_video_contract.py): the strict 1.5 V2 contract applies to the exact `grok-video-1.5` alias; a snapshot alias is not automatically contract-equivalent. The `GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED` gate controls 1.5 edit/extend admission.
- **Official information:** [text API](https://docs.x.ai/developers/model-capabilities/text/generate-text), [image generation](https://docs.x.ai/developers/model-capabilities/images/generation), [image editing](https://docs.x.ai/developers/model-capabilities/images/editing), [video generation](https://docs.x.ai/developers/model-capabilities/video/generation), [reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video), [video editing](https://docs.x.ai/developers/model-capabilities/video/editing), [extension](https://docs.x.ai/developers/model-capabilities/video/extension), [pricing](https://docs.x.ai/developers/pricing).
- **Grounded Responses accounting (2026-09-24):** exact configured Grok 4.20 aliases pin [current search tariffs](../pricing/evidence/xai-grounding-2026-09-24.json): web searches $0.005/call, fetched X posts $0.005/item and profiles $0.010/item. Provider `usage.server_side_tool_usage_details` supplies the item counts; `x_search_calls` is not billed at the former per-call rate. Missing counts leave total unresolved. Numeric counts are exposed as `gateway_grounding_usage` with source `provider_usage_counts`; citations remain unchanged. Original profiles are retained for old attempts. SDK and isolated accounting tests establish calculation behavior, not live provider acceptance.
- **Billing:** [xAI verification procedure](../skills/price-verification/references/xai.md). Preserve exact floating/snapshot identity when comparing provider usage and receipts.

### BytePlus / ModelArk

<a id="provider-byteplus"></a>

- **Endpoint/auth:** `https://ark.ap-southeast.bytepluses.com/api/v3`, bearer credential `BYTEDANCE_API_KEY`. `SEEDANCE_ARK_BASE` and `SEEDREAM_ARK_BASE` can override defaults; check the actual target environment before a live claim.
- **API distinction:** configured product is BytePlus **ModelArk**, not BytePlus LAS, Volcengine's mainland-China API, consumer Dreamina, or a reseller such as fal/Replicate. Model names, authentication, fields, releases and tariffs from those products are not interchangeable.
- **Seedance:** durable [`BytePlusAdapter`](../generation_job_adapters.py), `/contents/generations/tasks` submit/retrieve, selected route `byteplus_ark_v3`. Review [`seedance_video_contract.py`](../seedance_video_contract.py). Seedance 2.0/Fast have V1/V2 dispatch; 2.5 is durable V1 only. Deprecated blocking 2.0/Fast implementation: [`custom_handler_seedance.py`](../custom_handler_seedance.py). The stored compatibility label `byteplus_las_v1` maps to the current adapter; it does not mean newly submitted models use LAS.
- **Seedream:** [`custom_handler_seedream.py`](../custom_handler_seedream.py) sends generation and `aimage_edit` requests to ModelArk `/images/generations`; edits preserve ordered uploaded bytes as data URLs. Pro's exact upstream is `dola-seedream-5-0-pro-260628`. Pro supports ordinary 1K/1.5K/2K output and single-reference layer decomposition (optional prompt; 1K/1.5K/2K/auto), with every base/layer asset and stacking metadata retained. Each layer uses its actual pixel tier for pricing. Transparent edits require one alpha reference and PNG output; masks are unsupported. Pro still has no streaming or sequential generation. Lite (`seedream-5-0-lite-260128`) supports 2K/3K/4K and sequential/stream output; the Gateway collects its SSE into a concrete image response and settles once. Current official image documentation does not establish a Lite search schema or tariff, so search remains restricted. See [image API](https://docs.byteplus.com/en/docs/ModelArk/1541523) and [layer price evidence](../pricing/evidence/dola-seedream-5-0-pro-260628-layers-2026-09-24.json). These capabilities have hermetic pinned-SDK coverage; live layer acceptance is not yet established.
- **Official information:** [ModelArk models](https://docs.byteplus.com/en/docs/ModelArk/1330310), [image generation API](https://docs.byteplus.com/en/docs/ModelArk/1541523), [video task API](https://docs.byteplus.com/en/docs/ModelArk/1520757), [ModelArk pricing](https://docs.byteplus.com/en/docs/ModelArk/1544106). These pages may require a browser to render their content; do not treat a JavaScript shell or search snippet as complete implementation evidence.
- **Billing:** [BytePlus verification procedure](../skills/price-verification/references/byteplus.md), including product, region, duration/resolution and reference-video distinctions.

### ElevenLabs

<a id="provider-elevenlabs"></a>

- **Endpoint/auth:** `https://api.elevenlabs.io/v1`, `ELEVENLABS_API_KEY`, upstream `xi-api-key` header. No explicit regional endpoint is configured; do not infer data residency from the default hostname.
- **Speech:** native LiteLLM `elevenlabs/` speech translation, Gateway `/audio/speech` to provider `/text-to-speech/{voice_id}`. Inspect `litellm/llms/elevenlabs/` in the pinned SDK.
- **Sound effects/music:** [`AudioStudioLLM._prepare_request`](../custom_handler_audio.py), exported as `custom_handler.audio_studio`. Gateway `/images/generations` wraps provider `/sound-generation` or `/music`, returning **base64 audio** in an Images-shaped response. `mode: image_generation` does not mean the model creates images.
- **Identity distinction:** `audio-studio/elevenlabs-sfx` is the deployment wrapper for `eleven_text_to_sound_v2`; `audio-studio/elevenlabs-music` wraps `music_v1`. Both identities appear in the generated inventory. Do not use the wrapper name as the provider's `model_id`.
- **Magic Lens voice discovery exception:** its Admin adapter, `apps/workflow-manager/lib/voice-providers/elevenlabs.js`, calls ElevenLabs `/v2/voices` and `/v1/voices/{id}` directly with `ELEVENLABS_CATALOG_API_KEY`. Speech uses Gateway `ELEVENLABS_API_KEY`; the two credentials are not proven to belong to the same provider workspace. Existing curated Magic Lens voice UUIDs and verified assignments remain unchanged.
- **Private instant voices (additive implementation; deployment tracked separately):** [`voice_resources.py`](../voice_resources.py) owns `/v1/voices` creation/list/get/delete and migration 005 records consent, sample digests and lifecycle state without retaining sample bytes. Both creation and synthesis use the Gateway's ElevenLabs account, so cloned resources never enter Admin discovery's account boundary. Only authenticated key metadata `gateway_resource_delegate=true` plus stable `gateway_app_id` permits the server's `X-Gateway-Resource-Owner` delegation. Speech resolves `voice:<gateway-resource-id>` in an authenticated pre-call hook; tracked raw provider IDs cannot bypass ownership. Creation persists its idempotent intent before the provider call, reconciles unknown outcomes through a provider label, and never retries an uncertain creation. Refresh can move provider verification to ready without a curated Admin audition; deletion revokes locally before retryable provider cleanup. [IVC create](https://elevenlabs.io/docs/api-reference/voices/ivc/create), [get voice](https://elevenlabs.io/docs/api-reference/voices/get), [delete voice](https://elevenlabs.io/docs/api-reference/voices/delete).
- **Advanced owned audio (additive implementation):** [`advanced_audio.py`](../advanced_audio.py) adds finite dialogue, timestamp speech, composition planning, SFX/music generation, song import/edit/extend and speech-session routes. Migration 006 persists request identity, accounting IDs and private payloads; provider IDs remain inside owner-scoped resources. Uncertain provider calls are not retried. A one-use speech session pins model, voices and quoted character quota before its WebSocket connection; completed results remain retrievable after disconnect, with explicit incomplete metadata for valid partial audio. Default native speech and Images-shaped calls remain unchanged.
- **Image/native capability discovery:** Exact image aliases expose their implemented controls. Seedream Lite/5 stream requests buffer the complete provider event stream; they do not provide live partial images. Pro exposes layers and retains all layer outputs, without stream or sequential generation. Gemini video references use the Chat Completions file part only on the eligible Flash/Lite image aliases. Native Omni interactions and Gemini token counting have separate endpoints. Flare streaming remains explicitly unverified because the official documentation conflicts. `tests/test_image_discovery.py` checks these distinctions against the executing contracts.
- **Audio capability discovery:** `/v1/capabilities` advertises only the five implemented ElevenLabs aliases. Its audio operations distinguish native binary speech from timestamp JSON, v3-only dialogue, one-use WebSocket sessions, SFX settings, v1 sections, v2.5 chunks/import/edit/extend, and owner-scoped voice/song resources. Format examples remain subject to the provider model and subscription. Header-only import idempotency retains the original intent; malformed import responses remain unknown without resubmission, while invalid output media and free plans reach a terminal error instead of staying pending. Focused discovery and disposable-database coverage is in `tests/test_audio_discovery.py` and `tests/test_advanced_audio.py`.
- **Exact music and storage boundary:** `elevenlabs-music` stays `music_v1`; the separate `elevenlabs-music-2.5` maps to `music_v2_5`. Music plan drafting is documented free; song import is priced like music generation, and copyright rejection can be partially charged, so missing usage stays unknown. Owned song DELETE purges local reuse/payloads; provider song cleanup is explicitly unsupported because no documented DELETE exists. Account cleanup uses `/v1/audio/owner-data`, preserving accounting history. [Music plans](https://elevenlabs.io/docs/api-reference/music/create-composition-plan), [inpainting chunks](https://elevenlabs.io/docs/eleven-api/guides/how-to/music/inpainting), [paid song upload](https://elevenlabs.io/docs/api-reference/music/upload), [dialogue WebSocket](https://elevenlabs.io/docs/api-reference/text-to-dialogue/ttd-websocket).
- **Official information:** [speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert), [sound effects API](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert), [music API](https://elevenlabs.io/docs/api-reference/music/compose), [API pricing](https://elevenlabs.io/pricing/api). [LiteLLM speech translation](https://docs.litellm.ai/docs/providers/elevenlabs); [ElevenLabs billing procedure](../skills/price-verification/references/elevenlabs.md).

## Complete configured model inventory

Provider-qualified identities below come from the pricing registry. A second
deployment identity is shown when the LiteLLM wrapper differs. Prefixes such as
`seedream/` and `grok-image/` are Gateway routing namespaces; the provider receives
the model ID extracted by its handler. A dash in settings means no listed
location/effort override, not worldwide availability. Durable dispatch lists
selected routes, not every operation accepted on them.

<!-- BEGIN GENERATED PROVIDER INVENTORY -->

**56 configured aliases; 43 distinct upstream identities; 5 serving providers.** Alias counts: byteplus: 6; elevenlabs: 5; google: 18; openai: 18; xai: 9.

Generated from `litellm_config.yaml`, `pricing/registry.json`, and the executable `generation_job_adapters.route_for` dispatcher. `priced` means at least one enabled pricing profile; it does not certify access or every request option. The LiteLLM dispatch column identifies configuration, including legacy handlers; durable admission and model-specific restrictions still apply.

| Public alias | Serving vendor | Exact upstream identity | LiteLLM dispatch | Durable dispatch | Configured location / effort | Admission |
| --- | --- | --- | --- | --- | --- | --- |
| `gpt-latest` | [openai](#provider-openai) | `openai/gpt-6-astra` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.5` | [openai](#provider-openai) | `openai/gpt-5.5` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.5-thinking` | [openai](#provider-openai) | `openai/gpt-5.5` | LiteLLM `openai/` | — | reasoning_effort: `high` | priced |
| `gpt-6-astra` | [openai](#provider-openai) | `openai/gpt-6-astra` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.6-sol` | [openai](#provider-openai) | `openai/gpt-5.6-sol` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.6-terra` | [openai](#provider-openai) | `openai/gpt-5.6-terra` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.6-luna` | [openai](#provider-openai) | `openai/gpt-5.6-luna` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.6-sol-medium` | [openai](#provider-openai) | `openai/gpt-5.6-sol` | LiteLLM `openai/` | — | reasoning_effort: `medium` | priced |
| `gpt-5.6-terra-medium` | [openai](#provider-openai) | `openai/gpt-5.6-terra` | LiteLLM `openai/` | — | reasoning_effort: `medium` | priced |
| `gpt-5.6-luna-medium` | [openai](#provider-openai) | `openai/gpt-5.6-luna` | LiteLLM `openai/` | — | reasoning_effort: `medium` | priced |
| `gpt-5.6-luna-high` | [openai](#provider-openai) | `openai/gpt-5.6-luna` | LiteLLM `openai/` | — | reasoning_effort: `high` | priced |
| `gpt-5.4` | [openai](#provider-openai) | `openai/gpt-5.4` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.4-mini` | [openai](#provider-openai) | `openai/gpt-5.4-mini` | LiteLLM `openai/` | — | — | priced |
| `gpt-5.4-nano` | [openai](#provider-openai) | `openai/gpt-5.4-nano` | LiteLLM `openai/` | — | — | priced |
| `gpt-image-1.5` | [openai](#provider-openai) | `openai/gpt-image-1.5` | LiteLLM `openai/` | — | — | priced |
| `gpt-image-2` | [openai](#provider-openai) | `openai/gpt-image-2` | LiteLLM `openai/` | — | — | priced |
| `gpt-image-2.5-sunburst` | [openai](#provider-openai) | `openai/gpt-image-2.5-sunburst-2026-09-08` | LiteLLM `openai/` | — | — | priced |
| `gpt-image-2.5-flare` | [openai](#provider-openai) | `openai/gpt-image-2.5-flare-2026-09-08` | LiteLLM `openai/` | — | — | priced |
| `gemini-latest` | [google](#provider-google) | `vertex_ai/gemini-3.1-pro-preview` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `gemini-3.1-pro` | [google](#provider-google) | `vertex_ai/gemini-3.1-pro-preview` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `gemini-3.1-pro-customtools` | [google](#provider-google) | `vertex_ai/gemini-3.1-pro-preview-customtools` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `gemini-3.5-flash` | [google](#provider-google) | `vertex_ai/gemini-3.5-flash` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `gemini-3.8-flash` | [google](#provider-google) | `vertex_ai/gemini-3.8-flash` | LiteLLM `vertex_ai/` | — | vertex_location: `global` | priced |
| `gemini-3.5-flash-lite` | [google](#provider-google) | `vertex_ai/gemini-3.5-flash-lite` | LiteLLM `vertex_ai/` | — | vertex_location: `global` | priced |
| `gemini-3-flash-preview` | [google](#provider-google) | `vertex_ai/gemini-3-flash-preview` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `nano-banana` | [google](#provider-google) | `vertex_ai/gemini-3.1-flash-image` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `nano-banana-2` | [google](#provider-google) | `vertex_ai/gemini-3.1-flash-image` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `nano-banana-pro` | [google](#provider-google) | `vertex_ai/gemini-3-pro-image` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `nano-banana-2-lite` | [google](#provider-google) | `vertex_ai/gemini-3.1-flash-lite-image` | LiteLLM `vertex_ai/` | — | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `veo-3.1` | [google](#provider-google) | `vertex_ai/veo-3.1-generate-001` | LiteLLM `vertex_ai/` | V1: `vertex_litellm_video`<br>V2: `vertex_litellm_video`<br>V2 `video-contract-v2-2026-09-24`: `vertex_veo_expanded_v2` | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `veo-3.1-fast` | [google](#provider-google) | `vertex_ai/veo-3.1-fast-generate-001` | LiteLLM `vertex_ai/` | V1: `vertex_litellm_video`<br>V2: `vertex_litellm_video`<br>V2 `video-contract-v2-2026-09-24`: `vertex_veo_expanded_v2` | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `veo-3.1-lite` | [google](#provider-google) | `vertex_ai/veo-3.1-lite-generate-001` | LiteLLM `vertex_ai/` | V1: `vertex_litellm_video`<br>V2: `vertex_litellm_video`<br>V2 `video-contract-v2-2026-09-24`: `vertex_veo_expanded_v2` | vertex_location: `os.environ/GOOGLE_CLOUD_LOCATION` | priced |
| `gemini-omni-flash` | [google](#provider-google) | `vertex_ai/gemini-omni-1.1-flash-preview` | LiteLLM `vertex_ai/` | V1: `vertex_omni_interactions`<br>V2: unavailable<br>V2 `video-contract-v2-2026-09-24`: `vertex_omni_expanded_v2` | vertex_location: `global` | priced |
| `gemini-omni-flash-preview` | [google](#provider-google) | `vertex_ai/gemini-omni-1.1-flash-preview` | LiteLLM `vertex_ai/` | V1: `vertex_omni_interactions`<br>V2: unavailable<br>V2 `video-contract-v2-2026-09-24`: `vertex_omni_expanded_v2` | vertex_location: `global` | priced |
| `gemini-omni-1.1-flash` | [google](#provider-google) | `vertex_ai/gemini-omni-1.1-flash-preview` | LiteLLM `vertex_ai/` | V1: `vertex_omni_interactions`<br>V2: unavailable<br>V2 `video-contract-v2-2026-09-24`: `vertex_omni_expanded_v2` | vertex_location: `global` | priced |
| `gemini-omni-1.1-flash-preview` | [google](#provider-google) | `vertex_ai/gemini-omni-1.1-flash-preview` | LiteLLM `vertex_ai/` | V1: `vertex_omni_interactions`<br>V2: unavailable<br>V2 `video-contract-v2-2026-09-24`: `vertex_omni_expanded_v2` | vertex_location: `global` | priced |
| `grok-video` | [xai](#provider-xai) | `grok-video/grok-imagine-video` | `custom_handler.grok_video` | V1: `xai_videos_v1`<br>V2: `xai_videos_v2` | — | priced |
| `grok-video-1.5` | [xai](#provider-xai) | `grok-video/grok-imagine-video-1.5` | `custom_handler.grok_video` | V1: `xai_videos_v1`<br>V2: `xai_videos_v2`<br>V2 `video-contract-v2-2026-09-24`: `xai_expanded_v2` | — | priced |
| `grok-imagine-video-1.5-2026-05-30` | [xai](#provider-xai) | `grok-video/grok-imagine-video-1.5-2026-05-30` | `custom_handler.grok_video` | V1: `xai_videos_v1`<br>V2: `xai_videos_v2`<br>V2 `video-contract-v2-2026-09-24`: `xai_expanded_v2` | — | priced |
| `grok-image` | [xai](#provider-xai) | `grok-image/grok-imagine-image-quality` | `custom_handler.grok_image` | — | — | priced |
| `grok-imagine-image-quality` | [xai](#provider-xai) | `grok-image/grok-imagine-image-quality` | `custom_handler.grok_image` | — | — | priced |
| `grok-imagine-image-2.0` | [xai](#provider-xai) | `grok-image/grok-imagine-image-2.0` | `custom_handler.grok_image` | — | — | priced |
| `seedance-2.0` | [byteplus](#provider-byteplus) | `seedance/dreamina-seedance-2-0-260128` | `custom_handler.seedance` | V1: `byteplus_ark_v3`<br>V2: `byteplus_ark_v3`<br>V2 `video-contract-v2-2026-09-24`: `byteplus_expanded_v2` | — | priced |
| `seedance-2.0-fast` | [byteplus](#provider-byteplus) | `seedance/dreamina-seedance-2-0-fast-260128` | `custom_handler.seedance` | V1: `byteplus_ark_v3`<br>V2: `byteplus_ark_v3`<br>V2 `video-contract-v2-2026-09-24`: `byteplus_expanded_v2` | — | priced |
| `seedance-2.5` | [byteplus](#provider-byteplus) | `seedance/dreamina-seedance-2-5-260628` | `custom_handler.seedance` | V1: `byteplus_ark_v3`<br>V2: unavailable<br>V2 `video-contract-v2-2026-09-24`: `byteplus_expanded_v2` | — | priced |
| `seedream-5.0` | [byteplus](#provider-byteplus) | `seedream/seedream-5-0-260128` | `custom_handler.seedream` | — | — | priced |
| `seedream-5.0-lite` | [byteplus](#provider-byteplus) | `seedream/seedream-5-0-lite-260128` | `custom_handler.seedream` | — | — | priced |
| `seedream-5.0-pro` | [byteplus](#provider-byteplus) | `seedream/dola-seedream-5-0-pro-260628` | `custom_handler.seedream` | — | — | priced |
| `grok-latest` | [xai](#provider-xai) | `xai/grok-4.20-non-reasoning-latest` | LiteLLM `xai/` | — | — | priced |
| `grok-4.20-reasoning` | [xai](#provider-xai) | `xai/grok-4.20-reasoning-latest` | LiteLLM `xai/` | — | — | priced |
| `grok-4.20` | [xai](#provider-xai) | `xai/grok-4.20-non-reasoning-latest` | LiteLLM `xai/` | — | — | priced |
| `elevenlabs-v3-tts` | [elevenlabs](#provider-elevenlabs) | `elevenlabs/eleven_v3` | LiteLLM `elevenlabs/` | — | — | priced |
| `elevenlabs-multilingual-v2` | [elevenlabs](#provider-elevenlabs) | `elevenlabs/eleven_multilingual_v2` | LiteLLM `elevenlabs/` | — | — | priced |
| `elevenlabs-sfx` | [elevenlabs](#provider-elevenlabs) | `elevenlabs/eleven_text_to_sound_v2`<br>Deployment: `audio-studio/elevenlabs-sfx` | `custom_handler.audio_studio` | — | — | priced |
| `elevenlabs-music` | [elevenlabs](#provider-elevenlabs) | `elevenlabs/music_v1`<br>Deployment: `audio-studio/elevenlabs-music` | `custom_handler.audio_studio` | — | — | priced |
| `elevenlabs-music-2.5` | [elevenlabs](#provider-elevenlabs) | `elevenlabs/music_v2_5`<br>Deployment: `audio-studio/elevenlabs-music-2.5` | `custom_handler.audio_studio` | — | — | priced |

<!-- END GENERATED PROVIDER INVENTORY -->

## Magic Lens boundary and historical models

**JEV / TypeSafe is exclusively a Magic Lens integration.** Its decision client is
`services/experience-runner/src/canvasAssistantDecisionClient.js` in the sibling
Magic Lens repository. Its adjacent `canvasAssistantDecisionContract.js` pins
`jev-1.13.0` and `https://api.typesafe.ai/v1/systemone`; the client uses Magic Lens's
server-side `JEV_API_KEY`. Provider reference: [TypeSafe API](https://docs.typesafe.ai/api).
It must remain absent from Gateway models, handlers, routes and credential
configuration. See the [JEV boundary and consumer contracts](magiclens-integration-2026-09-24.md#integration-points-and-contracts-to-preserve).

Magic Lens has its own published model registry, request profiles, compiler,
Studio adapter and Runner adapter. Adding a Gateway capability does not update
them automatically. Preserve public aliases, contract revisions, media roles,
job schemas, idempotency, content retrieval, error semantics, accounting IDs and
pricing versions when changing implementation. Read the linked integration review
and inspect both consumers before changing those contracts.

Retired Imagen 4 aliases and Gemini 3.1 Flash-Lite Preview were removed from the
local active catalog. Historical pricing profiles remain for receipt resolution
and must not be counted as active models. The [dated retirement review](magiclens-integration-2026-09-24.md)
records that deployed dev still advertised 56 aliases and Magic Lens still exposed
the retired Flash-Lite Preview choice when checked. Deployment and consumer
retirement remain separate work; this reference does not imply they happened.

## Keeping this reference accurate

Run in the Gateway dependency environment:

```sh
python scripts/provider_inventory.py --write
python scripts/provider_inventory.py
python scripts/pricing_coverage.py --release
```

The [inventory generator](../scripts/provider_inventory.py) derives all aliases,
upstream/deployment identities, vendors, configured handlers, selected durable
routes, locations and effort defaults from code/configuration. CI rejects a stale
table, mismatched alias/deployment sets, or a vendor missing its implementation
section. Endpoint/auth/contract prose and external links still need human review
when those implementations change; this is not automatic live-provider auditing.

For each provider change, record the exact model/product/location, official source
and date, selected adapter and pinned SDK behavior, validation performed, and
remaining unverified claims. Use the existing [onboarding process](model-onboarding.md),
[pricing procedures](../skills/price-verification/SKILL.md),
[durable job contract](durable-generation-jobs.md), and
[LiteLLM upgrade and rollout notes](litellm-v1.102-upgrade.md). Never silently substitute another
serving product to make a model or feature appear supported.
