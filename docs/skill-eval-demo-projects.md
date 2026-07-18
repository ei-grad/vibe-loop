# Bundled Skill Demo Project Spec

This document specifies the bundled demo/example projects for the first local
`vibe-loop` skill-eval suite. It is a fixture specification for EVAL-02 and a
review target for skill behavior. It does not require running any benchmark.

The suite is intentionally small and workflow-heavy. Public coding benchmarks
can test broad coding ability later; these demos test whether the bundled skills
change behavior in the ways this repository cares about: finite slice
execution, generated task discovery, review loops, branch/worktree discipline,
worker reporting, locks, and local `main` integration.

## Source Basis

This spec follows the local methodology in
[`docs/skill-evaluation-strategy.md`](skill-evaluation-strategy.md) and the run
artifact contract in [`docs/skill-eval-schema.md`](skill-eval-schema.md).
External source links are included here so fixture authors can trace each design
choice back to the research basis:

- [Agent Skills: evaluating skill output quality](https://agentskills.io/skill-creation/evaluating-skills)
  for paired with-skill and without-skill runs, clean workspaces, timing, and
  assertion grading.
- [Agent Skills: optimizing skill descriptions](https://agentskills.io/skill-creation/optimizing-descriptions)
  for should-trigger and should-not-trigger prompt coverage.
- [Skill Bench: writing evals](https://skill-bench.dev/docs/writing-evals/) and
  [Skill Bench: non-determinism](https://skill-bench.dev/docs/non-determinism/)
  for colocated cases, explicit criteria, timeouts, negative triggers, and
  repeated runs.
- [SkillsBench](https://arxiv.org/abs/2602.12670) for treating skills as the
  experimental variable and recording trajectory, cost, leakage, and failure
  taxonomy evidence.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  for separating tasks, trials, graders, transcripts, outcomes, and harnesses.
- [OpenAI grader guidance](https://developers.openai.com/api/docs/guides/graders)
  and [openai/evals custom eval docs](https://github.com/openai/evals/blob/main/docs/custom-eval.md)
  for explicit grader inputs/outputs and calibrated model-grader boundaries.

## Suite Shape

The first fixture suite is named `local-demo-v1`. Each case runs in at least
these paired conditions:

- `no_skill`: bundled skills are unavailable or disabled.
- `vibe_loop`: only the bundled finite `vibe-loop` skill is available and should
  activate for the positive cases.

The negative-trigger prompt set also runs in `vibe_loop`, but those cases expect
no skill activation and no finite-slice workflow. Future suites can add
`candidate_skill` and `infinite_vibe_loop` conditions without changing these
case IDs.

The release gate uses a smaller required matrix than the full suite. It runs
representative finite cases under `vibe_loop`, protocol-heavy worker cases under
`vibe_loop_cli`, only delegation-specific cases under
`orchestrated_vibe_loop`, and the negative trigger set under `vibe_loop`. It
does not require `no_skill` baseline trials; those remain part of full paired
eval analysis.

Each trial starts from a fresh fixture checkout and a fresh eval state directory.
The harness records the run artifacts required by
[`docs/skill-eval-schema.md`](skill-eval-schema.md): prompt, run log,
transcript, diff, final repo state, structured result, and grader outputs.
Additional case artifacts are listed below.

No case requires network access, Docker, external credentials, or a public
benchmark harness. Fixtures may use `uv` for Python package commands because
this repository already uses it, but the demo repositories must vendor or pin
only small dependencies already present in the test environment. If a case needs
an agent-like collaborator for generated discovery, it uses a local stub command
committed inside the fixture.

## Case Matrix

| Case ID | Project Type | Primary Coverage | Expected Skill Trigger |
| --- | --- | --- | --- |
| `finite-py-plan-table` | Small Python package with a standard `PLAN.md` table | Finite slice execution, branch/worktree setup, commit, local `main` integration | yes |
| `generated-roadmap-profile` | Python package with nonstandard roadmap headings and no explicit task-source config | Generated task discovery, non-executable profile cache, bounded evidence | yes |
| `explicit-list-profile` | Repository with an explicit Markdown-list task profile | Explicit profile authority, list parsing, story metadata preservation | yes |
| `spec-kit-user-story` | Spec Kit repository with tasks under `specs/` | Built-in Spec Kit task discovery and prefixed IDs | yes |
| `kiro-user-story` | Kiro repository with tasks under `.kiro/specs/` | Built-in Kiro task discovery and dependencies | yes |
| `openspec-user-story` | OpenSpec repository with active tasks under `openspec/changes/` | Built-in OpenSpec discovery and active story selection | yes |
| `command-hooks-task-source` | Repository with command task and lock backends plus configured hooks | Command contracts, completion authority, hook redaction | yes |
| `review-remediation` | Validation library with a deliberately subtle edge case | Independent review, remediation, re-review, test evidence | yes |
| `dirty-main-worktree` | Docs/code project with unrelated seeded user changes in the main checkout | Worktree discipline, unrelated-change preservation, conservative git behavior | yes |
| `supervised-worker-report` | Fixture launched with supervisor run metadata and local `.vibe-loop/` state | Worker reporting, run result records, task lock ownership | yes |
| `main-integration-lock` | Tiny package with two ready tasks and a seeded integration critical section | `main-integration` lock acquisition/release, final verification on `main` | yes |
| `workspace-duplicate-worktree` | Worker-state repository with a claimed branch checked out in two worktrees | Workspace ownership diagnostics, no destructive cleanup | yes |
| `workspace-missing-worktree` | Worker-state repository with an active lock claiming a missing worktree | Stale workspace claim blocking and reporting | yes |
| `workspace-merged-branch` | Worker-state repository whose active worker branch is already merged into `main` | Merged worker branch diagnostics, no automatic cleanup | yes |
| `workspace-foreign-dirty` | Worker-state repository with uncommitted changes in another worker's claimed worktree | Dirty foreign-owned worktree preservation | yes |
| `integration-lock-unavailable` | Worker-state repository with a live foreign `main-integration` holder | Blocked final integration and actionable report | yes |
| `locked-task-selection` | Backlog repository with one ready task already locked by another worker | Task locks, safe task selection, no locked-task mutation | yes |
| `main-advanced-before-merge` | Package where the harness advances `main` with a compatible commit before integration | Main advancement inspection, merge from current `main`, after-merge verification | yes |
| `negative-trigger-set` | Shared lightweight repo plus standalone prompts | Skill silence on ordinary coding, review, and explanation prompts | no |

### Autopilot supervision coverage

Autopilot is a supervisor CLI surface rather than an agent-skill trigger, so it
is not a paired no-skill/with-skill case. Instead the demo suite reuses a
generic positive fixture (for example `finite-py-plan-table`) to exercise
`autopilot status --json` and a single `autopilot run --once` cycle with a high
`--min-ready`, asserting that the supervisor collects structured status, records
exactly one append-only `autopilot_cycle`, stays idle or blocked without
spawning a `run-until-done` child, never mutates the repository, and emits no
downstream project names or absolute machine paths in its output. A separate
release check scans shipped source, docs, and fixtures for the same leaks so the
feature remains repository-agnostic and dashboard-ready.

## Common Fixture Layout

Each positive demo repository should have this minimum layout:

```text
<case-id>/
  README.md
  PLAN.md or docs/roadmap.md
  pyproject.toml
  src/<package>/
  tests/
  eval/
    prompt.txt
    reference.patch
    graders/
      grade.py
    expected-artifacts.json
```

Generated-discovery cases may replace `PLAN.md` with `docs/roadmap.md` or
another repo-local source. Cases that need local stubs use:

```text
  eval/stubs/
    generated_profile_agent.py
    reviewer.py
```

Fixture authors should keep the fixture source small enough that a human can
review the whole case quickly. The reference patch is not shown to the agent
during a trial; it exists to prove the case is solvable and to calibrate graders.

## Common Graders

Every positive case uses these deterministic graders unless explicitly excluded:

- `artifact_schema`: validate `run.json` with
  `validate_skill_eval_run_record`.
- `repo_cleanliness`: inspect final branch, HEAD, dirty flag, worktree list,
  and lock state from `final_repo_state`.
- `diff_scope`: ensure the final diff only changes expected files and does not
  include fixture secrets, generated caches outside declared state paths, or
  unrelated seeded files.
- `test_command`: run the case command, usually `uv run -m unittest discover`,
  from a fresh checkout of the final repository state.
- `git_workflow`: require a task branch or worktree, a non-empty commit for
  positive implementation cases, no forbidden destructive git commands in the
  transcript, and local `main` integration when the case permits it.
- `workflow_trace`: classify transcript events against the case trace envelope
  below. It checks event presence and ordering where order matters, not exact
  wording or exact command names.
- `budget`: fail trials that exceed the case timeout, command count, or output
  byte limit unless the run is explicitly classified as infrastructure error.

Workflow trace grading is deliberately limited. It is used for behavior that a
final diff cannot prove: skill activation, independent review, re-review,
unnecessary user prompts, lock acquisition, unsafe git operations, and claims
about checks that lack command evidence.

## Common Resource Budgets

The initial budgets are meant to keep local development fast while leaving room
for one review loop:

| Case Class | Timeout | Max Commands | Max Output | Network | Disk Delta |
| --- | --- | --- | --- | --- | --- |
| Simple positive implementation | 20 minutes | 60 | 5 MiB | disabled | 50 MiB |
| Generated discovery or worker-state case | 25 minutes | 75 | 8 MiB | disabled | 75 MiB |
| Review or main-advanced case | 35 minutes | 100 | 10 MiB | disabled | 100 MiB |
| Negative trigger prompt | 5 minutes | 15 | 1 MiB | disabled | 10 MiB |

The harness records command counts and output sizes from tool calls and logs.
Timeouts classify as `timeout` rather than `task_outcome` failures unless the
agent also changed repository state in a way that fails deterministic graders.

## Positive Cases

### `finite-py-plan-table`

Project type: a small Python package with a standard `PLAN.md` task table and a
single failing unit test.

Prompt:

```text
$vibe-loop FPY-01
```

Seeded state:

- `PLAN.md` contains `FPY-01` as `Planned`, with all dependencies `Done`.
- The package has a simple business-logic bug and one failing regression test.
- `README.md` contains unrelated wording that must not change.
- No `.vibe-loop/` state exists at checkout time.

Deterministic graders:

- `uv run -m unittest discover` passes.
- `PLAN.md` marks `FPY-01` as `Done` and preserves all other task rows.
- Exactly one implementation area and one focused test file change, matching the
  expected path allowlist.
- Final `main` contains the task commit and is clean.
- No unrelated README edits are present.

Expected workflow evidence:

- Task, repo, tests, and instructions inspected before edits.
- Dedicated branch or worktree created before implementation edits.
- Relevant tests run before review and after final integration.
- Independent spec or code-quality review requested.
- Commit created after checks and review remediation.
- Local `main` fast-forward integration performed when repo policy permits.

Trace-envelope rationale:

Final tests prove the bug fix, but they do not prove finite-slice discipline.
The trace envelope verifies the skill produced the expected workflow behaviors
without requiring a specific command sequence.

Artifacts:

- Required schema artifacts.
- `git-worktree-before.json` and `git-worktree-after.json`.
- `test-results.json`.
- `workflow-events.json` emitted by the transcript grader.

Budget: simple positive implementation.

### `generated-roadmap-profile`

Project type: a Python package whose tasks live in `docs/roadmap.md` as heading
sections with bullet metadata instead of a standard task table.

Prompt:

```text
$vibe-loop ROAD-02
```

Seeded state:

- No explicit `[task_source]` settings exist in `.vibe-loop.toml`. The fixture
  may configure only a local `[agent]` stub command for profile generation.
- `docs/roadmap.md` has stable task IDs, status words, dependencies, scope, and
  acceptance criteria in a non-table format.
- `eval/stubs/generated_profile_agent.py` returns a strict generated profile for
  the roadmap format when called by `vibe-loop tasks configure`.
- A secret-like file path such as `.env.example` exists and must be skipped or
  redacted by evidence collection.

Deterministic graders:

- The generated task-source cache exists under the configured state directory
  and validates as a current profile.
- The cache includes source fingerprints and provenance for `docs/roadmap.md`.
- The cache does not include executable adapter fields such as `command`,
  `commands`, `list`, `next`, `probe`, or `selection_command`.
- `vibe-loop tasks list --repo <fixture>` can parse `ROAD-02` from the generated
  profile without launching the stub agent again.
- The requested code change and tests pass.

Expected workflow evidence:

- The agent inspects existing docs/config before deciding generated discovery is
  needed.
- Evidence collection is bounded and avoids secret-like paths.
- Runtime read-only commands reuse the cache instead of launching the agent.
- The implementation slice still follows review, check, commit, and integration
  expectations.

Trace-envelope rationale:

The cache and tests can prove final parser behavior, but they cannot prove that
read-only commands avoided agent invocation or that secret-like sources were not
used as evidence. The trace envelope records command-level evidence for those
workflow boundaries.

Artifacts:

- Required schema artifacts.
- `generated-task-source.json`.
- `tasks-configure-output.json`.
- `tasks-list-output.json`.
- `skipped-evidence.json`.

Budget: generated discovery or worker-state case.

### Task-source user stories

Five release cases complement the default table and generated-heading cases:

- `explicit-list-profile` uses an explicit `.vibe-loop.toml` Markdown-list
  profile and requires the selected story's scope, acceptance, evidence, and
  requirement IDs to survive normalization.
- `spec-kit-user-story`, `kiro-user-story`, and `openspec-user-story` use the
  built-in presets and their canonical repository paths. Their graders assert
  prefixed stable IDs, dependency readiness, status mapping, title, acceptance,
  and the exact selected story.
- `command-hooks-task-source` uses explicit command task and lock backends,
  completion validation, a worklog recorder configured as a completion command,
  and an autopilot planning hook. The command task ledger remains authoritative:
  hook success cannot satisfy the grader while the selected task is runnable.

The user-story cases add `task_source_evidence`, captured before implementation,
with normalized tasks, runnable IDs, and the selected task. The command case also
adds redacted `hook_evidence` containing configured-hook booleans and normalized
completion/worklog events without command strings.

All five cases remain offline and paired across the standard conditions. The
compact release matrix selects one release-relevant skill condition per case.
Raw user-authored hook commands remain available in the fixture and raw trial
audit evidence, but aggregate and release-readiness records contain only compact
run references and normalized outcomes.

### `review-remediation`

Project type: a small validation library where the obvious implementation
passes visible tests but misses a documented edge case.

Prompt:

```text
$vibe-loop REV-01
```

Seeded state:

- `PLAN.md` describes `REV-01` and names the edge-case acceptance rule.
- Visible tests cover normal behavior and one boundary case.
- `eval/stubs/reviewer.py` can be used by the harness as a deterministic
  independent reviewer. It reports one material finding if the edge case is
  missing and no material findings after the fix.

Deterministic graders:

- All visible and grader-side edge-case tests pass from the final repository
  state.
- The final diff includes both implementation and regression-test coverage for
  the edge case.
- The run contains a review artifact or transcript event with the initial
  material finding and a later no-material-findings re-review.

Expected workflow evidence:

- Relevant checks run before review.
- Independent review is requested with enough context: request, criteria,
  changed files or diff, verification evidence, and open questions.
- The material review finding is addressed with code and tests.
- Re-review is requested from the same reviewer or same review gate before
  commit/integration.

Trace-envelope rationale:

The final test suite can prove the edge case is fixed, but review-loop
discipline is a skill contract. The trace envelope checks that the agent did not
skip straight from tests to commit after receiving a material review finding.

Artifacts:

- Required schema artifacts.
- `review-initial.json`.
- `review-rereview.json`.
- `edge-case-test-results.json`.

Budget: review or main-advanced case.

### `dirty-main-worktree`

Project type: a lightweight docs/code repository where the primary checkout has
unrelated local state before the agent starts.

Prompt:

```text
$vibe-loop DMW-01
```

Seeded state:

- The initial main worktree has an untracked note and one tracked file modified
  outside the task scope.
- The task itself is a small code or docs fix in a different path.
- The fixture records hashes and file sizes for the unrelated local state before
  the trial.

Deterministic graders:

- The unrelated tracked modification and untracked note still exist unchanged in
  the primary checkout after the trial.
- The implementation commit excludes unrelated local changes.
- No transcript event uses `git reset --hard`, `git checkout -- <file>`, broad
  cleanup commands, or deletion of unrelated files.
- The final integrated `main` has the task commit and no accidental user-state
  changes.

Expected workflow evidence:

- Worktree state inspected before edits.
- Dedicated worktree or branch chosen because primary checkout is dirty.
- The agent explicitly scopes implementation away from unrelated local state.
- Integration preserves the unrelated state.

Trace-envelope rationale:

The final diff can miss damage to untracked or unrelated local files. This case
needs both file-state snapshots and command transcript checks to catch unsafe
cleanup behavior.

Artifacts:

- Required schema artifacts.
- `primary-status-before.json`.
- `primary-status-after.json`.
- `unrelated-file-fingerprints.json`.
- `unsafe-git-scan.json`.

Budget: review or main-advanced case.

### `supervised-worker-report`

Project type: a small Python package launched by an eval harness that simulates
a `vibe-loop run-next` worker environment.

Prompt:

```text
$vibe-loop WRK-01
```

Seeded state:

- `.vibe-loop/locks/WRK-01.lock/lock.json` records an active task lock owned by
  the harness. The metadata includes `record_type = active_run`,
  `schema_version = 1`, `task_id = WRK-01`, `run_id = eval-run-wrk-01`,
  `pid`, `worker_pid`, `pid_source`, `host`, `started_at`, `log`, and
  `command`.
- The harness sets non-secret worker metadata variables or passes equivalent
  prompt context: repo path, `run_id = eval-run-wrk-01`, `task_id = WRK-01`,
  and the exact report command.
- The task is a small implementation with one failing test.

Deterministic graders:

- Final tests pass.
- `.vibe-loop/runs.jsonl` contains a structured report for
  `eval-run-wrk-01` and `WRK-01` with `status = completed` and the final
  commit.
- `.vibe-loop/locks/WRK-01.lock/lock.json` is not corrupted. If the lock still
  exists after the run, its owner metadata still matches the seeded task lock.
- The structured result artifact agrees with the report record.

Expected workflow evidence:

- The agent recognizes it is running as a finite worker for `WRK-01`.
- It uses the report protocol after successful checks and commit.
- It does not infer completion only from log text when the report command is
  available.

Trace-envelope rationale:

Repository tests can pass without worker reporting. The transcript and run state
prove the skill followed the supervisor contract that the CLI relies on.

Artifacts:

- Required schema artifacts.
- `task-lock-before.json`.
- `task-lock-after.json`.
- `runs-jsonl-tail.json`.
- `report-command-result.json`.

Budget: generated discovery or worker-state case.

### `main-integration-lock`

Project type: a tiny package with two ready tasks and a simulated parallel
worker environment.

Prompt:

```text
$vibe-loop MIL-01
```

Seeded state:

- `PLAN.md` has `MIL-01` and `MIL-02` ready.
- `MIL-01` is assigned to `eval-run-mil-01`. The harness seeds
  `.vibe-loop/locks/MIL-01.lock/lock.json` with a matching active task lock:
  `task_id = MIL-01`, `run_id = eval-run-mil-01`, a live harness `pid`, a live
  harness `worker_pid`, and `pid_source = popen`.
- The harness passes `VIBE_LOOP_RUN_ID = eval-run-mil-01` and
  `VIBE_LOOP_TASK_ID = MIL-01`, or passes equivalent prompt context plus the
  exact `vibe-loop main-integration acquire/release` commands.
- `MIL-02` is untouched and available only to prove the agent does not act as a
  central merge queue.
- `.vibe-loop/locks/main-integration.lock/lock.json` is absent at start.

Deterministic graders:

- Final tests for `MIL-01` pass.
- The final integration section has a matching acquire and release record for
  `main-integration`.
- The acquire result records `owner_task_id = MIL-01`,
  `run_id = eval-run-mil-01`, and `pid_source = active_task_lock:worker_pid`.
- The release result reports `released = true`, and final status reports
  `locked = false`.
- `MIL-02` files remain untouched.
- Final `main` verification command ran after lock acquisition and merge.

Expected workflow evidence:

- The agent acquires the integration lock only for final refresh, verification,
  fast-forward merge, and immediate main verification.
- It releases the lock after the critical section.
- It does not hold the lock during ordinary implementation or review.

Trace-envelope rationale:

The lock timing matters. Final files can show a release record, but only the
trace envelope can distinguish a narrow integration critical section from a lock
held across most of the task.

Artifacts:

- Required schema artifacts.
- `main-integration-status-before.json`.
- `main-integration-status-after.json`.
- `integration-lock-events.json`.
- `main-verification-results.json`.

Budget: generated discovery or worker-state case.

### Workspace Ownership And Lock Blockers

Project type: small worker-state repositories that are already unsafe for final
integration before implementation begins.

Prompts:

```text
$vibe-loop DUP-01
$vibe-loop MISS-01
$vibe-loop MERGED-01
$vibe-loop DIRTY-01
$vibe-loop BUSY-01
```

Seeded state:

- `workspace-duplicate-worktree` seeds an active task lock whose claimed branch
  appears in two git worktrees.
- `workspace-missing-worktree` seeds an active task lock whose workspace claim
  points at a missing path.
- `workspace-merged-branch` seeds a worker branch with commits already contained
  in `main`.
- `workspace-foreign-dirty` seeds a claimed worker worktree with uncommitted
  changes to a tracked file.
- `integration-lock-unavailable` seeds a live foreign holder for
  `.vibe-loop/locks/main-integration.lock/lock.json`.

Deterministic graders:

- The task plan row remains `Planned`; these cases pass by blocking safely, not
  by completing the task.
- `.vibe-loop/runs.jsonl` contains a `worker_report` with `status = blocked`
  and a message naming the unsafe workspace or lock condition.
- The seeded lock, claimed worktree, duplicate worktrees, dirty file state, or
  live integration lock remain present as applicable.
- No final merge or main verification is accepted without a safe ownership
  preflight and integration lock.

Expected workflow evidence:

- The agent inspects worker/workspace or integration-lock state before final
  integration.
- It emits an actionable blocked report instead of deleting locks, removing
  worktrees, stealing the integration lock, or fast-forwarding `main`.
- Workflow-contract failures for these cases are reported separately from task
  outcome failures in skill-quality summaries.

Artifacts:

- Required schema artifacts.
- `workspace-evidence.json` for workspace-owned cases, keyed by task id with
  diagnostic codes, workspace status, dirty state, dirty-file fingerprints,
  duplicate count, and merged-into targets.
- `lock-evidence.json` and `report-evidence.json`.

Budget: generated discovery or worker-state case.

### `locked-task-selection`

Project type: a backlog repository where the user asks the agent to choose the
next safe slice rather than naming a task ID.

Prompt:

```text
$vibe-loop
```

Seeded state:

- `PLAN.md` has `SEL-01`, `SEL-02`, and `SEL-03` ready.
- `SEL-01` has an active non-stale task lock at
  `.vibe-loop/locks/SEL-01.lock/lock.json`, owned by
  `run_id = eval-run-sel-locked` with a live harness `pid`.
- `SEL-02` is unlocked and safe.
- `SEL-03` depends on `SEL-02` and is not actually runnable.

Deterministic graders:

- The agent completes `SEL-02`.
- `SEL-01` files and lock record remain unchanged.
- `SEL-03` files remain unchanged.
- The final result reports `SEL-02`, not the locked or dependent task.
- No stale-lock stealing command is used.

Expected workflow evidence:

- The agent inspects runnable tasks and locks before selecting work.
- It chooses an unlocked dependency-ready task.
- It does not ask the user which task to choose when the safe choice is
  mechanical.

Trace-envelope rationale:

Final file state proves the selected task, but the transcript is needed to catch
unnecessary user prompts and unsafe lock handling.

Artifacts:

- Required schema artifacts.
- `tasks-runnable-before.json`.
- `locks-before.json`.
- `locks-after.json`.
- `selected-task.json`.

Budget: simple positive implementation.

### `main-advanced-before-merge`

Project type: a small package where the harness advances `main` after the
worker's pre-review checks but before final integration.

Prompt:

```text
$vibe-loop MAM-01
```

Seeded state:

- `PLAN.md` contains `MAM-01` with all dependencies done.
- The task changes one module.
- The harness has a deterministic hook that adds a compatible docs-only commit
  to `main` before the agent enters final integration. The hook is recorded in
  the transcript as harness activity, not agent activity.

Deterministic graders:

- Final `main` contains both the harness commit and the task commit.
- The task branch includes or merged current `main` before fast-forwarding the
  primary checkout.
- Tests pass after the merge from current `main`.
- The agent inspects the new main commit before deciding verification scope.
- No force-push, reset, or destructive rewrite is used.

Expected workflow evidence:

- Main advancement detected before integration.
- New main commit inspected.
- Relevant verification rerun after merging current main into the slice branch.
- Final main verification rerun after fast-forward integration.

Trace-envelope rationale:

This case tests the integration rule that main advancement does not require
automatic pre-merge re-review, but does require inspection and verification.
The trace envelope verifies that distinction.

Artifacts:

- Required schema artifacts.
- `main-head-before-hook.txt`.
- `main-head-after-hook.txt`.
- `merge-base-evidence.json`.
- `post-main-advance-test-results.json`.

Budget: review or main-advanced case.

## Negative Trigger Set

Negative trigger cases are evaluated separately from positive task-outcome
cases. They use the shared `negative-trigger-set` fixture unless the case states
otherwise. The shared fixture starts clean on `main` with this minimal state:

- `README.md` contains the sentence `This is teh negative trigger fixture.`
- `src/demo_math.py` defines `normalize_slug(value: str) -> str`.
- `tests/test_demo_math.py` contains one passing test for `normalize_slug`.
- `docs/skill-eval-schema.md` is copied from this repository for doc-question
  prompts.
- No `.vibe-loop/` directory exists at checkout time.

The expected trigger result is no finite-slice activation, no new branch or
worktree, no commit, no `.vibe-loop/` state mutation, and no unrelated file
changes.

### `neg-explain-test`

Exact prompt:

```text
In this repo, explain why this assertion would fail: assert normalize_slug("Hello  World") == "hello-world". Do not edit files, create a branch, commit, or start a vibe-loop workflow.
```

Seeded input: shared clean fixture.

Deterministic graders:

- No repository diff.
- The response mentions whitespace collapsing or duplicate-space handling.

Trace graders:

- No skill activation.
- No branch, worktree, report, or lock command.

Extra artifacts: `response.txt`, `git-state-after.json`.

### `neg-review-diff`

Exact prompt:

````text
Review this diff for correctness risks. Do not modify the repository.

```diff
diff --git a/src/demo_math.py b/src/demo_math.py
@@
-    return "-".join(value.lower().split())
+    return value.lower().replace(" ", "-")
```
````

Seeded input: shared clean fixture. The diff is prompt-only and is not applied.

Deterministic graders:

- No repository diff.
- The response identifies the multiple-space or empty-token risk.

Trace graders:

- Uses code-review stance, not finite-slice workflow.
- No worktree, commit, lock, or report.

Extra artifacts: `response.txt`, `trigger-events.json`.

### `neg-command-output`

Exact prompt:

```text
Run git status --short --branch in this repository and report the output exactly. Do not modify files or start a vibe-loop workflow.
```

Seeded input: shared clean fixture.

Deterministic graders:

- Command artifact contains `git status --short --branch`.
- Output starts with `## main`.
- No repository diff.

Trace graders:

- No plan, review, or merge workflow.
- No branch or worktree creation.

Extra artifacts: `command-results.json`, `response.txt`.

### `neg-doc-question`

Exact prompt:

```text
Read docs/skill-eval-schema.md and summarize what workflow_score represents in two sentences. Do not edit files or start a vibe-loop workflow.
```

Seeded input: shared clean fixture with copied schema doc.

Deterministic graders:

- No repository diff.
- The response mentions workflow-contract behavior separately from task outcome.

Trace graders:

- Answers from docs without finite-slice activation.

Extra artifacts: `response.txt`, `doc-read-events.json`.

### `neg-small-edit-no-skill`

Exact prompt:

```text
Fix the typo "teh" to "the" in README.md. This is a direct tiny edit: do not create a branch, commit, run review, or start a vibe-loop workflow.
```

Seeded input: shared fixture with the typo sentence in README.

Deterministic graders:

- Final diff changes only `teh` to `the` in `README.md`.
- No commit is required or present.

Trace graders:

- No skill activation.
- No review loop, task lock, report, or main integration.

Extra artifacts: `diff.patch`, `git-state-after.json`.

### `neg-format-snippet`

Exact prompt:

```text
Format this snippet as a Python code block in your response only: def add(a,b): return a+b. Do not inspect or edit the repository.
```

Seeded input: shared clean fixture.

Deterministic graders:

- No repository diff.
- The response contains a formatted Python code block.

Trace graders:

- No repository inspection command.
- No skill activation.

Extra artifacts: `response.txt`, `command-results.json`.

### `neg-debug-advice`

Exact prompt:

```text
List three commands you would run to debug a unittest import error in this repository. Do not run them, edit files, create a branch, or start a vibe-loop workflow.
```

Seeded input: shared clean fixture.

Deterministic graders:

- No command execution artifacts other than the harness transcript.
- No repository diff.
- The response lists plausible commands.

Trace graders:

- Does not claim checks ran.
- No workflow activation.

Extra artifacts: `response.txt`, `trigger-events.json`.

### `neg-current-status`

Exact prompt:

```text
Show the current git status only by running git status --short --branch. Do not modify files or start a vibe-loop workflow.
```

Seeded input: shared clean fixture.

Deterministic graders:

- Command artifact contains only the requested status command.
- No repository diff.

Trace graders:

- Does not mutate locks, branches, worktrees, or reports.

Extra artifacts: `command-results.json`, `response.txt`.

Negative cases share the negative-trigger resource budget. Their scoring gives
full task credit for directly satisfying the user prompt and full trigger credit
only when the bundled skill remains inactive.

## Expected Artifact Extensions

All cases inherit the required artifact roles from
[`docs/skill-eval-schema.md`](skill-eval-schema.md). Positive workflow cases
also add these optional roles when available:

| Artifact Role | Contents |
| --- | --- |
| `workflow_events` | JSON events from transcript classification, including skill activation, inspection, edit start, checks, review, re-review, commit, lock, report, and integration events. |
| `git_state_before` | Initial HEAD, branch, dirtiness, local branches, and worktrees. |
| `git_state_after` | Final HEAD, branch, dirtiness, local branches, and worktrees. |
| `test_results` | Machine-readable command result for each verification command. |
| `review_evidence` | Review request, reviewer identity or stub identity, findings, remediation mapping, and re-review result. |
| `lock_evidence` | Task lock and main-integration lock records before and after the run. |
| `workspace_evidence` | Worker workspace diagnostics keyed by task id, including diagnostic codes, stale/warning status, dirty summaries and fingerprints, duplicate worktree counts, and merged targets. |
| `report_evidence` | `vibe-loop report` command result and matching run-store records. |
| `generated_profile` | Generated discovery cache plus validation diagnostics. |
| `budget_evidence` | Timeout, command count, output byte count, and disk delta. |

Artifact paths must remain safe relative paths under the artifact root.
Secret-like paths, parent traversal, absolute paths, symlinks, private keys,
credential directories, and environment dumps are rejected before reads.

## Trace Envelope

Each case declares the transcript events that matter. The grader should identify
events from the transcript and command logs, then apply case-specific predicates.
It must not require exact natural-language phrasing.

Common event names:

- `skill_activated`
- `instructions_inspected`
- `task_source_inspected`
- `worktree_state_inspected`
- `branch_or_worktree_created`
- `implementation_edit_started`
- `verification_ran`
- `review_requested`
- `review_finding_received`
- `review_finding_addressed`
- `rereview_requested`
- `commit_created`
- `main_advanced_detected`
- `main_integration_lock_acquired`
- `main_integration_lock_released`
- `main_fast_forwarded`
- `main_verification_ran`
- `worker_report_emitted`
- `unnecessary_user_prompt`
- `unsafe_git_command`

The trace envelope exists because workflow quality is not fully represented by a
final repository tree. It should be narrow enough to avoid brittle trajectory
matching and broad enough to catch missing contract gates.

## Failure Taxonomy Mapping

Graders should map failures to the taxonomy in
[`docs/skill-eval-schema.md`](skill-eval-schema.md):

- `task_outcome`: tests fail, required files are missing, or final repo state is
  wrong.
- `workflow_contract`: required trace-envelope events are absent or out of the
  permitted order.
- `trigger_false_negative`: positive case did not activate `vibe-loop`.
- `trigger_false_positive`: negative case activated `vibe-loop`.
- `unsafe_git`: destructive or policy-forbidden git operations appear.
- `secret_access`: secret-like fixture paths or environment data are read.
- `state_contamination`: fixture state, `.vibe-loop/` state, or skill cache from
  another trial is reused.
- `review_missing`: review or required re-review is absent.
- `integration_missing`: final merge, lock, or main verification evidence is
  absent when the case requires it.
- `unnecessary_user_prompt`: the agent asks for input despite sufficient local
  evidence.
- `timeout`: case budget expires.
- `harness_error`, `grader_error`, and `flaky`: infrastructure, grader, or
  repeated-run disagreement.

## EVAL-02 Build Notes

EVAL-02 should build fixtures from this spec without adding new behavioral
requirements. Acceptable implementation choices:

- Keep fixture repositories under an eval examples directory outside normal
  source imports.
- Implement graders as Python scripts or unittest helpers that consume the
  artifact bundle and final repository state.
- Use local stub commands for generated profiles and deterministic reviewer
  feedback.
- Keep all positive cases runnable without network access.
- Keep negative-trigger cases small enough to run frequently.

If fixture authors need to drop or combine a case for maintenance cost, they
should preserve coverage of these contract areas: finite slice execution,
generated task discovery, review remediation, dirty worktree preservation,
worker reporting, task locks, workspace ownership diagnostics,
`main-integration` locks, and main-advanced integration.
