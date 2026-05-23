# CLAUDE.md

## Development Commands

```bash
uv run -m pytest tests/                          # run tests
uv run python -m unittest discover               # alternative test runner
uv run ruff check
uv run ruff format
uv build && uv run --with twine --no-project python -m twine check dist/*
```

Run ruff check and ruff format before committing. CI also runs unittest
discover on Python 3.11 and 3.14 (minimum supported and latest).

## Design Context

`PROMPT.md` is a design document, not a task to execute. Read it for
architecture decisions, boundaries, and constraints before making design
choices or adding features. Read `docs/prd/` for component contracts and
`PLAN.md` for implementation slices.
