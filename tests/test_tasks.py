from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.config import TaskSourceConfig
from vibe_loop.tasks import MarkdownPlanSource, build_task_source, runnable_tasks
from vibe_loop.task_views import build_task_views, render_task_tree


PLAN = """# Plan

### Demo

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| DEMO-01 | P0 | Done | none | Finished base. | Works. | Worklog. |
| DEMO-02 | P1 | Next | DEMO-01 | Ready task. | Works. | Not started. |
| DEMO-03 | P1 | Next | MISSING-01 | Blocked task. | Works. | Not started. |
| DEMO-04 | P0 | Planned | DEMO-01 | Planned task. | Works. | Not started. |
| DEMO-05 | P0 | Gated | DEMO-01 | Gated task. | Works. | Gated. |
"""


class MarkdownPlanTests(unittest.TestCase):
    def test_runnable_tasks_filter_dependencies_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))

            tasks = runnable_tasks(source, ("Active", "Next", "Planned"))

        self.assertEqual([task.task_id for task in tasks], ["DEMO-02", "DEMO-04"])

    def test_plan_tasks_include_section_for_tree_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))
            views = build_task_views(source.list_tasks(), locked_ids=set())

            output = render_task_tree(views)

        self.assertIn("Demo", output)
        self.assertIn("DEMO-02 [Next/P1] Ready task", output)

    def test_default_markdown_source_discovers_nonstandard_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "planning").mkdir()
            (repo / "planning" / "backlog.md").write_text(PLAN, encoding="utf-8")

            source = build_task_source(repo, TaskSourceConfig())

            self.assertEqual(
                source.list_tasks()[0].source, f"{repo}/planning/backlog.md:Demo"
            )

    def test_explicit_plan_path_wins_over_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "custom.md").write_text(
                PLAN.replace("DEMO-02", "CUSTOM-02"),
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(plan_path="custom.md"),
            )

            self.assertIn("CUSTOM-02", [task.task_id for task in source.list_tasks()])

    def test_markdown_discovery_requires_explicit_path_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.md").write_text(PLAN, encoding="utf-8")
            (repo / "b.md").write_text(PLAN, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "multiple markdown plan files"):
                build_task_source(repo, TaskSourceConfig())

    def test_markdown_discovery_uses_candidate_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "notes.md").write_text(PLAN, encoding="utf-8")
            (repo / "roadmap.md").write_text(
                PLAN.replace("DEMO-02", "ROADMAP-02"),
                encoding="utf-8",
            )

            source = build_task_source(repo, TaskSourceConfig())

            self.assertIn("ROADMAP-02", [task.task_id for task in source.list_tasks()])

    def test_markdown_discovery_tolerates_invalid_utf8_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "plan.md").write_bytes(PLAN.encode("utf-8") + b"\xff\n")

            source = build_task_source(repo, TaskSourceConfig())

            self.assertIn("DEMO-02", [task.task_id for task in source.list_tasks()])


if __name__ == "__main__":
    unittest.main()
