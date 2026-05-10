---
name: orchestrated-vibe-loop
description: Use when the user asks for an orchestrated vibe loop, main-agent-only orchestration, multi-agent coding work, or explicit delegation across exploration, implementation, remediation, and review agents. The main agent coordinates subagents and handoffs instead of doing direct implementation or independent review itself.
---

# Orchestrated Vibe Loop

Use for bounded or unattended software work where the main agent is an
orchestrator. The main agent owns task framing, agent assignment, handoffs,
integration gates, status, blockers, and final reporting. Exploration,
implementation, remediation, and review must be delegated to agents other than
the main orchestrator. Review must be independent from implementation, while
remediation should normally return to the responsible implementation agent.

This skill is stricter than `vibe-loop`: the main agent must not become the
worker. It may run lightweight read-only commands needed for coordination, such
as inspecting repo instructions, worktree status, changed files, or logs. It
may perform mechanical integration commands when policy permits, but it must not
author product code, silently fix implementation defects, or treat its own
inspection as the required independent review.

## Orchestration State

Maintain a compact ledger during the run:

- objective, acceptance criteria, constraints, and stop conditions;
- repo/workspace state and branch or worktree ownership;
- active agents, assigned scope, write ownership, semantic conflict domains,
  branch/workspace, and current status;
- decisions made from exploration results;
- verification evidence reported by implementers;
- review findings, remediation status, and re-review status;
- integration queue, base revision, merge status, and cleanup status;
- blockers, parked items, and user approvals needed;
- close/keep-open decision for every agent.

Update the ledger after exploration, implementation, review, remediation,
integration, blockers, and final summary.

## Agent Roles

Use explorer agents for read-only investigation. Assign concrete questions:
repo instructions, relevant files, architecture, tests, risks, task boundaries,
likely write scopes, and semantic conflict domains. Explorers must not edit
files. Prefer cheaper/weaker models for bounded fact gathering when available
and the risk is low.

Use implementation agents for code and test changes. Give each implementation
agent explicit ownership of files, modules, or one dedicated worktree. Tell them
they are not alone in the codebase, must not revert other work, must keep edits
inside their assigned scope, and must report changed paths, verification
commands, results, caveats, and blockers. Prefer stronger models for risky
implementation, architectural decisions, cross-module behavior, data/schema
changes, and conflict recovery. Workers must not spawn reviewer agents, invoke
AI review tools, or treat self-review as the independent gate. They may run
tests, linters, type checks, and local self-checks; review gates are launched
and judged only by the orchestrator.

Use review agents as independent gates. Prefer a spec review first, then a
code-quality review when the change is non-trivial. Provide the request,
acceptance criteria, repo constraints, changed files or diff, implementation
report, and verification evidence. Review agents should lead with findings,
severity, file and line references, missing tests, and residual risk.
Reviewers should consume the reported evidence and avoid rerunning checks unless
evidence is missing, stale, suspicious, high-risk, or needed to validate a
specific finding. Prefer stronger models for final reviews.

Use the same reviewer for re-review when practical. Re-review must receive the
original findings, remediation report, updated diff, and updated verification
evidence.

## Core Loop

1. Inspect the user request, repo instructions, existing worktree state, and
   obvious task source only enough to frame delegation. Do not start coding.
2. Spawn one or more explorer agents with distinct read-only questions. Use
   parallel explorers when the questions are independent.
3. Synthesize explorer outputs into a small execution plan: selected slice,
   acceptance criteria, implementation ownership, verification expectations,
   semantic conflict domains, review gates, integration order, and known risks.
4. Spawn a bounded number of implementation agents with disjoint write scopes
   and non-overlapping semantic conflict domains. If scope or semantics overlap,
   serialize the work or split it more narrowly; do not let multiple agents edit
   the same files or behavior without an explicit handoff.
5. Require implementers to run relevant tests/checks for their scope and report
   exact commands, outcomes, changed files, assumptions, and blockers.
6. Keep implementation agents available until their slice is merged, abandoned,
   or no longer needs remediation.
7. Spawn independent review agents after implementation evidence exists.
8. Send material findings back to the responsible implementation agent for
   remediation. Do not remediate findings in the main agent.
9. Re-review until no material findings remain, findings are explicitly parked
   with a reason, or a blocker requires user input.
10. Integrate reviewed work through the main orchestrator gate. Merge only
    reviewed work, verify it landed, update active branches/workspaces when the
    integration branch advances, then clean up merged branches/workspaces.
11. Finalize only after implementation evidence, review status, integration
    result, and cleanup status are known. Report completed work, agents used,
    verification evidence, review result, unresolved risks, and any blocker.

