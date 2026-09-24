# ai-gateway policy

Canonical source: `skills/price-verification/` in ai-gateway. Refresh the matching
personal copy with `scripts/sync_price_verification.py` after repository changes.
Other projects retain their own admission and billing policy.

Use LiteLLM's maintained catalog and provider calculators by default. Capture the
resolved entry in `pricing/litellm_catalog.json` and a versioned profile in
`pricing/registry.json`; each attempt pins its complete price snapshot. Standard
catalog models do not need duplicate vendor research, paid acceptance per alias,
invoice access or manual approval before admission. Review dates are reminders.

Use this skill for missing custom-provider prices, demonstrated discrepancies,
price changes and audits. Prefer provider-reported monetary charges, then known
account rates, then catalog/posted API rates. Correct demonstrated SDK or catalog
errors explicitly with evidence and regression tests. Restrict only the affected
option when other options have working pricing. Missing pricing, an unresolved
material discrepancy or an unsupported billable option is a concrete blocker.
Missing account invoice access alone is not a blocker to using posted API rates.

Unknown usage after submission remains unresolved with null cost and is excluded
from automatic settlement. A free operation needs an explicit reason. Invoice
reconciliation is separate from request-time cost. For subscription products use
posted API rates without allocating subscription fees. Preserve customer quotes,
markups and settled charges. Historical corrections require original-date rates.

Keep evidence numeric and attributable without request content or credentials.
Resolve API products precisely: all configured Seedance models use ModelArk with
BYTEDANCE_API_KEY; LAS is a different product with a different pricing formula.
