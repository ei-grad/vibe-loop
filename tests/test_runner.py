from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.locks import LockBusy, LockManager
from vibe_loop.runner import (
    build_selection_prompt,
    parse_selected_task_id,
    run_streaming_command,
)
from vibe_loop.tasks import Task


class RunnerTests(unittest.TestCase):
    def test_selection_prompt_includes_recent_logs(self) -> None:
        task = Task(task_id="LIVE-04", title="Realtime reconcile", status="Next")

        prompt = build_selection_prompt([task], "recent log tail: timeout on WEB-01")

        self.assertIn("LIVE-04", prompt)
        self.assertIn("recent log tail", prompt)
        self.assertIn("blocked or just failed", prompt)

    def test_parse_selected_task_id_from_json_only_or_wrapped_output(self) -> None:
        self.assertEqual(
            parse_selected_task_id('{"task_id":"LIVE-04","reason":"ready"}'),
            "LIVE-04",
        )
        self.assertEqual(
            parse_selected_task_id('text\n{"task_id":"WEB-01"}\nmore'),
            "WEB-01",
        )
        self.assertIsNone(parse_selected_task_id("not json"))

    def test_lock_manager_rejects_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            task_lock = manager.acquire("LIVE-04", "run-1")
            try:
                self.assertTrue(manager.is_locked("LIVE-04"))
                with self.assertRaises(LockBusy):
                    manager.acquire("LIVE-04", "run-2")
            finally:
                manager.release(task_lock)
            self.assertFalse(manager.is_locked("LIVE-04"))

    def test_streaming_command_writes_stdout_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stdout = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stdout(stdout):
                    exit_code = run_streaming_command(
                        'python -c \'import sys; print("out"); print("err", file=sys.stderr)\'',
                        Path(directory),
                        log,
                    )

            self.assertEqual(exit_code, 0)
            self.assertIn("out", stdout.getvalue())
            self.assertIn("err", stdout.getvalue())
            self.assertIn("out", log_path.read_text(encoding="utf-8"))
            self.assertIn("err", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
