# Paid staging matrix

Release candidate: `8635ecdb92d371f3047e56ab8574f7aa5c82badc`

Immutable application image:
`us-central1-docker.pkg.dev/ai-gateway-495414/ai-gateway/litellm-proxy@sha256:d9a7ff3e4cd53f37d22c0dd6136dd051435691adbb48d808828a2a70956a7716`

Status values are `PASS`, `FAIL`, `BLOCKED`, and `PENDING`. Promotion to production
requires every required row to be `PASS` unless an explicitly unsupported workflow
is marked `BLOCKED` with its documented replacement.

## Release, database, and rollout gates

| Gate | Status | Evidence |
| --- | --- | --- |
| LiteLLM v1.95 unit/contract suite | PASS | 99 tests passed in the exact v1.95 application image; the two separately gated PostgreSQL tests were skipped in this run. |
| Durable-job PostgreSQL integration | PASS | 2 tests passed against local PostgreSQL. |
| Production backup | PASS | `gs://ai-gateway-495414-db-backups/pre-v1.95/ai-gateway-prod-pre-v195-20260810T203605Z.dump`; SHA-256 `15b7157728fe2bc4d1ca7d025b8ab6dbc0f43980d92f70db4c20441a898c7878`; 773 archive entries. |
| Restored-clone v1.95 migration | PASS | 23 migrations applied; verification-token, user, team, budget, proxy-model, organization, project, end-user, generation-job, and existing spend-table ID snapshots were unchanged; all foreign keys validated. |
| Production migration | PASS | Migration ran once from a tagged no-traffic, single-instance revision. Pre/post counts, sorted ID hashes, spend hashes, and all original foreign keys matched; the two expected new constraints were valid and the new daily-tool-spend table was empty. |
| Proxy candidate | PASS | Revision `ai-gateway-proxy-00050-zap` is Ready and pinned to the immutable digest above. |
| Callback service | PASS | Revision `ai-gateway-callbacks-00005-dum` is Ready and serves 100%; the application callback route returned the expected JSON and live BytePlus callbacks returned HTTP 202. |
| Staged production rollout | PASS | Candidate passed 1%, 10%, 50%, and 100% gates with all synthetic health requests returning 200 and no candidate error logs. |
| Final production state | PASS | Proxy revision `ai-gateway-proxy-00050-zap` serves 100%; 20/20 post-cutover health checks passed, required aliases were present, and the Cloud Tasks poll queue was empty. |
| Live spend parity | PASS | A transient Prisma `P2028` timeout missed the `$0.000789` virtual-key aggregate for the Studio Gemini test while preserving its immutable spend-log row. The exact aggregate was reconciled once from request `q0d6arC_NqXXqMgPo_2P-QY`. A fresh budget-capped production-key probe then recorded `$0.0001665` in both the detailed ledger and key aggregate on the second scheduler poll. The temporary probe key was deleted. |

The previous production revision recorded the same `P2028` transaction-acquisition
failure three times and numerous Prisma connection-pool timeouts during the comparison
window. One connection-pool timeout also occurred while rapidly creating, using, and
deleting the first temporary audit key; the corrected probe waited for both accounting
writes before cleanup and completed without a subsequent error. This is a known database
pool baseline, not a regression introduced by this release, and should be hardened in a
separate operational change.

## Provider contract probes

