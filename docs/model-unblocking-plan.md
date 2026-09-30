# Use LiteLLM pricing and restore model availability

Revised September 16, 2026 after testing the actual LiteLLM 1.95.0 price resolver
against every configured alias. This supersedes the earlier 50-model verification
exercise. The revised admission policy is implemented. All 56 configured aliases have
enabled pricing profiles, including Seedance 2.5 on ModelArk with the shared key.
Deployment status is recorded separately in the validation record.

## What the resolver actually found

The [SDK audit](../pricing/evidence/litellm-resolver-audit-2026-09-16.json) used the
captured current LiteLLM catalog in the pinned image, with no network or provider
generation calls. It called `get_model_info`, preserving upstream identity and
mapping custom xAI prefixes to `xai/` where needed. It found positive pricing
fields for 44 of the 56 configured aliases, including **43 of the 50 new blocks**.
The four already accepted Omni aliases use their existing custom pricing rule;
the exact preview identifier did not resolve in this catalog. Keep that working
rule rather than substituting another model's price.

| Newly blocked aliases | Count | Price source / action |
| --- | ---: | --- |
| OpenAI text/images; Gemini text; Nano Banana; Imagen; Veo; xAI text/images/video; ElevenLabs TTS | 43 | Use LiteLLM's existing model pricing and provider-specific cost calculation. Correct alias mappings where necessary. |
| Seedance 2.0 and Fast | 2 | Use the separately verified rates already collected; wire measured usage into accounting. |
| Seedream 5.0, Lite and Pro | 3 | Fill the custom-provider pricing gaps from applicable vendor sources. |
| ElevenLabs SFX and Music | 2 | Fill the missing endpoint rates and resolve the billing-unit discrepancy. |
| **Total** | **50** | **45 already have a pricing source; five need exception work.** |

These are price-resolution results, not claims that all routes/options or spend
writes have passed end-to-end tests. The audit distinguishes custom-provider
mapping from resolution of the configured identifier without mapping.

## Implementation

1. **Use LiteLLM as the default pricing engine.** Resolve the configured model to
   its actual provider/model and use LiteLLM's maintained rates and cost functions.
   Record the catalog version/hash and usage with each cost. Keep provider-reported
   monetary charges preferred where available. The journal records costs; it must
   not become a second manual approval system for models LiteLLM supports.
2. **Remove the blanket verification gate for supported models.** Do not require a
   second vendor audit, a per-alias paid test, invoice access or a manual review
   deadline before using an otherwise supported LiteLLM price. Retain only checks
   with a specific reason: unresolved pricing, a demonstrated mismatch, missing
   metering, a known expired rate or a broken accounting path. Apply an option's
   restriction to that option, not all requests to the model.
3. **Restore the 43 catalog-priced aliases, then the two Seedance aliases.** Fix
   provider mappings and feed actual usage into the existing calculation paths.
   Confirm the resulting spend reaches detailed logs and budgets, including final
   streaming usage and background jobs. Reuse shared-path tests; do not invent a
   separate acceptance project for every alias.
4. **Handle the five pricing exceptions directly.** Add supported custom pricing
   for `seedream-5.0`, `seedream-5.0-lite`, `seedream-5.0-pro`, `elevenlabs-sfx` and
   `elevenlabs-music`. Use the same accounting interface. An unresolved exception
   must have its specific reason and next fix recorded; it must not hold up the
   other models.
5. **Keep the original logging repair focused.** Preserve authenticated attribution,
   stable attempt IDs, awaited persistence, idempotency and background-job recovery.
   Unknown cost stays null and excluded from settlement. Do not duplicate native
   and custom spend writers. Leave customer pricing and settled charges unchanged.
6. **Test and prepare the Cloud deployment.** Test representative shared native,
   streaming and custom/media paths in the pinned image with isolated PostgreSQL;
   compare usage, cost, logs, aggregates and budgets. Validate migration/cutover on
   the clone and no-traffic candidate. Publish the exact remaining exceptions before
   promotion. Historical repairs and invoice reconciliation proceed separately.

Update onboarding, coverage and the gateway-specific Price Verification policy to
reflect this approach. The skill remains useful for discrepancies, new custom
providers and audits; it is not an admission requirement for every catalog model.
Refresh and validate its personal copy when its repository source changes.

## Target

Restore all 50 newly blocked aliases. Keep the existing five accepted aliases
working and preserve the earlier intentional `seedance-2.5` disablement. Do not
claim completion by removing models or silently substituting upstreams. Every
remaining block must correspond to a concrete pricing or accounting problem.

References: [current alias list](pricing-coverage.md),
[validation record](cost-accounting-validation.md),
[LiteLLM spend tracking](https://docs.litellm.ai/docs/proxy/cost_tracking), and
[LiteLLM custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing).
