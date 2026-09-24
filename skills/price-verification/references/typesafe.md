# TypeSafe System One

Sources: [models and pricing](https://docs.typesafe.ai/models),
[System One API and usage](https://docs.typesafe.ai/api).

1. Pin the Jev version and direct TypeSafe API endpoint. Do not infer the version
   from a moving latest/preview alias, or apply direct rates to another host.
2. Read the current model's input-token price and explicit output-token rule.
   Confirm currency and million/billion-token denominator. Check for documented
   cache, context, batch, region, or contract discounts rather than inventing them.
3. Preserve input_tokens and output_tokens from the provider response before
   validating its typed answers. Compute input_tokens * input_rate / 1,000,000
   with Decimal. Free output tokens still retain their measured count.
4. Malformed answers and interrupted requests are not evidence of free usage.
   Preserve known usage; leave missing usage unresolved with null cost.
5. Record exact served model, route, endpoint and retrieval date. A public price
   verifies a posted API rate, not a negotiated account rate. If the provider does
   not publish a historical effective date, use null in evidence and keep the
   Gateway profile's prospective applicability start separate.
6. Reconcile any available account evidence without changing settled charges.
   Do not send paid test calls just to populate a pricing report.
