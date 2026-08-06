from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.eval_examples import materialize_eval_example


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETION_SUITE_COMMAND = "uv run -m pytest tests/"
RELEASE_WHEEL_SUITE_COMMAND = (
    "uv run --no-project --with pytest --with dist/*.whl python -m pytest tests/"
)


class PytestCollectionTests(unittest.TestCase):
    def test_repository_workflows_retain_suite_gate_commands(self) -> None:
        ci_workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        release_workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"

        self.assertTrue(
            workflow_job_runs_command(ci_workflow, "test", COMPLETION_SUITE_COMMAND)
        )
        self.assertTrue(
            workflow_job_runs_command(
                release_workflow,
                "test",
                RELEASE_WHEEL_SUITE_COMMAND,
            )
        )

    def test_workflow_command_guard_accepts_other_steps_and_block_scalars(
        self,
    ) -> None:
        workflow = """\
jobs:
  test:
    steps:
      - run: uv sync --locked
      - run: |
          uv run -m pytest tests/
  other:
    steps: []
"""

        self.assertTrue(
            workflow_job_text_runs_command(workflow, "test", COMPLETION_SUITE_COMMAND)
        )

    def test_bare_collection_matches_explicit_project_suite(self) -> None:
        bare = collect_node_ids(REPO_ROOT)
        explicit_root = collect_node_ids(REPO_ROOT, ".")
        explicit = collect_node_ids(REPO_ROOT, "tests")

        self.assertEqual(bare, explicit)
        self.assertEqual(explicit_root, explicit)
        self.assertTrue(bare)

    def test_repository_recursion_exclusions_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            shutil.copyfile(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
            tests = repo / "tests"
            tests.mkdir()
            (tests / "test_authoritative.py").write_text(
                "def test_authoritative():\n    assert True\n"
            )
            excluded_tests = (
                repo
                / ".vibe-loop"
                / "main-verification"
                / "repo"
                / "tests"
                / "test_nested.py",
                repo
                / ".runtime-state"
                / "verification"
                / "repo"
                / "tests"
                / "test_runtime.py",
                repo
                / ".venv"
                / "lib"
                / "site-packages"
                / "pkg"
                / "tests"
                / "test_vendored.py",
                repo / "build" / "test_build.py",
                repo / "dist" / "test_dist.py",
                repo / "node_modules" / "pkg" / "test_node_modules.py",
            )
            for path in excluded_tests:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def test_excluded():\n    assert True\n")

            bare = collect_node_ids(repo)
            explicit_root = collect_node_ids(repo, ".")
            explicit_tests = collect_node_ids(repo, "tests")

        self.assertEqual(bare, ["tests/test_authoritative.py::test_authoritative"])
        self.assertEqual(explicit_root, bare)
        self.assertEqual(explicit_tests, bare)

    def test_materialized_eval_fixture_collects_from_its_own_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "finite-py-plan-table",
                Path(directory) / "finite",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo / "src")

            node_ids = collect_node_ids(repo, env=env)

        self.assertEqual(
            node_ids,
            [
                "tests/test_calculator.py::"
                "LoyaltyTotalTests::test_member_receives_ten_unit_discount",
                "tests/test_calculator.py::"
                "LoyaltyTotalTests::test_non_member_pays_full_total",
            ],
        )


def collect_node_ids(
    cwd: Path,
    *paths: str,
    env: dict[str, str] | None = None,
) -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *paths],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    return [line for line in completed.stdout.splitlines() if "::" in line]


def workflow_job_runs_command(workflow: Path, job: str, command: str) -> bool:
    return workflow_job_text_runs_command(workflow.read_text(), job, command)


def workflow_job_text_runs_command(workflow: str, job: str, command: str) -> bool:
    lines = workflow.splitlines()
    job_header = f"  {job}:"
    try:
        start = lines.index(job_header) + 1
    except ValueError as error:
        raise AssertionError(f"workflow job not found: {job}") from error

    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        stripped = line.strip()
        if stripped in {command, f"run: {command}"}:
            return True
    return False
