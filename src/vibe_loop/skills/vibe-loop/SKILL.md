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

## Invocation Contract

If an agent starts using this skill, whether through a slash command, a prompt
template, an external orchestrator, or direct invocation, it must follow the
loop to conform to user expectations. Treat the skill as the task contract, not
optional background guidance.

Carry the selected slice through inspection, implementation, verification,
review, review remediation, commit, integration when permitted, cleanup, and
final summary. Stop early only on explicit user redirection, missing access,
required approval, destructive-action confirmation, or a decision that cannot be
made safely. Report the precise blocker and any safe completed work.

Keep a compact slice state while working: objective, acceptance criteria,
workspace/branch, verification evidence, review status, blockers, and
integration/cleanup status. Update it after implementation, checks, review,
blockers, integration, and final summary. This is finite slice state, not a
cross-slice backlog.

## Core Loop

1. Inspect the task, code, tests, docs, repo instructions, worktree state, and
   constraints; if the prompt includes spec-aware worker context, use it as
   bounded source context for linked requirements, design references,
   fingerprints, and verification gates; create or choose the slice workspace
   and initialize slice state before implementation edits.
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
   `main`, apply the cleanup authorization rule below, and stop.

## Task Source State

For CLI-supervised runs, completion is reflected through the repository's active
task source, not through the supervisor run record alone. Before reporting a
slice as completed, update the relevant task source so the task is no longer
runnable there: for example, mark the Markdown task row `Done`, update the
project tracker, or verify that the configured command-backed adapter now
returns a completed/non-runnable status. If that update is blocked by policy,
tooling, or missing access, report the slice as blocked or unknown with the
specific reason instead of claiming completion only in local run metadata.

This is a deliberate part of the workflow model: task status remains
project-owned, so agents and humans working without the `vibe-loop` CLI can
manage the same backlog through the normal plan, tracker, or adapter.

## Review

Spec review checks requested behavior and evidence. Code-quality review checks
implementation, tests, security, performance, UX, maintainability, and repo fit.
Review may take longer than coding; do not shorten or skip it for speed.

Reviews must use a separate reviewer, sub-agent spawning is explicitly
authorized. Use the user's preferred review tools when specified; otherwise
prefer a subagent with clear context.
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
behavior, or directly adjacent implementation work. Avoid unrelated refactors
and speculative product changes.

If part of the bounded task needs missing access, credentials, destructive-action
confirmation, or an unsafe decision, park that part and continue only with
independent in-scope work. If all in-scope paths are blocked, report the specific
missing approval, access, or decision.

## Worktree And Integration

For full-cycle bounded work, use a dedicated branch/worktree; keep `main` clean
during implementation. Create the slice branch/worktree before implementation
edits begin.

When an external supervisor provides advisory workspace ownership, record the
claimed branch and worktree immediately after creating or choosing the
workspace and before implementation edits. Treat that ownership data as
diagnostic coordination state only: it never authorizes automatic cleanup,
branch deletion, resets, lock stealing, or central merge-queue behavior.

For a CLI-launched command-backed task, the supervisor must acquire the exact
task lock, invoke the repository-configured lifecycle adapter, and confirm a
non-runnable in-progress task state before starting the worker process. The
worker must not create, claim, or edit a workspace if repository evidence
contradicts that confirmed state. Activation remains project task-source state;
worker reports remain separate attempt outcomes.

If a dedicated worktree cannot be created, or repo/user policy forbids creating
one, state the precise blocker and proceed in the primary worktree only after
inspecting its current state. Do not silently fall back to editing the primary
worktree.

When repo/user policy permits local full-cycle delivery, this skill includes
permission to create the slice branch/worktree, commit the slice, fast-forward
merge it to `main`, and verify on `main`. Do not push, force-push, reset, or
bypass repo/user policy.

Cleanup is a separate, potentially destructive action. Apply effective user and
repository instructions before removing a worktree, deleting a local branch, or
deleting files: an explicit no-delete or confirmation-required instruction
overrides this skill's general cleanup workflow. A request to run the loop,
integrate the task, or report completion is not cleanup approval. When approval
is absent or deletion is prohibited, leave the merged worktree and branch
intact, report the exact worktree path and local branch name, and still record
the task as completed with commit provenance. When cleanup is expressly
authorized and repo policy permits it, remove only a clean, merged worktree and
its local branch after verifying ownership and that no active agent still uses
it.

If repo/user policy forbids local integration or requires missing approval, stop
with the precise blocker after the reviewed slice is committed.

When an external supervisor provides an advisory integration lock, use its
wait/timeout path for the final refresh, verification, fast-forward merge, and
immediate `main` verification section. If the lock is unavailable or reports
unsafe workspace state, park the slice as blocked with the precise reason
instead of entering integration.

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
