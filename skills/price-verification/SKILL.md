---
name: price-verification
description: Verify AI model API prices against official vendor sources and actual usage. Use for model onboarding, price changes, billing discrepancies, and pricing audits; produce attributable evidence and proposed rate changes.
---

# Price Verification

Resolve the requested alias to its exact provider, API product, upstream model or
snapshot, account, region and route before comparing prices. Read the applicable
vendor reference below. For ai-gateway also read [gateway policy](references/gateway-policy.md).

| Vendor | Procedure |
| --- | --- |
| OpenAI | [OpenAI](references/openai.md) |
| Google / Vertex AI | [Google](references/google.md) |
| xAI | [xAI](references/xai.md) |
| BytePlus ModelArk / LAS | [BytePlus](references/byteplus.md) |
| ElevenLabs | [ElevenLabs](references/elevenlabs.md) |

Open current official pricing tables and their footnotes. A search snippet, an old
skill value, a neighboring model, or a third-party calculator cannot verify a rate.
Use authorized browser/account access when rendering or authentication is needed;
otherwise mark that evidence unavailable. Do not infer that public rates establish
the account's negotiated rates. Do not treat a retrieval date as a historical
effective date. Record an unknown effective date explicitly.

Check every applicable dimension: tokens, cache reads/writes, reasoning inclusion,
modality, quality/resolution, duration, input references, output count, service tier,
region, tools and charged failures. Record currency, unit denominator, minimums,
rounding and discounts. Use actual metered quantities; use requested quantities
only when official billing rules explicitly require them. Separate tax, credits,
subscription fees and retail markup from inference cost.

Reproduce a representative calculation with decimal arithmetic. Preserve the
provider-reported charge separately from the calculated amount and compare both
with configured rates, detailed logs and provider evidence. Aggregate billing
exports prove only the attributable account/project/SKU/time bucket they describe.
Do not manufacture request-level invoice precision. For tool calls establish what
is already included in parent usage before adding any additional charge.

Produce JSON conforming to [the evidence schema](references/evidence.schema.json)
and a short readable report. Use `verified` only when identity, applicable rate,
units and usage semantics are supported; `mismatch` when supported evidence
disagrees with configuration; `unverified` when evidence is insufficient or
contradictory. Keep verification of public rates separate from production
acceptance and account applicability. Unknown cost is null; zero needs explicit
evidence. Retain numeric evidence, source URLs and short excerpts, not prompts,
generated content, credentials or raw API keys.

Default deliverables are the evidence report and proposed registry changes.
Invoking this skill alone does not authorize deployments, customer-billing changes
or paid generation. Follow the scope already authorized in the conversation.
For a new vendor, add an official-source reference and verification procedure
before claiming its models are verified.

When changing this skill, replay the [verification scenarios](references/validation-scenarios.md)
and validate the evidence schema and skill metadata before refreshing installed copies.
