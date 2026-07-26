from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_loop.autopilot import TaskQueueStatus, active_unlocked_task_blockers
from vibe_loop.config import parse_autopilot
from vibe_loop.runs import (
    RUN_CONTRACT_RESOLVED_RECORD_TYPE,
    RUN_RECORD_TYPE,
    RUN_STARTED_RECORD_TYPE,
)
from vibe_loop.upstream import check_upstream_sync


class UpstreamSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        self._run(root, "git", "init", "--bare", str(self.remote))
        self._run(root, "git", "init", "-b", "main", str(self.repo))
        self._run(self.repo, "git", "config", "user.name", "Test")
        self._run(self.repo, "git", "config", "user.email", "test@example.invalid")
        (self.repo / "value.txt").write_text("one\n", encoding="utf-8")
        self._run(self.repo, "git", "add", "value.txt")
        self._run(self.repo, "git", "commit", "-m", "initial")
        self._run(self.repo, "git", "remote", "add", "origin", str(self.remote))
        self._run(self.repo, "git", "push", "-u", "origin", "main")

    def _run(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _commit(self, value: str) -> str:
        (self.repo / "value.txt").write_text(value, encoding="utf-8")
        self._run(self.repo, "git", "add", "value.txt")
        self._run(self.repo, "git", "commit", "-m", value.strip())
        return self._run(self.repo, "git", "rev-parse", "HEAD")

    def _push_from_other_clone(self, value: str) -> None:
        other = self.repo.parent / "other"
        self._run(self.repo.parent, "git", "clone", str(self.remote), str(other))
        self._run(other, "git", "checkout", "main")
        self._run(other, "git", "config", "user.name", "Other")
        self._run(other, "git", "config", "user.email", "other@example.invalid")
        (other / "other.txt").write_text(value, encoding="utf-8")
        self._run(other, "git", "add", "other.txt")
        self._run(other, "git", "commit", "-m", value)
        self._run(other, "git", "push", "origin", "main")

    def test_disabled_policy_is_explicit_and_does_not_fetch(self) -> None:
        status = check_upstream_sync(
            self.repo,
            "main",
            required=False,
            refresh=False,
        )

        self.assertTrue(status.satisfied)
        self.assertEqual(status.relation, "policy_disabled")
        self.assertFalse(status.fresh)

    def test_equal_fresh_upstream_contains_reviewed_commit(self) -> None:
        reviewed = self._run(self.repo, "git", "rev-parse", "HEAD")

        status = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            reviewed_commit=reviewed,
            refresh=True,
        )

        self.assertTrue(status.satisfied)
        self.assertEqual(status.relation, "equal")
        self.assertTrue(status.fresh)
        self.assertTrue(status.reviewed_commit_contained)

    def test_local_main_ahead_is_typed_blocker(self) -> None:
        reviewed = self._commit("two\n")

        status = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            reviewed_commit=reviewed,
            refresh=True,
        )

        self.assertFalse(status.satisfied)
        self.assertIsNotNone(status.blocker)
        assert status.blocker is not None
        self.assertEqual(status.blocker.code, "upstream_ahead")
        self.assertEqual(status.blocker.relation, "ahead")
        self.assertEqual(status.blocker.ahead, 1)
        self.assertFalse(status.blocker.reviewed_commit_contained)
        self.assertEqual(status.blocker.unmet_prerequisite, "upstream_equality")

    def test_missing_upstream_and_stale_ref_are_distinct(self) -> None:
        self._run(self.repo, "git", "branch", "--unset-upstream")
        missing = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            refresh=True,
        )
        self._run(self.repo, "git", "branch", "--set-upstream-to=origin/main")
        stale = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            refresh=False,
        )

        self.assertEqual(
            missing.blocker.code if missing.blocker else "", "missing_upstream"
        )
        self.assertEqual(stale.blocker.code if stale.blocker else "", "stale_ref")

    def test_required_reviewed_commit_cannot_be_omitted(self) -> None:
        status = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            require_reviewed_commit=True,
            refresh=True,
        )

        self.assertEqual(
            status.blocker.code if status.blocker else "",
            "reviewed_commit_missing",
        )

    def test_behind_and_diverged_relations_are_distinct(self) -> None:
        self._push_from_other_clone("remote")
        behind = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            refresh=True,
        )
        self._commit("local\n")
        diverged = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            refresh=True,
        )

        self.assertEqual(
            behind.blocker.code if behind.blocker else "", "upstream_behind"
        )
        self.assertEqual(
            diverged.blocker.code if diverged.blocker else "",
            "upstream_diverged",
        )

    def test_fetch_failure_is_not_reported_as_stale_ref(self) -> None:
        self._run(
            self.repo,
            "git",
            "remote",
            "set-url",
            "origin",
            str(self.repo.parent / "missing.git"),
        )

        status = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            refresh=True,
        )

        self.assertEqual(status.blocker.code if status.blocker else "", "fetch_failed")

    def test_policy_config_is_typed_and_defaults_disabled(self) -> None:
        self.assertFalse(parse_autopilot({}).require_upstream_sync)
        self.assertTrue(
            parse_autopilot({"require_upstream_sync": True}).require_upstream_sync
        )

    def test_equal_upstream_still_reports_unmet_lifecycle_prerequisite(self) -> None:
        reviewed = self._run(self.repo, "git", "rev-parse", "HEAD")

        status = check_upstream_sync(
            self.repo,
            "main",
            required=True,
            reviewed_commit=reviewed,
            require_reviewed_commit=True,
            refresh=True,
            unmet_prerequisite="task_source_completion",
        )

        self.assertEqual(
            status.blocker.code if status.blocker else "",
            "lifecycle_prerequisite_unmet",
        )
        self.assertEqual(
            status.blocker.unmet_prerequisite if status.blocker else "",
            "task_source_completion",
        )


class ActiveUnlockedFenceTests(unittest.TestCase):
    def test_new_unclassified_run_is_not_masked_by_prior_result(self) -> None:
        queue = TaskQueueStatus(source_tasks=({"id": "TASK-1", "status": "active"},))
        records = (
            {
                "record_type": RUN_RECORD_TYPE,
                "task_id": "TASK-1",
                "run_id": "old",
                "classification": "failed",
            },
            {
                "record_type": RUN_STARTED_RECORD_TYPE,
                "task_id": "TASK-1",
                "run_id": "current",
            },
        )

        self.assertEqual(
            active_unlocked_task_blockers(queue, (), records),
            ["active_unlocked_without_terminal_classification:TASK-1"],
        )

    def test_completed_runtime_run_requires_task_and_provenance_settlement(
        self,
    ) -> None:
        queue = TaskQueueStatus(source_tasks=({"id": "TASK-1", "status": "active"},))
        records = (
            {
                "record_type": RUN_STARTED_RECORD_TYPE,
                "task_id": "TASK-1",
                "run_id": "current",
            },
            {
                "record_type": RUN_CONTRACT_RESOLVED_RECORD_TYPE,
                "task_id": "TASK-1",
                "run_id": "current",
                "mode": "runtime-owned",
            },
            {
                "record_type": RUN_RECORD_TYPE,
                "task_id": "TASK-1",
                "run_id": "current",
                "classification": "completed",
            },
        )

        self.assertEqual(
            active_unlocked_task_blockers(queue, (), records),
            [
                "active_unlocked_task_completion_unsettled:TASK-1",
                "task_completion_unsettled:TASK-1:current",
                "task_provenance_unsettled:TASK-1:current",
            ],
        )


if __name__ == "__main__":
    unittest.main()
