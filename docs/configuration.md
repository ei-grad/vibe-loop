# Configuration Reference

All configuration is optional. Put explicit settings in `.vibe-loop.toml` at
the repository root. Explicit configuration remains authoritative over
generated discovery and auto-detected behavior.

When `--repo` names a linked Git worktree without its own `.vibe-loop.toml`,
`vibe-loop` falls back to the main worktree's configuration and warns on
stderr. Runtime state, locks, logs, and caches still live under the invoked
worktree.

## Annotated configuration

This example shows the principal settings and their defaults or typical values.
The sections after it explain the options that need more context.

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
# Optional when kind = "auto" and Codex or Claude is available on PATH.
kind = "auto"
# model = "gpt-5.4"
# effort = "high"
command = "codex exec {prompt}"
selection_command = "codex exec {prompt}"
# analysis_command is read-only and separate from the implementation worker.
# analysis_command = "codex exec --sandbox read-only {prompt}"
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

# Runtime-owned is the default. It fails closed until an independent reviewer
# route and a task-provenance completion path are configured.
[orchestration]
mode = "runtime-owned"
# reviewer_profile = "review"
# task_provenance_mode = "external-confirmed"
# external_completion_actor = "external-system"
# max_initial_review_passes = 1
# max_closure_review_passes = 2
# reviewer_concurrency_budget = 1
# max_candidate_reanchors = 2

[supervision]
max_restarts = 3
cooldown_seconds = 30.0
recover_unknown_runs = true
worker_timeout_seconds = 10800.0
slice_token_threshold = 100000
cross_run_attempt_threshold = 3

[locks]
type = "directory"
# lease_seconds = 300

# The whole block is optional and disabled by default.
# [budget]
# enabled = true
# metric = "total_tokens"
# fail_safe = "reserved"
# default_declared = 150000
# [budget.declared]
# implementation = 200000
# review = 150000
# [[budget.limits]]
# provider = "anthropic"
# phase = "implementation"
# limit = 5000000
# warn_at = 0.8
# window_hours = 24

[autopilot]
# jobs = 2
# interval_seconds = 60.0
# min_ready = 1
# planning_recheck_seconds = 60.0
# idle_poll_max_seconds = 600.0
# planning_backoff_seconds = 21600.0
# planning_max_launches_per_day = 4
# planning_unproductive_threshold = 2
require_clean_repo = true
worktree_disposition = "report-only"
# health_command = "scripts/health.sh"
# summary_command = "scripts/summary.sh"
# troubleshoot_command = "scripts/troubleshoot.sh"
# planning_command = "scripts/plan.sh"
# idle_wake_command = "scripts/wait-for-change.sh"
```

## Agent configuration

`agent.kind` is `auto`, `codex`, `claude`, or `custom`. With `auto`, omitted
commands use deterministic Codex-first detection. `model` and `effort` add
provider-specific flags to inferred commands. Explicit `command`,
`selection_command`, and `analysis_command` templates remain authoritative.

`prompt_dialect` and `skill_ref_prefix` control how custom commands refer to the
bundled skill. `worker_prompt_extra` is repository-wide policy appended only to
worker prompts. `forward_stderr` mirrors worker stderr in addition to retaining
it in the run log.

The authoritative template, dialect, environment, and read-only-analysis
contracts are in [PRD-CLI-004 and PRD-CLI-005](prd/cli-runtime.md).

## Per-task agent routing

Named profiles let dispatch choose a worker independently for each task:

```toml
[agent]
kind = "codex"

[agent.profiles.claude-opus]
kind = "claude"
model = "opus"
effort = "high"

[[agent.routing]]
profile = "claude-opus"
match_hazards_any = ["abi", "dma", "irq"]
match_paths_glob = ["kernel/**"]
```

Each `[agent.profiles.<name>]` table accepts the agent-selection fields:
`kind`, `model`, `command`, `selection_command`, `analysis_command`, `effort`,
`prompt_dialect`, and `skill_ref_prefix`. `worker_prompt_extra` remains
top-level policy rather than a profile field.

### Routing rule predicates

Rules are ordered. All predicates within one rule must match, and the first
matching rule wins.

| Option | Match |
| --- | --- |
| `match_hazards_any` | The task's `hazards` contains any listed token. |
| `match_paths_glob` | Any task path matches any listed `fnmatch` glob. |
| `match_task_id_regex` | The regular expression searches the task ID. |
| `match_title_regex` | The regular expression searches the title. |
| `match_priority` | Priority equals the value, case-insensitively. |

An explicit per-task `agent` profile wins over routing rules. Otherwise the
first rule wins, then the default `[agent]`. An undefined profile is a hard
error. Task selection itself uses the default agent because the selected task's
profile is not known yet. Worker execution, recovery, and provenance use the
resolved profile.

After profile selection, an explicit per-task `model` overrides the profile
model. Command-backed sources may also emit per-task `hazards`; sources that
omit agent-routing fields are unchanged.

## Usage budgets and reservations

`[budget]` gates model launches. It is disabled when the block is absent or
`enabled = false`. When enabled, every launch atomically reserves its declared
allowance in `<state_dir>/budget.jsonl`. `on_insufficient = "block"` refuses the
run; `"defer"` leaves it for a later dispatch. A refusal launches no model and
is recorded durably.

Runtime-owned implementation, initial review, targeted closure, and remediation
launches reserve and reconcile separately. The aggregate terminal run usage
reconciles only the implementation reservation, so phase usage is not counted
twice.

### Budget dimensions

`metric` selects one dimension for all limits and declarations:
`input_tokens`, `output_tokens`, `total_tokens`,
`non_cached_input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, or `cost_usd`.

