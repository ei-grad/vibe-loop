from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.config import AgentConfig, VibeConfig
from vibe_loop.locks import LockBusy, LockManager, LockOwnerMismatch
from vibe_loop.runner import (
    VibeRunner,
    build_batch_selection_prompt,
    build_selection_prompt,
    parse_selected_task_id,
    parse_selected_task_ids,
    parse_worker_session_id,
    run_streaming_command,
    validate_selected_task_batch,
)
from vibe_loop.runs import WORKER_REPORT_STATUSES, RunResult, WorkerReport
from vibe_loop.tasks import Task


class MutableTaskSource:
    def __init__(self, tasks: list[Task]):
        self._tasks = tasks
        self._done: set[str] = set()
        self._lock = threading.Lock()

    def list_tasks(self) -> list[Task]:
        with self._lock:
            return [
                dataclasses.replace(
                    task,
                    status="Done" if task.task_id in self._done else task.status,
                )
                for task in self._tasks
            ]

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id),
            None,
        )

    def mark_done(self, task_id: str) -> None:
        with self._lock:
            self._done.add(task_id)


class RunnerTests(unittest.TestCase):
    def test_selection_prompt_includes_recent_logs(self) -> None:
        task = Task(task_id="LIVE-04", title="Realtime reconcile", status="Next")

        prompt = build_selection_prompt([task], "recent log tail: timeout on WEB-01")

        self.assertIn("LIVE-04", prompt)
        self.assertIn("recent log tail", prompt)
        self.assertIn("blocked or just failed", prompt)

    def test_batch_selection_prompt_includes_context(self) -> None:
        task = Task(task_id="LIVE-04", title="Realtime reconcile", status="Next")

        prompt = build_batch_selection_prompt(
            [task],
            max_tasks=2,
            recent_log_context="recent log tail: timeout on WEB-01",
            active_worker_context="Active vibe-loop workers: []",
        )

        self.assertIn('"max_batch_size": 2', prompt)
        self.assertIn('"task_ids"', prompt)
        self.assertIn("LIVE-04", prompt)
        self.assertIn("recent log tail", prompt)
        self.assertIn("Active vibe-loop workers", prompt)

    def test_parse_selected_task_id_from_json_only_or_wrapped_output(self) -> None:
        self.assertEqual(
            parse_selected_task_id('{"task_id":"LIVE-04","reason":"ready"}'),
            "LIVE-04",
        )
        self.assertEqual(
            parse_selected_task_id('text\n{"task_id":"WEB-01"}\nmore'),
            "WEB-01",
        )
        self.assertIsNone(parse_selected_task_id("not json"))

    def test_parse_selected_task_ids_from_batch_output(self) -> None:
        self.assertEqual(
            parse_selected_task_ids('{"task_ids":["LIVE-04","WEB-01"]}'),
            ["LIVE-04", "WEB-01"],
        )
        self.assertEqual(
            parse_selected_task_ids('text\n{"task_id":"WEB-01"}\nmore'),
            ["WEB-01"],
        )
        self.assertIsNone(parse_selected_task_ids('{"task_ids":["WEB-01", 2]}'))
        self.assertIsNone(parse_selected_task_ids('{"task_ids":[]}'))

    def test_validate_selected_task_batch_rejects_unsafe_ids(self) -> None:
        candidates = [
            Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
            Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
        ]

        valid = validate_selected_task_batch(
            ["TASK-02", "TASK-01"],
            candidates,
            limit=2,
            is_locked=lambda _task_id: False,
        )
        duplicate = validate_selected_task_batch(
            ["TASK-01", "TASK-01"],
            candidates,
            limit=2,
        )
        unknown = validate_selected_task_batch(["TASK-99"], candidates, limit=2)
        too_many = validate_selected_task_batch(
            ["TASK-01", "TASK-02"],
            candidates,
            limit=1,
        )
        locked = validate_selected_task_batch(
            ["TASK-02"],
            candidates,
            limit=2,
            is_locked=lambda task_id: task_id == "TASK-02",
        )

        self.assertTrue(valid.valid)
        self.assertEqual([task.task_id for task in valid.tasks], ["TASK-02", "TASK-01"])
        self.assertFalse(duplicate.valid)
        self.assertEqual(duplicate.error, "duplicate task_id: TASK-01")
        self.assertFalse(unknown.valid)
        self.assertEqual(unknown.error, "unknown task_id: TASK-99")
        self.assertFalse(too_many.valid)
        self.assertEqual(too_many.error, "too many task_ids")
        self.assertFalse(locked.valid)
        self.assertEqual(locked.error, "locked task_id: TASK-02")

    def test_parse_worker_session_id_from_codex_style_output(self) -> None:
        self.assertEqual(parse_worker_session_id("session id: abc-123"), "abc-123")
        self.assertEqual(parse_worker_session_id("Session_ID = codex.456"), "codex.456")
        self.assertIsNone(parse_worker_session_id("session started"))

    def test_classify_uses_worker_report_statuses_before_task_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))

            for status in WORKER_REPORT_STATUSES:
                for exit_code, message in (
                    (0, ""),
                    (7, ""),
                    (0, "completion check failed"),
                ):
                    with self.subTest(
                        status=status,
                        exit_code=exit_code,
                        message=message,
                    ):
                        result = runner.classify(
                            "TASK-01",
                            exit_code,
                            "aaa",
                            "aaa",
                            message,
                            WorkerReport(
                                run_id=f"run-{status}",
                                task_id="TASK-01",
                                status=status,
                            ),
                        )

                        self.assertEqual(result.status, status)
                        self.assertEqual(result.source, "worker_report")

    def test_run_until_done_parallel_honors_jobs_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                    Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
                    Task(task_id="TASK-04", title="Task 4", status="Next", order=4),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2)

        self.assertEqual(max_active, 2)
        self.assertEqual(len(results), 4)
        self.assertLessEqual(max_active, 2)
        self.assertEqual(
            sorted(result.task_id for result in results),
            ["TASK-01", "TASK-02", "TASK-03", "TASK-04"],
        )

    def test_run_until_done_default_remains_serial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.01)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done()

        self.assertEqual(max_active, 1)
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-02"],
        )

    def test_parallel_batch_selection_falls_back_to_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker", selection_command="selector"),
                )
            )
            tasks = [
                Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
            ]
            runner.ask_agent_to_select_batch = lambda _candidates, _limit: None

            selected = runner.select_batch_from_candidates(
                tasks,
                limit=2,
                ask_agent=True,
            )

        self.assertEqual(
            [task.task_id for task in selected],
            ["TASK-01", "TASK-02"],
        )

    def test_parallel_undersized_agent_batch_waits_before_refill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker", selection_command="selector"),
                )
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                    Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()
            selected_batches: list[list[str]] = []

            def select_one_task(candidates: list[Task], _limit: int) -> list[Task]:
                selected_batches.append([task.task_id for task in candidates])
                return [candidates[0]]

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.ask_agent_to_select_batch = select_one_task
            runner.run_task = run_task

            results = runner.run_until_done(ask_agent=True, jobs=2, max_slices=2)

        self.assertEqual(max_active, 1)
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-02"],
        )
        self.assertEqual(
            selected_batches,
            [
                ["TASK-01", "TASK-02", "TASK-03"],
                ["TASK-02", "TASK-03"],
            ],
        )

    def test_run_until_done_parallel_excludes_task_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source
            held_lock = runner.lock_manager.acquire("TASK-01", "external-run")

            def run_task(task: Task) -> RunResult:
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            try:
                results = runner.run_until_done(jobs=2, max_slices=1)
            finally:
                runner.lock_manager.release(held_lock)

        self.assertEqual([result.task_id for result in results], ["TASK-02"])

    def test_run_until_done_parallel_skips_task_lock_races(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                if task.task_id == "TASK-01":
                    raise LockBusy(repo / ".vibe-loop" / "locks" / "TASK-01.lock", {})
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_slices=1)

        self.assertEqual([result.task_id for result in results], ["TASK-02"])

    def test_lock_manager_rejects_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            task_lock = manager.acquire("LIVE-04", "run-1")
            try:
                self.assertTrue(manager.is_locked("LIVE-04"))
                with self.assertRaises(LockBusy):
                    manager.acquire("LIVE-04", "run-2")
            finally:
                manager.release(task_lock)
            self.assertFalse(manager.is_locked("LIVE-04"))

    def test_lock_manager_rejects_empty_existing_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_root = Path(directory) / "locks"
            (lock_root / "LIVE-04.lock").mkdir(parents=True)
            manager = LockManager(lock_root)

            with self.assertRaises(LockBusy):
                manager.acquire("LIVE-04", "run-2")

    def test_main_integration_lock_serializes_holder_and_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            holder = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
            )
            try:
                status = manager.main_integration_status(
                    process_exists=lambda pid: True,
                )

                self.assertTrue(status.locked)
                self.assertEqual(status.state, "held")
                self.assertEqual(status.process_state, "running")
                self.assertEqual(status.metadata["task_id"], "main-integration")
                self.assertEqual(status.metadata["owner_task_id"], "TASK-01")
                self.assertEqual(status.metadata["run_id"], "run-holder")
                with self.assertRaises(LockBusy) as busy:
                    manager.acquire_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
                self.assertEqual(busy.exception.metadata["owner_task_id"], "TASK-01")
                with self.assertRaises(LockOwnerMismatch):
                    manager.release_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
                self.assertTrue(
                    manager.release_main_integration(
                        task_id="TASK-01",
                        run_id="run-holder",
                    )
                )
                self.assertFalse(manager.main_integration_status().locked)
            finally:
                manager.release(holder)

    def test_main_integration_stale_lock_is_visible_but_not_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            held_lock = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
                metadata={"pid": 999999999, "host": "test-host"},
            )
            try:
                status = manager.main_integration_status(
                    current_host="test-host",
                    process_exists=lambda pid: False,
                )

                self.assertTrue(status.locked)
                self.assertEqual(status.state, "stale")
                self.assertEqual(status.process_state, "missing")
                self.assertEqual(status.stale_reason, "missing_process")
                with self.assertRaises(LockBusy):
                    manager.acquire_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
            finally:
                manager.release(held_lock)

    def test_streaming_command_forwards_stdout_and_logs_stderr_by_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stdout = StringIO()
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = run_streaming_command(
                        'python -c \'import sys; print("out"); print("err", file=sys.stderr)\'',
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(result.session_id)
            self.assertIsNone(result.session_id_source)
            self.assertEqual("", stdout.getvalue())
            self.assertIn("out", stderr.getvalue())
            self.assertNotIn("err", stderr.getvalue())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("out", log_text)
            self.assertIn("err", log_text)

    def test_streaming_command_can_forward_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        "python -c 'import sys; print(\"err\", file=sys.stderr)'",
                        Path(directory),
                        log,
                        forward_stderr=True,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIn("err", stderr.getvalue())
            self.assertIn("err", log_path.read_text(encoding="utf-8"))

    def test_streaming_command_captures_stdout_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        "python -c 'print(\"session id: native-stdout-123\")'",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.session_id, "native-stdout-123")
            self.assertEqual(result.session_id_source, "native:stdout")
            self.assertIn("session id: native-stdout-123", stderr.getvalue())
            self.assertIn(
                "session id: native-stdout-123",
                log_path.read_text(encoding="utf-8"),
            )

    def test_streaming_command_reports_started_process_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            started_pids: list[int] = []
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    "python -c 'print(\"ok\")'",
                    Path(directory),
                    log,
                    on_start=started_pids.append,
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(started_pids), 1)
        self.assertGreater(started_pids[0], 0)

    def test_streaming_command_captures_stderr_session_id_without_forwarding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        "python -c 'import sys; "
                        'print("session id: native-stderr-123", file=sys.stderr)\'',
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.session_id, "native-stderr-123")
            self.assertEqual(result.session_id_source, "native:stderr")
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn(
                "session id: native-stderr-123",
                log_path.read_text(encoding="utf-8"),
            )

    def test_streaming_command_replaces_undecodable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        'python -c "import sys; '
                        "sys.stdout.buffer.write(b'ok\\\\xff\\\\n'); "
                        "sys.stderr.buffer.write(b'bad\\\\xfe\\\\n')\"",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIn("ok", stderr.getvalue())
            self.assertIn("\ufffd", stderr.getvalue())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("ok", log_text)
            self.assertIn("bad", log_text)
            self.assertIn("\ufffd", log_text)


if __name__ == "__main__":
    unittest.main()
