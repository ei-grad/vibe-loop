from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


MAIN_INTEGRATION_LOCK_NAME = "main-integration"
MAIN_INTEGRATION_LOCK_RECORD_TYPE = "main_integration_lock"
MAIN_INTEGRATION_LOCK_SCHEMA_VERSION = 1
AUTOPILOT_LOCK_NAME = "autopilot-supervisor"
AUTOPILOT_LOCK_RECORD_TYPE = "autopilot_supervisor_lock"
AUTOPILOT_LOCK_SCHEMA_VERSION = 1
COMMAND_LOCK_MAX_OUTPUT_BYTES = 128 * 1024
COMMAND_LOCK_TIMEOUT_SECONDS = 30.0
METADATA_REPLACE_TIMEOUT_SECONDS = 5.0
FENCING_TOKEN_REDACTION = "<redacted>"
FENCING_TOKEN_FIELDS = frozenset({"fencing_token", "expected_token", "actual_token"})
MIN_OPAQUE_FENCING_TOKEN_REDACTION_LENGTH = 8
QUOTED_FENCING_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)([\"']?(?:fencing_token|expected_token|actual_token)[\"']?"
    r"\s*[:=]\s*[\"'])([^\"']*)([\"'])"
)
UNQUOTED_FENCING_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(\b(?:fencing_token|expected_token|actual_token)\b\s*[:=]\s*)"
    r"([^\s,;}\]]+)"
)


class LockBusy(RuntimeError):
    def __init__(self, path: Path, metadata: dict[str, object]):
        self.path = path
        self.metadata = metadata
        super().__init__(f"task lock exists: {path}")


class LockOwnerMismatch(RuntimeError):
    def __init__(
        self,
        path: Path,
        metadata: dict[str, object],
        *,
        run_id: str,
        task_id: str,
    ):
        self.path = path
        self.metadata = metadata
        self.run_id = run_id
        self.task_id = task_id
        super().__init__(
            f"lock owner mismatch: {path} expected run_id={run_id} task_id={task_id}"
        )


class LockFencingMismatch(RuntimeError):
    def __init__(
        self,
        path: Path,
        metadata: dict[str, object],
        *,
        expected_token: str,
        actual_token: str,
    ):
        self.path = path
        self.metadata = metadata
        self.expected_token = expected_token
        self.actual_token = actual_token
        super().__init__(f"lock fencing token mismatch: {path}")


class LockBackendError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class TaskLock:
    task_id: str
    path: Path
    metadata: dict[str, object]


@dataclasses.dataclass(frozen=True)
class IntegrationLockStatus:
    locked: bool
    state: str
    path: Path
    metadata: dict[str, object]
    process_state: str = "none"
    stale_reason: str | None = None

    def to_json(self) -> dict[str, object]:
        payload = {
            "resource": MAIN_INTEGRATION_LOCK_NAME,
            "locked": self.locked,
            "state": self.state,
            "process_state": self.process_state,
            "stale_reason": self.stale_reason,
            "path": str(self.path),
            "run_id": string_value(self.metadata.get("run_id")),
            "owner_task_id": string_value(self.metadata.get("owner_task_id")),
            "pid": int_value(self.metadata.get("pid")),
            "pid_source": string_value(self.metadata.get("pid_source")),
            "host": string_value(self.metadata.get("host")),
            "started_at": string_value(self.metadata.get("started_at")),
            "lease_seconds": numeric_value(self.metadata.get("lease_seconds")),
            "heartbeat_at": string_value(self.metadata.get("heartbeat_at")),
            "fencing_token": fencing_token_value(self.metadata.get("fencing_token")),
            "metadata": self.metadata,
        }
        redacted = redact_fencing_token_payload(payload)
        assert isinstance(redacted, dict)
        return redacted


ProcessExists = Callable[[int], bool]
Sleep = Callable[[float], None]
Monotonic = Callable[[], float]


@dataclasses.dataclass(frozen=True)
class IntegrationLockAcquireResult:
    acquired: bool
    status: IntegrationLockStatus
    timed_out: bool = False


class LockBackend(Protocol):
    def path_for(self, task_id: str) -> Path: ...

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock: ...

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock: ...

    def release(self, task_lock: TaskLock) -> None: ...

    def status(self, task_id: str) -> dict[str, object] | None: ...

    def list_locks(self) -> list[dict[str, object]]: ...


