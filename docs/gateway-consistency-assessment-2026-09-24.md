# Gateway consistency assessment and Instant Voice Cloning feasibility

Current implementation and acceptance status: [staged rollout gap table](capability-rollout-gap-table-2026-09-24.md). Earlier findings below describe the pre-rollout baseline.
Reviewed September 24, 2026 against the local 52-alias candidate. Updated after
the LiteLLM 1.102.1 upgrade; see [the upgrade evidence](litellm-v1.102-upgrade.md)
and [provider implementation reference](provider-implementation-reference.md).
This is an architectural assessment, not a cloud deployment certification.

## Rating against the intended goal

**6/10 overall as a reusable consistency layer.** This is an engineering judgment,
not a benchmark score. Authentication, provider routing, accounting and durable
execution provide a useful foundation. Clients still need substantial knowledge
of individual providers, endpoints, media representations and contract versions.

| Area | Rating | Reason |
| --- | --- | --- |
| Provider identity and routing | 8/10 | Exact mappings, five serving providers, controlled credentials and a checked provider inventory. |
| Accounting and execution durability | 8/10 | Journal, persisted attempts, idempotency for video, ownership, reconciliation, and explicit unknown costs. Historical accounting repairs are documented; current live behavior was not re-audited here. |
| Uniform multimodal interface | 5/10 | Reference images use different endpoints; audio is wrapped in Images responses; durable jobs only model video. |
| Executable capability discovery | 4/10 | Config and pricing metadata do not provide a complete, validated operation/parameter/media-role contract for clients. |
| Consumer independence and compatibility | 5/10 | Magic Lens carries duplicated provider decisions in Studio and Runner, owns a separate registry, and directly discovers ElevenLabs voices. |
| Upgrade and operational confidence | 7/10 | The 1.102.1 candidate has database migration, budget compatibility, accounting and Magic Lens regression evidence. SDK hooks, production capacity and live provider coverage still need attention. |

A consistent contract should preserve an operation's meaning when selecting
another model that supports it. It should also declare genuine model differences.
One schema does not make unsupported modalities, voice identities, resolutions,
or provider-hosted tools interchangeable. Stable common fields, typed extensions,
explicit constraints and useful unsupported-operation errors are preferable to
silently dropping user intent.

## Address first

P1 means the next corrective work or a prerequisite for the stated rollout; P2
means a planned feature expansion. Missing features are not all release blockers.

| Priority | Deficiency | Consequence and next action |
| --- | --- | --- |
| P1 — before retirement deployment | Magic Lens still offered the retired Flash-Lite Preview at the last dev check. | Retire that picker entry, reconcile the fallback and inspect saved/pinned references before deploying the Gateway removals. Preserve old IDs and pricing history; no silent successor substitution. |
| P1 — request correctness | Seedance 2.5 editing cannot represent required automatic duration; frame/extension defaults are unsuitable. | Preserve automatic duration and choose provider defaults appropriate to the operation, with corresponding usage accounting. Fix the actual translation without adding unrelated prerequisites. |
| P1 — request correctness | Audio and streaming flags can fail or lose meaning. | Omni rejects explicit `generate_audio=true` at pricing admission; Grok V1 omits the audio control; Seedream Lite forwards `stream=true` but reads the response as JSON. Implement those requested semantics and preserve usable results. A new blanket rejection layer would not fix these defects. |
| P2 — consistency | Clients lack a complete description of exposed operations and fields. | Extend the provider inventory with useful discovery metadata as workflows are implemented. Reuse the executing adapters as the source of truth. Missing verification metadata must not become a runtime prerequisite, and a new capability framework is not required for the next fixes. |
| P1 — consistency | Clients select transport based on provider/model families. | Add a versioned Gateway client contract with operation-specific typed inputs/results and shared errors, request IDs, accounting receipts and media handles. Adapt Gemini chat images, OpenAI edits and custom audio behind it. Keep current endpoints working during migration. |
| P1 — before another consumer / private voices | Resource and ownership contracts are incomplete beyond video jobs. | Introduce stable project/application authorization for private voice and media resources; support key rotation and revocation. Existing job reads are key-scoped; arbitrary end-user metadata is not authorization. Preserve the existing curated voice paths. |
| Completed locally — maintenance | Upgrade to stable LiteLLM 1.102.1. | Pinned by digest; adapted the changed budget hook, preserved nullable spend and removed redundant image response handling. See the upgrade record for validation and deployment boundaries. Custom adapter gaps remain separate work. |
| P1 — before scaling / promotion | Capacity and acceptance need to cover the actual shared service. | Deployment defaults permit 10 Gateway and 10 callback instances, while the runbook describes a 15-client session pool and multiple pools per instance. Size pool limits/autoscaling together and test overlap/recovery. Confirm actionable reconciliation alerts and current paid workflow evidence. This is a documented capacity risk, not a claim of a current outage. |

