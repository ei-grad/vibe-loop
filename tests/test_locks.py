from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_loop import locks


class ReplaceMetadataFileTests(unittest.TestCase):
    def test_retries_transient_windows_permission_error(self) -> None:
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky(self: Path, target: Path) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(self, target)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"
            target.write_text("old", encoding="utf-8")

            with (
                patch("vibe_loop.locks.sys.platform", "win32"),
                patch("vibe_loop.locks.time.sleep") as sleep,
                patch.object(Path, "replace", flaky),
            ):
                locks.replace_metadata_file(source, target)

            self.assertEqual(calls["n"], 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(sleep.call_count, 2)

    def test_reraises_windows_permission_error_after_timeout(self) -> None:
        clock = {"now": 0.0}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"

            def advancing_sleep(delay: float) -> None:
                clock["now"] += delay

            with (
                patch("vibe_loop.locks.sys.platform", "win32"),
                patch(
                    "vibe_loop.locks.time.monotonic",
                    side_effect=lambda: clock["now"],
                ),
                patch("vibe_loop.locks.time.sleep", side_effect=advancing_sleep),
                patch.object(
                    Path,
                    "replace",
                    side_effect=PermissionError(5, "Access is denied"),
                ),
            ):
                with self.assertRaises(PermissionError):
                    locks.replace_metadata_file(source, target)

    def test_reraises_immediately_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"

            with (
                patch("vibe_loop.locks.sys.platform", "linux"),
                patch("vibe_loop.locks.time.sleep") as sleep,
                patch.object(
                    Path,
                    "replace",
                    side_effect=PermissionError(13, "denied"),
                ),
            ):
                with self.assertRaises(PermissionError):
                    locks.replace_metadata_file(source, target)

            sleep.assert_not_called()


class WriteMetadataTests(unittest.TestCase):
    def test_removes_temp_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_dir = Path(directory) / "TASK-01.lock"
            lock_dir.mkdir()

            with patch.object(
                Path,
                "replace",
                side_effect=PermissionError(13, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    locks.write_metadata(lock_dir, {"task_id": "TASK-01"})

            leftover = [
                entry.name
                for entry in lock_dir.iterdir()
                if entry.name.endswith(".tmp")
            ]
            self.assertEqual(leftover, [])


class CommandLockContextTests(unittest.TestCase):
    def test_interrupt_gracefully_terminates_adapter_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = locks.CommandLockBackend(
                repo=root,
                lock_root=root / "locks",
                acquire_command="adapter",
                release_command="adapter",
                status_command="adapter",
                list_command="adapter",
            )
            process = Mock(pid=5252)
            with (
                patch("vibe_loop.locks.subprocess.Popen", return_value=process),
                patch(
                    "vibe_loop.locks.wait_for_lock_command",
                    side_effect=KeyboardInterrupt,
                ),
                patch("vibe_loop.locks.terminate_lock_command_gracefully") as terminate,
                self.assertRaises(KeyboardInterrupt),
            ):
                backend.status("TASK-1")

        terminate.assert_called_once_with(process)

    def test_status_timeout_is_bounded_by_caller_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = f'{sys.executable} -c "import time; time.sleep(2)"'
            backend = locks.CommandLockBackend(
                repo=root,
                lock_root=root / "locks",
                acquire_command=command,
                release_command=command,
                status_command=command,
                list_command=command,
            )

            started = time.monotonic()
            with self.assertRaisesRegex(locks.LockBackendError, "timed out"):
                backend.status_with_timeout("TASK-1", timeout_seconds=0.05)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)

    def test_list_timeout_is_bounded_by_caller_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = f'{sys.executable} -c "import time; time.sleep(2)"'
            backend = locks.CommandLockBackend(
                repo=root,
                lock_root=root / "locks",
                acquire_command=command,
                release_command=command,
                status_command=command,
                list_command=command,
            )
            manager = locks.LockManager(root / "locks", backend=backend)

            started = time.monotonic()
            with self.assertRaisesRegex(locks.LockBackendError, "timed out"):
                manager.list_locks(timeout_seconds=0.05)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)

    def test_context_covers_every_operation_and_protocol_values_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.py"
            records_path = root / "records.jsonl"
            adapter.write_text(
                "import json, os\n"
                "from pathlib import Path\n"
                "record = {key: os.environ.get(key, '') for key in (\n"
                "    'PROJECT_SELECTOR', 'VIBE_LOOP_LOCK_OPERATION',\n"
                "    'VIBE_LOOP_LOCK_TASK_ID', 'VIBE_LOOP_LOCK_RUN_ID')}\n"
                f"path = Path({str(records_path)!r})\n"
                "with path.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps(record) + '\\n')\n"
                "operation = record['VIBE_LOOP_LOCK_OPERATION']\n"
                "metadata = json.loads(os.environ['VIBE_LOOP_LOCK_METADATA_JSON'])\n"
                "if operation == 'list':\n"
                "    print('[]')\n"
                "elif operation == 'status':\n"
                "    print(json.dumps({'locked': False}))\n"
                "elif operation == 'release':\n"
                "    print(json.dumps({'released': True}))\n"
                "elif operation == 'update':\n"
                "    print(json.dumps({'updated': True, 'metadata': metadata}))\n"
                "else:\n"
                "    print(json.dumps({'acquired': True, 'metadata': metadata}))\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {adapter}"
            backend = locks.CommandLockBackend(
                repo=root,
                lock_root=root / "locks",
                acquire_command=command,
                release_command=command,
                status_command=command,
                list_command=command,
                runtime_context={
                    "PROJECT_SELECTOR": "entry-selector",
                    "VIBE_LOOP_LOCK_OPERATION": "spoofed-operation",
                    "VIBE_LOOP_LOCK_TASK_ID": "spoofed-task",
                    "VIBE_LOOP_LOCK_RUN_ID": "spoofed-run",
                },
            )

            with patch.dict(os.environ, {"PROJECT_SELECTOR": "host-selector"}):
                task_lock = backend.acquire("TASK-1", "run-1")
                task_lock = backend.update(task_lock, dict(task_lock.metadata))
                backend.release(task_lock)
                self.assertIsNone(backend.status("TASK-2"))
                self.assertEqual(backend.list_locks(), [])
                inherited_after = os.environ["PROJECT_SELECTOR"]

            records = [
                json.loads(line)
                for line in records_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(inherited_after, "host-selector")
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_OPERATION"] for record in records],
            ["acquire", "update", "release", "status", "list"],
        )
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_TASK_ID"] for record in records],
            ["TASK-1", "TASK-1", "TASK-1", "TASK-2", ""],
        )
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_RUN_ID"] for record in records],
            ["run-1", "run-1", "run-1", "", ""],
        )
        self.assertTrue(
            all(record["PROJECT_SELECTOR"] == "entry-selector" for record in records)
        )