class LockManager:
    def __init__(self, lock_root: Path, backend: LockBackend | None = None):
        self.lock_root = lock_root
        self.backend = (
            backend if backend is not None else DirectoryLockBackend(lock_root)
        )

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        task_lock = self.backend.acquire(task_id, run_id, metadata=metadata)
        try:
            record_acquired_fencing_token(
                self.lock_root,
                task_id,
                fencing_token_value(task_lock.metadata.get("fencing_token")),
            )
        except OSError:
            # Hold the invariant "granted implies recorded": a lock whose
            # generation was never recorded locally can never be recovered
            # later, so give it back rather than leaving it unreleasable.
            with suppress(LockBackendError, OSError):
                self.backend.release(task_lock)
            raise
        return task_lock

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock:
        current = self.current_lock(task_lock.task_id)
        validate_lock_fencing_token(
            task_lock.metadata,
            current.metadata,
            path=current.path,
        )
        validate_lock_run_id(task_lock, current.metadata)
        return self.backend.update(
            current,
            preserve_runtime_lock_fields(metadata, current.metadata),
        )

    def release(self, task_lock: TaskLock) -> None:
        self.release_with_timeout(task_lock)

    def release_with_timeout(
        self,
        task_lock: TaskLock,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        deadline = command_deadline(timeout_seconds)
        current = self._backend_status(task_lock.task_id, timeout_seconds)
        if current is None:
            return
        current_path = path_from_metadata(
            current,
            self.backend.path_for(task_lock.task_id),
        )
        validate_lock_fencing_token(task_lock.metadata, current, path=current_path)
        validate_lock_run_id(
            task_lock,
            current,
            path=current_path,
        )
        current_lock = TaskLock(
            task_id=task_lock.task_id,
            path=current_path,
            metadata=current,
        )
        if isinstance(self.backend, CommandLockBackend):
            self.backend.release_with_timeout(
                current_lock,
                timeout_seconds=remaining_command_timeout(deadline),
            )
        else:
            self.backend.release(current_lock)

    def status(self, task_id: str) -> dict[str, object] | None:
        return self.backend.status(task_id)

    def _backend_status(
        self,
        task_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, object] | None:
        if isinstance(self.backend, CommandLockBackend):
            return self.backend.status_with_timeout(
                task_id,
                timeout_seconds=timeout_seconds,
            )
        return self.backend.status(task_id)

    def current_lock(self, task_id: str) -> TaskLock:
        metadata = self.backend.status(task_id)
        path = self.backend.path_for(task_id)
        if metadata is None:
            raise LockBackendError(f"active lock not found: {path}")
        return TaskLock(
            task_id=task_id,
            path=path_from_metadata(metadata, path),
            metadata=metadata,
        )

    def validate_owner(
        self,
        *,
        task_id: str,
        run_id: str,
        fencing_token: str | None = None,
    ) -> TaskLock:
        current = self.current_lock(task_id)
        if string_value(current.metadata.get("run_id")) != run_id:
            raise LockOwnerMismatch(
                current.path,
                current.metadata,
                run_id=run_id,
                task_id=task_id,
            )
        if fencing_token:
            validate_lock_fencing_token(
                {"fencing_token": fencing_token},
                current.metadata,
                path=current.path,
            )
        return current

    def heartbeat(
        self,
        *,
        task_id: str,
        run_id: str,
        fencing_token: str | None = None,
        heartbeat_at: str | None = None,
    ) -> TaskLock:
        current = self.validate_owner(
            task_id=task_id,
            run_id=run_id,
            fencing_token=fencing_token,
        )
        metadata = dict(current.metadata)
        metadata["heartbeat_at"] = heartbeat_at or utc_now_iso()
        return self.update(current, metadata)

    def is_locked(self, task_id: str) -> bool:
        return self.backend.status(task_id) is not None

    def acquire_main_integration(
        self,
        *,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        lock_metadata = dict(metadata or {})
        lock_metadata.update(
            {
                "schema_version": MAIN_INTEGRATION_LOCK_SCHEMA_VERSION,
                "record_type": MAIN_INTEGRATION_LOCK_RECORD_TYPE,
                "task_id": MAIN_INTEGRATION_LOCK_NAME,
                "owner_task_id": task_id,
                "resource": MAIN_INTEGRATION_LOCK_NAME,
            }
        )
        return self.acquire(
            MAIN_INTEGRATION_LOCK_NAME,
            run_id,
            metadata=lock_metadata,
        )

    def acquire_main_integration_with_wait(
        self,
        *,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
        wait: bool = False,
        timeout_seconds: float | None = 0,
        poll_interval_seconds: float = 1,
        sleep: Sleep | None = None,
        monotonic: Monotonic | None = None,
    ) -> IntegrationLockAcquireResult:
        sleeper = sleep if sleep is not None else time.sleep
        clock = monotonic if monotonic is not None else time.monotonic
        deadline = (
            None if timeout_seconds is None else clock() + max(0.0, timeout_seconds)
        )
        interval = max(0.01, poll_interval_seconds)
        while True:
            try:
                self.acquire_main_integration(
                    task_id=task_id,
                    run_id=run_id,
                    metadata=metadata,
                )
            except LockBusy:
                status = self.main_integration_status()
                if not status.locked and wait:
                    continue
                if not wait or not integration_lock_waitable(status):
                    return IntegrationLockAcquireResult(
                        acquired=False,
                        status=status,
                    )
                if deadline is None:
                    sleeper(interval)
                    continue
                remaining = deadline - clock()
                if remaining <= 0:
                    return IntegrationLockAcquireResult(
                        acquired=False,
                        status=status,
                        timed_out=True,
                    )
                sleeper(min(interval, remaining))
                continue
            return IntegrationLockAcquireResult(
                acquired=True,
                status=self.main_integration_status(),
            )

    def release_main_integration(
        self,
        *,
        task_id: str,
        run_id: str,
        fencing_token: str | None = None,
    ) -> bool:
        status = self.main_integration_status()
        if not status.locked:
            return False
        owner_task_id = string_value(status.metadata.get("owner_task_id"))
        owner_run_id = string_value(status.metadata.get("run_id"))
        if owner_task_id != task_id or owner_run_id != run_id:
            raise LockOwnerMismatch(
                status.path,
                status.metadata,
                run_id=run_id,
                task_id=task_id,
            )
        if fencing_token:
            validate_lock_fencing_token(
                {"fencing_token": fencing_token},
                status.metadata,
                path=status.path,
            )
        self.release(
            TaskLock(
                task_id=MAIN_INTEGRATION_LOCK_NAME,
                path=status.path,
                metadata=status.metadata,
            )
        )
        return True

    def main_integration_status(
        self,
        *,
        current_host: str | None = None,
        process_exists: ProcessExists | None = None,
    ) -> IntegrationLockStatus:
        path = self.backend.path_for(MAIN_INTEGRATION_LOCK_NAME)
        metadata = self.backend.status(MAIN_INTEGRATION_LOCK_NAME)
        if metadata is None:
            return IntegrationLockStatus(
                locked=False,
                state="available",
                path=path,
                metadata={},
            )
        metadata.setdefault("task_id", MAIN_INTEGRATION_LOCK_NAME)
        path = path_from_metadata(metadata, path)
        metadata["path"] = str(path)
        return classify_integration_lock(
            path,
            metadata,
            current_host=current_host,
            process_exists=process_exists,
        )

    def acquire_autopilot(
        self,
        *,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        lock_metadata = dict(metadata or {})
        lock_metadata.update(
            {
                "schema_version": AUTOPILOT_LOCK_SCHEMA_VERSION,
                "record_type": AUTOPILOT_LOCK_RECORD_TYPE,
                "task_id": AUTOPILOT_LOCK_NAME,
                "resource": AUTOPILOT_LOCK_NAME,
            }
        )
        return self.acquire(AUTOPILOT_LOCK_NAME, run_id, metadata=lock_metadata)

    def autopilot_status(
        self,
        *,
        current_host: str | None = None,
        process_exists: ProcessExists | None = None,
        command_timeout_seconds: float | None = None,
    ) -> IntegrationLockStatus:
        path = self.backend.path_for(AUTOPILOT_LOCK_NAME)
        metadata = self._backend_status(
            AUTOPILOT_LOCK_NAME,
            command_timeout_seconds,
        )
        if metadata is None:
            return IntegrationLockStatus(
                locked=False,
                state="available",
                path=path,
                metadata={},
            )
        metadata.setdefault("task_id", AUTOPILOT_LOCK_NAME)
        path = path_from_metadata(metadata, path)
        metadata["path"] = str(path)
        return classify_integration_lock(
            path,
            metadata,
            current_host=current_host,
            process_exists=process_exists,
        )

    def local_fencing_token(self, task_id: str) -> str:
        return read_acquired_fencing_token(self.lock_root, task_id)

    def release_autopilot(
        self,
        *,
        run_id: str,
        fencing_token: str | None = None,
        command_timeout_seconds: float | None = None,
    ) -> bool:
        deadline = command_deadline(command_timeout_seconds)
        status = self.autopilot_status(
            command_timeout_seconds=command_timeout_seconds,
        )
        if not status.locked:
            return False
        owner_run_id = string_value(status.metadata.get("run_id"))
        if owner_run_id != run_id:
            raise LockOwnerMismatch(
                status.path,
                status.metadata,
                run_id=run_id,
                task_id=AUTOPILOT_LOCK_NAME,
            )
        if not fencing_token:
            raise LockBackendError("autopilot release requires a fencing token")
        if not fencing_token_value(status.metadata.get("fencing_token")):
            raise LockBackendError(
                "autopilot release requires a recorded fencing token"
            )
        validate_lock_fencing_token(
            {"fencing_token": fencing_token},
            status.metadata,
            path=status.path,
        )
        self.release_with_timeout(
            TaskLock(
                task_id=AUTOPILOT_LOCK_NAME,
                path=status.path,
                metadata=status.metadata,
            ),
            timeout_seconds=remaining_command_timeout(deadline),
        )
        return True

    def recover_stale_autopilot(
        self,
        *,
        run_id: str,
        fencing_token: str,
        verified_pid: int | None = None,
        current_host: str | None = None,
        process_exists: ProcessExists | None = None,
        command_timeout_seconds: float | None = None,
    ) -> bool:
        """Release a stale autopilot lock owned by an exact run and generation.

        `verified_pid` supplies the owner's process ID for backends that record
        none of their own; it is only consulted when the lock metadata omits
        `pid`, and the process it names must still be absent.
        """

        deadline = command_deadline(command_timeout_seconds)
        status = self.autopilot_status(
            current_host=current_host,
            process_exists=process_exists,
            command_timeout_seconds=command_timeout_seconds,
        )
        if not status.locked:
            return False
        if not run_id or not fencing_token:
            raise LockBackendError(
                "stale autopilot recovery requires run ID and fencing token"
            )
        owner_run_id = string_value(status.metadata.get("run_id"))
        if owner_run_id != run_id:
            raise LockOwnerMismatch(
                status.path,
                status.metadata,
                run_id=run_id,
                task_id=AUTOPILOT_LOCK_NAME,
            )
        actual_token = fencing_token_value(status.metadata.get("fencing_token"))
        if not actual_token:
            raise LockBackendError(
                "stale autopilot recovery requires a recorded fencing token"
            )
        validate_lock_fencing_token(
            {"fencing_token": fencing_token},
            status.metadata,
            path=status.path,
        )
        owner_host = string_value(status.metadata.get("host"))
        local_host = current_host if current_host is not None else socket.gethostname()
        if not owner_host or owner_host != local_host:
            raise LockBackendError(
                "stale autopilot recovery requires an exact local host owner"
            )
        pid = int_value(status.metadata.get("pid"))
        if pid is None:
            pid = verified_pid
        if pid is None:
            raise LockBackendError(
                "stale autopilot recovery requires a recorded process ID"
            )
        checker = process_exists if process_exists is not None else pid_exists
        if checker(pid):
            raise LockBackendError(
                "stale autopilot recovery refused for a live process"
            )
        if status.state != "stale":
            raise LockBackendError(
                f"stale autopilot recovery requires stale state, got {status.state}"
            )
        self.release_with_timeout(
            TaskLock(
                task_id=AUTOPILOT_LOCK_NAME,
                path=status.path,
                metadata=status.metadata,
            ),
            timeout_seconds=remaining_command_timeout(deadline),
        )
        return True

    def list_locks(self) -> list[dict[str, object]]:
        return self.backend.list_locks()

    @property
    def uses_directory_backend(self) -> bool:
        return isinstance(self.backend, DirectoryLockBackend)

    def release_stale_lock(
        self,
        *,
        task_id: str,
        run_id: str,
        path: Path,
        kind: str,
    ) -> bool:
        lock_task_id = MAIN_INTEGRATION_LOCK_NAME if kind == "integration" else task_id
        metadata = self.backend.status(lock_task_id)
        if metadata is None:
            return False
        current_path = path_from_metadata(metadata, self.backend.path_for(lock_task_id))
        if current_path != path or string_value(metadata.get("run_id")) != run_id:
            raise LockBackendError("lock metadata changed since collection")
        self.release(
            TaskLock(
                task_id=lock_task_id,
                path=current_path,
                metadata=metadata,
            )
        )
        return True


class DirectoryLockBackend:
    def __init__(self, lock_root: Path, lease_seconds: int | None = None):
        self.lock_root = lock_root
        self.lease_seconds = lease_seconds

    def path_for(self, task_id: str) -> Path:
        return self.lock_root / f"{safe_name(task_id)}.lock"

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        self.lock_root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(task_id)
        lock_metadata = default_lock_metadata(
            task_id,
            run_id,
            lease_seconds=self.lease_seconds,
        )
        if metadata is not None:
            lock_metadata.update(metadata)
        with metadata_update_lock(path):
            try:
                path.mkdir()
            except FileExistsError as exc:
                raise LockBusy(path, read_metadata(path)) from exc
            try:
                lock_metadata["fencing_token"] = next_fencing_token(
                    self.lock_root,
                    task_id,
                )
                write_metadata(path, lock_metadata)
            except OSError:
                shutil.rmtree(path)
                raise
        return TaskLock(task_id=task_id, path=path, metadata=lock_metadata)

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock:
        with metadata_update_lock(task_lock.path):
            current = read_metadata(task_lock.path)
            validate_lock_fencing_token(
                task_lock.metadata,
                current,
                path=task_lock.path,
            )
            metadata = preserve_runtime_lock_fields(metadata, current)
            write_metadata(task_lock.path, metadata)
        return TaskLock(
            task_id=task_lock.task_id,
            path=task_lock.path,
            metadata=metadata,
        )

    def release(self, task_lock: TaskLock) -> None:
        with metadata_update_lock(task_lock.path):
            current = read_metadata(task_lock.path)
            validate_lock_fencing_token(
                task_lock.metadata,
                current,
                path=task_lock.path,
            )
            if task_lock.path.exists():
                shutil.rmtree(task_lock.path)

    def status(self, task_id: str) -> dict[str, object] | None:
        path = self.path_for(task_id)
        if not path.exists() or not path.is_dir():
            return None
        metadata = read_metadata(path)
        metadata.setdefault("task_id", task_id)
        metadata["path"] = str(path)
        return metadata

    def list_locks(self) -> list[dict[str, object]]:
        if not self.lock_root.exists():
            return []
        locks: list[dict[str, object]] = []
        for path in sorted(self.lock_root.glob("*.lock")):
            if not path.is_dir():
                continue
            metadata = read_metadata(path)
            if not path.exists():
                continue
            metadata.setdefault("task_id", path.stem)
            metadata["path"] = str(path)
            locks.append(metadata)
        return locks


class CommandLockBackend:
    def __init__(
        self,
        *,
        repo: Path,
        lock_root: Path,
        acquire_command: str,
        release_command: str,
        status_command: str,
        list_command: str,
        lease_seconds: int | None = None,
        runtime_context: Mapping[str, str] | None = None,
    ):
        self.repo = repo
        self.lock_root = lock_root
        self.acquire_command = acquire_command
        self.release_command = release_command
        self.status_command = status_command
        self.list_command = list_command
        self.lease_seconds = lease_seconds
        self.runtime_context = dict(runtime_context or {})

    def path_for(self, task_id: str) -> Path:
        return self.lock_root / f"{safe_name(task_id)}.lock"

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        lock_metadata = default_lock_metadata(
            task_id,
            run_id,
            lease_seconds=self.lease_seconds,
        )
        if metadata is not None:
            lock_metadata.update(metadata)
        lock_metadata["fencing_token"] = next_fencing_token(self.lock_root, task_id)
        payload = self._run_json_command(
            "locks.acquire_command",
            self.acquire_command,
            operation="acquire",
            task_id=task_id,
            run_id=run_id,
            metadata=lock_metadata,
        )
        return self._task_lock_from_acquire_payload(
            payload,
            task_id=task_id,
            run_id=run_id,
            default_metadata=lock_metadata,
        )

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock:
        run_id = string_value(metadata.get("run_id"))
        payload = self._run_json_command(
            "locks.acquire_command",
            self.acquire_command,
            operation="update",
            task_id=task_lock.task_id,
            run_id=run_id,
            metadata=metadata,
        )
        if not isinstance(payload, dict):
            raise LockBackendError(
                "locks.acquire_command must return a JSON object for update"
            )
        update_result = payload.get("updated")
        acquire_result = payload.get("acquired")
        if update_result is False or acquire_result is False:
            raise LockBackendError("locks.acquire_command reported update failure")
        if update_result is not True and acquire_result is not True:
            raise LockBackendError(
                "locks.acquire_command must return boolean acquired or updated "
                "for update"
            )
        updated = command_payload_metadata(payload, metadata)
        normalized = normalize_command_metadata(
            updated,
            task_id=task_lock.task_id,
            run_id=run_id,
            default_path=task_lock.path,
        )
        return TaskLock(
            task_id=task_lock.task_id,
            path=path_from_metadata(normalized, task_lock.path),
            metadata=normalized,
        )

    def release(self, task_lock: TaskLock) -> None:
        self.release_with_timeout(task_lock)

    def release_with_timeout(
        self,
        task_lock: TaskLock,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        payload = self._run_json_command(
            "locks.release_command",
            self.release_command,
            operation="release",
            task_id=task_lock.task_id,
            run_id=string_value(task_lock.metadata.get("run_id")),
            metadata=task_lock.metadata,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("released"), bool
        ):
            raise LockBackendError(
                "locks.release_command must return a JSON object with boolean released"
            )
        if payload.get("released") is False:
            raise LockBackendError("locks.release_command reported released=false")

    def status(self, task_id: str) -> dict[str, object] | None:
        return self.status_with_timeout(task_id)

    def status_with_timeout(
        self,
        task_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object] | None:
        payload = self._run_json_command(
            "locks.status_command",
            self.status_command,
            operation="status",
            task_id=task_id,
            run_id="",
            metadata={"task_id": task_id},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise LockBackendError("locks.status_command must return a JSON object")
        locked = payload.get("locked")
        if locked is False:
            return None
        if locked is not True:
            raise LockBackendError("locks.status_command must return boolean locked")
        metadata = command_payload_metadata(payload, {"task_id": task_id})
        return normalize_command_metadata(
            metadata,
            task_id=task_id,
            run_id=string_value(metadata.get("run_id")),
            default_path=self.path_for(task_id),
        )

    def list_locks(self) -> list[dict[str, object]]:
        payload = self._run_json_command(
            "locks.list_command",
            self.list_command,
            operation="list",
            task_id="",
            run_id="",
            metadata={},
        )
        raw_locks = (
            payload.get("locks", payload) if isinstance(payload, dict) else payload
        )
        if not isinstance(raw_locks, list):
            raise LockBackendError(
                "locks.list_command must return a JSON array or {locks:[...]}"
            )
        locks: list[dict[str, object]] = []
        for index, raw_lock in enumerate(raw_locks):
            if not isinstance(raw_lock, dict):
                raise LockBackendError(
                    f"locks.list_command lock at index {index} must be an object"
                )
            metadata = command_payload_metadata(raw_lock, raw_lock)
            task_id = string_value(metadata.get("task_id"))
            if task_id:
                metadata = normalize_command_metadata(
                    metadata,
                    task_id=task_id,
                    run_id=string_value(metadata.get("run_id")),
                    default_path=self.path_for(task_id),
                )
            locks.append(metadata)
        return locks

    def _task_lock_from_acquire_payload(
        self,
        payload: object,
        *,
        task_id: str,
        run_id: str,
        default_metadata: dict[str, object],
    ) -> TaskLock:
        if not isinstance(payload, dict):
            raise LockBackendError("locks.acquire_command must return a JSON object")
        acquired = payload.get("acquired")
        metadata = command_payload_metadata(payload, default_metadata)
        if acquired is False:
            normalized = normalize_command_metadata(
                metadata,
                task_id=task_id,
                run_id="",
                default_path=self.path_for(task_id),
            )
            raise LockBusy(
                path_from_metadata(normalized, self.path_for(task_id)),
                normalized,
            )
        normalized = normalize_command_metadata(
            metadata,
            task_id=task_id,
            run_id=run_id,
            default_path=self.path_for(task_id),
        )
        if acquired is not True:
            raise LockBackendError("locks.acquire_command must return boolean acquired")
        return TaskLock(
            task_id=task_id,
            path=path_from_metadata(normalized, self.path_for(task_id)),
            metadata=normalized,
        )

    def _run_json_command(
        self,
        setting_name: str,
        command: str,
        *,
        operation: str,
        task_id: str,
        run_id: str,
        metadata: dict[str, object],
        timeout_seconds: float | None = None,
    ) -> object:
        env = os.environ.copy()
        env.update(self.runtime_context)
        env.update(
            {
                "VIBE_LOOP_LOCK_OPERATION": operation,
                "VIBE_LOOP_LOCK_TASK_ID": task_id,
                "VIBE_LOOP_LOCK_RUN_ID": run_id,
                "VIBE_LOOP_LOCK_ROOT": str(self.lock_root),
                "VIBE_LOOP_LOCK_METADATA_JSON": json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        with tempfile.TemporaryFile() as stdout_file:
            with tempfile.TemporaryFile() as stderr_file:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=self.repo,
                        shell=True,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        env=env,
                        start_new_session=os.name != "nt",
                    )
                except OSError as exc:
                    raise LockBackendError(
                        f"{setting_name} could not start: {exc}"
                    ) from exc
                try:
                    returncode = wait_for_lock_command(
                        process,
                        stdout_file,
                        stderr_file,
                        setting_name,
                        timeout_seconds=timeout_seconds,
                    )
                except KeyboardInterrupt:
                    terminate_lock_command_gracefully(process)
                    raise
                stdout = read_command_output(stdout_file, setting_name, "stdout")
                stderr = read_command_output(stderr_file, setting_name, "stderr")
        if returncode != 0:
            detail = truncate_diagnostic(
                redact_fencing_token_diagnostic(
                    redact_runtime_context_values(
                        stderr.strip(),
                        self.runtime_context,
                    ),
                    metadata,
                )
            )
            suffix = f": {detail}" if detail else ""
            raise LockBackendError(
                f"{setting_name} exited with status {returncode}{suffix}"
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LockBackendError(
                f"{setting_name} must write valid JSON to stdout: {exc.msg}"
            ) from exc


def redact_runtime_context_values(
    diagnostic: str,
    runtime_context: Mapping[str, str],
) -> str:
    redacted = diagnostic
    values = sorted(
        (value for value in runtime_context.values() if value),
        key=len,
        reverse=True,
    )
    for value in values:
        redacted = redacted.replace(value, "<runtime-context-redacted>")
    return redacted


def wait_for_lock_command(
    process: subprocess.Popen[bytes],
    stdout_file: object,
    stderr_file: object,
    setting_name: str,
    *,
    timeout_seconds: float | None = None,
) -> int:
    command_timeout = (
        COMMAND_LOCK_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0.001, timeout_seconds)
    )
    deadline = time.monotonic() + command_timeout
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode
        if lock_command_file_size(stdout_file) > COMMAND_LOCK_MAX_OUTPUT_BYTES:
            terminate_lock_command(process)
            raise LockBackendError(
                f"{setting_name} stdout exceeds {COMMAND_LOCK_MAX_OUTPUT_BYTES} bytes"
            )
        if lock_command_file_size(stderr_file) > COMMAND_LOCK_MAX_OUTPUT_BYTES:
            terminate_lock_command(process)
            raise LockBackendError(
                f"{setting_name} stderr exceeds {COMMAND_LOCK_MAX_OUTPUT_BYTES} bytes"
            )
        if time.monotonic() >= deadline:
            terminate_lock_command(process)
            raise LockBackendError(
                f"{setting_name} timed out after {command_timeout:g}s"
            )
        time.sleep(0.01)


def lock_command_file_size(file_obj: object) -> int:
    return int(file_obj.tell())


def read_command_output(file_obj: object, setting_name: str, stream_name: str) -> str:
    size = lock_command_file_size(file_obj)
    if size > COMMAND_LOCK_MAX_OUTPUT_BYTES:
        raise LockBackendError(
            f"{setting_name} {stream_name} exceeds {COMMAND_LOCK_MAX_OUTPUT_BYTES} bytes"
        )
    file_obj.seek(0)
    return file_obj.read().decode("utf-8", errors="replace")


def terminate_lock_command(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def terminate_lock_command_gracefully(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 5.0,
) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        terminate_lock_command(process)


def build_lock_manager(
    repo: Path,
    lock_root: Path,
    lock_config: object,
    *,
    runtime_context: Mapping[str, str] | None = None,
) -> LockManager:
    lock_type = getattr(lock_config, "type", "directory")
    if lock_type == "command":
        return LockManager(
            lock_root,
            backend=CommandLockBackend(
                repo=repo,
                lock_root=lock_root,
                acquire_command=str(getattr(lock_config, "acquire_command")),
                release_command=str(getattr(lock_config, "release_command")),
                status_command=str(getattr(lock_config, "status_command")),
                list_command=str(getattr(lock_config, "list_command")),
                lease_seconds=getattr(lock_config, "lease_seconds", None),
                runtime_context=runtime_context,
            ),
        )
    return LockManager(
        lock_root,
        backend=DirectoryLockBackend(
            lock_root,
            lease_seconds=getattr(lock_config, "lease_seconds", None),
        ),
    )


def default_lock_metadata(
    task_id: str,
    run_id: str,
    *,
    lease_seconds: int | None = None,
) -> dict[str, object]:
    started_at = datetime.now(UTC).isoformat()
    metadata: dict[str, object] = {
        "task_id": task_id,
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": started_at,
    }
    if lease_seconds is not None:
        metadata["lease_seconds"] = lease_seconds
        metadata["heartbeat_at"] = started_at
    return metadata


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def preserve_runtime_lock_fields(
    metadata: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    updated = dict(metadata)
    for key in ("fencing_token", "lease_seconds", "heartbeat_at"):
        if key not in updated and key in current:
            updated[key] = current[key]
    for key in (
        "workspace",
        "worker_pid",
        "pid",
        "session_id_source",
        "model_provider",
        "model_provider_source",
        "model_id",
        "model_id_source",
        "reasoning_effort",
        "reasoning_effort_source",
        "trailer_context",
        "trailer_context_sources",
    ):
        if runtime_lock_field_empty(updated.get(key)) and not runtime_lock_field_empty(
            current.get(key)
        ):
            updated[key] = current[key]
    if (
        updated.get("session_id") == updated.get("run_id")
        and not runtime_lock_field_empty(current.get("session_id"))
        and current.get("session_id") != current.get("run_id")
    ):
        updated["session_id"] = current["session_id"]
    if (
        updated.get("session_id_source") == "fallback:run_id"
        and not runtime_lock_field_empty(current.get("session_id_source"))
        and current.get("session_id_source") != "fallback:run_id"
    ):
        updated["session_id_source"] = current["session_id_source"]
    return updated


def runtime_lock_field_empty(value: object) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, dict):
        return not value
    return False


def command_deadline(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return time.monotonic() + max(0.0, timeout_seconds)


def remaining_command_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.001, deadline - time.monotonic())


def next_fencing_token(lock_root: Path, task_id: str) -> str:
    token_root = lock_root / ".fencing-tokens"
    token_root.mkdir(parents=True, exist_ok=True)
    token_path = token_root / f"{safe_name(task_id)}.token"
    with metadata_update_lock(token_path):
        current = 0
        try:
            current = int(token_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            current = 0
        next_token = current + 1
        token_path.write_text(f"{next_token}\n", encoding="utf-8")
    return str(next_token)


def acquired_fencing_token_path(lock_root: Path, task_id: str) -> Path:
    return lock_root / ".fencing-tokens" / f"{safe_name(task_id)}.acquired"


def record_acquired_fencing_token(lock_root: Path, task_id: str, token: str) -> None:
    """Remember the generation an acquire actually took, if it took one."""

    if not token:
        return
    token_path = acquired_fencing_token_path(lock_root, task_id)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_update_lock(token_path):
        token_path.write_text(f"{token}\n", encoding="utf-8")


def read_acquired_fencing_token(lock_root: Path, task_id: str) -> str:
    """Generation this installation last successfully acquired, or "".

    Both lock backends mint fencing tokens locally through `next_fencing_token`,
    but that counter advances on every acquire *attempt*: the command backend
    mints before it knows whether the backend granted the lock, so a refused
    acquire burns a generation. Only a granted acquire records here, which makes
    this an independent witness of the generation the backend should still be
    reporting. Stale recovery compares it against the generation the backend
    reports, which a token read back out of that same backend status cannot do.
    """

    token_path = acquired_fencing_token_path(lock_root, task_id)
    try:
        return fencing_token_value(token_path.read_text(encoding="utf-8").strip())
    except OSError:
        return ""


def validate_lock_run_id(
    expected: TaskLock,
    current: Mapping[str, object],
    *,
    path: Path | None = None,
) -> None:
    expected_run_id = string_value(expected.metadata.get("run_id"))
    actual_run_id = string_value(current.get("run_id"))
    if expected_run_id != actual_run_id:
        raise LockOwnerMismatch(
            path or expected.path,
            dict(current),
            run_id=expected_run_id,
            task_id=expected.task_id,
        )


def validate_lock_fencing_token(
    expected: dict[str, object],
    current: dict[str, object],
    *,
    path: Path,
) -> None:
    actual_token = fencing_token_value(current.get("fencing_token"))
    if not actual_token:
        return
    expected_token = fencing_token_value(expected.get("fencing_token"))
    if expected_token != actual_token:
        raise LockFencingMismatch(
            path,
            current,
            expected_token=expected_token,
            actual_token=actual_token,
        )


def lock_lease_expired(
    metadata: dict[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    lease_seconds = numeric_value(metadata.get("lease_seconds"))
    if lease_seconds is None or lease_seconds <= 0:
        return False
    heartbeat = string_value(metadata.get("heartbeat_at")) or string_value(
        metadata.get("started_at")
    )
    heartbeat_at = parse_iso_datetime(heartbeat)
    if heartbeat_at is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.timestamp() > heartbeat_at.timestamp() + float(lease_seconds)


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def command_payload_metadata(
    payload: dict[str, object],
    default_metadata: dict[str, object],
) -> dict[str, object]:
    raw_metadata = payload.get("metadata", payload.get("lock"))
    if raw_metadata is None:
        return dict(default_metadata)
    if not isinstance(raw_metadata, dict):
        raise LockBackendError("lock command metadata must be a JSON object")
    metadata = dict(default_metadata)
    metadata.update(raw_metadata)
    return metadata


def normalize_command_metadata(
    metadata: dict[str, object],
    *,
    task_id: str,
    run_id: str,
    default_path: Path,
) -> dict[str, object]:
    normalized = dict(metadata)
    metadata_task_id = string_value(normalized.get("task_id"))
    if metadata_task_id and metadata_task_id != task_id:
        raise LockBackendError(
            f"lock command returned task_id={metadata_task_id}, expected {task_id}"
        )
    metadata_run_id = string_value(normalized.get("run_id"))
    if run_id and metadata_run_id and metadata_run_id != run_id:
        raise LockBackendError(
            f"lock command returned run_id={metadata_run_id}, expected {run_id}"
        )
    normalized["task_id"] = task_id
    if run_id:
        normalized["run_id"] = run_id
    normalized.setdefault("path", str(default_path))
    return normalized


def path_from_metadata(metadata: dict[str, object], default: Path) -> Path:
    path = metadata.get("path")
    if isinstance(path, str) and path:
        return Path(path)
    return default


def truncate_diagnostic(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-._" else "_" for char in value)


def classify_integration_lock(
    path: Path,
    metadata: dict[str, object],
    *,
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
) -> IntegrationLockStatus:
    run_id = string_value(metadata.get("run_id"))
    pid = int_value(metadata.get("pid"))
    host = string_value(metadata.get("host"))
    checker = process_exists if process_exists is not None else pid_exists
    local_host = current_host if current_host is not None else socket.gethostname()

    state = "held"
    process_state = "running"
    stale_reason = None
    if not run_id:
        state = "stale"
        process_state = "unknown_pid" if pid is None else "unknown"
        stale_reason = "missing_run_id"
    elif lock_lease_expired(metadata):
        state = "stale"
        stale_reason = "lease_expired"
    elif host and host != local_host:
        state = "unknown"
        process_state = "foreign_host"
        stale_reason = "foreign_host"
    elif pid is None:
        state = "stale"
        process_state = "unknown_pid"
        stale_reason = "missing_pid"
    elif not checker(pid):
        state = "stale"
        process_state = "missing"
        stale_reason = "missing_process"

    return IntegrationLockStatus(
        locked=True,
        state=state,
        path=path,
        metadata=metadata,
        process_state=process_state,
        stale_reason=stale_reason,
    )


def integration_lock_waitable(status: IntegrationLockStatus) -> bool:
    return status.locked and status.state in {"held", "unknown"}


if sys.platform == "win32":

    def pid_exists(pid: int) -> bool:
        # os.kill(pid, 0) is unusable here: on Windows any signal other than
        # CTRL_C_EVENT/CTRL_BREAK_EVENT calls TerminateProcess, so probing
        # would kill the process, and it reports exited-but-still-open
        # processes (e.g. a child whose Popen handle is alive) as running.
        import ctypes
        from ctypes import wintypes

        if pid <= 0:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        # Explicit signatures: HANDLE is pointer-sized and would otherwise be
        # truncated through the default c_int return/argument conversion.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

else:

    def pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def fencing_token_value(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, str)):
        return str(value)
    return ""


def redact_fencing_token_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: (
                FENCING_TOKEN_REDACTION
                if key in FENCING_TOKEN_FIELDS and item not in (None, "")
                else redact_fencing_token_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_fencing_token_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_fencing_token_payload(item) for item in value)
    return value


def redact_fencing_token_diagnostic(
    diagnostic: str,
    metadata: Mapping[str, object],
) -> str:
    redacted = QUOTED_FENCING_DIAGNOSTIC_PATTERN.sub(
        rf"\1{FENCING_TOKEN_REDACTION}\3",
        diagnostic,
    )
    redacted = UNQUOTED_FENCING_DIAGNOSTIC_PATTERN.sub(
        rf"\1{FENCING_TOKEN_REDACTION}",
        redacted,
    )
    for token in sorted(fencing_token_values(metadata), key=len, reverse=True):
        if len(token) >= MIN_OPAQUE_FENCING_TOKEN_REDACTION_LENGTH:
            redacted = redacted.replace(token, FENCING_TOKEN_REDACTION)
    return redacted


def fencing_token_values(value: object) -> set[str]:
    if isinstance(value, Mapping):
        values = {
            fencing_token_value(item)
            for key, item in value.items()
            if key in FENCING_TOKEN_FIELDS and fencing_token_value(item)
        }
        for item in value.values():
            values.update(fencing_token_values(item))
        return values
    if isinstance(value, (list, tuple)):
        values: set[str] = set()
        for item in value:
            values.update(fencing_token_values(item))
        return values
    return set()


def numeric_value(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def read_metadata(path: Path) -> dict[str, object]:
    metadata_path = path / "lock.json"
    if not metadata_path.exists():
        return {}
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    metadata_path = path / "lock.json"
    temp_path = path / f".lock.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temp_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        replace_metadata_file(temp_path, metadata_path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def replace_metadata_file(source: Path, target: Path) -> None:
    # On Windows, replacing a file that another process currently holds open for
    # reading fails with PermissionError (WinError 5, sharing violation).
    # Concurrent workers read lock.json without holding the metadata-update lock,
    # so a reader's open handle can collide with a writer's atomic replace. Retry
    # briefly to ride out the transient collision; POSIX rename has no such
    # restriction, so re-raise there immediately.
    if sys.platform != "win32":
        source.replace(target)
        return
    deadline = time.monotonic() + METADATA_REPLACE_TIMEOUT_SECONDS
    delay = 0.005
    while True:
        try:
            source.replace(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


@contextmanager
def metadata_update_lock(path: Path):
    lock_path = path.parent / f".{path.name}.metadata-update"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        ensure_metadata_lock_byte(handle)
        lock_metadata_file(handle)
        try:
            yield
        finally:
            unlock_metadata_file(handle)


def ensure_metadata_lock_byte(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def lock_metadata_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise LockBackendError("metadata update locking is unsupported on this platform")


def unlock_metadata_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
