# CLAUDE.md

## Development Commands

```bash
uv run -m pytest tests/                          # run tests
uv run python -m unittest discover               # alternative test runner
uv run --no-project --with 'ruff>=0.15' ruff check src/ tests/
uv run --no-project --with 'ruff>=0.15' ruff format src/ tests/
uv build && uv run --with twine --no-project python -m twine check dist/*
```

CI runs ruff check, ruff format --check, and unittest discover on Python 3.11
and 3.14. Run these locally before pushing.

## Design Context

`PROMPT.md` is a design document, not a task to execute. Read it for
architecture decisions, boundaries, and constraints before making design
choices or adding features. Read `docs/prd/` for component contracts and
`PLAN.md` for implementation slices.
