# Skills PRD

This PRD owns Level 2 contracts for bundled workflow skills and their
installation/release behavior.

## PRD-SKL-001 Finite Skill Contract

The `vibe-loop` skill must guide one coherent bounded software-development slice
through inspection, implementation, verification, review, remediation, commit,
integration when permitted, cleanup, and final summary.

Acceptance must cover objective and acceptance tracking, worktree inspection,
repo-local planning, scoped edits, relevant checks, independent spec review,
independent code-quality review, remediation, final reporting, and stopping
after the finite slice. It must also cover early stop conditions: explicit user
redirection, missing access, required approval, destructive-action confirmation,
unsafe decisions, precise blocker reporting, and safe completed work summaries.

Related implementation IDs: `PAR-05`, `SKILL-01`, `EVAL-02`, `EVAL-03`.

## PRD-SKL-002 Infinite Skill Contract

The `infinite-vibe-loop` skill must own unattended continuation across finite
slices while requiring each slice to satisfy the finite skill contract.

Acceptance must cover candidate queue maintenance, conservative next-slice
selection, never voluntarily stopping before explicit user instruction or
session end, blocker parking, branch/worktree discipline, fast-forward
integration, cleanup, and continuation after summaries or corrections.

Related implementation IDs: `PAR-05`, `SKILL-01`, `EVAL-03`.

## PRD-SKL-003 Review Discipline

Bundled skills must treat independent spec review and code-quality review as
workflow gates for non-trivial slices, with enough context for reviewers to
judge requested behavior and implementation quality.

Acceptance must cover separate reviewer use, subagent authorization where
supported, review prompts containing request/criteria, changed files or diff,
verification results, evidence, constraints, and re-review after material
findings.

Related implementation IDs: `PAR-05`, `EVAL-02`, `EVAL-05`, `EVAL-09`.

## PRD-SKL-004 Integration Discipline

Bundled skills must preserve worker-owned branch/worktree management and final
integration discipline instead of turning the CLI into a central merge queue.

Acceptance must cover dedicated branch/worktree creation where permitted,
inspection before edits/review/integration, no silent fallback to primary
worktree, main-advanced inspection, fast-forward-only local integration where
allowed, main verification, after-merge review only when material interactions
exist, and no force-push/reset/bypass behavior.

Related implementation IDs: `PAR-04`, `PAR-05`, `PAR-10`, `PAR-12`, `SKILL-01`.

## PRD-SKL-005 Skill Installation

The CLI must install bundled skills into supported agent skill locations without
requiring the CLI for direct skill use.

Acceptance must cover Codex and Claude install targets, source skill files,
temporary/home install options used by tests, installed file drift checks, and
documentation that direct skill use and CLI-launched workers are both supported.

Related implementation IDs: `PAR-05`, `SKILL-01`, `EVAL-06`.

## PRD-SKL-006 Skill Release Readiness

Bundled skill changes must depend on the eval release gate before release, and
workflow-contract regressions must be fixed or explicitly parked with task IDs.

Acceptance must cover the skill publishing dependency on `PRD-EVL-005`, release
note evidence links, and documentation that skill readiness is proven by eval
records rather than by manual inspection alone.

Related implementation IDs: `EVAL-05`, `EVAL-06`, `EVAL-09`.
