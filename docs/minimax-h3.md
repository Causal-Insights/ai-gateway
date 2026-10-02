# MiniMax H3 hosted video

Verified against official MiniMax documentation on **2026-10-02**. Both
`minimax-h3` and `MiniMax-H3` resolve to **MiniMax-H3**, served directly by
MiniMax's hosted pay-as-you-go API. LiteLLM 1.102.1 accepts the catalog namespace
`minimax/MiniMax-H3`, but supplies neither an H3 video adapter nor H3 prices.
The Gateway uses `minimax_h3_v2`, revision `minimax_h3_v2@2026-10-02`.
No Hailuo, H3 Max, third-party serving, local inference, or fallback is used.

## Configuration

Set `MINIMAX_API_KEY` in the ignored `.env` used by Docker Compose, or bind the
same environment variable to a deployment secret. Use a **pay-as-you-go API key**,
not a subscription key. `.env.example` contains only a placeholder; the existing
`.env_example` remains the complete legacy environment template. Never place a
real credential in either template or a request body.

For Cloud Run deployment, set
`MINIMAX_API_KEY_SECRET` to the existing Secret Manager secret's name. The deploy
script adds the binding only when this variable is supplied, so deployments for
existing providers do not require a MiniMax secret. Do not run deployment as part
of this integration's offline verification. Apply the normal forward migrations;
009 extends the existing job provider constraint without changing old rows.
The Gateway image already includes ffprobe for media inspection.

## Gateway request contract

Use authenticated `POST /v1/generation-jobs` with `Idempotency-Key`,
`request_schema_version: 2`, `contract_revision: video-contract-v2-2026-09-24`,
and `operation: generate`. V1 and synchronous Images/Chat/Responses/Video routes
are unsupported. Discover the complete declaration through `/v1/capabilities`.

| Profile | Media roles |
| --- | --- |
| `generate.text` | None |
| `generate.first_frame` | One image `first_frame` |
| `generate.last_frame` | One image `last_frame` |
| `generate.first_last_frames` | One image of each frame role |
| `generate.references` | Images with `reference` |
| `generate.multimodal_references` | Any mix of image `reference`, video `reference_video`, audio `reference_audio` |

Each media item has `slot_id`, contiguous zero-based `index` within the slot,
`kind`, `role`, and exactly one HTTPS `url` or multipart `upload_field`. Order is
preserved. `reference` maps to MiniMax's `reference_image`. Reference videos are
**generation references**, not `source` inputs or an implicit edit operation.
First/last-frame inputs cannot mix with reference media.

Settings: integer `duration` 4–15 seconds (default 5), `resolution` `768p` or `2k`
(default `768p`), `outputCount: 1`, and `aspectRatio`. Text ratios are `21:9`,
`16:9`, `4:3`, `1:1`, `3:4`, `9:16` (default `16:9`). Frame profiles inherit the
image ratio and accept only `adaptive`. References accept those concrete ratios
or `adaptive` (default). Existing `aspect_ratio` and `output_count` spellings
are also accepted; conflicting duplicate settings are rejected.
Audio is provider-managed and embedded in the original MP4. There is no
`generateAudio` toggle, seed, preset voice ID, or multiple-output option.

## Input limits and remaining gaps

Every request requires a nonempty prompt, at most 7,000 characters. References
are limited to nine images, three videos, three audio clips, and twelve total
files. Video and audio each have a separate 15-second aggregate limit; every
clip is 2–15 seconds. Dimensions for images/video are 256–5760 pixels per axis,
aspect ratio 0.4–2.5; video frame rate is 23.976–60 FPS.

| Input | Formats | Maximum per file |
| --- | --- | --- |
| Image | JPEG, PNG, WebP, HEIC, HEIF | 30 MB |
| Video | MP4/MOV, H.264/H.265; embedded audio AAC/MP3 | 50 MB |
| Audio | WAV, MP3 | 15 MB |

Provider file/body limits use decimal MB (1,000,000 bytes).
Media is downloaded using existing public-HTTPS/redirect protections and probed
before paid submission. Unreadable or unsupported-by-ffprobe inputs receive an
explicit input error; no transcoding or format substitution occurs. HEIC/HEIF
inspection depends on the packaged ffprobe decoder. Original URLs are forwarded
for HTTPS inputs; inputs must remain available and stable for provider retrieval.