`default_declared` is the fallback reservation. Entries under
`[budget.declared]` override it by phase. Each `[[budget.limits]]` requires
`limit` and may select `project`, `provider`, `phase`, `model`, or `effort`.
`warn_at` sets a utilization warning fraction and `window_hours` bounds the
accounting window. A launch must satisfy every matching limit.

The `project` selector is the basename of the repository-common Git root, not
the project configured by `[project_binding]`. Linked worktrees therefore share
one budget ledger and lock. Provider projections remain separate and are never
summed across providers.

### Unknown and recovered usage

Authoritative provider usage reconciles a reservation exactly once. Unknown or
malformed usage is never charged as zero:

- `fail_safe = "reserved"` retains the declared reservation.
- `fail_safe = "fixed"` charges the positive `fail_safe_amount`.

A crashed run with durable terminal usage reconciles from that result. A
provably dead owner without a result receives the fail-safe charge. The owner
record includes process birth identity to distinguish a live process from PID
reuse; unavailable birth identity remains conservatively reserved.

The journal and checkpoint defend against torn writes and accidental corruption,
not an attacker who can write the state directory. Invalid, mismatched,
duplicate, orphaned, or torn terminal records never close a live reservation at
zero. Compaction runs under the same lock and preserves cumulative consumption,
live reservations, recent decisions, and bounded integrity counters.

`vibe-loop runs summary --json` reports per-limit consumption, reservation,
remaining allowance, utilization, warning/exceeded state, and separate
provider/phase projections. Admission limits later launches; it cannot stop a
model process that is already producing tokens.

## Orchestration and reviewer routing

`orchestration.mode = "runtime-owned"` delegates gates, independent review,
integration, and task provenance to the runtime. The worker-owned compatibility
mode keeps those duties with the worker.

`reviewer_profile` selects a named agent profile independently from the
implementation profile. The pass counts, reviewer concurrency budget, and
candidate re-anchor count bound runtime work. `task_provenance_mode` and
`external_completion_actor` determine how integration is confirmed against the
task source.

