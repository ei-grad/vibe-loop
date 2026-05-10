# Worker Supervision PRD

This PRD owns Level 2 contracts for finite worker execution, locks, reports,
parallel supervision, workspace visibility, and final integration coordination.

## PRD-WRK-001 Finite Worker Boundary

The CLI supervisor must launch finite worker commands for selected tasks without
taking over the worker-owned branch/worktree, implementation, review, and merge
workflow.

Acceptance must cover single-task `run-next`, serial `run-until-done`,
environment variables passed to workers, worker prompt addendum, and the rule
that workers own their slice lifecycle.

Related implementation IDs: `PAR-01`, `PAR-03`, `PAR-05`.

## PRD-WRK-002 Task Locks And Run Records

Task locks and run records must make active and historical worker attempts
inspectable without reading raw logs as the primary source of truth.

Acceptance must cover lock ownership, worker PID, task ID, run ID, log path,
start time, base main revision, host, resolved command identity, append-only
`runs.jsonl`, invalid JSON line tolerance, and `workers`, `runs list`, and
`runs inspect` views.

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
