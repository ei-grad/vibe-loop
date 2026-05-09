from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main
from vibe_loop.planning_benchmark import (
    BenchmarkExample,
    BenchmarkFold,
    fold_training_actuals,
)
from vibe_loop.planning_timeline import ActualSpan


class PlanningBenchmarkCliTests(unittest.TestCase):
    def test_benchmark_duration_writes_deterministic_reports_and_check_passes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_benchmark_repo(repo)
            rows = benchmark_rows()
            write_plan(repo, rows)
            commit_benchmark_history(repo)

            first_exit = run_cli("planning", "benchmark-duration", "--repo", str(repo))
            json_path = (
                repo / ".vibe-loop" / "planning-analytics" / ("duration-benchmark.json")
            )
            markdown_path = (
                repo / ".vibe-loop" / "planning-analytics" / ("duration-benchmark.md")
            )
            first_json = json_path.read_text(encoding="utf-8")
            first_markdown = markdown_path.read_text(encoding="utf-8")
            first_report = json.loads(first_json)
            check_exit = run_cli(
                "planning",
                "benchmark-duration",
                "--repo",
                str(repo),
                "--check",
            )

            write_plan(repo, list(reversed(rows)))
            second_exit = run_cli("planning", "benchmark-duration", "--repo", str(repo))
            second_json = json_path.read_text(encoding="utf-8")
            second_markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(first_exit, 0)
        self.assertEqual(check_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertEqual(first_report["schema_version"], 1)
        self.assertTrue(first_report["selected_estimator"]["matches_generator_config"])
        selected_metrics = first_report["selected_estimator"]["metrics"]
        self.assertIn("mae_minutes", selected_metrics)
        self.assertIn("mape", selected_metrics)
        self.assertIn("mean_log_error", selected_metrics)
        self.assertIn("coverage", selected_metrics)
        self.assertIn("bias_minutes", selected_metrics)
        self.assertIn("## Worst Misses", first_markdown)

    def test_benchmark_duration_folds_exclude_validation_tasks_and_shared_commits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_benchmark_repo(repo)
            rows = [
                *benchmark_rows(),
                (
                    "| SHARE-A | P0 | Done | none | shared alpha task. | Works. | "
                    "Trailer. |"
                ),
                (
                    "| SHARE-B | P0 | Done | none | shared beta task. | Works. | "
                    "Trailer. |"
                ),
            ]
            write_plan(repo, rows)
            commit_benchmark_history(repo)
            commit_file(
                repo,
                "shared.txt",
                "shared\n",
                "shared duration work",
                "2026-01-01T12:00:00+00:00",
                plan_items=(),
            )
            shared_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            write_plan(
                repo,
                [
                    *benchmark_rows(),
                    (
                        "| SHARE-A | P0 | Done | none | shared alpha task. | Works. | "
                        f"commit: {shared_commit}. |"
                    ),
                    (
                        "| SHARE-B | P0 | Done | none | shared beta task. | Works. | "
                        f"commit: {shared_commit}. |"
                    ),
                ],
            )

            exit_code = run_cli("planning", "benchmark-duration", "--repo", str(repo))
            report = json.loads(
                (
                    repo
                    / ".vibe-loop"
                    / "planning-analytics"
                    / "duration-benchmark.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(
            report["exclusion_checks"]["training_excludes_validation_tasks"]
        )
        self.assertTrue(
            report["exclusion_checks"]["training_excludes_shared_validation_commits"]
        )
        shared_folds = [
            fold
            for fold in report["folds"]
            if {"SHARE-A", "SHARE-B"} <= set(fold["validation_task_ids"])
        ]
        self.assertEqual(len(shared_folds), 1)
        shared_commit = set(shared_folds[0]["validation_commits"])
        for fold in report["folds"]:
            if shared_commit & set(fold["validation_commits"]):
                self.assertNotIn("SHARE-A", fold["training_task_ids"])
                self.assertNotIn("SHARE-B", fold["training_task_ids"])

    def test_benchmark_duration_check_fails_on_model_selection_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_benchmark_repo(repo)
            write_plan(repo, benchmark_rows())
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics.duration_model]\n"
                "similarity_blend_weight = 0.0\n"
                "similarity_max_examples = 0\n",
                encoding="utf-8",
            )
            commit_benchmark_history(repo)

            generate_exit = run_cli(
                "planning", "benchmark-duration", "--repo", str(repo)
            )
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                check_exit = main(
                    [
                        "planning",
                        "benchmark-duration",
                        "--repo",
                        str(repo),
                        "--check",
                    ]
                )

        self.assertEqual(generate_exit, 0)
        self.assertEqual(check_exit, 1)
        self.assertIn(
            "configured duration model does not match benchmark-selected estimator",
            stderr.getvalue(),
        )

    def test_benchmark_duration_check_fails_for_stale_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_benchmark_repo(repo)
            write_plan(repo, benchmark_rows())
            commit_benchmark_history(repo)
            json_path = (
                repo / ".vibe-loop" / "planning-analytics" / ("duration-benchmark.json")
            )

            generate_exit = run_cli(
                "planning", "benchmark-duration", "--repo", str(repo)
            )
            json_path.write_text("{}\n", encoding="utf-8")
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                check_exit = main(
                    [
                        "planning",
                        "benchmark-duration",
                        "--repo",
                        str(repo),
                        "--check",
                    ]
                )

        self.assertEqual(generate_exit, 0)
        self.assertEqual(check_exit, 1)
        self.assertIn("JSON benchmark report is stale", stderr.getvalue())

    def test_fold_training_actuals_exclude_validation_commit_timing(self) -> None:
        validation = benchmark_example(
            "VALIDATION",
            "aaaaaaaa",
            "2026-01-01T10:00:00+00:00",
            duration_minutes=1,
        )
        training = benchmark_example(
            "TRAINING",
            "bbbbbbbb",
            "2026-01-01T10:10:00+00:00",
            duration_minutes=10,
        )
        fold = BenchmarkFold(
            fold_id="fold-1",
            validation_task_ids=("VALIDATION",),
            validation_commits=("aaaaaaaa",),
            training_task_ids=("TRAINING",),
            excluded_shared_commit_task_ids=(),
            leakage_free=True,
        )

        actuals = fold_training_actuals(
            fold,
            {
                "VALIDATION": validation,
                "TRAINING": training,
            },
        )

        self.assertEqual(training.actual.duration_minutes, 10)
        self.assertEqual(actuals["TRAINING"].duration_minutes, 1)


