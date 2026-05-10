# CLI Runtime PRD

This PRD owns Level 2 contracts for the `vibe-loop` command-line runtime,
configuration, command output, local state boundaries, and package/release
surface.

## PRD-CLI-001 Command Surface

The CLI must expose task inspection, task configuration, single-slice execution,
multi-slice execution, worker visibility, run inspection, planning analytics,
doctor diagnostics, main integration locking, worker reporting, workspace
claiming, evals, and skill installation through stable subcommands.

Acceptance must cover documented commands, scriptable JSON where commands
promise it, user-facing diagnostics for missing task sources or missing agent
commands, and compatibility behavior such as `vibe-loop tasks` aliasing
`vibe-loop tasks runnable`.

Related implementation IDs: `CORE-01`, `CORE-02`, `DISC-04`, `PAR-01`,
`PAR-03`, `PAR-06`, `PAR-11`, `GANTT-01`, `GANTT-06`, `EVAL-03`.

## PRD-CLI-002 Configuration Authority

`.vibe-loop.toml` must be the explicit user-authored configuration boundary.
Defaults may keep the CLI usable, but explicit config must remain authoritative
over generated cache and auto-detected behavior.

Acceptance must cover `main_branch`, `state_dir`, `agent`, `task_source`,
`completion`, and `planning_analytics` settings; explicit-versus-default source
tracking; safe reporting that does not print raw command strings where those may
include local sensitive details; and validation errors for unsupported values.

Related implementation IDs: `AGENT-02`, `AGENT-04`, `DISC-01`, `DISC-04`,
`GANTT-01`, `GANTT-05`.

## PRD-CLI-003 Completion Checks

Completion commands must be explicit repository-configured checks that can help
classify a worker attempt when structured worker reports are absent or
insufficient.

Acceptance must cover `[completion].commands`, command execution after worker
exit, failure diagnostics, interaction with task probing, and the rule that
completion checks support classification but do not replace the final project
ledger or worker-owned verification evidence.

Related implementation IDs: `PAR-03`.

## PRD-CLI-004 Agent Command Resolution

Worker and selection commands must be configurable template strings, with
deterministic defaults for supported prompt-mode agents when explicit config is
absent.

Acceptance must cover Codex-only, Claude-only, both-present, neither-present,
explicit override, worker `{task_id}` and `{run_id}` interpolation, selection
`{prompt}` interpolation, shell quoting, command-source diagnostics, and clear
failure when no supported agent command is available.

Related implementation IDs: `AGENT-01`, `AGENT-02`, `AGENT-04`.

## PRD-CLI-005 Output And Logging Contract

Agent-facing result JSON must remain on stdout, while supervisor progress,
mirrored worker stdout, empty-queue messages, and diagnostics that are not the
result payload must go to stderr. Full worker stdout and stderr must be captured
in per-run logs.

Acceptance must cover `run_id`, native `session_id` detection when emitted,
fallback `session_id = run_id` semantics, `session_id_source`, invalid JSONL
tolerance in run records, and stable log paths under the configured state
directory.

Related implementation IDs: `CORE-01`, `AGENT-03`, `PAR-01`, `PAR-03`,
`PAR-06`.

## PRD-CLI-006 Local State Boundary

Runtime state must live under the configured `state_dir` by default and remain
separate from project-owned task sources, worklogs, PRDs, or generated reports
that a repository explicitly opts into committing.

Acceptance must cover `.vibe-loop/locks`, `.vibe-loop/runs`,
`.vibe-loop/runs.jsonl`, generated task-source cache, planning analytics
artifacts, eval output directories, state-dir configurability, and no accidental
mutation of repository docs from read-only commands.

Related implementation IDs: `CORE-01`, `DISC-02`, `DISC-04`, `GANTT-06`,
`EVAL-03`.

## PRD-CLI-007 Permission And Isolation Policy

When permission prompts are disabled for Codex or Claude, sessions launched
directly with bundled skills or by CLI worker commands must run in an isolated
environment with only the required repository, tools, network access, and
credentials available.

Acceptance must cover README safety guidance, skill and worker-command
documentation, no hidden assumption that `dontAsk` is safe on a broad host, and
clear separation between convenience recommendations and safe execution
requirements.

Related implementation IDs: `DOC-02`, `DOC-03`, `SKILL-01`.

## PRD-CLI-008 Packaging And Release Runtime

The package must install as a standalone CLI, expose bundled skills, and support
release workflows through `uv`, Python packaging metadata, and GitHub trusted
publishing.

Acceptance must cover supported Python version, install commands, release
workflow behavior, TestPyPI/PyPI target distinction, tag/version matching, and
release-readiness evidence requirements for bundled skill changes.

Related implementation IDs: `EVAL-06`.
