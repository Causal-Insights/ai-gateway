# MiniMax hosted API verification

Resolve the exact hosted model and endpoint first. H3 uses `MiniMax-H3` on
`https://api.minimax.io/v2/video_generation` with a pay-as-you-go
`MINIMAX_API_KEY`. Hailuo, H3 Max, subscription keys, third-party hosting, and
self-hosted weights do not establish H3 API rates.

Open the [posted API prices](https://platform.minimax.io/docs/guides/pricing-paygo),
[create schema](https://platform.minimax.io/docs/api-reference/video-generation-v2-create),
and [task usage schema](https://platform.minimax.io/docs/api-reference/video-generation-v2-query).
Check output resolution, input video seconds, the image allowance, audio input,
and separate Context-IR/regeneration charges. Record footnotes and effective-date
uncertainty. Do not apply H3 Max's input-material rates to H3.

Use task `usage.output_seconds`, `input_seconds`, and `input_image_count`.
Subtract the free image allowance once. `total_seconds` includes input and output;
token counters are converted usage and must not be billed again. Do not substitute
requested duration or local media duration for metered quantities. Preserve numeric
audio usage although its posted input rate is zero. A zero component does not imply
the entire generation is free.

There is no documented monetary charge field. Complete metering permits a
posted-rate calculation; absent/partial usage, including failures, stays unresolved.
Capture charged failures if metering is returned. Do not infer refunds or uncharged
failures from empty usage. Account invoice reconciliation remains separate.

Store per-resolution evidence reports and a decimal example. Label synthetic
calculations honestly; public price verification is not a live acceptance test.
The H3 query window is seven days; download outputs and reconcile evidence promptly.
