# LiteLLM pricing and gateway verification

LiteLLM's maintained model catalog and provider calculators are the default
pricing source. All 56 configured aliases now have enabled price profiles; the
broad manual approval gate has been removed. Native routes use immutable captured
catalog entries. Custom media routes use documented provider rates. See
[coverage](pricing-coverage.md) and [onboarding](model-onboarding.md).

Keep three questions separate:

1. Does the exact model/product/tier have the correct rate and billing formula?
2. Does the response retain every needed usage component?
3. Is the attributed cost durably written to detailed logs, aggregates and budgets?

The original missing Omni/video spend involved attribution and persistence. A
correct model price alone would not repair those dropped database writes.

LiteLLM 1.95.0 fetches its maintained map on startup unless local-only mode is
configured. Failed retrieval or integrity validation falls back to its bundled
snapshot. The snapshot and live catalog can differ. See
[LiteLLM pricing documentation](https://docs.litellm.ai/docs/completion/token_usage)
and [spend tracking](https://docs.litellm.ai/docs/proxy/cost_tracking).

On September 16, the live catalog's compared token-rate fields across all 11
configured OpenAI text aliases (including Standard/Flex/Fast and applicable
long-context fields) agreed with
[OpenAI's current table](https://developers.openai.com/api/docs/pricing).
The pinned image's older Luna snapshot instead contained USD 1/6 per million
input/output tokens; the live catalog and vendor table contained USD 0.20/1.20.
The [captured comparison](../pricing/evidence/litellm-comparison-2026-09-16.json)
records its source hash and individual field results. It does not establish what
the production process actually loaded.

Use maintained LiteLLM rates by default; investigate demonstrated discrepancies. The registry adds
official evidence, effective dates, deployment applicability and reproducible
calculation versions. It should not duplicate manual research when an existing
rate agrees, and a catalog update must not silently reprice historical requests.

Run `scripts/compare_litellm_pricing.py CAPTURED_MAP.json --source SOURCE` against
an explicitly captured live map or the pinned image's bundled map. It emits JSON
with a source hash, exact-key matches and token-rate comparisons. Other modalities
remain marked for semantic review. Missing exact keys are not proof that LiteLLM's
provider-specific resolution cannot find a price. No comparison changes rates or
enables a model automatically.

Known corrections are explicit: ElevenLabs v2/v3 TTS uses $0.10 per 1,000
characters from the current API page; the pinned SDK's Astra cache-write tier and
GPT Image 2.5 cache partition calculations are corrected using the existing
vendor-backed component profiles. Veo silent-video rates are lower than the
catalog's audio-video rates and are selected by the actual audio option.
Hosted tool amounts remain separate from the
token correction. Unknown usage remains unresolved, never free.

Gemini image models accept image generation and reference-image editing through
`/chat/completions`. Their image pricing profiles must admit `completion` as well
as `image_generation` and `image_edit`; catalog mode alone does not identify the
HTTP route. Catalog refresh must retain that route support. The September 23
correction creates successor profile versions with unchanged captured rates and
retains the original profiles for pinned historical calculations. The regression
suite exercises every Nano Banana alias, refresh behavior, positive measured
costs, unchanged old prices, and rejection of chat on OpenAI image-only models.
Local tests do not establish that the deployed Gateway contains this correction.
