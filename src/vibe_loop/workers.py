from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_loop.locks import MAIN_INTEGRATION_LOCK_NAME, LockManager, TaskLock
from vibe_loop.runs import (
    RUN_RECORD_TYPE,
    WORKER_REPORT_RECORD_TYPE,
    RunStore,
    WorkerReport,
    utc_now_iso,
)


ACTIVE_RUN_SCHEMA_VERSION = 1
ACTIVE_RUN_RECORD_TYPE = "active_run"
WORKSPACE_CLAIM_SCHEMA_VERSION = 1
WORKSPACE_CLAIM_RECORD_TYPE = "workspace_claim"
DIRTY_SUMMARY_LIMIT = 200


class WorkspaceClaimError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ):
        self.code = code
        self.details = details or {}
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class WorkspaceClaim:
    task_id: str
    run_id: str
    branch: str
    worktree: Path
    base_commit: str
    head_commit: str
    current_branch: str
    dirty: bool
    dirty_summary: tuple[str, ...]
    claimed_at: str = dataclasses.field(default_factory=utc_now_iso)

    @classmethod
    def from_json(cls, payload: object) -> WorkspaceClaim | None:
        if not isinstance(payload, dict):
            return None
        task_id = optional_string(payload.get("task_id"))
        run_id = optional_string(payload.get("run_id"))
        branch = optional_string(payload.get("branch"))
        worktree = optional_path(payload.get("worktree"))
        if not task_id or not run_id or not branch or worktree is None:
            return None
        return cls(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            base_commit=optional_string(payload.get("base_commit")) or "",
            head_commit=optional_string(payload.get("head_commit")) or "",
            current_branch=optional_string(payload.get("current_branch")) or "",
            dirty=optional_bool(payload.get("dirty")),
            dirty_summary=optional_string_tuple(payload.get("dirty_summary")),
            claimed_at=optional_string(payload.get("claimed_at")) or "",
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": WORKSPACE_CLAIM_SCHEMA_VERSION,
            "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "current_branch": self.current_branch,
            "dirty": self.dirty,
            "dirty_summary": list(self.dirty_summary),
            "claimed_at": self.claimed_at,
        }


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
    workspace: WorkspaceClaim | None = None

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
            workspace=WorkspaceClaim.from_json(metadata.get("workspace")),
        )

    def with_worker_pid(self, worker_pid: int) -> ActiveRunState:
        return dataclasses.replace(self, worker_pid=worker_pid)

    def to_lock_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
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
        if self.workspace is not None:
            metadata["workspace"] = self.workspace.to_json()
        return metadata


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
            "workspace": (
                self.active.workspace.to_json()
                if self.active.workspace is not None
                else None
            ),
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


def claim_worker_workspace(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    task_id: str,
    run_id: str,
    branch: str,
    worktree: Path,
    repo: Path,
    base_commit: str = "",
) -> WorkspaceClaim:
    if not branch:
        raise WorkspaceClaimError(
            "missing_branch",
            "workspace claim requires a branch",
        )
    lock = active_task_lock_for_claim(lock_manager, task_id=task_id, run_id=run_id)
    worktree_path = resolve_claim_worktree(repo, worktree)
    claim = inspect_workspace_claim(
        task_id=task_id,
        run_id=run_id,
        branch=branch,
        worktree=worktree_path,
        base_commit=base_commit
        or optional_string(lock.metadata.get("base_main"))
        or "",
    )
    updated_metadata = dict(lock.metadata)
    updated_metadata["workspace"] = claim.to_json()
    lock_manager.update(lock, updated_metadata)
    run_store.append_record(claim.to_json())
    return claim


def active_task_lock_for_claim(
    lock_manager: LockManager,
    *,
    task_id: str,
    run_id: str,
) -> TaskLock:
    matching_task: list[dict[str, object]] = []
    for metadata in lock_manager.list_locks():
        if metadata.get("task_id") != task_id:
            continue
        matching_task.append(metadata)
        if metadata.get("run_id") != run_id:
            continue
        path = optional_path(metadata.get("path"))
        if path is not None:
            return TaskLock(task_id=task_id, path=path, metadata=metadata)
    if matching_task:
        raise WorkspaceClaimError(
            "owner_mismatch",
            "workspace claim refused: active task lock owner does not match",
            details={
                "task_id": task_id,
                "run_id": run_id,
                "active_run_ids": [
                    value
                    for value in (
                        optional_string(item.get("run_id")) for item in matching_task
                    )
                    if value
                ],
            },
        )
    raise WorkspaceClaimError(
        "missing_active_task_lock",
        "workspace claim requires an active task lock",
        details={"task_id": task_id, "run_id": run_id},
    )


