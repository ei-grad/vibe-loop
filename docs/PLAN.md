# Vibe Loop Plan

## Parallel Worker Orchestration

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
2. It selects up to `N` ready, unlocked tasks.
3. It locks each task and spawns one finite worker per task with the configured
   `agent.command`, defaulting to `codex exec '$vibe-loop <task_id>'`.
4. Each worker writes to its own run log and performs the full slice lifecycle.
5. Workers use an advisory `main-integration` lock around the final refresh,
   verification, fast-forward merge, and immediate `main` verification.
6. The supervisor watches worker exits, re-reads task state, records results,
   and fills open worker slots until no runnable tasks remain.

The supervisor must not resolve merge conflicts, run reviews for workers, or
merge worker branches itself. It can retry, report, or park tasks when worker
results are missing or ambiguous.

## Foundation Contracts

- `runs.jsonl` is append-only and versioned. The CLI may add richer record types
  later, but readers must tolerate unknown fields and skip invalid JSON lines.
- Agent-facing result JSON remains stable and scriptable on stdout. Supervisor
  progress, worker output mirroring, and empty-queue messages belong on stderr.
- Markdown task discovery is explicit-first. If `task_source.plan_path` is set,
  use it directly; otherwise score parseable `.md` files and require an
  unambiguous best candidate.
- Agent execution is configurable. `codex exec` is the default worker command,
  not a required runtime dependency; other prompt-mode agents such as
  `claude -p` should be supported as first-class configured commands.
- Child agents are finite `$vibe-loop <task_id>` workers. The supervisor owns
  continuation; workers own their slice lifecycle and integration attempt.
- `main-integration` is an advisory lock for the final refresh/verify/merge
  window, not a central merge queue.

## Implementation Order

1. Finish single-worker foundations: run records, flexible plan discovery,
   scriptable output, and task locks.
2. Add explicit worker reporting before adding parallel process supervision.
3. Add `--jobs N` with task locks and per-worker logs, still without resource
   locks.
4. Add `main-integration` lock support and update bundled skills to use it.
5. Add worker/runs visibility commands and stale state reporting.
6. Add agent-assisted batch selection and optional resource/path locks.

## Task Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| CORE-01 | P0 | Done | none | Refactor `.vibe-loop/runs.jsonl` behind a run store and stable result record schema instead of ad hoc append/read logic in `runner.py`. | Run results have stable `finished_at`, versioned JSONL records, reusable recent-log context, and invalid JSON lines are ignored during reads. | `src/vibe_loop/runs.py`; `tests/test_runs.py`; `uv run python -m unittest discover`. |
| CORE-02 | P0 | Done | none | Replace fixed `docs/PLAN.md` default assumptions with scored Markdown plan discovery while preserving explicit `task_source.plan_path`. | Any `.md` file outside ignored build/state dirs can be evaluated; the best unambiguous parseable task table is selected, and ambiguous ties require explicit config. | `src/vibe_loop/tasks.py`; `tests/test_tasks.py`; `uv run python -m unittest discover`. |
| AGENT-01 | P0 | Planned | CORE-01 | Analyze and support `claude -p` as a first-class configured worker/selection command on par with `codex exec`, without making either agent mandatory. | Configuration examples and tests prove `agent.command` and `agent.selection_command` can drive a Claude prompt-mode worker while preserving stdout/stderr logging, task id interpolation, and result recording. | CLI tests with a stub `claude -p` command; README/config docs showing Codex and Claude examples. |
| DOC-01 | P1 | Planned | AGENT-01 | Refine `README.md` positioning: mention that vibe-loop is inspired by umputun/ralphex and document the approach differences. | README explains the ralphex inspiration and contrasts vibe-loop's agent-agnostic commands, flexible task discovery/adapters, finite worker plus supervisor model, merge-to-main-after-each-slice flow, local locks/logs, and non-dedicated-plan workflow. | README diff review. |
| PAR-02 | P0 | Planned | CORE-01 | Add active run state for worker pid, task id, run id, log path, start time, base `main`, and command. State must be reconstructable from lock files plus run records. | `vibe-loop workers` shows running workers and stale/missing process state without reading raw logs. | Unit tests for active state load/save and stale process detection. |
| PAR-03 | P0 | Planned | CORE-01 | Add explicit worker result reporting, for example `vibe-loop report --run-id ... --task-id ... --status ... --commit ...`. Keep log/task probing as fallback only. | A worker can mark completed, blocked, failed, or unknown with structured metadata; supervisor prefers the report over heuristics. | CLI tests for report writing and supervisor classification from report records. |
| PAR-01 | P0 | Planned | CORE-02, PAR-02, PAR-03 | Add `run-until-done --jobs N` supervisor mode that starts multiple finite `$vibe-loop <task_id>` workers and keeps `run-next` single-worker. | With `--jobs 2`, two independent ready tasks can run concurrently, each with a task lock and separate log; with default settings behavior remains serial. | Unit tests for scheduling limits and task lock exclusion; CLI test showing status/log paths for concurrent workers. |
| PAR-04 | P0 | Planned | PAR-01 | Add an advisory `main-integration` lock command/API for workers to serialize final refresh, verification, fast-forward merge, and immediate `main` verification. | Concurrent workers cannot enter final integration at the same time; stale integration locks are visible and handled conservatively. | Lock manager tests plus a CLI test demonstrating one integration lock holder and one blocked waiter. |
| PAR-05 | P0 | Planned | PAR-03, PAR-04 | Update bundled `vibe-loop` and `infinite-vibe-loop` skills to use the report protocol and `main-integration` lock while preserving worker-owned integration. | Skill text tells finite workers how to acquire/release the integration lock, report results, and continue using after-merge review for complex/material interactions. | Diff review of bundled skills and install-skills output check. |
| PAR-06 | P1 | Planned | PAR-01, PAR-02 | Add supervisor visibility commands: `workers`, `runs list`, `runs inspect <run-id>`, and clearer stderr progress for spawned workers. | A user can see what is running, which logs to inspect, and the latest structured result without tailing every log manually. | CLI snapshot-style tests for worker and run output. |
| PAR-07 | P1 | Planned | CORE-02, PAR-01, PAR-03 | Add agent-assisted batch selection from mechanically safe candidates, recent logs, active workers, and task metadata. CLI must validate the returned batch. | `--ask-agent --jobs N` can choose a compatible batch, but invalid/locked/duplicate choices are rejected before spawning. | Unit tests for batch validation and fallback to deterministic ordering. |
| PAR-08 | P1 | Planned | PAR-04, PAR-07 | Add optional resource/path locks for repositories that can declare task conflict domains. Unknown resources remain conservative. | Two tasks with overlapping resources are not scheduled together; tasks with disjoint explicit resources can run concurrently. | Unit tests for resource matching and scheduler exclusion. |
| PAR-09 | P2 | Planned | PAR-02, PAR-03 | Add watchdog handling for worker crashes, stale locks, interrupted supervisor runs, and orphaned worktrees without deleting user work. | Stale state is reported with precise recovery commands; no automatic destructive cleanup occurs. | Unit tests for stale lock classification and docs for manual recovery. |

## Non-Goals

- No central merge queue that takes ownership away from workers.
- No automatic conflict resolution by the supervisor.
- No parallel `infinite-vibe-loop` workers; the supervisor owns continuation,
  and children run finite `$vibe-loop <task_id>` slices.
- No forced cleanup of worktrees, branches, or locks without explicit evidence
  that they are safe to remove.
