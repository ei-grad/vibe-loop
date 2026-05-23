# CLAUDE.md

Project-level instructions for AI agents working on `vibe-loop`.

## Project Structure

- `PROMPT.md` — Level 1 seed: philosophy, architecture decisions, stack, PRD
  rules, VSM mapping, runtime evolution direction.
- `docs/prd/` — Level 2 contracts with stable `PRD-*` IDs.
- `PLAN.md` — Level 3 implementation slices with permanent task IDs.
- `src/vibe_loop/` — Python package source.
- `src/vibe_loop/skills/` — Bundled workflow skills (Markdown contracts).
- `tests/` — unittest-based test suite.
- `eval/` — Bundled eval fixtures.
- `.vibe-loop/` — Local runtime state (locks, runs, logs). Untracked.

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

## Architecture

Read `PROMPT.md` for architecture decisions, the VSM mapping, and the runtime
evolution direction before making design choices.

## Task Source Ownership

Task status is project-owned. The CLI records run outcomes in `.vibe-loop/` but
does not advance task status. Workers must update the project's task source
(Markdown plan row, tracker, adapter) before reporting completion. This boundary
is intentional: agents and humans working without the CLI must be able to manage
the same backlog.

## Planning Hierarchy

Read `PROMPT.md` for the authoritative design direction. Read `docs/prd/` for
Level 2 contracts. Read `PLAN.md` for implementation slices. Code and tests are
the source of truth for implemented behavior — PRDs describe intended contracts.

## Worktree Discipline

Implementation agents should work in dedicated worktrees. Merge back to main
only after independent review. Do not assume you are working alone — inspect
worktree state before each slice and treat unexpected changes as external work.
