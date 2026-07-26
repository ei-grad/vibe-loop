from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_ENV_NAMES = (
    "VIBE_LOOP_TASK_ID",
    "VIBE_LOOP_RUN_ID",
    "VIBE_LOOP_AGENT_KIND",
)


class GitHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "test@example.com",
            ],
            check=True,
        )
        shutil.copytree(REPO_ROOT / "scripts", self.repo / "scripts")

    @staticmethod
    def provenance_environment(**values: str) -> dict[str, str]:
        environment = os.environ.copy()
        for name in PROVENANCE_ENV_NAMES:
            environment.pop(name, None)
        environment.update(values)
        return environment

    def install_hooks(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["make", "-f", str(REPO_ROOT / "Makefile"), "install-hooks"],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
        )

    def commit(
        self,
        message: str,
        *,
        environment: dict[str, str],
        filename: str = "tracked.txt",
    ) -> subprocess.CompletedProcess[str]:
        tracked = self.repo / filename
        tracked.write_text(f"{filename}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", filename],
            check=True,
        )
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "commit",
                "-m",
                message,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def head_message(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "show", "-s", "--format=%B", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def head_trailers(self, key: str) -> list[str]:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "log",
                "-1",
                f"--format=%(trailers:key={key},valueonly)",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [line for line in output.splitlines() if line]

    def test_installed_commit_msg_adds_worker_provenance(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)
        result = self.commit(
            "Implement task",
            environment=self.provenance_environment(
                VIBE_LOOP_TASK_ID="TASK-42",
                VIBE_LOOP_RUN_ID="run-7",
                VIBE_LOOP_AGENT_KIND="codex",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.head_message()
        self.assertIn("Plan-Item: TASK-42", message)
        self.assertIn("Run-Id: run-7", message)
        self.assertIn("Agent-Kind: codex", message)
        self.assertEqual(self.head_trailers("Plan-Item"), ["TASK-42"])

    def test_commit_without_task_context_is_unchanged(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)

        result = self.commit(
            "Ordinary commit",
            environment=self.provenance_environment(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.head_message()
        self.assertNotIn("Plan-Item:", message)
        self.assertNotIn("Run-Id:", message)
        self.assertNotIn("Agent-Kind:", message)

    def test_optional_context_can_be_absent(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)

        result = self.commit(
            "Task commit",
            environment=self.provenance_environment(VIBE_LOOP_TASK_ID="TASK-42"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.head_message()
        self.assertIn("Plan-Item: TASK-42", message)
        self.assertNotIn("Run-Id:", message)
        self.assertNotIn("Agent-Kind:", message)

    def test_existing_plan_item_is_not_duplicated(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)

        result = self.commit(
            "Manual provenance\n\nPlan-Item: MANUAL-1",
            environment=self.provenance_environment(
                VIBE_LOOP_TASK_ID="TASK-42",
                VIBE_LOOP_RUN_ID="run-7",
                VIBE_LOOP_AGENT_KIND="codex",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.head_message()
        self.assertEqual(message.count("Plan-Item:"), 1)
        self.assertIn("Plan-Item: MANUAL-1", message)
        self.assertNotIn("Run-Id:", message)
        self.assertNotIn("Agent-Kind:", message)
        self.assertEqual(self.head_trailers("Plan-Item"), ["MANUAL-1"])

    def test_split_plan_item_paragraph_is_repaired_as_parsed_trailer(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)

        result = self.commit(
            "Implement task\n\n"
            "Plan-Item: STALE-TEXT\n\n"
            "Co-Authored-By: Codex CLI <noreply@openai.com>",
            environment=self.provenance_environment(
                VIBE_LOOP_TASK_ID="TASK-42",
                VIBE_LOOP_RUN_ID="run-7",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head_message().count("Plan-Item:"), 2)
        self.assertEqual(self.head_trailers("Plan-Item"), ["TASK-42"])

    def test_merge_message_is_unchanged(self) -> None:
        hook = self.repo / "scripts/hooks/commit-msg"
        merge_head = self.repo / ".git/MERGE_HEAD"
        merge_head.write_text("0" * 40 + "\n", encoding="utf-8")
        environment = self.provenance_environment(VIBE_LOOP_TASK_ID="TASK-42")
        message_file = self.repo / "merge.message"
        message_file.write_text("Existing message\n", encoding="utf-8")

        result = subprocess.run(
            [str(hook), str(message_file)],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            message_file.read_text(encoding="utf-8"),
            "Existing message\n",
        )

    def test_hook_failure_does_not_block_commit_message(self) -> None:
        hook = self.repo / "scripts/hooks/commit-msg"
        message_file = self.repo / "message"
        message_file.write_text("Existing message\n", encoding="utf-8")
        environment = self.provenance_environment(VIBE_LOOP_TASK_ID="TASK-42")
        environment["PATH"] = ""

        result = subprocess.run(
            [str(hook), str(message_file)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=self.repo,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            message_file.read_text(encoding="utf-8"),
            "Existing message\n",
        )

    def test_installed_wrapper_allows_worktree_without_hook_script(self) -> None:
        self.assertEqual(self.install_hooks().returncode, 0)
        hook = self.repo / "scripts/hooks/commit-msg"
        hook.rename(hook.with_suffix(".disabled"))

        result = self.commit(
            "Older worktree commit",
            environment=self.provenance_environment(VIBE_LOOP_TASK_ID="TASK-42"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Plan-Item:", self.head_message())

    def test_compatible_unmanaged_provenance_hook_is_preserved(self) -> None:
        hooks_dir = self.repo / ".git/hooks"
        hook = hooks_dir / "commit-msg"
        existing = (
            "#!/bin/sh\n"
            '[ -n "${VIBE_LOOP_TASK_ID:-}" ] || exit 0\n'
            'git interpret-trailers --in-place --trailer "Plan-Item: '
            '${VIBE_LOOP_TASK_ID}" "$1"\n'
        )
        hook.write_text(existing, encoding="utf-8")
        hook.chmod(0o755)

        result = self.install_hooks()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compatible unmanaged provenance hook", result.stdout)
        self.assertEqual(hook.read_text(encoding="utf-8"), existing)
        self.assertTrue((hooks_dir / "pre-commit").is_file())
        self.assertTrue((hooks_dir / "pre-push").is_file())

    def test_incompatible_unmanaged_hook_has_actionable_failure(self) -> None:
        hooks_dir = self.repo / ".git/hooks"
        hook = hooks_dir / "commit-msg"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)

        result = self.install_hooks()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("move or remove it", result.stderr)
        self.assertTrue((hooks_dir / "pre-commit").is_file())
        self.assertTrue((hooks_dir / "pre-push").is_file())

    def test_installer_removes_obsolete_managed_prepare_commit_msg(self) -> None:
        hooks_dir = self.repo / ".git/hooks"
        legacy_hook = hooks_dir / "prepare-commit-msg"
        legacy_hook.write_text(
            '#!/bin/sh\nexec "$repo_root/scripts/hooks/prepare-commit-msg" "$@"\n',
            encoding="utf-8",
        )

        result = self.install_hooks()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(legacy_hook.exists())
        self.assertTrue((hooks_dir / "commit-msg").is_file())


if __name__ == "__main__":
    unittest.main()