## Parallelism And Independence

Keep a bounded number of independent jobs active. Choose the bound from task
risk and repo size; prefer one implementation worker when conflict domains are
unclear.

Define independence by semantic overlap, not only by shared files or status
artifacts. Code paths, public APIs, data schemas, generated artifacts, subsystem
behavior, migration order, user workflows, and validation evidence can make two
tasks dependent even when they edit different files. Documentation, planning
tables, lock files, generated task caches, and release artifacts may create
ordinary write conflicts that can usually be resolved, but semantic overlap may
invalidate a slice and require restarting it from a newer base.

Track active branches/workspaces and semantic conflict domains before spawning a
worker. When the integration branch advances, update every active worker with
the new base context if it may affect their scope.

## Integration

The main orchestrator owns integration. It decides merge order, checks that work
has passed review, inspects branch/workspace state, performs or coordinates the
merge according to repo policy, verifies the landed result, updates the ledger,
and cleans up branches/workspaces.

Before merging a reviewed slice, record its base revision, inspect integration
branch commits added since that base, refresh the candidate against the current
integration branch, and rerun relevant verification. If refresh creates
conflicts or new base commits materially interact with the slice, delegate
behavioral conflict resolution to the implementer and run re-review after
remediation.

Delegate code-specific conflict resolution to the original implementation agent
when practical. The orchestrator may run mechanical merge/status/verification
commands and may resolve trivial non-code metadata conflicts when repo policy
allows, but must not make product-code decisions or silently repair behavior.

If semantic overlap appears during integration, stop that slice, update the
ledger, and either send it back to the original worker on the new base or park it
with the precise dependency. Do not merge unreviewed or stale work just because
text conflicts are absent.

Clean up branches/workspaces only after verifying the landed commit is contained
in the integration branch, the worktree is clean, no active agent still owns it,
and repo policy permits cleanup. If any precondition is missing, leave it in
place and report the reason.

## Prompt Requirements

Every delegated prompt should include:

- the user request and current slice objective;
- the broader flow: exploration feeds implementation, implementation evidence
  feeds orchestrator-managed review, findings return to the original worker,
  and the main orchestrator owns integration;
- applicable repo/user instructions and safety constraints;
- allowed and forbidden actions for the role;
- exact role instructions, including whether the agent is exploring,
  implementing, remediating, or reviewing;
- exact output required from the agent;
- file, module, branch, or worktree ownership where relevant;
- semantic conflict domains and known dependencies;
- how to handle blockers and unexpected external changes.

Implementation prompts must include: "You are not alone in the codebase; do not
revert edits made by others, and adapt your work to existing changes."
Implementation prompts must also say that workers must not spawn reviewer
agents, invoke AI review tools, or treat self-review as the independent review
gate. They must wait for orchestrator-managed review findings.

Each agent report must include role, prompt/scope understood, agent id when
available, branch/workspace, files read or changed, checks run with exact
commands and results, evidence paths or snippets, caveats, blockers, and whether
the agent should remain available.

Review prompts must avoid leaking the main agent's desired conclusion. Pass raw
artifacts, the diff, acceptance criteria, and verification evidence. Do not ask
for a rubber stamp.

## Coordination Rules

Do not duplicate work between agents. If two agents need related context, give
them the same raw artifacts and distinct questions.

Do not wait idly when independent coordination work remains, such as preparing
review prompts, summarizing explorer results, checking worktree state, or
assigning another independent explorer. When the main agent is genuinely blocked
on a running agent, wait with enough time for that agent to finish.

Keep implementation ownership disjoint. If a worker returns changes outside its
scope, either delegate correction to that worker or explicitly transfer
ownership before continuing.

Keep workers open while review, remediation, or integration may need them. Close
explorers, reviewers, and workers once their output is consumed and no further
handoff is expected.

Treat unexpected worktree changes as external. Do not revert them. Ask the
responsible implementation agent to adapt, or ask the user only when the
conflict cannot be resolved safely.

For destructive actions, credentials, production systems, or policy decisions,
pause the affected path and request the needed approval. Continue only with
independent safe work.

## Quality Bar

Do not claim completion from agent reports alone when the review gate is missing
or has unresolved material findings.

Do not collapse roles for speed. If no subagent or review mechanism is
available, report that the orchestrated contract cannot be satisfied instead of
performing the work directly under this skill.

Prefer smaller slices when orchestration overhead would otherwise hide defects.
The slice is too large if the main agent cannot clearly state ownership,
verification, and review status for each changed area.
