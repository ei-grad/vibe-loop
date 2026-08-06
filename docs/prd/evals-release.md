# Evals And Release PRD

This PRD owns Level 2 contracts for bundled skill evaluation, artifact records,
aggregate reporting, external benchmark adapters, and release-readiness gates.
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

## PRD-EVL-005 Release Readiness Gate

Bundled skill releases must require compact, release-relevant local-demo
evidence and block unresolved workflow-contract regressions unless they are
explicitly parked with task IDs.

Every publishing event classifies the canonical full commit being built against
the prior reachable `v*` release tag. The code-owned path boundary includes
bundled skills, the eval harness and fixtures, release schemas and contracts,
package manifests and lockfiles, and release workflows. Rename and deletion
paths are classified on both sides. Missing or non-ancestor history, shallow
history, malformed commits, and unknown diff statuses are fail-closed; only a
complete exact-base/head path set containing no owned path can produce an
unrelated-release exemption.

Required readiness records bind the base and head commits, each required
trial's skill source fingerprint, and the complete bundled-skill fingerprint
set. Admission compares those fingerprints with every built wheel and source
distribution. For readiness-required admission, GitHub discovery also persists
strict provenance binding the selected GitHub evidence to the exact
classification and canonical readiness record. The
[release-evidence schema](../skill-eval-schema.md#release-evidence-records) is
the sole authority for its persisted field set. The provenance is derived only
from mutually consistent GitHub repository, workflow, run, and artifact
responses and exposes their canonical stable GitHub HTTPS evidence reference.
A manual run id may narrow discovery but cannot supply or override provenance.
API download URLs, redirects, signed URLs, tokens, local paths, and
operator-supplied links are never provenance.

The classification, optional readiness record, required readiness provenance,
admission record, and distributions are hashed and revalidated after transfer
before either publisher exercises trusted publishing. Missing, malformed,
mismatched, substituted, or tampered readiness provenance blocks publication.
A complete unrelated-release exemption requires neither readiness record nor
provenance. Tag-triggered PyPI and manual TestPyPI/PyPI publication share this
contract. Admission command output and the GitHub job summary expose the stable
evidence reference, or explicitly identify an unrelated-release exemption,
without secret-bearing or expiring values. Non-publishing `workflow_run`
build/test executions produce no admission, exemption, readiness-evidence
reference, or publish-readiness claim.

Acceptance must cover a curated release matrix distinct from the full paired
eval suite, no required `no_skill` baseline for release readiness, required
trial pass/fail blocking, dry-run over existing aggregates, release-readiness
records, parked regression flags, optional external benchmark summaries, and
release-note references to evidence. The full local-demo suite must still
support paired `no_skill` comparisons for broader analysis.

The shipped matrix currently contains 20 cases and 22 required case/condition
pairs. The default minimum is one trial per pair. It covers table and generated
discovery, explicit-list and spec-driven profiles, command-backed task and lock
adapters, runtime-owned orchestration, review remediation, worktree safety, and
integration failure paths.

Related implementation IDs: `EVAL-06`,
`publish-gate-exact-revision-skill-readiness-evidence`.

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
