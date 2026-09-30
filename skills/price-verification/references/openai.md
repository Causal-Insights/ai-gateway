# OpenAI

Start with [API pricing](https://developers.openai.com/api/docs/pricing), the
exact model page linked from the [model catalog](https://developers.openai.com/api/docs/models),
[image usage](https://developers.openai.com/api/docs/guides/image-generation),
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), and
[Usage and Costs APIs](https://developers.openai.com/cookbook/examples/completions_usage_api).

1. Resolve gateway alias, snapshot, direct OpenAI versus another hosting product,
   region and requested/served service tier. An alias can change underneath a
   stable gateway name; retain the served model identifier.
2. Open the relevant pricing tab and footnotes. Verify per-million denominators,
   long-context thresholds and whether thresholds apply to the whole request.
   Verify cache reads/writes, data-residency uplifts and promotional end dates.
3. Inspect actual Responses/Chat usage. Reasoning commonly belongs to output totals;
   do not add it again. Subtract cached categories from ordinary input only when
   the response schema says they are included. Missing cache breakdowns are not zero.
4. For images, require text/image input and cache modality breakdowns when their
   rates differ. Count partial frames according to provider metering, not both
   output-token totals and an extra per-frame estimate. Check image-tool usage
   independently from parent-model usage and prove that the two do not overlap.
5. Use Decimal(quantity) × rate / unit denominator for each disjoint component.
   Compare with the configured profile and request spend. Report unsupported hosted
   tool charges as unverified rather than returning a tokens-only total.
6. With authorized admin billing access, compare Usage and Costs data at supported
   project/organization/line-item/time granularity. A normal inference key does not
   imply Costs API permission. Report agreement, mismatch or missing evidence.

No prices in this procedure are persistent rate authority. Reopen current sources
and record retrieved/effective dates for each verification.
