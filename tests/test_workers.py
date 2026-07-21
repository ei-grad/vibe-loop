from __future__ import annotations

import dataclasses
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
from vibe_loop.orchestration import RunStage, StageTransition
from vibe_loop.runs import (
    LOCK_ACQUIRED_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    WorkerReport,
)
from vibe_loop.workers import (
    KEEP_DIRTY_WORKTREE,
    KEEP_EVIDENCE_CHANGED,
    KEEP_GIT_STATE_UNAVAILABLE,
    KEEP_LIVE_CLAIM,
    KEEP_OWNERSHIP_UNVERIFIED,
    KEEP_PRIMARY_WORKTREE,
    KEEP_REMOTE_MAIN_NOT_CONTAINED,
    KEEP_REMOTE_MAIN_UNAVAILABLE,
    KEEP_STALE_CLAIM,
    KEEP_TERMINAL_COMMIT_MISMATCH,
    KEEP_TERMINAL_STATUS_UNSUCCESSFUL,
    KEEP_UNMERGED_WORKTREE,
    ActiveRunState,
    StaleLock,
    WorkerView,
    WorkspaceClaim,
    WorktreeDispositionDecision,
    WorktreeDispositionEvidence,
    build_worker_views,
    classify_process,
    clean_stale_locks,
    collect_stale_locks,
    collect_worktree_disposition_evidence,
    execute_worktree_disposition,
    load_active_run_states,
    parse_git_worktree_list,
    parse_worktree_disposition_decisions,
    pending_settlements_by_run_id,
    restore_projected_worker_process_identity,
)