| Provider/model and workflow | Status | Evidence / acceptance condition |
| --- | --- | --- |
| `gpt-5.6-sol-medium` chat | PASS | Exact alias, fixed medium reasoning, and non-empty output on the candidate; a conflicting high request returned HTTP 400. |
| `gpt-5.6-terra-medium` chat | PASS | Exact alias, fixed medium reasoning, and non-empty output. |
| `gpt-5.6-luna-medium` chat | PASS | Exact alias, fixed medium reasoning, and non-empty output. |
| `gpt-5.6-luna-high` Responses | PASS | Exact alias, fixed high reasoning, and non-empty Responses output. |
| `gemini-3.6-flash` text/stream/tools | PASS | Vertex global text response, streaming completion, and forced tool call succeeded. |
| `gemini-3.5-flash-lite` text/stream/tools | PASS | Vertex global text response, streaming completion, and forced tool call succeeded. |
| `gemini-3.5-flash` existing text alias | PASS | Candidate and 50% production canary returned successful responses through the deployed Cloud Run service account. |
| Gemini Omni text-to-video | PASS | Job `gen_5553fa840f044a1da3c14c0b6439ce27`; audiovisual MP4 retrieved; `$0.3061485`. |
| Gemini Omni first-frame-to-video | PASS | Job `gen_d1538fb753ba46cfb7bbd5220b419d87`; completed with the first-frame role preserved; `$0.3092235`. |
| Gemini Omni reference-images-to-video | PASS | Job `gen_8f40fa1dcd8d4de2ab73e0306b50b26c`; completed with reference images; `$0.3087015`. |
| Gemini Omni first-frame plus references | PASS | Job `gen_8b1b16af00a547c8b5152d002e3d67c9`; completed with both image roles; `$0.309477`. |
| Gemini Omni source-video edit | PASS | Job `gen_07312f69aeeb41b7a73e9c98d0fc4dec`; completed from one source video; `$0.334998`. |
| Gemini Omni previous-job iterative edit | PASS | Job `gen_6cd84cfc0518466a80642d0f0779189a`; same-owner prior interaction completed and produced a new MP4; `$0.3071385`. |
| Grok Video 1.5 text-to-video | PASS | Job `gen_931f9b59b0e743cd874ee35d682ae2ec`; exact `grok-imagine-video-1.5`; retrievable MP4; `$0.08`. |
| Grok Video 1.5 image-to-video | PASS | Job `gen_2e0abc38cf164d49a1856c9401a7e5cb`; exact 1.5 model; retrievable MP4; `$0.09`. |
| Grok Video 1.5 reference-images-to-video | PASS | Job `gen_49b6a709221a4084a42693e118584dc0`; exact 1.5 model; retrievable MP4; `$0.09`. |
| Grok Video 1.5 preset voices | PASS | Job `gen_b6c5e78887d6469d9794ad05684f27f0`; preset `eve`; MP4 contained video and audio tracks; `$0.24`. |
| Grok Video 1.5 edit/extension | BLOCKED | Intentionally rejected because xAI exposes edit/extension on the explicit legacy `grok-video` family. There is no silent upstream fallback. |
| Legacy `grok-video` edit/extension | PASS | Edit job `gen_fc27a11b11614185b021409603e7e545` (`$0.06`) and extension job `gen_04bd91acee3c465789d8e9897c2c8e01` (`$0.17`) completed with retrievable MP4s. |
| `grok-imagine-image-quality` generation/edit | PASS | Generation, single-image edit, and two-image compositing returned JPEGs. Spend logs recorded `$0.05`, `$0.06`, and `$0.07`; the gateway accepted uppercase client resolution values and sent xAI's lowercase enum. |
| Seedance existing alias | PASS | `seedance-2.0-fast` job `gen_80f5382f7c3946be93a8a287f6d3b93e`; callback and poll paths completed; retrievable MP4; `$0.2273264`. |
| Seedream existing alias | PASS | `seedream-5.0-lite` returned a retrievable 2048x2048 JPEG; spend log `$0.035`. |
| Veo existing alias | PASS | `veo-3.1-fast` job `gen_cc1f2bee5c494b3ca2594ad27719e60b`; retrievable MP4. LiteLLM recorded `$0.00`, matching all pre-upgrade durable Veo records and therefore not a rollout regression. |

## Consumer verification

| Consumer workflow | Status | Evidence |
| --- | --- | --- |
| Studio Pro with `gemini-3.5-flash` | PASS | A connected Text → AI Model workflow returned exactly `studio-gemini-ok`; the production spend log recorded a successful `vertex_ai/gemini-3.5-flash` request costing `$0.000789`. |
| Studio Pro with `grok-video-1.5` | PASS | A connected Text → Video Gen workflow completed as job `gen_997bb1d5172444dcbae5178d03c32a83`; the in-app player reached ready state 4 with a 1280x720, 4.041667-second video; gateway cost `$0.56`. Studio's initial `operation:auto` capability probe was rejected without spend, then its explicit `operation:generate` retry succeeded. No Magic Lens code changes were made. |