Multipart inputs become documented data URIs. MOV references require HTTPS
because MiniMax documents only MP4 inline video. The encoded upstream body is
limited to 64 MB, including Base64 expansion. Existing Gateway multipart limits
also apply: ten uploaded files and `GENERATION_MAX_UPLOAD_BYTES` total (default
100 MiB). Twelve references can be supplied using URLs or a mix of URLs/uploads.

Provider `mm_file://` references, standalone generated audio assets, Context-IR,
768P-to-2K regeneration, dedicated source editing/extension, cancellation controls,
and MiniMax callbacks are not exposed by this integration. They are never invoked
as hidden preparation steps. The original audiovisual MP4 is returned intact.

The create page's supported combinations and the current guide explicitly list
last-frame-only generation. Its nested role description still says to pair a last
frame with a first frame; this integration follows the explicit combinations and
guide, and records that documentation inconsistency rather than adding a gate.

## Durable jobs, errors, and cost

Submit uses `POST https://api.minimax.io/v2/video_generation`. Poll uses
`GET https://api.minimax.io/v2/query/video_generation/{task_id}`. `queued`,
`running`, `succeeded`, `failed`, `cancelled` map to Gateway `queued`,
`in_progress`, `completed`, `failed`, `cancelled`. H3 polling has a ten-second
minimum. The existing two-hour default deadline includes a final status check.
Status timeouts, throttling, and server errors (including 529) retry normally.
Ambiguous submissions, missing task IDs, and submission server errors are never
automatically repeated; reconcile the original attempt. Definitive provider
rejections retain the existing retryability semantics.

A completed job exposes `result.content_url` and one `outputs` entry. Both use
owner-authenticated Gateway downloads and retain the original audiovisual bytes.
Expired provider URLs are refreshed by task retrieval. MiniMax only permits task
queries for seven days, and output URLs expire sooner; persist assets promptly.
Gateway expiration does not prove that provider execution stopped or was free.

Official output rates are **$0.08/second at 768P** and **$0.13/second at 2K**.
Input video seconds use the output-resolution rate. The first five input images
are free; additional images cost $0.04 each. Input audio is free.

`cost = (usage.output_seconds + usage.input_seconds) × resolution_rate
        + max(usage.input_image_count − 5, 0) × 0.04`

Use provider-metered quantities and decimal arithmetic, not requested duration,
local media duration, or token equivalents. For example, four output seconds,
three input-video seconds, and seven images cost $0.64 at 768P or $0.99 at 2K.
This is a posted-rate calculation, not a reported monetary charge or an invoice.
No additional rounding rule, discount, or free-failure rule is assumed.

The journal/execution intent precedes submission. Numeric usage survives failed
or malformed-result operations, and repeated observations do not duplicate spend.
Partial/missing usage remains visible as null cost with unresolved status. A valid
asset remains available even when its cost cannot yet be calculated. Actual served
model/resolution is recorded; incompatible evidence is flagged for reconciliation.
Historical prices and customer quotes are unchanged. Retrieve authoritative exact
amounts through `/v1/costs/{accounting_id}`.

## Magic Lens handoff

Studio and Runner both use the shared V2 compiler and durable-jobs client, stable
idempotency keys, owner-authenticated content URLs, and Gateway accounting IDs.
This change does not update their independent published model registry. A future
consumer change must register H3 and the profiles above, explicitly select
`generate` for reference videos, and omit audio toggles. Consume the audiovisual
MP4 and persist it; do not turn absent/unresolved cost into zero. Preserve attempt
attribution, model identity, pricing version, and existing financial reconciliation.
No Magic Lens files are modified or end-to-end acceptance claimed here.

## Minimal live test — review before running

Estimated provider spend: **$0.32**, one four-second 768P text-only video. No paid
calls are part of the automated tests. Configure the server's MiniMax key first;
set `GATEWAY_BASE_URL` and `GATEWAY_API_KEY` privately in the client environment.
Use an already running local/test Gateway with durable polling configured.

```sh
curl -fsS "$GATEWAY_BASE_URL/v1/models" -H "Authorization: Bearer $GATEWAY_API_KEY"
curl -fsS "$GATEWAY_BASE_URL/v1/capabilities" -H "Authorization: Bearer $GATEWAY_API_KEY"
```

