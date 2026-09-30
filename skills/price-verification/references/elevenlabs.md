# ElevenLabs

Sources: [ElevenAPI pricing](https://elevenlabs.io/pricing/api),
[subscription information](https://elevenlabs.io/docs/api-reference/user/subscription/get),
[API reference](https://elevenlabs.io/docs/api-reference), and actual response headers
and usage records for the endpoint. Consumer website prices are a different product.

1. Resolve exact API model (`eleven_v3`, multilingual, SFX or music), endpoint and
   applicable account tier. Identify whether a supplied voice introduces another
   charge. Do not infer API billing from a consumer subscription balance.
2. Open the relevant API pricing table and footnotes. Verify whether the endpoint
   bills characters, seconds/minutes, generations or another unit, including any
   minimum, rounding and concurrency/tier condition.
3. Cross-check units against endpoint documentation and authorized usage evidence.
   If a table says per-minute but the metering FAQ says per-generation, do not
   choose whichever formula matches the existing code; resolve the discrepancy
   using account usage/support evidence or report unverified.
4. Extract actual billable usage or explicit monetary charge. A request ID allows
   correlation but is not itself a charge. Measure output audio only when official
   billing rules define delivered duration as the billable quantity. Requested
   duration, bytes and estimated tokens cannot replace missing billable usage.
5. Apply the project's policy for credit products. ai-gateway uses posted API usage
   prices rather than subscription-fee allocation. Preserve provider-reported
   usage charges separately and retain the original billing unit.
6. Calculate with Decimal; compare configuration, gateway and LiteLLM records and
   applicable account evidence. Subscription totals include effects unrelated to
   the request, so reconcile only supported periods and components.
