# Durable generation jobs API

Long-running video generation uses the job API. Legacy `/v1/images/generations` video calls
are deprecated because they may keep an HTTP request open while a provider renders.

## Submit

```http
POST /v1/generation-jobs
Authorization: Bearer <LiteLLM key>
Idempotency-Key: <stable run:step:attempt key>
Content-Type: application/json
```

```json
{
  "model": "seedance-2.0",
  "modality": "video",
  "operation": "auto",
  "prompt": "A slow dolly shot through a sunlit conservatory",
  "duration_seconds": 8,
  "resolution": "720p",
  "aspect_ratio": "16:9",
  "generate_audio": true,
  "previous_job_id": null,
  "reference_voice_ids": [],
  "media_inputs": []
}
```

The response is `202 Accepted`. Reusing the key with the same body returns the same job;
reusing it with a different body returns `409`. A timeout has an ambiguous outcome and must be
resolved by replaying the same idempotency key. Never switch to a legacy submit route.

Multipart submissions put the JSON above in a `request` form field. Each media item that uses
`upload_field` names an accompanying file field. BytePlus media must use HTTPS URLs; xAI and
Veo accept supported multipart references.

`operation` defaults to `auto`: a source video selects editing and all other inputs select
generation. Gemini Omni Flash 1.1 uses the canonical `gemini-omni-1.1-flash` alias. The
historical `gemini-omni-flash`, `gemini-omni-flash-preview`, and
`gemini-omni-1.1-flash-preview` aliases resolve to the same 1.1 Vertex upstream so saved
requests keep working.

Gemini Omni Flash 1.1 accepts text, one first frame, up to ten reference images, a first frame
plus references, ordered first and last frames for interpolation, one source video for editing
or extension, and `previous_job_id` for stateful edit or extension. Reference images may
accompany a source only for an explicit `extend` operation. A previous job must belong to the
same key owner and may be a completed job created through either an original or 1.1 Omni
alias. Source uploads must be MP4 so the Gateway can enforce the ten-second input limit.
Outputs are three to ten seconds, one video at fixed 24 FPS, 16:9 or 9:16, and 360p, 720p,
1080p, or 4K. Native generated audio is always present; uploaded audio and voice references
are rejected.

The public stable alias deliberately maps to the Google Cloud Vertex Interactions model
`gemini-omni-1.1-flash-preview`. Do not change that upstream to the Gemini Developer API GA
ID without an authenticated Vertex acceptance probe. Every durable job records the exact
upstream model in protected request metadata, while historical job rows retain the model alias
originally submitted.

Production verification on 2026-09-01 exercised every declared Omni 1.1 media path,
including first/last interpolation, source editing and extension, prior-interaction
continuation, and a 1080p audiovisual output. Revision `ai-gateway-proxy-00052-68m`
serves 100% and maps every retained Omni alias to the exact preview upstream.

`grok-video-1.5` supports text, starting-image, reference-image, and up to three preset voice
references. It never falls back to a different upstream model. Video editing and extension use
the explicit legacy `grok-video` alias until xAI documents and verifies those operations on 1.5.

## Retrieve and content

`GET /v1/generation-jobs/{id}` returns `queued`, `in_progress`, `completed`, `failed`,
`expired`, or `cancelled`, plus `poll_after_ms`, progress, provider request ID, usage/cost, and
a normalized error. Completed jobs include a gateway-owned content URL.

`GET /v1/generation-jobs/{id}/content` requires the same authentication and supports chunked
streaming. Consumers should persist the stream into their own asset storage promptly because
provider source URLs may be temporary.

When the gateway is started with `docker compose`, local in-process polling is enabled and
jobs advance automatically. Do not call the internal poll route manually. Cloud Run continues
to use Cloud Tasks and its configured internal authentication.

## Failure rules

- Status retrieval retries network failures, HTTP 408/429, and provider 5xx responses.
- A paid generation is never resubmitted unless the provider definitively rejected submission.
- `SUBMISSION_OUTCOME_UNKNOWN` is terminal and requires operator reconciliation.
- The default deadline is two hours, followed by one final provider status request.

## Request schema version

Absence of `request_schema_version` is V1. `2` selects `GenerationJobCreateV2`. Any other
value is `422 UNSUPPORTED_REQUEST_SCHEMA`. V1 `GenerationJobCreate` is frozen; do not add
fields to it. V2 hashes as `gj2:` plus SHA-256 of the canonical dump and sorted upload
triples. Changing that string is a schema-version bump.

V2 submissions are accepted for models with a V2 route (Grok, Seedance 2.0/fast,
and Veo). Gemini Omni Flash has no V2 route. Seedance 2.5 has no V2 route.
Seedance 2.5 uses ModelArk with BYTEDANCE_API_KEY on the V1 durable-job route.
Its media_inputs accept image, video and audio references. No LAS key is needed.

Each job row stores `request_schema_version`, `provider_route`, and `adapter_revision`.
Retrieve and content dispatch on the persisted `provider_route`, never on the current
model alias. In-flight V1 Veo jobs keep `vertex_litellm_video`. Live V2 Veo uses the
same LiteLLM Vertex adapter after a V2-to-V1 translation so generation keeps working.
`vertex_veo_direct` (`predictLongRunning` with ADC, camelCase parameters, and
`VEO_OUTPUT_GCS_PREFIX`) is implemented and unit-tested; it is not the live Veo
route. Apply a GCS object lifecycle of 30 days or less (ADR-001) and
grant the Gateway service account `storage.objects.create` and `storage.objects.get` on
that prefix before pointing any job at `vertex_veo_direct`. Legacy price environment
variables are not billing evidence; verified registry profiles control admission.

