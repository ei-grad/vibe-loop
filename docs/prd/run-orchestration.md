# Run Orchestration PRD

This PRD owns Level 2 contracts for deterministic, runtime-owned orchestration
of one bounded task lifecycle inside `vibe-loop run`: activation, workspace
provisioning, implementation, candidate stabilization, gates, review,
remediation, targeted closure, integration, task provenance, completion,
cancellation, and recovery. The design rationale and migration plan live in
`docs/deterministic-run-orchestration.md`.

These contracts describe the target runtime-owned mode. The legacy
worker-owned lifecycle (`PRD-WRK-001`) remains a supported compatibility mode
during migration; `PRD-ORC-011` governs coexistence. Where this PRD and
`PRD-WRK-*` describe the same mechanism (locks, reports, settlement), the
existing `PRD-WRK-*` contract is unchanged and referenced rather than
restated.

## PRD-ORC-001 Runtime-Owned Lifecycle State Machine

`vibe-loop run` must drive one task lifecycle through an explicit state
machine whose transitions are owned by named runtime components. No
acceptance-critical transition may depend on a model interpreting prose. A
model session may propose (candidates, findings, remediations, escalations),
but only the runtime validates the proposal, records it, and performs the
legal transition. Model sessions mutate lifecycle state exclusively through
explicit fenced runtime commands validated against the active task lock and
fencing token.

Acceptance must cover: the stage set (activation, workspace, implementing,
candidate, gates, review, remediation, closure, integration, provenance,
classification, finalization) with legal-transition enforcement; typed failure
transitions (`limit_wall`, `timed_out`, `stage_failed`, `blocked`,
`cancelled`) from every stage; journal-ahead recording so every transition is
derivable after process death; rejection of lifecycle mutations attempted
through model output text; a journaled task-source settlement step with a
named owner for every post-activation failure transition — including
terminal `blocked`/`stage_failed` outcomes and crash-derived failures — so a
task moved out of the runnable set by activation can never be stranded
in-progress by a failure that releases the lock (settlement mechanism:
`PRD-ORC-007`); and a per-transition ownership map naming the responsible
component for every state mutation and external process launch.

Related implementation IDs: `ORC-03` (`orc-lifecycle-state-machine`),
`ORC-09` (`orc-task-provenance-completion`, task-source settlement).

## PRD-ORC-002 Resolved Run Contract

Each supervised run must resolve a versioned run contract after task-lock
acquisition and before task-source activation — activation is the first
authoritative task-status mutation, so the contract must be durable before it
— and therefore before any workspace or repository mutation. The contract carries: mode,
implementer and reviewer routes (provider/model/effort/command identity),
gate list as typed references to allowlisted configured commands, review and
remediation budgets, integration and task-provenance settings, and the
identity plus version or digest of the config/profile/skill source that
produced or proposed it. Repository policy enters the runtime only as
validated contract input; arbitrary lifecycle shell orchestration is not a
valid contract value. Generated task-source profiles cannot introduce or
modify orchestration contract keys.

Acceptance must cover schema validation, pre-mutation recording, source
identity/digest capture, precedence (explicit config over profile over skill
proposal), rejection of non-allowlisted executable values, redaction rules,
and a recorded contract governing its run to completion even when repository
policy changes mid-flight.

Related implementation IDs: `ORC-02` (`orc-run-contract-record`).

## PRD-ORC-003 Workspace Pre-Provisioning And Fail-Closed Adoption

The runtime must provision or adopt a task-specific branch and linked worktree
after the task lock/activation fence and before the implementation agent
starts, record the workspace claim itself, and launch the agent with that
worktree as its working directory. A clean primary worktree remains on the
configured main branch and byte-for-byte unchanged from worker launch through
candidate integration. Existing task worktrees may be adopted only after
ownership, branch, cleanliness, base, and liveness checks; dirty or ambiguous
existing work is preserved fail-closed, never reset or deleted. Provisioning
failures unwind without leaking task locks or half-created workspaces.
Parallel jobs receive distinct worktrees and can never claim the primary
worktree. Recovery reuses a preserved worker-owned workspace for the same task
rather than silently creating a duplicate.

Acceptance must cover normal provisioning, safe adoption, dirty-primary and
name-collision failures, unwind on launch failure, jobs=2 separation,
primary-worktree non-mutation, and recovery adoption, per the re-scoped
`run-until-done-preprovision-worker-worktree` task.

Related implementation IDs: `ORC-04` (`run-until-done-preprovision-worker-worktree`).

## PRD-ORC-004 Runtime Gates And Candidate Stabilization

