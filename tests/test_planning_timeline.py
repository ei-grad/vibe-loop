from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vibe_loop.config import load_config
from vibe_loop.planning_timeline import (
    ActualSpan,
    DurationBaselineModel,
    build_planning_timeline,
)
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
            "robust-duration-baseline-v1/global",
        )
        self.assertEqual(projected["estimate"]["low_minutes"], 45)
        self.assertEqual(projected["estimate"]["high_minutes"], 182)
        self.assertEqual(
            projected["estimate"]["interval"]["coverage"],
            "conservative_small_history",
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

    def test_duration_model_uses_fixed_fallback_without_completed_history(
        self,
    ) -> None:
        target = task_payload(
            "TARGET",
            status="Planned",
            section="API",
            priority="P1",
            scope="Build projected API endpoint.",
        )
        estimate = DurationBaselineModel([target], {}).estimate(target).to_json()

        self.assertEqual(estimate["minutes"], 60)
        self.assertEqual(estimate["low_minutes"], 30)
        self.assertEqual(estimate["high_minutes"], 120)
        self.assertEqual(estimate["model"], "fixed-fallback-v1")
        self.assertEqual(estimate["sample_count"], 0)
        self.assertEqual(
            estimate["interval"]["coverage"],
            "conservative_small_history",
        )
        self.assertEqual(estimate["training_sample_counts"]["global"], 0)

    def test_duration_model_falls_back_to_workstream_and_priority_history(
        self,
    ) -> None:
        api_a = task_payload("API-A", section="API", priority="P1", scope="api cache")
        api_b = task_payload("API-B", section="API", priority="P2", scope="api auth")
        cli_a = task_payload("CLI-A", section="CLI", priority="P1", scope="cli output")
        infra_a = task_payload(
            "INFRA-A",
            section="Infra",
            priority="P1",
            scope="infra deploy",
        )
        api_future = task_payload(
            "API-FUTURE",
            status="Planned",
            section="API",
            priority="P9",
            scope="api future",
        )
        docs_future = task_payload(
            "DOCS-FUTURE",
            status="Planned",
            section="Docs",
            priority="P1",
            scope="docs future",
        )
        model = DurationBaselineModel(
            [api_a, api_b, cli_a, infra_a, api_future, docs_future],
            {
                "API-A": actual_span("API-A", 30),
                "API-B": actual_span("API-B", 60),
                "CLI-A": actual_span("CLI-A", 120),
                "INFRA-A": actual_span("INFRA-A", 180),
            },
        )

        workstream = model.estimate(api_future).to_json()
        priority = model.estimate(docs_future).to_json()

        self.assertEqual(
            workstream["model"],
            "robust-duration-baseline-v1/workstream",
        )
        self.assertEqual(workstream["minutes"], 45)
        self.assertEqual(workstream["sample_count"], 2)
        self.assertEqual(workstream["training_sample_counts"]["workstream"], 2)
        self.assertEqual(
            priority["model"],
            "robust-duration-baseline-v1/priority",
        )
        self.assertEqual(priority["minutes"], 120)
        self.assertEqual(priority["sample_count"], 3)
        self.assertEqual(priority["training_sample_counts"]["priority"], 3)

    def test_duration_model_clamps_log_space_outliers_for_bounds(self) -> None:
        tasks = [
            task_payload(f"DONE-{index}", scope=f"history {index}")
            for index in range(1, 6)
        ]
        target = task_payload("TARGET", status="Planned", scope="future task")
        actuals = {
            "DONE-1": actual_span("DONE-1", 60),
            "DONE-2": actual_span("DONE-2", 60),
            "DONE-3": actual_span("DONE-3", 60),
            "DONE-4": actual_span("DONE-4", 60),
            "DONE-5": actual_span("DONE-5", 960),
        }
        estimate = (
            DurationBaselineModel([*tasks, target], actuals).estimate(target).to_json()
        )

        self.assertEqual(estimate["minutes"], 60)
        self.assertEqual(estimate["outlier_handling"]["clipped_sample_count"], 1)
        self.assertEqual(estimate["outlier_handling"]["upper_minutes"], 240)
        self.assertLessEqual(estimate["high_minutes"], 240)
        self.assertEqual(
            estimate["interval"]["coverage"],
            "conservative_80_percent",
        )

    def test_similarity_blend_uses_pre_task_tokens_without_evidence_leakage(
        self,
    ) -> None:
        short = task_payload(
            "SHORT",
            title="Calendar grid",
            scope="render calendar grid",
            acceptance="snap grid",
            evidence="unrelated",
        )
        long = task_payload(
            "LONG",
            title="Migration",
            scope="database migration",
            acceptance="migrate tables",
            evidence="render calendar grid",
        )
        target = task_payload(
            "TARGET",
            status="Planned",
            title="Calendar grid",
            scope="render calendar grid",
            acceptance="snap grid",
        )
        estimate = (
            DurationBaselineModel(
                [short, long, target],
                {
                    "SHORT": actual_span("SHORT", 40),
                    "LONG": actual_span("LONG", 240),
                },
            )
            .estimate(target)
            .to_json()
        )

        self.assertEqual(
            estimate["model"],
            "robust-duration-baseline-v1/workstream-priority+similarity",
        )
        self.assertEqual(estimate["similarity_examples"][0]["task_id"], "SHORT")
        self.assertNotIn(
            "LONG",
            [item["task_id"] for item in estimate["similarity_examples"]],
        )
        self.assertEqual(
            estimate["features"]["token_fields"],
            ["title", "scope", "acceptance"],
        )

    def test_duration_model_estimates_are_deterministic(self) -> None:
        a = task_payload("A", section="API", priority="P0", scope="api cache")
        b = task_payload("B", section="API", priority="P0", scope="api auth")
        c = task_payload("C", section="CLI", priority="P2", scope="cli output")
        target = task_payload(
            "TARGET",
            status="Planned",
            section="API",
            priority="P0",
            scope="api future",
        )
        actuals = {
            "A": actual_span("A", 50),
            "B": actual_span("B", 70),
            "C": actual_span("C", 200),
        }

        first = DurationBaselineModel([a, b, c, target], actuals).estimate(target)
        second = DurationBaselineModel([target, c, b, a], actuals).estimate(target)

        self.assertEqual(first.to_json(), second.to_json())

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


def task_payload(
    task_id: str,
    *,
    status: str = "Done",
    section: str = "default",
    priority: str = "P0",
    title: str = "",
    scope: str = "",
    acceptance: str = "Works.",
    evidence: str = "",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": title or scope or task_id,
        "section": section,
        "status": status,
        "priority": priority,
        "dependencies": [],
        "resources": [],
        "paths": [],
        "conflict_domains_known": False,
        "scope": scope,
        "acceptance": acceptance,
        "evidence": evidence,
        "source": "PLAN.md:Test",
        "order": 0,
    }


def actual_span(task_id: str, duration_minutes: int) -> ActualSpan:
    end = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=duration_minutes
    )
    return ActualSpan(
        task_id=task_id,
        start=end - timedelta(minutes=duration_minutes),
        end=end,
        duration_minutes=duration_minutes,
        raw_duration_minutes=duration_minutes,
        idle_gap_clipped_minutes=0,
        commits=(),
        mapping_sources=("test",),
    )


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
