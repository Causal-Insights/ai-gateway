# Google / Vertex AI

Sources: [generative AI prices](https://cloud.google.com/vertex-ai/generative-ai/pricing),
[account price table](https://docs.cloud.google.com/billing/docs/how-to/pricing-table),
[billing exports](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery),
[Vertex model documentation](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models),
and the exact endpoint's usage schema. Follow official redirects but confirm the
product and SKU. Gemini Developer API prices are not Vertex account prices.

1. Resolve alias to actual Vertex model, project/account, endpoint, region and SKU.
   Identify on-demand, batch, provisioned throughput or another purchasing mode.
2. Open the model table and footnotes. Verify input/output modality rates, caching,
   context tiers, resolution, generated audio, grounding and other paid tools.
3. Inspect the account price table when authorized. Record account applicability
   without copying billing identifiers or credentials into public reports. Contract
   rates, promotional credits and invoice adjustments are separate concepts.
   Authorized Cloud Billing Pricing API access can list account services/SKUs and
   retrieve each SKU's contract price. Resolve the exact service and SKU instead
   of guessing an ID. Compare `contractPrice`, `listPrice`, `unitQuantity`, tiers
   and the price reason. API field/filter spelling can differ by version: validate
   against that endpoint's current documentation and error schema. Store sanitized
   numeric evidence without the billing account identifier or access token.
4. Use usageMetadata or the actual Interactions usage schema. Establish whether
   thoughts belong to or are separate from output totals; reconcile modality sums.
   For Omni retain input, output-by-modality and thought counts. Do not convert a
   requested duration to tokens when actual token usage is available.
5. For Veo confirm what the displayed billing unit means using the SKU and endpoint
   docs, and confirm resolution/audio/output duration actually served. A table that
   says only “count” does not establish “per second.” Missing units stay unverified.
6. Calculate disjoint components in Decimal, compare gateway and LiteLLM evidence,
   then reconcile attributable SKU/project/day totals to billing exports. Record
   currency conversion, credits, adjustments and late export arrival separately.
   Classify agreement, mismatch or insufficient evidence.
