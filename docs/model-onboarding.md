# Adding or changing a gateway model

**Mandatory architecture rule:** a billable provider execution always produces a
visible run/cost record, even with missing attribution, disabled optional logging,
failed validation or failed budget projections. Preserve numeric evidence before
parsing results or performing secondary work. Unknown amounts are null. See the
[execution-logging invariant](execution-logging-invariant.md) and its regression tests.

Resolve the exact model in LiteLLM first. Supported catalog models use its rates
and calculators without a duplicate manual vendor-verification gate. The gateway
retains a versioned snapshot and attributable usage for each request.

1. Resolve the exact upstream ID, provider API product, account context, region,
   supported endpoints and billable options. Keep disabled models disabled. An
   alias changing upstream is a pricing change even if the alias name is unchanged.
2. Resolve rates with LiteLLM's actual provider resolver and capture the complete
   price entry. Use `scripts/refresh_litellm_pricing.py CAPTURED_MAP.json` in the pinned
   SDK image to refresh existing catalog profiles; review its diff. New aliases
   first need an exact upstream/vendor mapping in the registry and config. Run `$price-verification` for missing prices or demonstrated
   discrepancies; routine catalog support does not require a second vendor audit.
   Store custom-provider evidence in `pricing/evidence/`. Unknown account discounts
   are reconciled later; they do not prevent using posted API rates.
3. Add a new immutable profile in `pricing/registry.json` with exact identity,
   effective interval, currency, unit denominators, disjoint components, supported
   options, source/verification dates and calculation version. Close the old
   profile's effective interval without changing its historical rates. Do not use
   current retrieval time to assert earlier rate validity.
4. Implement strict numeric usage extraction. Prove cache/reasoning inclusion,
   modality accounting, duration/resolution/audio, reference-media and actual tool
   invocations. A field named `cost` is not USD unless the official schema says so.
   Missing usage is unresolved. Requested quantities require explicit billing rules.
5. Use authenticated attribution and a persisted attempt before every billable
   submission. Repeated callbacks/polls reuse an attempt; real retries do not.
   Custom handlers preserve original provider usage in `gateway_usage`; arbitrary
   `_hidden_params.response_cost` is not trusted for billing.
6. Add success, charged-failure, explicit-zero, missing-usage, streaming/disconnect
   and retry fixtures for each route and billing dimension. Aliases sharing an
   upstream may share usage fixtures. Verify served identity after generation.
7. Run the pinned runtime with isolated PostgreSQL. Assert exact journal amount,
   public cost contract, detailed spend, daily aggregates and budgets agree.
   Exercise duplicates, races, rollback/restart, key ownership and price changes.
8. Reuse shared route/accounting tests across aliases. Attach provider-backed
   evidence where available and investigate concrete differences. Paid generation
   is optional unless needed to resolve a specific issue, and requires task
   authorization. Missing per-alias paid fixtures do not disable catalog models.
9. Regenerate coverage with `python scripts/pricing_coverage.py --write`, run its
   normal validation and the `--release` gate, then follow the accounting rollout
   runbook. Document deprecation and the replacement alias without silently moving
   outstanding jobs to another provider or pricing version.

For a new vendor, add its official-source reference and verification procedure
under the skill, register the vendor in `pricing/registry.json`, and add realistic
usage/calculation fixtures before enabling any profile. Vendor rates do not belong
as permanent values in `SKILL.md`.

Install or refresh the canonical skill:

```sh
python scripts/sync_price_verification.py
python scripts/sync_price_verification.py --check
python "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" skills/price-verification
```

When `CODEX_HOME` is unset, use `~/.codex` for the final validator path. The installer
uses this fallback itself. After every repository skill change, refresh and verify
the personal copy. The standalone skill's vendor procedures are portable; its
gateway-policy reference applies only to this project.