Configured gates must be executed by the runtime in the task worktree against
a recorded candidate, not self-reported by the implementer. The candidate
(head commit, base, changed paths) is either declared through a fenced worker
command or derived by the runtime from the claimed branch; gate results are
recorded as typed evidence referencing the gate's configuration key, exit
class, duration, and log. Gate failure routes to bounded remediation, not to
silent completion; gate evidence is part of the review request.

Acceptance must cover gate execution and evidence records, candidate
declaration and derivation, remediation budget enforcement on gate failure,
and refusal to enter review without a recorded candidate and passing gates.

Related implementation IDs: `ORC-05` (`orc-runtime-gates`).

## PRD-ORC-005 Reviewer Routing, Identity, And Continuation

Reviewer provider/model/effort/command must be selected by configuration,
independent of the implementer, and launched by the runtime with a typed
review request (candidate identity, changed paths, gate evidence, policy
references, pass kind, prior findings for closure). Reviewer output is
schema-validated into a verdict and findings; malformed output gets one
bounded re-ask then a typed failure. Remediation resumes the same implementer
session and targeted closure resumes the same reviewer session when the
provider supports continuation; when it cannot, the runtime records an
explicit continuation fallback with the reason and supplies prior-session
artifacts as context. Session identity, model/effort, and native usage are
recorded for every initial and closure pass.

Acceptance must cover independent route configuration and validation, typed
request/response round trips, Claude-implementer/Codex-reviewer and
Codex-implementer/Claude-reviewer matrices, missing reviewer command
diagnostics, continuation on resume-capable providers, recorded fallback on
non-resumable providers, and malformed-output handling.

Related implementation IDs: `ORC-06`
(`run-until-done-supervisor-review-routing`), `ORC-07`
(`orc-reviewer-continuation`).

## PRD-ORC-006 Findings Ledger And Review Budgets

Findings must persist as durable ledger records (stable id, severity, summary,
evidence, files, state) owned by the runtime. Review passes are budgeted: at
most the contract's initial passes plus targeted-closure passes per candidate
lineage, with closure passes rechecking recorded findings rather than
re-reviewing from scratch. Budget decisions use mechanical input only — the
candidate fingerprint (head commit plus changed paths) recorded with each
verdict; the runtime never resets a budget autonomously and no implementer or
reviewer output can. Exhaustion parks the run as a typed review-budget
failure with the ledger preserved; the only reset is a new dispatch with a
fresh contract, journaled as scheduler or operator action. Reviewer
concurrency is bounded
separately from implementation jobs; `jobs=1` still means one implementation
task per project. Status surfaces whether a task is implementing, reviewing,
remediating, or integrating.

Acceptance must cover ledger persistence and state transitions, budget
enforcement and exhaustion behavior, explicit budget-reset journaling,
separate reviewer concurrency, and stage-visible status output.

Related implementation IDs: `ORC-06`
(`run-until-done-supervisor-review-routing`), `ORC-07`
(`orc-reviewer-continuation`).

## PRD-ORC-007 Runtime Integration And Task Provenance

The final refresh, verification, fast-forward merge, and main verification
must be executed by the runtime inside the advisory main-integration lock
window, honoring the existing `PRD-WRK-007` lock semantics and the
no-commit `branch_already_merged` no-op case. In runtime-owned mode the
contract must declare a completion path and contract validation fails closed
before any mutation when none is available: either the runtime performs the
transition through an explicit `task_source.complete` adapter under the held
lock, or the contract declares external-confirmed completion and the runtime
confirms the authoritative done state by probing the task source before
recording provenance and reporting completed — a probe still showing the task
in progress parks the run blocked with the integrated candidate preserved.
Completion is never silently delegated back to prose. Ordering is invariant
and recoverable: review verdict before
integration, integration before provenance, provenance before the completed
report, durable local result before external settlement (`PRD-WRK-003`
unchanged).

Failure settlement is the completion path's counterpart. Every
post-activation failure transition must settle the task source under the
held lock with a typed intent — `requeue` to the runnable state via
`task_source.reset`, or `park` into the source's non-runnable held state via
an optional `task_source.park` adapter, with a recorded fallback to
`requeue` when park is unconfigured — journaled as `task_source_settled`
before the fenced lock release. On an activation-capable task source the
contract must include a settlement path; contract validation fails closed
before any mutation when `task_source.reset` is absent.
`task_source_settled` records only a confirmed settlement — the
authoritative task source observed non-in-progress — never a merely
attempted adapter call. A failed or unconfirmed attempt is journaled as
`task_source_settlement_attempted` and satisfies neither the settlement
step, the durable-outcome settlement gate, nor fenced lock release: the run
remains `settlement_pending`, retains the task lock, and retries with
bounded backoff. After process death, stage-aware fenced recovery must use
the run's exact private lock identity, confirm the authoritative task
source non-in-progress, append `task_source_settled`, and only then
release; generic stale-lock cleanup must not release a settlement-pending
lock. Leaving a task in-progress after lock release is never a legal
settlement outcome.

