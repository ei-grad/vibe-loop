# Autopilot PRD

This PRD owns Level 2 contracts for persistent supervision above
`run-until-done`. Autopilot keeps a repository's existing `vibe-loop` workflow
alive, visible, and restartable without becoming the owner of task authoring,
worker implementation, branch/worktree management, review, merge, or cleanup.

## PRD-AUT-001 Reusable Status Core

Autopilot must be implemented as a reusable service/status core with thin CLI
rendering, so external views (such as the loopyard web UI) and multi-project
tooling can consume structured state without scraping terminal text.

Acceptance must cover a dedicated autopilot module, structured project status
objects, one-cycle result objects, bounded git/task/worker/lock/supervisor
summaries, text rendering separated from state collection, and no dependency on
live process memory for read-only status.

Autopilot must stay repository-agnostic. Its source, documentation, bundled eval
fixtures, and command output must not embed downstream project names or absolute
developer-machine paths, and a release check must assert this so the feature is
safe to ship and to surface in a future shared dashboard.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-05`.

## PRD-AUT-002 Command Surface

The CLI must expose autopilot through a subcommand group:
`vibe-loop autopilot run` and `vibe-loop autopilot status`. The bare
`vibe-loop autopilot` command may remain as a shorthand for `run`, but command
semantics must be explicit in help and tests.

Acceptance must cover `--repo`, `--jobs`, `--interval`, `--once`,
`--max-cycles`, `--ask-agent`, `--continue-on-failure`, `--max-slices`,
`--max-tasks`, `--min-ready`, and `--json` where scriptable output is promised.
Human cycle output should be compact by default: repo, queue, supervisor state,
blockers or actions, log path, and next wake.

Related implementation IDs: `AUTO-02`, `AUTO-03`.

## PRD-AUT-003 Append-Only Cycle Records

Autopilot state must be recorded through additive records in the target
repository's configured runtime journal, such as `.vibe-loop/runs.jsonl`, rather
than a separate hidden state file.

Records include `autopilot_cycle`, `autopilot_supervisor_started`,
`autopilot_supervisor_observed`, and `autopilot_command_result`. Each record
must carry schema version, record type, occurrence time, cycle id, repo, queue
counts, worker and lock summaries, current and previous main refs when
available, actions, blockers, child pid/log path when relevant, and next wake.
Existing run readers must keep tolerating unknown record types.

Related implementation IDs: `AUTO-01`, `AUTO-03`, `AUTO-04`.

## PRD-AUT-004 Child Supervisor

`autopilot run` must supervise `run-until-done` as a child process instead of
duplicating worker scheduling logic. It starts a child only when no live
autopilot-owned child is already active, preflight state permits launch, and
runnable work exists.

Acceptance must cover child command construction, log redirection under the
configured state directory, pid/log observation, no duplicate supervisor
launch, one-cycle mode, bounded cycle counts, foreground interval sleeping,
signal behavior, and classification of clean drain versus restartable exit
versus blocked state.

Related implementation IDs: `AUTO-03`.

## PRD-AUT-005 Configured Maintenance Hooks

Autopilot may run optional project-configured health, summary, troubleshoot, or
planning commands, but only when those commands are explicitly user-authored in
`.vibe-loop.toml`.

Acceptance must cover an `[autopilot]` config section, bounded command output,
safe environment variables, command-result records, command redaction in status
JSON, low-ready queue handling, and the rule that generated task-source
profiles cannot introduce maintenance commands.

Related implementation IDs: `AUTO-04`.

## PRD-AUT-006 Non-Destructive Recovery Boundary

Autopilot recovery must be diagnostic and conservative by default. It must not
reset branches, steal live locks, kill arbitrary processes, merge, push, rebase,
edit tracked project files, or mutate task sources. It must not delete worktrees
or branches except under the bounded, evidence-gated worktree-disposition
exception defined below. Configured maintenance commands are external
user-authored checks or planners; their presence does not authorize autopilot to
perform destructive recovery.

Two bounded exceptions are permitted, and no others:

1. **Stale-lock release.** Autopilot may automatically release stale worker task
   locks whose recorded worker process is missing, using the same validated
   release path and `lock_expired` audit records as `workers clean --force`. This
   recovers aborted workers without deleting worktrees or branches. It must not
   release a lock that has not yet observed a worker PID (so a just-started
   worker cannot lose its task lock before its launcher writes PID metadata), and
   it must not take over locks held by live processes.

2. **Worktree disposition.** Autopilot may remove a worker-created worktree and
   delete its branch (`git worktree remove` plus `git branch -d`) only under the
   evidence-gated, agent-decided contract in `PRD-AUT-010`. This recovers
   orphaned worktrees left by workers that died before reporting `completed`. It
   must never force-remove a dirty or unmerged worktree, never touch a worktree
   claimed by a live run, act only on a per-worktree keep-or-reap decision the
   read-only analysis agent returned with a reason (no blanket reap), and journal
   every decision and action to the append-only run store. Salvageable unmerged
   or dirty work-in-progress must be kept, not reaped.

Acceptance must cover unsafe workspace diagnostics, dirty repo state, missing
task source, unavailable agent command, no runnable work, and child launch
failure as explicit blockers or observations rather than destructive cleanup
triggers; stale locks with a still-live or PID-unobserved owner remain blocking,
while stale locks with a missing worker process are recovered and audited; and
worktree disposition reaps only orphaned, non-dirty, merged-or-disposable,
non-live-claimed worktrees under the `PRD-AUT-010` guardrails while keeping all
salvageable work-in-progress.

Unknown-run recovery (`PRD-AUT-014`) does not breach this boundary: it launches
a new continuation worker against the existing claimed branch/worktree and never
deletes, resets, steals, merges, or mutates another worker's committed work.

Related implementation IDs: `AUTO-01`, `AUTO-03`, `AUTO-04`, `AUTO-13`,
`AUTO-14`, `AUTO-21`.

## PRD-AUT-007 Multi-Project Shape

The first implementation may operate on one repository at a time, but the data
model must not assume a single project forever. Status payloads should represent
project identity and be composable into a list for future project registries and
dashboards.

Acceptance must cover stable fields for repo, display name, state directory,
current main ref, dirty state, queue counts, active workers, stale locks,
workspace diagnostics, supervisor, blockers, last cycle, and next wake. Per-repo
state stays under that repo's configured state directory; no global registry is
required for the first implementation.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-06`.

