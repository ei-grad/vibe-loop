# Autopilot PRD

This PRD owns Level 2 contracts for persistent supervision above
`run-until-done`. Autopilot keeps a repository's existing `vibe-loop` workflow
alive, visible, and restartable without becoming the owner of task authoring,
worker implementation, branch/worktree management, review, merge, or cleanup.

## PRD-AUT-001 Reusable Status Core

Autopilot must be implemented as a reusable service/status core with thin CLI
rendering, so future TUI, WebUI, and multi-project views can consume structured
state without scraping terminal text.

Acceptance must cover a dedicated autopilot module, structured project status
objects, one-cycle result objects, bounded git/task/worker/lock/supervisor
summaries, text rendering separated from state collection, and no dependency on
live process memory for read-only status.

Related implementation IDs: `AUTO-01`, `AUTO-02`.

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
delete worktrees, reset branches, steal locks, kill arbitrary processes, merge,
push, rebase, edit tracked project files, or mutate task sources. Configured
maintenance commands are external user-authored checks or planners; their
presence does not authorize autopilot to perform destructive recovery.

Acceptance must cover stale locks, unsafe workspace diagnostics, dirty repo
state, missing task source, unavailable agent command, no runnable work, and
child launch failure as explicit blockers or observations rather than
destructive cleanup triggers.

Related implementation IDs: `AUTO-01`, `AUTO-03`, `AUTO-04`.

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

TUI and WebUI implementations are future work, but autopilot must expose a
machine-readable status boundary that makes them straightforward follow-ups.

Acceptance must cover `autopilot status --json`, path-addressable logs,
append-only durable records as the source of truth, no raw command or secret
leakage in UI-ready payloads, no text scraping requirement, and no TUI/WebUI
runtime dependencies in the first autopilot slice.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-07`, `AUTO-08`.
