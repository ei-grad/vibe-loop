# Bundled Skill Eval Schema

This contract defines the first local artifact format for evaluating bundled
skills. It is intentionally smaller than a full benchmark harness: EVAL-01
defines records, artifacts, validation boundaries, and scoring semantics so
later tasks can build demo fixtures and a runner against a stable target.

## Evaluation Matrix

Each case is run as paired trials under the same fixture checkout, prompt,
agent harness, model, tool policy, timeout, and grader set. The skill condition
is the experimental variable.

Required early conditions:

- `no_skill`: bundled skills are unavailable or disabled.
- `vibe_loop`: only the bundled finite `vibe-loop` skill is available and should
  activate.

Optional conditions:

- `infinite_vibe_loop`: continuation or backlog tasks where the infinite skill
  should activate.
- `candidate_skill`: a proposed revision of a bundled skill.
- `self_generated_skill`: research-only comparison, reported separately from
  curated bundled skills.

Run order should be randomized or alternated when practical. Every trial starts
from a fresh fixture checkout and a fresh eval state directory. A trial must not
reuse `.vibe-loop/` state, lock files, transcripts, skill caches, generated task
profiles, or modified repository files from another condition unless the case
explicitly declares that seeded state.

## Artifact Layout

The default artifact bundle is stored outside the fixture repository:

```text
eval-runs/
  <suite-id>/
    manifest.json
    aggregate.json
    aggregate.md
    cases/
      <case-id>/
        <condition>/
          trial-<n>/
            run.json
            prompt.txt
            skill-fingerprint.json
            repo-fingerprint.json
            logs/
              run.log
            transcript.jsonl
            diff.patch
            final-repo-state.json
            run-result.json
            command-results.json
            grader-outputs.json
```

`run.json` is the durable index. Large logs and transcripts stay in separate
files and are referenced by relative path plus SHA-256. `diff.patch` may be an
empty file for no-change trigger cases, but it is still recorded so aggregate
tools can distinguish "no diff" from "missing diff".

Required artifact roles for a completed trial record:

| Role | Purpose |
| --- | --- |
| `prompt` | Exact user prompt sent to the harness. |
| `run_log` | Harness stdout/stderr log or equivalent supervisor log. |
| `transcript` | Tool and assistant trajectory used by workflow-contract graders. |
| `diff` | Final repository diff from the fixture base. |
| `final_repo_state` | HEAD, branch, worktree dirtiness, local branches/worktrees, and lock state. |
| `structured_result` | Machine-readable run outcome emitted by the harness or worker. |
| `grader_outputs` | Deterministic, trajectory, model, and human grader outputs. |

Additional artifacts are allowed when referenced by role and relative path.
Artifact paths are validated as safe relative paths. Absolute paths,
parent-directory traversal, credential directories, `.env` files, private keys,
token-like names, and other secret-like paths are rejected before file reads.

## Run Record

`run.json` uses `schema_version = 1` and `record_type = "skill_eval_run"`.
Required top-level fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `suite_id` | string | Eval suite identifier. |
| `case_id` | string | Stable case identifier inside the suite. |
| `trial` | integer | One-based trial number for this condition. |
| `condition` | string | One of the matrix conditions. |
| `run_id` | string | Globally unique trial id. |
| `task` | object | Task metadata: task id, prompt id/hash, expected skill, domain, and declared negative-trigger status. |
| `skill_condition` | object | Skill availability, skill id, source path, SHA-256, description hash, and trigger expectation. |
| `agent` | object | Agent CLI, command source, resolved command identity, and session id source when available. |
| `model` | object | Provider, model id, reasoning effort, temperature, and any harness-visible model settings. |
| `harness` | object | Harness name/version, command template identity, sandbox/network policy, and tool permission policy. |
| `budget` | object | Timeout, command count/output limits, token/cost budgets when available, and retry policy. |
| `source_fingerprints` | array | Fixture, prompt, skill, grader, and task-source fingerprints used to detect stale runs. |
| `artifacts` | array | Relative artifact references with role, SHA-256, required flag, and content type. |
| `final_repo_state` | object | Final branch, HEAD, dirtiness, worktree list, lock/report state, and merge state. |
| `structured_result` | object | Harness result: exit status, timeout flag, reported task status, commit, and summary evidence. |
| `graders` | array | Individual grader outputs and provenance. |
| `scoring` | object | Normalized pass/fail and score fields. |
| `reproducibility` | object | Fixture seed, run order, host-independent command identity, random seed, fresh-workspace flags, and rerun hints. |
| `status` | string | `passed`, `failed`, `timeout`, `infrastructure_error`, or `skipped`. |
| `started_at`, `finished_at` | string | Timestamp strings in UTC-compatible ISO-8601 form. |
| `failure_taxonomy` | array | Zero or more labels from the taxonomy below. |

