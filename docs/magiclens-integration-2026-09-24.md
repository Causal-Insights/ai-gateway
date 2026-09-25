# Gateway retirement and Magic Lens integration review — 2026-09-24

This is dated evidence and a compatibility review, not a deployment record.
Gateway baseline: `0cc8ec95d25a34b69ac346294a81f68854d6a704` plus this retirement change.
Magic Lens inspected baseline: `63f3be14db08ff0b8719bdb9d159633873485ec7`; that checkout has unrelated local changes. No Magic Lens files or registry state were changed.

Subsequent local LiteLLM 1.102.1 upgrade results, including database preservation,
consumer tests and mocked browser workflows, are recorded in the
[upgrade evidence](litellm-v1.102-upgrade.md). The original live dev observations
below remain dated evidence; the upgrade did not deploy that environment.

## Retirement scope

Removed from `litellm_config.yaml` and the active alias map in `pricing/registry.json`:

| Alias | Exact retired upstream | Provider evidence |
| --- | --- | --- |
| `imagen-4.0` | `vertex_ai/imagen-4.0-generate-001` | [Google discontinuation notice](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes), March 24, 2026; migration advised before June 30 |
| `imagen-4.0-fast` | `vertex_ai/imagen-4.0-fast-generate-001` | Same notice |
| `imagen-4.0-ultra` | `vertex_ai/imagen-4.0-ultra-generate-001` | Same notice |
| `gemini-3.1-flash-lite-preview` | `vertex_ai/gemini-3.1-flash-lite-preview` | [Google retirement notice](https://docs.cloud.google.com/gemini-enterprise-agent-platform/release-notes?hl=en), July 9, 2026 |

There are now 52 configured aliases. All 124 historical pricing profiles and the captured LiteLLM catalog are retained. Every remaining model mapping is unchanged. New admission cannot select a removed alias, while historical receipts still resolve their pinned pricing versions. No automatic successor routing was introduced.

Other reviewed notices do not justify additional removals: the retired Nano Banana *preview* IDs are not the currently configured IDs; CodeMender removing Gemini 3 Flash as its own backend is not retirement of the serving endpoint; Grok Image Quality's November 2 transition is still in the future. Mere `preview` or `deprecated` labels do not establish retirement.

## GPT Image 2.5 access

Both alias and dated-snapshot model lookups returned HTTP 200. One direct OpenAI Image API request per snapshot returned HTTP 200 and one image. Each requested 1024×1024, low quality, and `n=1`; no automatic retries occurred. Each returned usage of 17 input text tokens and 196 output image tokens. Test tally: two images, no video/audio/text generations.

| Gateway alias | Tested exact snapshot | Lookup | Basic generation |
| --- | --- | --- | --- |
| `gpt-image-2.5-sunburst` | `gpt-image-2.5-sunburst-2026-09-08` | 200 | 200; one image |
| `gpt-image-2.5-flare` | `gpt-image-2.5-flare-2026-09-08` | 200 | 200; one image |

The requests used the existing Gateway `.env` OpenAI credential directly against `https://api.openai.com/v1`. This supersedes the September 9 access failure for that credential. It does not establish deployed Gateway credential equivalence, end-to-end Magic Lens success, editing, Responses image tools, or streaming. Provider model IDs remain unchanged.

[Sanitized access receipts](evidence/gpt-image-2.5-access-2026-09-24.json) retain timestamps, request IDs, status and numeric usage. They contain no keys, prompts or generated media.

## Live dev impact

Read-only checks used Magic Lens's dev-restricted `fetchDevRegistry` and `fetchDevGatewayAliases` helpers. The deployed dev Gateway still advertises 56 aliases; this local change has not been deployed. The registry helper found 37 models selectable for ordinary Studio users under its enabled/published/lifecycle/surface/pricing checks.

| Alias | Advertised by deployed dev Gateway | Selectable in dev Magic Lens for ordinary Studio users |
| --- | --- | --- |
| `imagen-4.0` | Yes | No |
| `imagen-4.0-fast` | Yes | No |
| `imagen-4.0-ultra` | Yes | No |
| `gemini-3.1-flash-lite-preview` | Yes | **Yes — active, chat profile** |
| `gpt-image-2.5-sunburst` | Yes | Yes — `openai_image`, revision `openai-2026-09-09/v1` |
| `gpt-image-2.5-flare` | Yes | Yes — `openai_image`, revision `openai-2026-09-09/v1` |

[Sanitized dev status](evidence/magiclens-integration-status-2026-09-24.json).

Before retiring the serving aliases in a deployed Gateway, remove the retired Gemini choice from Magic Lens's selectable registry and reconcile its fallback catalog. The absence of the Imagen choices from this ordinary-user projection does not prove that no admin, saved workflow, export, Tool package or historical run references them. This review did not enumerate saved customer content. Historical migrations contain all four IDs and must not be rewritten.

## Integration points and contracts to preserve

Paths below refer to the sibling `magiclens` repository.

| Boundary | Executing integration point | Compatibility requirements |
| --- | --- | --- |
| Model discovery and selection | `apps/workflow-manager/lib/model-registry-service.js` (`listAdminModels`); `apps/studio-pro/lib/studio-pro/modelRegistry.ts` (`listStudioModelCatalog`); `packages/model-registry/src/index.js` | Admin compares `/v1/models` with registry diagnostics. Studio selection comes from its own published database registry, not automatic Gateway synchronization. Keep stable `model_key`, `gateway_model_id`, `request_profile`, versions, defaults and prices. |
| Fallback catalog and saved identities | `apps/studio-pro/src/studio/lib/aiModels.ts`; append-only `schema/migrations/` | The fallback still contains retired IDs. Current-picker retirement and historical read compatibility are separate. Never silently replace a model in a quoted or pinned run. |
| Gateway authentication | `packages/ai-gateway-auth/src/index.js` | `LITELLM_BASE_URL`, Gateway bearer key, and Cloud Run `x-serverless-authorization` identity token/audience are separate. Preserve base-URL `/v1` normalization and authorization on content downloads. |
| Studio generation | `apps/studio-pro/lib/studio-pro/service/liteLlmNodes.ts` | Separate Studio adapter calls images, edits, Responses, chat, speech and durable jobs. A Runner-only fix does not establish Studio parity. |
| Runner generation | `services/experience-runner/src/providers/litellmProvider.js` | Text normally uses chat; Astra uses Responses. Gemini image references use chat; OpenAI references/masks use multipart edits; hosted image tools use Responses. SFX/music use Images generations with base64 audio. Preserve each request profile and output shape. |
| Compilation and validation | `packages/model-registry/src/generation.js`, `videoProfiles.js`, `videoCompatibility.js`, `openai.js`; `packages/run-package/` | Input roles, settings, schema version, contract revision and output validation are executable constraints. Gateway accepting a new field does not make Magic Lens capable of authoring or compiling it. |
| Durable video | `packages/ai-gateway-jobs/src/index.js`; Studio/Runner adapters | Preserve POST `/v1/generation-jobs`, stable `Idempotency-Key`, V1 and V2 contracts, job IDs/statuses, poll hints, errors/retryability, and GET status/content. Jobs resume across workers; changing the request hash or re-submitting ambiguous work risks duplicate charges. |
| Media transport and persistence | `packages/ai-gateway-auth/src/index.js`; `packages/upload-limits/`; Studio/Runner image adapters | Multipart size budgeting, lossless preparation, reference ordering, masks, alpha, output format and original bytes affect existing editing features. Preserve authenticated Gateway content URLs and downstream private-storage persistence. |
| Financial journal and settlement | `packages/ai-gateway-jobs/src/financial.js`; `services/experience-runner/src/financialReconciliation.js` | Preserve `x-magiclens-attempt-id`, `x-gateway-accounting-id`, cost status, usage, model identity and pricing version. Magic Lens reconciles `/v1/costs/{id}` or generation-job receipts; unknown cost must remain unknown. Historical pricing profiles remain necessary after retirement. |
| JEV boundary | `services/experience-runner/src/canvasAssistantDecisionClient.js`; `docs/ai-and-media-platform.md` | JEV calls TypeSafe directly from Magic Lens. No Gateway dependency should be restored. |

## Change strategy for later capability work

1. Define one explicit workflow and its current Magic Lens request profile, including saved/pinned inputs and defaults.
2. Make Gateway changes additive where possible. Retain old video schemas, parameter names, output shapes, error semantics and persisted job routing while existing consumers use them.
3. Verify both Studio and Runner request goldens, compiler/transmission checks, output persistence and quote/settlement behavior for the affected workflow.
4. Before deployment of this retirement change, use Magic Lens's controlled registry operations to retire the remaining selectable preview. Inspect active/pinned workflow and Tool references; publish explicit successor versions only where intended. Preserve old records and provide a clear unavailable-model outcome where a retired provider can no longer execute.
5. Identify the exact Gateway commit/config/image before verifying the Magic Lens candidate. A local sibling checkout or `/v1/models` listing alone is not runtime capability evidence. Perform an authorized dev end-to-end probe for the selected workflow before broader publication.

No Gateway deployment, Magic Lens registry mutation, migration, publication, or traffic change was performed in this task.

## Validation

- Gateway pinned-image unit suite: 261 tests; 192 passed, 69 skipped. Network disabled. Skipped checks are not claimed as passing.
- Pricing coverage release check: 52 aliases, 124 profiles, zero errors; report regenerated.
- Pricing preservation comparison: all 124 profiles and 52 retained model mappings exactly match the pre-change registry.
- Magic Lens selected protocol/adapter checks: 131 passed initially; three SQL checks could not initialize host PostgreSQL because of shared-memory exhaustion. All three passed when rerun with the existing `MAGICLENS_TEST_POSTGRES_DOCKER=1` isolated database helper: 134 selected checks passed across the runs.
- Magic Lens checked files: `ai-gateway-auth`, `ai-gateway-jobs`, `financial-transport`, `grok-video-v2`, `seedance-video-v2`, `openai-september-models`, and `litellm-provider` unit suites.
- Magic Lens documentation, model-documentation and Tool-documentation governance all passed. Their dated catalog snapshot still describes the deployed 56-alias Gateway, not this local 52-alias candidate.
- Gateway catalog regression explicitly rejects retired aliases while requiring historical profiles to remain.

Host Python lacks FastAPI/PyYAML/JSON Schema dependencies; checks requiring those dependencies were rerun in the pinned application image. No full Magic Lens browser or paid end-to-end suite was run; the two live image probes establish direct provider access only.
