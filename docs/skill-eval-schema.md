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
- `orchestrated_vibe_loop`: multi-agent tasks where the main agent should only
  coordinate explorer, implementation/remediation, and review agents.
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

`run.json` is the durable index. Artifact files are referenced by relative path
plus SHA-256. `diff.patch` may be an
empty file for no-change trigger cases, but it is still recorded so aggregate
tools can distinguish "no diff" from "missing diff".

Required artifact roles for a completed trial record:

| Role | Purpose |
| --- | --- |
| `prompt` | Exact user prompt sent to the harness. |
| `run_log` | Harness stdout/stderr log or equivalent supervisor log. |
| `transcript` | Versioned safe event envelope used by workflow-contract graders. |
| `diff` | Final repository diff from the fixture base. |
| `final_repo_state` | HEAD, branch, worktree dirtiness, local branches/worktrees, and lock state. |
| `structured_result` | Machine-readable run outcome emitted by the harness or worker. |
| `grader_outputs` | Deterministic, trajectory, model, and human grader outputs. |

Optional structured roles use these closed envelopes:

| Role | Admitted fields |
| --- | --- |
| `workflow_events` | `events`, containing only labels from the workflow vocabulary below. |
| `git_state_before`, `git_state_after` | HEAD and branch identifiers, dirty boolean, branch/worktree/changed-path counts, and branch-to-HEAD digests. |
| `test_results` | Projected grader id/type/pass state, check id/pass states, and exit code. |
| `review_evidence` | Initial and re-review material-finding counts. |
| `lock_evidence` | Task/run ids, lock state, process ids/state, timestamps, and the normalized acquire/release/final-status evidence used by case graders. Commands, logs, host paths, and fencing tokens are excluded. |
| `workspace_evidence` | Per-task lifecycle/workspace statuses, booleans, diagnostic codes, counts, merged branch ids, and explicit dirty-file relative paths with size and SHA-256. |
| `report_evidence` | Worker-report schema/type, task/run ids, status, commit, timestamp, and normalized reason label. |
| `delegation_evidence` | Role and agent ids, prompt/result presence booleans, and changed-path count. |
| `generated_profile` | Schema/status, prompt version, confidence, profile kind, and bounded stable ids. |
| `budget_evidence` | Timeout/truncation booleans and bounded duration, command, and byte counts. |
| `negative_prompt_results` | Prompt id plus skill-activation, repository-change, and response-term-match booleans. |
| `command_results` | Command kind/id, exit/timeout/truncation/refusal booleans, bounded timing/byte/count/usage values, and timestamps. |
| `task_source_evidence` | Source kind, task ids/statuses, requirement ids, and title/acceptance presence booleans. |
| `hook_evidence` | Hook kind/index, exit/timeout/truncation values, normalized runtime status/actions, and event kind/task id. |

The workflow-event vocabulary is closed to:

- `branch_or_worktree_created`
- `commit_created`
- `destructive_workspace_cleanup`
- `exploration_delegated`
- `implementation_delegated`
- `implementation_edit_started`
- `instructions_inspected`
- `integration_lock_busy_observed`
- `main_advanced_detected`
- `main_fast_forwarded`
- `main_integration_lock_acquired`
- `main_integration_lock_released`
- `main_verification_ran`
- `remediation_delegated`
- `rereview_requested`
- `review_delegated`
- `review_finding_addressed`
- `review_finding_received`
- `review_requested`
- `skill_activated`
- `task_lock_acquired`
- `task_source_inspected`
- `unnecessary_user_prompt`
- `unsafe_git_command`
- `verification_ran`
- `worker_report_emitted`
- `workspace_preflight_blocked`
- `worktree_state_inspected`

Additional artifacts are allowed only when their role and closed structured
schema are defined here and they are referenced by relative path.
Artifact paths are validated as safe relative paths. Absolute paths,
parent-directory traversal, credential directories, `.env` files, private keys,
token-like names, and other secret-like paths are rejected before file reads.

## Structured Retention Envelope

Every JSON or JSONL artifact is a closed, size-bounded safe envelope. Unknown
fields and event shapes, malformed input, wrong types, non-finite numbers,
excessive nesting, excessive collection cardinality, and over-budget strings or
records fail closed. Rejection diagnostics contain only a fixed category and,
where useful, a field name or collection index. They never contain rejected
values, parser exception text, command output, prompts, or model responses.

`transcript.jsonl` uses `schema_version = 1` and
`record_type = "skill_eval_transcript_event"`. Each record has exactly one
`kind` from `assistant`, `command`, `process_result`, `result`, `system`,
`tool_call`, `tool_result`, or `usage`. A record may additionally contain only
bounded numeric `command_count`, `cost_usd`, `duration_ms`, `duration_seconds`,
`exit_code`, `input_tokens`, `output_tokens`, `tokens`, `stdout_bytes`, or
`stderr_bytes`, and boolean `error` or `timeout`. A transcript is limited to 10
MiB of input and 4,096 projected records. Tool names, command strings and
arguments, tool input and output, stdout/stderr text, prompts, assistant prose,
paths, exception or database messages, and arbitrary nested payloads are not
admitted.

