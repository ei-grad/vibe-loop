from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.locks import LockManager
from vibe_loop.runs import RunResult, RunStore
from vibe_loop.workers import (
    ActiveRunState,
    build_worker_views,
    classify_process,
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


if __name__ == "__main__":
    unittest.main()
