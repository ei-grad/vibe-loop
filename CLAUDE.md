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
`PLAN.md` for implementation slices.

## Repository Task Status Authority

The loopyard `vibe-loop` project is authoritative for task dispatch and current
status. `PLAN.md` remains the implementation specification and history mirrored
by the loopyard project setting `source = "PLAN.md ## Task Plan"`.

For stable task IDs present in both sources, status presentation maps as
follows:

| PLAN.md | Loopyard stored status |
| --- | --- |
| `Done` | `done` |
| `Planned` | `ready` |
| `Blocked` | `on-hold` |

`Blocked` is a PLAN presentation label. Loopyard derives `blocked` from task
relationships and does not store it as a workflow status. Ad-hoc loopyard-only
findings do not require matching PLAN rows.

Run `uv run python tools/check_plan_board_drift.py` to compare the shared stable
IDs without changing either source.
