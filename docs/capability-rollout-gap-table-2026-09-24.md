# Capability rollout evidence and remaining gaps

This table tracks the September 24 implementation campaign. "Fixture tested"
means deterministic execution with provider fixtures and the pinned LiteLLM 1.102.1
runtime; it does not mean every upstream option was exercised live. Cloud
production includes all six releases; Gateway deployment acceptance is complete. Magic Lens changes remain local or staged, without a cloud deployment.
The gitignored campaign ledger/gallery records exact revisions, charges and bytes.

| Exact scope | Implementation / fixtures | Live evidence | Remaining limit or acceptance |
| --- | --- | --- | --- |
| Retired Imagen aliases and Gemini 3.1 Flash-Lite Preview | Removed from active Gateway/ML selections; historical identities retained | Production discovery checked | Saved history remains intentionally readable |
| Seedance 2.0 / Fast / 2.5 | Successor V2 ordered references, source edits/extension, automatic duration; 2.0 standard 4K; 2.5 draft/final/MOV/last-frame controls | 2.5 source edit completed, downloaded, $0.4736 priced | Draft/final and other combinations fixture-tested only; final is a separate paid action |
| Veo 3.1 / Fast / Lite | First/last, references where supported, extension, 1–4 outputs; seven new seconds billed for extension | Regional candidate completed a 4-second first/last-frame video, downloaded and $0.12 priced; initial wrong-location attempt retains its reservation | Lite has no asset-reference mode |
| Grok Video 1.5 exact aliases | Last-frame, first+last, timestamped keyframes and first+references; explicit audio preserved | No new paid probe | Edit/extension not claimed without exact upstream support |
| Gemini Omni 1.1 exact aliases | Expanded inputs, owned continuation, provider-managed audio; native text/stream/count-tokens implementation | 3s audible video completed/downloaded, $0.1031985 priced; two text attempts rejected (initial Gateway HTTP 500; corrected provider HTTP 400 invalid argument); error/incomplete lifecycle fixtures passed | Standalone text **unverified pending provider clarification**, not established as unsupported. [Official contract evidence](provider-implementation-reference.md#provider-google) supports text and the current SDK format but yields no verified fix; Magic Lens `gemini-omni-flash-text` remains disabled. No further paid permutations. Standalone audio input and grounding unsupported |
| GPT Image 1.5 / 2 / 2.5 | Edit fields/masks retained, finite native Images SSE with partial/final accounting | 2.5 Flare masked edit completed/downloaded, $0.018223 priced | Upstream 2.5 streaming not live verified; provider documentation remains inconsistent |
| Seedream 5.0 / Lite | Ordered references/edits and buffered provider streaming parsed with terminal usage | Two Lite stream outputs downloaded; $.035 each, corrected candidate priced normally. Signed-in Studio Pro 2K square run through localhost:4000 passed save/reopen, output history and downloads; $0.035 provider / 1 credit | Incremental preview is not claimed for buffered custom-provider delivery; search billing evidence remains insufficient |
| Seedream 5.0 Pro | Layers, edits, transparency and named presets; every layer/result retained; actual size-tier accounting | No new paid layers probe | No Pro streaming/sequential/search promised; layer billing needs actual dimensions |
| Grok Image exact aliases | Automatic quality and wide ratio normalization preserve explicit choices | Deterministic only | No new paid probe |
| Gemini image video conditioning | Eligible Nano Banana / Nano Banana 2 / Lite use SDK file content with original signed video URL | Deterministic actual SDK mapping | Pro excluded; no live video-conditioned image probe |
| Private ElevenLabs voices / IVC | Owned create/list/get/synthesize/delete, consent, verification and ambiguous-create recovery; curated UUIDs retained | Production authorization/ownership boundary checked | Live cloning/synthesis awaits authorized speaker sample |
| ElevenLabs speech/dialogue/stream | Timing, ordered speakers, formats, bounded WS sessions, partial/final persistence and browser incremental playback | 1.625s speech with character alignment completed/downloaded; $0.0021 priced. Signed-in Runner Voice-over through localhost:4000 completed, played through, reopened and downloaded; $0.0023 provider / 5 quoted credits | Account-dependent voice/format access; unsupported browser previews fall back to completed audio |
| ElevenLabs music_v1 and music_v2_5 | Separate aliases, structured plans/chunks, owned imports/song resources/successor edits/extension | 3.030188s Music 2.5 completed/downloaded; $0.007575 priced on existing account | Existing account access required; no subscription upgrade; provider-wide song deletion unsupported |
| Astra hosted tools / owned resources | Finite search/file/code/image tools, continuation, files/vector stores/containers; outputs retained with unknown costs | Short Astra Responses text completed/downloaded at $0.0005; hosted-tool execution remains deterministic only | Container duration/storage/image-tool attribution may remain unresolved; owner-wide export/deletion and late-create cleanup implemented; exhausted-key authorization fixture passed |
| Astra explicit batch | Per-item durable intents/results, cancellation/recovery, partial settlement and deduplication in Studio/Runner | Deterministic only | Exact Astra text endpoints; no hosted tools/streaming inside batch |
| Exact Gemini / xAI grounding | Supported controls, frozen tariffs, current X posts/profile usage; parent subtotal retained | Deterministic SDK + database settlement | Gemini SDK deduplicates queries: count is an estimate, total remains unresolved without authoritative billable count |
| GPT 5.6 flexible aliases | Sol/Terra/Luna aliases added alongside fixed-effort aliases; historical defaults retained | Deterministic only | Flexible ML aliases staged with exact effort/output-budget controls; final cloud discovery and ownership acceptance passed |
| Shared Gateway contract | Typed ML client, versioned exact-model discovery, retained snapshot fallback and financial/job transports | Local UI fixtures and historical boundaries exercised | Actual localhost:4000 upgraded; health and nine authenticated discovery/resource checks passed. Final ML gate passed (3,363 Node, 646 browser, 202 Canvas, typecheck and all builds); signed-in Studio image and Runner speech generations through localhost:4000 passed with durable outputs and exactly one settlement each |

No silent upstream substitution is permitted. Unknown costs are never converted
to zero merely to complete acceptance. Existing output bytes remain available
when accounting or optional verification needs reconciliation.

Campaign receipt terminology: live charges are provider-usage priced and applied
to Gateway projections. The campaign service key lacks full billing attribution;
these receipts are not invoice-reconciled or evidence of end-customer billing
settlement. The later signed-in Magic Lens image/speech runs also verified actual wallet holds and settlements; their provider receipts remain uninvoiced.

## Remaining gaps

- Standalone Omni text is implemented and fixture-tested but rejected by Vertex;
  its Magic Lens alias stays disabled pending provider clarification.
- Signed-in Studio image and Runner speech checks passed through localhost:4000.
  An initial Studio UI image used the retained port 4001 cloud forwarding setting;
  it is separately recorded. An initial speech attempt failed before provider dispatch
  because the launcher retained quotes around a URL with a trailing comment.
  The shared environment parser was fixed (3/3 focused tests), services restarted,
  and both local checks passed. The failed speech hold was fully refunded.
  Live IVC still awaits an authorized speaker sample.
- Hosted-tool attribution, authoritative Google grounding query counts and
  Seedream search billing remain incomplete. Usable outputs are preserved while
  costs require reconciliation.
- Exact upstream restrictions remain explicit, including Veo Lite asset
  references and unsupported Gemini Pro video conditioning. No silent model
  substitution was introduced.
- Most advanced combinations are fixture-tested, not individually live-tested.
  Representative live success verifies the tested settings only.
