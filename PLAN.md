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
- Task discovery is explicit-first. User-authored `.vibe-loop.toml` and command
  adapters win over generated state. Generic Markdown discovery must not require
  repositories to adopt vibe-loop's local planning table shape.
- Agent execution is configurable. `codex exec` is the default worker command,
  not a required runtime dependency; other prompt-mode agents such as
  `claude -p` should be supported as first-class configured commands.
- Child agents are finite `$vibe-loop <task_id>` workers. The supervisor owns
  continuation; workers own their slice lifecycle and integration attempt.
- `main-integration` is an advisory lock for the final refresh/verify/merge
  window, not a central merge queue.

## Repo-Specific Task Discovery Configuration

The current Markdown discovery model still assumes the repository exposes tasks
through a specific table:

`ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence`

That is acceptable for this repository's own plan, but it is the wrong generic
contract. A task-system-agnostic runner should not ask every repository to
reshape planning docs before it can discover work. The generic path should ask a
configured agent to analyze bounded repository evidence, generate a normalized
task-source profile, validate that profile mechanically, and cache it as
repo-local state.

The agent-generated profile should describe how to read existing repo artifacts,
not invent task state. It can map column names, heading/list conventions,
statuses, dependency syntax, done states, candidate files, and title/acceptance
extraction rules into the existing normalized `Task` model. The CLI must still
own validation: required fields, runnable statuses, dependency references,
duplicate IDs, parser output shape, and cache freshness are deterministic checks.
Generated profiles must be non-executable parser descriptions over bounded
repo-local evidence. Command adapters or any other executable task source may
only come from user-authored `.vibe-loop.toml`.

Cached configuration should live under the configured state directory, include
schema and prompt versions, source file fingerprints, provenance, confidence,
and the agent command used to generate it. Explicit `.vibe-loop.toml` settings
must override the cache, and `doctor`/task commands should report whether the
active task source came from user config, generated cache, or command output.
Default config values must not count as explicit user settings; the config loader
needs to preserve which `[task_source]` keys were present so generated state can
fill only unset behavior. Source-defining explicit settings such as command
adapters, profile paths, or plan paths win at source level. Non-source settings
such as explicitly configured runnable statuses override the matching generated
profile fields without disabling the generated source.

Stale or low-confidence cache should fail with an actionable configure command
instead of falling back to the fixed table format silently.

## Discovery Degradation Modes

Generated discovery should degrade in visible states, not by guessing harder.
The runner can tolerate missing optional metadata, but it needs a stable way to
decide whether a result is runnable, planning-only, or unusable.

- Explicit config or command adapters remain authoritative. If they are present
  and fail, report that failure instead of replacing them with generated state.
- A valid generated profile can omit optional fields such as priority, evidence,
  or acceptance, but must still produce stable IDs, titles, statuses, and source
  locations. Missing optional fields should be rendered as unknown, not invented.
- If dependencies cannot be inferred, tasks should be treated as independent
  only when the source format clearly has no dependency concept. Ambiguous
  dependency syntax should make the profile low-confidence.
- If statuses cannot be mapped, the agent may propose a conservative planning
  profile, but the CLI should not run tasks from it until a status policy is
  explicit.
- If stable task IDs do not exist, cache-local synthetic IDs are acceptable only
  for planning review. They must be tied to source fingerprints and treated as
  unstable across refreshes unless promoted into explicit configuration.
- If candidate task sources are missing, too large, unreadable, contradictory,
  or outside evidence-gathering limits, cache an unavailable/needs-input result
  with precise missing information instead of repeating an agent call on every
  command.

When task information is insufficient for runnable work, `vibe-loop` can launch
a bounded planning agent rather than fail blind, but only from explicit
configuration commands such as `tasks configure`, refresh, or a clearly named
opt-in flag. Read-only commands such as `tasks list`, `tasks runnable`, `next`,
and `doctor` should never launch an agent as a side effect. They may read fresh
cache and report the command that would refresh it.

