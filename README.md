# vibe-loop

`vibe-loop` is a small execution engine for one-slice AI coding loops. It
selects one unblocked task from a repository task source, locks it, runs an
agent command such as `codex exec '$vibe-loop <task_id>'`, captures logs,
validates completion, records local run metadata, and can repeat until no
runnable tasks remain. The runtime is built around the bundled finite
[`vibe-loop`](src/vibe_loop/skills/vibe-loop/SKILL.md) skill, with unattended
continuation handled by
[`infinite-vibe-loop`](src/vibe_loop/skills/infinite-vibe-loop/SKILL.md).
The package also includes
[`orchestrated-vibe-loop`](src/vibe_loop/skills/orchestrated-vibe-loop/SKILL.md)
for runs where the main agent only coordinates explorer, implementation, and
review agents.

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

The runner is task-system agnostic. Repositories can expose tasks through
explicit Markdown configuration, repo-specific generated profiles, command
adapters, or later tracker-specific adapters. The generated profile path lets a
repository keep its existing planning format: an explicit configuration command
collects bounded repo-local evidence, asks the configured selection agent for a
non-executable parser profile, validates it mechanically, and caches it under
the configured state directory.

This repository's own plan uses a Markdown table. That fixed table is an
example of a supported shape, not the generic task-source contract. Built-in
Markdown sources also include ralphex-style plan files with `### Task N:` or
`### Iteration N:` headings and task checkboxes.

```text
ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence
```

Without explicit source configuration, read-only task commands inspect
`<state_dir>/generated-task-source.json`. A fresh `profile` cache becomes the
active task source. A fresh degraded cache such as `planning_only`,
`needs_input`, `unavailable`, or `rejected` is diagnostic only; read-only
commands report it and may continue to built-in Markdown fallback discovery. A
stale or invalid cache blocks fallback and points to `tasks configure`. If no
generated cache exists, the Markdown fallback scores `.md` files outside ignored
build/state directories and picks the best unambiguous parseable table matching
the example shape. Set `task_source.plan_path` when a repo wants to pin a
specific Markdown source, or use command-backed task sources for issue trackers
and custom task tools.

Runnable statuses default to `Active`, `Next`, and `Planned`. A task is runnable
when all listed dependencies are `Done` and no local lock exists.

The active task source is the source of truth for the dependency graph.
`.vibe-loop/runs.jsonl` records worker attempts and outcomes, but those records
do not advance task status. A completed worker must update the task source
itself, or make sure the configured command-backed adapter will report the task
as completed/non-runnable before the next scheduling pass. This is an
intentional design boundary: `vibe-loop` should not create a private task-state
channel that only its supervisor understands. Agents and humans working on the
same project without the CLI must be able to advance the same backlog by
updating the project-owned plan, tracker, or adapter state directly.

## Spec-Driven Workflow Execution

