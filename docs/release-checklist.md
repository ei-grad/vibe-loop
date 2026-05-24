# Release Checklist

Use this checklist before publishing a package version that changes bundled
skills or their eval harness. The GitHub release workflow builds and publishes
artifacts; this checklist records the skill-readiness evidence that should exist
before the workflow is used for TestPyPI or PyPI.

## Bundled Skill Gate

Run the local release gate from a clean repository state:

```bash
uv run vibe-loop eval release-gate --repo . --trials 3 --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

The command runs `local-demo-v1` unless `--aggregate` or `--dry-run` is supplied.
The release gate requires:

- every bundled local-demo case and declared condition has at least 3 trials;
- the aggregate includes `skill_quality` condition comparisons and
  workflow-contract failure evidence;
- the aggregate has no unresolved `workflow_contract_regression` flags;
- any accepted workflow-contract regression is parked with a task id before
  publishing;
- release notes or the task plan reference the release-readiness record.

For changes to bundled skill text or the CLI worker prompt addendum, also verify
the install output and prompt contract from a clean tree:

```bash
uv run vibe-loop install-skills --codex --home <tmpdir>
uv run python -m unittest tests.test_cli.CliTests.test_install_skills_are_cli_agnostic \
  tests.test_cli.CliTests.test_cli_worker_addendum_contains_coordination
```

Installed skill files must remain CLI-agnostic: no worker report commands,
workspace-claim commands, integration-lock commands, or supervisor environment
variables belong in the reusable skill text. CLI-launched worker coordination
belongs in the runner addendum and must include workspace claiming,
integration-lock wait behavior, and blocked-report guidance for unsafe
workspace or integration-lock states.

For a dry-run over an existing aggregate, use:

```bash
uv run vibe-loop eval release-gate --repo . --dry-run \
  --aggregate .vibe-loop/eval-runs/local-demo-v1/aggregate.json \
  --record-output .vibe-loop/release-readiness-dry-run.json
```

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

`docs/examples/release-readiness-dry-run.json` shows the expected record shape
with local-suite evidence and optional external smoke evidence.

## Publish

After the release-readiness record passes:

1. Include the record path or artifact link in release notes.
2. If regressions were parked, include the task ids.
3. Run the manual release workflow for TestPyPI.
4. Publish to PyPI only from a `v<version>` tag matching `pyproject.toml`.
