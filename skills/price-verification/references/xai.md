# xAI

Sources: [pricing](https://docs.x.ai/developers/pricing),
[cost tracking](https://docs.x.ai/developers/cost-tracking),
[models](https://docs.x.ai/developers/models), and the exact API response schema.

1. Resolve alias and actual served snapshot; distinguish xAI direct from hosted
   products. Confirm regional endpoint and service tier.
2. Open pricing, model and cost-tracking docs. Confirm token/context/cache rates,
   image quality/resolution, video duration/resolution, input images/video/audio,
   tools and charged moderation failures. Do not assume every failed request is free.
3. Verify the units and scope of `usage.cost_in_usd_ticks` in the current schema.
   The cost-tracking documentation describes this as actual account charge,
   including discounts and tools. Reconfirm the ticks-per-dollar denominator;
   do not infer units from an SDK convenience property or the field name alone.
4. Extract the final reported total. For REST chat streaming request final usage;
   running totals must not be summed. Preserve integer ticks and divide with Decimal.
5. Preserve a separate rate-based comparison when all required measured inputs
   exist. Never add tool or image charges again to an inclusive reported total.
   Missing ticks may use a fallback only if its exact profile and all units are
   independently verified. Requested output duration alone is insufficient.
6. Compare reported charge, calculation, gateway record and available account
   evidence. Report discrepancies without overwriting the reported amount.
   A zero reported charge must be recorded as an explicit provider-reported zero.