class AutopilotRecoveryTests(unittest.TestCase):
    class Backend:
        def __init__(self, root: Path, metadata: dict[str, object]):
            self.root = root
            self.metadata = dict(metadata)
            self.released: locks.TaskLock | None = None

        def path_for(self, task_id: str) -> Path:
            return self.root / f"{task_id}.lock"

        def acquire(self, task_id, run_id, metadata=None):
            raise AssertionError("not used")

        def update(self, task_lock, metadata):
            raise AssertionError("not used")

        def release(self, task_lock: locks.TaskLock) -> None:
            self.released = task_lock
            self.metadata = {}

        def status(self, task_id: str) -> dict[str, object] | None:
            return dict(self.metadata) if self.metadata else None

        def list_locks(self):
            return [dict(self.metadata)] if self.metadata else []

    def _manager(self, root: Path, *, token: str = "fence-1"):
        metadata: dict[str, object] = {
            "task_id": locks.AUTOPILOT_LOCK_NAME,
            "run_id": "autopilot-1",
            "pid": 4321,
            "host": socket.gethostname(),
        }
        if token:
            metadata["fencing_token"] = token
        backend = self.Backend(root, metadata)
        return locks.LockManager(root, backend=backend), backend

    def test_recovery_releases_through_backend_with_exact_fencing_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self._manager(Path(directory))

            released = manager.recover_stale_autopilot(
                run_id="autopilot-1",
                fencing_token="fence-1",
                process_exists=lambda _pid: False,
            )

        self.assertTrue(released)
        self.assertIsNotNone(backend.released)
        self.assertEqual(backend.released.metadata["fencing_token"], "fence-1")

    def test_recovery_refuses_missing_or_wrong_fencing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self._manager(Path(directory), token="")
            with self.assertRaisesRegex(
                locks.LockBackendError,
                "recorded fencing token",
            ):
                manager.recover_stale_autopilot(
                    run_id="autopilot-1",
                    fencing_token="fence-1",
                    process_exists=lambda _pid: False,
                )
            self.assertIsNone(backend.released)

            manager, backend = self._manager(Path(directory))
            with self.assertRaises(locks.LockFencingMismatch):
                manager.recover_stale_autopilot(
                    run_id="autopilot-1",
                    fencing_token="wrong-fence",
                    process_exists=lambda _pid: False,
                )
            self.assertIsNone(backend.released)

    def test_recovery_refuses_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self._manager(Path(directory))

            with self.assertRaisesRegex(locks.LockBackendError, "live process"):
                manager.recover_stale_autopilot(
                    run_id="autopilot-1",
                    fencing_token="fence-1",
                    process_exists=lambda _pid: True,
                )

        self.assertIsNone(backend.released)

    def test_recovery_refuses_expired_foreign_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self._manager(Path(directory))
            backend.metadata.update(
                {
                    "host": "foreign.example.invalid",
                    "lease_seconds": 1,
                    "heartbeat_at": "2020-01-01T00:00:00+00:00",
                }
            )

            with self.assertRaisesRegex(locks.LockBackendError, "local host owner"):
                manager.recover_stale_autopilot(
                    run_id="autopilot-1",
                    fencing_token="fence-1",
                    process_exists=lambda _pid: False,
                )

        self.assertIsNone(backend.released)

    def test_release_revalidates_run_when_fencing_tokens_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self._manager(Path(directory))
            stale_owner = locks.TaskLock(
                task_id=locks.AUTOPILOT_LOCK_NAME,
                path=backend.path_for(locks.AUTOPILOT_LOCK_NAME),
                metadata={
                    "run_id": "autopilot-old",
                    "fencing_token": "fence-1",
                },
            )

            with self.assertRaises(locks.LockOwnerMismatch):
                manager.release(stale_owner)

        self.assertIsNone(backend.released)

    def test_fencing_generation_is_atomic_across_local_contenders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ThreadPoolExecutor(max_workers=8) as executor:
                tokens = list(
                    executor.map(
                        lambda _index: locks.next_fencing_token(root, "TASK-1"),
                        range(32),
                    )
                )

        self.assertEqual(len(set(tokens)), 32)
        self.assertEqual(sorted(map(int, tokens)), list(range(1, 33)))


