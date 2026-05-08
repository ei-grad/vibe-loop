from __future__ import annotations

import json
import os
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


TWO_TASK_PLAN = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | First test task. | Run agent. | Not run. |
| TASK-02 | P0 | Next | none | Second test task. | Run agent. | Not run. |
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

    def test_run_next_supports_configured_claude_prompt_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            bin_dir = repo / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                "if sys.argv[1] != '-p':\n"
                "    raise SystemExit(64)\n"
                "prompt = sys.argv[2]\n"
                "print(f'claude out: {prompt}')\n"
                "print(f'claude err: {prompt}', file=sys.stderr)\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = \"claude -p '$vibe-loop {task_id}'\"\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])
            finally:
                os.environ["PATH"] = original_path

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["classification"], "completed")
        self.assertIn("claude out: $vibe-loop TASK-01", stderr.getvalue())
        self.assertNotIn("claude err", stderr.getvalue())
        self.assertIn("claude out: $vibe-loop TASK-01", log_text)
        self.assertIn("claude err: $vibe-loop TASK-01", log_text)
        self.assertEqual(run_records[0]["task_id"], "TASK-01")
        self.assertEqual(run_records[0]["status"], "completed")

    def test_next_supports_configured_claude_prompt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            bin_dir = repo / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != '-p':\n"
                "    raise SystemExit(64)\n"
                "Path('selection-prompt.txt').write_text(sys.argv[2], encoding='utf-8')\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nselection_command = "claude -p {prompt}"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["next", "--repo", str(repo), "--ask-agent"])
            finally:
                os.environ["PATH"] = original_path

            prompt = (repo / "selection-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue().strip(), "TASK-02")
        self.assertIn("TASK-01", prompt)
        self.assertIn("TASK-02", prompt)
        self.assertIn("Return JSON only", prompt)
        self.assertIn("agent selected TASK-02", stderr.getvalue())

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

    def test_tasks_locks_does_not_require_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["tasks", "locks", "--repo", str(repo)])

        self.assertEqual(exit_code, 0)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
