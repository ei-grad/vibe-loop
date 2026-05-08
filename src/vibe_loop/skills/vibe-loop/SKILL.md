---
name: vibe-loop
description: Use for one-slice non-trivial AI coding work, full-cycle feature implementation, bounded project work, or finite "vibe loop" requests. Guides inspect-plan-edit-test-review-merge iteration with branch/worktree isolation.
---

# Vibe Loop

Use for one coherent slice of non-trivial bounded software development: feature
work, bug fixes with meaningful blast radius, multi-file project work, or
bounded requests that explicitly mention "vibe loop". Complete that slice
through review, integration to `main` when permitted, cleanup, and a final
summary, then stop so an external loop can invoke the next slice.

## Core Loop

1. Inspect the task, code, tests, docs, repo instructions, worktree state, and
   constraints; create or choose the slice workspace before implementation
   edits.
2. Plan the next coherent slice from repo-local sources first: instructions,
   design docs, roadmaps, issues, TODOs, and existing plans.
3. Edit in scoped increments and keep the project working.
4. Run relevant tests/checks. Prove acceptance scenarios with real assertions;
   use integration tests for external systems and UI automation/screenshots for
   UI changes.
5. Run independent spec review, then independent code-quality review when
   available.
6. Address findings with code, tests, or docs; re-review, preferably with the
   same reviewer, until no material findings remain or remediation is tracked.
7. Commit the reviewed slice, integrate it to `main` when permitted, verify on
   `main`, clean up the slice worktree/branch, and stop.

## Review

Spec review checks requested behavior and evidence. Code-quality review checks
implementation, tests, security, performance, UX, maintainability, and repo fit.
Review may take longer than coding; do not shorten or skip it for speed.

Reviews must use a separate reviewer. Prefer a subagent with clear context, or
use `codex review "<reviewer-instruction-prompt>" 2>/dev/null` as fallback.
The prompt must include the gate, request/criteria, changed files or diff,
verification results, evidence, and constraints/open questions. Follow repo
review policy first: `REVIEW.md`, `AGENTS.md`, `CLAUDE.md`, contribution guides,
CI docs, security checklists, and task-specific review instructions.

Expect review to take some time, 5-10-15+ minutes are normal.

## Scope And Workspace

Do not assume you are working alone. Inspect worktree state before each slice,
review, and integration; treat unexpected changes as external and do not revert
them unless the user explicitly asks for that exact action.

When priorities are underspecified, choose a conservative in-scope item from the
objective, backlog, failing checks, review findings, or obvious adjacent broken
behavior. Avoid unrelated refactors and speculative product changes.

If part of the bounded task needs missing access, credentials, destructive-action
confirmation, or an unsafe decision, park that part and continue only with
independent in-scope work. If all in-scope paths are blocked, report the specific
missing approval, access, or decision.

## Worktree And Integration

For full-cycle bounded work, use a dedicated branch/worktree; keep `main` clean
during implementation. Create the slice branch/worktree before implementation
edits begin.

If a dedicated worktree cannot be created, or repo/user policy forbids creating
one, state the precise blocker and proceed in the primary worktree only after
inspecting its current state. Do not silently fall back to editing the primary
worktree.

When repo/user policy permits local full-cycle delivery, this skill includes
permission to create the slice branch/worktree, commit the slice, fast-forward
merge it to `main`, verify on `main`, then remove the merged worktree and local
branch. Do not push, force-push, reset, or bypass repo/user policy.

If repo/user policy forbids local integration or requires missing approval, stop
with the precise blocker after the reviewed slice is committed.

When branch integration is permitted and `main` advanced after a slice passed
review:

1. Inspect new `main` commits since the slice base; use them plus the slice diff
   to choose verification.
2. Merge current `main` into the slice worktree, resolve conflicts, and run that
   verification.
3. If clean, fast-forward `main` to the reviewed branch through the repo-approved
   flow, for example:
   `git -C <worktree> merge main && git -C <main> merge --ff-only <branch>`.
4. Rerun relevant verification on `main`.
5. If conflicts were complex or behavior-affecting, or new `main` commits
   materially interact with the slice, run after-merge review on `main`; fix
   findings with a follow-up reviewed change.

Do not require pre-merge re-review solely because `main` advanced. This is not
permission to merge unreviewed work, force-push, bypass tests, or bypass repo
policy.
