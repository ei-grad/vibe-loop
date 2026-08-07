# Evals And Release PRD

This PRD owns Level 2 contracts for bundled skill evaluation, artifact records,
aggregate reporting, external benchmark adapters, pre-release eval usability,
and release admission.
The [skill evaluation strategy](../skill-evaluation-strategy.md) explains the
methodology and research rationale; it defers to this PRD for product behavior
and release policy.

## PRD-EVL-001 Paired Skill Evaluation

Local skill evals must compare the same task under paired skill conditions so
the bundled skill is the experimental variable rather than a hidden environment
change.

Acceptance must cover `no_skill`, `vibe_loop`, optional `infinite_vibe_loop`,
candidate/self-generated conditions, fresh fixture checkouts, fresh eval state,
stable prompts, stable agent harness settings, budgets, and repeated trials.

Related implementation IDs: `EVAL-00`, `EVAL-01`, `EVAL-02`, `EVAL-03`,
`EVAL-08`.

## PRD-EVL-002 Artifact Schema

Every completed trial must leave a reproducible artifact bundle with a durable
`run.json` index and safe relative artifact references.

Acceptance must cover prompt, run log, transcript, diff, final repo state,
structured result, grader outputs, source fingerprints, SHA-256 validation,
secret-like path rejection before reads, stale fingerprint detection, and
fresh-workspace evidence. Structured transcript, result, grader, summary,
aggregate, and history artifacts must retain only the closed, versioned,
size-bounded envelope owned by the
[skill eval schema](../skill-eval-schema.md#structured-retention-envelope).
`logs/run.log` remains the deliberate content-bearing audit exception.

Related implementation IDs: `EVAL-01`, `EVAL-03`.

## PRD-EVL-003 Workflow-Contract Grading

Eval scoring must separate task outcome from workflow-contract behavior so a
passing code patch can still fail the workflow contract.

Acceptance must cover task score, workflow score, trigger score, failure
taxonomy, review/integration discipline failures, unsafe git behavior,
unnecessary prompts, state contamination, timeout and infrastructure separation,
and transcript/trace-envelope grading only where final state cannot prove the
behavior. Grading must derive labels, counts, and classifications from
in-process raw evidence before persistence or from the admitted safe envelope;
missing or rejected evidence cannot silently turn an ungradable workflow into
a pass or reduce an observed command count.

Related implementation IDs: `EVAL-02`, `EVAL-03`, `EVAL-05`, `EVAL-08`,
`EVAL-09`.

## PRD-EVL-004 Aggregate Skill Quality Reporting

Eval aggregates must expose pass rates and skill-quality diagnostics with links
back to raw trial artifacts.

Acceptance must cover per-condition and per-task pass rates, uplift, normalized
gain, confidence intervals when repeated trials exist, latency, command count,
token/cost fields when available, per-domain reports, prior-run regressions, and
artifact-root links for each count or delta.

Related implementation IDs: `EVAL-03`, `EVAL-05`.

## PRD-EVL-005 Pre-Release Eval Usability

The eval harness must be usable for checking a bundled skill release before it
is published: an operator must be able to run a curated release matrix on
demand against an exact revision and read honest per-pair results. Running that
matrix and acting on its result is an operator step in the
[release checklist](../release-checklist.md), not a machine precondition of
publishing.

A matrix run binds the canonical full commit being checked and the prior
reachable `v*` release tag, records the complete bundled-skill fingerprint set,
and reports per-pair pass and fail counts. Required-trial failures, missing
matrix coverage, unresolved workflow-contract regressions, invalid parked
regression IDs, and trial evidence whose skill fingerprints do not match the
checked revision are reported as record blockers and as a non-zero exit status.
Regressions parked with task IDs are reported as parked rather than as
failures. A record that is not bound to exact commits and fingerprints stays
diagnostically useful but must report that gap instead of claiming
exact-revision coverage.

No eval record, and no other statement about a local environment or the skills
installed in it, is published to GitHub or any other remote service, and no
remotely hosted artifact is a precondition of publishing. Correspondence
between a local environment and its installed skills is a local-run concern
owned by [recorded skill deployment](../skill-deployment.md).

Publishing must still bind what is published to what was built. Every
publishing event resolves the canonical full commit being built, verifies that
the bundled skill sources present are the ones committed at that revision,
records their fingerprint set and each distribution's name, size, and SHA-256,
and blocks when a distribution's packaged skills differ from that commit. The
[release-evidence schema](../skill-eval-schema.md#release-evidence-records) is
the sole authority for the persisted admission field set. The admission record
is revalidated against the repository and the distributions after transfer and
before either publisher exercises trusted publishing, so a substituted,
tampered, or extended record blocks publication. Tag-triggered PyPI and manual
TestPyPI/PyPI publication share this contract. Admission command output and the
GitHub job summary are deterministic and carry no secret-bearing or expiring
values. Non-publishing `workflow_run` build/test executions produce no
admission or publish-readiness claim.

Acceptance must cover a curated release matrix distinct from the full paired
eval suite, no required `no_skill` baseline for the release matrix, per-pair
pass/fail reporting, dry-run over existing aggregates, exact-revision records,
parked regression flags, optional external benchmark summaries, and
release-note references to locally held evidence. The full local-demo suite
must still support paired `no_skill` comparisons for broader analysis.

The shipped matrix currently contains 20 cases and 22 required case/condition
pairs. The default minimum is one trial per pair. It covers table and generated
discovery, explicit-list and spec-driven profiles, command-backed task and lock
adapters, runtime-owned orchestration, review remediation, worktree safety, and
integration failure paths.

Related implementation IDs: `EVAL-06`,
`prd-evl-005-reframe-as-eval-usability`.

## PRD-EVL-006 External Benchmark Adapters

External benchmark adapters must be optional smoke or stress checks, not local
release gates or leaderboard claims.

The repository-owned verification for the pinned SWE-rebench V2 multilingual
smoke must exercise all 24 manifest identities hermetically through the
manifest adapter and benchmark CLI, verify the result taxonomy and six
four-instance language denominators, and prove that the generated compact
result can be attached intentionally to a dry-run release-readiness record.
Omitting or attaching that optional record must not change the local-suite
release decision. This verification covers only the repository's manifest,
adapter, result-accounting, and release-attachment contract. It does not verify
the upstream harness, container images, task export, agent quality, an actual
external smoke execution, or leaderboard comparability.

Acceptance must cover explicit configuration, Docker/storage/network cost
disclosure, dataset and harness provenance, sample IDs, image identifiers where
relevant, grader provenance, non-leaderboard caveats, and separation from
bundled skill release requirements.

The first supported adapter may be manifest-driven: repositories provide a
small, explicit benchmark manifest with instance metadata, setup commands,
grader commands, image identifiers, and harness provenance. Public
benchmark-specific adapters can be added later when their dataset terms,
container requirements, and comparability rules are pinned.

Related implementation IDs: `EVAL-04`, `EVAL-07`, `EVAL-10`.