Source fingerprints use safe relative paths, `sha256`, `size`, and optional
`mtime_ns`. Validation can compare them with current fixture fingerprints and
reports stale or missing sources before a run is reused as evidence.

The first schema version validates a minimum nested contract:

- `task`: `id`, `prompt_sha256`, `expected_skill`, and boolean
  `should_trigger`.
- `skill_condition`: `id`, boolean `skills_available`, and when skills are
  available, `skill_id` plus `skill_sha256`.
  `condition = "no_skill"` requires `skills_available = false` and no
  `skill_id`; bundled-skill conditions require `skills_available = true`.
  `vibe_loop` exposes `skill_id = "vibe-loop"`, and `infinite_vibe_loop`
  exposes `skill_id = "infinite-vibe-loop"`.
- `agent`: `name` and `command_source`.
- `model`: `provider` and `id`.
- `harness`: `name`, `version`, and `command`.
- `budget`: positive integer `timeout_seconds`, `max_commands`, and
  `max_output_bytes`.
- `final_repo_state`: `head`, `branch`, and boolean `dirty`.
- `structured_result`: integer `exit_code`, boolean `timeout`, `task_status`,
  and boolean `workflow_contract_completed`.
- `reproducibility`: `fixture_sha256`, positive integer `run_order`,
  `fresh_workspace = true`, and `state_reused = false`.

## Scoring Fields

`scoring` separates task outcome from workflow-contract behavior:

- `passed`: final boolean used for pass-rate calculations.
- `task_score`: deterministic repository outcome score from `0.0` to `1.0`.
- `workflow_score`: workflow-contract score from `0.0` to `1.0`.
- `trigger_score`: activation score for should-trigger and should-not-trigger
  cases.
- `normalized_gain_base`: the no-skill pass rate or score used for later
  normalized-gain reporting.
- `excluded_from_primary`: true only for infrastructure or grader failures that
  are excluded from primary pass-rate calculations and still reported.

Aggregate reports compute per-condition pass rate, per-task pass rate, absolute
uplift, normalized gain, timeout rate, infrastructure-error rate, workflow
violation rate, trigger false-positive/false-negative rate, latency, command
count, token usage and cost when available, plus confidence intervals once
repeated trials exist.

Aggregate JSON also includes `skill_quality`, a diagnostic report for bundled
skill behavior. It keeps task outcome and workflow-contract failures separate,
groups trigger misses, review/integration discipline failures, unsafe git
behavior, unnecessary prompts, overlong trajectories, infrastructure failures,
and cost regressions, and attaches every count or delta to the contributing
trial records by run id and artifact root. Per-task and per-domain uplift are
computed against the `no_skill` baseline when present. When an existing
`aggregate.json` is present before a run, the new report compares matching
condition metrics against that prior run and emits `prior_run_regressions` for
pass-rate, task-score, workflow-score, trigger-score, trajectory length, and
cost regressions while preserving token deltas for audit.

## Failure Taxonomy

Allowed labels:

- `task_outcome`: final repository state or deterministic tests failed.
- `workflow_contract`: required finite-slice behavior was missing.
- `trigger_false_negative`: relevant prompt did not activate the expected skill.
- `trigger_false_positive`: unrelated prompt activated a skill.
- `unsafe_git`: destructive or policy-forbidden git behavior.
- `secret_access`: secret-like file or environment access was attempted.
- `state_contamination`: previous trial state was reused or leaked.
- `review_missing`: independent review or required re-review was skipped.
- `integration_missing`: required branch/worktree/main integration evidence was
  missing.
- `unnecessary_user_prompt`: agent asked for input despite enough task evidence.
- `timeout`: trial exceeded budget.
- `harness_error`: harness or infrastructure failure outside agent behavior.
- `grader_error`: grader failed or produced invalid output.
- `flaky`: repeated trials disagree under the same condition.

Infrastructure and grader failures are reported separately from agent failures.
They can be excluded from primary task pass-rate calculations only when the
record explains the exclusion and keeps raw artifacts for audit.

## Safety And Reproducibility

Eval harnesses must not read environment variables or broad host state while
building source evidence. They should fingerprint explicit fixture files, skill
files, prompt files, grader code, and task-source documents. Secret-like paths
are rejected before reads using the same conservative path policy as generated
task discovery.

Each trial records:

- fixture repository seed or source archive hash;
- source fingerprints for prompt, fixture, skill, and grader inputs;
- model and harness identity;
- tool, sandbox, network, and permission policy;
- budgets and timeout behavior;
- run order and random seed if stochastic ordering is used;
- whether the workspace and eval state were fresh;
- final branch, HEAD, dirty state, worktrees, locks, reports, and local
  integration result.

No aggregate report may claim a bundled skill improvement unless every included
trial has matching source fingerprints, required artifacts, and non-contaminated
state evidence.