Code anchors: [`GenerationJobCreate`](../generation_job_models.py:37),
[`BytePlusAdapter` payload](../generation_job_adapters.py:644),
[`XAIAdapter`](../generation_job_adapters.py:329),
[`Seedream response handling`](../custom_handler_seedream.py:338),
[`request policy`](../gateway_request_policy.py),
[`SDK guard`](../gateway_accounting.py:551),
[`deployment defaults`](../deploy_cloud_run.sh:46), and the
[accounting runbook](cost-accounting-rollout.md).

## Major capability gaps carried forward

These are the outstanding items from the earlier all-model audit, updated for
completed retirement and access work. Provider-supported options remain subject
to exact-model/region verification; the linked master reference supplies the
corresponding official API and implementation sources.

| Family | Remaining gap | Order |
| --- | --- | --- |
| Veo 3.1 / Fast / Lite | Active adapter accepts text or one image and blocks first+last, reference sets and extension. The direct adapter exists but is not selected. Do not promise asset references for Lite, whose provider support differs. | P2, highest video expansion priority |
| Grok Video 1.5 | Missing last-frame/keyframe and first-frame-plus-reference workflows; restrictive voice/audio settings. Exact 1.5 edit/extend stays unverified/disabled rather than silently using another upstream model. | Correct audio semantics P1; expansion P2 |
| Seedance 2.0 / Fast | V2 exposes fewer input combinations than V1: missing first+last, audio/multiple-video references, extension and standard-model 4K. Automatic duration cannot be represented in durable V1. | P2; declare the difference immediately |
| Seedance 2.5 | Editing duration/default defects above; V1 only; draft/task/output-format controls absent. | Correctness P1; added controls P2 |
| Gemini Omni | V1 only; explicit audio enablement fails admission; single short MP4 source restriction, restricted continuation inputs, no standalone text-output surface. | Audio P1; parity/expansion P2 |
| GPT Image 1.5 / 2 | Pinned edit path omits output-format/compression/moderation controls; image streaming is not uniformly exposed. The local 2.5 preservation patch does not cover these models. | Preservation P1 for affected callers; streaming P2 |
| GPT Image 2.5 | Direct basic access now succeeds for Sunburst and Flare. Images streaming remains rejected; Responses is a separate implemented path. Deployed Gateway and Magic Lens edits/tools/streaming need their own evidence. | Integration verification P1; streaming P2 |
| Seedream | Pro layer decomposition is dropped; search remains pricing-restricted; Lite streaming handling is broken; no Images edits method despite that route appearing in pricing profiles. Pro lacks named `1.5K` preset. | Streaming/field truthfulness P1; layers/search P2 |
| Grok Image 2.0 | Automatic quality rejected; `size` rejects some wide ratios while explicit `aspect_ratio` is a workaround. Default quality is forced to medium. | P2 normalization |
| ElevenLabs | Voice creation/lifecycle missing; multi-speaker dialogue, timestamps/incremental WebSocket speech, full music composition/sections/seeds/editing, and SFX/music output-format selection absent. | Private voice foundation then IVC; other audio P2 |
| GPT-5.6 | Only named fixed reasoning efforts exposed. This can remain an intentional preset restriction, but must be discoverable; add a flexible alias only if wanted. | Product choice |
| Gemini 3.8 Flash | Rechecked on 1.102.1: native Vertex translation supports the model's reasoning-effort to thinking-level mapping. Its absence from the Gateway's special-policy set alone does not establish a failure. | Remove the earlier blanket P1 compatibility recommendation; verify any additional control when implementing it |
| Advanced tool/media workflows | Hosted tools, video-conditioned Gemini images, batch and native service APIs lack comprehensive current Gateway/consumer evidence. | Verify selected workflows before expanding claims |