The planning agent should inspect the same bounded evidence set, write a
structured planning result under the configured state directory, and classify
what is missing: candidate source paths, likely task format, unsupported fields,
questions for the user, and proposed explicit configuration. References to
`.vibe-loop/` in examples mean the default configured state directory, not a
hard-coded path. This planning cache is diagnostic state, not a task source,
until it passes the normal profile validation. It gives `doctor`,
`tasks configure`, and `tasks list` enough context to explain the degraded state
without polluting repository docs or silently creating runnable work.

The evidence collector for generated discovery should be deterministic and
testable. It should gather repo-local docs and config likely to describe work,
respect ignored build/state directories, enforce per-file and total byte limits,
skip binary files, skip secret-like paths such as `.env`, key files, credential
directories, and private state, and avoid environment-variable dumps entirely.
Any redaction should happen before prompt construction, and skipped evidence
should be listed by reason in the cache so users can see whether missing
information is a deliberate safety boundary or an actual absence of planning
data.

## Implementation Order

1. Finish single-worker foundations: run records, flexible plan discovery,
   scriptable output, and task locks.
2. Replace fixed generic Markdown discovery with validated repo-specific
   generated configuration, explicit degradation states, and cached planning
   diagnostics while keeping explicit config and command adapters authoritative.
3. Add explicit worker reporting before adding parallel process supervision.
4. Add `--jobs N` with task locks and per-worker logs, still without resource
   locks.
5. Add `main-integration` lock support and update bundled skills to use it.
6. Add worker/runs visibility commands and stale state reporting.
7. Add agent-assisted batch selection and optional resource/path locks.

