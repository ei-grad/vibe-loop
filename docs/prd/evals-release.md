# Evals And Release PRD

This PRD owns Level 2 contracts for bundled skill evaluation, artifact records,
aggregate reporting, external benchmark adapters, and release-readiness gates.

## PRD-EVL-001 Paired Skill Evaluation

Local skill evals must compare the same task under paired skill conditions so
the bundled skill is the experimental variable rather than a hidden environment
change.

Acceptance must cover `no_skill`, `vibe_loop`, optional `infinite_vibe_loop`,
candidate/self-generated conditions, fresh fixture checkouts, fresh eval state,
stable prompts, stable agent harness settings, budgets, and repeated trials.

Related implementation IDs: `EVAL-00`, `EVAL-01`, `EVAL-02`, `EVAL-03`.

## PRD-EVL-002 Artifact Schema

Every completed trial must leave a reproducible artifact bundle with a durable
`run.json` index and safe relative artifact references.

Acceptance must cover prompt, run log, transcript, diff, final repo state,
structured result, grader outputs, source fingerprints, SHA-256 validation,
secret-like path rejection before reads, stale fingerprint detection, and
fresh-workspace evidence.

Related implementation IDs: `EVAL-01`, `EVAL-03`.

## PRD-EVL-003 Workflow-Contract Grading

Eval scoring must separate task outcome from workflow-contract behavior so a
passing code patch can still fail the workflow contract.

Acceptance must cover task score, workflow score, trigger score, failure
taxonomy, review/integration discipline failures, unsafe git behavior,
unnecessary prompts, state contamination, timeout and infrastructure separation,
and transcript/trace-envelope grading only where final state cannot prove the
behavior.

Related implementation IDs: `EVAL-02`, `EVAL-03`, `EVAL-05`, `EVAL-09`.

## PRD-EVL-004 Aggregate Skill Quality Reporting

Eval aggregates must expose pass rates and skill-quality diagnostics with links
back to raw trial artifacts.

Acceptance must cover per-condition and per-task pass rates, uplift, normalized
gain, confidence intervals when repeated trials exist, latency, command count,
token/cost fields when available, per-domain reports, prior-run regressions, and
artifact-root links for each count or delta.

Related implementation IDs: `EVAL-03`, `EVAL-05`.

## PRD-EVL-005 Release Readiness Gate

Bundled skill releases must require complete local-demo evidence and block
unresolved workflow-contract regressions unless they are explicitly parked with
task IDs.

Acceptance must cover default three-trial release runs, dry-run over existing
aggregates, release-readiness records, parked regression flags, optional
external benchmark summaries, and release-note references to evidence.

Related implementation IDs: `EVAL-06`.

## PRD-EVL-006 External Benchmark Adapters

External benchmark adapters must be optional smoke or stress checks, not local
release gates or leaderboard claims.

Acceptance must cover explicit configuration, Docker/storage/network cost
disclosure, dataset and harness provenance, sample IDs, image identifiers where
relevant, grader provenance, non-leaderboard caveats, and separation from
bundled skill release requirements.

Related implementation IDs: `EVAL-04`, `EVAL-07`.