Save this as `h3-smoke.json`:

```json
{
  "request_schema_version": 2,
  "model": "minimax-h3",
  "contract_revision": "video-contract-v2-2026-09-24",
  "profile_id": "generate.text",
  "operation": "generate",
  "prompt": "A steady shot of gentle ocean waves with natural surf sounds.",
  "settings": {"resolution": "768p", "duration": 4, "aspectRatio": "16:9"},
  "media": []
}
```

Choose and retain one unique idempotency key for this test. After approval:

```sh
export H3_SMOKE_IDEMPOTENCY_KEY="h3-smoke-$(uuidgen)"
curl -fsS "$GATEWAY_BASE_URL/v1/generation-jobs" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Idempotency-Key: $H3_SMOKE_IDEMPOTENCY_KEY" \
  -H 'Content-Type: application/json' --data-binary @h3-smoke.json
```

Record the response `id` as `H3_JOB_ID`. Poll at `poll_after_ms` intervals:

```sh
curl -fsS "$GATEWAY_BASE_URL/v1/generation-jobs/$H3_JOB_ID" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

After completion, download and inspect the video, then query the returned
`accounting_id` (set as `H3_ACCOUNTING_ID`):

```sh
curl -fsS "$GATEWAY_BASE_URL/v1/generation-jobs/$H3_JOB_ID/content" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -o h3-smoke.mp4
ffprobe -v error -show_streams -show_format h3-smoke.mp4
curl -fsS "$GATEWAY_BASE_URL/v1/costs/$H3_ACCOUNTING_ID" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

Verify four output seconds, the served resolution, an audio stream, expected
$0.32 posted-rate cost, attribution, and a single execution charge. Replay the
original POST with the same body/key to confirm the same job and accounting ID.
After a submission transport timeout, replay that same key; never create another
key automatically. A failed/unknown job requires investigation before another
paid test. Never enable verbose HTTP tracing or shell tracing around credentials.

## Offline validation (2026-10-02)

- Pinned LiteLLM 1.102.1 dependency environment, network disabled: 442 tests,
  324 passed and 118 conditional skips. The final complete suite passed.
- Disposable PostgreSQL: 51 accounting checks, 36 execution-log checks, and
  three job-repository checks passed (90 total), including the new H3 cases.
  The disposable database and network were removed afterward.
- Inventory: 58 aliases; pricing coverage: 142 profiles, zero errors; 57
  structured evidence reports validated. All 140 prior profiles and 56 prior
  model mappings are unchanged.
- Deployment tests use fake cloud commands; no cloud deployment occurred.
  Skill metadata/synchronization, shell syntax, and whitespace checks passed.
- No live provider request, paid generation, or Magic Lens modification was made.
  Account access and actual provider output remain unverified pending the reviewed
  smoke test above.

## Live local test attempt (2026-10-02)

Following explicit authorization, a six-second 768P text generation was submitted
through the local Gateway's `/v1/generation-jobs` endpoint. MiniMax rejected the
submission with `insufficient balance (1008)`, surfaced as
`PROVIDER_REJECTED_SUBMISSION`. No provider task ID or generated asset was returned.
The failed attempt remains recorded under
`gen_c6992a9388c4441da0bcf7961179684f`; `/v1/costs/{accounting_id}` reports null cost
and unresolved usage, preserving the immutable 768P pricing snapshot.

That run stopped before submitting the remaining video-reference edit and 2K
generation. The rejected attempt remains preserved with null/unresolved cost.

After the user requested a retry, three generations completed through the local
LiteLLM Gateway and were downloaded through its authenticated content endpoint:

| Test | Gateway job / accounting ID | Provider task | Metered output / input video seconds | Calculated USD |
| --- | --- | --- | --- | --- |
| 768P redwood bicycle | `gen_a997e41c7cf345deb180692950cd45a7` | `448155806638577` | 6 / 0 | $0.48 |
| 768P bicycle-to-bear video-reference edit | `gen_4d6cf04528a14b2cbe792f1ac70f9ebf` | `448155985445372` | 6 / 7 | $1.04 |
| 2K clockwork ocean | `gen_a2cc89ccba3b428ab28214cc31d51325` | `448158235935240` | 6 / 0 | $0.78 |

