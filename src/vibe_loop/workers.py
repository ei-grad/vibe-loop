from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from vibe_loop.locks import (
    MAIN_INTEGRATION_LOCK_NAME,
    TERMINAL_LOCK_OUTCOMES,
    LockBackendError,
    LockFencingMismatch,
    LockManager,
    TaskLock,
    fencing_token_value,
    lock_lease_expired,
    numeric_value,
    pid_exists,
    redact_fencing_token_payload,
    validate_lock_fencing_token,
)
from vibe_loop.processes import process_birth_identity
from vibe_loop.runs import (
    LOCK_EXPIRED_RECORD_TYPE,
    LOCK_FINALIZATION_FAILED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    RUN_RECORD_TYPE,
    WORKER_PROCESS_STARTED_RECORD_TYPE,
    WORKSPACE_CLAIM_RECORD_TYPE,
    WORKSPACE_CLAIMED_EVENT_TYPE,
    WORKER_REPORT_RECORD_TYPE,
    RunLifecycleEvent,
    RunLifecycleProgress,
    RunStore,
    WorkerReport,
    derive_run_lifecycle,
    empty_run_lifecycle,
    utc_now_iso,
)


ACTIVE_RUN_SCHEMA_VERSION = 1
ACTIVE_RUN_RECORD_TYPE = "active_run"
WORKSPACE_CLAIM_SCHEMA_VERSION = 1
WORKSPACE_DIAGNOSTIC_SCHEMA_VERSION = 1
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
    started_at: str = ""
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
            started_at=optional_string(payload.get("started_at")) or "",
            claimed_at=(
                optional_string(payload.get("claimed_at"))
                or optional_string(payload.get("occurred_at"))
                or ""
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": WORKSPACE_CLAIM_SCHEMA_VERSION,
            "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
            "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
            "occurred_at": self.claimed_at,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "current_branch": self.current_branch,
            "dirty": self.dirty,
            "dirty_summary": list(self.dirty_summary),
            "started_at": self.started_at,
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
    session_id: str = ""
    session_id_source: str = ""
    agent_kind: str = ""
    agent_profile: str = ""
    agent_prompt_dialect: str = ""
    agent_prompt_dialect_source: str = ""
    agent_skill_ref_prefix: str = ""
    agent_skill_ref_prefix_source: str = ""
    model_provider: str = ""
    model_provider_source: str = ""
    model_id: str = ""
    model_id_source: str = ""
    reasoning_effort: str = ""
    reasoning_effort_source: str = ""
    trailer_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    trailer_context_sources: dict[str, Any] = dataclasses.field(default_factory=dict)
    restart_count: int = 0
    max_restarts: int = 0
    lease_seconds: int | float | None = None
    heartbeat_at: str = ""
    fencing_token: str = ""
    worker_pid: int | None = None
    worker_process_group_id: int | None = None
    worker_session_id: int | None = None
    worker_process_birth_id: str = ""
    pid_source: str = "popen"
    pid_scope: str = "configured_command_process"
    supervisor_pid: int | None = None
    host: str = dataclasses.field(default_factory=socket.gethostname)
    lock_path: Path | None = None
    workspace: WorkspaceClaim | None = None
    # Published into the lock metadata just before release so a command lock
    # backend that mirrors run provenance finalizes this run with the outcome
    # the supervisor actually settled on, instead of inferring one from the
    # release event alone. Never treat a parsed value as proof that the run
    # settled: a backend may materialize a placeholder "unknown" for every live
    # lock, so only the supervisor that classified the run knows the difference.
    settled_outcome: str = ""
    settled_classification: str = ""

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
        session_id: str = "",
        session_id_source: str = "",
        agent_kind: str = "",
        agent_profile: str = "",
        agent_prompt_dialect: str = "",
        agent_prompt_dialect_source: str = "",
        agent_skill_ref_prefix: str = "",
        agent_skill_ref_prefix_source: str = "",
        model_provider: str = "",
        model_provider_source: str = "",
        model_id: str = "",
        model_id_source: str = "",
        reasoning_effort: str = "",
        reasoning_effort_source: str = "",
        trailer_context: dict[str, Any] | None = None,
        trailer_context_sources: dict[str, Any] | None = None,
        restart_count: int = 0,
        max_restarts: int = 0,
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
            session_id=session_id,
            session_id_source=session_id_source,
            agent_kind=agent_kind,
            agent_profile=agent_profile,
            agent_prompt_dialect=agent_prompt_dialect,
            agent_prompt_dialect_source=agent_prompt_dialect_source,
            agent_skill_ref_prefix=agent_skill_ref_prefix,
            agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
            model_provider=model_provider,
            model_provider_source=model_provider_source,
            model_id=model_id,
            model_id_source=model_id_source,
            reasoning_effort=reasoning_effort,
            reasoning_effort_source=reasoning_effort_source,
            trailer_context=dict(trailer_context or {}),
            trailer_context_sources=dict(trailer_context_sources or {}),
            restart_count=restart_count,
            max_restarts=max_restarts,
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
            session_id=optional_string(metadata.get("session_id")) or "",
            session_id_source=(
                optional_string(metadata.get("session_id_source")) or ""
            ),
            agent_kind=optional_string(metadata.get("agent_kind")) or "",
            agent_profile=optional_string(metadata.get("agent_profile")) or "",
            agent_prompt_dialect=(
                optional_string(metadata.get("agent_prompt_dialect")) or ""
            ),
            agent_prompt_dialect_source=(
                optional_string(metadata.get("agent_prompt_dialect_source")) or ""
            ),
            agent_skill_ref_prefix=(
                optional_string(metadata.get("agent_skill_ref_prefix")) or ""
            ),
            agent_skill_ref_prefix_source=(
                optional_string(metadata.get("agent_skill_ref_prefix_source")) or ""
            ),
            model_provider=optional_string(metadata.get("model_provider")) or "",
            model_provider_source=(
                optional_string(metadata.get("model_provider_source")) or ""
            ),
            model_id=(
                optional_string(metadata.get("model_id"))
                or optional_string(metadata.get("model"))
                or ""
            ),
            model_id_source=(
                optional_string(metadata.get("model_id_source"))
                or optional_string(metadata.get("model_source"))
                or ""
            ),
            reasoning_effort=(
                optional_string(metadata.get("reasoning_effort"))
                or optional_string(metadata.get("effort"))
                or ""
            ),
            reasoning_effort_source=(
                optional_string(metadata.get("reasoning_effort_source"))
                or optional_string(metadata.get("effort_source"))
                or ""
            ),
            trailer_context=optional_mapping(metadata.get("trailer_context")),
            trailer_context_sources=optional_mapping(
                metadata.get("trailer_context_sources")
            ),
            restart_count=optional_int(metadata.get("restart_count")) or 0,
            max_restarts=optional_int(metadata.get("max_restarts")) or 0,
            lease_seconds=numeric_value(metadata.get("lease_seconds")),
            heartbeat_at=optional_string(metadata.get("heartbeat_at")) or "",
            fencing_token=fencing_token_value(metadata.get("fencing_token")),
            worker_pid=worker_pid,
            worker_process_group_id=optional_int(
                metadata.get("worker_process_group_id")
            ),
            worker_session_id=optional_int(metadata.get("worker_session_id")),
            worker_process_birth_id=(
                optional_string(metadata.get("worker_process_birth_id")) or ""
            ),
            pid_source=pid_source,
            pid_scope=(
                optional_string(metadata.get("pid_scope"))
                or "configured_command_process"
            ),
            supervisor_pid=optional_int(metadata.get("supervisor_pid")),
            host=optional_string(metadata.get("host")) or "",
            lock_path=optional_path(metadata.get("path")),
            workspace=WorkspaceClaim.from_json(metadata.get("workspace")),
            settled_outcome=optional_string(metadata.get("outcome")) or "",
            settled_classification=(
                optional_string(metadata.get("classification")) or ""
            ),
        )

    def with_worker_pid(
        self,
        worker_pid: int,
        *,
        process_group_id: int | None = None,
        session_id: int | None = None,
        process_birth_id: str = "",
    ) -> ActiveRunState:
        return dataclasses.replace(
            self,
            worker_pid=worker_pid,
            worker_process_group_id=process_group_id,
            worker_session_id=session_id,
            worker_process_birth_id=process_birth_id,
        )

    def with_trailer_context(
        self,
        *,
        session_id: str | None = None,
        session_id_source: str | None = None,
        model_provider: str | None = None,
        model_provider_source: str | None = None,
        model_id: str | None = None,
        model_id_source: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_effort_source: str | None = None,
        trailer_context: dict[str, Any] | None = None,
        trailer_context_sources: dict[str, Any] | None = None,
    ) -> ActiveRunState:
        return dataclasses.replace(
            self,
            session_id=session_id if session_id is not None else self.session_id,
            session_id_source=(
                session_id_source
                if session_id_source is not None
                else self.session_id_source
            ),
            model_provider=(
                model_provider if model_provider is not None else self.model_provider
            ),
            model_provider_source=(
                model_provider_source
                if model_provider_source is not None
                else self.model_provider_source
            ),
            model_id=model_id if model_id is not None else self.model_id,
            model_id_source=(
                model_id_source if model_id_source is not None else self.model_id_source
            ),
            reasoning_effort=(
                reasoning_effort
                if reasoning_effort is not None
                else self.reasoning_effort
            ),
            reasoning_effort_source=(
                reasoning_effort_source
                if reasoning_effort_source is not None
                else self.reasoning_effort_source
            ),
            trailer_context=(
                dict(trailer_context)
                if trailer_context is not None
                else self.trailer_context
            ),
            trailer_context_sources=(
                dict(trailer_context_sources)
                if trailer_context_sources is not None
                else self.trailer_context_sources
            ),
        )

    def to_lock_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "schema_version": ACTIVE_RUN_SCHEMA_VERSION,
            "record_type": ACTIVE_RUN_RECORD_TYPE,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "pid": self.worker_pid,
            "worker_pid": self.worker_pid,
            "worker_process_group_id": self.worker_process_group_id,
            "worker_session_id": self.worker_session_id,
            "worker_process_birth_id": self.worker_process_birth_id,
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
            "session_id": self.session_id,
            "session_id_source": self.session_id_source,
            "agent_kind": self.agent_kind,
            "agent_profile": self.agent_profile,
            "agent_prompt_dialect": self.agent_prompt_dialect,
            "agent_prompt_dialect_source": self.agent_prompt_dialect_source,
            "agent_skill_ref_prefix": self.agent_skill_ref_prefix,
            "agent_skill_ref_prefix_source": self.agent_skill_ref_prefix_source,
            "model_provider": self.model_provider,
            "model_provider_source": self.model_provider_source,
            "model_id": self.model_id,
            "model_id_source": self.model_id_source,
            "model": self.model_id,
            "model_source": self.model_id_source,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_effort_source": self.reasoning_effort_source,
            "effort": self.reasoning_effort,
            "effort_source": self.reasoning_effort_source,
            "trailer_context": self.trailer_context,
            "trailer_context_sources": self.trailer_context_sources,
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
        }
        if self.lease_seconds is not None:
            metadata["lease_seconds"] = self.lease_seconds
        if self.heartbeat_at:
            metadata["heartbeat_at"] = self.heartbeat_at
        if self.fencing_token:
            metadata["fencing_token"] = self.fencing_token
        if self.workspace is not None:
            metadata["workspace"] = self.workspace.to_json()
        if self.settled_outcome:
            metadata["outcome"] = self.settled_outcome
        if self.settled_classification:
            metadata["classification"] = self.settled_classification
        return metadata

    def with_settled_outcome(
        self,
        outcome: str,
        classification: str,
    ) -> ActiveRunState:
        return dataclasses.replace(
            self,
            settled_outcome=outcome,
            settled_classification=classification,
        )