Retirement of the four models is completed locally, and the September 24 GPT Image
2.5 direct probes supersede the old access failure. JEV removal is completed and
remains an intentional Magic Lens boundary. Do not list these as unfixed Gateway
implementation defects. [Retirement/access evidence](magiclens-integration-2026-09-24.md).

## Instant Voice Cloning

**High feasibility; medium implementation effort for a reusable production feature.**
The provider adapter is small. Ownership, lifecycle and consumer integration are
the larger work. A new voice is a reusable resource selected by a speech model;
it should not become another model alias or an Images generation workaround.

ElevenLabs exposes multipart `POST /v1/voices/add` with a name and audio files. It
returns `voice_id` and `requires_verification`. Its guide recommends roughly 1–2
minutes of clear, consistent speech; IVC does not require us to host a training
system. [Create API](https://elevenlabs.io/docs/api-reference/voices/ivc/create),
[recording guidance](https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning/instant-voice-cloning),
[cloning concepts](https://elevenlabs.io/docs/eleven-api/concepts/voice-cloning).

The installed LiteLLM 1.102.1 ElevenLabs speech adapter preserves a raw
provider voice ID when it is not a mapped stock voice name. Existing speech
aliases can therefore be the synthesis path once the clone is accessible to the
same provider credential and compatible with the chosen model. This was confirmed
by source inspection, not a live clone. Cloning itself has no implementation in
the inspected Gateway/pinned ElevenLabs adapter. [LiteLLM voice support](https://docs.litellm.ai/docs/providers/elevenlabs).

### Proposed contract (not implemented)

| Client operation | Gateway responsibility | ElevenLabs transport |
| --- | --- | --- |
| `POST /v1/voices` | Validate samples and consent evidence, authorize owner/project, persist create intent, return a stable Gateway voice ID and lifecycle state | Multipart `/v1/voices/add` |
| `GET /v1/voices` / `GET /v1/voices/{id}` | Filter by caller ownership/sharing; normalize availability and verification state | Voice listing/get as needed |
| `POST /v1/audio/speech` | Resolve authorized Gateway voice ID to the provider ID, check model compatibility, retain normal accounting | Existing LiteLLM ElevenLabs TTS |
| `DELETE /v1/voices/{id}` | Revoke use, reconcile provider deletion, handle sample/preview retention and saved references | `/v1/voices/{voice_id}` deletion |

Voice IDs remain scoped to compatible providers; an ElevenLabs clone does not
automatically work with another vendor's speech or video model. Keep cloning,
speaker verification and per-model synthesis acceptance as distinct states.

### Magic Lens integration requirements

Magic Lens already has `audio_voices`, `audio_voice_model_assignments`, a voice
picker, previews and model verification. Studio persists its own voice UUID and
resolves it in `apps/studio-pro/lib/studio-pro/audioVoiceCatalog.ts`; Runner uses
`services/experience-runner/src/audioVoiceCatalog.js`. Both currently check global
availability/model assignment with privileged data access and no owner argument.
These are suitable for the current curated catalog; private user clones require
an explicit ownership/sharing model and checks on list, preview, resolve and use.
`created_by` is an audit field, not an ownership authorization policy.

Admin voice discovery currently bypasses Gateway in
`apps/workflow-manager/lib/voice-providers/elevenlabs.js`, using
`ELEVENLABS_CATALOG_API_KEY`; synthesis uses Gateway's `ELEVENLABS_API_KEY`.
Before cloning, establish which account/workspace owns the resource and ensure
the synthesis credential can access it. For the reusable architecture, put new
provider resource operations behind Gateway and migrate existing discovery
additively. Magic Lens keeps its user experience, publication and application
permissions. Preserve existing Magic Lens voice UUIDs and verified assignments;
add the Gateway resource mapping without rewriting saved workflows.

### Requirements for a useful first release

- Sample upload/recording, preview and an explicit record of permission to clone the speaker. Do not put recordings in request logs; define sample and preview retention/deletion.
- Provider verification handling: creation may return `requires_verification=true`; voice GET exposes verification metadata. Surface the required next action and refresh status after completion. Verify the supported IVC verification journey before promising an entirely embedded experience; do not borrow PVC-only CAPTCHA endpoints. [Voice metadata](https://elevenlabs.io/docs/api-reference/voices/get).
- Owner/project-scoped creation, listing, previews, synthesis and deletion. Prevent submitting a raw private provider ID from bypassing those checks; retain the existing allowed curated voices. Trusted end-user identity must come from the application's authenticated server contract.
- Idempotent create intent and reconciliation after timeouts. Do not automatically recreate an outcome-unknown clone and consume another slot. Deletion/revocation should become effective locally even if provider cleanup needs retry.
- Account entitlement, available slots and monthly voice-operation quota. ElevenLabs documents cloning on Starter and above, and plan-dependent slot/operation limits. Confirm the actual account rather than assuming the existing speech key grants creation rights. [Billing eligibility](https://elevenlabs.io/docs/overview/administration/billing), [limits](https://help.elevenlabs.io/hc/en-us/articles/24351056337937-How-many-voice-slots-do-I-get-per-tier-and-how-can-I-increase-it).
- Log clone operations separately from TTS usage; do not assume a token tariff or invent a zero-dollar price for creation. Track applicable provider charges, quotas and ordinary speech accounting under the existing policies.
- End-to-end acceptance: create, verification-required recovery where applicable, preview, speak through both Studio and Runner, save/reload, unauthorized-user denial, quota failure, duplicate-submit recovery, deletion and saved-reference behavior. Check both current speech aliases with the chosen clone; keep old curated voices working.

No LiteLLM upgrade is required merely to add the create-voice adapter or consume
the returned ID. The upgrade and cloning work should have separate acceptance and
rollback plans. Native passthrough alone would not provide the resource ownership,
stable identifiers or standardized lifecycle needed by future applications.

## Recommended sequence and evidence

1. Checkpoint existing work, upgrade LiteLLM, exercise existing Gateway/Magic Lens behavior, and reassess gaps. This is the approved sequence carried out in the [1.102.1 upgrade](litellm-v1.102-upgrade.md).
2. Correct the specific Seedance duration/defaults, audio and streaming defects above. Coordinate Magic Lens's retired picker entry before cloud rollout of the model removals.
3. Add Instant Voice Cloning with owned voice resources, preserving the curated voice path and saved voice IDs. It does not depend on building a general capability framework first.
4. Expand one requested image/video workflow at a time through Gateway, Studio and Runner. Add useful shared client/discovery metadata alongside working behavior; keep existing inputs and defaults valid.

The original assessment re-read local implementations and previous recommendations, inspected
the pinned ElevenLabs adapter, and consulted current official documentation.
Offline probes reconfirmed Seedance's rejected `-1` duration, missing Omni/Seedance
2.5 V2 routes, Veo's selected LiteLLM route, Omni's false/true audio admission
asymmetry, and Gemini 3.8's absence from special request policy. No new paid
generation, clone, account inspection or live deployment audit was performed.
Earlier test totals remain dated evidence, not tests rerun for this assessment.
The later runtime upgrade and its tests are recorded separately in the linked upgrade evidence.