The completed generations total **$2.30 at posted rates**, without invoice-level
reconciliation or assigning zero cost to the earlier unresolved rejection. Every
completed job has one logged execution, complete attribution, and an applied cost
projection. Replaying each exact request/idempotency key returned its original job
and accounting ID. The source clip's `content` and `outputs/0` downloads were
byte-identical; unauthenticated download returned HTTP 401. Three numeric live
price-evidence reports passed the evidence JSON schema.

All outputs contain H.264 video at 24 FPS and AAC audio. The 768P files are
1344 × 768; the 2K file is 2560 × 1440. Although requested and metered output
duration is six seconds, the original MP4s run about 6.58 seconds. MiniMax metered
the reused source as seven input seconds; accounting uses that evidence, not a
guessed duration or rounding rule. Original bytes were preserved.

Sampled frames show the bear replacing the bicycle while retaining the rider's
yellow jacket, green helmet, forest setting, and side-on composition. This verifies
one prompt-driven video-reference edit, not precise pixel preservation or every
H3 profile. Frame/image/audio-reference profiles and URL refresh remain covered
offline rather than by this live set. Audio-stream presence was verified; semantic
audio quality was not independently assessed.

Local prompts, videos, preview sheets, job/cost receipts, and the comparison gallery
are retained under ignored `local-tests/minimax-h3-20261002/`. The local Compose
configuration now mounts the H3 contract/adapter alongside existing provider
modules. No cloud deployment or Magic Lens modification occurred.

## Cloud Run deployment (2026-10-02)

Following explicit redeployment authorization, release `minimax-h3-20261002-r1`
was verified as a no-traffic candidate and promoted callback-first to 100% traffic:

- Gateway: `ai-gateway-proxy-00178-sor`.
- Callback service: `ai-gateway-callbacks-00039-siw`.
- Image digest: `sha256:8f92bb4fedd368b2652d3e7585751fbc6abc223e8f593ebc44097401cdfd9e37`.
- Project / region: `ai-gateway-495414` / `us-central1`.
- Production URL: `https://ai-gateway-proxy-bdlot74z6a-uc.a.run.app`.

`MINIMAX_API_KEY` is bound from Secret Manager with access granted to the existing
Gateway runtime service account. The secret was not printed, committed, or included
in the build upload. Existing service capacity was preserved: maximum one instance
per service, concurrency eight, and async database pool size one.

Candidate and production checks returned HTTP 200 for Gateway liveliness/readiness,
callback liveliness, and capability discovery. Model discovery contains all 56
previous aliases plus the two H3 aliases. The final revisions had no error-level
logs at the post-promotion check. Release checks passed: nine deployment tests,
19 focused H3 tests, 58-alias inventory, 142 pricing profiles with zero errors, and
57 structured price evidence reports. No paid cloud generation was run.

The first uncached build failed because the current FFmpeg 9 package attempted to
overwrite OpenSSL configuration files owned by the pinned base image. The successful
Cloud Build (`afc64757-766e-4632-a606-3f499bcb190f`) reused the existing production
image's working FFmpeg layer via `--cache-from`; no forced package overwrites or
LiteLLM version change were used. A future uncached build can encounter the same
upstream package conflict; reuse this immutable release image or a compatible
dependency cache until the package conflict is addressed.

Release manifest, build configuration, baseline/candidate/production checks, and
error-log results are retained under ignored `local-tests/deployments/` with the
`h3-` prefix and manifest `b8d1acc2bff2-serving.json`. Previous revisions
`ai-gateway-proxy-00171-fic` and `ai-gateway-callbacks-00037-pig` remain available
for traffic rollback. The additive provider constraint does not require reversal
for the prior image. Magic Lens was not modified.

## Official sources

- [Create schema](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)
- [Guide and combined input limits](https://platform.minimax.io/docs/guides/video-generation)
- [Task status, outputs and metering](https://platform.minimax.io/docs/api-reference/video-generation-v2-query)
- [Pay-as-you-go pricing](https://platform.minimax.io/docs/guides/pricing-paygo)
- [Limits](https://platform.minimax.io/docs/guides/rate-limits): 300 RPM and 30 inflight H3 tasks; account limits remain provider-enforced.
