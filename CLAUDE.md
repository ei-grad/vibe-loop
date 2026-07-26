# CLAUDE.md

## Development Commands

```bash
uv run -m pytest tests/                          # run tests
uv run python -m unittest discover               # alternative test runner
uv run ruff check
uv run ruff format
uv build && uv run --with twine --no-project -m twine check dist/*
```

Run ruff check and ruff format before committing. CI also runs unittest
discover on Python 3.11 and 3.14 (minimum supported and latest).

## Design Context

`PROMPT.md` is a design document, not a task to execute. Read it for
architecture decisions, boundaries, and constraints before making design
choices or adding features. Read `docs/prd/` for component contracts and
the loopyard `vibe-loop` project for implementation slices.

## One Authority Per Behavior

This section is authoritative for repository documentation ownership policy.

For any behavior, contract, or interface, exactly one file is authoritative.
Every other mention is a link, not a paraphrase. When two files describe the
same thing, name the authoritative one in both and reduce the other to a
pointer.

Move, don't copy. A change that adds a section under `docs/` without deleting
the corresponding account elsewhere must state which file is authoritative.
An unanswered "both" is a review finding.

For Level 2 product contract and behavior material covered by a PRD,
`docs/prd/*.md` is authoritative. `README.md` is not authoritative for anything
it links to. A PRD may be stale: when behavior and prose disagree, use the code
to determine the correct account and correct the PRD rather than bypassing it or
adding another account.

This PRD authority does not override a narrower owner for material outside the
Level 2 product contract. `src/vibe_loop/skills/**/SKILL.md` owns exact shipped
skill instructions, [the configuration reference](docs/configuration.md) owns
the operator-facing option index, and
[the skill eval schema](docs/skill-eval-schema.md) owns its local artifact
format. Design and implementation references may explain rationale or mechanics,
but must link to the PRD instead of restating its product contract.

## Repository Task Status Authority

The loopyard `vibe-loop` project is authoritative for implementation task
bodies, dispatch, dependencies, and current status. The task-source binding is
operator/runtime configuration and is not a tracked repository artifact. Do not
create a repository-local mirror of the board; use loopyard task bodies for
acceptance text and loopyard workflow states for task status.
