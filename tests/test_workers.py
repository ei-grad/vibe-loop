from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.config import LockConfig
from vibe_loop.locks import (
    COMMAND_LOCK_MAX_OUTPUT_BYTES,
    LockBackendError,
    LockBusy,
    LockFencingMismatch,
    LockManager,
    build_lock_manager,
)
from vibe_loop.runs import (
    LOCK_ACQUIRED_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    WorkerReport,
)
from vibe_loop.workers import (
    ActiveRunState,
    StaleLock,
    WorkspaceClaim,
    build_worker_views,
    classify_process,
    clean_stale_locks,
    collect_stale_locks,
    load_active_run_states,
    parse_git_worktree_list,
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
                session_id="run-1",
                session_id_source="fallback:run_id",
                agent_kind="codex",
                agent_prompt_dialect="codex",
                agent_prompt_dialect_source="agent.kind:codex",
                agent_skill_ref_prefix="$",
                agent_skill_ref_prefix_source="agent.kind:codex",
                model_provider="openai",
                model_provider_source="command_executable:codex",
                model_id="gpt-5.5",
                model_id_source="native:stdout:json.model",
                reasoning_effort="high",
                reasoning_effort_source="native:stdout:json.reasoning_effort",
                trailer_context={
                    "plan_item_candidates": ["PAR-02"],
                    "run_id": "run-1",
                    "session_id": "run-1",
                    "model_id": "gpt-5.5",
                },
                trailer_context_sources={
                    "plan_item_candidates": "task_id",
                    "session_id": "fallback:run_id",
                    "model_id": "native:stdout:json.model",
                },
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
                    started_at="2026-05-09T00:00:00+00:00",
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
        self.assertEqual(loaded[0].session_id, "run-1")
        self.assertEqual(loaded[0].session_id_source, "fallback:run_id")
        self.assertEqual(loaded[0].agent_kind, "codex")
        self.assertEqual(loaded[0].agent_prompt_dialect, "codex")
        self.assertEqual(loaded[0].agent_prompt_dialect_source, "agent.kind:codex")
        self.assertEqual(loaded[0].agent_skill_ref_prefix, "$")
        self.assertEqual(loaded[0].agent_skill_ref_prefix_source, "agent.kind:codex")
        self.assertEqual(loaded[0].model_provider, "openai")
        self.assertEqual(loaded[0].model_provider_source, "command_executable:codex")
        self.assertEqual(loaded[0].model_id, "gpt-5.5")
        self.assertEqual(loaded[0].model_id_source, "native:stdout:json.model")
        self.assertEqual(loaded[0].reasoning_effort, "high")
        self.assertEqual(loaded[0].trailer_context["model_id"], "gpt-5.5")
        self.assertEqual(
            loaded[0].trailer_context_sources["model_id"],
            "native:stdout:json.model",
        )
        self.assertEqual(loaded[0].lock_path, task_lock.path)
        workspace = loaded[0].workspace
        self.assertIsNotNone(workspace)
        if workspace is None:
            self.fail("workspace claim did not round-trip")
        self.assertEqual(workspace.branch, "codex/PAR-02")
        self.assertEqual(workspace.worktree, repo)
        self.assertTrue(workspace.dirty)
        self.assertEqual(workspace.dirty_summary, (" M src/example.py",))
        self.assertEqual(workspace.started_at, "2026-05-09T00:00:00+00:00")

    def test_command_lock_backend_delegates_lock_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            adapter = repo / "lock_adapter.py"
            write_command_lock_adapter(adapter)
            command = f"{sys.executable} {json.dumps(str(adapter))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            task_lock = manager.acquire(
                "TASK-01",
                "run-1",
                metadata={"record_type": "active_run", "custom": "value"},
            )
            updated_metadata = dict(task_lock.metadata)
            updated_metadata["worker_pid"] = 123
            updated_lock = manager.update(task_lock, updated_metadata)
            locks = manager.list_locks()

            with self.assertRaises(LockBusy) as busy:
                manager.acquire("TASK-01", "run-2")

            self.assertTrue(manager.is_locked("TASK-01"))
            self.assertEqual(updated_lock.metadata["worker_pid"], 123)
            self.assertEqual(len(locks), 1)
            self.assertEqual(locks[0]["task_id"], "TASK-01")
            self.assertEqual(locks[0]["run_id"], "run-1")
            self.assertEqual(busy.exception.metadata["run_id"], "run-1")

            manager.release(updated_lock)

            self.assertFalse(manager.is_locked("TASK-01"))
            self.assertEqual(manager.list_locks(), [])

    def test_command_lock_backend_fencing_rejects_stale_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            adapter = repo / "lock_adapter.py"
            write_command_lock_adapter(adapter)
            command = f"{sys.executable} {json.dumps(str(adapter))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )
            first = manager.acquire("TASK-01", "run-1")
            first_token = first.metadata["fencing_token"]
            manager.release(first)
            second = manager.acquire("TASK-01", "run-2")
            second_token = second.metadata["fencing_token"]

            with self.assertRaises(LockFencingMismatch):
                manager.update(first, {"task_id": "TASK-01", "run_id": "run-1"})
            with self.assertRaises(LockFencingMismatch):
                manager.release(first)

            still_locked = manager.is_locked("TASK-01")
            manager.release(second)

        self.assertNotEqual(first_token, second_token)
        self.assertTrue(still_locked)

    def test_directory_lock_acquire_cleans_up_when_fencing_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            lock_root.mkdir(parents=True)
            (lock_root / ".fencing-tokens").write_text("not a dir\n", encoding="utf-8")
            manager = LockManager(lock_root)

            with self.assertRaises(OSError):
                manager.acquire("TASK-01", "run-1")

            lock_path = manager.backend.path_for("TASK-01")
            lock_exists = lock_path.exists()

        self.assertFalse(lock_exists)

    def test_directory_lock_preserves_fencing_token_on_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            task_lock = manager.acquire("TASK-01", "run-1")
            token = task_lock.metadata["fencing_token"]

            updated_lock = manager.update(
                task_lock,
                {"task_id": "TASK-01", "run_id": "run-1", "worker_pid": 123},
            )

        self.assertEqual(updated_lock.metadata["worker_pid"], 123)
        self.assertEqual(updated_lock.metadata["fencing_token"], token)

    def test_directory_lock_preserves_observed_runtime_fields_on_stale_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            task_lock = manager.acquire(
                "TASK-01",
                "run-1",
                metadata={
                    "session_id": "run-1",
                    "model_id": "",
                    "trailer_context": {},
                },
            )
            manager.update(
                task_lock,
                {
                    **task_lock.metadata,
                    "session_id": "native-1",
                    "session_id_source": "native:stdout",
                    "model_id": "gpt-5.5",
                    "model_id_source": "native:stdout:json.model",
                    "trailer_context": {"session_id": "native-1"},
                    "workspace": {"branch": "worker/TASK-01"},
                },
            )

            stale_update = manager.update(
                task_lock,
                {
                    **task_lock.metadata,
                    "session_id": "run-1",
                    "model_id": "",
                    "trailer_context": {},
                    "worker_pid": 123,
                },
            )

        self.assertEqual(stale_update.metadata["session_id"], "native-1")
        self.assertEqual(stale_update.metadata["session_id_source"], "native:stdout")
        self.assertEqual(stale_update.metadata["model_id"], "gpt-5.5")
        self.assertEqual(
            stale_update.metadata["model_id_source"],
            "native:stdout:json.model",
        )
        self.assertEqual(
            stale_update.metadata["trailer_context"],
            {"session_id": "native-1"},
        )
        self.assertEqual(
            stale_update.metadata["workspace"], {"branch": "worker/TASK-01"}
        )
        self.assertEqual(stale_update.metadata["worker_pid"], 123)

    def test_directory_backend_revalidates_fencing_token_during_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            first = manager.acquire("TASK-01", "run-1")
            manager.release(first)
            manager.acquire("TASK-01", "run-2")

            with self.assertRaises(LockFencingMismatch):
                manager.backend.update(
                    first,
                    {
                        **first.metadata,
                        "worker_pid": 123,
                    },
                )

    def test_directory_lock_fencing_rejects_stale_update_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            first = manager.acquire("TASK-01", "run-1")
            first_token = first.metadata["fencing_token"]
            manager.release(first)
            second = manager.acquire("TASK-01", "run-2")
            second_token = second.metadata["fencing_token"]

            with self.assertRaises(LockFencingMismatch):
                manager.update(first, {"task_id": "TASK-01", "run_id": "run-1"})
            with self.assertRaises(LockFencingMismatch):
                manager.release(first)

            still_locked = manager.is_locked("TASK-01")
            manager.release(second)

        self.assertNotEqual(first_token, second_token)
        self.assertTrue(still_locked)

    def test_expired_lease_marks_worker_stale_and_heartbeat_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(lease_seconds=60),
            )
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
            task_lock = manager.acquire(
                "TASK-01",
                "run-1",
                metadata=state.to_lock_metadata(),
            )
            expired_metadata = dict(task_lock.metadata)
            expired_metadata["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
            task_lock = manager.update(task_lock, expired_metadata)

            expired = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )
            manager.heartbeat(
                task_id="TASK-01",
                run_id="run-1",
                fencing_token=str(task_lock.metadata["fencing_token"]),
            )
            refreshed = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        self.assertEqual(expired[0].state, "stale")
        self.assertEqual(expired[0].process_state, "running")
        self.assertEqual(expired[0].stale_reason, "lease_expired")
        self.assertEqual(refreshed[0].state, "running")
        self.assertIsNone(refreshed[0].stale_reason)

    def test_command_lock_backend_handles_main_integration_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            adapter = repo / "lock_adapter.py"
            write_command_lock_adapter(adapter)
            command = f"{sys.executable} {json.dumps(str(adapter))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={"pid": 123, "pid_source": "test", "host": "test-host"},
            )
            status = manager.main_integration_status(
                current_host="test-host",
                process_exists=lambda pid: True,
            )

            self.assertTrue(status.locked)
            self.assertEqual(status.metadata["owner_task_id"], "TASK-01")
            self.assertEqual(status.state, "held")
            self.assertIsNone(status.stale_reason)
            self.assertTrue(
                manager.release_main_integration(task_id="TASK-01", run_id="run-1")
            )
            self.assertFalse(manager.main_integration_status().locked)

    def test_command_lock_backend_failure_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            failing = repo / "failing_lock_adapter.py"
            failing.write_text(
                "import sys\n"
                "print('adapter unavailable', file=sys.stderr)\n"
                "raise SystemExit(17)\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {json.dumps(str(failing))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            with self.assertRaisesRegex(
                LockBackendError,
                "locks.status_command exited with status 17: adapter unavailable",
            ):
                manager.is_locked("TASK-01")

    def test_command_lock_backend_rejects_invalid_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            invalid = repo / "invalid_lock_adapter.py"
            invalid.write_text("print('not-json')\n", encoding="utf-8")
            command = f"{sys.executable} {json.dumps(str(invalid))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            with self.assertRaisesRegex(LockBackendError, "valid JSON"):
                manager.list_locks()

    def test_command_lock_backend_rejects_oversized_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            oversized = repo / "oversized_lock_adapter.py"
            oversized.write_text(
                f"print('x' * {COMMAND_LOCK_MAX_OUTPUT_BYTES + 1})\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {json.dumps(str(oversized))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            with self.assertRaisesRegex(LockBackendError, "stdout exceeds"):
                manager.list_locks()

    def test_command_lock_backend_release_false_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            adapter = repo / "lock_adapter.py"
            release_false = repo / "release_false.py"
            write_command_lock_adapter(adapter)
            release_false.write_text(
                "import json\nprint(json.dumps({'released': False}))\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {json.dumps(str(adapter))}"
            release_command = f"{sys.executable} {json.dumps(str(release_false))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=release_command,
                    status_command=command,
                    list_command=command,
                ),
            )

            task_lock = manager.acquire("TASK-01", "run-1")

            with self.assertRaisesRegex(LockBackendError, "released=false"):
                manager.release(task_lock)

            self.assertTrue(manager.is_locked("TASK-01"))

    def test_command_lock_backend_requires_update_success_marker(self) -> None:
        cases = [
            "{}",
            '{"updated": "false"}',
            '{"ok": false}',
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    adapter = repo / "lock_adapter.py"
                    update_adapter = repo / "update_adapter.py"
                    write_command_lock_adapter(adapter)
                    update_adapter.write_text(
                        "from __future__ import annotations\n"
                        "\n"
                        "import os\n"
                        "import runpy\n"
                        "\n"
                        "if os.environ['VIBE_LOOP_LOCK_OPERATION'] == 'update':\n"
                        f"    print({payload!r})\n"
                        "else:\n"
                        f"    runpy.run_path({str(adapter)!r}, run_name='__main__')\n",
                        encoding="utf-8",
                    )
                    command = f"{sys.executable} {json.dumps(str(update_adapter))}"
                    manager = build_lock_manager(
                        repo,
                        repo / ".vibe-loop" / "locks",
                        LockConfig(
                            type="command",
                            acquire_command=command,
                            release_command=command,
                            status_command=command,
                            list_command=command,
                        ),
                    )
                    task_lock = manager.acquire("TASK-01", "run-1")

                    with self.assertRaisesRegex(
                        LockBackendError,
                        "must return boolean acquired or updated",
                    ):
                        manager.update(task_lock, task_lock.metadata)

    def test_command_lock_backend_stale_cleanup_delegates_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            adapter = repo / "lock_adapter.py"
            write_command_lock_adapter(adapter)
            command = f"{sys.executable} {json.dumps(str(adapter))}"
            manager = build_lock_manager(
                repo,
                repo / ".vibe-loop" / "locks",
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )
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
            result = clean_stale_locks(stale, manager)

            self.assertEqual(len(stale), 1)
            self.assertEqual(
                stale[0].recovery_command,
                "vibe-loop workers clean --force",
            )
            self.assertFalse(stale[0].lock_path.exists())
            self.assertEqual(result.cleaned, stale)
            self.assertEqual(result.errors, [])
            self.assertFalse(manager.is_locked("TASK-01"))

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
                agent_kind="claude",
                agent_prompt_dialect="claude",
                agent_prompt_dialect_source="agent.kind:claude",
                agent_skill_ref_prefix="/",
                agent_skill_ref_prefix_source="agent.kind:claude",
            )
            manager.acquire("PAR-02", "run-1", metadata=state.to_lock_metadata())

            missing = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            missing_payload = missing[0].to_json()
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
        self.assertEqual(missing_payload["agent_kind"], "claude")
        self.assertEqual(missing_payload["agent_prompt_dialect"], "claude")
        self.assertEqual(
            missing_payload["agent_prompt_dialect_source"],
            "agent.kind:claude",
        )
        self.assertEqual(missing_payload["agent_skill_ref_prefix"], "/")
        self.assertEqual(
            missing_payload["agent_skill_ref_prefix_source"],
            "agent.kind:claude",
        )
        self.assertEqual(recorded[0].state, "stale")
        self.assertEqual(recorded[0].process_state, "running")
        self.assertEqual(recorded[0].stale_reason, "result_recorded")
        self.assertEqual(recorded[0].result_status, "completed")
        self.assertEqual(recorded[0].lifecycle_progress.state, "finalized")

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
        self.assertEqual(views[0].lifecycle_progress.state, "reported")
        payload = views[0].to_json()
        self.assertEqual(payload["lifecycle_state"], "reported")
        self.assertIn("finalized", payload["missing_lifecycle_transitions"])

    def test_worker_views_include_lifecycle_progress_from_run_records(self) -> None:
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
            run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_ACQUIRED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="PAR-03",
                    lock_kind="task",
                    lock_path=repo / ".vibe-loop" / "locks" / "PAR-03.lock",
                )
            )
            run_store.append_lifecycle_event(
                RunLifecycleEvent.run_state_transition(
                    run_id="run-1",
                    task_id="PAR-03",
                    to_state="started",
                    reason="task_lock_acquired",
                )
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )
            payload = views[0].to_json()

        self.assertEqual(payload["lifecycle_state"], "started")
        by_state = {
            transition["state"]: transition
            for transition in payload["lifecycle_transitions"]
        }
        self.assertTrue(by_state["scheduled"]["observed"])
        self.assertTrue(by_state["started"]["observed"])
        self.assertFalse(by_state["reported"]["observed"])

    def test_worker_views_include_restart_count_from_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="RT-04",
                run_id="run-1",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                base_main="abc123",
                command="agent RT-04",
                restart_count=2,
                max_restarts=3,
            )
            manager.acquire("RT-04", "run-1", metadata=state.to_lock_metadata())

            views = build_worker_views(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: True,
            )
            payload = views[0].to_json()

        self.assertEqual(payload["restart_count"], 2)
        self.assertEqual(payload["max_restarts"], 3)

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
        self.assertEqual(views[0].lifecycle_progress.state, "")
        self.assertEqual(views[0].lifecycle_progress.missing_states[0], "scheduled")

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


