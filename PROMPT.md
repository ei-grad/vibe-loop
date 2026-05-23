# Vibe Loop Seed Prompt

This file is the Level 1 seed for building and maintaining full-system
`vibe-loop` PRD files. It is intentionally high level: it defines the target
product shape, operating philosophy, stack bias, architecture boundaries, and
rules for writing Level 2 PRDs. Component contracts belong in `docs/prd/`,
runnable implementation slices belong in `PLAN.md`, and detailed design notes
belong under `docs/`.

## Product Thesis

`vibe-loop` is an execution engine for bounded AI coding work. It consumes a
repository's task layer, selects one or more safe runnable slices, launches
finite worker agents, records logs and run state, and leaves durable evidence
that humans and later agents can inspect.

Spec-driven tools and repository planning workflows can own intent authoring:
requirements, designs, approvals, task decomposition, and spec deltas.
`vibe-loop` should sit below those tools as the execution layer for the task
artifact they produce.

Full-system PRDs should cover six durable product surfaces: CLI/runtime
configuration, task-source discovery, worker supervision, bundled workflow
skills, planning analytics, and skill evaluation/release readiness.
Spec-driven execution is the cross-cutting direction that connects those
surfaces to higher-level PRDs, requirements, and task artifacts.

## Operating Philosophy

- Keep authoring and execution separate. Spec tools own intent; `vibe-loop`
  owns repeatable task execution, locks, logs, reports, and evidence.
- Treat natural-language artifacts as contracts only when they have stable IDs,
  acceptance criteria, source provenance, and verification evidence.
- Preserve repository-local planning conventions instead of forcing every
  project into one task format.
- Prefer non-executable parser profiles and explicit user-authored command
  adapters over generated executable behavior.
- Keep workers finite. Long-running continuation belongs to the supervisor or
  an outer loop, not to a single open-ended agent session.
- Make coordination visible before making it automatic: task locks, worker
  reports, workspace claims, integration locks, and drift diagnostics should be
  inspectable before they become policy gates.
- Avoid false authority. A PRD, spec, run log, or completed task row is not proof
  of correct behavior unless tests, reviews, or explicit evidence support it.

## Opinionated Stack

- Python 3.11+ package and CLI.
- `uv` for local development, test execution, and packaging workflows.
- TOML configuration in `.vibe-loop.toml`.
- Repo-local state under the configured `state_dir`, defaulting to
  `.vibe-loop/`.
- Markdown and command-backed task sources normalized into a stable `Task`
  model.
- Agent commands treated as configuration, with Codex and Claude prompt-mode
  defaults when available.
- Markdown skills as reusable workflow contracts for finite and unattended
  coding loops.
- Deterministic JSON/JSONL records for runs, locks, generated task profiles,
  planning analytics, evals, and future spec traceability.

## Architecture Decisions

- `vibe-loop` is a supervisor, not a branch/worktree manager. Worker agents own
  branch/worktree setup, implementation, review, and repo-approved integration.
- Task discovery is explicit-first. User-authored config and command adapters
  win over generated parser profiles.
- Generated task-source profiles are non-executable descriptions over bounded
  repo evidence. They may describe how to parse task artifacts, but they must
  not introduce commands or hooks.
- Runtime scheduling consumes normalized tasks with stable IDs, statuses,
  dependencies, acceptance text, evidence text, and optional resource/path
  conflict domains.
- Parallel execution is safe only when readiness, locks, and declared conflict
  domains allow it. Unknown conflict domains remain conservative.
- Run attempts are not final project history. Final completion evidence belongs
  in task status, worker reports, commit trailers, worklogs, or future
  spec-trace records.
- Planning analytics report on the task/run/evidence graph; they must not become
  a hidden scheduler or completion source.
- Bundled skills are self-sufficient workflow contracts. They must work when
  invoked directly by an agent session, a slash command, a prompt template, or
  an external orchestrator — not only under the `vibe-loop` CLI. Skills must
  not reference CLI commands (`vibe-loop report`, `main-integration`,
  `claim-workspace`) or CLI environment variables (`VIBE_LOOP_*`). The runner
  appends CLI-specific coordination instructions at launch time through its
  worker addendum; that addendum is the only place where CLI contracts appear
  in the worker prompt.

## Viable System Model Mapping