Acceptance must cover the integration window and verification steps, conflict
and verification-failure transitions, the no-op case, adapter-configured and
unconfigured provenance paths, requeue and park settlement intents with
fallback recording, settlement-path fail-closed contract validation,
settlement-pending lock retention with fenced settlement recovery (including
stale-lock cleanup refusing settlement-pending locks), ordering enforcement,
and crash recovery at each boundary without duplicated effects.

Related implementation IDs: `ORC-08` (`orc-runtime-integration`), `ORC-09`
(`orc-task-provenance-completion`).

## PRD-ORC-008 Stage-Typed Quota And Retry Classification

Every stage subprocess result must be classified once by the runtime into
`ok`, `transient`, `limit_wall`, `timeout`, or `fatal`. A typed provider
limit on one route pauses that route without consuming the task restart
budget or triggering retries on another route. Usage is attributed by the
runtime to `implementation`, `initial_review`, `remediation`, or
`targeted_closure` phases from state-machine position, keeping worker-reported
phase as corroboration only.

Acceptance must cover per-route wall pauses with and without reset evidence,
no blind retries against walls, restart-budget isolation, and phase-correct
usage records for all four phases across both providers.

Related implementation IDs: `ORC-06`
(`run-until-done-supervisor-review-routing`).

## PRD-ORC-009 Scheduler And Runtime Separation

`run-until-done` must schedule independent `vibe-loop run` lifecycles —
selection, conflict domains, slots, restart/recovery budgets, backoff — and
must not own lifecycle internals. `autopilot` keeps health/recovery/planning
policy above the scheduler. In runtime-owned mode the generated worker prompt
must describe only the implementation stage and the fenced commands available
to the worker, not lifecycle steps the runtime owns.

Acceptance must cover scheduler behavior parity in both modes, prompt-content
assertions for runtime-owned mode, and unchanged autopilot boundaries.

Related implementation IDs: `ORC-10` (`orc-scheduler-separation`).

## PRD-ORC-010 Skill Composition And Operating Modes

Skills remain the adaptive intent and policy layer and must keep working in
both operating modes: invoked interactively without a supervisor (carrying
the full workflow, as today), and invoked as the implementation-stage content
inside a supervised run. Deliberate overlap between skill guidance and runtime
invariants is permitted where it improves guidance, enforcement, portability,
or interactive use; deduplication is explicitly not an objective. A skill or
profile may propose contract inputs (gates, rubric, budgets) through the
validated contract path; no skill or model response can bypass workspace,
review, quota, provenance, or integration invariants. Missing skills, version
skew, changed repository policy, and partially supported providers degrade
with recorded diagnostics, never silent behavior changes.

Acceptance must cover interactive-mode preservation (skill files still free
of CLI commands and `VIBE_LOOP_*` variables), supervised-mode composition,
contract-mediated skill proposals, invariant-bypass rejection fixtures, and
degradation diagnostics for missing/mismatched skills and providers.

Related implementation IDs: `ORC-10` (`orc-scheduler-separation`).

## PRD-ORC-011 Migration And Legacy Compatibility

An explicit `[orchestration] mode` selects worker-owned (initial default) or
runtime-owned orchestration; the active mode and contract are recorded per
run, and a run may never record a mode it does not execute — while the
runtime-owned path is incomplete, selecting it fails closed with an
actionable not-yet-available diagnostic instead of silently executing the
worker-owned lifecycle. Migration proceeds in independently shippable phases
that never silently
weaken repository review policy: a repository-mandated reviewer becomes an
enforced runtime route before the prose that mandated it is relaxed. New
journal record types are additive; existing readers tolerate them; runs
without stage records are treated as worker-owned by recovery and are never
reinterpreted. The default flips to runtime-owned only after the compatibility
matrix (Codex/Claude in both roles, command-backed task sources and locks,
recovery fixtures) is green; worker-owned removal is a separate later
decision.

Acceptance must cover mode selection and provenance, per-phase compatibility
of worker-owned behavior, additive-journal tolerance, legacy recovery
semantics for pre-migration runs, and documented default-flip criteria.

Related implementation IDs: `ORC-02` (`orc-run-contract-record`), `ORC-11`
(`orc-migration-default-flip`).
