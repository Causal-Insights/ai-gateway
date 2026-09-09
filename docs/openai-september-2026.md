# OpenAI September 2026 Gateway release

This release adds Gateway routes only. MagicLens registration, controls, pricing,
publication, migrations, and deployment are deferred at the user's request until
OpenAI grants image-model access to the configured project.

| Gateway alias | Exact upstream model | Routes |
|---|---|---|
| `gpt-image-2.5-sunburst` | `openai/gpt-image-2.5-sunburst-2026-09-08` | Images generations and edits; Astra Responses image tool |
| `gpt-image-2.5-flare` | `openai/gpt-image-2.5-flare-2026-09-08` | Images generations and edits; Astra Responses image tool |
| `gpt-6-astra` | `openai/gpt-6-astra` | Responses and Chat Completions |

`gpt-latest` already pointed to Astra before this change. It receives the same
Astra validation and retry policy. Gateway discovery confirms routing, not
upstream project entitlement. On 2026-09-09, read-only OpenAI model lookups using
the Gateway's existing credential returned `model_not_found` for both image
aliases and snapshots. No paid image-generation probe was submitted.

## Request contract

Astra accepts reasoning efforts `low`, `medium`, `high`, `xhigh`, and `max`,
defaulting to `medium`. Sampling/log-probability overrides are rejected. Both
ordinary text/image inputs and structured output use native LiteLLM dispatch.
Responses supports image-generation tools and streaming. Tool model aliases are
mapped to the exact snapshots above; other supported tools retain native routing.
Explicit `prompt_cache_options` survive the pinned LiteLLM parameter filters.

The Images routes accept qualities `auto`, `low`, `medium`, `high`, `xhigh`, and
`max`; PNG, JPEG, and WebP output; `auto`, `opaque`, or `transparent` backgrounds;
and JPEG/WebP compression from 0 through 100. Transparent JPEG and unverified
`input_fidelity` overrides are rejected. This Gateway bounds image counts to 1–4.
Dimensions must be multiples of 16, have maximum edge 3840, contain
655,360–8,294,400 pixels, and have aspect ratio at most 3:1, or use `auto` size.
Image edits preserve uploaded image order and masks through native multipart
handling. Local request validation also checks multipart scalar settings without
rewriting the uploaded bytes.

**Images-route streaming is explicitly unavailable in LiteLLM 1.95.0.** Such
requests fail before provider submission instead of silently dropping streaming
settings. Use the Responses image-generation tool when streaming is needed.
Provider access and live streaming/media verification remain outstanding.

```json
{
  "model": "gpt-6-astra",
  "input": "Draw a small green lighthouse.",
  "reasoning": {"effort": "medium"},
  "max_output_tokens": 4096,
  "max_tool_calls": 1,
  "tools": [{
    "type": "image_generation",
    "model": "gpt-image-2.5-flare",
    "quality": "low",
    "size": "1024x1024"
  }]
}
```

No application image is generated merely by installing or discovering an alias.
The examples require OpenAI project access and incur provider charges when used.
Both router retries and provider SDK retries default to zero for these aliases.
A caller must reconcile an ambiguous submission before attempting another call.

## Accounting

`openai_usage.py` owns the dated token-rate schedule and integrates with
LiteLLM's existing spend/header/log pipeline for these exact models only.
Astra partitions ordinary input, cache reads, cache writes, and output; requests
above 272,000 input tokens apply the long-context multipliers to the whole
request. Reasoning output is already included in the output-token total.
The served service tier determines its multiplier.

Image accounting separates text input, cached text input, image input, cached
image input, and output. Final output-token totals already include paid preview
frames. A Responses request adds separately reported image-tool usage to parent
usage exactly once. Missing or incomplete usage remains an unknown cost, rather
than a zero-cost success or an invented per-image price. Requests with other
hosted tools currently retain unknown total cost until all their charges can be
accounted for. Full billing/media proof awaits entitled provider access.

## Verification

Run unit tests in the pinned application image with networking disabled. The
wire tests mock the HTTP client beneath both the OpenAI SDK and native LiteLLM
adapters, assert exact upstream bodies, usage preservation, and error/no-retry
behavior. The remaining database integration tests require an isolated database.
They are not a production migration gate for this configuration-only release.

Source evidence, accessed 2026-09-09:

- [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Sunburst model](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)
- [Flare model](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
- [Image generation](https://developers.openai.com/api/docs/guides/image-generation)
- [Prompt caching and usage accounting](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Pricing](https://developers.openai.com/api/docs/pricing)
