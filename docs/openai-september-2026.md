# OpenAI September 2026 Gateway release

This release adds Gateway routes. MagicLens registration, controls, pricing and
publication have a separate lifecycle; Gateway access alone does not establish
MagicLens feature readiness. See the [integration review](magiclens-integration-2026-09-24.md).

| Gateway alias | Exact upstream model | Routes |
|---|---|---|
| `gpt-image-2.5-sunburst` | `openai/gpt-image-2.5-sunburst-2026-09-08` | Images generations and edits; Astra Responses image tool |
| `gpt-image-2.5-flare` | `openai/gpt-image-2.5-flare-2026-09-08` | Images generations and edits; Astra Responses image tool |
| `gpt-6-astra` | `openai/gpt-6-astra` | Responses and Chat Completions |

`gpt-latest` already pointed to Astra before this change. It receives the same
Astra validation and retry policy. Gateway discovery confirms routing, not
upstream project entitlement. On 2026-09-09, read-only OpenAI model lookups using
the Gateway's existing credential returned `model_not_found` for both image
aliases and snapshots. No paid image-generation probe was submitted at that time.

On 2026-09-24, both aliases and exact snapshots returned HTTP 200 using the
Gateway-configured OpenAI credential. One direct Image API generation per
snapshot also succeeded (1024×1024, low quality, one image). This supersedes
the access restriction above. The test verifies provider access and basic
generation, not deployed Gateway dispatch, editing, streaming, or MagicLens
execution. Sanitized request IDs and usage are in the
[access evidence](evidence/gpt-image-2.5-access-2026-09-24.json).

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

The September 24 capability rollout adds finite Images SSE handling around the
pinned LiteLLM 1.102.1 runtime, preserving partial images, completed images and
terminal usage. Deterministic SDK contract tests cover generations and edits.
A deployed Flare masked edit succeeded; GPT Image 2.5 upstream streaming has not
been live verified. Responses image-tool streaming remains available separately.
See the [current gap table](capability-rollout-gap-table-2026-09-24.md) for the
release and Magic Lens acceptance status.

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

The versioned pricing registry now owns token rates and service-tier/context
conditions; `openai_usage.py` delegates compatibility calculations to it.
Accounting partitions ordinary input, cache reads, cache writes and output using
actual usage. Reasoning output is already included in the output-token total.
Served model and service tier must match the accepted profile.

Image accounting separates text input, cached text input, image input, cached
image input and output. Missing modality breakdowns remain unresolved. Hosted
image-tool billing requires separate verification of parent and tool usage to
prevent double counting. Unsupported tool types are rejected; supported outputs remain available when
provider tool usage is insufficient to reconcile the total charge. See [pricing coverage](pricing-coverage.md)
and the [accounting guide](cost-accounting.md) for the current release state.

## Verification

Run unit tests in the pinned application image with networking disabled. The
wire tests mock the HTTP client beneath both the OpenAI SDK and native LiteLLM
adapters, assert exact upstream bodies, usage preservation, and error/no-retry
behavior. The remaining database integration tests require an isolated database.
They are required before promoting the accounting candidate; this document's earlier capability evidence does not establish accounting acceptance.

Source evidence, accessed 2026-09-09:

- [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Sunburst model](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)
- [Flare model](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
- [Image generation](https://developers.openai.com/api/docs/guides/image-generation)
- [Prompt caching and usage accounting](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Pricing](https://developers.openai.com/api/docs/pricing)
