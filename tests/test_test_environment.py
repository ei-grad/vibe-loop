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


MUTATION_PROBE = "VIBE_LOOP_TEST_GIT_ISOLATION_MUTATION"


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

    @unittest.skipIf(
        os.environ.get(MUTATION_PROBE) == "1",
        "mutation probe must not invoke itself",
    )
    def test_both_runners_detect_disabled_isolation(self) -> None:
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
            mutated_environment[MUTATION_PROBE] = "1"
            mutated_environment["GIT_CONFIG_GLOBAL"] = str(global_config)
            mutated_environment.pop("GIT_CONFIG_NOSYSTEM", None)
            mutated_environment["PYTHON_COLORS"] = "0"
            mutated_environment["NO_COLOR"] = "1"

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

            methods = (
                "test_git_environment_is_controlled",
                "test_git_configuration_is_isolated",
            )
            runner_commands = {
                "pytest": [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--color=no",
                    "-q",
                    "tests/test_test_environment.py",
                ],
                "unittest discovery": [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_test_environment.py",
                ],
            }
            for runner, command in runner_commands.items():
                result = subprocess.run(
                    command,
                    cwd=Path(__file__).parent.parent,
                    check=False,
                    capture_output=True,
                    env=mutated_environment,
                    text=True,
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, runner)
                for method in methods:
                    self.assertIn(method, output, runner)
                if runner == "pytest":
                    self.assertIn("2 failed, 1 skipped", output)
                else:
                    self.assertIn("FAILED (failures=2, skipped=1)", output)


if __name__ == "__main__":
    unittest.main()
