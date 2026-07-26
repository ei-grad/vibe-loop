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

## Skill Artifact Gate

`src/vibe_loop/skills/**/SKILL.md` is the only source of truth. Runtime skill
directories (`~/.claude/skills/`, `~/.codex/skills/`) hold installed artifacts:
never edit them in place, and install only from a clean `main` checkout. A task
branch or dirty tree installed there becomes the live instructions for every
agent on the host, including agents on unrelated projects, unreviewed and with
nothing naming the revision loaded. Update them only through the install/sync
step documented in `README.md`.
