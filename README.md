# vibe-loop

`vibe-loop` is a small execution engine for one-slice AI coding loops. It
selects one unblocked task from a repository task source, locks it, runs an
agent command such as `codex exec '$vibe-loop <task_id>'`, captures logs,
validates completion, records local run metadata, and can repeat until no
runnable tasks remain.

The CLI is a supervisor, not a branch or worktree manager. It owns task
discovery, selection, locks, process execution, logs, completion checks, and
run records. The configured worker agent owns branch/worktree setup,
implementation, review, and any merge-to-`main` workflow defined by the
repository instructions.

The runtime is built around three bundled skills — see [Skills](#skills) — which
also work on their own in Codex or Claude without the CLI.

> [!WARNING]
> `vibe-loop` is in early development. It is not yet well tested or broadly
> reviewed, so treat it as experimental automation and run it only where failed
> commands or incorrect agent behavior cannot damage important work.

## Installation

`vibe-loop` requires Python 3.11 or newer.

Install it as a standalone CLI:

```bash
uv tool install vibe-loop
pipx install vibe-loop
```

Install it into an existing Python environment:

```bash
python -m pip install vibe-loop
```

For unreleased changes, install the current repository state from GitHub:

```bash
uv tool install git+https://github.com/ei-grad/vibe-loop
```

## Quick Start

Point `vibe-loop` at a supported task source. For a small repository, this
Markdown table is enough:

```markdown
| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Make one scoped change. | Tests pass. | Not run. |
```

For an existing planning format, generate a repo-specific profile instead:

```bash
vibe-loop tasks configure --repo . --dry-run --json   # review candidate
vibe-loop tasks configure --repo . --json             # activate
```

Inspect runnable work, then run one selected task with the configured agent:

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop next --repo .
vibe-loop run-next --repo .
```

Add `--ask-agent` to delegate selection to the configured selection command
after the mechanically safe candidate list is built:

```bash
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent
```

> [!NOTE]
> Worker commands and direct skill use work best when routine edits, tests,
> reviews, and integration steps do not stop on permission prompts. Configure
> Codex or Claude with a thoroughly scoped allowlist and `dontAsk` mode.

> [!WARNING]
> With permission prompts disabled, any Codex or Claude session — launched
> directly or by a worker command — MUST run in isolation (Docker container or
> VM) with only the required repository, tools, network access, and credentials
> available.

## Skills

The package includes three installable skills:

- **`vibe-loop`** — one coherent bounded slice. The agent inspects the task,
  edits, verifies, asks for independent review when available, commits,
  integrates to `main` when policy permits, cleans up, and stops.
- **`infinite-vibe-loop`** — unattended continuation across finite slices. After
  each slice it chooses conservative next work, reports blocked paths, and
  continues until stopped.
- **`orchestrated-vibe-loop`** — multi-agent execution where the main agent keeps
  orchestration state and delegates exploration, implementation, and independent
  review without doing the code or review work itself.

Install them into Codex and/or Claude:

```bash
vibe-loop install-skills --codex --claude
```

The skills do not require the CLI; you can invoke them directly for manual
bounded or unattended work. The CLI exists when a repository already has a task
source and you want repeatable orchestration: candidate discovery, locks,
configured worker commands, run logs, completion checks, and run metadata.

## Commands

### Tasks

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop tasks inspect QUERY-09 --repo .
vibe-loop tasks runnable --repo .
vibe-loop tasks locks --repo .
vibe-loop tasks configure --repo . --dry-run --json
vibe-loop tasks configure --repo . --force-refresh --json
vibe-loop tasks configure --repo . --promotion-toml
vibe-loop next --repo .
```

`vibe-loop tasks` without a subcommand is a compatibility alias for
`vibe-loop tasks runnable`.

### Run

```bash
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent --jobs 2
```

`--ask-agent` hands the agent the mechanically safe candidate list plus recent
`.vibe-loop/runs.jsonl` entries and log tails. The CLI validates returned IDs
against current unlocked candidates, rejects duplicates and unknown or locked
tasks, and falls back to deterministic ready order before spawning. When task
sources declare `resources` or `paths`, the scheduler also rejects overlapping
conflict domains; undeclared tasks are not paired once conflict-domain
scheduling is active.

`run-next` always runs a single worker. `run-until-done` is serial by default;
`--jobs N` keeps up to `N` workers active, each with its own task lock, run id,
and log path. Two independent stop limits bound a `run-until-done` session:

| Flag | Caps | Counts | Default |
| --- | --- | --- | --- |
| `--max-slices N` | total dispatched slices | every attempt, any outcome | `0` (unlimited) |
| `--max-tasks N` | completed slices | only `completed` results | `0` (unlimited) |

Whichever limit is reached first ends the loop. Under `--jobs`, the scheduler
never dispatches more in-flight work than the remaining `--max-tasks` budget
allows, so completed runs do not overshoot `N`.

`run-next` and `run-until-done` keep their result JSON on stdout; progress and
mirrored agent stdout go to stderr, and full streams are captured in
`.vibe-loop/runs/<run-id>.log`. Each result includes a `run_id`. If the worker
emits a Codex-style `session id: ...` line, `session_id` stores that native id
(`session_id_source` = `native:stdout`/`native:stderr`); otherwise it falls back
to `run_id` (`fallback:run_id`).

### Worker-side commands

Workers can report status, claim a workspace, and serialize final integration
while the supervisor run is active.

```bash
# Report final status (authoritative for classifying that run).
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \
  --task-id "$VIBE_LOOP_TASK_ID" --status blocked --commit HEAD \
  --message "waiting on reviewer" --metadata-json '{"reason":"review"}'

# Make branch/worktree ownership visible (advisory metadata only).
vibe-loop worker claim-workspace --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --branch "$BRANCH" --worktree "$WORKTREE"

# Serialize the refresh/verify/fast-forward-merge critical section.
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" --wait --timeout 300
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
vibe-loop main-integration status --repo .
```

Report statuses are `completed`, `blocked`, `failed`, and `unknown`. Reports
classify a run but are not task-source mutations and do not mark a task `Done`;
before reporting `completed`, the worker should update the active task source.
Without a report, the supervisor falls back to exit status, completion checks,
task probing, and main-branch change heuristics.

`claim-workspace` requires a matching active task lock and verifies the worktree
is on the requested branch; it records branch, worktree, base commit, HEAD, and
dirty state, and never creates, deletes, resets, merges, or cleans up
branches/worktrees. `main-integration acquire --wait --timeout N` waits for a
live or unknown holder; stale locks are reported, never stolen. If the active
task lock has a workspace claim, acquisition is blocked when the claim's
diagnostics make integration unsafe. Worktree and branch handling stay outside
the CLI runtime — put that policy in repository instructions or the agent
command.

### Status and diagnostics

```bash
vibe-loop workers --repo . --json
vibe-loop runs list --repo .
vibe-loop runs inspect <run-id> --repo .
vibe-loop doctor --repo . --json
vibe-loop specs check --repo . --json
vibe-loop planning artifacts --repo . --check
vibe-loop planning benchmark-duration --repo . --check
vibe-loop --version
```

`runs list` groups records by run id and shows the latest status plus log path;
`runs inspect <run-id>` prints the detailed record history. `vibe-loop
--version` prints the package version; editable source-tree and non-tag Git
installs append `(git <short-sha>)`.

### Evals

```bash
vibe-loop eval local-demo --repo . --trials 3 --agent-command '*=codex exec {prompt}'
vibe-loop eval release-gate --repo . --overwrite --record-output .vibe-loop/release-readiness.json
vibe-loop eval benchmark --adapter manifest --manifest path/to/benchmark.json
```

`eval local-demo` materializes fresh bundled fixture repositories, runs the same
prompt across selected skill conditions and agent commands, and emits
`aggregate.json`/`aggregate.md` with a `skill_quality` section that separates
task failures from workflow-contract failures and compares against any previous
aggregate.

`eval release-gate` is the bundled skill release-readiness check. Without
`--aggregate`/`--dry-run` it runs a 16-trial release matrix and writes a
`skill_release_readiness` record. It requires required trials to pass and blocks
unresolved `workflow_contract_regression` findings; a regression is accepted only
when parked with a task id (e.g.
`--parked-regression condition_comparison:vibe_loop=EVAL-99`). `eval benchmark`
runs an explicit external smoke manifest through the same paired-condition
harness without claiming public leaderboard comparability. See
[`docs/skill-evaluation-strategy.md`](docs/skill-evaluation-strategy.md) and
[`docs/external-benchmark-fit.md`](docs/external-benchmark-fit.md).

## Autopilot

Autopilot supervises one repository (or a registry of several) and drives
`run-until-done` cycles. It never deletes worktrees, resets branches, steals
locks, or mutates tracked project files on its own. See
[`docs/prd/autopilot.md`](docs/prd/autopilot.md) for the full contract.

```bash
vibe-loop autopilot status --repo . --json
vibe-loop autopilot run --repo . --once
vibe-loop autopilot run --repo . --interval 60 --max-cycles 10 --jobs 2
vibe-loop autopilot projects register --repo . --name my-project
vibe-loop autopilot projects status --json
vibe-loop autopilot tui --registry
vibe-loop autopilot webui --registry --port 8765
vibe-loop autopilot wait --pid 12345 --cycle-schedule 1800 --json
```

**`status`** collects a read-only snapshot — queue counts, runnable tasks, active
workers, stale locks, workspace diagnostics, git refs/dirty state, the
main-integration lock, supervisor state, blockers, and the last cycle. It never
starts a worker or mutates state. The `--json` `ProjectStatus` payload is the
machine-readable boundary shared by every surface below.

**`run`** is a foreground supervisor that launches `run-until-done` as a child
and append-records one `autopilot_cycle` per iteration. A cycle is blocked
(never force-recovered) when preflight diagnostics are unsafe: dirty repo, stale
locks, unsafe workspace diagnostics, missing task source, or an unavailable
agent command. `--once` runs one cycle. Without `--interval`, it drains runnable
work and exits when a cycle is idle or blocked; with `--interval N` it stays
resident, sleeping `N` seconds between cycles until `--max-cycles` or an
interrupt. `--jobs`, `--ask-agent`, `--continue-on-failure`, `--max-slices`, and
`--max-tasks` are forwarded to each child; `--min-ready` sets the minimum
runnable depth required before launching. A single supervisor lock prevents
duplicates; Ctrl-C terminates the in-flight child and releases the lock.

**`projects`** manages an optional multi-project registry (`register`, `list`,
`remove`, `status`). It records only repo paths and display names in a small JSON
file (default `~/.vibe-loop/projects.json`, `--registry` to override); each
project keeps its own state directory. `projects status [--json]` returns one
aggregate entry per repo, and a repo that cannot be read becomes an isolated
error entry so one broken project never hides the others.

**`tui`** opens a read-only [Textual](https://textual.textualize.io/) dashboard
over the status API. It is an optional extra: `pip install vibe-loop[tui]` (or
`uv add textual`); without `textual` it prints an install hint and exits, keeping
the CLI dependency-free. **`webui`** serves the same read-only status from a
standard-library web server, binding to `127.0.0.1:8765` by default
(`--host`/`--port`). It answers only `GET` (status page + `GET /api/status`) and
rejects writes; `--host 0.0.0.0` exposes status to the network, so use it only on
a trusted host. Both take `--repo` (single, default) or `--registry [PATH]`.

**`wait`** blocks until a watched process exits or a wall-clock deadline arrives,
so an unattended steward can sleep between cycles. `--pid` (repeatable) wakes on
process exit; `--cycle-schedule [SECONDS]` wakes at the next UTC `*/SECONDS`
boundary (default 1800s); `--deadline` takes an explicit ISO-8601 UTC time;
`--mode all` waits for every PID. It prints `wake_reason` (`pid`, `all_complete`,
or `deadline`) with a summary. It watches OS processes and the clock only —
agent-specific wake signals stay in the agent environment.

## Configuration

All configuration is optional. A typical `.vibe-loop.toml`:

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
# Optional when kind = "auto" and Codex or Claude is available on PATH.
kind = "auto"
command = "codex exec {prompt}"
selection_command = "codex exec {prompt}"
forward_stderr = false   # agent stderr is log-only by default; set true to mirror it

[task_source]
type = "markdown-plan"
# Set source keys only when you want to pin Markdown discovery.
plan_path = "PLAN.md"
plan_paths = ["PLAN.md", "docs/PLAN.md", "ROADMAP.md", "TODO.md"]
runnable_statuses = ["Active", "Next", "Planned"]

[completion]
commands = [
  "uv run python scripts/record_worklog.py --validate",
  "uv run python scripts/generate_gantt.py --coverage-check",
]

[supervision]
max_restarts = 3
cooldown_seconds = 30.0

[locks]
type = "directory"
# lease_seconds = 300   # locks go stale after this many seconds without a heartbeat

[autopilot]
# Defaults for `autopilot run`; explicit CLI flags override these.
# jobs = 2
# interval_seconds = 60.0
# min_ready = 1
require_clean_repo = true   # set false to let a dirty tree run a cycle
# Optional user-authored maintenance hooks, redacted in status/doctor JSON.
# A failing health command blocks the launch; planning runs when the runnable
# queue is below min_ready; summary runs after a launch; troubleshoot after a
# failed child. Generated profiles can never introduce them.
# health_command = "scripts/health.sh"
# summary_command = "scripts/summary.sh"
# troubleshoot_command = "scripts/troubleshoot.sh"
# planning_command = "scripts/plan.sh"

[planning_analytics]
schedule_policy = "current-runner-parity"
subject_matching = "diagnostic"
# worklog_command = "my-worklog export --jsonl"   # optional collector adapter

[planning_analytics.duration_model]
name = "robust-duration-baseline-v1"
group_min_sample_count = 2
similarity_min_score = 0.35
similarity_max_examples = 3
similarity_blend_weight = 0.25
fallback_minutes = 60

[planning_analytics.outputs]
# Omitted paths write under <state_dir>/planning-analytics.
# timeline_json = "docs/planning/timeline.json"
# gantt_html = "docs/planning/gantt.html"
# benchmark_json = "docs/planning/duration-benchmark.json"
# benchmark_markdown = "docs/planning/duration-benchmark.md"
```

When `--repo` points at a Git linked worktree without its own `.vibe-loop.toml`,
`vibe-loop` falls back to the main worktree's config (warning on stderr).
Runtime state, locks, logs, caches, and analytics outputs still live under the
invoked `--repo` worktree.

### Agent command and prompt dialect

The executable command and the worker prompt dialect resolve independently.
`agent.command` and `agent.selection_command` are shell templates; `agent.kind`,
`agent.prompt_dialect`, and `agent.skill_ref_prefix` control how the prompt
references the bundled skill. Explicit `.vibe-loop.toml` command values stay
authoritative — no generated profile can introduce executable commands.

| `kind` | Behavior |
| --- | --- |
| `auto` (default) | Omitted commands use deterministic supported-agent detection. |
| `codex` | Codex-style worker prompts with `$vibe-loop`. |
| `claude` | Claude-style worker prompts with `/vibe-loop`. |
| `custom` | Explicit templates; requires `prompt_dialect` or `skill_ref_prefix`. |

Under `kind = "auto"`, omitted commands follow a Codex-first policy: Codex only →
`codex exec {prompt}`; Claude only → `claude -p {prompt}`; both installed → Codex.
When neither CLI is available, agent-using commands fail with a diagnostic.

Set Claude (or a custom launcher) explicitly when that is the worker you want
regardless of what else is installed — the dialect comes from `kind`, not from
parsing the command string:

```toml
[agent]
kind = "claude"
command = "CLAUDE_HOME=.claude claude -p {prompt}"
selection_command = "CLAUDE_HOME=.claude claude -p {prompt}"

[agent]
kind = "custom"
command = "my-worker --prompt {prompt}"
selection_command = "my-selector --prompt {prompt}"
prompt_dialect = "claude"   # maps to /vibe-loop; "codex" maps to $vibe-loop
# skill_ref_prefix = "/"     # equivalent low-level form ($ or /)
```

`kind = "custom"` without `prompt_dialect` or `skill_ref_prefix` is a
configuration error, not an implicit Codex default. Legacy configs that set
`agent.command` without `agent.kind` still run; set one of the dialect fields to
clear the migration diagnostic.

`agent.command` receives `{task_id}`, `{run_id}`, and a shell-quoted `{prompt}`
(skill reference, normalized task context, CLI addendum). Workers also get
`VIBE_LOOP_RUN_ID`, `VIBE_LOOP_TASK_ID`, `VIBE_LOOP_REPO`, and `VIBE_LOOP_LOG` in
their environment; `selection_command` receives a `{prompt}` with the candidate
list and recent run context. Single-task selection prints JSON with `task_id`;
batch selection prints `task_ids`. If a task has traceability metadata,
`agent.command` must include `{prompt}` — task-id-only templates fail fast.

### Locks

Locks default to directory locks under `<state_dir>/locks`. Repos that
coordinate through an external service can opt into command-backed locks:

```toml
[locks]
type = "command"
acquire_command = "my-lock-tool acquire --json"
release_command = "my-lock-tool release --json"
status_command = "my-lock-tool status --json"
list_command = "my-lock-tool list --json"
```

Lock commands run from the repository root and receive
`VIBE_LOOP_LOCK_OPERATION`, `VIBE_LOOP_LOCK_TASK_ID`, `VIBE_LOOP_LOCK_RUN_ID`,
`VIBE_LOOP_LOCK_ROOT`, and `VIBE_LOOP_LOCK_METADATA_JSON`. `acquire_command`
handles both `acquire` and `update`, returning `{"acquired": true|false,
"metadata": {...}}`. Release returns `{"released": true|false}`; status returns
`{"locked": true|false, ...}`; list returns a JSON array or `{"locks": [...]}`.
Once `type = "command"` is set, lock failures fail closed instead of falling back
to directory locks. When `locks.lease_seconds` is set, acquired locks carry
`lease_seconds`, `heartbeat_at`, and a fencing token; workers refresh with
`vibe-loop worker heartbeat`, and stale holders are rejected on a generation
mismatch.

### Task sources

The runner is task-system agnostic. Without explicit source configuration,
read-only commands inspect `<state_dir>/generated-task-source.json`: a fresh
`profile` cache becomes the active source; a degraded cache (`planning_only`,
`needs_input`, `unavailable`, `rejected`) is diagnostic only and may continue to
Markdown fallback; a stale or invalid cache blocks fallback and points to `tasks
configure`. With no cache, the Markdown fallback scores `.md` files outside
ignored directories and picks the best unambiguous table. Set
`task_source.plan_path` to pin a specific Markdown source.

Runnable statuses default to `Active`, `Next`, and `Planned`. A task is runnable
when all dependencies are `Done` and no local lock exists. The active task
source is the source of truth for the dependency graph; `.vibe-loop/runs.jsonl`
records attempts but does not advance task status. A completed worker must update
the task source itself (or its command-backed adapter must report the task as
non-runnable) before the next pass — `vibe-loop` deliberately keeps no private
task-state channel, so agents and humans working without the CLI advance the
same backlog.

Setting any explicit source key — `type`, `plan_path`, `plan_paths`, `profile`,
`list`, `next`, `probe` — disables generated cache as the active source.
Non-source settings such as `runnable_statuses` still override matching generated
fields without disabling the generated parser.

**Command-backed sources** read tasks from an issue tracker or custom tool:

```toml
[task_source]
type = "command"
list = "my-task-tool list --json"
probe = "my-task-tool show {task_id} --json"
```

`list` returns a JSON array or `{"tasks":[...]}`. Each task should include `id`,
`title`, `status`, `priority`, `dependencies`, `scope`, `acceptance`, and
`evidence`. Optional `resources` and `paths` arrays declare conflict domains for
parallel scheduling: resource names match exactly, path locks use repo-relative
paths and conflict when one is an ancestor of another. Omitted/`null` arrays are
undeclared; empty arrays explicitly declare no domains. Tasks may also carry
optional traceability fields — `requirement_ids`, `spec_paths`, `design_refs`,
`approval_state`, `source_fingerprints` — emitted in task JSON, analytics,
promotion, and worker prompts when present.

**Spec gates** (read-only diagnostics by default). `doctor` and `specs check`
report unapproved tasks, stale fingerprints, missing requirement IDs, and
completed traceable tasks without evidence. Repos that require current approved
specs can opt into execution gates:

```toml
[specs]
require_approved = true
require_current_fingerprints = true
require_requirement_coverage = true
require_completion_evidence = true
approved_states = ["approved"]
override_commands = ["make specs-override"]
```

The `require_*` settings gate execution commands (`run-next`,
`run-until-done`); read-only inspection stays available. Override commands are
reported as repository-owned recovery guidance and are never run automatically.

**Ralphex-style Markdown plans:**

```toml
[task_source]
type = "ralphex-markdown"
plan_path = "docs/plans/checkout.md"
# plan_paths = ["docs/plans/checkout.md", "docs/plans/refund.md"]
```

The parser reads `### Task N:` and `### Iteration N:` headings, derives `Done`
only when every checkbox in a block is checked (`Planned` otherwise), and uses
stable repo-relative IDs such as `docs.plans.checkout:task-1`. A `## Validation
Commands` section is copied into each task's evidence. Conflict domains can be
declared per task, in a plan-level `## Conflict Surface` section, or inline:

```markdown
### Task 1: Add checkout API
- [ ] Add checkout handler
- Resources: api, checkout
- Paths: src/checkout.py, tests/test_checkout.py
- Conflict Surface: resources: api, checkout; paths: src/checkout.py
```

In a plan-level `## Conflict Surface` section, unlabeled bullets that look like
repo-relative paths (including root files like `Makefile`) are treated as path
domains. Use `Resources: none` / `Paths: none` to declare an empty domain; blank
or absent labels leave it unknown.

**Spec-driven presets** for common task artifacts, instead of a command adapter:

```toml
[task_source]
type = "spec-kit"   # specs/*/tasks.md, .specify/specs/*/tasks.md
# type = "kiro"     # .kiro/specs/*/tasks.md
# type = "openspec" # openspec/changes/*/tasks.md
```

`spec-kit` reads checkbox lists with `T001`-style IDs, optional `[P]`/story
markers, inline `(depends on T001)`, and nested `Depends`/`Acceptance`/`Evidence`
labels. `kiro` and `openspec` read numbered checkbox lists (`1.`, `1.2`) with the
same patterns. Checked → `Done`, unchecked → `Planned`, `[-]`/`[~]` → `Active`.
All three also read nested `Conflict Resources`/`Conflict Paths` labels. When
several files are exposed, IDs are prefixed with the spec/change directory (e.g.
`001-login:T001`, `checkout-mutation:1.2`); missing files, missing or duplicate
IDs, and invalid dependency syntax fail visibly. Markdown profiles can map the
same traceability and conflict-domain fields:

```toml
[task_source.profile.fields.resources]
column = "Resources"
none_values = ["none"]

[task_source.profile.fields.paths]
column = "Paths"
none_values = ["none"]
```

**Generated discovery** asks the resolved `agent.selection_command` for a strict
JSON parser profile, validates it, and caches it under `state_dir`:

```bash
vibe-loop tasks configure --repo . --dry-run --json       # review candidate, no write
vibe-loop tasks configure --repo . --json                 # create/repair active cache
vibe-loop tasks configure --repo . --force-refresh --json # regenerate a fresh cache
vibe-loop tasks configure --repo . --promotion-toml       # print committable [task_source] TOML
```

Generated caches are versioned JSON with fingerprints, redacted provenance,
confidence, and a degradation status; they record agent identity and command
source, never raw command strings, and can never contain executable adapters or
lock backends. Read-only commands never launch an agent to create or repair the
cache. `--promotion-toml` prints a non-executable `type = "markdown-profile"`
snippet so a repo can make discovery explicit. See
[`docs/generated-task-discovery.md`](docs/generated-task-discovery.md) for the
schema, precedence, stale-cache behavior, and degradation states.

## Planning Analytics

Planning analytics is a reporting boundary over normalized tasks, run records,
optional worklogs, and bounded git metadata. It does not affect task selection,
locks, or completion classification. Artifacts default to
`<state_dir>/planning-analytics`, so analytics never mutate repository docs
unless you opt in with explicit output paths.

```bash
vibe-loop planning artifacts --repo .            # timeline JSON + static Gantt HTML
vibe-loop planning artifacts --repo . --check    # rebuild and fail if stale/missing
vibe-loop planning benchmark-duration --repo . --check
```

Coverage uses authoritative mappings only — task-source completion state, worker
reports, optional worklogs, explicit commit references, and `Plan-Item:` commit
trailers; subject matching, branch names, and raw logs are diagnostic. Projected
timelines default to `current-runner-parity` (the runner's dependency readiness
and deterministic order); `lightmetrics-parity` is available for comparison.
Duration estimates use robust historical baselines from completed spans.
`vibe-loop doctor` reports analytics readiness without running a collector. See
[`docs/planning-analytics.md`](docs/planning-analytics.md) for the full contract.

## Local State

Runner state is intentionally untracked:

```text
.vibe-loop/
  locks/
  runs/
  runs.jsonl
```

**Task locks** store the worker `pid`, `task_id`, `run_id`, log path, start time,
base `main` revision, host, resolved command, and optional lease metadata.
`vibe-loop workers` reconstructs the active view from lock files plus
`runs.jsonl` and marks same-host locks with missing processes/PIDs, expired
leases, or incomplete metadata as stale — without reading raw logs. When a worker
claims its workspace, the lock also stores a `workspace` object (branch,
worktree, base commit, HEAD, current branch, dirty state); `workers --json` adds
read-only `workspace_git_state` and `workspace_diagnostics` that flag missing or
duplicate worktrees, already-merged branches, dirty worktrees, and stale
mismatches with manual recovery hints. `doctor --json` summarizes the same
diagnostics. Neither command deletes locks, branches, or worktrees.

**`main-integration.lock`** is a separate advisory lock for worker-owned final
integration (owner task, run id, host, pid, start time), visible through
`vibe-loop main-integration status`. Stale status is diagnostic only.

**`runs.jsonl`** is an append-only stream of versioned run records: result
records carry the `run_id`, `started_at`, resolved `session_id` and source, the
agent command/selection sources, prompt dialect and skill reference sources, and
the default agent policy source. Lifecycle records (`run_started`,
`agent_context_observed`, `run_state_transition`) expose the same anchor plus
bounded trailer-ready context — task IDs for `Plan-Item`/`Run-Id`/`Session-Id`,
agent kind, prompt dialect, and model provider/ID/reasoning effort when the agent
emits them. `vibe-loop` does not own commit hooks; repository tooling decides
whether to persist this context into project history. Project worklogs should
remain final evidence ledgers — attempt logs and failed runs belong in
`.vibe-loop/`, not in completion records.

## Spec-Driven Workflow Execution

`vibe-loop` can sit underneath spec-driven development tools as the task
execution layer. Tools such as [Spec Kit](https://github.com/github/spec-kit),
[Kiro](https://kiro.dev/docs/specs/), and [OpenSpec](https://openspec.dev/) own
intent — requirements, design docs, proposals, task lists, approvals.
`vibe-loop` owns repeatable execution: it consumes the task layer, schedules
runnable slices, launches finite workers, captures logs, enforces locks, and
records completion. A spec or PRD is not treated as proof of implementation
unless a task row, worker report, commit reference, test, review, or other
explicit evidence links the contract to completed work.

This repository uses a three-level planning model: `PROMPT.md` (philosophy,
architecture boundaries, PRD-writing rules) → `docs/prd/` (stable `PRD-*`
contracts) → `PLAN.md` (runnable slices with permanent task IDs).

## Relationship to ralphex

`vibe-loop` is inspired by
[umputun/ralphex](https://github.com/umputun/ralphex): a repeatable autonomous
loop that gives coding agents bounded tasks, validates results, and records
progress instead of relying on one long interactive chat. The main differences:

- `ralphex` is plan-file centered; `vibe-loop` is task-source agnostic
  (generated profiles, Markdown tables, explicit plan paths, or command
  adapters) and fits existing project planning instead of requiring a dedicated
  plan directory.
- `ralphex` runs a dedicated plan through task and review phases; `vibe-loop`
  runs one repository backlog slice at a time and merges reviewed slices back to
  `main` frequently.
- `vibe-loop` treats agent execution as configuration (template commands, not a
  hard dependency on one CLI) and keeps workers finite, leaving
  branch/worktree management to the agent.

## Future Plans

The current implementation supports generated task-source profiles,
command-backed sources, dependencies, conflict domains, finite workers, run logs,
structured reports, planning analytics, and skill evals. Planned spec-driven
additions stay below the authoring layer:

- parser presets for Spec Kit, Kiro, OpenSpec, and similar artifacts;
- optional traceability fields on normalized tasks;
- read-only spec coverage and drift checks;
- opt-in execution gates requiring approved, current spec artifacts;
- spec-aware worker prompt context;
- completion evidence mapping requirements to plan rows, reports, trailers,
  tests, and reviews.

## Development

Install the repository tools with `uv`, then run the standard checks:

```bash
uv sync
uv run python -m unittest discover
uv build
uv run --with twine --no-project -m twine check dist/*
```

The `Makefile` wraps the common release steps:

```bash
make install-hooks
make bump-patch
make bump-minor
make check
make tag
```

`make tag` uses the current `uv version --short` value by default; pass
`VERSION=...` to check or tag an explicit version. The installed `pre-push` hook
rejects pushed `v*` tags when `pyproject.toml` or the `vibe-loop` entry in
`uv.lock` does not match the tag.

Releases are built by `.github/workflows/release.yml` via PyPI trusted
publishing with the `TestPyPI` and `PyPI` GitHub environments. Run the workflow
manually with target `TestPyPI` for staging; to publish, push a `v<version>` tag
matching `project.version`, or dispatch from that tag with target `PyPI`. Before
publishing bundled skill changes, run the release-readiness gate and put the
record path in the release notes:

```bash
uv run vibe-loop eval release-gate --repo . --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

See [`docs/release-checklist.md`](docs/release-checklist.md) for the checklist
and dry-run record format.

## License

`vibe-loop` is licensed under the MIT License. See [`LICENSE`](LICENSE).
