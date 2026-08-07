from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import ast
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_environment import (
    DISABLE_GIT_ISOLATION,
    DISABLE_HOME_ISOLATION,
    TEST_HOME_VARIABLE,
)

from vibe_loop.skill_deployment import (
    MANIFEST_NAME,
    verify_worker_skill_deployments,
    worker_launch_verdict,
)


MUTATION_PROBE = "VIBE_LOOP_TEST_GIT_ISOLATION_MUTATION"
# A worker dispatch that reads the effective home during preflight. The probe
# below runs exactly this test against a drifted operator home.
DISPATCH_PROBE_MODULE = "test_runner.py"
DISPATCH_PROBE_CASE = (
    "AgentCommandModelTests::test_run_task_model_field_without_resolved_model"
    "_does_not_launch"
)


def record_deployment(
    home: Path,
    *,
    installed_text: str,
    recorded_text: str,
    source: Path,
) -> None:
    """Materialise a recorded `vibe-loop` deployment under `home`.

    `installed_text` differing from `recorded_text` yields `runtime-edited`;
    a `source` whose content differs from `recorded_text` yields `stale`.
    """
    digest = hashlib.sha256(recorded_text.encode("utf-8")).hexdigest()
    for runtime in (".codex", ".claude"):
        root = home / runtime / "skills"
        (root / "vibe-loop").mkdir(parents=True, exist_ok=True)
        (root / "vibe-loop" / "SKILL.md").write_text(
            installed_text,
            encoding="utf-8",
        )
        (root / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "installed_at": "2026-08-07T00:00:00+00:00",
                    "entries": {
                        "vibe-loop/SKILL.md": {
                            "installed_at": "2026-08-07T00:00:00+00:00",
                            "sha256": digest,
                            "source_branch": "main",
                            "source_commit": "0" * 40,
                            "source_dirty": False,
                            "source_location": str(source.parent),
                            "source_main_branch": "main",
                            "source_path": source.name,
                            "source_repo": str(source.parent),
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_every_test_module_bootstraps_before_other_imports(self) -> None:
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            module = ast.parse(path.read_text(), filename=str(path))
            first_runtime_statement = next(
                statement
                for statement in module.body
                if not (
                    isinstance(statement, ast.ImportFrom)
                    and statement.module == "__future__"
                )
            )
            self.assertIsInstance(first_runtime_statement, ast.ImportFrom, path.name)
            self.assertEqual(
                first_runtime_statement.module, "_test_bootstrap", path.name
            )

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

    def test_home_environment_is_isolated(self) -> None:
        home = Path.home()

        self.assertEqual(str(home), os.environ.get("HOME"))
        self.assertEqual(str(home), os.environ.get(TEST_HOME_VARIABLE))
        self.assertNotEqual(home, Path(pwd.getpwuid(os.getuid()).pw_dir))
        for name in ("CLAUDE_HOME", "CODEX_HOME"):
            self.assertNotIn(name, os.environ)
        # Agent processes the suite launches write into this home, so assert the
        # property that matters instead of an empty tree: it carries no recorded
        # deployment for preflight to judge.
        for runtime in (".codex", ".claude"):
            self.assertFalse((home / runtime / "skills" / MANIFEST_NAME).exists())
        self.assertEqual(verify_worker_skill_deployments(home), ())

    def test_worker_preflight_reads_the_effective_home(self) -> None:
        # Isolation supplies a different home; it must not disable the check.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "SKILL.md"
            source.parent.mkdir()
            source.write_text("source moved on\n", encoding="utf-8")

            stale_home = root / "stale-home"
            record_deployment(
                stale_home,
                installed_text="recorded\n",
                recorded_text="recorded\n",
                source=source,
            )
            with mock.patch.dict(os.environ, {"HOME": str(stale_home)}):
                reports = verify_worker_skill_deployments(Path.home())
            blocking, advisories = worker_launch_verdict(reports)
            self.assertEqual(
                {entry.state for report in reports for entry in report.entries},
                {"stale"},
            )
            self.assertEqual(blocking, ())
            self.assertEqual(len(advisories), 2)

            edited_home = root / "edited-home"
            record_deployment(
                edited_home,
                installed_text="edited in place\n",
                recorded_text="recorded\n",
                source=source,
            )
            with mock.patch.dict(os.environ, {"HOME": str(edited_home)}):
                edited_reports = verify_worker_skill_deployments(Path.home())
            edited_blocking, _ = worker_launch_verdict(edited_reports)
            self.assertEqual(
                {entry.state for report in edited_reports for entry in report.entries},
                {"runtime-edited"},
            )
            self.assertEqual(len(edited_blocking), 2)

    @unittest.skipIf(
        os.environ.get(MUTATION_PROBE) == "1",
        "a nested probe run re-verifies git isolation only",
    )
    def test_both_runners_ignore_a_drifted_operator_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator_home = root / "operator-home"
            record_deployment(
                operator_home,
                installed_text="edited in place\n",
                recorded_text="recorded\n",
                source=root / "absent-source" / "SKILL.md",
            )

            probe_environment = os.environ.copy()
            probe_environment["HOME"] = str(operator_home)
            probe_environment.pop(TEST_HOME_VARIABLE, None)
            probe_environment["PYTHON_COLORS"] = "0"
            probe_environment["NO_COLOR"] = "1"

            runner_commands = {
                "pytest": [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--color=no",
                    "-q",
                    f"tests/{DISPATCH_PROBE_MODULE}::{DISPATCH_PROBE_CASE}",
                ],
                "unittest discovery": [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    DISPATCH_PROBE_MODULE,
                    "-k",
                    DISPATCH_PROBE_CASE.rsplit("::", 1)[-1],
                ],
            }
            # A renamed probe case must fail this test rather than silently
            # measure nothing, so the isolated run also asserts it ran.
            selected_case = {"pytest": "1 passed", "unittest discovery": "Ran 1 test"}

            for runner, command in runner_commands.items():
                isolated = subprocess.run(
                    command,
                    cwd=Path(__file__).parent.parent,
                    check=False,
                    capture_output=True,
                    env=probe_environment,
                    text=True,
                )
                isolated_output = isolated.stdout + isolated.stderr
                self.assertEqual(isolated.returncode, 0, isolated_output)
                self.assertIn(selected_case[runner], isolated_output, runner)

                leaked = subprocess.run(
                    command,
                    cwd=Path(__file__).parent.parent,
                    check=False,
                    capture_output=True,
                    env={**probe_environment, DISABLE_HOME_ISOLATION: "1"},
                    text=True,
                )
                leaked_output = leaked.stdout + leaked.stderr
                self.assertNotEqual(leaked.returncode, 0, runner)
                self.assertIn("SkillDeploymentError", leaked_output, runner)

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


if __name__ == "__main__":
    unittest.main()
