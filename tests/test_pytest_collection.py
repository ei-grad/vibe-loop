from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.eval_examples import materialize_eval_example


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETION_SUITE_COMMAND = "uv run -m pytest tests/"


class PytestCollectionTests(unittest.TestCase):
    def test_ci_uses_completion_suite_command(self) -> None:
        workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"

        self.assertEqual(
            workflow_job_run_commands(workflow, "test"),
            [COMPLETION_SUITE_COMMAND],
        )

    def test_bare_collection_matches_explicit_project_suite(self) -> None:
        bare = collect_node_ids(REPO_ROOT)
        explicit_root = collect_node_ids(REPO_ROOT, ".")
        explicit = collect_node_ids(REPO_ROOT, "tests")

        self.assertEqual(bare, explicit)
        self.assertEqual(explicit_root, explicit)
        self.assertTrue(bare)

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


def workflow_job_run_commands(workflow: Path, job: str) -> list[str]:
    lines = workflow.read_text().splitlines()
    job_header = f"  {job}:"
    try:
        start = lines.index(job_header) + 1
    except ValueError as error:
        raise AssertionError(f"workflow job not found: {job}") from error

    commands = []
    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        stripped = line.strip()
        if stripped.startswith("run: "):
            commands.append(stripped.removeprefix("run: "))
    return commands
