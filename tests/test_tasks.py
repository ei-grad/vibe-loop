from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.tasks import MarkdownPlanSource, runnable_tasks
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


if __name__ == "__main__":
    unittest.main()