def run_cli(*args: str) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(list(args))


def benchmark_rows() -> list[str]:
    return [
        "| WARMUP | P0 | Done | none | neutral warmup. | Works. | Trailer. |",
        "| ALPHA-1 | P0 | Done | none | alpha calendar grid. | Works. | Trailer. |",
        "| BETA-1 | P0 | Done | none | beta database migration. | Works. | Trailer. |",
        "| ALPHA-2 | P0 | Done | none | alpha calendar grid. | Works. | Trailer. |",
        "| BETA-2 | P0 | Done | none | beta database migration. | Works. | Trailer. |",
        "| ALPHA-3 | P0 | Done | none | alpha calendar grid. | Works. | Trailer. |",
        "| BETA-3 | P0 | Done | none | beta database migration. | Works. | Trailer. |",
    ]


def benchmark_example(
    task_id: str,
    commit_hash: str,
    author_time: str,
    *,
    duration_minutes: int,
) -> BenchmarkExample:
    end = datetime.fromisoformat(author_time)
    return BenchmarkExample(
        task_id=task_id,
        task={
            "id": task_id,
            "title": task_id,
            "section": "default",
            "priority": "P0",
            "scope": task_id,
            "acceptance": "Works.",
        },
        actual=ActualSpan(
            task_id=task_id,
            start=end - timedelta(minutes=duration_minutes),
            end=end,
            duration_minutes=duration_minutes,
            raw_duration_minutes=duration_minutes,
            idle_gap_clipped_minutes=0,
            commits=(
                {
                    "commit": commit_hash,
                    "author_time": author_time,
                    "sources": ["test"],
                },
            ),
            mapping_sources=("test",),
        ),
        commits=frozenset({commit_hash}),
    )


def write_plan(repo: Path, rows: list[str]) -> None:
    repo.joinpath("PLAN.md").write_text(
        "# Plan\n\n"
        "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def init_benchmark_repo(repo: Path) -> None:
    git(repo, "init")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.com")


def commit_benchmark_history(repo: Path) -> None:
    git(repo, "add", "PLAN.md")
    commit(
        repo,
        "plan baseline",
        "2026-01-01T00:00:00+00:00",
        plan_items=("WARMUP",),
    )
    commit_file(
        repo,
        "alpha-1.txt",
        "alpha 1\n",
        "alpha first",
        "2026-01-01T00:20:00+00:00",
        plan_items=("ALPHA-1",),
    )
    commit_file(
        repo,
        "beta-1.txt",
        "beta 1\n",
        "beta first",
        "2026-01-01T03:40:00+00:00",
        plan_items=("BETA-1",),
    )
    commit_file(
        repo,
        "alpha-2.txt",
        "alpha 2\n",
        "alpha second",
        "2026-01-01T04:02:00+00:00",
        plan_items=("ALPHA-2",),
    )
    commit_file(
        repo,
        "beta-2.txt",
        "beta 2\n",
        "beta second",
        "2026-01-01T07:32:00+00:00",
        plan_items=("BETA-2",),
    )
    commit_file(
        repo,
        "alpha-3.txt",
        "alpha 3\n",
        "alpha third",
        "2026-01-01T07:53:00+00:00",
        plan_items=("ALPHA-3",),
    )
    commit_file(
        repo,
        "beta-3.txt",
        "beta 3\n",
        "beta third",
        "2026-01-01T11:23:00+00:00",
        plan_items=("BETA-3",),
    )


def commit_file(
    repo: Path,
    relative_path: str,
    content: str,
    subject: str,
    timestamp: str,
    *,
    plan_items: tuple[str, ...],
) -> None:
    repo.joinpath(relative_path).write_text(content, encoding="utf-8")
    git(repo, "add", relative_path)
    commit(repo, subject, timestamp, plan_items=plan_items)


def commit(
    repo: Path,
    subject: str,
    timestamp: str,
    *,
    plan_items: tuple[str, ...] = (),
) -> None:
    args = ["commit", "-m", subject]
    for item in plan_items:
        args.extend(["-m", f"Plan-Item: {item}"])
    git(
        repo,
        *args,
        env={
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )


def git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=process_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
