from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class GitHookTests(unittest.TestCase):
    def test_installed_prepare_commit_msg_adds_worker_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test User"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "user.email",
                    "test@example.com",
                ],
                check=True,
            )
            shutil.copytree(REPO_ROOT / "scripts", repo / "scripts")

            subprocess.run(
                ["make", "-f", str(REPO_ROOT / "Makefile"), "install-hooks"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            tracked = repo / "tracked.txt"
            tracked.write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
            environment = os.environ.copy()
            environment.update(
                {
                    "VIBE_LOOP_TASK_ID": "TASK-42",
                    "VIBE_LOOP_RUN_ID": "run-7",
                    "VIBE_LOOP_AGENT_KIND": "codex",
                }
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "commit",
                    "--no-verify",
                    "-m",
                    "Implement task",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            message = subprocess.run(
                ["git", "-C", str(repo), "show", "-s", "--format=%B", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertIn("Plan-Item: TASK-42", message)
        self.assertIn("Run-Id: run-7", message)
        self.assertIn("Agent-Kind: codex", message)


if __name__ == "__main__":
    unittest.main()
