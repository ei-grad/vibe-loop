from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.locks import LockManager
from vibe_loop.runs import RunResult, RunStore, WorkerReport
from vibe_loop.workers import (
    ActiveRunState,
    StaleLock,
    WorkspaceClaim,
    build_worker_views,
    classify_process,
    clean_stale_locks,
    collect_stale_locks,
    load_active_run_states,
)


class WorkerStateTests(unittest.TestCase):
    def test_active_state_round_trips_through_lock_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            log_path = repo / ".vibe-loop" / "runs" / "run-1.log"
            state = ActiveRunState(
                task_id="PAR-02",
                run_id="run-1",
                worker_pid=1234,
                supervisor_pid=5678,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=log_path,
                base_main="abc123",
                command="codex exec '$vibe-loop PAR-02'",
                workspace=WorkspaceClaim(
                    task_id="PAR-02",
                    run_id="run-1",
                    branch="codex/PAR-02",
                    worktree=repo,
                    base_commit="abc123",
                    head_commit="def456",
                    current_branch="codex/PAR-02",
                    dirty=True,
                    dirty_summary=(" M src/example.py",),
                    claimed_at="2026-05-09T00:01:00+00:00",
                ),
            )
            task_lock = manager.acquire(
                "PAR-02",
                "run-1",
                metadata=state.to_lock_metadata(),
            )

            loaded = load_active_run_states(manager)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].task_id, "PAR-02")
        self.assertEqual(loaded[0].run_id, "run-1")
        self.assertEqual(loaded[0].worker_pid, 1234)
        self.assertEqual(loaded[0].pid_source, "popen")
        self.assertEqual(loaded[0].pid_scope, "configured_command_process")
        self.assertEqual(loaded[0].supervisor_pid, 5678)
        self.assertEqual(loaded[0].host, "test-host")
        self.assertEqual(loaded[0].started_at, "2026-05-09T00:00:00+00:00")
        self.assertEqual(loaded[0].log_path, log_path)
        self.assertEqual(loaded[0].base_main, "abc123")
        self.assertEqual(loaded[0].command, "codex exec '$vibe-loop PAR-02'")
        self.assertEqual(loaded[0].lock_path, task_lock.path)
        workspace = loaded[0].workspace
        self.assertIsNotNone(workspace)
        if workspace is None:
            self.fail("workspace claim did not round-trip")
        self.assertEqual(workspace.branch, "codex/PAR-02")
        self.assertEqual(workspace.worktree, repo)
        self.assertTrue(workspace.dirty)
        self.assertEqual(workspace.dirty_summary, (" M src/example.py",))

    def test_process_classification_detects_running_and_missing_pid(self) -> None:
        state = ActiveRunState(
            task_id="PAR-02",
            run_id="run-1",
            worker_pid=100,
            host="test-host",
            started_at="2026-05-09T00:00:00+00:00",
            log_path=Path("run.log"),
            base_main="abc123",
            command="agent PAR-02",
        )

        self.assertEqual(
            classify_process(state, "test-host", process_exists=lambda pid: True),
            "running",
        )
        self.assertEqual(
            classify_process(state, "test-host", process_exists=lambda pid: False),
            "missing",
        )

    def test_worker_views_mark_missing_process_and_recorded_result_as_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="PAR-02",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent PAR-02",
            )
            manager.acquire("PAR-02", "run-1", metadata=state.to_lock_metadata())

            missing = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            run_store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="PAR-02",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                    start_main="abc123",
                    end_main="def456",
                )
            )
            recorded = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(missing[0].state, "stale")
        self.assertEqual(missing[0].process_state, "missing")
        self.assertEqual(missing[0].stale_reason, "missing_process")
        self.assertEqual(recorded[0].state, "stale")
        self.assertEqual(recorded[0].process_state, "running")
        self.assertEqual(recorded[0].stale_reason, "result_recorded")
        self.assertEqual(recorded[0].result_status, "completed")

    def test_worker_views_show_report_status_before_final_result_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="PAR-03",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent PAR-03",
            )
            manager.acquire("PAR-03", "run-1", metadata=state.to_lock_metadata())
            run_store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="PAR-03",
                    status="blocked",
                    metadata={"reason": "dependency"},
                )
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(views[0].state, "running")
        self.assertEqual(views[0].stale_reason, None)
        self.assertEqual(views[0].result_status, "blocked")
        self.assertEqual(views[0].result_record_type, "worker_report")
        self.assertEqual(views[0].result_metadata, {"reason": "dependency"})

    def test_worker_views_ignore_invalid_or_mismatched_report_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="PAR-03",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent PAR-03",
            )
            manager.acquire("PAR-03", "run-1", metadata=state.to_lock_metadata())
            run_store.append_record(
                {
                    "record_type": "worker_report",
                    "run_id": "run-1",
                    "task_id": "PAR-03",
                    "status": "not-valid",
                }
            )
            run_store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="OTHER-01",
                    status="blocked",
                )
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(views[0].state, "running")
        self.assertEqual(views[0].result_status, None)
        self.assertEqual(views[0].result_metadata, None)

    def test_worker_views_report_corrupt_or_incomplete_lock_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_path = repo / ".vibe-loop" / "locks" / "PAR-02.lock"
            lock_path.mkdir(parents=True)
            (lock_path / "lock.json").write_text("{not-json", encoding="utf-8")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].active.task_id, "PAR-02")
        self.assertEqual(views[0].active.run_id, "")
        self.assertEqual(views[0].state, "stale")
        self.assertEqual(views[0].process_state, "unknown_pid")
        self.assertEqual(views[0].stale_reason, "missing_run_id")
        self.assertEqual(views[0].active.lock_path, lock_path)

    def test_worker_views_use_legacy_pid_when_worker_pid_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            manager.acquire(
                "PAR-02",
                "run-1",
                metadata={
                    "task_id": "PAR-02",
                    "run_id": "run-1",
                    "pid": 100,
                    "host": "test-host",
                    "started_at": "2026-05-09T00:00:00+00:00",
                },
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(views[0].active.worker_pid, 100)
        self.assertEqual(views[0].active.pid_source, "legacy_pid")
        self.assertEqual(views[0].state, "stale")
        self.assertEqual(views[0].process_state, "missing")
        self.assertEqual(views[0].stale_reason, "missing_process")

    def test_worker_views_mark_active_run_without_worker_pid_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="PAR-02",
                run_id="run-1",
                worker_pid=None,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent PAR-02",
            )
            manager.acquire("PAR-02", "run-1", metadata=state.to_lock_metadata())

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(views[0].state, "stale")
        self.assertEqual(views[0].process_state, "unknown_pid")
        self.assertEqual(views[0].stale_reason, "missing_worker_pid")

    def test_worker_views_ignore_main_integration_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={"pid": 100, "host": "test-host"},
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(views, [])


