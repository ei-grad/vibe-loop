---
name: infinite-vibe-loop
description: Use when the user asks for "infinite vibe loop", unattended persistent coding work, autonomous continuation through a backlog or workplan, or to keep working without stopping after reviewed slices.
---

# Infinite Vibe Loop

Use for open-ended, unattended software development sessions. It owns
continuation and integration discipline; use ordinary `vibe-loop` for bounded
work.

Each infinite-loop slice must satisfy the finite `vibe-loop` slice contract:
inspect, plan, edit, verify, review, remediate, commit, integrate when
permitted, clean up, report supervisor status when running under the CLI, and
summarize before selecting the next slice. The infinite-loop difference is
continuation after each completed, parked, or blocked slice.

When a slice is launched by the `vibe-loop` CLI, use `VIBE_LOOP_REPO`,
`VIBE_LOOP_RUN_ID`, `VIBE_LOOP_TASK_ID`, and `VIBE_LOOP_LOG` for structured
worker reports and advisory integration locking. If those variables or the CLI
are unavailable because the skill is being used directly, keep the same workflow
discipline and state that no structured supervisor report was sent.

## Mandatory Continuation

Assume an unattended session. Never voluntarily stop: continue after clean
slices, summaries, corrections, review, prioritization uncertainty, or blocked
individual items. Stop only on explicit user instruction or session end. Treat
corrections as new constraints.

When priorities are underspecified, choose a conservative next item from the
current objective, backlog, failing checks, review findings, obvious broken
behavior, or directly adjacent implementation work. Avoid unrelated refactors
and speculative product changes.

Keep a compact queue of candidate slices, dependencies, blockers, assumptions,
and the reason for the current next item. Update it after review, merge,
correction, blocker, and summary. Plan from repo-local sources first:
instructions, design docs, roadmaps, issues, TODOs, and existing plans.

## Worktree Discipline

Do not assume you are working alone. Other agents or the user may be working on
the same codebase on the same host. Inspect worktree state before each slice,
before review, and before merge; treat unexpected changes as external work and
do not revert them.

For each coherent work piece:

1. Start a separate branch/worktree from current `main`; do not implement slices
   directly on `main`.
2. Never leave uncommitted changes at rest. Commit before switching tasks,
   review, merge, blocker reports, or moving worktrees; use WIP/checkpoint
   commits when needed.
3. Verify and independently review the slice.
4. Before the final refresh, verification, fast-forward merge, and immediate
   `main` verification, acquire the advisory integration lock when running under
   the `vibe-loop` CLI:

   ```bash
   vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
     --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
   ```

5. Merge back to `main` with fast-forward-only integration and run immediate
   `main` verification.
6. Release the advisory integration lock after immediate `main` verification,
   or immediately when integration is parked:

   ```bash
   vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
     --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
   ```

7. Remove the merged worktree and delete the slice branch.

If the lock is held by another live worker, wait and retry or park the slice as
blocked; do not enter the final integration section without the lock while under
CLI supervision. If the lock appears stale, report the precise status and follow
repo policy rather than stealing it. If release reports an owner mismatch, do not
remove another worker's lock.

If `main` advanced after a slice passed review:

1. Inspect new `main` commits since the slice base; use them plus the slice diff
   to choose verification.
2. Merge current `main` into the slice worktree, resolve conflicts, and run that
   verification.
3. If clean, fast-forward `main` to the reviewed branch, for example:
   `git -C <worktree> merge main && git -C <main> merge --ff-only <branch>`.
4. Rerun relevant verification on `main`.
5. If conflicts were complex or behavior-affecting, or new `main` commits
   materially interact with the slice, run after-merge review on `main`; fix
   findings with a follow-up reviewed change.

Do not require pre-merge re-review solely because `main` advanced. Never use a
non-fast-forward merge for an infinite-loop slice.

## Worker Reports

Each CLI-launched slice should explicitly report its final status before moving
to the next slice:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \
  --task-id "$VIBE_LOOP_TASK_ID" --status completed --commit HEAD \
  --message "completed slice"
```

Use `completed` only after integration is permitted, final `main` verification
has passed, the integration lock has been released, and cleanup is done. Use
`blocked` for missing access, required approval, a persistently unavailable
integration lock, or a decision that cannot be made safely. Use `failed` when an
attempted slice cannot be left working despite reasonable debugging. Use
`unknown` only when the worker cannot classify the result. Include the best
available commit reference and a concise message; use `--metadata-json` only for
structured facts that help the supervisor or later review.

After reporting a blocked or failed slice, keep the infinite-loop behavior:
record the blocker in the compact queue, select another independent actionable
item if one exists, and continue.

## Working Loop

1. Inspect the objective, repo instructions, current worktree state, tests, docs,
   backlog, design docs, roadmap, proposals, and constraints.
2. Maintain a compact cross-slice continuation plan with current status, next
   candidates, dependencies, blockers, and assumptions.
3. Pick the next coherent slice and create a dedicated branch/worktree for it.
4. Implement the slice in scoped increments.
5. Run relevant tests/checks and verify evidence against the requested behavior.
6. Run independent spec review, then code-quality review. Use the user's
   preferred review tools when specified; otherwise prefer a subagent with clear
   context.
7. Address findings with code, tests, or docs; re-review, preferably with the
   same reviewer, until no material findings remain or remediation is tracked.
8. Acquire the main-integration lock when under CLI supervision, merge the slice
   back to `main` with fast-forward-only integration, verify `main`, release the
   lock, remove the merged slice worktree and branch, submit the worker report,
   record a concise status summary, select the next actionable item, and
   continue.

## Review

Spec review checks requested behavior and evidence. Code-quality review checks
implementation, tests, security, performance, UX, maintainability, and repo fit.
Review may take longer than coding; do not shorten or skip it for speed.

Reviews must use a separate reviewer. Use the user's preferred review tools when
specified; otherwise prefer a subagent with clear context.
The prompt must include the gate, request/criteria, changed files or diff,
verification results, evidence, and constraints/open questions. Follow repo
review policy first: `REVIEW.md`, `AGENTS.md`, `CLAUDE.md`, contribution guides,
CI docs, security checklists, and task-specific review instructions.

Expect review to take some time, 5-10-15+ minutes are normal.

## User Contact

If the user asks for a summary, provide current objective, completed slices,
verification performed, review status, blockers if any, and next action. Then
continue working.

Do not ask routine questions. When input would normally be useful, make a
reasonable conservative assumption, document it in the working summary, and keep
moving.

## Blockers

If an item needs missing access, credentials, destructive-action confirmation,
or a decision that cannot be made safely, park that item with a precise blocker
note and continue with another independent actionable item.

If every actionable path is blocked, leave a concise blocker report with the
specific missing approval, access, or decision, then keep looking for newly
available or independent work. Do not invent unsafe work.
