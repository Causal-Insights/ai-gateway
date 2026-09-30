# High priority: preserve simple execution paths

- Prefer the simplest path that preserves the requested behavior. Add conditions,
  gates, flags, contracts, or defensive checks only when needed to implement the
  task or enforce an established correctness or security/safety requirement that
  is not already enforced. A failing test is evidence only when it captures
  intended behavior; do not invent stricter requirements through tests.
- Each added guard should prevent a concrete, reachable failure not already
  covered by existing guarantees. Explain the reason briefly when it is
  non-obvious; no separate approval or justification document is needed. Do not
  invent preconditions that valid execution cannot satisfy.
- Rely on established types, framework guarantees, invariants, and relevant tests.
  Validate untrusted inputs at their boundaries; avoid duplicating checks inside
  trusted paths or adding runtime gates solely to satisfy procedural preferences.
- Preserve outputs that satisfy the task and established contracts. Do not
  discard, suppress, or downgrade them because of newly invented procedural
  checks. If additional verification is unavailable, report that limitation
  without treating it as proof that the output failed. Reject outputs only for a
  concrete correctness, contract, or security/safety failure.
- Preserve existing behavior unless the task requires a change. Do not silently
  change accepted inputs, defaults, error handling, or output semantics through
  new guards, fallbacks, or opt-in flags. Explain necessary behavior changes.
- During edits, remove obsolete or self-created gates in the affected path when
  safe, preserving required invariants and security/safety checks. Keep cleanup
  within the task's scope.
- Verify the affected behavior with proportionate checks. Add tests only for
  meaningful behavior or regressions. Once relevant checks pass, complete any
  remaining requested work without repeating or broadening verification unless
  new changes, failures, or concrete unresolved concerns justify it.

# Provider implementation work

For provider-related work, use the relevant sections of
[the provider implementation reference](docs/provider-implementation-reference.md).
Resolve the public alias, exact upstream model ID, serving API, location, adapter,
and installed LiteLLM behavior where they affect the requested change or claim.
Consult official provider documentation for external behavior that needs verification.

When a change affects information recorded in the provider reference, update the
affected notes. Regenerate its inventory with `python scripts/provider_inventory.py --write`
and check it with `python scripts/provider_inventory.py` in the Gateway dependency
environment when its generated contents change. The generated table is an index,
not proof of provider access or full capability support.

When changing consumer-visible requests, responses, model discovery, or job
behavior, use the linked Magic Lens integration review to identify and inspect
the affected Studio or Runner contracts. Inspect both when the changed contract
is shared. Preserve historical pricing and persisted job identities.

Apply referenced procedures to the affected functionality. Preserve explicit
requirements, but do not infer additional prerequisites or expand the task into
unrelated audits.
