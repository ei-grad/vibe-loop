from __future__ import annotations

import dataclasses
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_loop.locks import LockManager
from vibe_loop.runs import RUN_RECORD_TYPE, RunStore, utc_now_iso


ACTIVE_RUN_SCHEMA_VERSION = 1
ACTIVE_RUN_RECORD_TYPE = "active_run"


@dataclasses.dataclass(frozen=True)
class ActiveRunState:
    task_id: str
    run_id: str
    log_path: Path
    started_at: str
    base_main: str
    command: str
    worker_pid: int | None = None
    pid_source: str = "popen"
    pid_scope: str = "configured_command_process"
    supervisor_pid: int | None = None
    host: str = dataclasses.field(default_factory=socket.gethostname)
    lock_path: Path | None = None

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        run_id: str,
        log_path: Path,
        base_main: str,
        command: str,
    ) -> ActiveRunState:
        return cls(
            task_id=task_id,
            run_id=run_id,
            log_path=log_path,
            started_at=utc_now_iso(),
            base_main=base_main,
            command=command,
            supervisor_pid=os.getpid(),
        )

    @classmethod
    def from_lock_metadata(cls, metadata: dict[str, object]) -> ActiveRunState | None:
        task_id = optional_string(metadata.get("task_id"))
        if not task_id:
            return None
        run_id = optional_string(metadata.get("run_id")) or ""
        log_path = optional_path(metadata.get("log")) or optional_path(
            metadata.get("log_path")
        )
        worker_pid = optional_int(metadata.get("worker_pid"))
        pid_source = optional_string(metadata.get("pid_source")) or "popen"
        if worker_pid is None:
            worker_pid = optional_int(metadata.get("pid"))
            if worker_pid is not None:
                pid_source = "legacy_pid"
        return cls(
            task_id=task_id,
            run_id=run_id,
            log_path=log_path or Path(""),
            started_at=optional_string(metadata.get("started_at")) or "",
            base_main=(
                optional_string(metadata.get("base_main"))
                or optional_string(metadata.get("start_main"))
                or ""
            ),
            command=optional_string(metadata.get("command")) or "",
            worker_pid=worker_pid,
            pid_source=pid_source,
            pid_scope=(
                optional_string(metadata.get("pid_scope"))
                or "configured_command_process"
            ),
            supervisor_pid=optional_int(metadata.get("supervisor_pid")),
            host=optional_string(metadata.get("host")) or "",
            lock_path=optional_path(metadata.get("path")),
        )

    def with_worker_pid(self, worker_pid: int) -> ActiveRunState:
        return dataclasses.replace(self, worker_pid=worker_pid)

    def to_lock_metadata(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVE_RUN_SCHEMA_VERSION,
            "record_type": ACTIVE_RUN_RECORD_TYPE,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "pid": self.worker_pid,
            "worker_pid": self.worker_pid,
            "pid_source": self.pid_source,
            "pid_scope": self.pid_scope,
            "supervisor_pid": self.supervisor_pid,
            "host": self.host,
            "started_at": self.started_at,
            "log": str(self.log_path),
            "base_main": self.base_main,
            "start_main": self.base_main,
            "command": self.command,
        }


@dataclasses.dataclass(frozen=True)
class WorkerView:
    active: ActiveRunState
    state: str
    process_state: str
    stale_reason: str | None = None
    result_status: str | None = None
    result_finished_at: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.active.task_id,
            "run_id": self.active.run_id,
            "state": self.state,
            "process_state": self.process_state,
            "stale_reason": self.stale_reason,
            "pid": self.active.worker_pid,
            "worker_pid": self.active.worker_pid,
            "pid_source": self.active.pid_source,
            "pid_scope": self.active.pid_scope,
            "supervisor_pid": self.active.supervisor_pid,
            "host": self.active.host,
            "started_at": self.active.started_at,
            "log": str(self.active.log_path),
            "base_main": self.active.base_main,
            "command": self.active.command,
            "lock": str(self.active.lock_path) if self.active.lock_path else "",
            "result_status": self.result_status,
            "result_finished_at": self.result_finished_at,
        }


ProcessExists = Callable[[int], bool]


def load_active_run_states(lock_manager: LockManager) -> list[ActiveRunState]:
    states: list[ActiveRunState] = []
    for metadata in lock_manager.list_locks():
        state = ActiveRunState.from_lock_metadata(metadata)
        if state is not None:
            states.append(state)
    return states


def build_worker_views(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
) -> list[WorkerView]:
    host = current_host if current_host is not None else socket.gethostname()
    process_checker = process_exists if process_exists is not None else pid_exists
    result_by_run_id = latest_result_by_run_id(run_store.read_records())
    views: list[WorkerView] = []
    for active in load_active_run_states(lock_manager):
        result = result_by_run_id.get(active.run_id)
        process_state = classify_process(active, host, process_checker)
        result_status = result_value(result, "status") or result_value(
            result, "classification"
        )
        result_finished_at = result_value(result, "finished_at")
        state = "running"
        stale_reason = None
        if not active.run_id:
            state = "stale"
            stale_reason = "missing_run_id"
        elif result_status:
            state = "stale"
            stale_reason = "result_recorded"
        elif process_state == "missing":
            state = "stale"
            stale_reason = "missing_process"
        elif process_state == "unknown_pid":
            state = "stale"
            stale_reason = "missing_worker_pid"
        elif process_state != "running":
            state = "unknown"
            stale_reason = process_state
        views.append(
            WorkerView(
                active=active,
                state=state,
                process_state=process_state,
                stale_reason=stale_reason,
                result_status=result_status,
                result_finished_at=result_finished_at,
            )
        )
    return views


def classify_process(
    active: ActiveRunState,
    current_host: str,
    process_exists: ProcessExists | None = None,
) -> str:
    process_checker = process_exists if process_exists is not None else pid_exists
    if active.host and active.host != current_host:
        return "foreign_host"
    if active.worker_pid is None:
        return "unknown_pid"
    return "running" if process_checker(active.worker_pid) else "missing"


def latest_result_by_run_id(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        record_type = record.get("record_type")
        if record_type not in {None, RUN_RECORD_TYPE}:
            continue
        run_id = record.get("run_id")
        if isinstance(run_id, str) and run_id:
            results[run_id] = record
    return results


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def optional_path(value: object) -> Path | None:
    text = optional_string(value)
    return Path(text) if text is not None else None


def optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def result_value(record: dict[str, Any] | None, key: str) -> str | None:
    if record is None:
        return None
    value = record.get(key)
    return value if isinstance(value, str) and value else None