`vibe-loop` maps onto Stafford Beer's Viable System Model. This mapping
explains the role of each subsystem and guides where new functionality belongs.

- **System 1 — Operations.** Worker agents that execute individual task slices.
  Each worker owns its branch/worktree, implementation, review, and integration
  lifecycle. Workers are finite and independent.
- **System 2 — Coordination.** Task locks, conflict-domain scheduling,
  integration locks, and workspace claims. These mechanisms prevent interference
  between concurrent S1 workers without centralized control. Coordination is
  advisory and inspectable, not automatic.
- **System 3 — Control.** The scheduler/dispatcher: task selection, `run-next`,
  `run-until-done`, `--jobs N`, refill, and restart budgets. S3 allocates work
  to S1 workers and monitors their completion.
- **System 3\* — Audit.** `doctor`, stale lock detection, workspace diagnostics,
  and worker state visibility. Sporadic read-only checks that surface problems
  without taking corrective action.
- **System 4 — Intelligence.** Planning analytics, duration estimation,
  agent-assisted selection, and timeline projections. S4 observes the
  environment and informs future decisions, but does not directly drive
  scheduling. The boundary between S3 and S4 is intentional: analytics report,
  they do not actuate.
- **System 5 — Policy.** User configuration (`.vibe-loop.toml`), task-source
  authority, `PROMPT.md`, and repository-level conventions. S5 sets identity and
  ultimate authority. The task source — not the runtime — remains the source of
  truth for project state.

New features should strengthen the system they belong to rather than blur
boundaries between systems. S2 coordination should remain advisory. S3\*
audit should remain read-only. S4 intelligence should inform without actuating.

## Runtime Evolution Direction

- Extend `runs.jsonl` with typed lifecycle events beyond `run_result` and
  `worker_report`: lock acquisitions and releases, workspace claims, run state
  transitions, and restart attempts. New record types are additive; readers must
  tolerate unknown types.
- Make run state transitions explicit and inspectable. A run progresses through
  observable states — scheduled, started, session observed, workspace claimed,
  reported, classified, finalized — recorded as events and derivable from the existing
  append-only log.
- Lock backends should be pluggable. Directory-based advisory locks are the
  default, but repositories that share `state_dir` across hosts or integrate
  with external coordination should be able to use command-backed lock adapters,
  mirroring the task-source command adapter pattern.
- Support optional lock leases with time-based expiry alongside PID-based stale
  detection. Fencing tokens prevent stale holders from corrupting newer lock
  state. Advisory locks without leases remain the default.
- Make restart budgets explicit and configurable rather than hardcoded. When a
  budget is exhausted, the supervisor escalates to a clear failed state rather
  than retrying silently.
- Keep concurrency decisions explicit and user-controlled. `--jobs N` is the WIP
  limit. `doctor` and `workers` commands surface WIP count, blocked ratio, and
  lock contention as diagnostics, not as inputs to automatic adjustment.

## Spec And Planning Flow

Use three levels:

1. Level 1: `PROMPT.md` sets system philosophy, design direction, stack choices,
   architecture boundaries, and PRD-writing rules.
2. Level 2: `docs/prd/` owns product and component contracts with stable
   `PRD-*` IDs.
3. Level 3: `PLAN.md` owns runnable implementation slices with permanent task
   IDs consumed by agents and `vibe-loop`.

Semi-autonomous agents may decompose PRDs into plan rows, implement plan rows,
verify behavior, and propose PRD updates. They must not silently change a PRD
contract to fit an implementation. When implementation discovers that a PRD is
wrong, the contract change should be explicit in the same reviewed slice or
parked as a gated planning item.

## PRD Writing Rules

- Assign stable requirement IDs under the namespace documented in
  `docs/prd/README.md`.
- State the user, operator, maintainer, or integrator outcome before
  implementation details.
- Name affected CLI commands, config fields, task-source contracts, run records,
  worker contracts, skills, or generated artifacts.
- Use constrained acceptance language for behavior that tests, command output,
  review, or deterministic diagnostics can prove.
- List negative/error cases for stale specs, unsafe generated profiles,
  ambiguous task sources, missing approvals, conflicting workers, secret-like
  evidence, and unverifiable completion claims.
- Put runnable implementation steps in `PLAN.md`, not in PRDs.