@dataclasses.dataclass(frozen=True)
class GitWorktreeEntry:
    path: Path
    head: str = ""
    branch: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "head": self.head,
            "branch": self.branch,
        }


@dataclasses.dataclass(frozen=True)
class WorkspaceGitContext:
    repo: Path
    main_branch: str
    worktrees: tuple[GitWorktreeEntry, ...] = ()
    worktree_list_error: str = ""
    ignored_dirty_paths: tuple[Path, ...] = ()


@dataclasses.dataclass(frozen=True)
class WorkspaceDiagnostic:
    code: str
    severity: str
    message: str
    recovery_hint: str
    recovery_commands: tuple[str, ...] = ()
    details: dict[str, object] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": WORKSPACE_DIAGNOSTIC_SCHEMA_VERSION,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "recovery_hint": self.recovery_hint,
            "recovery_commands": list(self.recovery_commands),
            "details": self.details,
        }


@dataclasses.dataclass(frozen=True)
class WorkspaceGitState:
    status: str
    worktree_exists: bool
    worktree_listed: bool
    current_branch: str = ""
    head_commit: str = ""
    dirty: bool = False
    dirty_summary: tuple[str, ...] = ()
    duplicate_worktrees: tuple[GitWorktreeEntry, ...] = ()
    merged_into: tuple[str, ...] = ()
    diagnostics: tuple[WorkspaceDiagnostic, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "worktree_exists": self.worktree_exists,
            "worktree_listed": self.worktree_listed,
            "current_branch": self.current_branch,
            "head_commit": self.head_commit,
            "dirty": self.dirty,
            "dirty_summary": list(self.dirty_summary),
            "duplicate_worktrees": [
                worktree.to_json() for worktree in self.duplicate_worktrees
            ],
            "merged_into": list(self.merged_into),
            "diagnostics": [diagnostic.to_json() for diagnostic in self.diagnostics],
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
    lifecycle_progress: RunLifecycleProgress = dataclasses.field(
        default_factory=empty_run_lifecycle
    )
    workspace_git_state: WorkspaceGitState | None = None
    workspace_diagnostics: tuple[WorkspaceDiagnostic, ...] = ()

    def to_json(self) -> dict[str, object]:
        payload = {
            "task_id": self.active.task_id,
            "run_id": self.active.run_id,
            "state": self.state,
            "process_state": self.process_state,
            "stale_reason": self.stale_reason,
            "pid": self.active.worker_pid,
            "worker_pid": self.active.worker_pid,
            "worker_process_group_id": self.active.worker_process_group_id,
            "worker_session_id": self.active.worker_session_id,
            # The birth ID embeds this host's boot ID, so status diagnostics
            # report only whether one is known, never its value. The raw value
            # stays in lock metadata, where identity verification needs it.
            "worker_process_birth_id_known": bool(self.active.worker_process_birth_id),
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
            "session_id": self.active.session_id,
            "session_id_source": self.active.session_id_source,
            "agent_kind": self.active.agent_kind,
            "agent_prompt_dialect": self.active.agent_prompt_dialect,
            "agent_prompt_dialect_source": self.active.agent_prompt_dialect_source,
            "agent_skill_ref_prefix": self.active.agent_skill_ref_prefix,
            "agent_skill_ref_prefix_source": self.active.agent_skill_ref_prefix_source,
            "model_provider": self.active.model_provider,
            "model_provider_source": self.active.model_provider_source,
            "model_id": self.active.model_id,
            "model_id_source": self.active.model_id_source,
            "model": self.active.model_id,
            "model_source": self.active.model_id_source,
            "reasoning_effort": self.active.reasoning_effort,
            "reasoning_effort_source": self.active.reasoning_effort_source,
            "effort": self.active.reasoning_effort,
            "effort_source": self.active.reasoning_effort_source,
            "trailer_context": self.active.trailer_context,
            "trailer_context_sources": self.active.trailer_context_sources,
            "restart_count": self.active.restart_count,
            "max_restarts": self.active.max_restarts,
            "lease_seconds": self.active.lease_seconds,
            "heartbeat_at": self.active.heartbeat_at,
            "fencing_token": self.active.fencing_token,
            "lock": str(self.active.lock_path) if self.active.lock_path else "",
            "workspace": (
                self.active.workspace.to_json()
                if self.active.workspace is not None
                else None
            ),
            "workspace_git_state": (
                self.workspace_git_state.to_json()
                if self.workspace_git_state is not None
                else None
            ),
            "workspace_diagnostics": [
                diagnostic.to_json() for diagnostic in self.workspace_diagnostics
            ],
            "result_status": self.result_status,
            "result_finished_at": self.result_finished_at,
            "result_record_type": self.result_record_type,
            "result_metadata": self.result_metadata,
        }
        payload.update(self.lifecycle_progress.to_json())
        redacted = redact_fencing_token_payload(payload)
        assert isinstance(redacted, dict)
        return redacted


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
    fencing_token: str | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> WorkspaceClaim:
    if not branch:
        raise WorkspaceClaimError(
            "missing_branch",
            "workspace claim requires a branch",
        )
    lock = active_task_lock_for_claim(
        lock_manager,
        task_id=task_id,
        run_id=run_id,
        fencing_token=fencing_token,
    )
    worktree_path = resolve_claim_worktree(repo, worktree)
    claim = inspect_workspace_claim(
        task_id=task_id,
        run_id=run_id,
        branch=branch,
        worktree=worktree_path,
        base_commit=base_commit
        or optional_string(lock.metadata.get("base_main"))
        or "",
        started_at=optional_string(lock.metadata.get("started_at")) or "",
        ignored_dirty_paths=ignored_dirty_paths,
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
    fencing_token: str | None = None,
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
            try:
                if fencing_token:
                    validate_lock_fencing_token(
                        {"fencing_token": fencing_token},
                        metadata,
                        path=path,
                    )
            except LockFencingMismatch as exc:
                raise WorkspaceClaimError(
                    "fencing_token_mismatch",
                    "workspace claim refused: fencing token mismatch",
                    details={"lock_path": str(exc.path)},
                ) from exc
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
    started_at: str = "",
    ignored_dirty_paths: Iterable[Path] = (),
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
    status_lines = git_status_lines(worktree, ignored_dirty_paths=ignored_dirty_paths)
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
        started_at=started_at,
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


def git_status_lines(
    repo: Path,
    *,
    ignored_dirty_paths: Iterable[Path] = (),
) -> list[str]:
    return git_lines(repo, *git_status_args(repo, ignored_dirty_paths))


def git_dirty_snapshot(
    repo: Path,
    *,
    ignored_dirty_paths: Iterable[Path] = (),
) -> tuple[list[str], str]:
    status = git_status_lines(repo, ignored_dirty_paths=ignored_dirty_paths)
    excludes = git_status_exclude_pathspecs(repo, ignored_dirty_paths)
    scope = ("--", ".", *excludes)
    evidence: list[str] = ["status", *status]
    for label, args in (
        ("worktree", ("diff", "--binary", "--no-ext-diff", *scope)),
        ("index", ("diff", "--cached", "--binary", "--no-ext-diff", *scope)),
    ):
        result = run_git(repo, *args)
        if result.returncode != 0:
            raise WorkspaceClaimError(
                "git_state_unavailable",
                "workspace dirty snapshot could not be read",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )
        evidence.extend((label, result.stdout))
    untracked = run_git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        *scope,
    )
    if untracked.returncode != 0:
        raise WorkspaceClaimError(
            "git_state_unavailable",
            "workspace untracked files could not be read",
            details={"stderr": untracked.stderr.strip()},
        )
    for relative in sorted(path for path in untracked.stdout.split("\0") if path):
        evidence.extend(
            (
                "untracked",
                relative,
                git_text(repo, "hash-object", "--no-filters", "--", relative),
            )
        )
    digest = hashlib.sha256("\0".join(evidence).encode()).hexdigest()
    return status, digest


def git_status_args(repo: Path, ignored_dirty_paths: Iterable[Path]) -> tuple[str, ...]:
    excludes = git_status_exclude_pathspecs(repo, ignored_dirty_paths)
    if not excludes:
        return ("status", "--short")
    return ("status", "--short", "--", ".", *excludes)


def git_status_exclude_pathspecs(
    repo: Path,
    ignored_dirty_paths: Iterable[Path],
) -> tuple[str, ...]:
    repo = repo.resolve()
    excludes: list[str] = []
    seen: set[str] = set()
    for path in (repo / ".vibe-loop", *ignored_dirty_paths):
        try:
            relative = path.resolve().relative_to(repo)
        except ValueError:
            continue
        if not relative.parts:
            continue
        git_path = relative.as_posix()
        if git_path in seen:
            continue
        seen.add(git_path)
        excludes.append(f":(exclude){git_path}")
    return tuple(excludes)


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


def build_workspace_git_context(
    repo: Path,
    *,
    main_branch: str = "main",
    ignored_dirty_paths: Iterable[Path] = (),
) -> WorkspaceGitContext:
    result = run_git_result(repo, "worktree", "list", "--porcelain")
    if result is None:
        return WorkspaceGitContext(
            repo=repo,
            main_branch=main_branch,
            worktree_list_error="git could not be executed",
            ignored_dirty_paths=tuple(ignored_dirty_paths),
        )
    if result.returncode != 0:
        return WorkspaceGitContext(
            repo=repo,
            main_branch=main_branch,
            worktree_list_error=git_error_text(result),
            ignored_dirty_paths=tuple(ignored_dirty_paths),
        )
    return WorkspaceGitContext(
        repo=repo,
        main_branch=main_branch,
        worktrees=parse_git_worktree_list(result.stdout),
        ignored_dirty_paths=tuple(ignored_dirty_paths),
    )


def parse_git_worktree_list(output: str) -> tuple[GitWorktreeEntry, ...]:
    entries: list[GitWorktreeEntry] = []
    path: Path | None = None
    head = ""
    branch = ""

    def flush() -> None:
        nonlocal path, head, branch
        if path is not None:
            entries.append(GitWorktreeEntry(path=path, head=head, branch=branch))
        path = None
        head = ""
        branch = ""

    for line in output.splitlines():
        if not line:
            flush()
            continue
        key, separator, value = line.partition(" ")
        if key == "worktree" and separator:
            flush()
            path = Path(value).resolve()
        elif key == "HEAD" and separator:
            head = value
        elif key == "branch" and separator:
            branch = short_git_ref(value)
    flush()
    return tuple(entries)


def short_git_ref(value: str) -> str:
    prefix = "refs/heads/"
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def inspect_workspace_git_state(
    active: ActiveRunState,
    context: WorkspaceGitContext,
) -> WorkspaceGitState | None:
    claim = active.workspace
    if claim is None:
        return None

    diagnostics: list[WorkspaceDiagnostic] = []
    if context.worktree_list_error:
        diagnostics.append(
            WorkspaceDiagnostic(
                code="git_worktree_list_unavailable",
                severity="warning",
                message="git worktree list could not be read for workspace diagnostics",
                recovery_hint="Run git worktree list in the repository and inspect active worker locks manually.",
                recovery_commands=(git_command(context.repo, "worktree", "list"),),
                details={"error": context.worktree_list_error},
            )
        )

    branch_worktrees = worktrees_for_branch(context, claim.branch)
    path_worktrees = worktrees_for_path(context, claim.worktree)
    duplicate_worktrees = tuple(branch_worktrees) if len(branch_worktrees) > 1 else ()
    worktree_exists = claim.worktree.exists() and claim.worktree.is_dir()
    worktree_listed = bool(path_worktrees)
    current_branch = ""
    head_commit = ""
    dirty_summary: tuple[str, ...] = ()
    dirty = False

    if not worktree_exists:
        diagnostics.append(
            WorkspaceDiagnostic(
                code="missing_claimed_worktree",
                severity="stale",
                message="active worker lock points at a claimed worktree that is missing",
                recovery_hint="Confirm the worker is no longer using the workspace, then recreate the worktree or remove the stale lock manually.",
                recovery_commands=(
                    git_command(context.repo, "worktree", "list"),
                    git_command(context.repo, "branch", "--list", claim.branch),
                ),
                details={"worktree": str(claim.worktree), "branch": claim.branch},
            )
        )
    else:
        current_branch, branch_error = git_optional_text(
            claim.worktree,
            "branch",
            "--show-current",
        )
        head_commit, head_error = git_optional_text(
            claim.worktree,
            "rev-parse",
            "--verify",
            "HEAD",
        )
        status_text, status_error = git_optional_text(
            claim.worktree,
            *git_status_args(claim.worktree, context.ignored_dirty_paths),
        )
        git_state_error = branch_error or head_error or status_error
        if git_state_error:
            diagnostics.append(
                WorkspaceDiagnostic(
                    code="claimed_worktree_git_unavailable",
                    severity="stale",
                    message="claimed worktree exists but current git state could not be read",
                    recovery_hint="Inspect the claimed path manually before removing any lock or worktree.",
                    recovery_commands=(git_command(claim.worktree, "status"),),
                    details={
                        "worktree": str(claim.worktree),
                        "error": git_state_error,
                    },
                )
            )
        else:
            dirty_summary = tuple(
                line for line in status_text.splitlines()[:DIRTY_SUMMARY_LIMIT] if line
            )
            dirty = bool(dirty_summary)
            if current_branch and current_branch != claim.branch:
                diagnostics.append(
                    stale_lock_worktree_mismatch(
                        context,
                        claim,
                        message="claimed worktree is currently on a different branch",
                        details={
                            "claimed_branch": claim.branch,
                            "current_branch": current_branch,
                        },
                    )
                )
            if dirty:
                diagnostics.append(
                    WorkspaceDiagnostic(
                        code="foreign_dirty_claimed_worktree",
                        severity="warning",
                        message="claimed worker worktree has uncommitted changes",
                        recovery_hint="Treat the worktree as worker-owned; inspect or preserve changes before cleanup.",
                        recovery_commands=(
                            git_command(claim.worktree, "status", "--short"),
                        ),
                        details={
                            "worktree": str(claim.worktree),
                            "dirty_summary": list(dirty_summary),
                        },
                    )
                )

    if worktree_exists and not worktree_listed and not context.worktree_list_error:
        diagnostics.append(
            stale_lock_worktree_mismatch(
                context,
                claim,
                message="claimed worktree is not present in git worktree list",
                details={"worktree": str(claim.worktree), "branch": claim.branch},
            )
        )

    if path_worktrees and all(entry.branch != claim.branch for entry in path_worktrees):
        diagnostics.append(
            stale_lock_worktree_mismatch(
                context,
                claim,
                message="claimed worktree list entry is associated with a different branch",
                details={
                    "claimed_branch": claim.branch,
                    "listed_branches": [entry.branch for entry in path_worktrees],
                },
            )
        )

    if branch_worktrees and all(
        entry.path != claim.worktree.resolve() for entry in branch_worktrees
    ):
        diagnostics.append(
            stale_lock_worktree_mismatch(
                context,
                claim,
                message="claimed branch is checked out at a different worktree path",
                details={
                    "claimed_worktree": str(claim.worktree),
                    "listed_worktrees": [str(entry.path) for entry in branch_worktrees],
                },
            )
        )

    if duplicate_worktrees:
        diagnostics.append(
            WorkspaceDiagnostic(
                code="duplicate_branch_worktrees",
                severity="warning",
                message="more than one git worktree is checked out for the claimed branch",
                recovery_hint="Inspect duplicate worktrees and preserve any local changes before removing one manually.",
                recovery_commands=(git_command(context.repo, "worktree", "list"),),
                details={
                    "branch": claim.branch,
                    "worktrees": [str(entry.path) for entry in duplicate_worktrees],
                },
            )
        )

    branch_ref = local_branch_ref(claim.branch)
    branch_head = git_ref_commit(context.repo, branch_ref)
    branch_has_worker_commits = bool(branch_head) and (
        not claim.base_commit or branch_head != claim.base_commit
    )
    merged_into = (
        merged_branch_targets(context.repo, claim.branch, context.main_branch)
        if branch_has_worker_commits
        else ()
    )
    if merged_into:
        diagnostics.append(
            WorkspaceDiagnostic(
                code="branch_already_merged",
                severity="warning",
                message="active worker branch is already contained in mainline history",
                recovery_hint="Confirm the worker result is recorded before manually cleaning the lock, branch, or worktree.",
                recovery_commands=(
                    git_command(context.repo, "branch", "--contains", claim.branch),
                    git_command(context.repo, "worktree", "list"),
                ),
                details={"branch": claim.branch, "merged_into": list(merged_into)},
            )
        )
    elif not git_ref_exists(context.repo, branch_ref):
        diagnostics.append(
            WorkspaceDiagnostic(
                code="claimed_branch_missing",
                severity="stale",
                message="active worker lock claims a branch that is not available",
                recovery_hint="Inspect the lock and worktree manually before removing the lock or recreating the branch.",
                recovery_commands=(
                    git_command(context.repo, "branch", "--list", claim.branch),
                    git_command(context.repo, "worktree", "list"),
                ),
                details={"branch": claim.branch},
            )
        )

    status = workspace_status(tuple(diagnostics))
    return WorkspaceGitState(
        status=status,
        worktree_exists=worktree_exists,
        worktree_listed=worktree_listed,
        current_branch=current_branch,
        head_commit=head_commit,
        dirty=dirty,
        dirty_summary=dirty_summary,
        duplicate_worktrees=duplicate_worktrees,
        merged_into=merged_into,
        diagnostics=tuple(diagnostics),
    )


def stale_lock_worktree_mismatch(
    context: WorkspaceGitContext,
    claim: WorkspaceClaim,
    *,
    message: str,
    details: dict[str, object],
) -> WorkspaceDiagnostic:
    return WorkspaceDiagnostic(
        code="stale_lock_worktree_mismatch",
        severity="stale",
        message=message,
        recovery_hint="Refresh the worker's workspace claim or clean the stale lock only after confirming ownership.",
        recovery_commands=(
            git_command(context.repo, "worktree", "list"),
            git_command(claim.worktree, "status", "--short"),
        ),
        details=details,
    )


def workspace_status(diagnostics: tuple[WorkspaceDiagnostic, ...]) -> str:
    if any(diagnostic.severity == "stale" for diagnostic in diagnostics):
        return "stale"
    if diagnostics:
        return "warning"
    return "ok"


def worktrees_for_branch(
    context: WorkspaceGitContext,
    branch: str,
) -> tuple[GitWorktreeEntry, ...]:
    return tuple(entry for entry in context.worktrees if entry.branch == branch)


def worktrees_for_path(
    context: WorkspaceGitContext,
    path: Path,
) -> tuple[GitWorktreeEntry, ...]:
    resolved = path.resolve()
    return tuple(entry for entry in context.worktrees if entry.path == resolved)


def git_optional_text(repo: Path, *args: str) -> tuple[str, str]:
    result = run_git_result(repo, *args)
    if result is None:
        return "", "git could not be executed"
    if result.returncode != 0:
        return "", git_error_text(result)
    return result.stdout.strip(), ""


def run_git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return run_git(repo, *args)
    except WorkspaceClaimError:
        return None


def git_error_text(result: subprocess.CompletedProcess[str]) -> str:
    return (
        result.stderr.strip()
        or result.stdout.strip()
        or f"git exited {result.returncode}"
    )


def git_ref_exists(repo: Path, ref: str) -> bool:
    result = run_git_result(
        repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
    )
    return result is not None and result.returncode == 0


def git_ref_commit(repo: Path, ref: str) -> str:
    result = run_git_result(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def merged_branch_targets(
    repo: Path,
    branch: str,
    main_branch: str,
) -> tuple[str, ...]:
    branch_ref = local_branch_ref(branch)
    if not git_ref_exists(repo, branch_ref):
        return ()
    targets = [
        (main_branch, local_branch_ref(main_branch)),
        (f"origin/{main_branch}", remote_main_ref(main_branch)),
    ]
    merged: list[str] = []
    for target_name, target_ref in targets:
        if target_name in merged or not git_ref_exists(repo, target_ref):
            continue
        result = run_git_result(
            repo,
            "merge-base",
            "--is-ancestor",
            branch_ref,
            target_ref,
        )
        if result is not None and result.returncode == 0:
            merged.append(target_name)
    return tuple(merged)


def local_branch_ref(branch: str) -> str:
    if branch.startswith("refs/"):
        return branch
    return f"refs/heads/{branch}"


def remote_main_ref(main_branch: str) -> str:
    branch = short_git_ref(main_branch)
    if branch.startswith("origin/"):
        branch = branch.removeprefix("origin/")
    return f"refs/remotes/origin/{branch}"


def git_command(repo: Path, *args: str) -> str:
    parts = ["git", "-C", str(repo), *args]
    return " ".join(shlex.quote(part) for part in parts)


def build_worker_views(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    repo: Path | None = None,
    main_branch: str = "main",
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> list[WorkerView]:
    host = current_host if current_host is not None else socket.gethostname()
    process_checker = process_exists if process_exists is not None else pid_exists
    records = run_store.read_records()
    result_by_run_id = latest_worker_status_by_run_id(records)
    workspace_context = (
        build_workspace_git_context(
            repo,
            main_branch=main_branch,
            ignored_dirty_paths=ignored_dirty_paths,
        )
        if repo is not None
        else None
    )
    views: list[WorkerView] = []
    for projected_active in load_active_run_states(lock_manager):
        active = restore_projected_worker_process_identity(
            projected_active,
            records,
        )
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
        elif lock_lease_expired(active.to_lock_metadata()):
            state = "stale"
            stale_reason = "lease_expired"
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
        workspace_git_state = (
            inspect_workspace_git_state(active, workspace_context)
            if workspace_context is not None
            else None
        )
        workspace_diagnostics = (
            workspace_git_state.diagnostics if workspace_git_state is not None else ()
        )
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
                lifecycle_progress=worker_lifecycle_progress(active, records),
                workspace_git_state=workspace_git_state,
                workspace_diagnostics=workspace_diagnostics,
            )
        )
    return views


def restore_projected_worker_process_identity(
    active: ActiveRunState,
    records: Sequence[dict[str, Any]],
) -> ActiveRunState:
    """Restore identity fields omitted by a command lock's public projection.

    The local start record is independent evidence captured from the exact
    ``Popen`` child. It is usable only for the same task, run, PID, host, and
    supervisor. Existing projected values are never replaced, so conflicting
    backend identity still reaches the normal fail-closed mismatch checks.
    """

    if (
        active.worker_pid is None
        or not active.run_id
        or not active.task_id
        or not active.host
        or active.supervisor_pid is None
    ):
        return active
    for record in reversed(records):
        if record.get("record_type") != WORKER_PROCESS_STARTED_RECORD_TYPE:
            continue
        if record.get("run_id") != active.run_id:
            continue
        if optional_string(record.get("task_id")) != active.task_id:
            continue
        if optional_int(record.get("worker_pid")) != active.worker_pid:
            continue
        record_host = optional_string(record.get("host"))
        if not record_host:
            continue
        if active.host != record_host:
            continue
        record_supervisor_pid = optional_int(record.get("supervisor_pid"))
        if record_supervisor_pid is None:
            continue
        if active.supervisor_pid != record_supervisor_pid:
            continue
        return dataclasses.replace(
            active,
            worker_process_group_id=(
                active.worker_process_group_id
                if active.worker_process_group_id is not None
                else optional_int(record.get("worker_process_group_id"))
            ),
            worker_session_id=(
                active.worker_session_id
                if active.worker_session_id is not None
                else optional_int(record.get("worker_session_id"))
            ),
            worker_process_birth_id=(
                active.worker_process_birth_id
                or optional_string(record.get("worker_process_birth_id"))
            ),
            pid_source=(
                active.pid_source
                if active.pid_source != "legacy_pid"
                else optional_string(record.get("pid_source")) or active.pid_source
            ),
            pid_scope=(
                active.pid_scope
                or optional_string(record.get("pid_scope"))
                or "configured_command_process"
            ),
        )
    return active


def worker_lifecycle_progress(
    active: ActiveRunState,
    records: list[dict[str, Any]],
) -> RunLifecycleProgress:
    if not active.run_id:
        return empty_run_lifecycle()
    return derive_run_lifecycle(
        [
            record
            for record in records
            if record_matches_worker_identity(
                record,
                run_id=active.run_id,
                task_id=active.task_id,
            )
        ]
    )


def record_matches_worker_identity(
    record: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
) -> bool:
    if record.get("run_id") != run_id:
        return False
    record_task_id = optional_string(record.get("task_id"))
    return record_task_id is None or record_task_id == task_id


def classify_process(
    active: ActiveRunState,
    current_host: str,
    process_exists: ProcessExists | None = None,
    birth_identity_lookup: Callable[[int], str] | None = None,
) -> str:
    """Live-process disposition for one active-run lock.

    When the run recorded a worker birth ID, a live PID alone is not enough:
    the kernel may have recycled that PID for an unrelated process. Comparing
    the recorded birth ID keeps a recycled PID from reading as this worker
    still running. Runs recorded before birth IDs existed keep the plain
    existence check rather than degrading to "missing".
    """

    process_checker = process_exists if process_exists is not None else pid_exists
    get_birth_identity = (
        birth_identity_lookup
        if birth_identity_lookup is not None
        else process_birth_identity
    )
    if active.host and active.host != current_host:
        return "foreign_host"
    if active.worker_pid is None:
        return "unknown_pid"
    if not process_checker(active.worker_pid):
        return "missing"
    if not active.worker_process_birth_id:
        return "running"
    current_birth_id = get_birth_identity(active.worker_pid)
    if not current_birth_id:
        return "running"
    return (
        "running" if current_birth_id == active.worker_process_birth_id else "missing"
    )


def active_run_is_live(
    active: ActiveRunState,
    *,
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
) -> bool:
    """Whether an active-run lock still represents a run that may make progress.

    Mirrors the staleness rules in ``build_worker_views``: a run whose owning
    process is gone (missing pid / no recorded pid), whose lease expired, or
    whose run_id is missing is not live and must not keep holding its
    conflict-domain leases. A run on another host is treated as live (uncertain
    rather than provably dead) so cross-host work still serializes.
    """
    if not active.run_id:
        return False
    if lock_lease_expired(active.to_lock_metadata()):
        return False
    host = current_host if current_host is not None else socket.gethostname()
    process_state = classify_process(active, host, process_exists)
    return process_state not in {"missing", "unknown_pid"}


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


def optional_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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
    started_at: str = ""
    settled_outcome: str = ""
    settled_classification: str = ""
    settlement_pending: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "lock_path": str(self.lock_path),
            "stale_reason": self.stale_reason,
            "kind": self.kind,
            "recovery_command": self.recovery_command,
            "started_at": self.started_at,
            "settlement_pending": self.settlement_pending,
        }


