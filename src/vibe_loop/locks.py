from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path


class LockBusy(RuntimeError):
    def __init__(self, path: Path, metadata: dict[str, object]):
        self.path = path
        self.metadata = metadata
        super().__init__(f"task lock exists: {path}")


@dataclasses.dataclass(frozen=True)
class TaskLock:
    task_id: str
    path: Path
    metadata: dict[str, object]


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
