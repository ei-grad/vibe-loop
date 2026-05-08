from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


RUN_SCHEMA_VERSION = 3
RUN_RECORD_TYPE = "run_result"
WORKER_REPORT_SCHEMA_VERSION = 1
WORKER_REPORT_RECORD_TYPE = "worker_report"
WORKER_REPORT_STATUSES = ("completed", "blocked", "failed", "unknown")
_APPEND_LOCK = threading.Lock()
LOCK_POLL_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 30.0


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    classification: str
    exit_code: int
    log_path: Path
    start_main: str
    end_main: str
    message: str = ""
    session_id: str | None = None
    session_id_source: str = "fallback:run_id"
    agent_command_source: str = ""
    agent_selection_command_source: str = ""
    agent_default_policy_source: str = ""
    agent_default_policy: str = ""
    classification_source: str = ""
    worker_report: dict[str, object] | None = None
    finished_at: str = dataclasses.field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id or self.run_id,
            "session_id_source": self.session_id_source,
            "task_id": self.task_id,
            "classification": self.classification,
            "exit_code": self.exit_code,
            "log": str(self.log_path),
            "start_main": self.start_main,
            "end_main": self.end_main,
            "message": self.message,
            "agent_command_source": self.agent_command_source,
            "agent_selection_command_source": self.agent_selection_command_source,
            "agent_default_policy_source": self.agent_default_policy_source,
            "agent_default_policy": self.agent_default_policy,
            "classification_source": self.classification_source,
            "worker_report": self.worker_report,
            "finished_at": self.finished_at,
        }

    def to_record(self) -> dict[str, object]:
        record = self.to_json()
        record.update(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "record_type": RUN_RECORD_TYPE,
                "status": self.classification,
            }
        )
        return record


@dataclasses.dataclass(frozen=True)
class WorkerReport:
    run_id: str
    task_id: str
    status: str
    commit: str = ""
    message: str = ""
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    reported_at: str = dataclasses.field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.status not in WORKER_REPORT_STATUSES:
            raise ValueError(
                "worker report status must be one of: "
                f"{', '.join(WORKER_REPORT_STATUSES)}"
            )
        if not self.run_id:
            raise ValueError("worker report run_id is required")
        if not self.task_id:
            raise ValueError("worker report task_id is required")

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "commit": self.commit,
            "message": self.message,
            "metadata": self.metadata,
            "reported_at": self.reported_at,
        }

    def to_record(self) -> dict[str, object]:
        record = self.to_json()
        record.update(
            {
                "schema_version": WORKER_REPORT_SCHEMA_VERSION,
                "record_type": WORKER_REPORT_RECORD_TYPE,
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> WorkerReport | None:
        if record.get("record_type") != WORKER_REPORT_RECORD_TYPE:
            return None
        run_id = record.get("run_id")
        task_id = record.get("task_id")
        status = record.get("status")
        if not isinstance(run_id, str) or not run_id:
            return None
        if not isinstance(task_id, str) or not task_id:
            return None
        if not isinstance(status, str) or status not in WORKER_REPORT_STATUSES:
            return None
        metadata = record.get("metadata")
        return cls(
            run_id=run_id,
            task_id=task_id,
            status=status,
            commit=string_value(record.get("commit")),
            message=string_value(record.get("message")),
            metadata=metadata if isinstance(metadata, dict) else {},
            reported_at=string_value(record.get("reported_at")),
        )


class RunStore:
    def __init__(self, path: Path):
        self.path = path

    def append_result(self, result: RunResult) -> None:
        self.append_record(result.to_record())

    def append_report(self, report: WorkerReport) -> None:
        self.append_record(report.to_record())

    def append_record(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                    handle.flush()

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def recent_records(self, max_runs: int = 5) -> list[dict[str, Any]]:
        return self.read_records()[-max_runs:]

    def recent_result_records(self, max_runs: int = 5) -> list[dict[str, Any]]:
        return [
            record
            for record in self.read_records()
            if record.get("record_type") in {None, RUN_RECORD_TYPE}
        ][-max_runs:]

    def latest_worker_report(
        self,
        run_id: str,
        task_id: str | None = None,
    ) -> WorkerReport | None:
        for record in reversed(self.read_records()):
            report = WorkerReport.from_record(record)
            if report is None or report.run_id != run_id:
                continue
            if task_id is not None and report.task_id != task_id:
                continue
            return report
        return None

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        records = self.recent_result_records(max_runs)
        if not records:
            return "No prior vibe-loop runs recorded."
        chunks = ["Recent vibe-loop runs:"]
        for record in records:
            chunks.append(json.dumps(record, sort_keys=True))
            log_path = record_log_path(record)
            if log_path is not None:
                chunks.append(f"Log tail for {log_path}:")
                chunks.extend(tail(log_path, tail_lines))
        return "\n".join(chunks)


def record_log_path(record: dict[str, Any]) -> Path | None:
    record_type = record.get("record_type")
    if record_type not in {None, RUN_RECORD_TYPE}:
        return None
    log = record.get("log")
    if not isinstance(log, str) or not log:
        return None
    path = Path(log)
    if not path.is_file():
        return None
    return path


def tail(path: Path, line_count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-line_count:]


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


@contextmanager
def append_record_lock(path: Path):
    if fcntl is None and msvcrt is None:
        with append_record_directory_lock(path):
            yield
        return

    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        ensure_lock_byte(handle)
        lock_file(handle)
        try:
            yield
        finally:
            unlock_file(handle)


@contextmanager
def append_record_directory_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lockdir")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_path.mkdir()
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring append lock: {lock_path}")
            time.sleep(LOCK_POLL_SECONDS)
        else:
            break
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def lock_file(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def unlock_file(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
