from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.config import AgentResolutionError, detect_agent_clis, load_config


class ConfigTests(unittest.TestCase):
    def test_detect_agent_clis_reports_supported_binaries_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            write_executable(bin_dir / "codex")

            detected = detect_agent_clis(path=str(bin_dir))

        self.assertEqual(detected.available, ("codex",))
        self.assertTrue(detected.to_json()["codex"]["available"])
        self.assertFalse(detected.to_json()["claude"]["available"])

    def test_codex_only_path_resolves_default_agent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "codex exec '$vibe-loop {task_id}'")
        self.assertEqual(config.agent.command_source, "auto:codex")
        self.assertEqual(config.agent.selection_command, "codex exec {prompt}")
        self.assertEqual(config.agent.selection_command_source, "auto:codex")

    def test_claude_only_path_resolves_default_agent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "claude")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "claude -p '$vibe-loop {task_id}'")
        self.assertEqual(config.agent.command_source, "auto:claude")
        self.assertEqual(config.agent.selection_command, "claude -p {prompt}")
        self.assertEqual(config.agent.selection_command_source, "auto:claude")

    def test_missing_agent_cli_leaves_defaults_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertIsNone(config.agent.command)
        self.assertEqual(config.agent.command_source, "unresolved:no-supported-cli")
        with self.assertRaisesRegex(AgentResolutionError, "install codex or claude"):
            config.agent.require_command()

    def test_both_agent_clis_require_explicit_default_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")
            write_executable(bin_dir / "claude")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertIsNone(config.agent.command)
        self.assertEqual(
            config.agent.command_source,
            "unresolved:multiple-supported-clis",
        )
        with self.assertRaisesRegex(AgentResolutionError, "multiple supported"):
            config.agent.require_command()

    def test_explicit_agent_commands_remain_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                "command = \"custom-worker {task_id}\"\n"
                "selection_command = \"custom-selector {prompt}\"\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "custom-worker {task_id}")
        self.assertEqual(config.agent.command_source, "explicit")
        self.assertEqual(config.agent.selection_command, "custom-selector {prompt}")
        self.assertEqual(config.agent.selection_command_source, "explicit")

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


def write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
