# Worker Supervision PRD

This PRD owns Level 2 contracts for finite worker execution, locks, reports,
parallel supervision, workspace visibility, and final integration coordination.

## PRD-WRK-001 Finite Worker Boundary

The CLI supervisor must launch finite worker commands for selected tasks without
taking over the worker-owned branch/worktree, implementation, review, and merge
workflow.

Acceptance must cover single-task `run-next`, serial `run-until-done`,
environment variables passed to workers, worker prompt addendum, and the rule
that workers own their slice lifecycle. For command-backed sources it must also
cover exact task-lock acquisition followed by project adapter activation and
non-runnable-state confirmation before `run_started`, worker launch, workspace
claim, or edit. Missing or unconfirmed activation must fail closed without
resetting project state or touching another worker's lock or workspace.

Related implementation IDs: `PAR-01`, `PAR-03`, `PAR-05`.

## PRD-WRK-002 Task Locks And Run Records

Task locks and run records must make active and historical worker attempts
inspectable without reading raw logs as the primary source of truth.

Acceptance must cover lock ownership, worker PID, task ID, run ID, log path,
start time, base main revision, host, resolved command identity, prompt dialect
and skill reference source metadata, append-only `runs.jsonl`, invalid JSON line
tolerance, and `workers`, `runs list`, and `runs inspect` views.

Related implementation IDs: `CORE-01`, `PAR-02`, `PAR-06`, `PAR-09`.

## PRD-WRK-003 Worker Reports

Workers must be able to publish structured final status for a run before exit,
and supervisors must prefer matching reports over heuristics.

Acceptance must cover statuses `completed`, `blocked`, `failed`, and `unknown`;
commit refs; concise messages; metadata JSON; owner matching; report records;
and fallback classification when no report exists. Fallback classification must
consider worker exit status, configured completion commands, task probing, and
main-branch change heuristics while marking the result as less authoritative
than a matching worker report.

A settled run must also finalize where external provenance lives. Once the run
is classified, the supervisor publishes a settled outcome — one of `completed`,
`failed`, `blocked`, `unknown` — into the task lock metadata while it still owns
the lock, so a command lock backend that mirrors run provenance finalizes the
run from the supervisor's own conclusion rather than inferring one at release.
Classifications that do not settle the run, such as `timed_out` and
`limit_wall`, publish `unknown`; so does any exit that never reached
classification. Publishing must precede the lock release and must not depend on
the enclosing `run-until-done` process, whose next dispatch or idle transition
would otherwise race it. A backend failure while publishing is reported but
never masks the recorded `run_result`.

Related implementation IDs: `PAR-03`, `PAR-05`.

## PRD-WRK-004 Parallel Supervision

`run-until-done --jobs N` must supervise multiple independent finite workers
while preserving the same worker contract as serial execution.

Acceptance must cover job limits, per-worker run IDs and logs, active worker
state, refill behavior, task-lock exclusion, agent-assisted batch selection,
duplicate/unknown/locked selection rejection, and deterministic fallback order.

Related implementation IDs: `PAR-01`, `PAR-07`.

## PRD-WRK-005 Conflict-Domain Scheduling

Parallel scheduling must avoid pairing tasks that declare overlapping resources
or overlapping repo-relative paths. Unknown domains must remain conservative
once conflict-domain scheduling is active.

Acceptance must cover resource equality conflicts, path ancestry conflicts,
empty-domain declarations, unknown-domain behavior, scheduler lock protection,
and active-worker conflict checks during refill.

Related implementation IDs: `PAR-08`.

## PRD-WRK-006 Workspace Claims

Workers that create or adopt their own branch/worktree should publish advisory
workspace metadata without transferring workspace ownership to the supervisor.

Acceptance must cover claim command inputs, matching active task lock, current
branch verification, branch/worktree path recording, base commit, current HEAD,
dirty-at-claim summary, `workspace_claim` run record, and no branch/worktree
creation, deletion, reset, merge, or cleanup by the claim command.

Related implementation IDs: `PAR-10`, `PAR-11`, `SKILL-01`.

## PRD-WRK-007 Main Integration Lock

Workers must be able to serialize the final refresh, verification,
fast-forward merge to `main`, and immediate `main` verification through an
advisory integration lock.

Acceptance must cover acquire/release/status commands, owner metadata, live and
stale holder reporting, no automatic stale lock stealing, owner mismatch
diagnostics, future wait/timeout ergonomics, and integration preflight checks
against claimed workspace state.

Related implementation IDs: `PAR-04`, `PAR-05`, `PAR-12`, `SKILL-01`.

## PRD-WRK-008 Stale State Visibility

The supervisor must report stale workers, stale locks, missing PIDs, missing
claimed worktrees, duplicate worktrees, merged active branches, and dirty
foreign-owned workspaces without destructive cleanup.

Acceptance must cover `workers --json`, `doctor --json`, recovery hints, no
automatic removal of locks or worktrees, and no stealing another worker's dirty
workspace or integration lock.

Related implementation IDs: `PAR-09`, `PAR-11`, `EVAL-09`.

