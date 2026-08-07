# Release Checklist

Use this checklist to check a candidate and start publishing. It documents the
operator procedure; [PRD-EVL-005](prd/evals-release.md#prd-evl-005-pre-release-eval-usability)
is the sole authority for pre-release eval usability, admission, and publishing
policy.

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

## Check The Bundled Skills

When a release candidate changes bundled skills, the eval harness, or the
runtime behavior they exercise, run the curated release matrix from a clean
repository state and read the result before publishing. This is an operator
step: nothing in the publishing path waits on it, and the record it writes
stays on the local machine.

```bash
uv run vibe-loop eval release-gate --repo . --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

The command runs `local-demo-v1` unless `--aggregate` or `--dry-run` is supplied.
Use `eval local-demo` for broad no-skill baseline comparisons. Use `--trials N`
with `--minimum-trials N` when a repeated run is needed.

To run with an alternative recorded agent command and model, use:

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
whether its record describes the candidate revision or only an older aggregate.

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

[PRD-EVL-005](prd/evals-release.md#prd-evl-005-pre-release-eval-usability) owns
release attachment policy, and
[PRD-EVL-006](prd/evals-release.md#prd-evl-006-external-benchmark-adapters) owns
the external-adapter contract and its evidentiary limits. To attach a compact
summary intentionally, pass it to the release gate:

```bash
uv run vibe-loop eval release-gate --repo . --dry-run \
  --aggregate .vibe-loop/eval-runs/local-demo-v1/aggregate.json \
  --external-benchmark-json path/to/external-smoke-summary.json \
  --record-output .vibe-loop/release-readiness.json
```

The release gate stores the summary file path, size, SHA-256, benchmark name,
status, and selected summary fields. Do not attach raw benchmark logs or
transcripts to the release-readiness record.

The pinned SWE-rebench V2 multilingual smoke is a version-neutral research
adapter. Its setup and interpretation are documented in
[External Benchmark Fit](external-benchmark-fit.md#pinned-swe-rebench-v2-smoke).
Pass the generated `swe-rebench-v2-multilingual-smoke-results.json` only when
intentionally attaching that evidence, distinguish `infrastructure_failed`
from `agent_failed`, and retain the manifest's non-leaderboard caveat.

`docs/examples/release-readiness-dry-run.json` shows an exact-revision record
shape with illustrative full commits and skill fingerprints, local-suite
evidence, and optional external smoke evidence.

## Publish

Nothing is uploaded to GitHub to authorize a release. The eval record stays on
the machine that produced it; publishing is gated only on the built
distributions matching the commit being published.

1. Decide, from the matrix result you just read, whether the candidate is worth
   releasing. If regressions were parked, cite the task ids in the release
   notes and keep the record locally for reference.
2. Run the release workflow for TestPyPI. PyPI remains restricted to a matching
   `v<version>` tag.
3. Let the release workflow apply the exact-revision admission and
   pre-publishing checks defined by
   [PRD-EVL-005](prd/evals-release.md#prd-evl-005-pre-release-eval-usability). Do not
   bypass or manually reconstruct that admission path.