class StaleLockTests(unittest.TestCase):
    def test_collect_stale_task_lock_with_missing_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=999999999,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].task_id, "TASK-01")
        self.assertEqual(stale[0].run_id, "run-1")
        self.assertEqual(stale[0].stale_reason, "missing_process")
        self.assertEqual(stale[0].kind, "task")
        self.assertIn("rm -rf", stale[0].recovery_command)

    def test_collect_stale_integration_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={"pid": 999999999, "host": "test-host"},
            )

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].task_id, "main-integration")
        self.assertEqual(stale[0].kind, "integration")
        self.assertEqual(stale[0].stale_reason, "missing_process")
        self.assertIn("rm -rf", stale[0].recovery_command)

    def test_collect_skips_running_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(stale, [])

    def test_collect_stale_lock_with_result_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())
            run_store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                    start_main="abc123",
                    end_main="def456",
                )
            )

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].stale_reason, "result_recorded")

    def test_clean_removes_stale_lock_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=999999999,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            lock_path = stale[0].lock_path
            self.assertTrue(lock_path.exists())

            result = clean_stale_locks(stale)

        self.assertEqual(len(result.cleaned), 1)
        self.assertEqual(result.cleaned[0].task_id, "TASK-01")
        self.assertEqual(result.errors, [])
        self.assertFalse(lock_path.exists())

    def test_clean_handles_already_removed_locks(self) -> None:
        lock = StaleLock(
            task_id="GONE",
            run_id="run-x",
            lock_path=Path("/tmp/nonexistent-lock-dir"),
            stale_reason="missing_process",
            kind="task",
            recovery_command="rm -rf /tmp/nonexistent-lock-dir",
        )
        result = clean_stale_locks([lock])
        self.assertEqual(result.cleaned, [])
        self.assertEqual(result.errors, [])

    def test_stale_lock_to_json(self) -> None:
        lock = StaleLock(
            task_id="T-01",
            run_id="r-1",
            lock_path=Path("/state/locks/T-01.lock"),
            stale_reason="missing_process",
            kind="task",
            recovery_command="rm -rf /state/locks/T-01.lock",
        )
        payload = lock.to_json()
        self.assertEqual(payload["task_id"], "T-01")
        self.assertEqual(payload["run_id"], "r-1")
        self.assertEqual(payload["stale_reason"], "missing_process")
        self.assertEqual(payload["kind"], "task")
        self.assertIn("rm -rf", payload["recovery_command"])

    def test_collect_both_task_and_integration_stale_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=999999999,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={"pid": 999999999, "host": "test-host"},
            )

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        kinds = {s.kind for s in stale}
        self.assertEqual(kinds, {"task", "integration"})
        self.assertEqual(len(stale), 2)

    def test_clean_refuses_if_run_id_changed_since_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="run-1",
                worker_pid=999999999,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire("TASK-01", "run-1", metadata=state.to_lock_metadata())

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            lock_path = stale[0].lock_path
            (lock_path / "lock.json").write_text(
                '{"run_id": "run-2-new", "task_id": "TASK-01"}',
                encoding="utf-8",
            )

            result = clean_stale_locks(stale)
            lock_still_exists = lock_path.exists()

        self.assertEqual(result.cleaned, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("changed since collection", result.errors[0][1])
        self.assertTrue(lock_still_exists)

    def test_recovery_command_quotes_paths_with_spaces(self) -> None:
        import shlex

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_dir = repo / ".vibe-loop" / "locks"
            lock_dir.mkdir(parents=True)
            spaced_lock = lock_dir / "my task.lock"
            spaced_lock.mkdir()
            (spaced_lock / "lock.json").write_text(
                '{"run_id": "r-1", "task_id": "my task", "pid": 999999999,'
                ' "host": "test-host", "record_type": "active_run"}',
                encoding="utf-8",
            )
            manager = LockManager(lock_dir)
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(len(stale), 1)
        cmd = stale[0].recovery_command
        parts = shlex.split(cmd)
        self.assertEqual(parts[0], "rm")
        self.assertEqual(parts[1], "-rf")
        self.assertEqual(parts[2], str(spaced_lock))


if __name__ == "__main__":
    unittest.main()
