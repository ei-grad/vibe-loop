# Planning Analytics PRD

> **Superseded (removed from vibe-loop).** PRD-ANL-001..004 described the in-tree
> planning-analytics feature (`vibe-loop planning ...`, `[planning_analytics]`
> config, timeline/Gantt artifacts). That feature was removed; timeline and Gantt
> reporting now live in the [loopyard](https://github.com/ei-grad/loopyard) web UI
> over the read-only `autopilot status --json` boundary. This PRD is retained for
> historical reference only.

This PRD owns Level 2 contracts for planning evidence, coverage checks,
timeline/Gantt artifacts, duration benchmarking, and analytics readiness.

## PRD-ANL-001 Analytics Boundary

Planning analytics must report on normalized tasks, run records, optional
worklogs, and bounded git metadata without affecting task selection, worker
locks, or completion classification.

Acceptance must cover analytics inputs, derived fields scoped to analytics
output, no feedback into `next` or `tasks runnable`, and readiness diagnostics
anchored to task-source usability.

Related implementation IDs: `GANTT-01`, `GANTT-02`, `GANTT-07`.

## PRD-ANL-002 Evidence Tiers

Analytics must distinguish authoritative completion evidence from diagnostic
heuristics.

Acceptance must cover task-source `Done` state, structured worker reports,
optional worklog adapter records, explicit commit references, `Plan-Item:`
trailers, trailer-ready run context emitted through `runs.jsonl`, diagnostic
subject/branch matching, raw log exclusion from coverage, and warnings that
distinguish attempted work from accepted completion evidence. Run context that
has not been persisted to a worker report, commit trailer, task source, or
worklog remains diagnostic candidate evidence, not authoritative completion
evidence. Model fields in that context must come from observed agent startup
output or safely inferred runtime facts, and must be omitted when unavailable.

Related implementation IDs: `GANTT-01`, `GANTT-02`, `SDD-05`, `RT-07`.

## PRD-ANL-003 Git Metadata Safety

Analytics may read bounded git metadata needed for coverage and timelines, but
must avoid secrets, broad environment state, and full diffs by default.

Acceptance must cover allowed commit fields, changed repo-relative paths where
needed, skip reasons for secret-like paths, generated-artifact exemptions,
metadata-only commit exemptions, and no environment variable reads.

Related implementation IDs: `GANTT-02`.

## PRD-ANL-004 Timeline And Gantt Artifacts

The CLI must generate deterministic planning timeline JSON and optional static
Gantt HTML reports under the configured state directory by default, with
explicit repo-relative output paths for repositories that opt into committed
artifacts.

Acceptance must cover source provenance, sections, actual and projected spans,
schedule policy serialization, warning serialization, default artifact paths,
explicit output validation, `--check` staleness failures, and `--inspect`
read-only artifact diagnostics.

Related implementation IDs: `GANTT-03`, `GANTT-06`, `GANTT-07`.

## PRD-ANL-005 Duration Estimation And Benchmarking

Projected durations must use benchmarked, leakage-safe historical baselines
rather than unvalidated feature heuristics.

Acceptance must cover robust-duration-baseline-v1, completed actual span
training data, log-space winsorization, workstream/priority/global fallbacks,
pre-task similarity only, interval reporting, benchmark folds, validation-task
exclusion from training, MAE/MAPE/log-error/bias/coverage reporting, and
generator-versus-benchmark parameter drift detection.

Related implementation IDs: `GANTT-04`, `GANTT-05`.

## PRD-ANL-006 Doctor Readiness

`vibe-loop doctor` must report planning analytics readiness without running a
collector or mutating artifacts.

Acceptance must cover selected schedule policy, subject matching mode, worklog
adapter presence, coverage tiers, artifact paths, repo-artifact opt-in status,
schema/freshness state, warning counts, next repair commands, and
`task_source_unusable` diagnostics.

Related implementation IDs: `GANTT-06`.