class WorkerStateTests(unittest.TestCase):
    def test_fencing_mismatch_exception_message_redacts_tokens(self) -> None:
        expected_token = "expected-exception-fencing-canary"
        actual_token = "actual-exception-fencing-canary"

        mismatch = LockFencingMismatch(
            Path("lock-path"),
            {"fencing_token": actual_token},
            expected_token=expected_token,
            actual_token=actual_token,
        )

        self.assertNotIn(expected_token, str(mismatch))
        self.assertNotIn(actual_token, str(mismatch))
        self.assertIn("fencing token mismatch", str(mismatch))

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
                agent_profile="claude-opus",
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
        self.assertEqual(loaded[0].agent_profile, "claude-opus")
        self.assertEqual(loaded[0].agent_prompt_dialect, "codex")
        self.assertEqual(loaded[0].agent_prompt_dialect_source, "agent.kind:codex")
        self.assertEqual(loaded[0].agent_skill_ref_prefix, "$")
        self.assertEqual(loaded[0].agent_skill_ref_prefix_source, "agent.kind:codex")
        self.assertEqual(loaded[0].model_provider, "openai")
        self.assertEqual(loaded[0].model_provider_source, "command_executable:codex")
        self.assertEqual(loaded[0].model_id, "gpt-5.5")
        self.assertEqual(loaded[0].model_id_source, "native:stdout:json.model")
        self.assertEqual(loaded[0].reasoning_effort, "high")
        self.assertEqual(task_lock.metadata["model"], "gpt-5.5")
        self.assertEqual(task_lock.metadata["effort"], "high")
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

    def test_command_lock_backend_failure_redacts_fencing_metadata(self) -> None:
        fencing_token = "987654321012345679"
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            failing = repo / "failing_lock_adapter.py"
            failing.write_text(
                "import os\n"
                "import sys\n"
                "print('adapter unavailable metadata=' + "
                "os.environ['VIBE_LOOP_LOCK_METADATA_JSON'], file=sys.stderr)\n"
                "raise SystemExit(17)\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {json.dumps(str(failing))}"
            lock_root = repo / ".vibe-loop" / "locks"
            token_root = lock_root / ".fencing-tokens"
            token_root.mkdir(parents=True)
            (token_root / "TASK-01.token").write_text(
                "987654321012345678\n", encoding="utf-8"
            )
            manager = build_lock_manager(
                repo,
                lock_root,
                LockConfig(
                    type="command",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            with self.assertRaises(LockBackendError) as raised:
                manager.acquire("TASK-01", "run-1")

        diagnostic = str(raised.exception)
        self.assertIn("locks.acquire_command exited with status 17", diagnostic)
        self.assertIn("adapter unavailable", diagnostic)
        self.assertIn('"fencing_token":"<redacted>"', diagnostic)
        self.assertNotIn(fencing_token, diagnostic)

    def test_command_lock_backend_redaction_preserves_low_entropy_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            failing = repo / "failing_lock_adapter.py"
            failing.write_text(
                "import os\n"
                "import sys\n"
                "print('adapter unavailable task=TASK-01 attempt=1 metadata=' + "
                "os.environ['VIBE_LOOP_LOCK_METADATA_JSON'], file=sys.stderr)\n"
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

            with self.assertRaises(LockBackendError) as raised:
                manager.acquire("TASK-01", "run-1")

        diagnostic = str(raised.exception)
        self.assertIn("task=TASK-01 attempt=1", diagnostic)
        self.assertIn('"fencing_token":"<redacted>"', diagnostic)

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

    def test_recycled_worker_pid_does_not_read_as_the_recorded_worker(self) -> None:
        state = ActiveRunState(
            task_id="PAR-02",
            run_id="run-1",
            worker_pid=100,
            worker_process_group_id=100,
            worker_session_id=100,
            worker_process_birth_id="boot-id:500",
            host="test-host",
            started_at="2026-05-09T00:00:00+00:00",
            log_path=Path("run.log"),
            base_main="abc123",
            command="agent PAR-02",
        )

        self.assertEqual(
            classify_process(
                state,
                "test-host",
                process_exists=lambda pid: True,
                birth_identity_lookup=lambda pid: "boot-id:500",
            ),
            "running",
        )
        self.assertEqual(
            classify_process(
                state,
                "test-host",
                process_exists=lambda pid: True,
                birth_identity_lookup=lambda pid: "boot-id:900",
            ),
            "missing",
        )
        # A run recorded before birth identities existed keeps the plain
        # existence check rather than degrading to "missing".
        legacy = dataclasses.replace(state, worker_process_birth_id="")
        self.assertEqual(
            classify_process(
                legacy,
                "test-host",
                process_exists=lambda pid: True,
                birth_identity_lookup=lambda pid: "boot-id:900",
            ),
            "running",
        )

    def test_worker_view_reports_birth_identity_presence_not_its_value(self) -> None:
        state = ActiveRunState(
            task_id="PAR-02",
            run_id="run-1",
            worker_pid=100,
            worker_process_group_id=100,
            worker_session_id=100,
            worker_process_birth_id="boot-id-canary:500",
            host="test-host",
            started_at="2026-05-09T00:00:00+00:00",
            log_path=Path("run.log"),
            base_main="abc123",
            command="agent PAR-02",
        )

        payload = json.dumps(
            WorkerView(active=state, state="running", process_state="running").to_json()
        )

        # The birth ID embeds the host boot ID, so status must never render it.
        self.assertNotIn("boot-id-canary", payload)
        self.assertTrue(json.loads(payload)["worker_process_birth_id_known"])

    def test_worker_identity_round_trips_through_lock_metadata(self) -> None:
        state = ActiveRunState.new(
            task_id="PAR-03",
            run_id="run-2",
            log_path=Path("run.log"),
            base_main="abc123",
            command="agent PAR-03",
        ).with_worker_pid(
            321,
            process_group_id=321,
            session_id=320,
            process_birth_id="boot-id:777",
        )

        restored = ActiveRunState.from_lock_metadata(state.to_lock_metadata())

        self.assertEqual(restored.worker_pid, 321)
        self.assertEqual(restored.worker_process_group_id, 321)
        self.assertEqual(restored.worker_session_id, 320)
        self.assertEqual(restored.worker_process_birth_id, "boot-id:777")

    def test_worker_view_restores_identity_quarantined_from_command_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            projected = ActiveRunState.new(
                task_id="PAR-03",
                run_id="run-2",
                log_path=Path("run.log"),
                base_main="abc123",
                command="agent PAR-03",
            ).with_worker_pid(321)
            manager.acquire(
                "PAR-03",
                "run-2",
                metadata=projected.to_lock_metadata(),
            )
            run_store.append_lifecycle_event(
                RunLifecycleEvent.worker_process_started(
                    run_id="run-2",
                    task_id="PAR-03",
                    worker_pid=321,
                    supervisor_pid=projected.supervisor_pid or 1,
                    process_group_id=321,
                    session_id=320,
                    process_birth_id="boot-id:777",
                    host=projected.host,
                )
            )

            views = build_worker_views(
                manager,
                run_store,
                current_host=projected.host,
                process_exists=lambda pid: True,
            )

        restored = views[0].active
        self.assertEqual(restored.worker_process_group_id, 321)
        self.assertEqual(restored.worker_session_id, 320)
        self.assertEqual(restored.worker_process_birth_id, "boot-id:777")
        self.assertTrue(views[0].to_json()["worker_process_birth_id_known"])

    def test_worker_identity_record_requires_exact_projected_owner(self) -> None:
        active = dataclasses.replace(
            ActiveRunState.new(
                task_id="PAR-03",
                run_id="run-2",
                log_path=Path("run.log"),
                base_main="abc123",
                command="agent PAR-03",
            ).with_worker_pid(321),
            supervisor_pid=100,
            host="test-host",
        )
        mismatched = RunLifecycleEvent.worker_process_started(
            run_id="run-2",
            task_id="PAR-03",
            worker_pid=321,
            supervisor_pid=999,
            process_group_id=321,
            session_id=321,
            process_birth_id="boot-id:777",
            host="test-host",
        ).to_record()

        cases = {
            "missing_host": dataclasses.replace(active, host=""),
            "missing_supervisor": dataclasses.replace(active, supervisor_pid=None),
            "mismatched_host": dataclasses.replace(active, host="other-host"),
            "mismatched_supervisor": active,
        }
        for name, projected in cases.items():
            with self.subTest(name=name):
                restored = restore_projected_worker_process_identity(
                    projected,
                    [mismatched],
                )

                self.assertEqual(restored, projected)
                self.assertEqual(restored.worker_process_birth_id, "")

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
            run_store.append_lifecycle_event(
                RunLifecycleEvent.stage_transition(
                    run_id="run-1",
                    task_id="PAR-03",
                    transition=StageTransition(
                        from_stage=RunStage.WORKSPACE,
                        to_stage=RunStage.IMPLEMENTING,
                        reason="worker_process_launch",
                        ordinal=1,
                        accepted=True,
                    ),
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
        self.assertEqual(payload["stage"], "implementing")
        self.assertEqual(payload["stage_ordinal"], 1)

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
        self.assertEqual(stale[0].settled_outcome, "completed")

    def test_collect_does_not_apply_an_unrelated_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            state = ActiveRunState(
                task_id="TASK-01",
                run_id="retained-run",
                worker_pid=100,
                host="test-host",
                started_at="2026-05-09T00:00:00+00:00",
                log_path=repo / ".vibe-loop" / "runs" / "retained-run.log",
                base_main="abc123",
                command="agent TASK-01",
            )
            manager.acquire(
                "TASK-01", "retained-run", metadata=state.to_lock_metadata()
            )
            run_store.append_result(
                RunResult(
                    run_id="other-run",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "other-run.log",
                    start_main="abc123",
                    end_main="def456",
                )
            )

            stale = collect_stale_locks(
                manager,
                run_store,
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].run_id, "retained-run")
        self.assertEqual(stale[0].settled_outcome, "")

    def test_terminal_result_fallback_is_same_run_and_terminal_only(self) -> None:
        def result(run_id: str, classification: str) -> dict[str, object]:
            return RunResult(
                run_id=run_id,
                task_id=f"task-{run_id}",
                classification=classification,
                exit_code=0,
                log_path=Path(f"{run_id}.log"),
                start_main="abc123",
                end_main="def456",
            ).to_record()

        pending = pending_settlements_by_run_id(
            [
                result("completed-run", "completed"),
                result("failed-run", "failed"),
                result("blocked-run", "blocked"),
                result("unknown-run", "unknown"),
                result("timed-out-run", "timed_out"),
            ]
        )

        self.assertEqual(
            pending,
            {
                "completed-run": ("completed", "completed"),
                "failed-run": ("failed", "failed"),
                "blocked-run": ("blocked", "blocked"),
            },
        )
        self.assertNotIn("unknown-run", pending)
        self.assertNotIn("timed-out-run", pending)

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


def make_disposition_evidence(
    path: Path,
    *,
    branch: str = "worker/TASK",
    head_commit: str = "deadbee",
    is_primary: bool = False,
    merged: bool = True,
    merged_into: tuple[str, ...] = ("main",),
    dirty: bool = False,
    dirty_summary: tuple[str, ...] = (),
    git_state_error: str = "",
    claiming_run_id: str = "",
    claiming_task_id: str = "",
    claim_state: str = "",
    claim_is_live: bool = False,
    ownership_error: str = "",
    terminal_status: str = "completed",
    terminal_commit: str = "deadbee",
) -> WorktreeDispositionEvidence:
    return WorktreeDispositionEvidence(
        path=path,
        branch=branch,
        head_commit=head_commit,
        is_primary=is_primary,
        local_main_contained=merged,
        remote_main_contained=merged,
        remote_main_error="",
        merged_into=merged_into if merged else (),
        dirty=dirty,
        dirty_summary=dirty_summary,
        git_state_error=git_state_error,
        claiming_run_id=claiming_run_id,
        claiming_task_id=claiming_task_id,
        claim_state=claim_state,
        claim_is_live=claim_is_live,
        ownership_error=ownership_error,
        terminal_status=terminal_status,
        terminal_commit=terminal_commit,
    )


class WorktreeDispositionEvidenceTests(unittest.TestCase):
    def test_evidence_collects_merged_dirty_claim_and_liveness_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")

            merged_tree = base / "merged"
            git(
                repo, "worktree", "add", "-b", "worker/MERGED", str(merged_tree), "main"
            )
            (merged_tree / "feature.txt").write_text("done\n", encoding="utf-8")
            git(merged_tree, "add", "feature.txt")
            git(merged_tree, "commit", "-m", "merged work")
            git(repo, "merge", "--ff-only", "worker/MERGED")
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="MERGED",
                run_id="run-merged",
                branch="worker/MERGED",
                worktree=merged_tree,
            )

            wip_tree = base / "wip"
            git(repo, "worktree", "add", "-b", "worker/WIP", str(wip_tree), "main")
            (wip_tree / "draft.txt").write_text("in progress\n", encoding="utf-8")
            git(wip_tree, "add", "draft.txt")
            git(wip_tree, "commit", "-m", "wip work")
            (wip_tree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

            evidence = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: pid == 100,
            )

        by_path = {item.path: item for item in evidence}
        self.assertEqual(len(evidence), 3)

        primary = by_path[repo.resolve()]
        self.assertTrue(primary.is_primary)

        merged = by_path[merged_tree.resolve()]
        self.assertFalse(merged.is_primary)
        self.assertTrue(merged.local_main_contained)
        self.assertFalse(merged.remote_main_contained)
        self.assertIn("main", merged.merged_into)
        self.assertFalse(merged.dirty)
        self.assertEqual(merged.claiming_run_id, "run-merged")
        self.assertEqual(merged.claiming_task_id, "MERGED")
        self.assertTrue(merged.claim_is_live)
        self.assertFalse(merged.reapable)
        self.assertIn(KEEP_LIVE_CLAIM, merged.keep_guardrails)
        self.assertIn(KEEP_REMOTE_MAIN_UNAVAILABLE, merged.keep_guardrails)
        self.assertIn(KEEP_TERMINAL_STATUS_UNSUCCESSFUL, merged.keep_guardrails)

        wip = by_path[wip_tree.resolve()]
        self.assertFalse(wip.merged)
        self.assertTrue(wip.dirty)
        self.assertEqual(wip.claiming_run_id, "")
        self.assertFalse(wip.claim_is_live)
        self.assertFalse(wip.reapable)
        self.assertIn(KEEP_DIRTY_WORKTREE, wip.keep_guardrails)
        self.assertIn(KEEP_UNMERGED_WORKTREE, wip.keep_guardrails)

    def test_evidence_reaps_only_remote_contained_completed_released_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")

            orphan = base / "orphan"
            git(repo, "worktree", "add", "-b", "worker/ORPHAN", str(orphan), "main")
            (orphan / "feature.txt").write_text("done\n", encoding="utf-8")
            git(orphan, "add", "feature.txt")
            git(orphan, "commit", "-m", "orphan work")
            git(repo, "merge", "--ff-only", "worker/ORPHAN")
            git(repo, "update-ref", "refs/remotes/origin/main", "main")
            head = git(orphan, "rev-parse", "HEAD").stdout.strip()
            claim = WorkspaceClaim(
                task_id="ORPHAN",
                run_id="run-orphan",
                branch="worker/ORPHAN",
                worktree=orphan,
                base_commit=head,
                head_commit=head,
                current_branch="worker/ORPHAN",
                dirty=False,
                dirty_summary=(),
            )
            run_store.append_record(claim.to_json())
            run_store.append_report(
                WorkerReport(
                    task_id="ORPHAN",
                    run_id="run-orphan",
                    status="completed",
                    commit=head,
                )
            )

            evidence = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        by_path = {item.path: item for item in evidence}
        orphan_evidence = by_path[orphan.resolve()]
        self.assertEqual(orphan_evidence.claiming_run_id, "run-orphan")
        self.assertFalse(orphan_evidence.claim_is_live)
        self.assertEqual(orphan_evidence.claim_state, "released")
        self.assertTrue(orphan_evidence.local_main_contained)
        self.assertTrue(orphan_evidence.remote_main_contained)
        self.assertEqual(orphan_evidence.terminal_status, "completed")
        self.assertTrue(orphan_evidence.reapable)

    def test_evidence_preserves_crash_windows_before_completed_remote_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")

            orphan = base / "orphan"
            git(repo, "worktree", "add", "-b", "worker/ORPHAN", str(orphan), "main")
            (orphan / "feature.txt").write_text("done\n", encoding="utf-8")
            git(orphan, "add", "feature.txt")
            git(orphan, "commit", "-m", "orphan work")
            head = git(orphan, "rev-parse", "HEAD").stdout.strip()
            git(repo, "merge", "--ff-only", "worker/ORPHAN")
            claim = WorkspaceClaim(
                task_id="ORPHAN",
                run_id="run-orphan",
                branch="worker/ORPHAN",
                worktree=orphan,
                base_commit=head,
                head_commit=head,
                current_branch="worker/ORPHAN",
                dirty=False,
                dirty_summary=(),
            )
            run_store.append_record(claim.to_json())

            before_push = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            git(repo, "update-ref", "refs/remotes/origin/main", "main")
            before_report = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: False,
            )
            run_store.append_report(
                WorkerReport(
                    task_id="ORPHAN",
                    run_id="run-orphan",
                    status="completed",
                    commit=head,
                )
            )
            acquire_worker_lock(
                manager,
                repo=repo,
                task_id="ORPHAN",
                run_id="run-orphan",
                branch="worker/ORPHAN",
                worktree=orphan,
            )
            during_stale_lock_recovery = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        before_push_item = {item.path: item for item in before_push}[orphan.resolve()]
        before_report_item = {item.path: item for item in before_report}[
            orphan.resolve()
        ]
        stale_item = {item.path: item for item in during_stale_lock_recovery}[
            orphan.resolve()
        ]
        self.assertIn(KEEP_REMOTE_MAIN_UNAVAILABLE, before_push_item.keep_guardrails)
        self.assertIn(
            KEEP_TERMINAL_STATUS_UNSUCCESSFUL,
            before_report_item.keep_guardrails,
        )
        self.assertIn(KEEP_STALE_CLAIM, stale_item.keep_guardrails)

    def test_evidence_preserves_ambiguous_or_unsuccessful_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            orphan = base / "orphan"
            git(repo, "worktree", "add", "-b", "worker/ORPHAN", str(orphan), "main")
            (orphan / "feature.txt").write_text("done\n", encoding="utf-8")
            git(orphan, "add", "feature.txt")
            git(orphan, "commit", "-m", "orphan work")
            git(repo, "merge", "--ff-only", "worker/ORPHAN")
            git(repo, "update-ref", "refs/remotes/origin/main", "main")
            head = git(orphan, "rev-parse", "HEAD").stdout.strip()
            for task_id, run_id, status in (
                ("ORPHAN", "run-one", "blocked"),
                ("OTHER", "run-two", "completed"),
            ):
                run_store.append_record(
                    WorkspaceClaim(
                        task_id=task_id,
                        run_id=run_id,
                        branch="worker/ORPHAN",
                        worktree=orphan,
                        base_commit=head,
                        head_commit=head,
                        current_branch="worker/ORPHAN",
                        dirty=False,
                        dirty_summary=(),
                    ).to_json()
                )
                run_store.append_report(
                    WorkerReport(
                        task_id=task_id,
                        run_id=run_id,
                        status=status,
                        commit=head,
                    )
                )

            evidence = collect_worktree_disposition_evidence(
                manager,
                run_store,
                repo=repo,
                main_branch="main",
                current_host="test-host",
                process_exists=lambda pid: False,
            )

        item = {item.path: item for item in evidence}[orphan.resolve()]
        self.assertFalse(item.reapable)
        self.assertIn(KEEP_OWNERSHIP_UNVERIFIED, item.keep_guardrails)


class FakeWorktreeSideEffects:
    def __init__(self, *, remove_error: str = "", delete_error: str = "") -> None:
        self.remove_error = remove_error
        self.delete_error = delete_error
        self.removed: list[Path] = []
        self.deleted: list[str] = []

    def remove_worktree(self, worktree: Path) -> str:
        self.removed.append(worktree)
        return self.remove_error

    def delete_branch(self, branch: str) -> str:
        self.deleted.append(branch)
        return self.delete_error


class WorktreeDispositionExecuteTests(unittest.TestCase):
    def test_reaps_orphaned_merged_clean_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/orphan"), branch="worker/ORPHAN")
        ]
        decisions = [
            WorktreeDispositionDecision(
                worktree=Path("/tmp/orphan"),
                action="reap",
                reason="worker process missing and branch merged",
            )
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].applied, "reaped")
        self.assertEqual(outcomes[0].requested, "reap")
        self.assertEqual(effects.removed, [Path("/tmp/orphan")])
        self.assertEqual(effects.deleted, ["worker/ORPHAN"])
        kinds = [action.kind for action in outcomes[0].actions]
        self.assertEqual(kinds, ["worktree_remove", "branch_delete"])
        self.assertTrue(all(action.ok for action in outcomes[0].actions))

    def test_revalidates_before_each_destructive_action(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/orphan"), branch="worker/ORPHAN")
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/orphan"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()
        actions: list[str] = []

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
            revalidate=lambda _item, action: actions.append(action) or (),
        )

        self.assertEqual(outcomes[0].applied, "reaped")
        self.assertEqual(actions, ["worktree_remove", "branch_delete"])

    def test_refuses_reap_when_revalidation_changes_evidence(self) -> None:
        evidence = [make_disposition_evidence(Path("/tmp/orphan"))]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/orphan"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
            revalidate=lambda _item, _action: (KEEP_EVIDENCE_CHANGED,),
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_EVIDENCE_CHANGED, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])
        self.assertEqual(effects.deleted, [])

    def test_refuses_to_reap_dirty_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(
                Path("/tmp/dirty"),
                dirty=True,
                dirty_summary=(" M src/example.py",),
            )
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/dirty"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_DIRTY_WORKTREE, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])
        self.assertEqual(effects.deleted, [])

    def test_refuses_to_reap_unmerged_worktree(self) -> None:
        evidence = [make_disposition_evidence(Path("/tmp/wip"), merged=False)]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/wip"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_UNMERGED_WORKTREE, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])

    def test_refuses_terminal_statuses_other_than_completed(self) -> None:
        for status in ("", "blocked", "failed", "unknown"):
            with self.subTest(status=status or "missing_report"):
                effects = FakeWorktreeSideEffects()
                outcomes = execute_worktree_disposition(
                    [
                        make_disposition_evidence(
                            Path("/tmp/orphan"),
                            terminal_status=status,
                        )
                    ],
                    [
                        WorktreeDispositionDecision(
                            worktree=Path("/tmp/orphan"), action="reap"
                        )
                    ],
                    remove_worktree=effects.remove_worktree,
                    delete_branch=effects.delete_branch,
                )

                self.assertEqual(outcomes[0].applied, "refused")
                self.assertIn(
                    KEEP_TERMINAL_STATUS_UNSUCCESSFUL,
                    outcomes[0].guardrails,
                )
                self.assertEqual(effects.removed, [])

    def test_refuses_completed_report_for_a_different_commit(self) -> None:
        effects = FakeWorktreeSideEffects()
        outcomes = execute_worktree_disposition(
            [
                make_disposition_evidence(
                    Path("/tmp/orphan"),
                    terminal_commit="other-commit",
                )
            ],
            [WorktreeDispositionDecision(worktree=Path("/tmp/orphan"), action="reap")],
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_TERMINAL_COMMIT_MISMATCH, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])

    def test_refuses_missing_or_noncontained_remote_main(self) -> None:
        for remote_error, remote_contained, expected in (
            ("remote main ref is unavailable", False, KEEP_REMOTE_MAIN_UNAVAILABLE),
            ("", False, KEEP_REMOTE_MAIN_NOT_CONTAINED),
        ):
            with self.subTest(expected=expected):
                effects = FakeWorktreeSideEffects()
                outcomes = execute_worktree_disposition(
                    [
                        dataclasses.replace(
                            make_disposition_evidence(Path("/tmp/orphan")),
                            remote_main_error=remote_error,
                            remote_main_contained=remote_contained,
                        )
                    ],
                    [
                        WorktreeDispositionDecision(
                            worktree=Path("/tmp/orphan"), action="reap"
                        )
                    ],
                    remove_worktree=effects.remove_worktree,
                    delete_branch=effects.delete_branch,
                )

                self.assertEqual(outcomes[0].applied, "refused")
                self.assertIn(expected, outcomes[0].guardrails)
                self.assertEqual(effects.removed, [])

    def test_refuses_to_reap_live_claimed_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(
                Path("/tmp/live"),
                claiming_run_id="run-live",
                claim_state="running",
                claim_is_live=True,
            )
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/live"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_LIVE_CLAIM, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])

    def test_refuses_to_reap_primary_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(
                Path("/tmp/repo"),
                branch="main",
                is_primary=True,
            )
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/repo"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_PRIMARY_WORKTREE, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])

    def test_keep_decision_and_missing_decision_keep_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/keep")),
            make_disposition_evidence(Path("/tmp/no-decision")),
        ]
        decisions = [
            WorktreeDispositionDecision(
                worktree=Path("/tmp/keep"),
                action="keep",
                reason="agent chose to keep",
            )
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        by_path = {outcome.worktree: outcome for outcome in outcomes}
        self.assertEqual(by_path[Path("/tmp/keep")].applied, "kept")
        self.assertEqual(by_path[Path("/tmp/keep")].requested, "keep")
        self.assertEqual(by_path[Path("/tmp/no-decision")].applied, "kept")
        self.assertEqual(by_path[Path("/tmp/no-decision")].requested, "none")
        self.assertEqual(effects.removed, [])

    def test_records_failed_reap_when_side_effect_errors(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/orphan"), branch="worker/ORPHAN")
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/orphan"), action="reap")
        ]
        effects = FakeWorktreeSideEffects(remove_error="worktree is locked")

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "failed")
        self.assertEqual(len(outcomes[0].actions), 1)
        self.assertFalse(outcomes[0].actions[0].ok)
        self.assertEqual(outcomes[0].actions[0].error, "worktree is locked")
        self.assertEqual(effects.deleted, [])

    def test_refuses_to_reap_git_unreadable_worktree(self) -> None:
        evidence = [
            make_disposition_evidence(
                Path("/tmp/unreadable"),
                git_state_error="claimed worktree path does not exist",
            )
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/unreadable"), action="reap")
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "refused")
        self.assertIn(KEEP_GIT_STATE_UNAVAILABLE, outcomes[0].guardrails)
        self.assertEqual(effects.removed, [])

    def test_records_failed_reap_when_branch_delete_errors(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/orphan"), branch="worker/ORPHAN")
        ]
        decisions = [
            WorktreeDispositionDecision(worktree=Path("/tmp/orphan"), action="reap")
        ]
        effects = FakeWorktreeSideEffects(delete_error="branch not fully merged")

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        self.assertEqual(outcomes[0].applied, "failed")
        self.assertEqual(effects.removed, [Path("/tmp/orphan")])
        self.assertEqual(effects.deleted, ["worker/ORPHAN"])
        kinds = [action.kind for action in outcomes[0].actions]
        self.assertEqual(kinds, ["worktree_remove", "branch_delete"])
        self.assertTrue(outcomes[0].actions[0].ok)
        self.assertFalse(outcomes[0].actions[1].ok)
        self.assertEqual(outcomes[0].actions[1].error, "branch not fully merged")

    def test_outcome_round_trips_through_json(self) -> None:
        evidence = [
            make_disposition_evidence(Path("/tmp/orphan"), branch="worker/ORPHAN")
        ]
        decisions = [
            WorktreeDispositionDecision(
                worktree=Path("/tmp/orphan"), action="reap", reason="orphaned"
            )
        ]
        effects = FakeWorktreeSideEffects()

        outcomes = execute_worktree_disposition(
            evidence,
            decisions,
            remove_worktree=effects.remove_worktree,
            delete_branch=effects.delete_branch,
        )

        payload = json.loads(json.dumps(outcomes[0].to_json()))
        self.assertEqual(payload["worktree"], str(Path("/tmp/orphan")))
        self.assertEqual(payload["branch"], "worker/ORPHAN")
        self.assertEqual(payload["applied"], "reaped")
        self.assertEqual(payload["reason"], "orphaned")
        self.assertEqual(
            [action["kind"] for action in payload["actions"]],
            ["worktree_remove", "branch_delete"],
        )

    def test_evidence_serializes_guardrails_and_reapable_to_json(self) -> None:
        reapable = make_disposition_evidence(
            Path("/tmp/orphan"), branch="worker/ORPHAN"
        )
        kept = make_disposition_evidence(
            Path("/tmp/wip"),
            merged=False,
            dirty=True,
            dirty_summary=(" M src/example.py",),
        )

        reapable_payload = json.loads(json.dumps(reapable.to_json()))
        kept_payload = json.loads(json.dumps(kept.to_json()))

        self.assertEqual(reapable_payload["path"], str(Path("/tmp/orphan")))
        self.assertEqual(reapable_payload["keep_guardrails"], [])
        self.assertTrue(reapable_payload["reapable"])
        self.assertIn(KEEP_DIRTY_WORKTREE, kept_payload["keep_guardrails"])
        self.assertIn(KEEP_UNMERGED_WORKTREE, kept_payload["keep_guardrails"])
        self.assertFalse(kept_payload["reapable"])


