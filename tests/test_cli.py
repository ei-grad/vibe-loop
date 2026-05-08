from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main


PLAN = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Test task. | Run agent. | Not run. |
"""


class CliTests(unittest.TestCase):
    def test_run_next_keeps_json_stdout_when_agent_streams_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                'import sys\nprint("agent out")\nprint("agent err", file=sys.stderr)\n',
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\ncommand = "python agent.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["classification"], "unknown")
        self.assertIn("agent out", stderr.getvalue())
        self.assertNotIn("agent err", stderr.getvalue())
        self.assertIn("[vibe-loop] running TASK-01", stderr.getvalue())
        self.assertIn("agent out", log_text)
        self.assertIn("agent err", log_text)

    def test_run_next_empty_queue_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(
                PLAN.replace("| TASK-01 | P0 | Next |", "| TASK-01 | P0 | Done |"),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("no runnable tasks", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
