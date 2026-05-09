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

If an agent starts using this skill, including through a `$vibe-loop` prompt or
a `vibe-loop` CLI worker command, it must follow the loop to conform to user
expectations. Treat the skill as the task contract, not optional background
guidance.

Carry the selected slice through inspection, implementation, verification,
review, review remediation, commit, integration when permitted, cleanup, and
final summary. Stop early only on explicit user redirection, missing access,
required approval, destructive-action confirmation, or a decision that cannot be
made safely. Report the precise blocker and any safe completed work.

When launched by the `vibe-loop` CLI, the worker receives `VIBE_LOOP_REPO`,
`VIBE_LOOP_RUN_ID`, `VIBE_LOOP_TASK_ID`, and `VIBE_LOOP_LOG`. Use those values
for worker status reports and integration locking. If those variables or the CLI
are unavailable because the skill is being used directly, continue the finite
slice normally and state that no structured supervisor report was sent.

Keep a compact slice state while working: objective, acceptance criteria,
workspace/branch, verification evidence, review status, blockers, and
integration/cleanup/report status. Update it after implementation, checks,
review, blockers, integration, worker report submission, and final summary. This
is finite slice state, not a cross-slice backlog.

## Task Source Context

When invoked with a task id, treat the task details as normalized work from the
repository's active task source. That source may be explicit configuration,
a generated profile cache at `<state_dir>/generated-task-source.json`,
command-backed adapters, issue trackers, or Markdown planning docs. Do not
require repositories to reshape their docs into this repository's example
Markdown table before doing the slice.

If task details are insufficient, inspect repo-local sources and task CLI output
before making assumptions. Generated profiles and command adapters describe how
to discover work; they do not replace acceptance evidence, verification, or
review for the selected slice.

## Core Loop

1. Inspect the task, code, tests, docs, repo instructions, worktree state, and
   constraints; create or choose the slice workspace and initialize slice state
   before implementation edits.
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
   `main`, clean up the slice worktree/branch, submit a worker result report
   when running under the CLI, and stop.

## Worker Reports

Workers launched by the `vibe-loop` CLI should explicitly report their final
status before exiting:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \
  --task-id "$VIBE_LOOP_TASK_ID" --status completed --commit HEAD \
  --message "completed $VIBE_LOOP_TASK_ID"
```

Use `completed` only after the reviewed slice has been integrated when
integration is permitted, verified on `main`, and cleaned up. Use `blocked` for
missing access, required approval, an unavailable integration lock, or a
decision that cannot be made safely. Use `failed` when an attempted slice cannot
be left working despite reasonable debugging. Use `unknown` only when the worker
cannot classify the result. Include the best available commit reference and a
concise message; include `--metadata-json` only for structured facts that help
the supervisor or later review.

When a blocker or failure occurs after code was changed, commit or otherwise
stabilize the slice according to repo policy before reporting unless doing so
would be unsafe. Do not let the report replace the final user-facing summary;
the report is supervisor state, while the summary explains what happened.

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

If a dedicated worktree cannot be created, or repo/user policy forbids creating
one, state the precise blocker and proceed in the primary worktree only after
inspecting its current state. Do not silently fall back to editing the primary
worktree.

When repo/user policy permits local full-cycle delivery, this skill includes
permission to create the slice branch/worktree, commit the slice, fast-forward
merge it to `main`, verify on `main`, then remove the merged worktree and local
branch. Do not push, force-push, reset, or bypass repo/user policy.

If repo/user policy forbids local integration or requires missing approval, stop
with the precise blocker after the reviewed slice is committed, and submit a
`blocked` worker report when running under the `vibe-loop` CLI.

When running under the `vibe-loop` CLI and preparing to refresh, verify,
fast-forward merge to `main`, and immediately verify `main`, acquire the
advisory main-integration lock first:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

If the lock is held by another live worker, wait and retry or park the slice as
blocked; do not enter the final integration section without the lock. If the
lock appears stale, report the precise status and follow repo policy rather than
stealing it. If acquisition fails because the worker is not under an active
`vibe-loop` task lock, treat that as a blocker unless explicit repo/user policy
allows direct integration.

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

Release the advisory integration lock after the immediate `main` verification,
or immediately when integration is parked. Then clean up the merged worktree and
branch:

```bash
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

If release reports an owner mismatch, do not remove another worker's lock; report
the mismatch in the final summary and, when under the CLI, in the worker report.

Do not require pre-merge re-review solely because `main` advanced. This is not
permission to merge unreviewed work, force-push, bypass tests, or bypass repo
policy.