class WorktreeDispositionDecisionParseTests(unittest.TestCase):
    def test_parses_decisions_list_and_skips_invalid_entries(self) -> None:
        decisions = parse_worktree_disposition_decisions(
            {
                "decisions": [
                    {
                        "worktree": "/tmp/orphan",
                        "action": "reap",
                        "reason": "orphaned",
                    },
                    {"path": "/tmp/keep", "action": "keep"},
                    {"worktree": "/tmp/bad", "action": "delete"},
                    {"action": "reap"},
                    "not-a-dict",
                ]
            }
        )

        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0].worktree, Path("/tmp/orphan").resolve())
        self.assertEqual(decisions[0].action, "reap")
        self.assertEqual(decisions[0].reason, "orphaned")
        self.assertEqual(decisions[1].worktree, Path("/tmp/keep").resolve())
        self.assertEqual(decisions[1].action, "keep")

    def test_parses_bare_list_payload(self) -> None:
        decisions = parse_worktree_disposition_decisions(
            [{"worktree": "/tmp/orphan", "action": "reap"}]
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "reap")

    def test_returns_empty_for_unexpected_payload(self) -> None:
        self.assertEqual(parse_worktree_disposition_decisions(None), [])
        self.assertEqual(parse_worktree_disposition_decisions("nope"), [])


if __name__ == "__main__":
    unittest.main()