class WorkspaceGitDiagnosticsTests(unittest.TestCase):
    def test_worker_view_reports_missing_claimed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            missing = base / "missing-worktree"
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=missing,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        codes = diagnostic_codes(views[0])
        self.assertIn("missing_claimed_worktree", codes)
        self.assertIn("claimed_branch_missing", codes)
        self.assertEqual(views[0].workspace_git_state.status, "stale")

    def test_worker_view_reports_claimed_branch_at_different_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            actual = base / "actual-worker"
            missing = base / "missing-worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(actual), "main")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=missing,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        codes = diagnostic_codes(views[0])
        self.assertIn("missing_claimed_worktree", codes)
        self.assertIn("stale_lock_worktree_mismatch", codes)

    def test_worker_view_reports_duplicate_worktrees_for_claimed_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            first = base / "worker-one"
            second = base / "worker-two"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(first), "main")
            git(repo, "worktree", "add", "--force", str(second), "worker/TASK-01")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=first,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        state = views[0].workspace_git_state
        self.assertIsNotNone(state)
        if state is None:
            self.fail("workspace git state missing")
        self.assertIn("duplicate_branch_worktrees", diagnostic_codes(views[0]))
        self.assertEqual(len(state.duplicate_worktrees), 2)

    def test_worker_view_reports_active_branch_already_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            base_commit = git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
            worktree = base / "worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(worktree), "main")
            (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
            git(worktree, "add", "feature.txt")
            git(worktree, "commit", "-m", "branch work")
            git(repo, "merge", "--ff-only", "worker/TASK-01")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=worktree,
                base_commit=base_commit,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        state = views[0].workspace_git_state
        self.assertIsNotNone(state)
        if state is None:
            self.fail("workspace git state missing")
        self.assertIn("branch_already_merged", diagnostic_codes(views[0]))
        self.assertEqual(state.merged_into, ("main",))

    def test_worker_view_reports_active_branch_contained_in_origin_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            base_commit = git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
            worktree = base / "worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(worktree), "main")
            (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
            git(worktree, "add", "feature.txt")
            git(worktree, "commit", "-m", "branch work")
            branch_head = git(
                repo,
                "rev-parse",
                "--verify",
                "worker/TASK-01",
            ).stdout.strip()
            git(repo, "update-ref", "refs/remotes/origin/main", branch_head)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=worktree,
                base_commit=base_commit,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        state = views[0].workspace_git_state
        self.assertIsNotNone(state)
        if state is None:
            self.fail("workspace git state missing")
        self.assertIn("branch_already_merged", diagnostic_codes(views[0]))
        self.assertEqual(state.merged_into, ("origin/main",))

    def test_branch_containment_uses_branch_ref_when_tag_name_collides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            base_commit = git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
            worktree = base / "worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(worktree), "main")
            (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
            git(worktree, "add", "feature.txt")
            git(worktree, "commit", "-m", "branch work")
            git(repo, "tag", "worker/TASK-01", base_commit)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=worktree,
                base_commit=base_commit,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        state = views[0].workspace_git_state
        self.assertIsNotNone(state)
        if state is None:
            self.fail("workspace git state missing")
        self.assertNotIn("branch_already_merged", diagnostic_codes(views[0]))
        self.assertEqual(state.merged_into, ())
        self.assertEqual(state.status, "ok")

    def test_worker_view_reports_dirty_claimed_worktree_as_foreign_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            worktree = base / "worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(worktree), "main")
            (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
            git(worktree, "add", "feature.txt")
            git(worktree, "commit", "-m", "branch work")
            (worktree / "notes.txt").write_text("not committed\n", encoding="utf-8")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/TASK-01",
                worktree=worktree,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        state = views[0].workspace_git_state
        self.assertIsNotNone(state)
        if state is None:
            self.fail("workspace git state missing")
        self.assertIn("foreign_dirty_claimed_worktree", diagnostic_codes(views[0]))
        self.assertTrue(state.dirty)
        self.assertTrue(any("notes.txt" in line for line in state.dirty_summary))

    def test_worker_view_reports_stale_lock_to_worktree_branch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            worktree = base / "worker"
            git(repo, "worktree", "add", "-b", "worker/TASK-01", str(worktree), "main")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="TASK-01",
                run_id="run-1",
                branch="worker/OTHER",
                worktree=worktree,
            )

            views = build_worker_views(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: True,
            )

        codes = diagnostic_codes(views[0])
        self.assertIn("stale_lock_worktree_mismatch", codes)
        self.assertIn("claimed_branch_missing", codes)
        self.assertEqual(views[0].workspace_git_state.status, "stale")

    def test_worktree_porcelain_parser_uses_short_branch_names(self) -> None:
        entries = parse_git_worktree_list(
            "worktree /tmp/repo\nHEAD abc123\nbranch refs/heads/worker/TASK-01\n\n"
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].branch, "worker/TASK-01")


def init_git_repo(repo: Path) -> None:
    repo.mkdir()
    git(repo, "init")
    git(repo, "checkout", "-B", "main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.com")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "baseline")


def acquire_worker_lock(
    manager: LockManager,
    *,
    repo: Path,
    task_id: str,
    run_id: str,
    branch: str,
    worktree: Path,
    base_commit: str = "",
) -> None:
    claim_base = (
        base_commit or git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
    )
    state = ActiveRunState(
        task_id=task_id,
        run_id=run_id,
        worker_pid=100,
        supervisor_pid=200,
        host="test-host",
        started_at="2026-05-09T00:00:00+00:00",
        log_path=repo / ".vibe-loop" / "runs" / f"{run_id}.log",
        base_main=claim_base,
        command=f"agent {task_id}",
        workspace=WorkspaceClaim(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree.resolve(),
            base_commit=claim_base,
            head_commit=head_commit_for_claim(worktree),
            current_branch=branch,
            dirty=False,
            dirty_summary=(),
            claimed_at="2026-05-09T00:01:00+00:00",
        ),
    )
    manager.acquire(task_id, run_id, metadata=state.to_lock_metadata())


def head_commit_for_claim(worktree: Path) -> str:
    if not worktree.exists():
        return ""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def diagnostic_codes(view) -> set[str]:
    return {diagnostic.code for diagnostic in view.workspace_diagnostics}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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
        self.assertEqual(stale[0].task_id, "TASK-01")
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


def write_command_lock_adapter(path: Path) -> None:
    path.write_text(
        "from __future__ import annotations\n"
        "\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "lock_root = Path(os.environ['VIBE_LOOP_LOCK_ROOT'])\n"
        "store_path = lock_root.parent / 'command-lock-store.json'\n"
        "operation = os.environ['VIBE_LOOP_LOCK_OPERATION']\n"
        "task_id = os.environ['VIBE_LOOP_LOCK_TASK_ID']\n"
        "run_id = os.environ.get('VIBE_LOOP_LOCK_RUN_ID', '')\n"
        "metadata = json.loads(os.environ.get('VIBE_LOOP_LOCK_METADATA_JSON') or '{}')\n"
        "\n"
        "def read_store():\n"
        "    if not store_path.exists():\n"
        "        return {}\n"
        "    return json.loads(store_path.read_text(encoding='utf-8'))\n"
        "\n"
        "def write_store(store):\n"
        "    store_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    store_path.write_text(json.dumps(store, sort_keys=True), encoding='utf-8')\n"
        "\n"
        "store = read_store()\n"
        "if operation in {'acquire', 'update'}:\n"
        "    existing = store.get(task_id)\n"
        "    if operation == 'acquire' and existing and existing.get('run_id') != run_id:\n"
        "        print(json.dumps({'acquired': False, 'metadata': existing}))\n"
        "    else:\n"
        "        metadata['task_id'] = task_id\n"
        "        if run_id:\n"
        "            metadata['run_id'] = run_id\n"
        "        metadata.setdefault('path', str(lock_root / f'{task_id}.lock'))\n"
        "        store[task_id] = metadata\n"
        "        write_store(store)\n"
        "        print(json.dumps({'acquired': True, 'updated': operation == 'update', 'metadata': metadata}))\n"
        "elif operation == 'release':\n"
        "    released = task_id in store\n"
        "    store.pop(task_id, None)\n"
        "    write_store(store)\n"
        "    print(json.dumps({'released': released}))\n"
        "elif operation == 'status':\n"
        "    current = store.get(task_id)\n"
        "    print(json.dumps({'locked': current is not None, 'metadata': current or {}}))\n"
        "elif operation == 'list':\n"
        "    print(json.dumps({'locks': list(store.values())}))\n"
        "else:\n"
        "    raise SystemExit(f'unsupported operation: {operation}')\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
