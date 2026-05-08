# Review Gates

Applies to behavior, persisted data, defaults, orchestration, artifacts, and
selection. Evidence belongs in tests or review notes.

## Semantic Regression Gate

Preserve meaning, not shape. Block changes that miss the touched contract,
assert only presence or success, alias external/user values without a documented
tested fallback, or drop accepted in-scope behavior during conflict resolution.

## Default UX Gate

Defaults keep common valid environments usable. Explicit config wins; multiple
valid choices need documented deterministic or persisted selection; failures
need actionable diagnostics; tests cover single, multiple, override, and none.
