from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if __package__:
    from ._test_environment import DISABLE_GIT_ISOLATION, configure_test_environment
else:
    from _test_environment import DISABLE_GIT_ISOLATION, configure_test_environment


# Both unittest discovery and pytest import selected modules before running tests.
configure_test_environment()


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_git_environment_is_controlled(self) -> None:
        self.assertEqual(
            os.environ.get("GIT_CONFIG_GLOBAL"),
            os.devnull,
        )
        self.assertEqual(os.environ.get("GIT_CONFIG_NOSYSTEM"), "1")

    def test_git_configuration_is_isolated(self) -> None:
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

    def test_both_checks_detect_disabled_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            excludes_file = root / "global-ignore"
            template_directory = root / "template"
            excludes_file.touch()
            template_directory.mkdir()
            global_config = root / "gitconfig"
            global_config.write_text(
                "[user]\n"
                "\tname = Host User\n"
                "[core]\n"
                f"\texcludesFile = {excludes_file}\n"
                "[init]\n"
                f"\ttemplateDir = {template_directory}\n"
            )

            mutated_environment = os.environ.copy()
            mutated_environment[DISABLE_GIT_ISOLATION] = "1"
            mutated_environment["GIT_CONFIG_GLOBAL"] = str(global_config)
            mutated_environment.pop("GIT_CONFIG_NOSYSTEM", None)

            expected_configuration = {
                "user.name": "Host User",
                "core.excludesFile": str(excludes_file),
                "init.templateDir": str(template_directory),
            }
            for key, expected in expected_configuration.items():
                result = subprocess.run(
                    ["git", "config", "--global", "--get", key],
                    check=True,
                    capture_output=True,
                    env=mutated_environment,
                    text=True,
                )
                self.assertEqual(result.stdout.strip(), expected, key)

            for method in (
                "test_git_environment_is_controlled",
                "test_git_configuration_is_isolated",
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        f"tests.test_test_environment.TestEnvironmentIsolationTests.{method}",
                    ],
                    cwd=Path(__file__).parent.parent,
                    check=False,
                    capture_output=True,
                    env=mutated_environment,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, method)
                self.assertIn(f"FAIL: {method}", result.stderr)


if __name__ == "__main__":
    unittest.main()
