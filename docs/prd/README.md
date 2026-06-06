# PRD Index

`docs/prd/` is the Level 2 product and component contract layer. PRDs translate
the Level 1 seed in `PROMPT.md` into stable requirements that can be decomposed
into Level 3 plan rows in `PLAN.md`.

The PRD set records target contracts for the product surfaces named by
`PROMPT.md`. Detailed design notes remain useful; PRDs provide stable
requirement IDs for review and future traceability.

## Authority

- Code and tests remain the source of truth for implemented runtime behavior.
- PRDs describe intended contracts and acceptance criteria. A PRD is not proof
  that behavior is implemented.
- `PLAN.md` remains the scheduler-facing implementation plan for `vibe-loop`.
- `.vibe-loop/` remains local run, lock, cache, and analytics state. It is not a
  project completion ledger.
- README sections may explain positioning and supported workflows, but they do
  not replace PRD contracts or plan rows.

## ID Rules

PRD IDs are contract IDs. Plan IDs are implementation-slice IDs. Keep existing
plan IDs stable when adding PRD coverage.

| Namespace | File | Scope |
| --- | --- | --- |
| `PRD-CLI-*` | `cli-runtime.md` | CLI commands, configuration, agent command resolution, stdout/stderr contracts, local state, and release packaging. |
| `PRD-TSK-*` | `task-discovery.md` | Task-source normalization, Markdown profiles, generated discovery cache, command adapters, precedence, and degraded states. |
| `PRD-WRK-*` | `worker-supervision.md` | Worker execution, locks, reports, parallel scheduling, workspace claims, integration locking, and stale state visibility. |
| `PRD-SKL-*` | `skills.md` | Bundled finite and infinite skills, installation, workflow contracts, review discipline, and skill release readiness. |
| `PRD-ANL-*` | `planning-analytics.md` | Planning evidence, timeline/Gantt artifacts, duration benchmarking, coverage semantics, and `doctor` readiness. |
| `PRD-EVL-*` | `evals-release.md` | Local skill eval suites, artifact schema, aggregate reporting, external benchmark adapters, and release gates. |
| `PRD-SDE-*` | `spec-driven-execution.md` | Execution-engine support for spec-driven workflows, task-layer adapters, traceability, gates, drift checks, worker context, and completion evidence. |
| `PRD-AUT-*` | `autopilot.md` | Persistent autopilot supervision, reusable status core, append-only cycle records, future multi-project management, and TUI/WebUI readiness. |

Plan rows should cite PRD IDs in `Scope`, `Acceptance`, or `Evidence` when a
slice implements or changes a contract. A single plan row may satisfy multiple
PRD IDs, and a single PRD ID may require many plan rows.

## Semi-Autonomous Flow

1. Read `PROMPT.md`, relevant PRD files, `PLAN.md`, and repository instructions.
2. Select or add one Level 3 plan row with a permanent implementation task ID.
3. Implement only that row's scoped contract change.
4. Verify with tests, deterministic CLI checks, fixture runs, evals, or
   documented manual evidence proportional to risk.
5. Run spec-compliance review before code-quality review for non-trivial
   behavior changes.
6. Update `PLAN.md` and any affected PRD when the slice changes the intended
   contract.

`vibe-loop` should consume Level 3 tasks. It should not schedule `PROMPT.md` or
PRD files directly.
