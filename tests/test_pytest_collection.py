from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.eval_examples import materialize_eval_example


REPO_ROOT = Path(__file__).resolve().parents[1]


class PytestCollectionTests(unittest.TestCase):
    def test_bare_collection_matches_explicit_project_suite(self) -> None:
        bare = collect_node_ids(REPO_ROOT)
        explicit = collect_node_ids(REPO_ROOT, "tests")

        self.assertEqual(bare, explicit)
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
