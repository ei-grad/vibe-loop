# vibe-loop

`vibe-loop` is a small runner for one-slice AI coding loops. It selects one
unblocked task from a repository task source, locks it, runs an agent command
such as `codex exec '$vibe-loop <task_id>'`, captures logs, validates completion,
records local run metadata, and can repeat until no runnable tasks remain.

The CLI is a supervisor, not a branch or worktree manager. The configured worker
agent owns branch/worktree setup, implementation, review, and any merge-to-main
workflow defined by the repository instructions. `vibe-loop` owns task
discovery, selection, locks, process execution, logs, completion checks, and run
records.

> [!WARNING]
> `vibe-loop` is in early development. It is not yet well tested or broadly
> reviewed, so treat it as experimental automation and run it only where failed
> commands or incorrect agent behavior cannot damage important work.

The bundled skills are the workflow layer. They can be used directly in Codex or
Claude without the CLI. When the CLI is used, it is a thin semi-deterministic
orchestrator above the finite `vibe-loop` skill: the CLI chooses tasks, creates
local locks, starts worker commands, captures logs, and records outcomes; the
worker agent follows the skill.

> [!NOTE]
> Direct skill use and `vibe-loop` CLI worker commands work best when routine
> edits, tests, reviews, and integration steps do not stop on permission prompts.
> Configure Codex or Claude with a thoroughly scoped allowlist and `dontAsk`
> mode.

> [!WARNING]
> If permission prompts are disabled, any Codex or Claude session launched
> directly or by a `vibe-loop` CLI worker command MUST run in isolation, such as
> a Docker container or VM, with only the required repository, tools, network
> access, and credentials available.

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

## Relationship to ralphex

