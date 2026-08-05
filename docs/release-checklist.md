# Release Checklist

Use this checklist to produce evidence and start publishing. The GitHub release
workflow, not this checklist, makes the exact-revision admission decision before
either package-index credential is exercised. The contract is authoritative in
[PRD-EVL-005](prd/evals-release.md#prd-evl-005-release-readiness-gate).

## Versioning And Repository Hooks

Use `make bump-patch`, `make bump-minor`, or `make bump-major` to update the
project and lockfile versions through `uv`. `make tag` creates `v<version>` from
`uv version --short`; pass `VERSION=...` to validate and tag an explicit
version. Tagging requires a clean worktree. The pre-push hook rejects a pushed
`v*` tag unless the tag, `pyproject.toml`, and the `vibe-loop` entry in
`uv.lock` have the same version.

`make install-hooks` installs the repository's pre-commit, pre-push,
prepare-commit-msg, and commit-msg hooks. The pre-commit hook runs the
documentation gates for Markdown changes plus `ruff check` and
`ruff format --check`. For commits made by a vibe-loop worker, the commit-msg
hook adds `Plan-Item`, `Run-Id`, and `Agent-Kind` trailers when available; the
prepare-commit-msg hook preserves that provenance path when `--no-verify`
bypasses commit-msg. Installation refuses to overwrite unmanaged hooks except
for a compatible existing provenance hook.

## Bundled Skill Gate

Run the local release gate from a clean repository state:

```bash
uv run vibe-loop eval release-gate --repo . --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

The command runs `local-demo-v1` unless `--aggregate` or `--dry-run` is supplied.
The release gate requires:

- every required release-gate case/condition pair has at least one passing
  trial;
- the aggregate includes `skill_quality` condition summaries and
  workflow-contract failure evidence;
- the aggregate has no unresolved `workflow_contract_regression` flags;
- any accepted workflow-contract regression is parked with a task id before
  publishing;
- the aggregate was produced at the exact clean commit and every required
  trial contains the matching bundled-skill fingerprint;
- release notes or the task plan will reference the stable evidence URL from
  validated release-admission output, rather than a copied download URL.

The default release matrix is intentionally smaller than the full paired eval
suite. It excludes `no_skill`, covers finite `vibe_loop` behavior across the
representative task domains, runs CLI-supervised cases under `vibe_loop_cli`,
pins legacy workspace/integration stories to explicit worker-owned mode, runs
`runtime-owned-implementation` with the slim runtime-owned worker contract,
checks orchestration only on delegation-specific cases, and runs the negative
trigger set under `vibe_loop`. Use `eval local-demo` for broad no-skill baseline
comparisons. Use `--trials N --minimum-trials N` when a repeated release run is
needed.

The release gate may use a cheaper deterministic-enough model as long as the
agent command, model id, and artifacts are recorded in the aggregate. For
example:

```bash
uv run vibe-loop eval release-gate --repo . --overwrite \
  --agent-command '*=codex exec -m gpt-5.3-codex-spark {prompt}' \
  --record-output .vibe-loop/release-readiness.json
```

For changes to bundled skill text or the CLI worker prompt addendum, also verify
the install output and prompt contract from a clean tree:

```bash
uv run vibe-loop install-skills --codex --home <tmpdir>
uv run vibe-loop verify-skills --home <tmpdir>
uv run python -m unittest tests.test_cli.CliTests.test_install_skills_are_cli_agnostic \
  tests.test_cli.CliTests.test_cli_worker_addendum_contains_coordination
```

Installed skill files must remain CLI-agnostic: no worker report commands,
workspace-claim commands, integration-lock commands, or supervisor environment
variables belong in the reusable skill text. CLI-launched worker coordination
belongs in the runner addendum and must describe the runtime-provisioned claim,
integration-lock wait behavior, and blocked-report guidance for unsafe
workspace or integration-lock states.

For a dry-run over an existing aggregate, use:

```bash
uv run vibe-loop eval release-gate --repo . --dry-run \
  --aggregate .vibe-loop/eval-runs/local-demo-v1/aggregate.json \
  --record-output .vibe-loop/release-readiness-dry-run.json
```

This diagnostic path may run from a dirty worktree because it does not execute
new trials. Exact commit and bundled-skill fingerprint checks still determine
whether its record is publishable.

If a workflow-contract regression is intentionally parked, use the regression id
from the release-readiness record:

```bash
uv run vibe-loop eval release-gate --repo . --dry-run \
  --aggregate .vibe-loop/eval-runs/local-demo-v1/aggregate.json \
  --parked-regression condition_comparison:vibe_loop=EVAL-99
```

`--parked-workflow-regression EVAL-99` is available when every current
workflow-contract regression is covered by the same follow-up task.

## External Smoke Evidence

External benchmark smoke results are optional. They should be summarized in a
small JSON file and attached to the release record:

```bash
uv run vibe-loop eval release-gate --repo . --dry-run \
  --aggregate .vibe-loop/eval-runs/local-demo-v1/aggregate.json \
  --external-benchmark-json path/to/external-smoke-summary.json \
  --record-output .vibe-loop/release-readiness.json
```

The release gate stores the summary file path, size, SHA-256, benchmark name,
status, and selected summary fields. Do not attach raw benchmark logs or
transcripts to the release-readiness record.

The pinned SWE-rebench V2 multilingual smoke is a post-`0.2.0` follow-up, not a
`0.2.0` release blocker and not a replacement for the local-demo matrix. Its
result is absent by default and is recorded only when the operator intentionally
passes the generated `swe-rebench-v2-multilingual-smoke-results.json` with
`--external-benchmark-json`. Treat `infrastructure_failed` separately from
`agent_failed` when interpreting the optional summary, and retain the manifest's
non-leaderboard caveat in release evidence.

`docs/examples/release-readiness-dry-run.json` shows an exact-revision record
shape with illustrative full commits and skill fingerprints, local-suite
evidence, and optional external smoke evidence.

## Publish

After the release-readiness record passes:

1. Upload its compact JSON for the exact commit through the `Skill Release
   Readiness Evidence` workflow. The immutable artifact name includes the full
   commit. A dry-run is accepted only when it carries the same exact revision
   and trial fingerprint contract.
2. Do not construct the release-note evidence link manually. After admission,
   copy the stable GitHub evidence reference printed by `release-admit` and the
   release job summary. If regressions were parked, include the task ids.
3. Run the release workflow for TestPyPI, optionally supplying the evidence run
   id to select among exact-head artifacts. PyPI remains restricted to a
   matching `v<version>` tag.
4. The release workflow discovers the prior reachable release tag, classifies
   every changed/renamed/deleted path, validates the record only when an owned
   path changed, compares every distribution's bundled skills, and uploads a
   hashed admission bundle containing the validated repository, workflow, run,
   artifact, exact-head, and stable-reference provenance. Both publishers
   download and revalidate that bundle and the distributions before trusted
   publishing begins. A missing or changed provenance file blocks publication.
   Releases with only unrelated paths emit an explicit exemption summary and
   do not require provenance or run the 20-case gate.
