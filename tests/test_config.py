from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.config import load_config


class ConfigTests(unittest.TestCase):
    def test_agent_forward_stderr_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertFalse(config.agent.forward_stderr)

    def test_agent_forward_stderr_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\nforward_stderr = true\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.agent.forward_stderr)

    def test_agent_forward_stderr_rejects_non_bool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nforward_stderr = "yes"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "agent.forward_stderr"):
                load_config(repo)

    def test_task_source_plan_paths_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_paths = ["WORK.md", "docs/BACKLOG.md"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.task_source.plan_paths, ("WORK.md", "docs/BACKLOG.md"))

    def test_task_source_plan_paths_rejects_non_string_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\nplan_paths = [123]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "task_source.plan_paths"):
                load_config(repo)


if __name__ == "__main__":
    unittest.main()
