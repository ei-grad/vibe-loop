# Worker Supervision PRD

This PRD owns Level 2 contracts for finite worker execution, locks, reports,
parallel supervision, workspace visibility, and final integration coordination.

## PRD-WRK-001 Finite Worker Boundary

The CLI supervisor must launch finite worker commands for selected tasks without
taking over the worker-owned branch/worktree, implementation, review, and merge
workflow.

Acceptance must cover single-task `run-next`, serial `run-until-done`,
environment variables passed to workers, including the effective routed agent
kind and profile for repository-owned provenance hooks, worker prompt addendum,
and the rule that workers own their slice lifecycle. For command-backed sources
it must also cover exact task-lock acquisition followed by project adapter
activation and non-runnable-state confirmation before `run_started`, worker
launch, workspace claim, or edit. Missing or unconfirmed activation must fail
closed without resetting project state or touching another worker's lock or
workspace.

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
classification. Finalization must not depend on the enclosing `run-until-done`
process, whose next dispatch or idle transition would otherwise race it.

The settled outcome and the lock release form one fail-closed finalization
boundary. A provenance-mirroring backend finalizes a run from the lock row it has
already stored and discards whatever the release call carries, so storing the
outcome is the only operation that can settle the run and it gates the release.
When the store succeeds the lock is released normally. When it fails for any
backend, ownership, fencing, or I/O reason, no release is issued, no
`lock_released` event is recorded, and a typed finalization failure is surfaced
alongside a `lock_finalization_failed` event; the lock stays held under the same
run id and fencing token, so it remains recoverable. There is therefore no
ordering in which the lock is given up while the external run stays `unknown`
even though the supervisor settled on `completed`. The recorded `run_result` is
durable before finalization is attempted and is never masked by this failure.

An `unknown` outcome is exempt from the gate: it is what a backend records for a
run it was told nothing about, so a failed store loses no information, and
blocking release there would strand the lock of an interrupted or report-less
run.

Four ordering rules keep the two stores in agreement:

- A settled outcome is only publishable once the local `run_result` append has
  succeeded. External provenance may never claim a completion vibe-loop itself
  failed to record, so a failed append leaves the run settling as `unknown`.
- A settled outcome is monotonic in the lock row: a stored terminal outcome
  (`completed`, `failed`, `blocked`) may only be replaced by another terminal
  one. A same-owner update carrying no outcome, or still carrying `unknown` —
  a heartbeat refreshing from a snapshot read before settlement — keeps the
  stored outcome and its classification instead of reopening the run.
- Row precedence only holds if every writer merges against the row its own write
  lands on, and a command backend offers no compare-and-swap to guarantee that.
  `LockManager.update` therefore takes read, preserve and write as one critical
  section, serialized across processes by a per-task file lock under the lock
  root. This is the same single-host assumption the local fencing-token ledger
  already makes: the supervisor, `vibe-loop worker heartbeat` and the workspace
  claim all write a task lock from the host that owns its lock root. A backend
  whose writers are genuinely distributed would need conditional updates in the
  backend itself.
- A recovery attempt that exhausts the unknown-run recovery budget settles as
  `failed`, not `unknown`. That run records the terminal `failed` result
  itself, before releasing its lock, and the recovery driver reuses it, so the
  external outcome is never published ahead of the durable local one. Only a
  run classified `unknown` re-enters recovery, so a `timed_out` or
  `limit_wall` run is terminal as itself and stays `unknown` externally.

The gate would be worthless if stale recovery could undo it. A lock retained by
a failed settlement still stores `unknown` while the run's `run_result` is
terminal, so `vibe-loop workers clean --force` republishes that durable outcome
onto the row before releasing it, and refuses the release when republication
fails. Recovery prefers the matching `lock_finalization_failed` event, but falls
back to the same run's durable terminal `run_result` if that event append also
failed or the supervisor exited first. Non-terminal and unrelated results are
never promoted. Recovery therefore either finalizes the run as it actually
settled or leaves the lock held and recoverable; it can never finalize it as
`unknown`.

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
