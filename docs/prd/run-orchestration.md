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
existing work is preserved fail-closed, never reset or deleted. Adoption also
requires the recorded current main base to be an ancestor of the workspace
HEAD; an older workspace base appearing in current main history is not
sufficient. An ordinary adoption whose workspace fails that requirement is
refreshed automatically when it is clean by merging the current main base into
the owned branch. Commits reachable only from the task branch are preserved by
the merge. A content conflict is aborted, recorded as the distinct
`workspace_refresh_conflict` condition, and leaves the branch and worktree
unchanged for deliberate resolution. Any failed merge that entered merge state
is aborted before its failure is recorded, including failures from repository
commit hooks. An abort failure is a distinct restoration condition rather than
ordinary dirty-work evidence. Any condition that cannot be proven -- an
unreadable Git state, a HEAD that moved under the check, a recovery adoption
resuming against a recorded workspace state -- falls back to deferral. The
preflight records a bounded typed decision, retry disposition, and -- when a
refresh was declined -- the closed-vocabulary reason it was declined, before
any implementation process starts, so an operator can distinguish a merge
conflict, preserved dirty work, and unreadable state. The durable claim must
still match the validated branch, current base,
`HEAD`, and content-sensitive dirty snapshot; a change between preflight and
claim fails closed. Deferred recovery persists only a bounded state fingerprint and remains
suppressed in serial and parallel dispatch until the relevant base, branch,
`HEAD`, Git identity, or dirty state changes. Suppressed checks do not consume
restart, recovery, or attempt budget. The same durable state gate applies to a
normal dispatch rejected before launch, preventing ordinary task selection from
bypassing the deferral on later supervisor cycles. Provisioning
failures unwind without leaking task locks or half-created workspaces.
Parallel jobs receive distinct worktrees and can never claim the primary
worktree. Recovery reuses a preserved worker-owned workspace for the same task
rather than silently creating a duplicate.

Acceptance must cover normal provisioning, safe adoption, dirty-primary and
name-collision failures, unwind on launch failure, jobs=2 separation,
primary-worktree non-mutation, non-conflicting refresh of a diverged owned
branch, named conflict preservation, and recovery adoption, per the re-scoped
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

The candidate's base is the commit its workspace was provisioned from, which
for an adopted workspace is legitimately older than `main` at run start. When
that base is behind the integration branch, the runtime may re-anchor the
candidate only through a conflict-free rebase whose aggregate binary diff and
changed paths are unchanged. Every attempt records a typed base-anchor outcome
and the rewritten candidate receives fresh gate evidence. Re-anchoring is an
optimization over the merge integration already performs, so a conflict or a
content divergence refuses and preserves the candidate rather than failing the
run before review. Repeated base drift past the bound resolved in the run
contract is the one case that parks the run, because such a run cannot produce
gate evidence against a settled base at all.

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
explicit continuation fallback with the reason and launches a fresh independent
reviewer with only the candidate, gate evidence, and recorded open-findings
ledger as closure context. The fresh reviewer verifies those findings' closure
checks rather than repeating the initial review. Its session identity must
differ from the original reviewer's, and each start and verdict records
`fresh_closure` provenance plus the prior reviewer identity; the original
review records remain unchanged. If the fresh reviewer cannot be launched, the
run blocks with the concrete unresolved findings instead of remaining in the
review stage. Session identity, model/effort, and native usage are recorded for
every initial and closure pass.

The exact runtime-owned Codex reviewer command `codex review {prompt}` has no
supported way to bind a first-class provider/model/effort route without changing
argv. Project config cannot override `model_provider`, and the command exposes
no effective-route metadata surface. A profile combining that exact command
with `model` or `effort` therefore fails during run-contract resolution, before
candidate disclosure or executor launch. The exact command remains valid when
both settings are unset. The runtime does not inspect Codex session rollouts or
create temporary config state to infer the route.

Acceptance must cover independent route configuration and validation, typed
request/response round trips, Claude-implementer/Codex-reviewer and
Codex-implementer/Claude-reviewer matrices, missing reviewer command
diagnostics, continuation on resume-capable providers, recorded fallback on
non-resumable providers, distinct fresh-closure provenance, blocked closure
with unresolved findings when no independent reviewer is available, and
malformed-output handling.

The continuation contract also forbids unbudgeted nested reviewer/model
delegation. Provider launch policy disables nested Agent/Task use when the
surface supports it; observable structured nested-launch events invalidate the
verdict, journal a typed policy violation, and attribute nested usage
separately. A `review_started` record without a matching verdict is an
incomplete durable wait, not authority to replay a fresh full review after
worker or supervisor recovery.

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

Budget initialization is a dispatch-contract event. Candidate changes never
create a reset event; they retain the lineage budget and consume its remaining
targeted-closure allowance. A new dispatch is the only runtime-recognized fresh
budget boundary.

Related implementation IDs: `ORC-06`
(`run-until-done-supervisor-review-routing`), `ORC-07`
(`orc-reviewer-continuation`).

## PRD-ORC-007 Runtime Integration And Task Provenance

The final refresh, verification, fast-forward merge, and main verification
must be executed by the runtime inside the advisory main-integration lock
window, honoring the existing `PRD-WRK-007` lock semantics and the
no-commit `branch_already_merged` no-op case. In runtime-owned mode the
runtime resolves a clean mainline advance itself and reruns the configured
gates before fast-forwarding. A content conflict remains under the same lock
and receives one bounded continuation of the implementing session with the
exact conflicted paths and integration-base commit. The runtime accepts only a
clean two-parent merge-resolution commit, reruns the integration gates, and
then continues the same run. A conflicted path whose approved-candidate content
differs from the integration base cannot resolve to the base unchanged; that
would discard the approved side rather than resolve it. If the continuation
cannot produce a valid resolution, the terminal run evidence must name the
preserved approved candidate branch and commit; a generic conflict reason alone
is insufficient. The runtime never performs semantic conflict resolution
itself.