## Provider routes

| Route | Used for |
|---|---|
| `xai_videos_v1` | V1 Grok jobs |
| `xai_videos_v2` | V2 Grok jobs |
| `byteplus_ark_v3` | Seedance 2.0, Fast and 2.5 (shared ModelArk key) |
| `vertex_litellm_video` | V1 and live V2 Veo via LiteLLM |
| `vertex_veo_direct` | Implemented Vertex `predictLongRunning` adapter; not the live Veo route |
| `vertex_omni_interactions` | Gemini Omni Flash |


## Grok Video 1.5 registered V2 profiles

The local September 10 correction validates contract revision
`video-contract-v2-2026-09-03` before a job is created and again before provider
submission. `grok_video_contract.py` defines the three registered profiles:
`generate.text`, `generate.first_frame`, and `generate.references`. It rejects
unknown settings, slot roles, noncontiguous indices, invalid counts and unapproved
preset voice IDs. Reference images or preset voices cap output at 720p; text and
first-frame requests without voices also permit 1080p. The registered contract
fixes generated audio on and produces one video, for an integer 1–15 seconds.

New matching jobs record adapter revision `xai_grok15_v2@2026-09-10` and route
`xai_videos_v2`. The provider request explicitly includes `generate_audio` and
keeps first-frame inheritance, reference order and preset voices. No model
fallback is used. Idempotency hashing, owner-scoped retrieval and retained
provider identities are unchanged.

`tests/fixtures/generation_jobs_v2/grok_video_15_profiles.json` is the literal
MagicLens compiler output for these profiles; `tests/test_grok_video_v2.py`
checks the exact provider request and rejects invalid requests without network
calls. The combined Grok/Seedance correction was deployed to dev September 10 from
commit `fbf6dff0e9ccf4071f3d320becd1b850b3a9aeaf`, revision
`ai-gateway-proxy-video-fbf6dff`, image digest
`sha256:d2cbb5f421aa476570c7be219fecc5b77ee90a584255d1704f6ec36f84e4f66c`.
Candidate and post-promotion checks verified exact image/configuration, sole
traffic, health, aliases and invalid-contract rejection. Paid output evidence
is separate; this release does not deploy MagicLens application changes.

## Seedance 2.0 registered V2 profiles

`seedance_video_contract.py` validates contract revision
`video-contract-v2-2026-09-03` for `seedance-2.0` and `seedance-2.0-fast` before
job creation. A native V2 adapter emits the exact ModelArk body for text, first
frame, ordered reference images and source-video editing. New matching jobs
persist adapter revision `byteplus_seedance20_v2@2026-09-10`; their durable route
remains `byteplus_ark_v3`. Seedance 2.5 is unchanged.

Editing uses the provider-documented `reference_video` role with prompt-directed
edit intent. The source is safely downloaded and probed before paid submission.
The adapter sends adaptive aspect ratio and the verified source resolution tier,
instead of defaulting to square 480p. Source videos must be readable, 2–15 seconds,
and match a known standard or BytePlus output grid. Unknown frame grids and Fast
1080p sources are rejected with actionable errors; no output tier is substituted.

Generation settings preserve the registered 4–15 second range and audio boolean.
Fast and image-reference profiles are limited to 480p/720p; standard text and
first-frame profiles also permit 1080p. Billing admission additionally requires a
verified registry profile for the exact served options and actual usage. Public
rates alone do not complete that acceptance check. Customer quotes remain owned
by MagicLens's existing registry.

`tests/fixtures/generation_jobs_v2/seedance_20_profiles.json` matches the MagicLens
compiler goldens for all eight model/profile combinations.
`tests/test_seedance_video_v2.py` verifies provider requests, rejection, source
format preservation, retry identity, polling and cost. The combined September 10
Gateway revision above is deployed and verified. Paid output checks are tracked
separately in the MagicLens video capability contract.

Official sources rechecked September 10, 2026:
- https://docs.byteplus.com/en/docs/ModelArk/2291680
- https://docs.byteplus.com/en/docs/ModelArk/1520757
- https://docs.byteplus.com/en/docs/ModelArk/1544106
# Accounting contract update

Job responses now include `accounting_id`, `cost_status`, `cost_source`,
`pricing_version`, `breakdown` and `billing_eligible`. The existing `cost_usd` field
is populated only from committed journal accounting. Legacy stored estimates do
not establish a verified amount. Use `GET /v1/costs/{accounting_id}` with the
originating key for the exact decimal cost and per-attempt evidence.

**Every provider execution is logged regardless of attribution.** Spend completion
means its execution row and journal are persisted; daily aggregates and budgets
have a separate retryable projection status. A pending/unknown cost remains a
visible null amount. Older retained jobs missing logs are imported as unverified
evidence, without repricing history or adjusting customer charges. Repeated polls/callbacks do not charge twice.
Existing owner hashes stay compatible while billing uses the canonical LiteLLM
key hash. Unknown provider outcomes are never automatically resubmitted.
See [cost accounting](cost-accounting.md) for pricing and historical-repair policy.