## PRD-AUT-008 TUI And WebUI Readiness

> **Superseded (removed from vibe-loop).** The in-tree `autopilot tui` (Textual)
> and `autopilot webui` surfaces were removed; interactive dashboards now live in
> the [loopyard](https://github.com/ei-grad/loopyard) web UI. The still-active
> requirement here is the machine-readable `autopilot status --json` boundary that
> loopyard consumes; the TUI/WebUI acceptance items below are historical.

TUI and WebUI implementations are future work, but autopilot must expose a
machine-readable status boundary that makes them straightforward follow-ups.

Acceptance must cover `autopilot status --json`, path-addressable logs,
append-only durable records as the source of truth, no raw command or secret
leakage in UI-ready payloads, no text scraping requirement, and no TUI/WebUI
runtime dependencies in the first autopilot slice.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-07`, `AUTO-08`.

## PRD-AUT-009 Read-Only Analysis Agent

Autopilot must be able to run a *read-only* analysis agent to make judgement
calls that the supervisor cannot decide mechanically, while keeping product-code
authorship limited to the existing read-write worker agent. The analysis agent
inspects evidence and returns a structured decision; it must never author or
mutate tracked project files.

The architectural contract is: the agent decides, `vibe-loop` executes
deterministically within safety guardrails, and every action is recorded to the
append-only journal so the system stays monitorable and recoverable. Analysis
and decision steps run a new read-only agent invocation distinct from the
read-write worker.

Acceptance must cover a per-agent-kind `analysis_command` default that is
read-only by construction (Claude disallows `Edit`/`Write`/`NotebookEdit` while
retaining `Read`/`Grep`/`Glob` so it can inspect work-in-progress to judge
salvageability; Codex uses a read-only sandbox), an `AgentConfig.analysis_command`
field with a `require_analysis_command()` accessor, parsing of the new config key,
reuse of the generic key-based command resolution, inclusion of `analysis_command`
in the generated-task-profile forbidden keys, and a runner entry point that
launches the analysis agent and parses its strict structured (JSON) decision
using the same prompt-delivery and shell-preparation validation as the existing
selection path. The analysis agent must not be granted write/edit tools and must
not be required for routine read-only status commands.

Related implementation IDs: `AUTO-12`, `AUTO-18`.

## PRD-AUT-010 Native Worktree Disposition Health Step

Autopilot cycles must include a native worktree-disposition health step so that
orphaned worktrees are reaped without per-project configuration. The root cause
of orphaned worktrees is that worktrees are created by the read-write worker and
only cleaned on a `completed` report; a worker that dies first (for example agent
quota exhaustion or a crash) leaves an orphaned worktree that nothing currently
reaps.

The step follows the agent-decides / code-executes / guardrails contract:
`vibe-loop` gathers per-worktree evidence mechanically (path, branch,
merged-into-main predicate, dirty state, the claiming run and whether its process
is alive); passes that evidence to the read-only analysis agent
(`PRD-AUT-009`); receives a per-worktree keep-or-reap decision *with a reason*;
and executes `git worktree remove` plus `git branch -d` only within safety
guardrails. There must be no blanket reap: salvageable unmerged or dirty
work-in-progress must be kept.

Acceptance must cover mechanical evidence gathering that reuses existing
workers.py helpers (worktree enumeration, merged-branch predicate, dirty-state
inspection, worker-view claim and liveness), an agent decision per worktree with
a reason, and code-side execution that never force-removes dirty or unmerged
worktrees, never removes a worktree claimed by a live run, and records each
decision and each action to the append-only journal. Side effects (git removal,
branch deletion) must be dependency-injected so tests do not run real git. The
step runs unconditionally in the cycle and must not become a destructive default
that violates the `PRD-AUT-006` non-destructive recovery boundary; it stays
within the bounded, evidence-gated exception this contract defines.

Related implementation IDs: `AUTO-13`, `AUTO-14`.

## PRD-AUT-011 Full Cycle Action Logging

Every autopilot cycle action, including native worktree disposition, must be
recorded to the append-only journal so the loop is monitorable and recoverable.
Each native maintenance behavior added to the cycle must register a typed record
in the run store's known record-type set so existing readers do not silently drop
it, and must append a concise action tag to the cycle's `actions` list while
emitting any detailed payload as a separate dedicated record.

Acceptance must cover a new `autopilot_worktree_reap` record type that mirrors
the existing maintenance-command-result record shape (schema version, record
type, occurrence time, repo, cycle id, and reaped/kept/errors/status payload),
registration of every new native record type in the run store's autopilot and
known record-type sets, tolerance of unknown record types on read, and a cycle
action tag (for example `reaped_worktrees:N`) appended for the disposition step.

Related implementation IDs: `AUTO-13`, `AUTO-14`, `AUTO-15`, `AUTO-16`,
`AUTO-17`.

## PRD-AUT-012 Configuration-Free Generic Cycle

`vibe-loop autopilot run` must behave like the generic `autopilot` skill cycle
without per-project configuration. Today the cycle is a supervisor plus
stale-lock cleaner plus four empty maintenance-command slots (health, summary,
troubleshoot, planning) that are all unset by default, so with no project config
the loop never reaps worktrees, checks disk, summarizes what landed, detects
recurring trouble, or plans new work.

The native generic cycle must provide repository-agnostic defaults for these
behaviors while preserving the non-destructive recovery boundary
(`PRD-AUT-006`): native worktree disposition (`PRD-AUT-010`), native disk-health
checks, a native "what landed" git-log summary, native troubleshoot detection
derived from `runs.jsonl`, and native planning that invokes the configured agent
rather than requiring a separate `planning_command` script. Project-authored
`[autopilot]` maintenance commands (`PRD-AUT-005`) continue to override or
augment the native behaviors; native behavior is the default, not a replacement
for explicit configuration.

Acceptance must cover each native behavior landing as an independently
reviewable slice, every native action appearing in the append-only journal
(`PRD-AUT-011`), and no native behavior performing destructive recovery outside
its declared evidence-gated guardrails.

Related implementation IDs: `AUTO-12`, `AUTO-14`, `AUTO-15`, `AUTO-16`,
`AUTO-17`, `AUTO-18`, `AUTO-19`.

## PRD-AUT-013 Observed Agent Session Id And Transcript Linkage

A run record must let an operator find the agent's real session transcript.
Today the worker is launched with `claude -p {prompt}` in default text mode,
which never emits its session id, so `runs.jsonl` records
`session_id_source: fallback:run_id` and only the wrapper-log path. The real
agent session transcript (for Claude,
`~/.claude/projects/.../<uuid>.jsonl`) is referenced nowhere, so what the agent
actually did cannot be recovered from a run record.

The worker invocation must surface the agent's real session id when the agent
can provide one, and the run records must persist both that id and the resolved
transcript path. For Claude the supervisor injects a known `--session-id <uuid>`
into the worker command (the alternative to `claude -p --output-format json`,
which would change stdout from streamed text to a JSON envelope and force the
selection/analysis text-scraping paths to adapt). Injection leaves stdout
unchanged, so streaming/progress output and the read-only selection/analysis
paths are untouched; the transcript path is resolved by globbing
`$CLAUDE_HOME/projects/*/<uuid>.jsonl` after the run, independent of Claude's
cwd encoding, and is skipped when the command already pins `--session-id`. For
Codex the CLI was inspected: `codex exec` has no flag to force or print a
session id without `--json` (which would replace the streamed human-readable
output the wrapper log and selection/analysis parsing rely on), so Codex worker
runs retain `fallback:run_id` and this limitation is documented in the README
rather than silently faked.

Acceptance must cover: a real `session_id` recorded with
`session_id_source: observed` (distinct from `fallback:run_id`) whenever the
agent surfaces one; the resolved transcript file path recorded on the
`run_started` and `run_result`/run context records; `fallback:run_id` retained
only when the agent genuinely surfaces no session id; preserved
streaming/progress behavior (the chosen `--session-id` injection leaves stdout
unchanged); and a documented path from a run record to the agent's transcript
file. This contract is independent of recovery and may land on its own.

Related implementation IDs: `AUTO-20`.

## PRD-AUT-014 Unknown-Run Recovery And Continuation

When a worker run ends without a clear terminal report — classified `unknown`,
or committed on its claimed branch but neither merged nor reported — the work is
orphaned: the supervisor stops or re-attempts from scratch and the
committed-but-unmerged-unreported work is left behind. The observed failure mode
is a worker that did real work, committed to its branch, then *parked* on a
billable external/authorization gate ("I'll continue when the monitor fires")
and exited; because `claude -p` is one-shot, the process exit left the run
`unknown`, never merged and never reported.

`run-until-done` must deterministically launch a bounded continuation worker for
such runs. The supervisor's deterministic control flow decides *when* to
recover; the agent does the *work*. Recovery must launch a continuation
read-write worker agent (the existing worker command path, not the read-only
analysis agent of `PRD-AUT-009`) with a recovery prompt that conveys the task
id, the prior `run_id`, the claimed branch and worktree (from the
`workspace_claim` record), the prior agent transcript path (from
`PRD-AUT-013`) and the wrapper log, and the instruction: the previous session
ended `unknown`; investigate what went wrong, finish the work and/or emit a
proper status (`completed`/`blocked`/`failed`); if blocked on an
external/authorization gate, report `blocked` with the precise reason — do not
park.

Recovery must be bounded: cap recovery attempts per task by reusing or extending
the existing `task_restart` counter/record so an `unknown → recover → unknown`
cycle cannot loop forever; after the cap, leave a clear `blocked`/`failed`
terminal record. Every recovery launch and outcome must be journaled
append-only.

This recovery stays inside the `PRD-AUT-006` non-destructive boundary: it
launches a *new* worker against the existing claimed branch/worktree and never
deletes, resets, steals, merges, or otherwise mutates another worker's
committed work; it neither reclassifies live work nor force-removes workspaces.
It depends on `PRD-AUT-013` because the recovery prompt must point the
continuation worker at the prior agent transcript.

Acceptance must cover: deterministic detection of `unknown` (and
committed-but-unmerged-unreported) runs in `run-until-done`; launch of a
read-write continuation worker with the recovery prompt context above; a
per-task recovery attempt cap built on the `task_restart` counter; a clear
terminal `blocked`/`failed` record once the cap is reached; append-only
journaling of each recovery launch and outcome; and no destructive action on
the claimed workspace.

Related implementation IDs: `AUTO-21`.

## PRD-AUT-015 Direct User Message Wake

`wait-helper` may poll a trusted external message adapter in addition to its
default process and wall-clock signals. The integration is explicit through
`--message-command`; vibe-loop does not import, detect, or infer a task backend.
The recipient is `--session-ref`, falling back only to `VIBE_LOOP_RUN_ID`, and
is passed to the adapter as `VIBE_LOOP_WAIT_SESSION_REF` without shell
interpolation.

The adapter prints one JSON document with `received` and `message`. A received
message must include a stable `id` and non-blank `content`; optional sender and
timestamp fields are preserved as structured data. A message wakes regardless
of PID `--mode`. Already-satisfied PID/deadline conditions retain precedence
and do not invoke the adapter. Nonzero exits, timeouts, or invalid JSON/schema
produce a safe `adapter_error` result and never silently degrade to clock-only
waiting or echo adapter stdout/stderr.

Acceptance must cover immediate and delayed messages, PID precedence, session
resolution, literal environment delivery, schema validation, bounded command
execution, safe errors, and unchanged behavior when no adapter is configured.

Related implementation IDs: `AUTO-22`.