`vibe-loop` can sit underneath spec-driven development tools as the task
execution layer. Tools such as
[Spec Kit](https://github.com/github/spec-kit),
[Kiro](https://kiro.dev/docs/specs/), and
[OpenSpec](https://openspec.dev/) focus on authoring requirements, design
documents, proposals, task lists, and approvals. `vibe-loop` consumes the task
layer, schedules bounded runnable slices, launches finite worker agents,
captures logs, enforces local locks, records completion reports, and keeps the
execution trace inspectable.

The boundary is intentional. Spec tools own intent; `vibe-loop` owns repeatable
execution. A spec or PRD is not treated as proof of implementation unless a task
row, worker report, commit reference, test, review, or other explicit evidence
links the contract to completed work.

This repository uses a three-level planning model:

1. Level 1: `PROMPT.md` records project philosophy, architecture boundaries,
   stack choices, and PRD-writing rules.
2. Level 2: `docs/prd/` records stable product and workflow contracts with
   `PRD-*` IDs.
3. Level 3: `PLAN.md` records runnable implementation slices with permanent
   task IDs consumed by agents and `vibe-loop`.

## Future Plans

The current implementation already supports generated task-source profiles,
command-backed task sources, dependencies, resource/path conflict domains,
finite workers, run logs, structured reports, planning analytics, and skill
evals. The next spec-driven additions should stay below the authoring layer:

- parser profiles or presets for task artifacts produced by Spec Kit, Kiro,
  OpenSpec, and similar PRD/plan workflows;
- optional traceability fields on normalized tasks, including requirement IDs,
  spec paths, design references, approval state, and source fingerprints;
- read-only spec coverage and drift checks that report stale specs, missing
  task coverage, and completed tasks without evidence;
- opt-in execution gates for repositories that require approved and current
  spec artifacts before `run-next` or `run-until-done`;
- spec-aware worker prompt context that passes only bounded requirement,
  acceptance, design, and verification context to the worker;
- completion evidence that maps requirements to plan rows, worker reports,
  commit trailers, tests, reviews, and planning analytics output.

## Relationship to ralphex

`vibe-loop` is inspired by
[umputun/ralphex](https://github.com/umputun/ralphex): the useful core idea is a
repeatable autonomous loop that gives coding agents bounded tasks, validates the
result, records progress, and avoids relying on one long interactive chat.

The difference is in the workflow `vibe-loop` is built around:

- `ralphex` is plan-file centered; `vibe-loop` is task-source agnostic. It can
  use generated profiles, discover Markdown task tables, use explicit plan
  paths, or read tasks through command adapters.
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

Point `vibe-loop` at a supported task source. For a small repository, this
example Markdown table is enough:

```markdown
| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Make one scoped change. | Tests pass. | Not run. |
```

For an existing planning format, review and write a repo-specific generated
profile instead:

```bash
vibe-loop tasks configure --repo . --dry-run --json
vibe-loop tasks configure --repo . --json
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

The package includes three installable skills:

- `vibe-loop`: one coherent bounded slice. The agent inspects the task, edits,
  verifies, asks for independent review when available, commits, integrates to
  `main` when policy permits, cleans up, and stops. Once invoked directly or by
  a CLI worker command, the agent is expected to follow the finite loop rather
  than treating the skill as optional guidance.
- `infinite-vibe-loop`: unattended continuation across finite slices. Each
  slice follows the finite `vibe-loop` discipline; after cleanup/status, the
  agent chooses conservative next work, reports blocked paths, and continues
  until explicitly stopped or the session ends.
- `orchestrated-vibe-loop`: multi-agent execution where the main agent keeps
  orchestration state, delegates read-only exploration, delegates scoped
  implementation/remediation, runs independent review gates, and reports the
  result without doing the code or review work itself.

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
vibe-loop --version
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop tasks inspect QUERY-09 --repo .
vibe-loop tasks runnable --repo .
vibe-loop tasks locks --repo .
vibe-loop tasks configure --repo . --dry-run --json
vibe-loop tasks configure --repo . --force-refresh --json
vibe-loop tasks configure --repo . --promotion-toml
vibe-loop next --repo .
vibe-loop run-next --repo . --ask-agent
vibe-loop run-until-done --repo . --ask-agent --jobs 2
vibe-loop specs check --repo . --json
vibe-loop eval local-demo --repo . --trials 3 --agent-command '*=codex exec {prompt}'
vibe-loop eval release-gate --repo . --trials 3 --overwrite --record-output .vibe-loop/release-readiness.json
vibe-loop workers --repo .
vibe-loop workers --repo . --json
vibe-loop runs list --repo .
vibe-loop runs inspect <run-id> --repo .
vibe-loop planning artifacts --repo .
vibe-loop planning artifacts --repo . --check
vibe-loop planning artifacts --repo . --inspect
vibe-loop planning benchmark-duration --repo .
vibe-loop planning benchmark-duration --repo . --check
vibe-loop doctor --repo .
vibe-loop doctor --repo . --json
vibe-loop main-integration status --repo .
vibe-loop main-integration acquire --repo . --run-id ... --task-id ... --wait --timeout 300
vibe-loop main-integration release --repo . --run-id ... --task-id ...
vibe-loop worker claim-workspace --repo . --run-id ... --task-id ... --branch ... --worktree ...
vibe-loop report --repo . --run-id ... --task-id ... --status completed --commit ...
vibe-loop install-skills --codex --claude
```

`vibe-loop --version` prints the installed package version. Editable source-tree
installs and non-tag Git installs append `(git <short-sha>)`; release-tag
installs print only the package version.

`--ask-agent` gives the agent the mechanically safe candidate list plus recent
`.vibe-loop/runs.jsonl` entries and log tails. With `run-until-done --jobs N`,
the selection prompt asks for a batch and includes active worker state so the
selector can avoid work that is already in progress. The CLI validates returned
IDs against the current unlocked candidates, rejects duplicates and unknown or
locked tasks, and falls back to deterministic ready order before spawning.
When task sources declare `resources` or `paths`, the scheduler also rejects
overlapping conflict domains before spawning. Tasks without declarations are
treated as unknown and are not paired once conflict-domain scheduling is active;
repositories that do not declare any domains keep the legacy ready-order
parallel behavior.

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

Two independent stop limits bound a `run-until-done` session. `--max-slices N`
caps the total number of dispatched slices, counting every attempt regardless of
outcome. `--max-tasks N` stops once `N` slices have been classified `completed`;
failed, blocked, and unknown results do not consume the budget. Both default to
`0` (unlimited), and whichever limit is reached first ends the loop. Under
`--jobs`, the parallel scheduler never dispatches more in-flight work than the
remaining `--max-tasks` budget allows, so completed runs do not overshoot `N`.

`eval local-demo` materializes fresh bundled fixture repositories under the
configured output directory, runs the same prompt across selected skill
conditions and agent commands, writes per-trial artifacts, and emits
`aggregate.json` plus `aggregate.md` summaries. The aggregate includes a
`skill_quality` section that separates task failures from workflow-contract
failures, reports per-task and per-domain uplift, flags trigger, review,
integration, git, prompt, budget, and cost issues, compares against any previous
aggregate in the same output directory, and links each reported count or delta
back to the contributing trial artifact roots.

`eval release-gate` is the bundled skill release-readiness check. Without
`--aggregate` or `--dry-run`, it runs the local demo suite with a release default
of 3 trials per case and condition, then writes or prints a
`skill_release_readiness` record. The gate requires full local-demo coverage and
requires `skill_quality` comparison evidence before it can pass. It blocks
unresolved `workflow_contract_regression` findings from the aggregate's
`skill_quality` section. A regression can be accepted only when it is parked with
a task id, for example
`--parked-regression condition_comparison:vibe_loop=EVAL-99`. External benchmark
smoke evidence can be attached with `--external-benchmark-json`; it is recorded
as optional context and is not required for every bundled skill change.

Workers can explicitly report their final status while the supervisor run is
active:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \
  --task-id "$VIBE_LOOP_TASK_ID" --status blocked --commit HEAD \
  --message "waiting on reviewer" --metadata-json '{"reason":"review"}'
```

Report statuses are `completed`, `blocked`, `failed`, and `unknown`. Matching
report records are authoritative for classifying that worker run; they are not
task-source mutations and do not by themselves mark a task `Done` in the
runnable graph. Before reporting `completed`, the worker should update the
active task source, such as the Markdown plan row or command-backed tracker
state. Without a report, the supervisor falls back to exit status, completion
checks, task probing, and main-branch change heuristics.

Workers that create or adopt their own branch/worktree can make that ownership
visible without transferring control to the supervisor:

```bash
vibe-loop worker claim-workspace --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --branch "$BRANCH" --worktree "$WORKTREE"
```

The claim requires a matching active task lock and verifies that the worktree is
currently on the requested branch. It records the claimed branch, worktree path,
active-lock base commit, current HEAD, and dirty-at-claim status in the task
lock and appends a `workspace_claim` run record. It never creates, deletes,
resets, merges, or cleans up branches/worktrees.

Workers that are about to refresh, verify, fast-forward merge to `main`, and
immediately verify `main` can use the advisory `main-integration` lock to
serialize that final critical section:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --wait --timeout 300
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

`main-integration acquire --wait --timeout N` waits for a live or unknown
holder to release the advisory lock and returns the same busy payload if the
timeout expires. Stale locks are reported immediately and are not stolen.
`main-integration status` shows the current holder, process state, and stale
reason when the recorded same-host process is missing. By default, `acquire`
records the active task lock's worker process for the same run and task. If the
active task lock has a workspace claim, `acquire` also checks the claim against
the current worktree list and branch state before entering the final integration
section; stale or warning diagnostics block acquisition with recovery hints.
Pass `--pid` only when a wrapper needs to record a different long-lived owner
process or no active task lock exists.

Worktree and branch handling are intentionally outside the CLI runtime. Put that
policy in the repository instructions or in the configured agent command; keep
`.vibe-loop/` for locks, logs, and run metadata. Workspace claims are advisory
visibility metadata only.

`vibe-loop tasks` without a subcommand remains a compatibility alias for
`vibe-loop tasks runnable`.

## Configuration

Optional `.vibe-loop.toml`:

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
# Optional when kind = "auto" and Codex or Claude is available on PATH.
kind = "auto"
command = "codex exec {prompt}"
selection_command = "codex exec {prompt}"
forward_stderr = false

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
# Optional. When set, locks become stale after this many seconds without a
# heartbeat update.
# lease_seconds = 300

[planning_analytics]
schedule_policy = "current-runner-parity"
subject_matching = "diagnostic"
# Optional future collector adapter. Doctor reports whether this is set without
# printing the command string.
# worklog_command = "my-worklog export --jsonl"

[planning_analytics.duration_model]
name = "robust-duration-baseline-v1"
group_min_sample_count = 2
similarity_min_score = 0.35
similarity_max_examples = 3
similarity_blend_weight = 0.25
fallback_minutes = 60

[planning_analytics.outputs]
# Explicit repo artifact paths are opt-in. Omitted paths write under
# <state_dir>/planning-analytics.
# timeline_json = "docs/planning/timeline.json"
# gantt_html = "docs/planning/gantt.html"
# benchmark_json = "docs/planning/duration-benchmark.json"
# benchmark_markdown = "docs/planning/duration-benchmark.md"
```

Omit `[task_source]` source keys to allow generated cache use before Markdown
fallback discovery. The explicit source keys are `type`, `plan_path`,
`plan_paths`, `profile`, `list`, `next`, and `probe`; setting any of them
disables generated cache as the active task source. Non-source settings such as
`runnable_statuses` can still override matching generated fields without
disabling the generated parser.

Agent executable commands and worker prompt dialect are resolved independently.
`agent.command` and `agent.selection_command` are shell command templates.
`agent.kind`, `agent.prompt_dialect`, and `agent.skill_ref_prefix` control how
the worker prompt references the bundled skill. Explicit `.vibe-loop.toml`
command values remain authoritative; no generated profile can introduce
executable commands.

Supported agent kinds:

- `auto`: default. Omitted worker and selection commands use deterministic
  supported-agent detection.
- `codex`: use Codex-style worker prompts with `$vibe-loop`.
- `claude`: use Claude-style worker prompts with `/vibe-loop`.
- `custom`: use explicit command templates and require `prompt_dialect` or
  `skill_ref_prefix` before a worker prompt can be built.

Omitted worker and selection commands under `kind = "auto"` use a deterministic
Codex-first policy:

- Codex only: `codex exec {prompt}` for worker and selection commands.
- Claude only: `claude -p {prompt}` for worker and selection commands.
- Codex and Claude: Codex is selected for omitted commands.

When neither supported CLI is available, agent-using commands fail with a
diagnostic that points to installation or explicit config.

Lock storage defaults to directory locks under `<state_dir>/locks`. Repos that
coordinate through an external service can opt into command-backed locks with
explicit user-authored commands:

```toml
[locks]
type = "command"
acquire_command = "my-lock-tool acquire --json"
release_command = "my-lock-tool release --json"
status_command = "my-lock-tool status --json"
list_command = "my-lock-tool list --json"
```

When `locks.lease_seconds` is set, acquired locks include `lease_seconds`,
`heartbeat_at`, and a fencing token. Workers can refresh the lease with
`vibe-loop worker heartbeat`; mutating lock operations that receive a fencing
token reject stale holders when the current lock generation differs.

Lock commands run from the repository root and receive
`VIBE_LOOP_LOCK_OPERATION`, `VIBE_LOOP_LOCK_TASK_ID`,
`VIBE_LOOP_LOCK_RUN_ID`, `VIBE_LOOP_LOCK_ROOT`, and
`VIBE_LOOP_LOCK_METADATA_JSON`. `acquire_command` handles both `acquire` and
`update` operations. Acquire/update returns `{"acquired": true,
"metadata": {...}}` or `{"acquired": false, "metadata": {...}}` for a held
lock. Release must return `{"released": true}`; `false` is treated as a failed
release. Status returns `{"locked": true, "metadata": {...}}` or
`{"locked": false}`. List returns a JSON array or `{"locks": [...]}`. Once
`type = "command"` is set, lock command failures fail closed instead of falling
back to directory locks.

Configure Claude prompt mode explicitly when that is the worker or selector you
want to run regardless of what else is installed. The executable command can use
environment prefixes or wrappers because the prompt dialect comes from
`kind = "claude"`, not from parsing the command string:

```toml
[agent]
kind = "claude"
command = "CLAUDE_HOME=.claude claude -p {prompt}"
selection_command = "CLAUDE_HOME=.claude claude -p {prompt}"
forward_stderr = false
```

Configure a custom launcher by making both the executable template and skill
syntax explicit:

```toml
[agent]
kind = "custom"
command = "my-worker --prompt {prompt}"
selection_command = "my-selector --prompt {prompt}"
prompt_dialect = "claude"
# Equivalent low-level form:
# skill_ref_prefix = "/"
```

`prompt_dialect = "codex"` maps to `$vibe-loop`; `prompt_dialect = "claude"`
maps to `/vibe-loop`. `skill_ref_prefix` accepts `$` or `/` directly. If both
are set, they must agree. `kind = "custom"` without one of those fields is a
configuration diagnostic and worker launch failure, not an implicit Codex-style
default.

For compatibility, old configurations that set an explicit `agent.command`
without `agent.kind` still run. The runtime reports the prompt dialect source as
legacy command inference when it recognizes a simple Codex or Claude command, or
as a legacy Codex-style default when it cannot infer one. Set `agent.kind`,
`agent.prompt_dialect`, or `agent.skill_ref_prefix` to remove that migration
diagnostic.

`agent.command` receives `{task_id}` for the selected task and `{run_id}` for
the supervisor run. It also receives a shell-quoted `{prompt}` containing the
skill reference, normalized task context, and the CLI worker addendum. Worker
commands also receive `VIBE_LOOP_RUN_ID`, `VIBE_LOOP_TASK_ID`, `VIBE_LOOP_REPO`,
and `VIBE_LOOP_LOG` in their environment. `selection_command` receives a
shell-quoted `{prompt}` containing the dependency-ready candidate list and
recent run context. Single-task selection should print JSON containing
`task_id`; parallel batch selection should print JSON containing `task_ids`.
Spec traceability and future spec gate context are added to the worker prompt
from normalized task metadata independently of the executable command and prompt
dialect. If a task has traceability metadata, `agent.command` must include the
`{prompt}` placeholder; legacy task-id-only command templates fail fast because
they cannot receive the spec-aware prompt bundle.

For command-backed task sources:

```toml
[task_source]
type = "command"
list = "my-task-tool list --json"
probe = "my-task-tool show {task_id} --json"
```

`list` must return either a JSON array or `{"tasks":[...]}`. Each task should
include `id`, `title`, `status`, `priority`, `dependencies`, `scope`,
`acceptance`, and `evidence` where available. Optional `resources` and `paths`
arrays declare conflict domains for parallel scheduling. Resource names match
exactly; path locks use repo-relative paths and conflict when one path is the
same as, or an ancestor of, another. Omitted or `null` arrays are undeclared;
empty arrays explicitly declare no conflict domains for that task.

Tasks may also include optional traceability fields: `requirement_ids`,
`spec_paths`, `design_refs`, `approval_state`, and `source_fingerprints`.
Traceability is emitted in task JSON, planning analytics, generated-profile
promotion, and worker prompts when present; absent fields are omitted.

Spec diagnostics are read-only. `doctor` and `specs check` report unapproved
tasks, stale source fingerprints, missing requirement IDs, and completed
traceable tasks without evidence without launching an agent or running override
commands. Repositories that require current approved specs can opt into
execution gates:

```toml
[specs]
require_approved = true
require_current_fingerprints = true
require_requirement_coverage = true
require_completion_evidence = true
approved_states = ["approved"]
override_commands = ["make specs-override"]
```

The `require_*` settings are gates for execution commands such as `run-next`
and `run-until-done`; read-only task inspection remains available. Override
commands are reported as repository-owned recovery guidance and are never run
as a side effect by `doctor`, `specs check`, or task selection.

For ralphex-style Markdown plans:

```toml
[task_source]
type = "ralphex-markdown"
plan_path = "docs/plans/checkout.md"
# Or expose several plan files:
# plan_paths = ["docs/plans/checkout.md", "docs/plans/refund.md"]
```

The parser reads `### Task N:` and `### Iteration N:` headings, derives `Done`
status only when every checkbox in that task block is checked, and uses
`Planned` for tasks with any unchecked checkbox. Task IDs are stable
repo-relative IDs such as `docs.plans.checkout:task-1`, so multiple plan files
can be exposed together without colliding on `Task 1`. A `## Validation
Commands` section is copied into each task's evidence text.

Ralphex-style task blocks can declare conflict domains with labels:

```markdown
### Task 1: Add checkout API
- [ ] Add checkout handler
- Resources: api, checkout
- Paths: src/checkout.py, tests/test_checkout.py
```

The same labels can live in a plan-level `## Conflict Surface` section and
apply to every task unless that task declares its own values. A combined
task-local label is also accepted:

```markdown
- Conflict Surface: resources: api, checkout; paths: src/checkout.py
```

In a plan-level `## Conflict Surface` section, unlabeled bullet items that look
like repo-relative paths are also treated as path conflict domains, including
root-level files such as `Makefile` and code-spanned paths inside short prose.

Use `Resources: none` or `Paths: none` to explicitly declare an empty domain.
Blank or absent labels leave the domain unknown.

For common spec-driven task artifacts, use a built-in non-executable preset
instead of a command adapter:

```toml
[task_source]
type = "spec-kit"
# Default discovery: specs/*/tasks.md, .specify/specs/*/tasks.md
```

```toml
[task_source]
type = "kiro"
# Default discovery: .kiro/specs/*/tasks.md
```

```toml
[task_source]
type = "openspec"
# Default discovery: openspec/changes/*/tasks.md
```

The `spec-kit` preset reads checkbox task lists with `T001`-style IDs,
optional `[P]` and story markers, inline `(depends on T001)` text, and nested
`Depends`, `Depends on`, `Dependencies`, `Acceptance`, and `Evidence` labels.
`kiro` and `openspec` read numbered checkbox lists such as
`1. Prepare fixtures` or `1.2 Implement mutation` with the same optional
dependency and label patterns. Acceptance and evidence labels may be single-line
values or followed by nested bullet text. Checked boxes normalize to `Done`,
unchecked boxes to `Planned`, and `[-]` or `[~]` normalize to `Active`.
All three presets also read nested `Conflict Resources` and `Conflict Paths`
labels as conflict domains for `run-until-done --jobs N`. Use comma-separated
values or nested bullets, or `none` to declare an explicitly empty domain for a
task. Unprefixed `Resources` and `Paths` labels are left available for
repository-specific prose.
Markdown profiles may map the same optional traceability fields as command
task sources when the source artifact exposes them as columns, labels, prefixes,
or patterns.

When several task files are exposed, task IDs are prefixed with the parent
spec/change directory, for example `001-login:T001`,
`session-refresh:2`, or `checkout-mutation:1.2`. Dependencies declared with
local IDs are rewritten with the same prefix. Source provenance points back to
the source `tasks.md` file and section. Set `plan_path` or `plan_paths` to pin
specific files. Missing source files, missing stable IDs, duplicate IDs, and
invalid dependency syntax fail visibly instead of silently inventing runnable
tasks.

Markdown profiles can expose the same domains with optional `resources` and
`paths` field mappings:

```toml
[task_source.profile.fields.resources]
column = "Resources"
none_values = ["none"]

[task_source.profile.fields.paths]
column = "Paths"
none_values = ["none"]
```

For Markdown profiles, blank mapped cells are undeclared. A configured
`none_values` marker such as `none` explicitly declares an empty domain.

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

Review a generated candidate without activating it:

```bash
vibe-loop tasks configure --repo . --dry-run --json
```

`--dry-run` invokes the configured selection agent, validates the returned
profile, and prints the candidate cache envelope without writing
`generated-task-source.json`. This is the review path for checking the proposed
profile before it can affect task selection.

Create or refresh the active cache explicitly:

```bash
vibe-loop tasks configure --repo . --json
vibe-loop tasks configure --repo . --force-refresh --json
```

`tasks configure` creates a missing cache, repairs stale or degraded cache
records, and reuses a fresh runnable cache without launching the agent again.
Use `--force-refresh` when a repository wants to regenerate the profile even
though the current cache is still fresh, for example after planning docs move or
their format changes in a way the old fingerprints cannot explain. Malformed,
low-confidence, unsupported, or incomplete agent output is stored as an explicit
degraded cache record rather than changing runnable task behavior.

Explicit `.vibe-loop.toml` task-source settings stay authoritative. User-written
source keys disable generated discovery for the active source: `type`,
`plan_path`, `plan_paths`, `profile`, `list`, `next`, and `probe`. Defaults do
not count as explicit settings, and non-source settings such as
`task_source.runnable_statuses` override the matching generated field without
disabling the generated parser. Generated cache records cannot contain
executable adapters or lock backend settings such as `type = "command"`,
`list`, `next`, `probe`, generic command fields, `[locks]`, or lock command
fields. Add explicit `[task_source]` or `[locks]` settings to override cached
generated behavior.

Promote a reviewed generated profile into committed configuration when a repo
wants task discovery to be explicit instead of cache-backed:

```bash
vibe-loop tasks configure --repo . --promotion-toml
```

The command prints a non-executable `[task_source]` TOML snippet using
`type = "markdown-profile"` and the validated parser profile. It omits agent
metadata, provenance, fingerprints, and degradation state. If
`task_source.runnable_statuses` was already explicitly configured, the snippet
includes that override so promotion preserves the active task semantics.

See `docs/generated-task-discovery.md` for the generated profile schema,
precedence rules, stale-cache behavior, and degradation states.

## Planning Analytics

Planning analytics is a reporting boundary over normalized tasks, run records,
optional project worklogs, and bounded git metadata. It does not affect task
selection, worker locks, or completion classification. The default generated
artifact location is `<state_dir>/planning-analytics`, so analytics defaults do
not mutate repository docs. Repositories that want committed reports must opt in
with explicit output paths or future command flags.

Coverage checks use authoritative mappings only: task-source completion state,
structured worker reports, optional project worklog records, explicit commit
references, and `Plan-Item:` commit trailers. Subject matching, branch names,
and raw logs are diagnostic by default and do not satisfy coverage.

Projected timelines default to `current-runner-parity`, matching the runner's
dependency readiness and deterministic task order. `lightmetrics-parity` is
available as an explicit policy for comparison with the prototype behavior, and
generated timeline artifacts must serialize the selected `schedule_policy`.
Projected duration estimates use robust historical baselines from completed
actual spans. Each projected task records the selected model, minutes, low/high
interval, training sample counts, outlier handling notes, and feature/evidence
reasons.

`vibe-loop planning artifacts --repo .` writes deterministic timeline JSON and
a static Gantt HTML report under `<state_dir>/planning-analytics` by default.
Use `--output docs/planning/timeline.json` and
`--html-output docs/planning/gantt.html` for repo-owned docs workflows.
`--check` rebuilds both artifacts and fails if either file is missing or stale;
`--inspect` reports artifact freshness state, schema status, warning counts, and
rendered timeline warnings without regenerating files. Use `--check` when actual
staleness must be computed.

`vibe-loop planning benchmark-duration --repo .` writes deterministic JSON and
Markdown reports for duration-estimator candidates under
`<state_dir>/planning-analytics` by default, or under explicit benchmark output
paths when configured. `--check` validates those reports and fails when the
configured generator model name or parameters differ from the estimator selected
by the benchmark. The benchmark uses stable task/commit folds and excludes
validation tasks and validation-shared commits from each training fold.

`vibe-loop doctor` reports planning analytics readiness without running a
collector. It includes the selected schedule policy, subject matching mode,
worklog adapter presence, duration-model parameters, coverage tiers, resolved
artifact paths, artifact freshness, warning counts, next repair commands, and
whether repo-artifact outputs are explicitly enabled. See
`docs/planning-analytics.md` for the full contract.

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

`make tag` uses the current `uv version --short` value by default. Pass
`VERSION=...` to check or tag an explicit version. The installed `pre-push` hook
rejects pushed `v*` tags when `pyproject.toml` or the `vibe-loop` entry in
`uv.lock` does not match the tag version.

Releases are built by `.github/workflows/release.yml`. The workflow uses PyPI
trusted publishing with the GitHub environments named `TestPyPI` and `PyPI`.
Run the workflow manually with target `TestPyPI` for a staging upload. To publish
to PyPI, push a tag named `v<version>` where `<version>` exactly matches
`project.version` in `pyproject.toml`, or dispatch the workflow from that tag
with target `PyPI`.

Before publishing bundled skill changes, run the release-readiness gate and put
the resulting record path or artifact link in the release notes:

```bash
uv run vibe-loop eval release-gate --repo . --trials 3 --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

See `docs/release-checklist.md` for the checklist and dry-run record format.

## Local State

Runner state is intentionally untracked:

```text
.vibe-loop/
  locks/
  runs/
  runs.jsonl
```

Active task locks store the worker command `pid`, `task_id`, `run_id`, log path,
start time, base `main` revision, host, resolved command, and optional lease
metadata. `vibe-loop workers` reconstructs the active view from those lock
files plus `runs.jsonl`, then marks same-host locks with missing worker
processes, missing worker PIDs, expired leases, or incomplete metadata as stale
without reading raw logs. The PID is the immediate configured command process
started by the runner; deeper process identity checks are left to the later
watchdog work.

When a worker claims its workspace, the active task lock also stores a
`workspace` object with the branch, worktree path, base commit, current HEAD,
current branch, and dirty-at-claim summary. `workers --json` includes this
object, and text output shows the claimed branch/worktree plus clean or dirty
state. The JSON view also includes read-only `workspace_git_state` and
`workspace_diagnostics` fields built from `git worktree list`, current
worktree status, and branch containment in `main` or `origin/main`. These
diagnostics report missing claimed worktrees, duplicate worktrees for a branch,
already-merged active branches, dirty claimed worktrees, and stale
lock-to-worktree mismatches with manual recovery hints. `doctor --json`
summarizes the same diagnostics; neither command deletes locks, branches, or
worktrees.

The `main-integration.lock` entry is a separate advisory lock for worker-owned
final integration. Its metadata records the owner task, run id, host, pid, and
start time. It is visible through `vibe-loop main-integration status` rather
than `vibe-loop workers`; stale status is diagnostic only and does not grant a
new holder permission to take over automatically. `main-integration acquire`
can wait for a live holder with `--wait --timeout N`, but it blocks immediately
when the worker's claimed workspace has diagnostics that make final integration
unsafe.

`runs.jsonl` is an append-only stream of versioned run result records. Run
records include the vibe-loop `run_id`, the resolved worker `session_id`, the
`session_id_source`, the `agent_command_source` used for the worker command,
the `agent_selection_command_source`, prompt dialect and skill reference source
metadata, and the default agent policy source used when commands are
auto-resolved. `vibe-loop runs list` groups those records by run id and shows
the latest structured status plus the log path; `vibe-loop runs inspect
<run-id>` prints the detailed record history for one run.
Project worklogs should remain final evidence ledgers. Attempt logs and failed
runs belong in `.vibe-loop/`, not in project completion records.

Branches and worktrees created by worker agents are not tracked as runner state.
The agent workflow that creates them is responsible for refresh, review, merge,
and cleanup according to repository policy.

## License

`vibe-loop` is licensed under the MIT License. See `LICENSE`.
