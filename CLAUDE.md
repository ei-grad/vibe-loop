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

For any behavior, contract, or interface, exactly one file is authoritative.
Every other mention is a link, not a paraphrase. When two files describe the
same thing, name the authoritative file in both and reduce the other account to
a pointer.

Move, do not copy. A change that adds a section under `docs/` without deleting
the corresponding account elsewhere must state which file is authoritative.
An unanswered "both" is a review finding.

For contract and behavior material, `docs/prd/*.md` is authoritative.
`README.md` is not authoritative for anything it links to. A PRD may be stale:
when behavior and prose disagree, use the code to determine the correct account
and correct the PRD rather than bypassing it or adding another account.

## Repository Task Status Authority

The loopyard `vibe-loop` project is authoritative for implementation task
bodies, dispatch, dependencies, and current status. The task-source binding is
operator/runtime configuration and is not a tracked repository artifact. Do not
create a repository-local mirror of the board; use loopyard task bodies for
acceptance text and loopyard workflow states for task status.