class AcquireWitnessCompensationTests(unittest.TestCase):
    """Give-back behaviour when the local fencing witness cannot be written."""

    @staticmethod
    def _witness_failure():
        return patch.object(
            locks,
            "record_acquired_fencing_token",
            side_effect=OSError("witness volume offline"),
        )

    def test_witness_failure_removes_the_lock_and_allows_reacquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = locks.DirectoryLockBackend(root)
            manager = locks.LockManager(root, backend=backend)

            with self._witness_failure():
                with self.assertRaises(OSError):
                    manager.acquire("TASK-1", "run-1")

            self.assertIsNone(backend.status("TASK-1"))
            self.assertFalse(backend.path_for("TASK-1").exists())
            self.assertEqual(locks.read_acquired_fencing_token(root, "TASK-1"), "")

            reacquired = manager.acquire("TASK-1", "run-2")
            self.assertEqual(reacquired.metadata["run_id"], "run-2")
            self.assertEqual(
                locks.read_acquired_fencing_token(root, "TASK-1"),
                locks.fencing_token_value(reacquired.metadata.get("fencing_token")),
            )

    def test_double_failure_surfaces_both_roles_without_leaking_the_token(
        self,
    ) -> None:
        class TokenLeakingReleaseBackend(locks.DirectoryLockBackend):
            def release(self, task_lock: locks.TaskLock) -> None:
                token = locks.fencing_token_value(
                    task_lock.metadata.get("fencing_token")
                )
                raise locks.LockBackendError(
                    f"give-back refused with fencing_token={token}"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TokenLeakingReleaseBackend(root)
            manager = locks.LockManager(root, backend=backend)

            with self._witness_failure():
                with self.assertRaises(locks.LockWitnessCompensationError) as caught:
                    manager.acquire("TASK-1", "run-1")

            error = caught.exception
            granted_token = locks.fencing_token_value(
                (backend.status("TASK-1") or {}).get("fencing_token")
            )
            self.assertTrue(granted_token)
            self.assertTrue(backend.path_for("TASK-1").exists())
            self.assertEqual(locks.read_acquired_fencing_token(root, "TASK-1"), "")

            self.assertIs(error.witness_error, error.__cause__)
            self.assertIsInstance(error.witness_error, OSError)
            self.assertIsInstance(error.release_error, locks.LockBackendError)
            self.assertIn("witness volume offline", error.witness_detail)
            self.assertIn("give-back refused", error.release_detail)
            self.assertIn("may remain held", str(error))
            self.assertNotIn(f"fencing_token={granted_token}", str(error))
            self.assertIn(
                f"fencing_token={locks.FENCING_TOKEN_REDACTION}",
                error.release_detail,
            )

    def test_compensation_mismatch_cannot_replace_the_witness_failure(self) -> None:
        for name, field, replacement, expected_error in (
            ("owner", "run_id", "other-run", locks.LockOwnerMismatch),
            ("fencing", "fencing_token", "9", locks.LockFencingMismatch),
        ):
            with self.subTest(mismatch=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    backend = locks.DirectoryLockBackend(root)
                    manager = locks.LockManager(root, backend=backend)

                    def replace_lock_owner(*_args: object) -> None:
                        path = backend.path_for("TASK-1")
                        current = locks.read_metadata(path)
                        current[field] = replacement
                        locks.write_metadata(path, current)
                        raise OSError("witness volume offline")

                    with patch.object(
                        locks,
                        "record_acquired_fencing_token",
                        side_effect=replace_lock_owner,
                    ):
                        with self.assertRaises(
                            locks.LockWitnessCompensationError
                        ) as caught:
                            manager.acquire("TASK-1", "run-1")

                    error = caught.exception
                    self.assertIs(error.witness_error, error.__cause__)
                    self.assertIn("witness volume offline", error.witness_detail)
                    self.assertIsInstance(error.release_error, expected_error)
                    self.assertIn(expected_error.__name__, error.release_detail)
                    self.assertIn("may remain held", str(error))
                    self.assertTrue(backend.path_for("TASK-1").exists())
                    self.assertEqual(
                        (backend.status("TASK-1") or {}).get(field), replacement
                    )

    def test_unlabelled_low_entropy_generation_is_redacted(self) -> None:
        class BareGenerationReleaseBackend(locks.DirectoryLockBackend):
            def release(self, task_lock: locks.TaskLock) -> None:
                token = locks.fencing_token_value(
                    task_lock.metadata.get("fencing_token")
                )
                raise locks.LockBackendError(
                    f"adapter refused: generation {token} rejected, run 141 kept"
                    f"; retry after generation {token}. schema {token}.4 held"
                    f"; generation {token}- unavailable"
                    f"; generation {token}-rejected, node n-{token}-b idle"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = BareGenerationReleaseBackend(root)
            manager = locks.LockManager(root, backend=backend)

            with self._witness_failure():
                with self.assertRaises(locks.LockWitnessCompensationError) as caught:
                    manager.acquire("TASK-1", "run-1")

            error = caught.exception
            granted_token = locks.fencing_token_value(
                (backend.status("TASK-1") or {}).get("fencing_token")
            )
            self.assertEqual(granted_token, "1")
            self.assertIn(
                f"generation {locks.FENCING_TOKEN_REDACTION} rejected",
                error.release_detail,
            )
            self.assertNotIn(f"generation {granted_token} ", error.release_detail)
            # Sentence punctuation ends the generation. A leading generation
            # before a hyphen is ambiguous and therefore over-redacted.
            self.assertIn(
                f"generation {locks.FENCING_TOKEN_REDACTION}. schema",
                error.release_detail,
            )
            self.assertIn(
                f"generation {locks.FENCING_TOKEN_REDACTION}- unavailable",
                error.release_detail,
            )
            self.assertIn(
                f"generation {locks.FENCING_TOKEN_REDACTION}-rejected",
                error.release_detail,
            )
            self.assertNotIn(f"generation {granted_token}.", error.release_detail)
            self.assertNotIn(f"generation {granted_token}-", error.release_detail)
            self.assertIn(f"node n-{granted_token}-b idle", error.release_detail)
            # A generation this short over-redacts standalone matches rather
            # than leak, but must not rewrite the inside of larger numbers.
            self.assertIn("run 141 kept", error.release_detail)
            self.assertIn(f"schema {granted_token}.4 held", error.release_detail)

    def test_short_generation_is_redacted_at_punctuation_boundaries(self) -> None:
        metadata = {"fencing_token": "1"}
        cases = (
            ("generation 1.", "generation <redacted>."),
            ("generation 1-rejected", "generation <redacted>-rejected"),
            ("1-rejected", "<redacted>-rejected"),
            ("generation 1-", "generation <redacted>-"),
        )

        for diagnostic, expected in cases:
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(
                    locks.describe_lock_failure(
                        locks.LockBackendError(diagnostic), metadata
                    ),
                    f"LockBackendError: {expected}",
                )

    def test_command_backend_spawn_rejection_is_a_compensation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with patch.object(
                locks.subprocess,
                "Popen",
                side_effect=ValueError("embedded null byte"),
            ):
                with self.assertRaises(locks.LockBackendError) as spawn_caught:
                    locks.CommandLockBackend(
                        repo=root,
                        lock_root=root,
                        status_command="true",
                        acquire_command="true",
                        release_command="true\0",
                        list_command="true",
                    ).release(
                        locks.TaskLock(
                            task_id="TASK-1",
                            path=root / "TASK-1.json",
                            metadata={"run_id": "run-1", "fencing_token": "1"},
                        )
                    )

            self.assertIn("could not start", str(spawn_caught.exception))
            self.assertIsInstance(spawn_caught.exception.__cause__, ValueError)

            class SpawnRejectingReleaseBackend(locks.DirectoryLockBackend):
                def release(self, task_lock: locks.TaskLock) -> None:
                    raise locks.LockBackendError(
                        "lock release_command could not start: embedded null byte"
                    )

            backend = SpawnRejectingReleaseBackend(root)
            manager = locks.LockManager(root, backend=backend)
            with self._witness_failure():
                with self.assertRaises(locks.LockWitnessCompensationError) as caught:
                    manager.acquire("TASK-2", "run-1")
            self.assertIn("could not start", caught.exception.release_detail)


if __name__ == "__main__":
    unittest.main()
