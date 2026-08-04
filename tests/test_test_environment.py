from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_git_environment_and_configuration_are_isolated(self) -> None:
        git_environment = {
            name: value for name, value in os.environ.items() if name.startswith("GIT_")
        }
        self.assertEqual(
            git_environment,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            for key in ("user.name", "core.excludesFile", "init.templateDir"):
                result = subprocess.run(
                    ["git", "config", "--get", key],
                    cwd=repo,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1, key)
                self.assertEqual(result.stdout, "", key)


if __name__ == "__main__":
    unittest.main()