The full reviewer-route behavior, including exact `codex review` restrictions,
continuation fallback, malformed verdict handling, and immutable review budgets,
is reconciled in [Runtime-owned reviewer route](skill-work-modes.md#runtime-owned-reviewer-route).

## Lock configuration

Directory locks under `<state_dir>/locks` are the default. Set `locks.type =
"command"` for explicit external acquire, release, status, and list adapters.
`lease_seconds` enables heartbeats and fencing generations.

The adapter environment, JSON wire contracts, failure behavior, leases, and
heartbeat command are authoritative in
[PRD-WRK-011 and PRD-WRK-012](prd/worker-supervision.md#prd-wrk-011-pluggable-lock-backends).

## Task-source configuration

`task_source.type`, `plan_path`, `plan_paths`, `profile`, `list`, `next`,
`probe`, `activate`, `complete`, `reset`, and `park` select or define the active
source. Setting any of them disables generated cache as the active source.
`runnable_statuses` is a non-source override and can replace generated runnable
statuses without disabling a generated parser.

The default runnable statuses are `Active`, `Next`, and `Planned`. Semantic
completion, rank, and blocked-family checks are case-insensitive; an explicitly
configured runnable-status allowlist matches source wire values exactly.

### Source references

- Generated-cache schema, Markdown fallback, ralphex plans, spec-tool presets,
  precedence, and degradation states:
  [Generated Task Discovery](generated-task-discovery.md).
- Normalized tasks, command adapter lifecycle, source authority, activation,
  completion, reset, parking, and selection:
  [Task Discovery PRD](prd/task-discovery.md).
- A complete plan fixture:
  [Ralphex Markdown Plan Example](examples/ralphex-markdown-plan.md).

These documents are the single accounts for their subjects. This reference
lists the configuration keys but does not repeat their contracts.

## Other configuration groups

`[completion].commands` runs explicit repository checks that help classify an
attempt; they do not replace the task source or structured worker reports.

`[supervision]` controls restart count, retry cooldown, unknown-run recovery,
worker timeout, low-change/high-token diagnostics, and repeated unchanged
attempt suppression.

`[autopilot]` controls worker count, cycle interval, ready-queue floor, planning
poll/backoff limits, repository cleanliness, worktree disposition, disk reserve,
and optional maintenance commands. Generated profiles cannot introduce those
commands.

`[specs]` controls approval, fingerprint, requirement-coverage, and completion
evidence gates plus allowed approval states and operator-owned override commands.

## Option destination map

This map records where every option documented in the former README
configuration section now lives.

| Options | Authoritative destination |
| --- | --- |
| `main_branch`, `state_dir` | Annotated configuration above; configuration authority in [CLI Runtime PRD](prd/cli-runtime.md). |
| `agent.kind`, `model`, `effort`, `command`, `selection_command`, `analysis_command`, `prompt_dialect`, `skill_ref_prefix`, `worker_prompt_extra`, `forward_stderr` | [CLI Runtime PRD](prd/cli-runtime.md). |
| `agent.profiles.*`; routing `profile`, `match_hazards_any`, `match_paths_glob`, `match_task_id_regex`, `match_title_regex`, `match_priority`; per-task `agent`, `model`, `hazards` | Per-task agent routing above. |
| `task_source.type`, `plan_path`, `plan_paths`, `profile`, `runnable_statuses`, `list`, `next`, `probe`, `activate`, `complete`, `reset`, `park` | [Generated Task Discovery](generated-task-discovery.md) and [Task Discovery PRD](prd/task-discovery.md). |
| Profile fields `resources`, `paths`, `column`, `none_values`; ralphex conflict surfaces; `spec-kit`, `kiro`, `openspec` | [Generated Task Discovery](generated-task-discovery.md) and the [ralphex example](examples/ralphex-markdown-plan.md). |
| `completion.commands` | Other configuration groups above and [PRD-CLI-003](prd/cli-runtime.md#prd-cli-003-completion-checks). |
| `orchestration.mode`, `reviewer_profile`, `task_provenance_mode`, `external_completion_actor`, `max_initial_review_passes`, `max_closure_review_passes`, `reviewer_concurrency_budget`, `max_candidate_reanchors` | [Runtime-owned reviewer route](skill-work-modes.md#runtime-owned-reviewer-route). |
| `supervision.max_restarts`, `cooldown_seconds`, `recover_unknown_runs`, `worker_timeout_seconds`, `slice_token_threshold`, `cross_run_attempt_threshold` | Annotated and other groups above; supervision contracts in [Worker Supervision PRD](prd/worker-supervision.md). |
| `locks.type`, `acquire_command`, `release_command`, `status_command`, `list_command`, `lease_seconds` | [PRD-WRK-011 and PRD-WRK-012](prd/worker-supervision.md#prd-wrk-011-pluggable-lock-backends). |
| `budget.enabled`, `metric`, `fail_safe`, `fail_safe_amount`, `default_declared`, `on_insufficient`, `declared.*`; limit `project`, `provider`, `phase`, `model`, `effort`, `limit`, `warn_at`, `window_hours` | Usage budgets and reservations above. |
| `autopilot.jobs`, `interval_seconds`, `min_ready`, `planning_recheck_seconds`, `idle_poll_max_seconds`, `planning_backoff_seconds`, `planning_max_launches_per_day`, `planning_unproductive_threshold`, `require_clean_repo`, `worktree_disposition`, `health_command`, `summary_command`, `troubleshoot_command`, `planning_command`, `idle_wake_command` | Annotated and other groups above. |
| `specs.require_approved`, `require_current_fingerprints`, `require_requirement_coverage`, `require_completion_evidence`, `approved_states`, `override_commands` | [Task Discovery PRD](prd/task-discovery.md) and other groups above. |

## Reconciliation record

- The CLI Runtime PRD is authoritative for agent command/dialect and logging
  contracts because it owns `PRD-CLI-004` and `PRD-CLI-005`. Missing analysis,
  worker-environment, and stderr details were merged there.
- The runtime reviewer explanation is design rationale rather than a component
  requirement, so the account was merged into `docs/skill-work-modes.md`.
- The Worker Supervision PRD is authoritative for lock backends and leases under
  `PRD-WRK-011` and `PRD-WRK-012`; the adapter wire format was merged into those
  requirements.
- The Task Discovery PRD is authoritative for normalized task and command-source
  lifecycle contracts. Generated/Markdown operator guidance remains in
  `docs/generated-task-discovery.md`, with the tutorial fixture under
  `docs/examples/`.
