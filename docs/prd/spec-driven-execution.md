# Spec-Driven Execution PRD

This PRD owns Level 2 contracts for positioning `vibe-loop` as the execution
engine beneath spec-driven development workflows.

## PRD-SDE-001 Task-Layer Execution Boundary

`vibe-loop` must execute the task layer produced by a repository planning or
spec-driven workflow without taking over spec authoring, approval, or design
ownership.

Acceptance must cover README positioning, task-source configuration boundaries,
and examples that distinguish authoring tools from `vibe-loop` execution.

Related implementation IDs: `DOC-01`, `DISC-01`, `DISC-06`, `DOC-02`.

## PRD-SDE-002 Spec-Tool Task Adapters

`vibe-loop` should be able to consume task artifacts produced by common
spec-driven workflows such as Spec Kit, Kiro, OpenSpec, and repository-specific
PRD/plan systems.

Acceptance must cover non-executable parser profiles or explicit command
adapters, stable IDs, task statuses, dependencies, acceptance text, source
provenance, and clear degradation when a source cannot safely produce runnable
tasks.

Related implementation IDs: `DISC-03`, `DISC-05`, `DISC-09`, `SDD-01`.

## PRD-SDE-003 Requirement Traceability

Normalized tasks should optionally preserve links to higher-level intent:
requirement IDs, spec paths, design references, approval state, and source
fingerprints.

Acceptance must cover JSON output, command-backed task sources, Markdown
profiles, generated profiles, planning analytics, and backward compatibility
for task sources that do not expose traceability fields.

Related implementation IDs: `GANTT-01`, `GANTT-02`, `SDD-02`.

## PRD-SDE-004 Spec Approval And Drift Gates

Repositories should be able to opt into checks that prevent execution when a
task comes from an unapproved, stale, or internally inconsistent spec artifact.

Acceptance must cover read-only diagnostics, explicit override behavior,
stale-source fingerprint reporting, missing-requirement coverage, completed-task
evidence gaps, and safe behavior when a repository has no spec layer.

Related implementation IDs: `DISC-04`, `GANTT-02`, `SDD-03`.

## PRD-SDE-005 Spec-Aware Worker Context

When a worker is launched for a task with linked spec context, the prompt should
include a compact, bounded bundle of relevant requirement text, acceptance
criteria, design references, source fingerprints, and required verification
gates. That spec-aware context is appended independently of the executable agent
command and independently of whether the skill reference syntax is Codex-style
or Claude-style, but delivery requires the worker command template to consume
`{prompt}`. Traceable task launches with legacy task-id-only command templates
must fail clearly instead of silently dropping linked spec context.

Acceptance must cover size limits, secret-like path exclusions, deterministic
prompt construction, worker log visibility, prompt dialect compatibility, and
behavior when linked spec context is missing or stale.

Related implementation IDs: `PAR-03`, `PAR-07`, `SDD-04`.

## PRD-SDE-006 Completion Evidence From Specs To Code

`vibe-loop` should make it possible to audit which requirements a completed
slice satisfied and which commits, reports, tests, or reviews support that
claim.

Acceptance must cover commit trailers or worker-report metadata, planning
analytics ingestion, spec coverage reports, unmapped commit warnings, and
distinguishing attempted work from accepted completion evidence.

Related implementation IDs: `GANTT-02`, `GANTT-06`, `SDD-05`.

## PRD-SDE-007 Parallel Spec Task Waves

For task artifacts that declare dependencies and conflict domains, `vibe-loop`
should run independent tasks concurrently while preserving finite-worker
boundaries and review/integration discipline.

Acceptance must cover dependency readiness, resource/path conflicts,
agent-assisted batch validation, per-worker logs, structured reports, and
integration-lock behavior.

Related implementation IDs: `PAR-01`, `PAR-07`, `PAR-08`, `PAR-12`, `SDD-06`.
