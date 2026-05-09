from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.config import load_config
from vibe_loop.planning_timeline import build_planning_timeline
from vibe_loop.runs import RunStore, WorkerReport


def plan_table(rows: list[str]) -> str:
    return (
        "# Plan\n\n"
        "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
    )


class PlanningTimelineTests(unittest.TestCase):
    def test_completed_spans_and_projection_use_authoritative_commit_times(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    [
                        "| TASK-01 | P0 | Done | none | Done. | Works. | Trailer. |",
                        "| TASK-02 | P1 | Planned | none | Future. | Works. | Later. |",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "feature.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "feature.txt")
            git_commit(
                repo,
                "TASK-01 first commit",
                "2026-01-01T10:00:00+00:00",
                plan_item="TASK-01",
            )
            (repo / "feature.txt").write_text("two\n", encoding="utf-8")
            git(repo, "add", "feature.txt")
            git_commit(
                repo,
                "TASK-01 second commit",
                "2026-01-01T11:30:00+00:00",
                plan_item="TASK-01",
            )

            timeline = build_planning_timeline(load_config(repo))

        by_id = tasks_by_id(timeline)
        actual = by_id["TASK-01"]["actual"]
        projected = by_id["TASK-02"]["projected"]

        self.assertEqual(actual["start"], "2026-01-01T09:59:00+00:00")
        self.assertEqual(actual["end"], "2026-01-01T11:30:00+00:00")
        self.assertEqual(actual["duration_minutes"], 91)
        self.assertEqual(actual["commit_count"], 2)
        self.assertEqual(actual["mapping_sources"], ["plan_item_trailer"])
        self.assertEqual(projected["start"], "2026-01-01T11:30:00+00:00")
        self.assertEqual(projected["duration_minutes"], 91)
        self.assertEqual(
            projected["estimate"]["model"],
            "completed-actual-median-v1",
        )
        self.assertEqual(timeline["schedule_policy"], "current-runner-parity")

    def test_single_commit_actual_span_clips_idle_gap_from_previous_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    [
                        "| TASK-01 | P0 | Done | none | Done. | Works. | Trailer. |",
                        "| TASK-02 | P0 | Done | none | Done. | Works. | Trailer. |",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "one.txt")
            git_commit(
                repo,
                "TASK-01 work",
                "2026-01-01T08:00:00+00:00",
                plan_item="TASK-01",
            )
            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            git(repo, "add", "two.txt")
            git_commit(
                repo,
                "TASK-02 work after idle gap",
                "2026-01-02T08:00:00+00:00",
                plan_item="TASK-02",
            )

            timeline = build_planning_timeline(load_config(repo))

        actual = tasks_by_id(timeline)["TASK-02"]["actual"]

        self.assertEqual(actual["start"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(actual["end"], "2026-01-02T08:00:00+00:00")
        self.assertEqual(actual["duration_minutes"], 480)
        self.assertEqual(actual["raw_duration_minutes"], 1440)
        self.assertEqual(actual["idle_gap_clipped_minutes"], 960)

    def test_incomplete_commit_mappings_do_not_train_projection_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    [
                        "| DONE | P0 | Done | none | Done. | Works. | Trailer. |",
                        "| WIP | P0 | Planned | DONE | WIP. | Works. | Later. |",
                        "| FUTURE | P0 | Planned | WIP | Future. | Works. | Later. |",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "done.txt").write_text("done\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "done.txt")
            git_commit(
                repo,
                "DONE work",
                "2026-01-01T10:00:00+00:00",
                plan_item="DONE",
            )
            (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
            git(repo, "add", "wip.txt")
            git_commit(repo, "WIP implementation", "2026-01-01T18:00:00+00:00")
            wip_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "worklog.py").write_text(
                "import json\n"
                f"print(json.dumps({{'task_id':'WIP','commit':'{wip_commit}'}}))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
                encoding="utf-8",
            )

            timeline = build_planning_timeline(load_config(repo))

        by_id = tasks_by_id(timeline)
        self.assertIsNone(by_id["WIP"]["actual"])
        self.assertEqual(by_id["WIP"]["projected"]["estimate"]["sample_count"], 1)
        self.assertEqual(by_id["WIP"]["projected"]["estimate"]["minutes"], 1)
        self.assertEqual(by_id["FUTURE"]["projected"]["estimate"]["sample_count"], 1)

    def test_dependency_projection_and_unknown_dependency_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    [
                        "| BASE | P0 | Done | none | Done. | Works. | Trailer. |",
                        "| API | P0 | Planned | BASE | API. | Works. | Later. |",
                        "| UI | P0 | Planned | API | UI. | Works. | Later. |",
                        "| BROKEN | P0 | Planned | MISSING | Broken. | Works. | Later. |",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "base.txt")
            git_commit(
                repo,
                "BASE work",
                "2026-01-01T10:00:00+00:00",
                plan_item="BASE",
            )

            timeline = build_planning_timeline(load_config(repo))

        by_id = tasks_by_id(timeline)
        self.assertEqual(
            by_id["API"]["projected"]["start"], "2026-01-01T10:00:00+00:00"
        )
        self.assertEqual(by_id["UI"]["projected"]["start"], "2026-01-01T10:01:00+00:00")
        self.assertTrue(by_id["BROKEN"]["projected"]["blocked"])
        self.assertEqual(
            by_id["BROKEN"]["projected"]["blockers"],
            ["unknown_dependency:MISSING"],
        )
        self.assertIn(
            ("unknown_dependency", "BROKEN", "MISSING"),
            warning_tuples(timeline),
        )

    def test_projection_policy_changes_ready_task_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    [
                        "| BASE | P0 | Done | none | Done. | Works. | Trailer. |",
                        "| ACTIVE | P2 | Active | BASE | Active. | Works. | Later. |",
                        "| NEXT | P1 | Next | BASE | Next. | Works. | Later. |",
                        "| PLANNED | P0 | Planned | BASE | Planned. | Works. | Later. |",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "base.txt")
            git_commit(
                repo,
                "BASE work",
                "2026-01-01T10:00:00+00:00",
                plan_item="BASE",
            )

            current = build_planning_timeline(load_config(repo))
            (repo / ".vibe-loop.toml").write_text(
                '[planning_analytics]\nschedule_policy = "lightmetrics-parity"\n',
                encoding="utf-8",
            )
            lightmetrics = build_planning_timeline(load_config(repo))

        self.assertEqual(projected_order(current), ["ACTIVE", "NEXT", "PLANNED"])
        self.assertEqual(projected_order(lightmetrics), ["ACTIVE", "PLANNED", "NEXT"])
        self.assertEqual(lightmetrics["schedule_policy"], "lightmetrics-parity")

    def test_stale_run_records_are_reported_without_scheduling_unknown_tasks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(
                    ["| CURRENT | P0 | Planned | none | Now. | Works. | Later. |"]
                ),
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git_commit(repo, "baseline", "2026-01-01T10:00:00+00:00")
            RunStore(repo / ".vibe-loop" / "runs.jsonl").append_report(
                WorkerReport(
                    run_id="run-old",
                    task_id="OLD-01",
                    status="completed",
                )
            )

            timeline = build_planning_timeline(load_config(repo))

        self.assertIn(("stale_run_record", "OLD-01", ""), warning_tuples(timeline))
        self.assertNotIn("OLD-01", tasks_by_id(timeline))

    def test_json_schema_shape_is_stable_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                plan_table(["| BASE | P0 | Done | none | Done. | Works. | Trailer. |"]),
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git_commit(
                repo,
                "BASE work",
                "2026-01-01T10:00:00+00:00",
                plan_item="BASE",
            )

            timeline = build_planning_timeline(load_config(repo))

        task = timeline["tasks"][0]
        self.assertEqual(timeline["schema_version"], 1)
        self.assertEqual(
            list(timeline),
            [
                "schema_version",
                "generated_by",
                "schedule_policy",
                "source_provenance",
                "sections",
                "tasks",
                "warnings",
            ],
        )
        self.assertEqual(
            list(task),
            [
                "id",
                "title",
                "section",
                "status",
                "priority",
                "dependencies",
                "source",
                "actual",
                "projected",
                "timeline_order",
            ],
        )
        self.assertEqual(
            list(task["actual"]),
            [
                "start",
                "end",
                "duration_minutes",
                "raw_duration_minutes",
                "idle_gap_clip_minutes",
                "idle_gap_clipped_minutes",
                "commit_count",
                "commits",
                "mapping_sources",
                "provenance",
            ],
        )


def tasks_by_id(timeline: dict[str, object]) -> dict[str, dict[str, object]]:
    tasks = timeline["tasks"]
    assert isinstance(tasks, list)
    return {str(task["id"]): task for task in tasks}


def projected_order(timeline: dict[str, object]) -> list[str]:
    projected = [
        task
        for task in tasks_by_id(timeline).values()
        if isinstance(task["projected"], dict) and not task["projected"]["blocked"]
    ]
    return [
        str(task["id"])
        for task in sorted(projected, key=lambda task: task["projected"]["sequence"])
    ]


def warning_tuples(timeline: dict[str, object]) -> set[tuple[str, str, str]]:
    warnings = timeline["warnings"]
    assert isinstance(warnings, list)
    return {
        (
            str(warning.get("code", "")),
            str(warning.get("task_id", "")),
            str(warning.get("dependency", "")),
        )
        for warning in warnings
        if isinstance(warning, dict)
    }


def init_git_repo(repo: Path) -> None:
    git(repo, "init")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.com")


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


def git_commit(
    repo: Path,
    subject: str,
    timestamp: str,
    *,
    plan_item: str = "",
) -> None:
    args = ["commit", "-m", subject]
    if plan_item:
        args.extend(["-m", f"Plan-Item: {plan_item}"])
    git(
        repo,
        *args,
        env={
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        },
    )


if __name__ == "__main__":
    unittest.main()