## Task Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| CORE-01 | P0 | Done | none | Refactor `.vibe-loop/runs.jsonl` behind a run store and stable result record schema instead of ad hoc append/read logic in `runner.py`. | Run results have stable `finished_at`, versioned JSONL records, reusable recent-log context, and invalid JSON lines are ignored during reads. | `src/vibe_loop/runs.py`; `tests/test_runs.py`; `uv run python -m unittest discover`. |
| CORE-02 | P0 | Done | none | Replace fixed single-path plan defaults with scored Markdown plan discovery while preserving explicit `task_source.plan_path`. | Any `.md` file outside ignored build/state dirs can be evaluated; the best unambiguous parseable task table is selected, and ambiguous ties require explicit config. | `src/vibe_loop/tasks.py`; `tests/test_tasks.py`; `uv run python -m unittest discover`. |
| AGENT-01 | P0 | Planned | CORE-01 | Analyze and support `claude -p` as a first-class configured worker/selection command on par with `codex exec`, without making either agent mandatory. | Configuration examples and tests prove `agent.command` and `agent.selection_command` can drive a Claude prompt-mode worker while preserving stdout/stderr logging, task id interpolation, and result recording. | CLI tests with a stub `claude -p` command; README/config docs showing Codex and Claude examples. |
| DISC-01 | P0 | Planned | CORE-02 | Design the generated task-source profile schema, precedence rules, and degradation states. Cover explicit config overrides, default-versus-user-set config tracking, generated cache location under the configured state dir, schema/prompt versioning, source fingerprints, provenance, confidence, stale-cache behavior, partial profiles, planning-only profiles, unavailable/needs-input results, and the rule that generated profiles are non-executable parser descriptions only. | `PLAN.md` and config docs specify where generated discovery config is stored, when it is trusted, how it is invalidated, how degraded states are represented, which explicit settings override which generated fields, which generated fields are forbidden, and how users can inspect or override them. | Design diff review plus config parsing tests proving defaults do not block generated cache, explicit task-source settings do override it, and generated command adapters are rejected. |
| DISC-08 | P0 | Planned | DISC-01 | Build a deterministic evidence collector for generated discovery. It should enumerate allowed repo-local task evidence, enforce size limits, skip ignored build/state paths, skip secret-like files and credential directories, redact before prompt construction, and record skipped evidence by reason. | The collector produces a bounded evidence bundle and manifest without reading environment variables or sensitive paths; tests cover file type filtering, size caps, ignored directories, `.env`/key-file skips, redaction, and manifest output. | Unit tests for evidence collection and prompt input fixtures with skipped-evidence manifests. |
| DISC-02 | P0 | Planned | DISC-01, DISC-08, AGENT-01 | Add an explicit agent-driven `tasks configure` flow that uses the bounded evidence collector and asks the configured agent for a strict JSON task-source profile or structured degradation result. Runtime read-only task commands must not launch this agent unless the user passes a clearly named opt-in configuration flag. | With a stub agent, the CLI writes a validated cache for a repo-specific task format; malformed, low-confidence, unsupported, or incomplete profiles become explicit degraded cache records and do not change active runnable task behavior. `tasks list`, `tasks runnable`, `next`, and `doctor` reuse cache and print diagnostics without invoking the agent. | CLI tests with stubbed agent output; fixture repos for accepted, partial, unavailable, rejected, and read-only-no-agent-invocation paths. |
| DISC-03 | P0 | Planned | DISC-01 | Generalize Markdown parsing behind profile-driven adapters instead of the hard-coded `ID/Priority/Status/Dependencies/Scope/Acceptance/Evidence` header. Keep this repository's table as one supported profile, not the generic requirement. | Nonstandard Markdown tables and heading/list-based task docs can be normalized into `Task` objects from profile configuration; duplicate IDs, missing required fields, and dependency syntax errors fail clearly. | Unit tests for column aliases, reordered columns, heading/list extraction, dependency parsing, and validation failures. |
| DISC-07 | P0 | Planned | DISC-02, DISC-03 | Add a bounded discovery-planning run for cases where runnable task information is unavailable. It should launch the configured agent only from explicit configure/refresh entry points, then cache diagnostic planning output under the configured state directory without mutating project docs or creating runnable tasks. | Missing sources, ambiguous formats, no stable IDs, unmapped statuses, unreadable files, and agent-unavailable cases produce inspectable planning cache records with missing inputs, proposed config, source fingerprints, skipped-evidence reasons, and next safe action. Repeated read-only commands reuse fresh degraded cache instead of invoking the agent. | CLI tests for planning-only cache creation, no-agent degradation, negative-cache reuse, stale invalidation, read-only no-invocation behavior, and `doctor` diagnostics. |
| DISC-04 | P0 | Planned | DISC-02, DISC-03, DISC-07 | Load generated task-source cache at runtime with deterministic validation and clear diagnostics. Explicit `.vibe-loop.toml` task-source settings and command adapters must remain authoritative. | `tasks list`, `tasks runnable`, `next`, and `doctor` report whether task discovery used explicit config, generated cache, command output, or planning-only/unavailable cache; stale cache points to the configure command instead of silently enforcing the fixed table. | CLI tests for cache precedence, stale-cache errors, degraded-cache diagnostics, `doctor` output, and no-agent/no-cache diagnostics. |
| DISC-05 | P1 | Planned | DISC-04 | Add cache refresh and promotion ergonomics: force refresh, dry-run review of the generated profile, and documented promotion into `.vibe-loop.toml` when a repo wants committed configuration. | Users can regenerate a cache after planning docs move, inspect the proposed profile before activation, and copy a stable profile into explicit config without changing task semantics. | CLI tests for refresh/dry-run modes and README examples for generated versus committed config. |
| DISC-06 | P1 | Planned | DISC-04 | Update README and bundled skills so generic task discovery no longer documents the fixed table as a required format. Explain repo-specific generated profiles, validation boundaries, and command-backed task sources. | Docs present the fixed table only as an example, describe the agent-generated cache path, and preserve command-adapter guidance for issue trackers or custom task tools. | README and skill diff review. |
| DOC-01 | P1 | Done | none | Refine `README.md` positioning: mention that vibe-loop is inspired by umputun/ralphex and document the approach differences. | README explains the ralphex inspiration and contrasts vibe-loop's agent-agnostic commands, flexible task discovery/adapters, finite worker plus supervisor model, merge-to-main-after-each-slice flow, local locks/logs, and non-dedicated-plan workflow. | README section: `Relationship to ralphex`. |
| PAR-02 | P0 | Planned | CORE-01 | Add active run state for worker pid, task id, run id, log path, start time, base `main`, and command. State must be reconstructable from lock files plus run records. | `vibe-loop workers` shows running workers and stale/missing process state without reading raw logs. | Unit tests for active state load/save and stale process detection. |
| PAR-03 | P0 | Planned | CORE-01 | Add explicit worker result reporting, for example `vibe-loop report --run-id ... --task-id ... --status ... --commit ...`. Keep log/task probing as fallback only. | A worker can mark completed, blocked, failed, or unknown with structured metadata; supervisor prefers the report over heuristics. | CLI tests for report writing and supervisor classification from report records. |
| PAR-01 | P0 | Planned | DISC-04, PAR-02, PAR-03 | Add `run-until-done --jobs N` supervisor mode that starts multiple finite `$vibe-loop <task_id>` workers and keeps `run-next` single-worker. | With `--jobs 2`, two independent ready tasks can run concurrently, each with a task lock and separate log; with default settings behavior remains serial. | Unit tests for scheduling limits and task lock exclusion; CLI test showing status/log paths for concurrent workers. |
| PAR-04 | P0 | Planned | PAR-01 | Add an advisory `main-integration` lock command/API for workers to serialize final refresh, verification, fast-forward merge, and immediate `main` verification. | Concurrent workers cannot enter final integration at the same time; stale integration locks are visible and handled conservatively. | Lock manager tests plus a CLI test demonstrating one integration lock holder and one blocked waiter. |
| PAR-05 | P0 | Planned | PAR-03, PAR-04 | Update bundled `vibe-loop` and `infinite-vibe-loop` skills to use the report protocol and `main-integration` lock while preserving worker-owned integration. | Skill text tells finite workers how to acquire/release the integration lock, report results, and continue using after-merge review for complex/material interactions. | Diff review of bundled skills and install-skills output check. |
| PAR-06 | P1 | Planned | PAR-01, PAR-02 | Add supervisor visibility commands: `workers`, `runs list`, `runs inspect <run-id>`, and clearer stderr progress for spawned workers. | A user can see what is running, which logs to inspect, and the latest structured result without tailing every log manually. | CLI snapshot-style tests for worker and run output. |
| PAR-07 | P1 | Planned | DISC-04, PAR-01, PAR-03 | Add agent-assisted batch selection from mechanically safe candidates, recent logs, active workers, and task metadata. CLI must validate the returned batch. | `--ask-agent --jobs N` can choose a compatible batch, but invalid/locked/duplicate choices are rejected before spawning. | Unit tests for batch validation and fallback to deterministic ordering. |
| PAR-08 | P1 | Planned | PAR-04, PAR-07 | Add optional resource/path locks for repositories that can declare task conflict domains. Unknown resources remain conservative. | Two tasks with overlapping resources are not scheduled together; tasks with disjoint explicit resources can run concurrently. | Unit tests for resource matching and scheduler exclusion. |
| PAR-09 | P2 | Planned | PAR-02, PAR-03 | Add watchdog handling for worker crashes, stale locks, interrupted supervisor runs, and orphaned worktrees without deleting user work. | Stale state is reported with precise recovery commands; no automatic destructive cleanup occurs. | Unit tests for stale lock classification and docs for manual recovery. |

## Non-Goals

- No central merge queue that takes ownership away from workers.
- No automatic conflict resolution by the supervisor.
- No parallel `infinite-vibe-loop` workers; the supervisor owns continuation,
  and children run finite `$vibe-loop <task_id>` slices.
- No forced cleanup of worktrees, branches, or locks without explicit evidence
  that they are safe to remove.
