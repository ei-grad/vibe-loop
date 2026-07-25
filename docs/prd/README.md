# PRD Index

`docs/prd/` is the Level 2 product and component contract layer. PRDs translate
the Level 1 seed in `PROMPT.md` into stable requirements that can be decomposed
into Level 3 entries in the configured task source.

The PRD set records target contracts for the product surfaces named by
`PROMPT.md`. Detailed design notes remain useful; PRDs provide stable
requirement IDs for review and future traceability.

## Authority

- Code and tests remain the source of truth for implemented runtime behavior.
- PRDs describe intended contracts and acceptance criteria. A PRD is not proof
  that behavior is implemented.
- The loopyard `vibe-loop` project is this repository's scheduler-facing task
  source and implementation history.
- `.vibe-loop/` remains local run, lock, cache, and generated-discovery state.
  It is not a project completion ledger.
- README sections may explain positioning and supported workflows, but they do
  not replace PRD contracts or task-source entries.

## ID Rules

PRD IDs are contract IDs. Task IDs are implementation-slice IDs. Keep existing
task IDs stable when adding PRD coverage.

| Namespace | File | Scope |
| --- | --- | --- |
| `PRD-CLI-*` | `cli-runtime.md` | CLI commands, configuration, agent command resolution, stdout/stderr contracts, local state, and release packaging. |
| `PRD-TSK-*` | `task-discovery.md` | Task-source normalization, Markdown profiles, generated discovery cache, command adapters, precedence, and degraded states. |
| `PRD-WRK-*` | `worker-supervision.md` | Worker execution, locks, reports, parallel scheduling, workspace claims, integration locking, and stale state visibility. |
| `PRD-SKL-*` | `skills.md` | Bundled finite and infinite skills, installation, workflow contracts, review discipline, and skill release readiness. |
| `PRD-ANL-*` | `planning-analytics.md` | _(Superseded — feature removed; timeline/Gantt now in loopyard.)_ Planning evidence, timeline/Gantt artifacts, duration benchmarking, coverage semantics, and `doctor` readiness. |
| `PRD-EVL-*` | `evals-release.md` | Local skill eval suites, artifact schema, aggregate reporting, external benchmark adapters, and release gates. |
| `PRD-SDE-*` | `spec-driven-execution.md` | Execution-engine support for spec-driven workflows, task-layer adapters, traceability, gates, drift checks, worker context, and completion evidence. |
| `PRD-AUT-*` | `autopilot.md` | Persistent autopilot supervision, reusable status core, append-only cycle records, future multi-project management, and status-boundary readiness. _(In-tree TUI/WebUI removed; dashboards now in loopyard.)_ |
| `PRD-ORC-*` | `run-orchestration.md` | Deterministic runtime-owned task lifecycle inside `vibe-loop run`: run contracts, workspace pre-provisioning, runtime gates, reviewer routing/continuation, findings ledger, integration and task provenance, stage-typed quotas, and worker-owned-mode migration. |

Task bodies should cite PRD IDs in their scope, acceptance, or evidence when a
slice implements or changes a contract. A single task may satisfy multiple PRD
IDs, and a single PRD ID may require many tasks.

## Density Budget

PRDs are contract indexes, not exhaustive implementation references. Measure a
requirement-bearing PRD's density as its UTF-8 byte count divided by the number
of stable `## PRD-*` requirement headings. The median density across
requirement-bearing files in this directory is the baseline; a file above twice
that median is over budget.

An over-budget PRD has no growth allowance. A change may reduce it, split it
along contract boundaries, or move non-contract detail to a linked design or
reference document. A change may grow it only if the resulting file is within
budget. Before promoting README content into a PRD, record the before-and-after
density in review evidence and reconcile duplicate or conflicting prose rather
than appending a second account. Enforcement tooling belongs to the separate
documentation size/structure gate.

## Semi-Autonomous Flow

1. Read `PROMPT.md`, relevant PRD files, the authoritative loopyard task body,
   and repository instructions.
2. Select or add one Level 3 task with a permanent implementation task ID.
3. Implement only that task's scoped contract change.
4. Verify with tests, deterministic CLI checks, fixture runs, evals, or
   documented manual evidence proportional to risk.
5. Run spec-compliance review before code-quality review for non-trivial
   behavior changes.
6. Update the loopyard task and any affected PRD when the slice changes the
   intended contract.

`vibe-loop` should consume Level 3 tasks from the configured task source. It
should not schedule `PROMPT.md` or PRD files directly.
