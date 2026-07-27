---
name: autopilot
description: Use for unattended stewardship of an autonomous vibe-loop development loop. The agent keeps the detached autopilot supervisor running and healthy while its native generic cycle checks capacity, summarizes landed work, diagnoses recurring trouble, plans when the queue is shallow, and dispatches workers.
---

# Autopilot

Use this skill to steward an autonomous `vibe-loop` development loop. The agent
keeps the CLI autopilot supervisor healthy while it drives
`vibe-loop run-until-done` as the worker pool. The supervisor already implements
the repository-agnostic generic cycle: it checks capacity and worktrees,
summarizes landed work, detects recurring trouble, invokes native planning when
the ready queue is shallow, and records the complete cycle before waiting or
dispatching again. Repository-authored maintenance commands are optional
overrides or additions, not prerequisites for those native behaviors.

This is an operator skill, not a worker skill. It drives the `vibe-loop` CLI and
monitors the supervisor's recorded evidence. It does not duplicate routine
native analysis, planning, summary, or troubleshooting outside the supervisor,
and it does not author product code in the main worktree.

For actual product work outside the native cycle, use the
`orchestrated-vibe-loop` skill or the repository's equivalent reviewed workflow:
no edits in the main worktree, dedicated branches/worktrees per piece of work,
and independent review before merge to `main`. Keep the main worktree clean —
if it becomes dirty, inspect the exact files and process evidence first and do
not revert peer or user changes.

## Continuation

Assume an unattended session. Do not stop voluntarily: keep a background wait
running so you wake on the next UTC 30-minute cycle boundary or when
`run-until-done` exits, then run the operator cycle below and continue. Stop
only on explicit instruction or session end.

## Operator Cycle

After every wake:

1. State the exact `wake_reason` and `wake_summary`.
2. Run `vibe-loop autopilot status --json`, `vibe-loop doctor`, `vibe-loop
   workers`, and `vibe-loop main-integration status`. Cross-check the recorded
   autopilot and `run-until-done` process identities against actual liveness.
3. Inspect the latest cycle actions and their typed worktree, disk, landed
   summary, troubleshooting, and planning records. Report their conclusions;
   do not rerun native summary, troubleshooting, or planning outside the
   supervisor.
