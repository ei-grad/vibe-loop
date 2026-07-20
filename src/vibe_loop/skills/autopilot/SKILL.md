---
name: autopilot
description: Use for unattended stewardship of an autonomous vibe-loop development loop. The agent keeps the detached autopilot supervisor running and healthy, reviews what landed, replenishes the ready queue by running planning through the orchestrated-vibe-loop skill, and recovers the supervisor, running until stopped.
---

# Autopilot

Use this skill to steward an autonomous `vibe-loop` development loop. The agent
keeps the CLI autopilot supervisor healthy while it drives
`vibe-loop run-until-done` as the worker pool, and adds the judgement a plain
loop cannot: reviewing landed work, troubleshooting worker sessions, planning
to keep the ready queue fed, and deciding when to recover or stop.

This is an operator skill, not a worker skill. It drives the `vibe-loop` CLI and
delegates analysis, planning, and any code/docs work to subagents and skills. It
does not author product code in the main worktree.

For any actual work — including planning and docs updates — follow the
`orchestrated-vibe-loop` skill: no edits in the main worktree, dedicated
branches/worktrees per piece of work, and independent review before merge to
`main`. Keep the main worktree clean — if it becomes dirty, inspect the exact
files and process evidence first and do not revert peer or user changes.

## Continuation

Assume an unattended session. Do not stop voluntarily: keep a background wait
running so you wake on the next UTC 30-minute cycle boundary or when
`run-until-done` exits, then run the cycle and continue. Stop only on explicit
instruction or session end.

## Cycle

1. **Health**: disk, process liveness, git sync, locks, worktrees, queue depth.
   Use `vibe-loop doctor`, `vibe-loop workers`, and `vibe-loop main-integration
   status`; cross-check process liveness for the `run-until-done` supervisor and
   its workers. Confirm the configured worktree-disposition policy; do not infer
   permission to reap from the existence of an autopilot session.
2. **Summarize**: run a read-only subagent to analyze commits merged to `main`
   since the previous cycle anchor and produce a concise "what landed" note. Use
   the last reported `main` SHA as the anchor; if no durable anchor exists, state
   the chosen range explicitly and continue.
3. **Troubleshoot**: run a read-only subagent over recent worker run logs and
   `.vibe-loop/runs.jsonl` (or `vibe-loop runs list` / `vibe-loop runs inspect`)
   to catch problems faced during implementation. Address findings appropriately
   — update project instructions, or feed them into planning as new tasks.
4. **Plan**: when the ready queue is shallow, invoke the `orchestrated-vibe-loop`
   skill to plan from the repository's own planning inputs — the configured task
   source, design docs, roadmaps, issues, and TODOs — and to decompose enough
   reviewed, ready tasks for workers to implement for a couple of cycles. Run
   that planning/docs work in a worktree with independent review before it merges
   to `main`, like any other work.
5. **Maintain**: keep the task source and related status docs current.
6. **Recover**: if the `run-until-done` process has exited, investigate from
   evidence, fix the concrete cause, and relaunch it.

## Launch The Supervisor

Before launching, confirm:

- `main` is clean.
- `vibe-loop doctor` reports no stale task or integration locks blocking
  selection.
- the task source has at least one ready task.
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

Recovery reads the fencing generation this installation last minted from the
local token counter under the lock root, then requires the backend to report
that same generation. It refuses live, foreign, missing-token, mismatched-token,
or run-mismatched ownership. Never put a fencing token in argv, logs, prompts,
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
- `deadline`: the cycle boundary arrived — run the full cycle.
- `message`: a user instruction arrived — read the structured `user_message`
  event and apply it as a redirect before continuing the cycle.
- `adapter_error`: message polling failed — inspect the adapter directly before
  waiting again; do not silently disable it.

After every wake, state the exact `wake_reason`/`wake_summary`, run the health
checks, then decide whether to recover, summarize, troubleshoot, plan, or keep
waiting. When the repository exposes a trusted direct-message adapter, add
`--message-command` and identify the recipient with `--session-ref` (or
`VIBE_LOOP_RUN_ID`). Keep harness-specific wake signals, such as completion of
one of your own subagents, in the agent environment.

## Investigate Loop Termination

Answer from evidence, not process absence alone.

- Last worker result: inspect `.vibe-loop/runs.jsonl` and the newest worker log
  under `.vibe-loop/runs/`, or use `vibe-loop runs list` / `vibe-loop runs
  inspect`.
- Locks and workspaces: trust `vibe-loop doctor` and `vibe-loop workers` before
  adopting work; do not delete scheduler metadata from JSON contents alone.
- Queue state: zero ready tasks plus no active worker means the supervisor
  drained the runnable set or cannot select work. That is a planning signal for
  this operator skill, not a crash and not built-in task authoring by the CLI.
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