def inspect_workspace_claim(
    *,
    task_id: str,
    run_id: str,
    branch: str,
    worktree: Path,
    base_commit: str,
) -> WorkspaceClaim:
    if not worktree.exists() or not worktree.is_dir():
        raise WorkspaceClaimError(
            "missing_worktree",
            f"workspace claim refused: worktree does not exist: {worktree}",
            details={"worktree": str(worktree)},
        )
    current_branch = git_text(worktree, "branch", "--show-current")
    if current_branch != branch:
        raise WorkspaceClaimError(
            "branch_worktree_mismatch",
            "workspace claim refused: worktree branch does not match claim",
            details={
                "branch": branch,
                "current_branch": current_branch,
                "worktree": str(worktree),
            },
        )
    head_commit = git_text(worktree, "rev-parse", "--verify", "HEAD")
    status_lines = git_lines(worktree, "status", "--short")
    return WorkspaceClaim(
        task_id=task_id,
        run_id=run_id,
        branch=branch,
        worktree=worktree,
        base_commit=base_commit,
        head_commit=head_commit,
        current_branch=current_branch,
        dirty=bool(status_lines),
        dirty_summary=tuple(status_lines[:DIRTY_SUMMARY_LIMIT]),
    )


def resolve_claim_worktree(repo: Path, worktree: Path) -> Path:
    if not worktree.is_absolute():
        worktree = repo / worktree
    return worktree.resolve()


def git_text(repo: Path, *args: str) -> str:
    result = run_git(repo, *args)
    if result.returncode != 0:
        raise WorkspaceClaimError(
            "git_state_unavailable",
            "workspace claim refused: git state is unavailable",
            details={
                "worktree": str(repo),
                "git_args": list(args),
                "stderr": result.stderr.strip(),
            },
        )
    return result.stdout.strip()


def git_lines(repo: Path, *args: str) -> list[str]:
    text = git_text(repo, *args)
    return [line for line in text.splitlines() if line]


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise WorkspaceClaimError(
            "git_state_unavailable",
            "workspace claim refused: git could not be executed",
            details={"worktree": str(repo), "error": str(exc)},
        ) from exc


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
    except OSError:
        # Windows raises OSError(winerror=87, ERROR_INVALID_PARAMETER) for
        # non-existent PIDs instead of ProcessLookupError.
        return False
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
                recovery_command=f"rm -rf {shlex.quote(str(lock_path))}",
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
                run_id=optional_string(integration_status.metadata.get("run_id")) or "",
                lock_path=integration_status.path,
                stale_reason=integration_status.stale_reason or "unknown",
                kind="integration",
                recovery_command=(
                    f"rm -rf {shlex.quote(str(integration_status.path))}"
                ),
            )
        )
    return stale


@dataclasses.dataclass(frozen=True)
class CleanResult:
    cleaned: list[StaleLock]
    errors: list[tuple[StaleLock, str]]


def clean_stale_locks(
    stale_locks: list[StaleLock],
) -> CleanResult:
    cleaned: list[StaleLock] = []
    errors: list[tuple[StaleLock, str]] = []
    for lock in stale_locks:
        if not lock.lock_path.exists():
            continue
        if not _lock_metadata_matches(lock):
            errors.append((lock, "lock metadata changed since collection"))
            continue
        try:
            shutil.rmtree(lock.lock_path)
        except OSError as exc:
            errors.append((lock, str(exc)))
            continue
        cleaned.append(lock)
    return CleanResult(cleaned=cleaned, errors=errors)


def _lock_metadata_matches(lock: StaleLock) -> bool:
    metadata_path = lock.lock_path / "lock.json"
    if not metadata_path.exists():
        return True
    try:
        raw = metadata_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    current_run_id = payload.get("run_id")
    if not isinstance(current_run_id, str):
        return True
    return current_run_id == lock.run_id
