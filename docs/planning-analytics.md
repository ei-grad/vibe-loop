# Planning Analytics Contract

> **Superseded (removed from vibe-loop).** The in-tree planning-analytics
> commands (`vibe-loop planning timeline/artifacts/benchmark-duration`),
> `[planning_analytics]` config, and the timeline/Gantt artifact generators were
> removed. Timeline and Gantt reporting now live in the
> [loopyard](https://github.com/ei-grad/loopyard) web UI over the read-only
> `autopilot status --json` boundary. The contract below is retained for
> historical reference only.

Planning analytics is a reporting boundary above task discovery and run
recording. It must not become a scheduler, task selector, or completion source
for the finite worker loop. The first supported outputs are generated planning
artifacts: evidence collection data, timeline JSON, optional static Gantt HTML,
and duration benchmark reports.

## Normalized Input

Analytics starts from the same normalized `Task` objects used by runtime task
selection. A task record supplies the stable id, title, section, status,
priority, dependencies, source provenance, order, optional resource/path domains,
and human-readable scope, acceptance, and evidence text. Generated task-source
profiles and explicit task-source config are already resolved before analytics
runs, so analytics does not parse repository planning files independently.

The analytics collector may add derived fields, but those fields are scoped to
analytics output and must not feed back into `next`, `tasks runnable`, locks, or
worker selection.

## Evidence Tiers

Authoritative evidence is allowed to satisfy coverage checks and map completed
work to task ids:

- normalized task-source completion state, especially `Done`;
- structured worker reports with explicit task ids and commit refs;
- structured worker-report metadata that declares `plan_items`,
  `requirement_ids`, `reviews`, or `tests`;
- optional project worklog adapter records that include task ids or explicit
  commit mappings;
- explicit commit references in task evidence fields;
- `Plan-Item:` and `Requirement:` commit trailers.

Diagnostic evidence can explain likely relationships, but must not satisfy
coverage by default:

- subject-line or branch-name matching;
- raw run log text;
- unstructured final messages;
- commits that only happen to touch files near a task source.

If a future repository wants heuristic matching to become authoritative, that
must be an explicit config choice and the generated output must mark the mapping
source. The default `subject_matching = "diagnostic"` keeps heuristics out of
coverage success.

## Run Attempts And Final Ledgers

`.vibe-loop/runs.jsonl` records attempts. Attempts are useful for stale state,
failure analysis, elapsed time, and linking worker reports to logs. They are not
a final project completion ledger because they include failed, blocked, and
unknown results.

Run-start and agent-context records may expose trailer-ready context such as
task IDs, run IDs, `started_at`, session IDs, agent kind, prompt dialect, and
model fields observed from startup output or safely inferred from runtime facts.
Analytics treats that context as diagnostic candidate evidence until project
history persists it through a worker report, worklog, task-source state, or git
trailer.

Final evidence belongs in project worklogs, task-source status, explicit commit
mappings, or commit trailers. Analytics joins attempts and final evidence, but
warnings must distinguish "the worker tried this" from "the project recorded
this as completed evidence."

## Optional Worklog Adapters

`planning_analytics.worklog_command` is reserved for a future command adapter
that emits bounded JSON or JSONL project evidence. The command is optional. When
it is absent, analytics uses normalized tasks, run records, explicit commit
mappings, and bounded git metadata. `doctor` reports only whether the adapter is
configured; it does not print the command string.

Adapter output should be treated as repo-local evidence, not executable config
for task selection. It must include enough provenance to explain which task ids,
commit hashes, and source records produced each evidence item.

## Git Metadata Boundary

Planning analytics may read bounded git metadata needed to map work:

- commit hash, subject, author name/email, author time, committer time, parent
  count, and trailers;
- changed repo-relative file paths when needed for diagnostics;
- merge base and branch/head names when explicitly requested for diagnostics.

It must not read environment variables, credential directories, secret-like
paths, or full diff contents by default. Generated outputs should avoid storing
raw command strings or sensitive path fragments. Secret-like evidence skips must
be represented as skipped reasons so users can tell the difference between
missing evidence and intentionally bounded evidence.

## Artifact Locations

Generated analytics artifacts default under the configured state directory:

```text
<state_dir>/planning-analytics/
  timeline.json
  gantt.html
  duration-benchmark.json
  duration-benchmark.md
```

`state_dir` defaults to `.vibe-loop`, so these defaults do not mutate repository
docs or create committed artifacts. Explicit output paths are opt-in through
command flags or `[planning_analytics.outputs]` config when a repository wants
generated reports in a tracked docs workflow.

Explicit output paths must be repo-relative and cannot contain `..`. `doctor`
serializes every resolved output path and marks each source as
`default_state_dir` or `explicit`.

## Coverage Semantics

Coverage answers two separate questions:

- Done task coverage: every task marked complete should have authoritative
  evidence explaining the final completion mapping.
- Commit coverage: every non-generated, in-scope commit in the selected window
  should be mapped to an explicit task id or reported with a skipped/warning
  reason.
- Requirement coverage: every requirement id exposed by normalized tasks or
  explicit evidence should be classified as `satisfied`, `attempted`,
  `missing_evidence`, `pending`, or `unmapped`.

Generated commits and generated analytics artifacts are excluded from commit
coverage by default. Subject matching may add diagnostics for unmapped commits,
but coverage remains failed or warning-marked unless an authoritative mapping is
present.

Requirement coverage is derived from normalized task `requirement_ids`,
task-level completion evidence, worker-report metadata, and `Requirement:`
commit trailers. `satisfied` means accepted completion evidence exists beyond
task-source status alone. `attempted` means a report or mapping exists but is
not accepted completion evidence. `missing_evidence` means a requirement is
linked to completed plan rows without accepted evidence. `unmapped` means
evidence cites a requirement id that no plan row exposes.

## Projection Policy

The default projection policy is `current-runner-parity`. Projected incomplete
tasks are scheduled from the latest actual end by dependency readiness, then the
same deterministic sort used by the runner: status rank (`Active`, `Next`,
`Planned`), priority rank, and source order. This makes analytics reflect what
`vibe-loop` would run today.

`lightmetrics-parity` is a named alternate policy for repositories comparing
against the prototype behavior. It preserves the prototype's dependency
readiness, Active-first behavior, priority-before-remaining-status ordering, and
plan order. Generated timeline JSON must serialize the selected
`schedule_policy` so readers can tell which policy produced projections.

## Timeline JSON

`vibe-loop planning timeline --json` emits a versioned JSON document with
source provenance, sections, task rows, schedule policy, and warnings. Completed
tasks with authoritative commit mappings receive an `actual` span built from
mapped commit author times. The elapsed gap assigned to each mapped commit is
clipped to eight hours; the first mapped commit uses a one-minute floor because
there is no prior mapped author time. The output preserves both
`raw_duration_minutes` and `idle_gap_clipped_minutes` so long idle periods are
visible without inflating projected durations.

The timeline JSON also includes `requirements`, a requirement coverage ledger
with linked task ids, satisfied or attempted task ids, missing-evidence task ids,
mapped commits, run ids, source names, and review/test references. Source
provenance includes requirement counts by status.

Incomplete tasks receive `projected` spans from the latest actual end, or from a
documented fallback anchor when no actual end exists. Projections are scheduled
by dependency readiness and the configured projection policy.

Projected estimates are produced by `robust-duration-baseline-v1`. The model
trains only on completed tasks with authoritative actual spans, applies
log-space winsorization before calculating medians, and selects the narrowest
usable baseline in this order: matching workstream plus priority, matching
workstream, matching priority, then global history. When no completed actual
history exists, estimates fall back to one hour. Each estimate serializes the
chosen minutes, low/high interval, interval coverage, model name, training
sample counts, outlier handling, feature reasons, evidence reasons, and any
leakage-safe similarity examples. Similarity uses only pre-task text fields:
title, scope, and acceptance.

## Timeline And Gantt Artifacts

`vibe-loop planning artifacts` writes the timeline JSON plus a static Gantt HTML
report. Defaults use `<state_dir>/planning-analytics/timeline.json` and
`<state_dir>/planning-analytics/gantt.html`. `--output` and `--html-output`
accept repo-relative paths for repositories that intentionally commit generated
planning docs.

`--check` rebuilds the same deterministic JSON and HTML and fails if either
artifact is missing or stale. `--inspect` reads existing artifacts without
running collection, reports their source, freshness state, schema status,
warning counts, and timeline warning details, and prints repair commands. It
reports `freshness = "not_checked"` for readable current-schema artifacts; use
`--check` when actual staleness must be computed. The HTML report embeds a small
generated metadata marker so `doctor` and `--inspect` can read the schema
version and warning count without executing analytics.

## Duration Benchmark

`vibe-loop planning benchmark-duration` evaluates duration-estimator candidates
against completed tasks with authoritative actual spans. It assigns stable
validation folds from task ids and mapped commit ids, keeps tasks that share a
validation commit out of that fold's training set, and reports the leakage
checks in the generated JSON.

The JSON and Markdown reports include MAE, MAPE, mean log error, interval
coverage, signed bias, and worst misses for each candidate. Reports are written
to `<state_dir>/planning-analytics/duration-benchmark.{json,md}` by default, or
to explicit `[planning_analytics.outputs]` benchmark paths. `--check` rebuilds
the report, compares it with those files, and fails if the configured generator
model name or parameters differ from the benchmark-selected estimator.

Duration model configuration is explicit under
`[planning_analytics.duration_model]`: model name, group minimum sample count,
similarity threshold, maximum similarity examples, similarity blend weight, and
fallback minutes. The timeline generator and benchmark compare the same
configuration payload so stale reports and unbenchmarked parameter changes are
visible.

## Doctor Readiness

`vibe-loop doctor` reports planning analytics readiness without running a
collector. The report includes the selected schedule policy, subject matching
mode, worklog adapter presence, coverage tiers, resolved artifact paths, whether
repo-artifact outputs are explicitly enabled, artifact freshness state, schema
status, warning counts, next repair commands, and diagnostics.

Planning analytics is `ready` when the active task source is usable. If task
discovery is unavailable, stale, invalid, or disabled by broken explicit config,
the readiness status is `task_source_unusable` and the diagnostics point back to
the task-source problem. This keeps analytics failures anchored to the source
contract rather than silently inventing tasks.
