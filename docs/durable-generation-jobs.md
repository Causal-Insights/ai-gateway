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
