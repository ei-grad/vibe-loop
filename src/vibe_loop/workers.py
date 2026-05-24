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
    WORKSPACE_CLAIM_RECORD_TYPE,
    WORKSPACE_CLAIMED_EVENT_TYPE,
    WORKER_REPORT_RECORD_TYPE,
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
    restart_count: int = 0
    max_restarts: int = 0
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
            restart_count=optional_int(metadata.get("restart_count")) or 0,
            max_restarts=optional_int(metadata.get("max_restarts")) or 0,
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
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
        }
        if self.workspace is not None:
            metadata["workspace"] = self.workspace.to_json()
        return metadata


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
            "restart_count": self.active.restart_count,
            "max_restarts": self.active.max_restarts,
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
        return payload


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


def build_workspace_git_context(
    repo: Path,
    *,
    main_branch: str = "main",
) -> WorkspaceGitContext:
    result = run_git_result(repo, "worktree", "list", "--porcelain")
    if result is None:
        return WorkspaceGitContext(
            repo=repo,
            main_branch=main_branch,
            worktree_list_error="git could not be executed",
        )
    if result.returncode != 0:
        return WorkspaceGitContext(
            repo=repo,
            main_branch=main_branch,
            worktree_list_error=git_error_text(result),
        )
    return WorkspaceGitContext(
        repo=repo,
        main_branch=main_branch,
        worktrees=parse_git_worktree_list(result.stdout),
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
            "status",
            "--short",
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
) -> list[WorkerView]:
    host = current_host if current_host is not None else socket.gethostname()
    process_checker = process_exists if process_exists is not None else pid_exists
    records = run_store.read_records()
    result_by_run_id = latest_worker_status_by_run_id(records)
    workspace_context = (
        build_workspace_git_context(repo, main_branch=main_branch)
        if repo is not None
        else None
    )
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
    repo: Path | None = None,
    main_branch: str = "main",
    current_host: str | None = None,
    process_exists: ProcessExists | None = None,
) -> list[StaleLock]:
    stale: list[StaleLock] = []
    for view in build_worker_views(
        lock_manager,
        run_store,
        repo=repo,
        main_branch=main_branch,
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
