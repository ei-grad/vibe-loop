# Parallel Worker Orchestration

`vibe-loop` parallel mode should supervise independent finite workers, not take
over the worker-agent integration flow. Each worker keeps owning the normal
`$vibe-loop <task_id>` contract: branch/worktree isolation, implementation,
verification, independent review, refresh against current `main`, fast-forward
integration, main verification, cleanup, and final task reporting.

The supervisor owns only scheduling, locks, logs, visibility, and result
collection. This keeps `main` integration semantics in the skill instructions
while making `vibe-loop run-until-done --jobs N` useful for unattended work.

## Operating Model

1. The supervisor reads the task graph and recent run logs.
2. It selects up to `N` ready, unlocked, mutually compatible tasks.
3. It locks each task and spawns one finite worker per task with the configured
   `agent.command`, defaulting to `codex exec '$vibe-loop <task_id>'`.
4. Each worker writes to its own run log and performs the full slice lifecycle.
5. Workers may claim their branch/worktree in the active task lock after they
   create or adopt that workspace, making workspace ownership visible without
   transferring workspace management to the supervisor.
6. Workers use an advisory `main-integration` lock around the final refresh,
   verification, fast-forward merge, and immediate `main` verification.
7. The supervisor watches worker exits, re-reads task state, records results,
   and fills open worker slots until no runnable tasks remain.

The supervisor must not resolve merge conflicts, run reviews for workers, or
merge worker branches itself. It can retry, report, or park tasks when worker
results are missing or ambiguous.

## Foundation Contracts

- `runs.jsonl` is append-only and versioned. The CLI may add richer record types
  later, but readers must tolerate unknown fields and skip invalid JSON lines.
- Agent-facing result JSON remains stable and scriptable on stdout. Supervisor
  progress, worker output mirroring, and empty-queue messages belong on stderr.
- Task discovery is explicit-first. User-authored `.vibe-loop.toml` and command
  adapters win over generated state. Generic Markdown discovery must not require
  repositories to adopt vibe-loop's local planning table shape.
- Resource/path conflict domains are optional task metadata. When present, the
  supervisor rejects overlapping resources or path ancestry before spawning a
  batch; undeclared domains are treated conservatively once conflict-domain
  scheduling is active.
- Agent execution is configurable. `codex exec` is the default worker command,
  not a required runtime dependency; other prompt-mode agents such as
  `claude -p` should be supported as first-class configured commands.
- Child agents are finite `$vibe-loop <task_id>` workers. The supervisor owns
  continuation; workers own their slice lifecycle and integration attempt.
- `main-integration` is an advisory lock for the final refresh/verify/merge
  window, not a central merge queue.
- Workspace ownership is advisory metadata on the task lock. It records the
  claimed branch, worktree path, base commit, and current git state so
  `workers` and `doctor` can report missing worktrees, duplicate branch
  worktrees, already-merged active branches, and dirty foreign-owned workspaces.
  It does not authorize the supervisor to create, delete, reset, or merge those
  branches/worktrees.

## Non-Goals

- No central merge queue that takes ownership away from workers.
- No automatic conflict resolution by the supervisor.
- No parallel `infinite-vibe-loop` workers; the supervisor owns continuation,
  and children run finite `$vibe-loop <task_id>` slices.
- No forced cleanup of worktrees, branches, or locks without explicit evidence
  that they are safe to remove.
- No treating a stale or mismatched workspace claim as permission to steal
  another worker's branch, dirty worktree, or integration lock.