def collect_stale_locks(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    repo: Path | None = None,
    main_branch: str = "main",
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> list[StaleLock]:
    stale: list[StaleLock] = []
    records = run_store.read_records()
    pending_settlements = pending_settlements_by_run_id(records)
    source_settlement_pending = task_source_settlement_pending_run_ids(records)
    for view in build_worker_views(
        lock_manager,
        run_store,
        repo=repo,
        main_branch=main_branch,
        current_host=current_host,
        process_exists=process_exists,
        ignored_dirty_paths=ignored_dirty_paths,
    ):
        if view.state != "stale":
            continue
        lock_path = view.active.lock_path
        if lock_path is None:
            continue
        settlement = pending_settlements.get(view.active.run_id, ("", ""))
        stale.append(
            StaleLock(
                task_id=view.active.task_id,
                run_id=view.active.run_id,
                lock_path=lock_path,
                stale_reason=view.stale_reason or "unknown",
                kind="task",
                recovery_command=stale_lock_recovery_command(
                    lock_manager,
                    lock_path,
                ),
                started_at=view.active.started_at,
                settled_outcome=settlement[0],
                settled_classification=settlement[1],
                settlement_pending=view.active.run_id in source_settlement_pending,
            )
        )

    integration_status = lock_manager.main_integration_status(
        current_host=current_host,
        process_exists=process_exists,
    )
    if integration_status.locked and integration_status.state == "stale":
        owner_task_id = (
            optional_string(integration_status.metadata.get("owner_task_id"))
            or MAIN_INTEGRATION_LOCK_NAME
        )
        stale.append(
            StaleLock(
                task_id=owner_task_id,
                run_id=optional_string(integration_status.metadata.get("run_id")) or "",
                lock_path=integration_status.path,
                stale_reason=integration_status.stale_reason or "unknown",
                kind="integration",
                recovery_command=stale_lock_recovery_command(
                    lock_manager,
                    integration_status.path,
                ),
                started_at=(
                    optional_string(integration_status.metadata.get("owner_started_at"))
                    or optional_string(integration_status.metadata.get("started_at"))
                    or ""
                ),
            )
        )
    return stale


def pending_settlements_by_run_id(
    records: Sequence[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    """Terminal outcomes that are durable locally but never reached their lock.

    Prefer the explicit ``lock_finalization_failed`` event when it exists. If
    that append also failed, the same run's terminal ``run_result`` is the
    fallback source. A later ``lock_released`` for the run clears either source.
    """

    pending: dict[str, tuple[str, str]] = {}
    event_derived: set[str] = set()
    for record in records:
        run_id = optional_string(record.get("run_id"))
        if not run_id:
            continue
        record_type = record.get("record_type")
        if record_type == LOCK_RELEASED_RECORD_TYPE:
            pending.pop(run_id, None)
            event_derived.discard(run_id)
            continue
        if record_type == RUN_RECORD_TYPE:
            classification = optional_string(record.get("classification")) or ""
            if classification in TERMINAL_LOCK_OUTCOMES and run_id not in event_derived:
                pending[run_id] = (classification, classification)
            continue
        if record_type != LOCK_FINALIZATION_FAILED_RECORD_TYPE:
            continue
        outcome = optional_string(record.get("outcome")) or ""
        if outcome not in TERMINAL_LOCK_OUTCOMES:
            continue
        pending[run_id] = (
            outcome,
            optional_string(record.get("classification")) or "",
        )
        event_derived.add(run_id)
    return pending


def task_source_settlement_pending_run_ids(
    records: Sequence[dict[str, Any]],
) -> set[str]:
    pending: set[str] = set()
    runtime_owned: set[str] = set()
    for record in records:
        run_id = optional_string(record.get("run_id"))
        if not run_id:
            continue
        record_type = record.get("record_type")
        if record_type == "run_contract_resolved" and record.get("mode") == (
            "runtime-owned"
        ):
            runtime_owned.add(run_id)
        elif (
            record_type == "stage_transition"
            and record.get("accepted") is True
            and record.get("to_stage") == "activation"
            and run_id in runtime_owned
        ):
            pending.add(run_id)
        elif record_type == "task_source_settlement_attempted":
            pending.add(run_id)
        elif record_type in {
            "task_provenance_committed",
            "task_source_settled",
            LOCK_RELEASED_RECORD_TYPE,
        }:
            pending.discard(run_id)
    return pending


@dataclasses.dataclass(frozen=True)
class CleanResult:
    cleaned: list[StaleLock]
    errors: list[tuple[StaleLock, str]]


def clean_stale_locks(
    stale_locks: list[StaleLock],
    lock_manager: LockManager | None = None,
) -> CleanResult:
    cleaned: list[StaleLock] = []
    errors: list[tuple[StaleLock, str]] = []
    for lock in stale_locks:
        if lock.settlement_pending:
            errors.append(
                (
                    lock,
                    "task-source settlement pending; stage-aware fenced recovery "
                    "must settle the authoritative source before release",
                )
            )
            continue
        if lock_manager is not None:
            try:
                released = lock_manager.release_stale_lock(
                    task_id=lock.task_id,
                    run_id=lock.run_id,
                    path=lock.lock_path,
                    kind=lock.kind,
                    settled_outcome=lock.settled_outcome,
                    settled_classification=lock.settled_classification,
                )
            except LockBackendError as exc:
                errors.append((lock, str(exc)))
                continue
            if released:
                cleaned.append(lock)
            continue
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


def record_expired_locks(run_store: RunStore, stale_locks: list[StaleLock]) -> None:
    for stale_lock in stale_locks:
        if not stale_lock.run_id:
            continue
        run_store.append_lifecycle_event(
            RunLifecycleEvent.lock_event(
                LOCK_EXPIRED_RECORD_TYPE,
                run_id=stale_lock.run_id,
                task_id=stale_lock.task_id,
                lock_kind=stale_lock.kind,
                lock_path=stale_lock.lock_path,
                payload={
                    "stale_reason": stale_lock.stale_reason,
                    "started_at": stale_lock.started_at,
                },
            )
        )


def stale_lock_recovery_command(lock_manager: LockManager, lock_path: Path) -> str:
    if lock_manager.uses_directory_backend:
        return f"rm -rf {shlex.quote(str(lock_path))}"
    return "vibe-loop workers clean --force"


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


WORKTREE_DISPOSITION_SCHEMA_VERSION = 1

# Guardrail reason codes returned for a worktree that must be kept even if the
# analysis agent asked to reap it. They implement the bounded, evidence-gated
# worktree-disposition exception documented in PRD-AUT-006 / PRD-AUT-010.
KEEP_PRIMARY_WORKTREE = "primary_worktree"
KEEP_GIT_STATE_UNAVAILABLE = "git_state_unavailable"
KEEP_LIVE_CLAIM = "live_claim"
KEEP_DIRTY_WORKTREE = "dirty_worktree"
KEEP_LOCAL_MAIN_NOT_CONTAINED = "local_main_not_contained"
KEEP_REMOTE_MAIN_UNAVAILABLE = "remote_main_unavailable"
KEEP_REMOTE_MAIN_NOT_CONTAINED = "remote_main_not_contained"
KEEP_OWNERSHIP_UNVERIFIED = "ownership_unverified"
KEEP_STALE_CLAIM = "stale_claim"
KEEP_TERMINAL_STATUS_UNSUCCESSFUL = "terminal_status_unsuccessful"
KEEP_TERMINAL_COMMIT_MISMATCH = "terminal_commit_mismatch"
KEEP_UNMERGED_WORKTREE = "unmerged_worktree"
KEEP_EVIDENCE_CHANGED = "evidence_changed"


@dataclasses.dataclass(frozen=True)
class WorktreeDispositionEvidence:
    """Mechanically gathered per-worktree evidence for a keep/reap decision.

    Reuses the same git/worker helpers as the read-only status path so the
    analysis agent and the executor judge worktrees from one consistent view:
    ``parse_git_worktree_list`` (via ``build_workspace_git_context``) for
    enumeration, ``merged_branch_targets`` for the merged predicate,
    ``git_status_lines`` for dirty state, and ``build_worker_views`` plus
    ``active_run_is_live`` for the claiming run and its liveness.
    """

    path: Path
    branch: str
    head_commit: str
    is_primary: bool
    local_main_contained: bool
    remote_main_contained: bool
    remote_main_error: str
    merged_into: tuple[str, ...]
    dirty: bool
    dirty_summary: tuple[str, ...]
    git_state_error: str
    claiming_run_id: str
    claiming_task_id: str
    claim_state: str
    claim_is_live: bool
    ownership_error: str
    terminal_status: str
    terminal_commit: str

    @property
    def merged(self) -> bool:
        """Compatibility summary for callers that only need both containment checks."""
        return self.local_main_contained and self.remote_main_contained

    @property
    def keep_guardrails(self) -> tuple[str, ...]:
        return worktree_keep_guardrails(self)

    @property
    def reapable(self) -> bool:
        return not self.keep_guardrails

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": WORKTREE_DISPOSITION_SCHEMA_VERSION,
            "path": str(self.path),
            "branch": self.branch,
            "head_commit": self.head_commit,
            "is_primary": self.is_primary,
            "local_main_contained": self.local_main_contained,
            "remote_main_contained": self.remote_main_contained,
            "remote_main_error": self.remote_main_error,
            "merged": self.merged,
            "merged_into": list(self.merged_into),
            "dirty": self.dirty,
            "dirty_summary": list(self.dirty_summary),
            "git_state_error": self.git_state_error,
            "claiming_run_id": self.claiming_run_id,
            "claiming_task_id": self.claiming_task_id,
            "claim_state": self.claim_state,
            "claim_is_live": self.claim_is_live,
            "ownership_error": self.ownership_error,
            "terminal_status": self.terminal_status,
            "terminal_commit": self.terminal_commit,
            "keep_guardrails": list(self.keep_guardrails),
            "reapable": self.reapable,
        }


def worktree_keep_guardrails(
    evidence: WorktreeDispositionEvidence,
) -> tuple[str, ...]:
    """Guardrail reasons that force a worktree to be kept regardless of decision.

    The executor enforces these independently of the agent so an erroneous or
    stale ``reap`` decision can never remove the primary worktree, a worktree
    whose git state is unreadable, lacks unambiguous successful ownership, is
    only locally merged, is claimed by a live or stale run, or is dirty.
    """
    reasons: list[str] = []
    if evidence.is_primary:
        reasons.append(KEEP_PRIMARY_WORKTREE)
    if evidence.git_state_error:
        reasons.append(KEEP_GIT_STATE_UNAVAILABLE)
    if evidence.claim_is_live:
        reasons.append(KEEP_LIVE_CLAIM)
    if evidence.dirty:
        reasons.append(KEEP_DIRTY_WORKTREE)
    if not evidence.local_main_contained:
        reasons.append(KEEP_LOCAL_MAIN_NOT_CONTAINED)
        reasons.append(KEEP_UNMERGED_WORKTREE)
    if evidence.remote_main_error:
        reasons.append(KEEP_REMOTE_MAIN_UNAVAILABLE)
    elif not evidence.remote_main_contained:
        reasons.append(KEEP_REMOTE_MAIN_NOT_CONTAINED)
    if evidence.ownership_error:
        reasons.append(KEEP_OWNERSHIP_UNVERIFIED)
    if evidence.claim_state == "stale":
        reasons.append(KEEP_STALE_CLAIM)
    if evidence.terminal_status != "completed":
        reasons.append(KEEP_TERMINAL_STATUS_UNSUCCESSFUL)
    if evidence.terminal_commit != evidence.head_commit:
        reasons.append(KEEP_TERMINAL_COMMIT_MISMATCH)
    return tuple(reasons)


def collect_worktree_disposition_evidence(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    repo: Path,
    main_branch: str = "main",
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> list[WorktreeDispositionEvidence]:
    """Enumerate every git worktree with merged/dirty/claim/liveness evidence."""
    repo = repo.resolve()
    context = build_workspace_git_context(
        repo,
        main_branch=main_branch,
        ignored_dirty_paths=ignored_dirty_paths,
    )
    claims_by_path, claims_by_branch = worktree_claims_by_path(
        lock_manager,
        run_store,
        repo=repo,
        main_branch=main_branch,
        current_host=current_host,
        process_exists=process_exists,
        ignored_dirty_paths=ignored_dirty_paths,
    )
    evidence: list[WorktreeDispositionEvidence] = []
    for entry in context.worktrees:
        path = entry.path
        is_primary = path == repo
        dirty, dirty_summary, git_state_error = worktree_dirty_state(
            path,
            context.ignored_dirty_paths,
        )
        merged_into: tuple[str, ...] = ()
        remote_main_error = ""
        if entry.branch and not is_primary:
            merged_into = merged_branch_targets(repo, entry.branch, main_branch)
            if not git_ref_exists(repo, remote_main_ref(main_branch)):
                remote_main_error = "remote main ref is unavailable"
        claims = [*claims_by_path.get(path, ())]
        if entry.branch:
            claims.extend(claims_by_branch.get(entry.branch, ()))
        owner, ownership_error = resolve_worktree_claim_owner(
            claims,
            path=path,
            branch=entry.branch,
        )
        evidence.append(
            WorktreeDispositionEvidence(
                path=path,
                branch=entry.branch,
                head_commit=entry.head,
                is_primary=is_primary,
                local_main_contained=main_branch in merged_into,
                remote_main_contained=(f"origin/{main_branch}" in merged_into),
                remote_main_error=remote_main_error,
                merged_into=merged_into,
                dirty=dirty,
                dirty_summary=dirty_summary,
                git_state_error=git_state_error,
                claiming_run_id=owner.run_id if owner is not None else "",
                claiming_task_id=owner.task_id if owner is not None else "",
                claim_state=owner.state if owner is not None else "",
                claim_is_live=owner.is_live if owner is not None else False,
                ownership_error=ownership_error,
                terminal_status=owner.terminal_status if owner is not None else "",
                terminal_commit=owner.terminal_commit if owner is not None else "",
            )
        )
    return evidence


@dataclasses.dataclass(frozen=True)
class _WorktreeClaim:
    run_id: str
    task_id: str
    branch: str
    worktree: Path
    state: str
    is_live: bool
    terminal_status: str
    terminal_commit: str


def worktree_claims_by_path(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    repo: Path,
    main_branch: str = "main",
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> tuple[
    dict[Path, tuple[_WorktreeClaim, ...]], dict[str, tuple[_WorktreeClaim, ...]]
]:
    records = run_store.read_records()
    reports = worker_reports_by_owner(records)
    claims: list[_WorktreeClaim] = []
    for view in build_worker_views(
        lock_manager,
        run_store,
        repo=repo,
        main_branch=main_branch,
        current_host=current_host,
        process_exists=process_exists,
        ignored_dirty_paths=ignored_dirty_paths,
    ):
        claim = view.active.workspace
        if claim is None:
            continue
        report = reports.get((view.active.task_id, view.active.run_id))
        claims.append(
            _WorktreeClaim(
                run_id=view.active.run_id,
                task_id=view.active.task_id,
                branch=claim.branch,
                worktree=claim.worktree.resolve(),
                state=view.state,
                is_live=active_run_is_live(
                    view.active,
                    current_host=current_host,
                    process_exists=process_exists,
                ),
                terminal_status=report.status if report is not None else "",
                terminal_commit=report.commit if report is not None else "",
            )
        )
    active_owners = {
        (claim.task_id, claim.run_id, claim.branch, claim.worktree) for claim in claims
    }
    for record in records:
        if record.get("record_type") != WORKSPACE_CLAIM_RECORD_TYPE:
            continue
        claim = WorkspaceClaim.from_json(record)
        if claim is None:
            continue
        identity = (claim.task_id, claim.run_id, claim.branch, claim.worktree.resolve())
        if identity in active_owners:
            continue
        report = reports.get((claim.task_id, claim.run_id))
        claims.append(
            _WorktreeClaim(
                run_id=claim.run_id,
                task_id=claim.task_id,
                branch=claim.branch,
                worktree=claim.worktree.resolve(),
                state="released",
                is_live=False,
                terminal_status=report.status if report is not None else "",
                terminal_commit=report.commit if report is not None else "",
            )
        )
    claims_by_path: dict[Path, list[_WorktreeClaim]] = {}
    claims_by_branch: dict[str, list[_WorktreeClaim]] = {}
    for claim in claims:
        claims_by_path.setdefault(claim.worktree, []).append(claim)
        if claim.branch:
            claims_by_branch.setdefault(claim.branch, []).append(claim)
    return (
        {path: tuple(items) for path, items in claims_by_path.items()},
        {branch: tuple(items) for branch, items in claims_by_branch.items()},
    )


def worker_reports_by_owner(
    records: Sequence[dict[str, Any]],
) -> dict[tuple[str, str], WorkerReport]:
    reports: dict[tuple[str, str], WorkerReport] = {}
    for record in records:
        report = WorkerReport.from_record(record)
        if report is None:
            continue
        reports.setdefault((report.task_id, report.run_id), report)
    return reports


def resolve_worktree_claim_owner(
    claims: Iterable[_WorktreeClaim],
    *,
    path: Path,
    branch: str,
) -> tuple[_WorktreeClaim | None, str]:
    unique = {
        (claim.task_id, claim.run_id, claim.branch, claim.worktree): claim
        for claim in claims
    }
    if not unique:
        return None, "no durable workspace claim"
    if len(unique) != 1:
        return None, "multiple task/run claims match this worktree or branch"
    owner = next(iter(unique.values()))
    if owner.worktree != path or not branch or owner.branch != branch:
        return None, "workspace claim does not match the listed branch and path"
    return owner, ""


def worktree_dirty_state(
    path: Path,
    ignored_dirty_paths: Iterable[Path],
) -> tuple[bool, tuple[str, ...], str]:
    if not path.exists() or not path.is_dir():
        return False, (), "claimed worktree path does not exist"
    status_text, status_error = git_optional_text(
        path,
        *git_status_args(path, ignored_dirty_paths),
    )
    if status_error:
        return False, (), status_error
    dirty_summary = tuple(
        line for line in status_text.splitlines()[:DIRTY_SUMMARY_LIMIT] if line
    )
    return bool(dirty_summary), dirty_summary, ""


@dataclasses.dataclass(frozen=True)
class WorktreeDispositionDecision:
    worktree: Path
    action: str
    reason: str = ""

    @classmethod
    def from_json(cls, payload: object) -> WorktreeDispositionDecision | None:
        if not isinstance(payload, dict):
            return None
        worktree = optional_path(payload.get("worktree")) or optional_path(
            payload.get("path")
        )
        action = optional_string(payload.get("action"))
        if worktree is None or action not in {"reap", "keep"}:
            return None
        return cls(
            worktree=worktree.resolve(),
            action=action,
            reason=optional_string(payload.get("reason")) or "",
        )


def parse_worktree_disposition_decisions(
    payload: object,
) -> list[WorktreeDispositionDecision]:
    if isinstance(payload, dict):
        payload = payload.get("decisions")
    if not isinstance(payload, list):
        return []
    decisions: list[WorktreeDispositionDecision] = []
    for item in payload:
        decision = WorktreeDispositionDecision.from_json(item)
        if decision is not None:
            decisions.append(decision)
    return decisions


@dataclasses.dataclass(frozen=True)
class WorktreeReapAction:
    kind: str
    target: str
    ok: bool
    error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "ok": self.ok,
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True)
class WorktreeDispositionOutcome:
    worktree: Path
    branch: str
    requested: str
    applied: str
    reason: str
    guardrails: tuple[str, ...]
    actions: tuple[WorktreeReapAction, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": WORKTREE_DISPOSITION_SCHEMA_VERSION,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "requested": self.requested,
            "applied": self.applied,
            "reason": self.reason,
            "guardrails": list(self.guardrails),
            "actions": [action.to_json() for action in self.actions],
        }


# Side effects are dependency-injected so tests never run real git. AUTO-14
# wires the real ``git_worktree_remove`` / ``git_branch_delete`` wrappers below.
WorktreeRemover = Callable[[Path], str]
BranchDeleter = Callable[[str], str]
WorktreeDispositionRevalidator = Callable[
    [WorktreeDispositionEvidence, str], tuple[str, ...]
]


def execute_worktree_disposition(
    evidence: Iterable[WorktreeDispositionEvidence],
    decisions: Iterable[WorktreeDispositionDecision],
    *,
    remove_worktree: WorktreeRemover,
    delete_branch: BranchDeleter,
    revalidate: WorktreeDispositionRevalidator | None = None,
) -> list[WorktreeDispositionOutcome]:
    """Apply per-worktree keep/reap decisions within the disposition guardrails.

    A ``reap`` decision is honored only when no keep-guardrail applies; otherwise
    the worktree is refused and kept. Per-decision and per-action outcomes are
    returned for journaling. When provided, ``revalidate`` collects fresh
    mechanical evidence immediately before each destructive action and returns
    guardrail codes that require preservation. Side effects are injected so
    tests never run git.
    """
    decision_by_path = {decision.worktree.resolve(): decision for decision in decisions}
    outcomes: list[WorktreeDispositionOutcome] = []
    for item in evidence:
        decision = decision_by_path.get(item.path.resolve())
        guardrails = item.keep_guardrails
        requested = decision.action if decision is not None else "none"
        reason = decision.reason if decision is not None else ""
        if requested != "reap":
            outcomes.append(
                WorktreeDispositionOutcome(
                    worktree=item.path,
                    branch=item.branch,
                    requested=requested,
                    applied="kept",
                    reason=reason,
                    guardrails=guardrails,
                )
            )
            continue
        if guardrails:
            outcomes.append(
                WorktreeDispositionOutcome(
                    worktree=item.path,
                    branch=item.branch,
                    requested=requested,
                    applied="refused",
                    reason=reason,
                    guardrails=guardrails,
                )
            )
            continue
        actions: list[WorktreeReapAction] = []
        refreshed_guardrails = (
            revalidate(item, "worktree_remove") if revalidate is not None else ()
        )
        if refreshed_guardrails:
            outcomes.append(
                WorktreeDispositionOutcome(
                    worktree=item.path,
                    branch=item.branch,
                    requested=requested,
                    applied="refused",
                    reason=reason,
                    guardrails=tuple(
                        dict.fromkeys((*guardrails, *refreshed_guardrails))
                    ),
                )
            )
            continue
        remove_error = remove_worktree(item.path)
        actions.append(
            WorktreeReapAction(
                kind="worktree_remove",
                target=str(item.path),
                ok=not remove_error,
                error=remove_error,
            )
        )
        if not remove_error and item.branch:
            refreshed_guardrails = (
                revalidate(item, "branch_delete") if revalidate is not None else ()
            )
            delete_error = (
                "evidence changed before branch deletion: "
                + ", ".join(refreshed_guardrails)
                if refreshed_guardrails
                else delete_branch(item.branch)
            )
            actions.append(
                WorktreeReapAction(
                    kind="branch_delete",
                    target=item.branch,
                    ok=not delete_error,
                    error=delete_error,
                )
            )
        applied = "reaped" if all(action.ok for action in actions) else "failed"
        outcomes.append(
            WorktreeDispositionOutcome(
                worktree=item.path,
                branch=item.branch,
                requested=requested,
                applied=applied,
                reason=reason,
                guardrails=tuple(dict.fromkeys((*guardrails, *refreshed_guardrails))),
                actions=tuple(actions),
            )
        )
    return outcomes


def worktree_branch_delete_revalidation_guardrails(
    approved: WorktreeDispositionEvidence,
    refreshed: Iterable[WorktreeDispositionEvidence],
    *,
    lock_manager: LockManager,
    run_store: RunStore,
    repo: Path,
    main_branch: str,
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
    ignored_dirty_paths: Iterable[Path] = (),
) -> tuple[str, ...]:
    """Fail closed if a branch changed ownership before its deletion."""
    refreshed_items = tuple(refreshed)
    if any(
        item.path == approved.path or item.branch == approved.branch
        for item in refreshed_items
    ):
        return (KEEP_EVIDENCE_CHANGED,)
    if git_ref_commit(repo, local_branch_ref(approved.branch)) != approved.head_commit:
        return (KEEP_EVIDENCE_CHANGED,)
    merged_into = merged_branch_targets(repo, approved.branch, main_branch)
    if main_branch not in merged_into:
        return (KEEP_EVIDENCE_CHANGED,)
    if not git_ref_exists(repo, remote_main_ref(main_branch)):
        return (KEEP_EVIDENCE_CHANGED,)
    if f"origin/{main_branch}" not in merged_into:
        return (KEEP_EVIDENCE_CHANGED,)
    claims_by_path, claims_by_branch = worktree_claims_by_path(
        lock_manager,
        run_store,
        repo=repo,
        main_branch=main_branch,
        current_host=current_host,
        process_exists=process_exists,
        ignored_dirty_paths=ignored_dirty_paths,
    )
    claims = [*claims_by_path.get(approved.path, ())]
    claims.extend(claims_by_branch.get(approved.branch, ()))
    owner, ownership_error = resolve_worktree_claim_owner(
        claims,
        path=approved.path,
        branch=approved.branch,
    )
    if ownership_error or owner is None:
        return (KEEP_EVIDENCE_CHANGED,)
    if (
        owner.run_id != approved.claiming_run_id
        or owner.task_id != approved.claiming_task_id
        or owner.state != approved.claim_state
        or owner.is_live != approved.claim_is_live
        or owner.terminal_status != approved.terminal_status
        or owner.terminal_commit != approved.terminal_commit
    ):
        return (KEEP_EVIDENCE_CHANGED,)
    return ()


def git_worktree_remove(repo: Path, worktree: Path) -> str:
    result = run_git_result(repo, "worktree", "remove", str(worktree))
    if result is None:
        return "git could not be executed"
    if result.returncode != 0:
        return git_error_text(result)
    return ""


def git_branch_delete(repo: Path, branch: str) -> str:
    result = run_git_result(repo, "branch", "-d", branch)
    if result is None:
        return "git could not be executed"
    if result.returncode != 0:
        return git_error_text(result)
    return ""