The runtime-owned contract must declare a completion path and contract
validation fails closed
before any mutation when none is available: either the runtime performs the
transition through an explicit `task_source.complete` adapter under the held
lock, or the contract declares external-confirmed completion with an explicit
operator or external-system transition actor and the runtime confirms the
authoritative done state through the selected task source's native or
command-backed probe capability before recording provenance and reporting
completed — a probe still showing the task in progress parks the run blocked
with the integrated candidate preserved. A
runtime-owned contract that names the implementation worker as the transition
actor is rejected because that worker is forbidden from changing the
authoritative task source.
Completion is never silently delegated back to prose. Ordering is invariant
and recoverable: review verdict before
integration, integration before provenance, provenance before the completed
report, durable local result before external settlement (`PRD-WRK-003`
unchanged).

Failure settlement is the completion path's counterpart. Every
post-activation failure transition after worker launch must settle the task
source under the held lock with a typed intent — `requeue` to the runnable
state via `task_source.reset`, or `park` into the source's non-runnable held
state via an optional `task_source.park` adapter, with a recorded fallback to
`requeue` when park is unconfigured — journaled as `task_source_settled` before
the fenced lock release. On an activation-capable task source the contract must
include a settlement path; contract validation fails closed before any mutation
when `task_source.reset` is absent.

Integration-lock contention does not consume a second implementation or review
pass. The runtime records each expired acquisition attempt against the approved
candidate, including the holder task and run identities, then retries the
serialized integration window once. If the retry also expires, the runtime
records retry exhaustion and returns a blocked result instead of waiting
indefinitely. The wait per attempt is configurable as
`orchestration.integration_lock_timeout_seconds`; its 900-second default exceeds
this repository's measured 328-second verify-on-main duration and leaves time
for refresh, merge, provenance, and routine variance.

A failure before worker launch must not retain its task lock. If activation may
already have succeeded, pre-launch finalization releases the lock and
immediately attempts an unfenced `requeue`; the attempt is journaled as
`task_source_settled` when confirmed or `task_source_settlement_attempted` with
recovery instructions when it remains unconfirmed.

`task_source_settled` records only a confirmed settlement — the
authoritative task source observed non-in-progress — never a merely
attempted adapter call. A failed or unconfirmed attempt is journaled as
`task_source_settlement_attempted` and satisfies neither the settlement
step, the durable-outcome settlement gate, nor fenced lock release: the run
remains `settlement_pending`, retains the task lock, and retries with
bounded backoff. On Linux, the task lock records the supervisor PID, kernel
process-birth identity, and the worker launcher's publication-barrier guarantee.
The launcher blocks on an inherited pipe before invoking either a direct worker
or `/bin/sh`; the supervisor opens the barrier only after the worker PID is
durable in the lock and start journal. Recovery may therefore treat a
pre-worker run as dead only when that guarantee is present and the exact
supervisor identity is absent or has been replaced: supervisor death closes the
pipe before any configured command can execute. Once the barrier opens, only
the published worker identity is authoritative. Other platforms and legacy
locks stay identity-ambiguous and fail closed. After process death, stage-aware
fenced recovery must use the run's exact private lock identity, confirm the
authoritative task source non-in-progress, append `task_source_settled`, and
only then release; generic stale-lock cleanup must not release a
settlement-pending lock without that process-death proof. Leaving a task
in-progress after lock release is never a legal settlement outcome.

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

Agent-assisted selection receives only mechanically safe candidates plus
bounded recent run evidence. The scheduler validates returned task ids, rejects
duplicates and candidates that are unknown, locked, or in an overlapping
declared conflict domain, and falls back to deterministic ready order.
`run-next` dispatches one worker. `run-until-done` is serial by default and
`--jobs N` bounds concurrent implementations. Its independent dispatch budget
(`--max-slices`) counts every attempt, while its completion budget
(`--max-tasks`) counts only completed results; parallel dispatch may not
overshoot the remaining completion budget.

An explicit per-task agent route that cannot satisfy the run contract fails
that task with the route diagnostic and is excluded for the rest of the current
scheduler invocation. This task-scoped refusal does not activate a worker and
does not stop `run-until-done` from dispatching other candidates, even when
general continue-on-failure behavior is disabled.

Acceptance must cover scheduler behavior parity in both modes, safe
agent-selection validation and deterministic fallback, conflict-domain
exclusion, serial and parallel dispatch, independent attempt/completion limits
without overshoot, task-scoped route refusal with the refused task first in
dispatch order, prompt-content assertions for runtime-owned mode, and unchanged
autopilot boundaries.

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

An explicit `[orchestration] mode` selects worker-owned compatibility mode or
runtime-owned orchestration, which is the default; the active mode and contract
are recorded per run, and a run may never record a mode it does not execute —
while the
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
decision governed by the documented release and repository-inventory criteria.

Acceptance must cover mode selection and provenance, per-phase compatibility
of worker-owned behavior, additive-journal tolerance, legacy recovery
semantics for pre-migration runs, and documented default-flip criteria.

Related implementation IDs: `ORC-02` (`orc-run-contract-record`), `ORC-11`
(`orc-migration-default-flip`).

Acceptance evidence: provider-role and default-contract coverage lives in
`tests/test_orchestration.py`; explicit worker-owned isolation, legacy-journal
classification, and the slow-gate-to-review runtime lifecycle live in
`tests/test_runner.py`; the runtime-owned implementation-stage user story and
release matrix live under `eval/examples/local-demo-v1` and
`src/vibe_loop/eval_release.py`.
