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

## Architecture Boundary: Skills vs CLI

Bundled skills (`src/vibe_loop/skills/*/SKILL.md`) are self-sufficient workflow
contracts. They must work when invoked directly by a slash command, prompt
template, or external orchestrator — not only under the `vibe-loop` CLI.

**Skills must not reference:**
- CLI commands: `vibe-loop report`, `main-integration`, `claim-workspace`
- CLI environment variables: `VIBE_LOOP_RUN_ID`, `VIBE_LOOP_TASK_ID`, etc.
- Supervisor-specific coordination protocols

**The runner injects CLI coordination at launch time** through
`CLI_WORKER_ADDENDUM` in `src/vibe_loop/runner.py`. That addendum is the only
place where CLI-specific worker contracts (reports, integration locking,
workspace claims, env vars) appear in the worker prompt.

When adding CLI coordination features (workspace claims, integration locking,
worker reports), update `CLI_WORKER_ADDENDUM` — not the skill files.

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
