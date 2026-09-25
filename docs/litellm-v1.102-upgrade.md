# LiteLLM 1.102.1 upgrade — September 24, 2026

Current implementation and acceptance status: [staged rollout gap table](capability-rollout-gap-table-2026-09-24.md). Earlier findings below describe the pre-rollout baseline.
This upgrade follows the approved sequence: checkpoint, upgrade the runtime,
verify existing behavior, then reassess capability work. It keeps the 52 configured
aliases, five serving providers, upstream IDs, public endpoints and saved job
identities. JEV remains exclusively in Magic Lens.

The [official stable release](https://github.com/BerriAI/litellm/releases/tag/v1.102.1)
is pinned in `Dockerfile`:

```text
ghcr.io/berriai/litellm:v1.102.1@sha256:87f34979b9f8cb274fac90ca8a4fdda07d8480de22755562a26adeb95ce20d02
```

The image supplies LiteLLM 1.102.1, OpenAI SDK 2.33.0 and Python 3.13.
Newer development/RC tags were not selected. This record describes local
validation; it does not claim a Cloud Run deployment or live provider acceptance.

## Compatibility changes

| Change | Why it is needed |
| --- | --- |
| Update the image digest and accounting version guard | The private hooks are verified against this exact SDK. |
| Accept `window_duration` in the committed-spend hook | 1.102.1 passes it even for ordinary key budgets. Without the fix, a valid virtual-key request returns an authentication error. Real HTTP verification found this regression. |
| Use the SDK's end-user spend reader | The new generic reader no longer handles end-user totals. Existing budgets must still read current committed values. |
| Continue aggregating spend logs for window budgets | Gateway owns the spend writer and does not maintain LiteLLM's new window-spend cache table. Reading that table could understate Gateway spend. No new admission rule was added. |
| Remove the GPT Image 2.5 response-format override | Native image generation now preserves the provider's format and supplies the request default when absent. |
| Record the installed SDK in future pricing captures | New captures must not claim they were made with 1.95.0. Existing captured profiles and historical version IDs remain unchanged. |

The OpenAI edit-field, generation-parameter, Astra cache/streaming, retry and usage
hooks remain where the native SDK still needs them. The nullable Prisma SpendLogs
patch remains: an unknown cost must be readable as `null`, including through the
stock Logs API. No new capability registry, feature flag or verification gate was
introduced.

## Verification

| Check | Result |
| --- | --- |
| Application build | Passed with the pinned amd64 image. |
| Complete Gateway suite | **264 passed, zero skipped**, with real isolated PostgreSQL. Includes accounting, duplicate suppression, usage, budget reads, durable job recovery, OpenAI wire contracts and all existing custom adapter tests. |
| Provider inventory | 52 aliases verified. |
| Pricing integrity | 52 aliases / 124 profiles; zero errors. All 50 structured evidence reports validate. No price refresh was performed. |
| Migration rehearsal | 30 pending LiteLLM migrations applied to a disposable copy of the local database. All identifier checks passed; 32 foreign keys validated. |
| Historical data | 15 virtual keys, 28 jobs and 228 detailed spend rows preserved. Hashes of complete jobs, cost requests/attempts, key/user budget values and selected spend/usage fields match the original local database. Six unknown spend values remain `null`. |
| Real HTTP candidate | Authenticated readiness/discovery and virtual-key calls passed. One local mock-provider chat returned HTTP 200, a priced **$0.0006** receipt and one spend row. Reducing that test key's budget returned HTTP 429 before another provider call; the fixture received exactly one request. |
| Magic Lens consumer suites | **153 passed, zero skipped** across authentication, jobs, financial transport, OpenAI, Grok/Seedance video, general provider and audio consumers. |
| Magic Lens browser workflows | **18 passed**: image comparison/generation, OpenAI model controls, first-frame/persisted video modes and Grok/Seedance controls. Providers and authentication were mocked. |
| Magic Lens documentation checks | Documentation, model documentation and Tool documentation all passed. |
| Native capability probes | Gemini 3.8 reasoning levels map correctly; ElevenLabs preserves raw voice IDs; OpenAI native edits still omit format/compression/moderation. |

Magic Lens was tested at sibling checkout `63f3be14db08ff0b8719bdb9d159633873485ec7`
with its pre-existing local changes. This upgrade did not edit that repository.
Browser startup emitted an existing `uploadStudioBlob` export warning; the selected
workflows passed. This does not establish that every unrelated upload workflow works.

The upgrade verification uses isolated databases and mock providers. It adds no
paid provider generations. The earlier two direct GPT Image 2.5 access probes
remain the live evidence for those models; they are not reclassified as full
Magic Lens end-to-end tests.

## Remaining capability gaps

The overall consistency assessment remains **6/10**. Runtime compatibility and
upgrade confidence improved; custom adapters and consumer contracts still account
for most of the gaps. The [provider reference](provider-implementation-reference.md)
lists every configured model and the exact implementation to inspect.

| Area | Status after upgrading | Next action |
| --- | --- | --- |
| Gemini 3.8 thinking | Native Vertex mapping correctly preserves `minimal`, `low`, `medium`, `high`. | Drop the earlier blanket incompatibility concern. A missing local policy-set entry does not justify adding a gate. |
| GPT Image response format | Native response handling replaces the local override. | Completed. |
| Seedance 2.5 editing/defaults | V1 still cannot express `-1` automatic duration; omitted duration/ratio still receive unsuitable defaults for some operations. | Fix these specific translations and corresponding accounting first. |
| Omni / Grok audio controls | Explicit Omni audio enablement still fails pricing admission; Grok V1 still drops its audio control. | Preserve and account for the requested audio behavior. |
| Seedream | Lite streaming still assumes JSON; Pro layers are dropped; search remains pricing-restricted; no Images edits implementation. | Correct streaming/field handling before adding further options. |
| Veo | Selected adapter still supports text or one image, not first+last, reference sets or extension. | Add one requested workflow at a time through Gateway, Studio and Runner. Upgrading the SDK does not select the existing direct adapter. |
| Grok / Seedance / Omni video | V1/V2 parity gaps, reference roles and advanced operations remain. | Preserve existing schemas while adding the requested operation. Exact upstream identities remain significant. |
| GPT Images | Older 1.5/2 edit controls are still filtered by the native SDK. Images-route streaming remains unavailable through Gateway; 2.5 edits retain their local compatibility hook. | Expand supported controls only where needed; validate transmitted fields and returned results. |
| Grok Image 2 | Automatic quality and some wide `size` ratios remain restricted; explicit aspect ratio is a workaround. | Normalize equivalent controls without discarding user intent. |
| ElevenLabs / Instant Voice Cloning | Existing TTS accepts provider voice IDs. Clone creation, private ownership, listing/deletion and Magic Lens integration are still absent. | IVC remains feasible as a focused next feature; a general capability framework is not a prerequisite. Preserve curated voices and saved Magic Lens voice UUIDs. |
| Client consistency | Studio and Runner still choose provider-specific transports; custom audio still uses Images-shaped responses. | Add shared client/discovery metadata alongside working workflows, with no new mandatory metadata dependency. |
| Retired models in Magic Lens | The last dev inspection still found Flash-Lite Preview selectable. | Coordinate that registry retirement before cloud rollout of the Gateway removals; retain historical records. |

See the [updated assessment](gateway-consistency-assessment-2026-09-24.md) for voice
ownership and the remaining capacity considerations. Missing verification alone
must not block otherwise valid requests or discard usable outputs.

## Local use and later cloud rollout

The normal local Gateway was rebuilt and restarted successfully on
`http://localhost:4000`. Authenticated liveliness, readiness and 52-model discovery
passed on LiteLLM 1.102.1. The normal local database's identifier hashes and spend
counts also matched the pre-upgrade snapshot after migration. Final local image:
`sha256:59ba9f7a333b236d883ae4c9d461871ea9e13588968aa8df0b295f04e1c0e44b`.

`./run_gateway.sh --no-follow` rebuilds the normal local image, applies checked
migrations to the local Compose database and waits for health. A private local
database backup and the previous image tag `ai-gateway-litellm:before-11021-20260924`
are retained for rollback. Rolling back the image also requires its matching
checkpointed source because Compose mounts source files.

Cloud rollout remains separate. Use the existing deployment helper's migration
and serving-candidate stages, with an immutable application tag, and coordinate
Magic Lens's remaining retired picker entry before promoting the catalog removal.
The local database rehearsal is useful evidence; it is not a production backup
or proof of a production migration. This task did not change cloud traffic,
credentials, model publication, private voices or historical charges.
