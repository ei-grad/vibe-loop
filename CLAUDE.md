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

## Repository Task Status Authority

The loopyard `vibe-loop` project is authoritative for implementation task
bodies, dispatch, dependencies, and current status. The task-source binding is
operator/runtime configuration and is not a tracked repository artifact. Do not
create a repository-local mirror of the board; use loopyard task bodies for
acceptance text and loopyard workflow states for task status.
