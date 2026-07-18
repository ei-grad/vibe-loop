# CLI Runtime PRD

This PRD owns Level 2 contracts for the `vibe-loop` command-line runtime,
configuration, command output, local state boundaries, and package/release
surface.

> **Note:** Mentions of planning analytics below (the `planning` subcommands, the
> `[planning_analytics]` config settings, and planning-analytics state artifacts)
> are superseded — that feature was removed from vibe-loop. Timeline/Gantt
> reporting now lives in the [loopyard](https://github.com/ei-grad/loopyard) web
> UI. See `planning-analytics.md` for the retired contract.

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

Related implementation IDs: `AGENT-02`, `AGENT-04`, `CORE-03`, `DISC-01`,
`DISC-04`, `GANTT-01`, `GANTT-05`.

## PRD-CLI-003 Completion Checks

Completion commands must be explicit repository-configured checks that can help
classify a worker attempt when structured worker reports are absent or
insufficient.

Acceptance must cover `[completion].commands`, command execution after worker
exit, failure diagnostics, interaction with task probing, and the rule that
completion checks support classification but do not replace the final project
ledger or worker-owned verification evidence.

Related implementation IDs: `PAR-03`.

## PRD-CLI-004 Agent Command And Prompt Dialect Resolution

Worker and selection executable commands must be configurable template strings,
while worker prompt construction must be controlled by explicit agent kind or
prompt dialect metadata rather than by guessing from the command string.

The `[agent]` table supports `kind = "auto" | "codex" | "claude" | "custom"`.
Built-in kinds determine the bundled skill reference syntax directly: Codex uses
`$vibe-loop`, Claude uses `/vibe-loop`. `auto` keeps the deterministic
Codex-first default for omitted worker and selection commands. `custom` has no
implicit skill syntax; it must set `prompt_dialect` or `skill_ref_prefix` before
a worker prompt can be built. Existing unkinded explicit commands remain a
compatibility path only: the runtime may infer a known Codex or Claude dialect
from a simple command shape, or fall back to the legacy Codex-style prefix, but
diagnostics must identify that the prompt dialect came from legacy inference or
legacy defaulting and tell the user how to make it explicit.

Executable command resolution, model resolution, and prompt dialect resolution
are independent. `[agent]` and each named agent profile may set an optional
`model`. Omitted built-in commands include the kind-specific model flag only
when a model is resolved: Codex uses `-m`, while Claude uses `--model`. A task's
optional `model` overrides the selected profile's model without changing how
that profile is selected.

Explicit user-authored `command` and `selection_command` values remain
authoritative executable templates. `command` receives `{prompt}`, `{model}`,
`{task_id}`, and `{run_id}`; `selection_command` receives `{prompt}` and
`{model}`. `{model}` is shell-quoted and fails closed before launch when the
template references it but neither task nor profile configuration resolves a
model. Templates that omit `{model}` remain unchanged. The worker prompt is
constructed from the selected skill reference syntax, the normalized task, and
the runner's worker addendum. An optional top-level
`agent.worker_prompt_extra` plain-text value is appended to every generated
worker prompt, independently of the selected agent profile, with explicit
precedence over conflicting generic worker protocol. Selection and analysis
prompts do not receive the extension or use the worker skill reference syntax.
A worker command for a task with traceability metadata must include `{prompt}`;
task-id-only compatibility templates must fail clearly rather than silently
dropping spec-aware worker context.

Acceptance must cover Codex-only, Claude-only, both-present, neither-present,
explicit `kind`, explicit prompt dialect or skill prefix, explicit command
overrides including environment-prefixed Claude commands, custom commands with
and without explicit prompt syntax, legacy unkinded explicit commands, worker
`{prompt}`, `{task_id}`, and `{run_id}` interpolation, selection `{prompt}`
interpolation, shell quoting, command-source diagnostics, prompt-dialect/source
diagnostics, prompt-required diagnostics for traceable tasks, and clear failure
when no supported agent command or required custom prompt syntax is available.
Prompt extension acceptance must cover Codex and Claude dialects, routed agent
profiles, recovery runs, explicit conflict precedence, and unchanged prompts
when the setting is absent.

Related implementation IDs: `AGENT-01`, `AGENT-02`, `AGENT-04`, `AGENT-05`,
`AGENT-06`, `vl-worker-prompt-extension`.

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

Related implementation IDs: `CORE-01`, `CORE-03`, `DISC-02`, `DISC-04`,
`GANTT-06`, `EVAL-03`.

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
