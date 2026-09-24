# Typed decisions

`POST /v1/decisions` is authenticated with the same Gateway key and model access
checks as other inference routes. It accepts exactly `{model, state, questions}`
using TypeSafe's native [System One contract](https://docs.typesafe.ai/api).
The initial model is pinned to `jev-1.13.0`. Noul, Choice, and Score are supported.
This endpoint never calls a generative, vision, audio, or fallback model.

The provider credential is `JEV_API_KEY` in the Gateway process. It is never a
browser variable, client payload field, returned diagnostic, or LiteLLM generation
model. `GET /v1/decisions/models` is authenticated read-only discovery; it returns
the `Decision` kind, `assistant` surface, `system_one` profile, and availability.
It does not call TypeSafe or add Jev to the generation catalog.

Each call sends one HTTP request to `https://api.typesafe.ai/v1/systemone` with
redirects and retries disabled. The Gateway validates the request before admission
and persists an execution intent before provider dispatch. It checks served model,
answer ids/types, candidate membership, finite probabilities, their possible unrounded sum, and score
consistency before returning usable answers. Only typed answers and numeric usage
are returned; extra provider prose is discarded. The 2 MiB JSON transport limit is
not a tokenizer or a claim that a request fits TypeSafe's token context limit.
Native probabilities rounded to hundredths are accepted only when their rounding
interval can contain a valid distribution; native values and confidence are
preserved. This handles the observed 0.99 total across 65 choices without treating
an impossible distribution as valid.

The response preserves `{model, answers, usage:{input_tokens,output_tokens}}` and
adds `provider`, `accounting_id`, `cost_status`, `cost_usd`, `cost_source`,
`pricing_version`, `breakdown`, optional `provider_request_id`, and
`provider_requests`. `provider_requests` is 0 for known pre-dispatch rejection
and 1 after dispatch, including ambiguous network failures; callers must count
an interrupted request conservatively if no receipt arrives. Errors use
`{error:{code,message},...receipt}`. The normal `/v1/costs/{accounting_id}` endpoint
can reconcile a pending receipt. Request state, candidate descriptions, answers,
and credentials are excluded from the numeric cost journal.

Metering is captured even when a charged response fails typed validation. Known
usage is priced; unknown costs stay null and unresolved. The initial official
public rate is $0.042 per million input tokens and free output tokens, verified
from [the model page](https://docs.typesafe.ai/models) on 2026-09-22. The structured
evidence explicitly has no historical effective date and does not assert an
account discount or invoice acceptance. The prospective pricing profile is
disabled; deployment or activation is a separate operation. Mocked tests enable
an isolated copy and never call TypeSafe.

MagicLens owns the per-user monthly 5,000-request allowance and conversation
history. A multi-question request is one provider request; each chain hop is
another. The Gateway supplies internal vendor cost receipts. Routing is included
in the product and is not a customer generation-credit charge.

Validation: `python -m unittest tests.test_decisions tests.test_cost_accounting
tests.test_price_verification_skill` and `python scripts/validate_price_evidence.py`.
The route tests use the pinned LiteLLM auth dependency and HTTPX mock transport;
they require the Gateway Python dependencies but no provider keys or database.
