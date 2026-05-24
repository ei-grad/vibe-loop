from __future__ import annotations

import dataclasses
import json
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


MAIN_INTEGRATION_LOCK_NAME = "main-integration"
MAIN_INTEGRATION_LOCK_RECORD_TYPE = "main_integration_lock"
MAIN_INTEGRATION_LOCK_SCHEMA_VERSION = 1
COMMAND_LOCK_MAX_OUTPUT_BYTES = 128 * 1024
COMMAND_LOCK_TIMEOUT_SECONDS = 30.0


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
        return {
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
            "metadata": self.metadata,
        }


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
        return self.backend.acquire(task_id, run_id, metadata=metadata)

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock:
        return self.backend.update(task_lock, metadata)

    def release(self, task_lock: TaskLock) -> None:
        self.backend.release(task_lock)

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

    def release_main_integration(self, *, task_id: str, run_id: str) -> bool:
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
    def __init__(self, lock_root: Path):
        self.lock_root = lock_root

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
        lock_metadata = default_lock_metadata(task_id, run_id)
        if metadata is not None:
            lock_metadata.update(metadata)
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise LockBusy(path, read_metadata(path)) from exc
        try:
            write_metadata(path, lock_metadata)
        except OSError:
            shutil.rmtree(path)
            raise
        return TaskLock(task_id=task_id, path=path, metadata=lock_metadata)

    def update(self, task_lock: TaskLock, metadata: dict[str, object]) -> TaskLock:
        write_metadata(task_lock.path, metadata)
        return TaskLock(
            task_id=task_lock.task_id,
            path=task_lock.path,
            metadata=metadata,
        )

    def release(self, task_lock: TaskLock) -> None:
        if task_lock.path.exists():
            shutil.rmtree(task_lock.path)

    def status(self, task_id: str) -> dict[str, object] | None:
        path = self.path_for(task_id)
        if not path.exists():
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
    ):
        self.repo = repo
        self.lock_root = lock_root
        self.acquire_command = acquire_command
        self.release_command = release_command
        self.status_command = status_command
        self.list_command = list_command

    def path_for(self, task_id: str) -> Path:
        return self.lock_root / f"{safe_name(task_id)}.lock"

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        lock_metadata = default_lock_metadata(task_id, run_id)
        if metadata is not None:
            lock_metadata.update(metadata)
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
        payload = self._run_json_command(
            "locks.release_command",
            self.release_command,
            operation="release",
            task_id=task_lock.task_id,
            run_id=string_value(task_lock.metadata.get("run_id")),
            metadata=task_lock.metadata,
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
        payload = self._run_json_command(
            "locks.status_command",
            self.status_command,
            operation="status",
            task_id=task_id,
            run_id="",
            metadata={"task_id": task_id},
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
    ) -> object:
        env = os.environ.copy()
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
                returncode = wait_for_lock_command(
                    process,
                    stdout_file,
                    stderr_file,
                    setting_name,
                )
                stdout = read_command_output(stdout_file, setting_name, "stdout")
                stderr = read_command_output(stderr_file, setting_name, "stderr")
        if returncode != 0:
            detail = truncate_diagnostic(stderr.strip())
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


def wait_for_lock_command(
    process: subprocess.Popen[bytes],
    stdout_file: object,
    stderr_file: object,
    setting_name: str,
) -> int:
    deadline = time.monotonic() + COMMAND_LOCK_TIMEOUT_SECONDS
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
                f"{setting_name} timed out after {COMMAND_LOCK_TIMEOUT_SECONDS:g}s"
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
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def build_lock_manager(repo: Path, lock_root: Path, lock_config: object) -> LockManager:
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
            ),
        )
    return LockManager(lock_root)


def default_lock_metadata(task_id: str, run_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": datetime.now(UTC).isoformat(),
    }


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
    temp_path.replace(metadata_path)
