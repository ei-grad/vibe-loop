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

## One Authority Gate

For any behavior, contract, or interface, exactly one file is authoritative.
Every other mention must link to it rather than paraphrase it. When two files
describe the same thing, require the change to name the authoritative file in
both and reduce the other account to a pointer.

Move, do not copy. A change that adds a section under `docs/` without deleting
the corresponding account elsewhere must state which file is authoritative. An
unanswered "both" is a finding.

- [ ] Verify each added or moved contract account has a corresponding deletion
      or names its single authoritative file.

In this repository, `docs/prd/*.md` is authoritative for contract and behavior
material, and `README.md` is not authoritative for anything it links to. A PRD
may be stale: where behavior and prose disagree, use the code to determine the
correct account and require the correction to land in the PRD rather than
bypassing it or adding another account.
