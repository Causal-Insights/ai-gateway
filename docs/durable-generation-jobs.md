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
generation. `extend` is available only on legacy `grok-video`. Gemini Omni accepts text,
one first frame, reference images, a first frame plus references, one source video for editing,
or `previous_job_id` for a stateful edit. A previous job must belong to the same key owner and
must be a completed Omni job. Source uploads must be MP4 so the gateway can enforce the
10-second input limit. Omni output is a 720p MP4 with its generated soundtrack embedded.

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
