# Hosted tools and owned OpenAI resources

Release 6 uses the existing native Responses execution path for `web_search`,
`file_search`, `code_interpreter`, and `image_generation`. Existing function tools
and image outputs remain intact. The exact GPT-6 Astra capability document lists
these four tools; discovery advertises this finite contract for that alias.

Resource requests require an authenticated key whose metadata has
`gateway_resource_delegate: true` and `gateway_app_id`, plus
`X-Gateway-Resource-Owner` supplied by the trusted application backend. Body
metadata cannot grant delegation. Resource lookup checks both application and
owner. Raw foreign provider IDs are denied even when the provider account could
otherwise access them. Existing provider resources have no inferred ownership;
they require an explicit administrative ownership import before use here.
Owner-scoped Responses requests bypass shared response caches so cached provider
resource IDs cannot cross delegated owners.

| Surface | Implemented operations |
| --- | --- |
| `/v1/files` | Multipart `file` and `purpose` create; owned list, retrieve, raw content, delete |
| `/v1/batches` | Create, owned list/retrieve, cancel, recovery against an original create intent |
| `/v1/vector_stores` | Owned create/list/retrieve/delete and owned file attach/list/retrieve/delete/content |
| `/v1/containers` | Owned list/retrieve/delete and generated file list/retrieve/content/delete; containers are discovered from owned Code Interpreter responses |
| `/v1/responses/{id}` | Owned retrieve/delete/cancel; retrieval can settle the original background execution |
| `/v1/resources/owner-data` | Owner metadata export (`GET`) and resumable account cleanup (`DELETE`) |

Owner cleanup revokes access before provider operations. It cancels active
batches/responses, observes available receipts, then deletes vector stores,
containers and files. `cleanup_status: pending` and per-resource reasons require
another `DELETE`; cancellation may take time, and unavailable provider identities
need reconciliation. Known resource creates admitted before revocation are cleaned
up when their late responses arrive. Accounting records and receipt identifiers
remain. Batch has no provider delete operation: its input/output files are deleted
after settlement, and its minimal Gateway tombstone remains. Provider-retained
batch metadata is subject to the provider's retention policy. Unknown or missing
usage is never converted to zero by account cleanup. Export contains resource
identifiers and selected metadata, not file bytes, model inputs or generated text.
These authenticated owner export/delete operations do not require available
generation budget. The pinned SDK exemption is limited to this exact route and
its two methods; credential, delegation and owner checks still apply.

File purposes are `batch`, `assistants`, `user_data`, and `vision`. Provider file
limits apply: 200 MB for batch inputs and 512 MB otherwise. Creation requires an
`Idempotency-Key`. Retrying an identical request returns its original intent or
result; conflicting input returns 409. Unknown creates never resubmit
automatically. Responses expose `gateway_resource_id` and `gateway_state` in
addition to provider fields. A lost upload response can remain unresolved because
the Files API does not provide an ownership-bearing recovery marker.

The initial batch contract supports exact `gpt-6-astra` text requests to
`/v1/responses` and `/v1/chat/completions`, one model and endpoint per JSONL file,
unique `custom_id` values, and the provider's 24-hour completion window. Streaming,
hosted tools, alternate processing tiers, and other endpoints are outside this
initial batch contract. Each item passes native model access checks and fresh
budget admission, then receives an immutable pricing snapshot and durable attempt
before batch submission. This fresh budget check is not a worst-case reservation
for all future batch output; consumers must retain their existing quote/hold
discipline when submitting paid work.

Polling and cancellation process each returned `custom_id` against its original
attempt. Duplicate receipts do not increment spend twice. Failed, cancelled,
expired, or absent output without billable usage remains unknown, not zero.
`gateway_items` contains each item's `accounting_id` and the usual cost response.
Provider output/error files become owned downloadable resources. A lost batch
create can be recovered with `POST /v1/batches/{gateway_resource_id}/recover` and
`{"provider_id":"batch_..."}`; the provider's original
`metadata.gateway_resource_id` must match the authenticated owner's intent.

Pricing is pinned from the registry's `hosted_tools` contract. Completed web
**search actions** cost $0.01 each; opening or finding text on a page is not counted
as another search. Completed file-search calls cost $0.0025 each. Parent tokens
use the submitted model's frozen profile. Exact Astra Batch pricing applies 50%
of Standard rates, including existing cache and context corrections.

Container IDs do not establish billed duration or session eligibility. Vector
store size snapshots do not establish GB-days or allocation of the account's free
storage. These resource costs remain unresolved. Image-tool outputs and available
child usage are retained, but the SDK's parent subtotal does not establish a full
image-tool charge: LiteLLM 1.102.1's built-in tool calculator has no
`image_generation_call` pricing branch. The known parent subtotal is retained as
`gateway_hosted_parent_cost_usd`; no guessed combined total is projected to spend.
Resource storage charges are not projected into model execution totals without
authoritative owner-attributable usage.

Verification is hermetic against LiteLLM 1.102.1 and an isolated PostgreSQL copy.
No live hosted-tool, batch, container, or storage acceptance is claimed by these
tests. See [official evidence](../pricing/evidence/openai-hosted-tools-2026-09-24.json),
[Astra capabilities and Batch rates](https://developers.openai.com/api/docs/models/gpt-6-astra),
[tool pricing](https://developers.openai.com/api/docs/pricing), and
[Batch lifecycle](https://developers.openai.com/api/docs/guides/batch).
