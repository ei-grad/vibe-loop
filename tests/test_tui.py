from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_loop.autopilot import (
    AggregateProjectStatus,
    collect_project_status,
)
from vibe_loop.config import load_config

# textual is an optional extra (vibe-loop[tui]); these tests skip cleanly when
# it is not installed, e.g. the packaged-wheel CI job that omits dev deps.
try:
    from textual.widgets import DataTable

    from vibe_loop.tui import (
        TUI_COLUMNS,
        AutopilotTUIApp,
        project_status_rows,
    )

    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual (vibe-loop[tui]) not installed")
class ProjectStatusRowsTests(unittest.TestCase):
    def test_maps_status_and_isolated_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, [("TASK-01", "Next", "ready slice")])
            commit_all(repo)
            status = collect_project_status(load_config(repo))

        results = [
            AggregateProjectStatus(name="good", repo=repo, status=status),
            AggregateProjectStatus(
                name="broken", repo=Path("/repos/broken"), error="boom"
            ),
        ]
        rows = project_status_rows(results)

        self.assertEqual(rows[0]["project"], "good")
        self.assertEqual(rows[0]["queue"], "1/1 ready")
        self.assertEqual(rows[0]["workers"], "0")
        self.assertEqual(rows[1]["project"], "broken")
        self.assertEqual(rows[1]["blockers"], "error: boom")
        # No raw command strings or secrets leak into the rendered rows.
        joined = " ".join(value for row in rows for value in row.values())
        self.assertNotIn("{prompt}", joined)


@unittest.skipUnless(HAS_TEXTUAL, "textual (vibe-loop[tui]) not installed")
class AutopilotTUIRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_renders_one_row_per_project(self) -> None:
        rows = [
            {
                "project": "alpha",
                "repo": "/repos/alpha",
                "queue": "2/5 ready",
                "workers": "1",
                "supervisor": "idle",
                "blockers": "none",
                "next_wake": "—",
            },
            {
                "project": "beta",
                "repo": "/repos/beta",
                "queue": "unavailable",
                "workers": "0",
                "supervisor": "running pid=4242",
                "blockers": "repo_dirty",
                "next_wake": "2026-06-06T00:00:00+00:00",
            },
        ]
        app = AutopilotTUIApp(rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#projects", DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertEqual(len(table.columns), len(TUI_COLUMNS))
            first_cell = table.get_cell_at((0, 0))
            self.assertEqual(first_cell, "alpha")


def init_repo(repo: Path) -> None:
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.com")
    run(repo, "git", "config", "user.name", "Test User")


def write_plan(repo: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = [
        "# Plan",
        "",
        "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task_id, status, scope in rows:
        lines.append(f"| {task_id} | P0 | {status} | none | {scope} | works | tests |")
    (repo / "PLAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def commit_all(repo: Path) -> None:
    run(repo, "git", "add", "PLAN.md")
    run(repo, "git", "commit", "-m", "initial")


def run(repo: Path, *args: str) -> None:
    subprocess.run(
        args,
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


if __name__ == "__main__":
    unittest.main()