`workflow-events.json` contains only `events`, a de-duplicated list of at most
128 schema-known workflow labels. Labels are derived from raw in-process events
before persistence. Local-demo execution requests each recognized first-party
agent's native structured stream: Codex `exec` commands receive `--json`, while
already structured Codex commands and their model, effort, environment, timeout,
output-budget, and provider-routing settings remain otherwise unchanged.
Workflow labels may be projected from schema-known Claude tool-use blocks,
Codex command-execution items, or Codex collaboration tool items. Final-answer
text, terminal rendering, filenames, arbitrary artifact text, and deterministic
repository-state facts are not delegation or review evidence. State projection
may establish only the corresponding worktree, verification, commit, or
mainline-movement facts. Unknown labels and malformed or unknown native
envelopes fail closed; rejected values are not copied into diagnostics. Command
counting likewise uses raw in-process events or safe `command`/`tool_call`
records and uses the maximum available observation, so sanitization cannot lower
a count.

The remaining structured artifacts retain only schema-known booleans, bounded
numbers, statuses and failure-taxonomy labels, stable identifiers or SHA-256
digests, safe artifact-relative paths, and bounded collections of those values.
In particular, command results have result kinds and numeric outcomes but no
command or path; negative-prompt summaries have prompt ids and boolean grading
outcomes but no prompt, path, or response; grader records have grader/check ids,
types, booleans, numeric usage/counts, admitted taxonomy, and workflow labels
but no command, stdout/stderr, message, payload, or nested output.
`run-result.json`, `run.json`, aggregates, and aggregate Markdown derive only
from those admitted values. The harness command is represented by
`harness.command_sha256`, not command text.

Before overwrite archival, every preexisting structured source below an active
artifact root is parsed and checked against the same bounds. Malformed, unsafe,
unknown, over-budget, or symlinked sources reject the archive before any history
destination is created. Current safe transcript records are re-projected into
the same envelope before copying. Known Claude and Codex raw harness stream
records from pre-envelope runs are projected through the same closed adapters;
their content-bearing fields are discarded. The legacy transcript shape
containing exactly `stream` (`stdout` or `stderr`) and `text` is also admitted
only for archival projection: its text is discarded and only the encoded byte
count is retained. Malformed records and unrecognized event types or fields
remain rejected. This applies equally to root trials, nested `prompt-runs/**`,
and structured copies under `history/**`.

History snapshots exclude materialized `repo/` and `repo-workspaces/` fixture
working trees. Those trees are execution inputs rather than artifact roles and
may contain arbitrary repository content; the run record retains their admitted
source fingerprints, diff, and final repository-state evidence instead. Branch
names outside the stable-identifier alphabet are retained only as
`sha256:<digest>` values. Symlinks anywhere else in an archived artifact root
remain rejected before the history destination is created.

`logs/run.log` is the deliberate exception. It is an intentionally
content-bearing audit artifact and may retain the harness command and captured
stdout/stderr under the existing safe artifact-path and SHA-256 contract,
including when archived. Consumers must not copy its text into any structured
artifact or CLI diagnostic.

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
  `vibe_loop` exposes `skill_id = "vibe-loop"`, `infinite_vibe_loop` exposes
  `skill_id = "infinite-vibe-loop"`, and `orchestrated_vibe_loop` exposes
  `skill_id = "orchestrated-vibe-loop"`.
- `agent`: `name` and `command_source`.
- `model`: `provider` and `id`.
- `harness`: `name`, `version`, and `command_sha256`.
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

## Release Evidence Records

`skill_release_readiness` schema version 2 adds `revision.base`,
`revision.head`, and `bundled_skills`, a map from packaged source path to
SHA-256. For an exact-revision record, `release_provenance` reports whether the
aggregate's recorded head and required trial skill fingerprints match those
fields. A dry-run record without these bindings remains useful diagnostically
but has blocked readiness and provenance status and cannot satisfy publishing
admission.

`skill_release_classification` records the ownership-contract version, canonical
base/head commits, all Git name-status entries (including both rename paths),
owned paths, uncertainty, and either `readiness_required` or
`unrelated_exemption`.

This section is authoritative for the persisted provenance field set.
`skill_release_readiness_provenance` schema version 1 has exactly these fields:
`schema_version`; `record_type`; `classification_head`; `readiness_sha256`, the
canonical JSON hash of the selected readiness record; `repository.full_name`
and canonical `repository.html_url`; numeric `workflow.id` and the fixed
repository `workflow.path`; numeric `run.id`, exact `run.head`, and successful
`run.conclusion`; numeric `artifact.id` and its exact-head immutable
`artifact.name`; and `evidence_reference`. The reference is the canonical
GitHub HTTPS artifact page derived from the repository, run id, and artifact
id. Extra fields are invalid so API/download/redirect URLs, signed values,
tokens, and local paths cannot enter the record.

`skill_release_admission` schema version 2 embeds and hashes that provenance in
`readiness_provenance` and `readiness_provenance_sha256`, hashes the
classification and readiness record, records each distribution's name, size,
and SHA-256, and has status `passed` only when the selected evidence path and
packaged skill bytes validate. Both provenance fields are null for an unrelated
exemption. Exact publishing behavior is owned by
[PRD-EVL-005](prd/evals-release.md#prd-evl-005-release-readiness-gate).
