from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
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

    def acquire(self, task_id: str, run_id: str) -> TaskLock:
        self.lock_root.mkdir(parents=True, exist_ok=True)
        path = self.lock_root / f"{safe_name(task_id)}.lock"
        metadata = {
            "task_id": task_id,
            "run_id": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise LockBusy(path, read_metadata(path)) from exc
        (path / "lock.json").write_text(
            json.dumps(metadata, indent=2) + "\n",
            encoding="utf-8",
        )
        return TaskLock(task_id=task_id, path=path, metadata=metadata)

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
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