4. For `pid`, a missing supervisor, or an inconsistent state, use
   [Investigate Loop Termination](#investigate-loop-termination), correct only
   the evidenced cause, and relaunch through
   [Launch The Supervisor](#launch-the-supervisor) when its preconditions hold.
   For `deadline` or `runtime_event`, address recorded blockers or confirm that
   the supervisor continues to advance. Treat `message` as a user redirect
   before taking another action.
5. Resume [Wake / Wait](#wake--wait) unless an explicit stop instruction or a
   blocker requires a status report.

## Native Supervisor Cycle

The
[Autopilot PRD](../../../../docs/prd/autopilot.md#prd-aut-012-configuration-free-generic-cycle)
owns the native generic-cycle product contract. The following is the
operator-facing map of the shipped behavior, not a second contract account.
Each supervisor cycle provides these behaviors without requiring
repository-specific maintenance commands:

1. It collects queue, worker, lock, git, and supervisor status; performs only
   evidence-gated stale-lock settlement; refreshes required upstream-sync
   evidence; and runs configured health and task-source-health hooks as
   additional preflight gates.
2. It inspects worktree ownership, liveness, dirty state, and merge state. The
   default `report-only` policy records candidates without mutation. Explicit
   `reap` uses a reasoned decision from the read-only analysis agent, while code
   enforces the ownership, liveness, cleanliness, merge, and task-state
   guardrails before removal.
3. It measures disk capacity on native worktree storage, records a bounded
   read-only `main` summary from the prior cycle anchor, and derives recurring
   trouble from the bounded run journal. These native steps run every cycle;
   warnings and task-scoped observations do not terminate existing workers.
4. When the queue is shallow, an explicit planning hook takes precedence.
   Otherwise the read-only analysis agent returns a structured plan-or-no-plan
   decision from bounded evidence. Only a separate supervised read-write worker
   may author tasks, invalid decisions fail closed, and the supervisor re-reads
   rather than edits the authoritative task source.
5. It re-collects status, honors blockers, dispatch floors, provider pauses, and
   planning budgets, then observes an existing child or launches
   `run-until-done`. A configured summary hook runs only in that observe/launch
   branch; a configured troubleshoot hook additionally requires a
   restartable-or-terminated child exit.

The contract is **agent decides, code executes, guardrails constrain, every
action is logged**. Judgement is isolated in the read-only analysis agent;
write-capable planning and implementation run in separate supervised workers.
The runtime validates structured decisions and owns side effects. Detailed
native maintenance results use registered typed records in the append-only run
journal, while every action also appears as a concise tag on the enclosing
cycle, including successful no-op decisions. Treat that journal and
`vibe-loop autopilot status --json` as the cycle's authority rather than
reconstructing actions from process absence or ad hoc log inspection.

## Launch The Supervisor

Before launching, confirm:

- `vibe-loop autopilot status --json` resolves the intended repository and task
  source. If `project_binding` reports required selectors, each must be under
  `resolved` with the expected value; fix the durable repository or registry
  binding rather than relying on ambient shell state.
- `main` is clean.
- `vibe-loop doctor` reports no stale task or integration locks blocking
  selection.
- the task source is readable. It may be empty: native planning handles a
  shallow queue within its recorded budget and backoff.
- no other `vibe-loop run-until-done` supervisor is already active for this
  repository.

Use the supported detached launcher on POSIX systems:

```bash
vibe-loop autopilot start --interval 60 --jobs 2 --json
```

`start` creates a new POSIX session, redirects standard input and output, and
returns only after the supervisor process and matching autopilot lock are both
live. Retain its run ID, PID, process-group/session IDs, and log path. If launch
verification fails, inspect the reported blocker and log before retrying.

This detached path survives normal caller exit but is not a boot service. Use a
platform service manager such as systemd, launchd, or a container orchestrator
when restart-on-failure, reboot persistence, resource limits, or non-POSIX
operation is required. Do not substitute a plain `nohup ... &` launch in job
harnesses that reap child jobs; it has no verified lock handoff or durable
session identity.

## Stop The Supervisor

On Linux, stop a detached supervisor through the verified lifecycle command:

```bash
vibe-loop autopilot stop --repo <repo> --json
```

Do not send signals manually. `stop` correlates the live lock with the recorded
run, PID, process group, session, and kernel birth identity, signals the exact
pidfd, and succeeds only after both the process and singleton lock are absent.
Identity ambiguity, a foreign host, interruption, timeout, or backend failure
is a blocker; the command does not escalate to `SIGKILL`. Live verified stop is
Linux-only even though detached `start` is available on POSIX systems more
broadly. For a foreground supervisor, the first `SIGINT` or `SIGTERM` starts
bounded cleanup; repeated supported signals are coalesced until child cleanup
and fenced lock release finish.

If status shows that the recorded process is already absent while its lock
remains, use the explicit fenced recovery path only after verifying the exact
recorded run ID:

```bash
vibe-loop autopilot stop --repo <repo> --recover-stale \
  --run-id <exact-supervisor-run-id> --json
```

Recovery reads the fencing generation this installation last successfully
acquired, recorded under the lock root only when a backend actually granted the
lock, then requires the backend to report that same generation. A refused
acquire — a fenced `autopilot start` against the stale lock — must not advance
it, or recovery would be locked out of the singleton it exists to release. It
refuses live, foreign, missing-token, mismatched-token, or run-mismatched
ownership. Never put a fencing token in argv, logs, prompts,
or diagnostics. Directory and command lock backends use the same manager release
and post-release verification path.

A command-backed singleton may hold no PID of its own. Recovery then takes the
exact PID from this installation's local `autopilot_supervisor_started` record
for the requested run and verifies that exact process is absent before
releasing. With no PID in either place the run is unverifiable, and recovery
refuses it as `autopilot_stale_recovery_missing_pid` rather than writing a
terminal record that status could never confirm.

A supervisor state of `inconsistent` is a blocker, not a stop. It means the
terminal stop record and the recorded process disagree — a stop record whose
process is still alive, a live supervisor that no longer holds the singleton
lock, or a vanished supervisor that never recorded its termination. Investigate
the reported blocker and the recorded PID before starting a replacement
supervisor; do not treat it as a clean `stopped`.

## Wake / Wait

Use `vibe-loop wait-helper` instead of ad hoc polling loops. By default it wakes
on the first watched process exit or at the next UTC 30-minute cycle boundary:

```bash
vibe-loop wait-helper --pid <run-until-done-pid> --json
```

Use `--deadline` only when you have an explicit absolute wake time. Use
`--cycle-schedule SECONDS` only when the repo or user requires a non-default
wall-clock cadence.

Wake results report `wake_reason`:

- `pid`: the supervisor exited — investigate and likely recover.
- `deadline`: the cycle boundary arrived — run the operator cycle.
- `message`: a user instruction arrived — read the structured `user_message`
  event and apply it as a redirect before continuing the cycle.
- `runtime_event`: an authoritative, allowlisted operator-action condition was
  recorded — inspect the event kind and scoped project/run/task identity, then
  run the operator cycle before deciding whether recovery or escalation is safe.
- `adapter_error`: message polling failed — inspect the adapter directly before
  waiting again; `runtime_event_adapter_error` identifies the separate runtime
  source. Do not silently disable either adapter.

After every wake, follow the [Operator Cycle](#operator-cycle). A direct
`message` is a user redirect and may change the task; a `runtime_event` contains
no message content and only signals that the scoped runtime needs operator
action. When the repository exposes a trusted direct-message adapter, add
`--message-command` and identify the recipient with `--session-ref` (or
`VIBE_LOOP_RUN_ID`). When it exposes typed lifecycle events, add a trusted
`--runtime-event-command` or `--runtime-event-journal` with a project-scoped
durable cursor. For first-time live stewardship of a trusted run journal,
initialize that cursor with `--runtime-event-start-at-tail`; reuse it without
that flag on later waits. Replay from the beginning only for a deliberate
audit, and replace an existing cursor only as an explicit reset action. Keep
harness-specific wake signals, such as completion of one of your own subagents,
in the agent environment.

Typed agent activity is observation, not general wake input. Do not wake for
ordinary checkpoints, gate progress, reviewer verdicts, completion, or
transient provider/dependency errors. Treat `work_blocked` as
`operator_action_required` only when its typed reason is `needs_decision`,
`authorization`, or `non_retryable_policy`; verified provider quota/account
walls retain their existing actionable kinds. Use `last_activity_at` to report
quiet or missing activity, never as authority to kill or restart without fenced
process and child-operation evidence. The repository's
`docs/prd/worker-supervision.md#prd-wrk-009-runtime-lifecycle-events` section
owns the product contract.

## Investigate Loop Termination

Answer from evidence, not process absence alone.

- Last worker result: inspect `.vibe-loop/runs.jsonl` and the newest worker log
  under `.vibe-loop/runs/`, or use `vibe-loop runs list` / `vibe-loop runs
  inspect`.
- Locks and workspaces: trust `vibe-loop doctor` and `vibe-loop workers` before
  adopting work; do not delete scheduler metadata from JSON contents alone.
- Queue state: zero ready tasks plus no active worker means the supervisor
  drained the runnable set, cannot select work, or recorded a native planning
  decision, backoff, provider limit, or planning failure. Inspect the latest
  cycle actions and planning records before classifying it as a crash or
  launching any separate planning work.
- A worker log ending with a completed report and released locks is a clean
  completion, not a failure. Repeated review/remediation rounds are
  implementation churn, not supervisor failure, while the worker log keeps
  advancing. If a worker stops making progress against repeated serious
  findings, the next action is a checkpoint or blocked report with the concrete
  unresolved finding, not unbounded waiting.

## Recovery Boundary

Recovery is conservative and non-destructive by default. Starting autopilot is
not approval to delete a worktree or branch: the native
`worktree_disposition = "report-only"` policy only inspects and journals eligible
candidates. Automatic reaping is permitted only when an operator explicitly set
`[autopilot] worktree_disposition = "reap"` or passed
`--worktree-disposition reap`; an unattended steward must not choose that opt-in
on the operator's behalf. Even then, keep the existing analysis-agent decision,
ownership, merged-state, cleanliness, task-state, and liveness guards, with every
decision and result journaled. Never reset branches, steal locks, kill arbitrary
processes, or revert peer/user changes. Stop only a specific process tree after
identifying its pids and confirming it is on the autopilot critical path. Do not
start a second supervisor while a live one still owns workers. If every safe path
is blocked, report the precise missing access, approval, or decision and keep
watching for newly available work.

## Status Reports

Keep updates brief and factual:

- what was missed or failed, with timestamps when known;
- current health: git sync, locks, queue depth, process liveness;
- the recovery or planning action in progress;
- exact blockers if every safe path is blocked.
