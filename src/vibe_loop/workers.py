from __future__ import annotations

import dataclasses
import os
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_loop.locks import MAIN_INTEGRATION_LOCK_NAME, LockManager
from vibe_loop.runs import (
    RUN_RECORD_TYPE,
    WORKER_REPORT_RECORD_TYPE,
    RunStore,
    WorkerReport,
    utc_now_iso,
)


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
    resources: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    conflict_domains_known: bool = False
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
        resources: tuple[str, ...] = (),
        paths: tuple[str, ...] = (),
        conflict_domains_known: bool = False,
    ) -> ActiveRunState:
        return cls(
            task_id=task_id,
            run_id=run_id,
            log_path=log_path,
            started_at=utc_now_iso(),
            base_main=base_main,
            command=command,
            resources=resources,
            paths=paths,
            conflict_domains_known=conflict_domains_known,
            supervisor_pid=os.getpid(),
        )

    @classmethod
    def from_lock_metadata(cls, metadata: dict[str, object]) -> ActiveRunState | None:
        record_type = optional_string(metadata.get("record_type"))
        if record_type not in {None, ACTIVE_RUN_RECORD_TYPE}:
            return None
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
            resources=optional_string_tuple(metadata.get("resources")),
            paths=optional_string_tuple(metadata.get("paths")),
            conflict_domains_known=optional_bool(
                metadata.get("conflict_domains_known")
            ),
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
            "resources": list(self.resources),
            "paths": list(self.paths),
            "conflict_domains_known": self.conflict_domains_known,
        }


@dataclasses.dataclass(frozen=True)
class WorkerView:
    active: ActiveRunState
    state: str
    process_state: str
    stale_reason: str | None = None
    result_status: str | None = None
    result_finished_at: str | None = None
    result_record_type: str | None = None
    result_metadata: dict[str, Any] | None = None

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
            "resources": list(self.active.resources),
            "paths": list(self.active.paths),
            "conflict_domains_known": self.active.conflict_domains_known,
            "lock": str(self.active.lock_path) if self.active.lock_path else "",
            "result_status": self.result_status,
            "result_finished_at": self.result_finished_at,
            "result_record_type": self.result_record_type,
            "result_metadata": self.result_metadata,
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
    result_by_run_id = latest_worker_status_by_run_id(run_store.read_records())
    views: list[WorkerView] = []
    for active in load_active_run_states(lock_manager):
        result = result_by_run_id.get(active.run_id)
        if (
            result is not None
            and result.get("record_type") == WORKER_REPORT_RECORD_TYPE
            and result.get("task_id") != active.task_id
        ):
            result = None
        process_state = classify_process(active, host, process_checker)
        result_status = result_value(result, "status") or result_value(
            result, "classification"
        )
        result_finished_at = result_value(result, "finished_at") or result_value(
            result,
            "reported_at",
        )
        result_record_type = result_value(result, "record_type")
        result_metadata = result_metadata_value(result)
        state = "running"
        stale_reason = None
        if not active.run_id:
            state = "stale"
            stale_reason = "missing_run_id"
        elif result_status and result_record_type != WORKER_REPORT_RECORD_TYPE:
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
                result_record_type=result_record_type,
                result_metadata=result_metadata,
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


def latest_worker_status_by_run_id(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        record_type = record.get("record_type")
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if record_type in {None, RUN_RECORD_TYPE}:
            results[run_id] = record
            continue
        if record_type != WORKER_REPORT_RECORD_TYPE:
            continue
        report = WorkerReport.from_record(record)
        if report is None:
            continue
        existing = results.get(run_id)
        if existing is None or existing.get("record_type") == WORKER_REPORT_RECORD_TYPE:
            results[run_id] = report.to_record()
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


def optional_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def optional_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def result_value(record: dict[str, Any] | None, key: str) -> str | None:
    if record is None:
        return None
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def result_metadata_value(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    worker_report = record.get("worker_report")
    if not isinstance(worker_report, dict):
        return None
    metadata = worker_report.get("metadata")
    return metadata if isinstance(metadata, dict) else None


@dataclasses.dataclass(frozen=True)
class StaleLock:
    task_id: str
    run_id: str
    lock_path: Path
    stale_reason: str
    kind: str
    recovery_command: str

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "lock_path": str(self.lock_path),
            "stale_reason": self.stale_reason,
            "kind": self.kind,
            "recovery_command": self.recovery_command,
        }


def collect_stale_locks(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
) -> list[StaleLock]:
    stale: list[StaleLock] = []
    for view in build_worker_views(
        lock_manager,
        run_store,
        current_host=current_host,
        process_exists=process_exists,
    ):
        if view.state != "stale":
            continue
        lock_path = view.active.lock_path
        if lock_path is None:
            continue
        stale.append(
            StaleLock(
                task_id=view.active.task_id,
                run_id=view.active.run_id,
                lock_path=lock_path,
                stale_reason=view.stale_reason or "unknown",
                kind="task",
                recovery_command=f"rm -rf {lock_path}",
            )
        )

    integration_status = lock_manager.main_integration_status(
        current_host=current_host,
        process_exists=process_exists,
    )
    if integration_status.locked and integration_status.state == "stale":
        stale.append(
            StaleLock(
                task_id=MAIN_INTEGRATION_LOCK_NAME,
                run_id=optional_string(
                    integration_status.metadata.get("run_id")
                ) or "",
                lock_path=integration_status.path,
                stale_reason=integration_status.stale_reason or "unknown",
                kind="integration",
                recovery_command=f"rm -rf {integration_status.path}",
            )
        )
    return stale


def clean_stale_locks(
    stale_locks: list[StaleLock],
) -> list[StaleLock]:
    cleaned: list[StaleLock] = []
    for lock in stale_locks:
        if lock.lock_path.exists():
            shutil.rmtree(lock.lock_path)
            cleaned.append(lock)
    return cleaned