`vibe-loop` is inspired by
[umputun/ralphex](https://github.com/umputun/ralphex): the useful core idea is a
repeatable autonomous loop that gives coding agents bounded tasks, validates the
result, records progress, and avoids relying on one long interactive chat.

The difference is in the workflow `vibe-loop` is built around:

- `ralphex` is plan-file centered; `vibe-loop` is task-source agnostic. It can
  discover Markdown task tables, use explicit plan paths, or read tasks through
  command adapters.
- `ralphex` conventionally runs a dedicated plan through task and review phases;
  `vibe-loop` runs one repository backlog slice at a time and is designed to
  merge reviewed slices back to `main` frequently.
- `ralphex` has its own plan format under `docs/plans/`; `vibe-loop` is intended
  to fit existing project planning and worklog conventions instead of requiring a
  dedicated plan directory.
- `vibe-loop` treats agent execution as configuration. Worker and selection
  commands are template strings resolved from explicit config or supported CLI
  detection rather than a hard dependency on one agent CLI.
- `vibe-loop` packages the workflow skills as reusable instructions. They remain
  useful on their own; the CLI adds task selection, locking, execution, logging,
  and result-recording around the finite `vibe-loop` skill.
- `vibe-loop` keeps workers finite and leaves branch/worktree management to the
  agent. The runner/supervisor owns scheduling, locks, logs, and result
  collection; planned parallel mode keeps the same boundary instead of becoming a
  central merge queue.
- `vibe-loop` keeps attempt state, locks, run logs, and recent run metadata under
  `.vibe-loop/`, leaving project worklogs as final evidence records.

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

## Skills

The package includes two installable skills:

- `vibe-loop`: one coherent bounded slice. The agent inspects the task, edits,
  verifies, asks for independent review when available, commits, integrates to
  `main` when policy permits, cleans up, and stops. Once invoked directly or by
  a CLI worker command, the agent is expected to follow the finite loop rather
  than treating the skill as optional guidance.
- `infinite-vibe-loop`: unattended continuation across finite slices. Each
  slice follows the finite `vibe-loop` discipline; after cleanup/status, the
  agent chooses conservative next work, reports blocked paths, and continues
  until explicitly stopped or the session ends.

Install them into Codex and/or Claude with:

```bash
vibe-loop install-skills --codex --claude
```

The skills do not require the CLI. You can invoke them directly in an agent
session for manual bounded or unattended work. The CLI exists for cases where a
repository already has a task source and you want repeatable orchestration:
mechanical candidate discovery, locks, configured worker commands, run logs,
completion checks, and local run metadata.

## Commands

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop tasks inspect QUERY-09 --repo .
vibe-loop tasks runnable --repo .
vibe-loop tasks locks --repo .
vibe-loop tasks configure --repo . --json
vibe-loop next --repo .
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent --jobs 2
vibe-loop workers --repo .
vibe-loop main-integration status --repo .
vibe-loop main-integration acquire --repo . --run-id ... --task-id ...
vibe-loop main-integration release --repo . --run-id ... --task-id ...
vibe-loop report --repo . --run-id ... --task-id ... --status completed --commit ...
vibe-loop install-skills --codex --claude
```

`--ask-agent` gives the agent the mechanically safe candidate list plus recent
`.vibe-loop/runs.jsonl` entries and log tails. The CLI still performs the lock
and completion checks itself.

`run-next` and `run-until-done` keep their result JSON on stdout. Run progress
and mirrored agent stdout are written to stderr, and full stdout/stderr streams
are captured in `.vibe-loop/runs/<run-id>.log`. Agent stderr is log-only by
default. Each run result includes a `run_id` for vibe-loop locks, logs, and run
records. If the worker emits a Codex-style `session id: ...` line on stdout or
stderr, `session_id` stores that native worker id and `session_id_source` is
`native:stdout` or `native:stderr`. If no native session id is observed,
`session_id` falls back to `run_id` and `session_id_source` is
`fallback:run_id`.

`run-until-done` is serial by default. Pass `--jobs N` to let the supervisor
keep up to `N` finite worker commands active at once; each worker still gets its
own task lock, run id, and log path. `run-next` always runs a single worker.

Workers can explicitly report their final status while the supervisor run is
active:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \
  --task-id "$VIBE_LOOP_TASK_ID" --status blocked --commit HEAD \
  --message "waiting on reviewer" --metadata-json '{"reason":"review"}'
```

Report statuses are `completed`, `blocked`, `failed`, and `unknown`. Matching
report records are authoritative; without a report, the supervisor falls back to
exit status, completion checks, task probing, and main-branch change heuristics.

Workers that are about to refresh, verify, fast-forward merge to `main`, and
immediately verify `main` can use the advisory `main-integration` lock to
serialize that final critical section:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

`main-integration status` shows the current holder, process state, and stale
reason when the recorded same-host process is missing. Stale locks are reported
conservatively; a waiter does not steal them automatically. By default,
`acquire` records the active task lock's worker process for the same run and
task. Pass `--pid` only when a wrapper needs to record a different long-lived
owner process or no active task lock exists.

Worktree and branch handling are intentionally outside the CLI runtime. Put that
policy in the repository instructions or in the configured agent command; keep
`.vibe-loop/` for locks, logs, and run metadata.

`vibe-loop tasks` without a subcommand remains a compatibility alias for
`vibe-loop tasks runnable`.

## Configuration

Optional `.vibe-loop.toml`:

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
# Optional when Codex or Claude is available on PATH.
command = "codex exec '$vibe-loop {task_id}'"
selection_command = "codex exec {prompt}"
forward_stderr = false

[task_source]
type = "markdown-plan"
# Optional. If omitted, vibe-loop discovers the best Markdown task table.
plan_path = "PLAN.md"
plan_paths = ["PLAN.md", "docs/PLAN.md", "ROADMAP.md", "TODO.md"]
runnable_statuses = ["Active", "Next", "Planned"]

[completion]
commands = [
  "uv run python scripts/record_worklog.py --validate",
  "uv run python scripts/generate_gantt.py --coverage-check",
]
```

Agent commands are resolved independently. Explicit `.vibe-loop.toml` values
remain authoritative. Omitted worker and selection commands use a deterministic
Codex-first policy:

- Codex only: `codex exec '$vibe-loop {task_id}'` and `codex exec {prompt}`.
- Claude only: `claude -p '$vibe-loop {task_id}'` and `claude -p {prompt}`.
- Codex and Claude: Codex is selected for omitted commands.

When neither supported CLI is available, agent-using commands fail with a
diagnostic that points to installation or explicit config.

Configure Claude prompt mode explicitly when that is the worker or selector you
want to run regardless of what else is installed:

```toml
[agent]
command = "claude -p '$vibe-loop {task_id}'"
selection_command = "claude -p {prompt}"
forward_stderr = false
```

`agent.command` receives `{task_id}` for the selected task and `{run_id}` for
the supervisor run. Worker commands also receive `VIBE_LOOP_RUN_ID`,
`VIBE_LOOP_TASK_ID`, `VIBE_LOOP_REPO`, and `VIBE_LOOP_LOG` in their environment.
`selection_command` receives a shell-quoted `{prompt}` containing the
dependency-ready candidate list and recent run context, and should print JSON
containing `task_id`.

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

Generated task-source discovery is an explicit configuration flow. It uses the
bounded repo-local evidence collector, asks the resolved `agent.selection_command`
for strict JSON, validates the result, and writes a cache under the configured
state directory:

```text
.vibe-loop/generated-task-source.json
```

The cache path follows `state_dir`; `.vibe-loop/` is only the default. Generated
profiles are versioned JSON parser descriptions with source fingerprints,
redacted provenance, confidence, and a degradation status such as `profile`,
`planning_only`, `needs_input`, `unavailable`, or `rejected`. The cache records
agent identity and command source, not raw command strings. Read-only commands
must not launch an agent to create or repair that cache; explicit configure or
refresh commands own agent invocation.

Create or refresh the cache explicitly:

```bash
vibe-loop tasks configure --repo . --json
```

Malformed, low-confidence, unsupported, or incomplete agent output is stored as
an explicit degraded cache record rather than changing runnable task behavior.

Explicit `.vibe-loop.toml` task-source settings stay authoritative. User-written
command adapters and explicit source paths disable generated discovery for the
active source. Defaults do not count as explicit settings, and non-source
settings such as `task_source.runnable_statuses` override the matching generated
field without disabling the generated parser. Generated cache records cannot
contain executable adapters such as `type = "command"`, `list`, `next`, `probe`,
or generic command fields. Add explicit `[task_source]` settings to override
cached generated behavior.

See `docs/generated-task-discovery.md` for the generated profile schema,
precedence rules, stale-cache behavior, and degradation states.

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

Active task locks store the worker command `pid`, `task_id`, `run_id`, log path,
start time, base `main` revision, host, and resolved command. `vibe-loop
workers` reconstructs the active view from those lock files plus `runs.jsonl`,
then marks same-host locks with missing worker processes, missing worker PIDs,
or incomplete metadata as stale without reading raw logs. The PID is the
immediate configured command process started by the runner; deeper process
identity checks are left to the later watchdog work.

The `main-integration.lock` entry is a separate advisory lock for worker-owned
final integration. Its metadata records the owner task, run id, host, pid, and
start time. It is visible through `vibe-loop main-integration status` rather
than `vibe-loop workers`; stale status is diagnostic only and does not grant a
new holder permission to take over automatically.

`runs.jsonl` is an append-only stream of versioned run result records. Run
records include the vibe-loop `run_id`, the resolved worker `session_id`, the
`session_id_source`, the `agent_command_source` used for the worker command,
the `agent_selection_command_source`, and the default agent policy source used
when commands are auto-resolved.
Project worklogs should remain final evidence ledgers. Attempt logs and failed
runs belong in `.vibe-loop/`, not in project completion records.

Branches and worktrees created by worker agents are not tracked as runner state.
The agent workflow that creates them is responsible for refresh, review, merge,
and cleanup according to repository policy.

## License

`vibe-loop` is licensed under the MIT License. See `LICENSE`.
