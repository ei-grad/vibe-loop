from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


MAIN_INTEGRATION_LOCK_NAME = "main-integration"
MAIN_INTEGRATION_LOCK_RECORD_TYPE = "main_integration_lock"
MAIN_INTEGRATION_LOCK_SCHEMA_VERSION = 1


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


class LockManager:
    def __init__(self, lock_root: Path):
        self.lock_root = lock_root

    def acquire(
        self,
        task_id: str,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> TaskLock:
        self.lock_root.mkdir(parents=True, exist_ok=True)
        lock_name = safe_name(task_id)
        path = self.lock_root / f"{lock_name}.lock"
        lock_metadata = {
            "task_id": task_id,
            "run_id": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
        }
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

    def is_locked(self, task_id: str) -> bool:
        return (self.lock_root / f"{safe_name(task_id)}.lock").exists()

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
        path = self.lock_root / f"{MAIN_INTEGRATION_LOCK_NAME}.lock"
        if not path.exists():
            return IntegrationLockStatus(
                locked=False,
                state="available",
                path=path,
                metadata={},
            )
        metadata = read_metadata(path)
        metadata.setdefault("task_id", MAIN_INTEGRATION_LOCK_NAME)
        metadata["path"] = str(path)
        return classify_integration_lock(
            path,
            metadata,
            current_host=current_host,
            process_exists=process_exists,
        )

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
