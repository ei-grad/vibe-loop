# vibe-loop

`vibe-loop` is a small runner for one-slice AI coding loops. It selects one
unblocked task from a repository task source, locks it, runs an agent command
such as `codex exec '$vibe-loop <task_id>'`, captures logs, validates completion,
records local run metadata, and can repeat until no runnable tasks remain.

The runner is task-system agnostic. Repositories can expose tasks through a
Markdown plan table, command adapters, or later tracker-specific adapters. The
default adapter discovers Markdown files with tables using these columns:

```text
ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence
```

Without explicit configuration, Markdown discovery scores all `.md` files outside
ignored build/state directories and picks the best unambiguous parseable plan.
Set `task_source.plan_path` when a repo has multiple plausible plan files.

Runnable statuses default to `Active`, `Next`, and `Planned`. A task is runnable
when all listed dependencies are `Done` and no local lock exists.

## Installation

`vibe-loop` requires Python 3.11 or newer.

Install it as a standalone CLI with one of these commands:

```bash
uv tool install vibe-loop
pipx install vibe-loop
```

Install it into an existing Python environment with:

```bash
python -m pip install vibe-loop
```

For unreleased changes, install the current repository state directly from
GitHub:

```bash
uv tool install git+https://github.com/ei-grad/vibe-loop
```

## Quick Start

Create or point `vibe-loop` at a Markdown task table:

```markdown
| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Make one scoped change. | Tests pass. | Not run. |
```

Inspect runnable work:

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop next --repo .
```

Run one selected task with the configured agent command:

```bash
vibe-loop run-next --repo .
```

Use `--ask-agent` when task selection should be delegated to the configured
selection command after the mechanically safe candidate list is built:

```bash
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent
```

## Commands

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop tasks inspect QUERY-09 --repo .
vibe-loop tasks runnable --repo .
vibe-loop tasks locks --repo .
vibe-loop next --repo .
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent
vibe-loop install-skills --codex --claude
```

`--ask-agent` gives the agent the mechanically safe candidate list plus recent
`.vibe-loop/runs.jsonl` entries and log tails. The CLI still performs the lock
and completion checks itself.

`run-next` and `run-until-done` keep their result JSON on stdout. Run progress
and mirrored agent stdout are written to stderr, and full stdout/stderr streams
are captured in `.vibe-loop/runs/<run-id>.log`. Agent stderr is log-only by
default.

`vibe-loop tasks` without a subcommand remains a compatibility alias for
`vibe-loop tasks runnable`.

## Configuration

Optional `.vibe-loop.toml`:

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
command = "codex exec '$vibe-loop {task_id}'"
selection_command = "codex exec {prompt}"
forward_stderr = false

[task_source]
type = "markdown-plan"
# Optional. If omitted, vibe-loop discovers the best Markdown task table.
plan_path = "docs/PLAN.md"
plan_paths = ["docs/PLAN.md", "PLAN.md", "ROADMAP.md", "TODO.md"]
runnable_statuses = ["Active", "Next", "Planned"]

[completion]
commands = [
  "uv run python scripts/record_worklog.py --validate",
  "uv run python scripts/generate_gantt.py --coverage-check",
]
```

For command-backed task sources:

```toml
[task_source]
type = "command"
list = "my-task-tool list --json"
probe = "my-task-tool show {task_id} --json"
```

`list` must return either a JSON array or `{"tasks":[...]}`. Each task should
include `id`, `title`, `status`, `priority`, `dependencies`, `scope`,
`acceptance`, and `evidence` where available.

## Development

Install the repository tools with `uv`, then run the standard checks:

```bash
uv sync
uv run python -m unittest discover
uv build
uv run --with twine --no-project python -m twine check dist/*
```

Releases are built by `.github/workflows/release.yml`. The workflow uses PyPI
trusted publishing with the GitHub environments named `TestPyPI` and `PyPI`.
Run the workflow manually with target `TestPyPI` for a staging upload. To publish
to PyPI, push a tag named `v<version>` where `<version>` exactly matches
`project.version` in `pyproject.toml`, or dispatch the workflow from that tag
with target `PyPI`.

## Local State

Runner state is intentionally untracked:

```text
.vibe-loop/
  locks/
  runs/
  runs.jsonl
```

`runs.jsonl` is an append-only stream of versioned run result records. Project
worklogs should remain final evidence ledgers. Attempt logs and failed runs
belong in `.vibe-loop/`, not in project completion records.

## License

`vibe-loop` is licensed under the MIT License. See `LICENSE`.