## PRD-WRK-009 Runtime Lifecycle Events

`runs.jsonl` should support additional record types beyond `run_result` and
`worker_report`. New types include lock events (acquired, released, expired),
run start snapshots, observed agent runtime context, workspace events (claimed,
mismatch detected), and run state transitions (session observed, classified).

Records must be additive — unknown types are ignored on read, consistent with
the existing invalid JSON line tolerance. Each record carries `schema_version`,
`record_type`, `occurred_at`, and a type-specific payload. Records for the same
run share `run_id`; lock records carry `task_id`.

Events are diagnostic and inspectable, not authoritative for task status. The
task source remains the authority for task completion state.

Fencing tokens are internal lock capabilities, not diagnostic metadata. Raw
tokens must not be persisted in run records or emitted through CLI JSON,
worker, status, doctor, or troubleshooting views. Fencing failures retain a
stable `fencing_token_mismatch` reason plus non-sensitive owner and lock-path
context. Readers redact fencing fields in historical records as well as new
writes so legacy diagnostics cannot re-expose a token.

`runs.jsonl` must also be able to expose trailer-ready run context for
repository-owned tools that persist task evidence in git or project worklogs.
This context should include bounded, non-secret values such as task IDs suitable
for `Plan-Item`, resolved `Agent-Kind`, `Run-Id`, `started_at`, observed
`Session-Id`, prompt dialect metadata, and model provider/model ID/reasoning
effort only when the agent emits those values during startup or they can be
safely derived from the runtime command/executable. Each value should carry
provenance. `vibe-loop` publishes this context; repository-specific
`prepare-commit-msg`, `commit-msg`, or worklog hooks remain outside the
`vibe-loop` runtime and are responsible for deciding whether and how to mutate
commits.

Acceptance must cover new record types appended without breaking existing
readers, unknown type tolerance, correlation by `run_id` and `task_id`, payload
schema per type, trailer-ready context availability before worker commits when
possible, session-observed updates when native session IDs appear, redaction of
raw command configuration and fencing tokens, omission of model metadata that
cannot be observed or safely inferred, `started_at` on run results and emitted
run events, and the rule that lifecycle events do not replace task-source
authority.

Related implementation IDs: `RT-01`, `RT-05`, `RT-07`.

## PRD-WRK-010 Run State Machine

The run lifecycle should be formalized into explicit derivable states:
`scheduled`, `started`, `session_observed`, `workspace_claimed`, `reported`,
`classified`, and `finalized`.

State must be derivable from existing records in `runs.jsonl` without hidden
mutable state. `runs inspect` and `workers` should show the current lifecycle
state. Incomplete runs show which transitions are missing.

Acceptance must cover state derivation from recorded events, correct lifecycle
display in `runs inspect` and `workers`, partial state for incomplete runs,
and no hidden mutable state outside the append-only log.

Related implementation IDs: `RT-02`.

## PRD-WRK-011 Pluggable Lock Backends

Lock backends should be configurable so repositories can use directory-based
advisory locks (the default), command-backed adapters for external coordination
tools, or future backend implementations.

Command-backed lock adapters should follow the same pattern as command-backed
task sources: explicit user-authored commands in `.vibe-loop.toml`, bounded
JSON contracts for acquire/release/status/list, and clear failure diagnostics.
Generated profiles must not introduce lock adapters.

Acceptance must cover directory-based default behavior, command adapter
configuration, acquire/release/status/list command contracts, error handling
for adapter failures, fallback behavior when an adapter is unavailable, and
the rule that generated profiles cannot introduce lock backends. When a
configured command adapter is unavailable, lock operations must fail closed
with an actionable diagnostic rather than silently falling back to directory
locks.

Related implementation IDs: `RT-06`.

## PRD-WRK-012 Lock Leases

Lock metadata should support optional `lease_seconds` and heartbeat fields.
When a lease is set, the lock is considered expired after `lease_seconds`
without a heartbeat update.

Expired leases are visible in `doctor` and `workers` as an additional stale
reason alongside PID-based checks. Fencing tokens provide a monotonic
generation counter per lock path; sensitive operations (report, workspace
claim, integration lock release) should validate the token. Default behavior
is advisory locks without leases, fully backward compatible.

Acceptance must cover lease expiry detection, heartbeat updates resetting the
lease timer, fencing token validation on release and update, expired lease
visibility in diagnostics, and unchanged default advisory lock behavior.

Related implementation IDs: `RT-03`.

## PRD-WRK-013 Restart Budgets

The maximum restart count per task per supervisor session should be configurable
rather than hardcoded. Cooldown between retries should also be configurable.

Restart count must be visible in `workers` and `runs` output. When the budget
is exhausted, the supervisor escalates to a failed state with an explicit
exhaustion reason instead of stopping silently. The budget does not cross
supervisor sessions.

Acceptance must cover configurable max restarts and cooldown, restart count
visibility, explicit exhaustion failure classification, budget isolation per
supervisor session, and default values matching current hardcoded behavior.

Related implementation IDs: `RT-04`.
