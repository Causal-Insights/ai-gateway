# Capability rollout implementation record

User authorized six staged Gateway releases on September 24, 2026. Gateway cloud
promotion/migrations/rollback are authorized; Magic Lens remains local. LiteLLM
stays pinned to 1.102.1. New paid generation spending is capped at $15 across all
agents, with reservations recorded before dispatch.

## Current state

- Releases 1–6 are deployed and promoted. Current Gateway revision
  `ai-gateway-proxy-00163-yuj`, callback `ai-gateway-callbacks-00035-hiy`,
  immutable digest `sha256:03ee857e77bf84d20767eec7a7f29e5eeaf3c9568dba13bcf60bf73b4ed8e183`.
  Each promotion verified health and an unchanged settled receipt at 1/10/50/100%.
- Production Prisma advanced 141→171 after restored-copy rehearsal. Historical
  IDs and spend counts were retained, including 48 unknown costs left NULL.
  Private-voice migration 005 was separately backed up and rehearsed twice;
  ten lifecycle/authorization tests passed against the restored schema.
- Fresh release 1 Gateway suite: 287 passed without skips. Magic Lens fresh unit
  baseline: 3,240 passed; initial browser failures were repaired/rechecked;
  Canvas 202 passed, affected builds/typecheck/documentation checks passed.
  Final integrated Magic Lens verification passed: 3,363 Node tests, 646 runnable
  browser cases (three initial failures resolved by focused reruns), 202 Canvas
  cases, typechecks, documentation and all consumer builds.
- Downloaded live outputs: two Seedream streams, GPT Image 2.5 Flare masked
  edit, Omni video with audio, Seedance source edit, and Veo first/last-frame
  video, timed speech and minimum-length music2.5. Astra text also completed at $0.0005. Priced total is $0.7951965; $0.16 remains reserved for unsuccessful
  attempts with unreconciled charges. The ledger conservatively rounds to micros.
  Seedance's fixture prompt described a square while its source was a boat:
  execution/bytes/accounting are verified, not semantic edit fidelity.
- Magic Lens additive migrations 209–218 are applied; retirement is effective,
  new capabilities remain staged for owner-bound local rehearsal.
  No Magic Lens cloud deployment has occurred.
- Release 6 is promoted after its candidate acceptance checks. Release 4 cumulative
  suite passed 356 tests; release 5 passed 377, with database tests enabled. Exact video/native text,
  Images SSE/layers, advanced audio and owned tools/batch have focused fixtures;
  those fixtures do not establish live provider acceptance. Corrected release 6
  cumulative suite passed 419 tests with all database classes enabled.
- Actual localhost:4000 now runs the corrected release 6 image on LiteLLM1.102.1;
  health and nine authenticated resource/discovery checks pass. Signed-in Studio image and Runner speech
  live checks passed through localhost:4000, including saved outputs and settlement.
  No authorized speaker sample exists, so live IVC is still conditional.
- The campaign ledger is authoritative for spending and outstanding commitments.

## Owners

- Gateway lead: adapters, pricing/accounting, Gateway interfaces and deployment.
- magiclens_model_integration (GPT-6 Astra High): Magic Lens text/image/video,
  registry/compiler/shared client, Studio and Runner implementation.
- magiclens_voice_resources (GPT-6 Astra High): private voices and advanced audio,
  focused tests, coordinated Gateway voice resource groundwork.
- magiclens_regression_lead (GPT-6 Astra High): independent regressions, serialized
  browser tests, budget/gallery collector, deployment helper corrections.

## Evidence

Gitignored campaign directory: `local-tests/capability-rollout-20260924/`.
`budget.json`, `baseline.json`, `evidence.py`, `index.html` and its README record
fresh tests, budget commitments and downloaded generations. No generated output
is claimed until present in that ledger/gallery.

## Remaining acceptance

Gateway release implementation, migration rehearsals, candidate checks and
promotion are complete. All eight representative cloud classes produced outputs.
Final Magic Lens Canvas/build checks passed. Signed-in Studio/Runner
generations through localhost:4000 now pass: Seedream Lite image ($0.035, 1 credit)
and ElevenLabs Multilingual v2 speech ($0.0023, 5 quoted credits), with save/reopen,
downloads and speech playback. A launcher parsing fix handles quoted URLs with
inline comments; 3 focused tests and documentation checks passed. The initial
pre-provider speech failure refunded its full hold. An extra initial Studio image
used the old cloud-forwarding process and is recorded separately ($0.035).

Preserve V1 hashes, saved defaults, curated voice UUIDs, historical prices and
in-flight jobs. IVC live synthesis depends on an authorized speaker sample. Music
2.5 depends on existing account access, without a subscription purchase.

## Release 3 candidate findings

The initial video candidate passed 328 Gateway tests and migration008 rehearsal
retained identifiers/history and48NULL costs. Seedance2.5 edit completed and
settled once at$0.4736; original640×640 H.264 output is downloaded (3.708333s,
no audio track). Veo exposed a regional routing defect: global Gemini settings
must not determine these Veo models' `us-central1` endpoint. Omni text returned
an explicit provider `invalid_request`, exposing missing text-format/default and
error-retention coverage. The corrected regional Veo candidate completed a four-second first/last-frame
video at $0.12 and was promoted. Native Omni text still returns HTTP 400 despite
a request matching current SDK types; its Magic Lens alias remains disabled
pending provider clarification. Working video paths are unaffected. Uncertain reservations remain in the ledger.
