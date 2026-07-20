from __future__ import annotations

import dataclasses
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time as time_module
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from vibe_loop.config import (
    AgentResolutionError,
    REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES,
    REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES,
    VibeConfig,
    command_template_uses_field,
    format_agent_command,
    load_config,
    normalize_registry_runtime_context,
    normalize_registry_runtime_context_assignments,
    prepare_shell_command,
    unresolved_agent_command_message,
    unresolved_prompt_dialect_message,
)
from vibe_loop.locks import (
    AUTOPILOT_LOCK_NAME,
    IntegrationLockStatus,
    LockBackendError,
    LockBusy,
    LockFencingMismatch,
    LockManager,
    LockOwnerMismatch,
    build_lock_manager,
    redact_fencing_token_payload,
)
from vibe_loop.retry import parse_limit_wall_reset_delay
from vibe_loop.runner import VibeRunner, new_run_id
from vibe_loop.runs import (
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
    AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
    AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    RUN_SUPERVISOR_EXITED_RECORD_TYPE,
    RUN_SUPERVISOR_STARTED_RECORD_TYPE,
    RunStore,
    utc_now_iso,
)
from vibe_loop.tasks import BLOCKED_FAMILY_STATUSES, Task
from vibe_loop.workers import (
    ActiveRunState,
    ProcessExists,
    StaleLock,
    WorktreeDispositionDecision,
    WorktreeDispositionEvidence,
    WorktreeDispositionOutcome,
    WorkerView,
    clean_stale_locks,
    collect_stale_locks,
    collect_worktree_disposition_evidence,
    execute_worktree_disposition,
    git_branch_delete,
    git_worktree_remove,
    pid_exists,
    record_expired_locks,
)

RunUntilDoneLauncher = Callable[..., int]
Sleep = Callable[[float], None]


AUTOPILOT_RECORD_SCHEMA_VERSION = 1
AUTOPILOT_RUNTIME_CONTEXT_FD_ENV = "VIBE_LOOP_AUTOPILOT_RUNTIME_CONTEXT_FD"
AUTOPILOT_RUNTIME_CONTEXT_MAX_BYTES = (
    6 * REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES
    + 6 * REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES
    + 2
)
ACTIVE_QUEUE_STATUSES = frozenset({"active"})
BLOCKED_QUEUE_STATUSES = BLOCKED_FAMILY_STATUSES


@dataclasses.dataclass(frozen=True)
class GitStatus:
    current_ref: str = ""
    head: str = ""
    main_ref: str = ""
    main_head: str = ""
    dirty: bool = False
    dirty_summary: tuple[str, ...] = ()
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    available: bool = True
    error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "current_ref": self.current_ref,
            "head": self.head,
            "main_ref": self.main_ref,
            "main_head": self.main_head,
            "dirty": self.dirty,
            "dirty_summary": list(self.dirty_summary),
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "available": self.available,
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True)
class TaskQueueStatus:
    total: int = 0
    runnable: int = 0
    active: int = 0
    done: int = 0
    blocked: int = 0
    statuses: dict[str, int] = dataclasses.field(default_factory=dict)
    runnable_tasks: tuple[dict[str, object], ...] = ()
    source_error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "runnable": self.runnable,
            "active": self.active,
            "done": self.done,
            "blocked": self.blocked,
            "statuses": dict(self.statuses),
            "runnable_tasks": [dict(task) for task in self.runnable_tasks],
            "source_error": self.source_error,
        }


@dataclasses.dataclass(frozen=True)
class SupervisorStatus:
    state: str = "idle"
    pid: int | None = None
    log: Path | None = None
    run_id: str = ""
    cycle_id: str = ""
    observed_at: str = ""
    record: dict[str, Any] | None = None
    blocker: str = ""

    def to_json(self) -> dict[str, object]:
        payload = {
            "state": self.state,
            "pid": self.pid,
            "log": str(self.log) if self.log is not None else "",
            "run_id": self.run_id,
            "cycle_id": self.cycle_id,
            "observed_at": self.observed_at,
            "record": self.record or {},
            "blocker": self.blocker,
        }
        redacted = redact_fencing_token_payload(payload)
        assert isinstance(redacted, dict)
        return redacted


@dataclasses.dataclass(frozen=True)
class CycleSummary:
    cycle_id: str
    status: str
    occurred_at: str
    actions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_wake: str = ""
    record: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "next_wake": self.next_wake,
            "record": dict(self.record),
        }


@dataclasses.dataclass(frozen=True)
class ProjectStatus:
    repo: Path
    display_name: str
    state_dir: Path
    collected_at: str
    main_branch: str
    git: GitStatus
    queue: TaskQueueStatus
    workers: tuple[WorkerView, ...] = ()
    stale_locks: tuple[StaleLock, ...] = ()
    integration_lock: dict[str, object] = dataclasses.field(default_factory=dict)
    agent: dict[str, object] = dataclasses.field(default_factory=dict)
    worktree_disposition_policy: str = "report-only"
    workspace_diagnostics: tuple[dict[str, object], ...] = ()
    supervisor: SupervisorStatus = dataclasses.field(default_factory=SupervisorStatus)
    blockers: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    last_cycle: CycleSummary | None = None
    next_wake: str = ""
    runtime_context: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict[str, object]:
        payload = {
            "repo": str(self.repo),
            "display_name": self.display_name,
            "state_dir": str(self.state_dir),
            "collected_at": self.collected_at,
            "main_branch": self.main_branch,
            "git": self.git.to_json(),
            "queue": self.queue.to_json(),
            "workers": [worker.to_json() for worker in self.workers],
            "stale_locks": [lock.to_json() for lock in self.stale_locks],
            "integration_lock": self.integration_lock,
            "agent": self.agent,
            "worktree_disposition_policy": self.worktree_disposition_policy,
            "workspace_diagnostics": [
                dict(diagnostic) for diagnostic in self.workspace_diagnostics
            ],
            "supervisor": self.supervisor.to_json(),
            "blockers": list(self.blockers),
            "observations": list(self.observations),
            "last_cycle": (
                self.last_cycle.to_json() if self.last_cycle is not None else None
            ),
            "next_wake": self.next_wake,
        }
        redacted = redact_runtime_context_payload(payload, self.runtime_context)
        assert isinstance(redacted, dict)
        fencing_redacted = redact_fencing_token_payload(redacted)
        assert isinstance(fencing_redacted, dict)
        return fencing_redacted


@dataclasses.dataclass(frozen=True)
class AutopilotCycleResult:
    cycle_id: str
    repo: Path
    status: str
    occurred_at: str
    project_status: ProjectStatus
    actions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    child_pid: int | None = None
    child_log: Path | None = None
    next_wake: str = ""
    limit_wall_pause_seconds: float | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
            "cycle_id": self.cycle_id,
            "repo": str(self.repo),
            "status": self.status,
            "occurred_at": self.occurred_at,
            "queue": self.project_status.queue.to_json(),
            "workers": [worker.to_json() for worker in self.project_status.workers],
            "stale_locks": [lock.to_json() for lock in self.project_status.stale_locks],
            "integration_lock": self.project_status.integration_lock,
            "git": self.project_status.git.to_json(),
            "worktree_disposition_policy": (
                self.project_status.worktree_disposition_policy
            ),
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "child_pid": self.child_pid,
            "child_log": str(self.child_log) if self.child_log is not None else "",
            "next_wake": self.next_wake,
            "limit_wall_pause_seconds": self.limit_wall_pause_seconds,
        }

    def append_to(self, run_store: RunStore) -> None:
        run_store.append_record(self.to_json())


def collect_project_status(
    config: VibeConfig,
    *,
    process_exists: ProcessExists | None = None,
) -> ProjectStatus:
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    run_store = RunStore(config.state_path / "runs.jsonl")
    workers = tuple(
        collect_worker_views(
            config,
            run_store,
            process_exists=process_exists,
        )
    )
    stale_locks = tuple(
        collect_stale_locks(
            lock_manager,
            run_store,
            repo=config.repo,
            main_branch=config.main_branch,
            process_exists=process_exists,
        )
    )
    integration_lock = lock_manager.main_integration_status(
        process_exists=process_exists,
    ).to_json()
    git_status = collect_git_status(
        config.repo,
        config.main_branch,
        ignored_dirty_paths=(config.state_path,),
    )
    queue_status = collect_task_queue_status(config)
    agent = config.agent.to_json()
    agent_blockers = agent_blocking_diagnostics(config)
    last_cycle = latest_cycle_summary(run_store)
    supervisor_lock = lock_manager.autopilot_status(process_exists=process_exists)
    supervisor = collect_supervisor_status(
        run_store,
        supervisor_lock=supervisor_lock,
        process_exists=process_exists,
    )
    workspace_diagnostics = tuple(
        diagnostic.to_json()
        for worker in workers
        for diagnostic in worker.workspace_diagnostics
    )
    blockers = tuple(
        project_blockers(
            git_status=git_status,
            queue_status=queue_status,
            stale_locks=stale_locks,
            workspace_diagnostics=workspace_diagnostics,
            integration_lock=integration_lock,
            agent_diagnostics=agent_blockers,
            supervisor=supervisor,
        )
    )
    observations = tuple(
        project_observations(queue_status=queue_status, workers=workers)
    )
    return ProjectStatus(
        repo=config.repo,
        display_name=config.repo.name,
        state_dir=config.state_path,
        collected_at=utc_now_iso(),
        main_branch=config.main_branch,
        git=git_status,
        queue=queue_status,
        workers=workers,
        stale_locks=stale_locks,
        integration_lock=integration_lock,
        agent=agent,
        worktree_disposition_policy=config.autopilot.worktree_disposition,
        workspace_diagnostics=workspace_diagnostics,
        supervisor=supervisor,
        blockers=blockers,
        observations=observations,
        last_cycle=last_cycle,
        next_wake=last_cycle.next_wake if last_cycle is not None else "",
        runtime_context=config.runtime_context,
    )


def collect_worker_views(
    config: VibeConfig,
    run_store: RunStore,
    *,
    process_exists: ProcessExists | None = None,
) -> list[WorkerView]:
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    from vibe_loop.workers import build_worker_views

    return build_worker_views(
        lock_manager,
        run_store,
        repo=config.repo,
        main_branch=config.main_branch,
        process_exists=process_exists,
    )


def collect_task_queue_status(
    config: VibeConfig,
    timeout_seconds: float | None = None,
    *,
    active_runs: tuple[ActiveRunState, ...] | None = None,
) -> TaskQueueStatus:
    effective_config = config
    if timeout_seconds is not None:
        bounded_timeout = max(
            min(config.task_source.command_timeout_seconds, timeout_seconds),
            0.001,
        )
        effective_config = dataclasses.replace(
            config,
            task_source=dataclasses.replace(
                config.task_source,
                command_timeout_seconds=bounded_timeout,
            ),
        )
    runner = VibeRunner(effective_config)
    try:
        tasks = runner.source.list_tasks()
        runnable = runner.list_candidates_from_snapshot(
            tasks,
            active_runs=active_runs,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return TaskQueueStatus(source_error=str(exc))
    except (subprocess.SubprocessError, OSError) as exc:
        # A command-backed source shells out: a nonzero exit raises
        # CalledProcessError, a spawn failure raises OSError, and a hung command
        # now raises TimeoutExpired (see TaskSourceConfig.command_timeout_seconds).
        # None of these are in the parser trio above, so fold them into
        # source_error here — this status collection runs every cycle and on the
        # recheck poll, and a task-source failure must degrade to a blocker
        # rather than propagate and crash the supervisor.
        return TaskQueueStatus(source_error=str(exc))
    statuses: dict[str, int] = {}
    for task in tasks:
        statuses[task.status] = statuses.get(task.status, 0) + 1
    return TaskQueueStatus(
        total=len(tasks),
        runnable=len(runnable),
        active=count_statuses(statuses, ACTIVE_QUEUE_STATUSES),
        done=sum(1 for task in tasks if task.done),
        blocked=count_statuses(statuses, BLOCKED_QUEUE_STATUSES),
        statuses=statuses,
        runnable_tasks=tuple(task_summary(task) for task in runnable),
    )


def count_statuses(statuses: dict[str, int], accepted: frozenset[str]) -> int:
    return sum(
        count for status, count in statuses.items() if status.casefold() in accepted
    )


def task_summary(task: Task) -> dict[str, object]:
    return {
        "id": task.task_id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "source": task.source,
    }


def agent_blocking_diagnostics(config: VibeConfig) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if not config.agent.command:
        diagnostics.append(
            unresolved_agent_command_message(
                "agent.command",
                config.agent.command_source,
                config.agent.detected,
            )
        )
    if config.agent.command and not config.agent.skill_ref_prefix:
        diagnostics.append(
            unresolved_prompt_dialect_message(
                config.agent.agent_kind,
                config.agent.prompt_dialect_source,
            )
        )
    return tuple(diagnostic for diagnostic in diagnostics if diagnostic)


def collect_git_status(
    repo: Path,
    main_branch: str,
    *,
    ignored_dirty_paths: tuple[Path, ...] = (),
) -> GitStatus:
    current_ref, current_error = git_text(repo, "branch", "--show-current")
    head, head_error = git_text(repo, "rev-parse", "--verify", "HEAD")
    main_ref = f"refs/heads/{main_branch}"
    main_head, main_error = git_text(repo, "rev-parse", "--verify", main_ref)
    status, status_error = git_text(
        repo,
        "status",
        "--short",
        "--",
        ".",
        *git_status_excludes(repo, ignored_dirty_paths),
    )
    upstream, _upstream_error = git_text(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    ahead, behind = ahead_behind(repo, upstream)
    errors = tuple(
        error
        for error in (current_error, head_error, main_error, status_error)
        if error
    )
    return GitStatus(
        current_ref=current_ref,
        head=head,
        main_ref=main_ref,
        main_head=main_head,
        dirty=bool(status.strip()),
        dirty_summary=tuple(line for line in status.splitlines() if line),
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        available=not errors,
        error="; ".join(errors),
    )


def git_text(repo: Path, *args: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return "", str(exc)
    if result.returncode != 0:
        return "", result.stderr.strip() or result.stdout.strip()
    return result.stdout.strip(), ""


def git_status_excludes(
    repo: Path, ignored_dirty_paths: tuple[Path, ...]
) -> tuple[str, ...]:
    repo = repo.resolve()
    excludes: list[str] = []
    for path in (repo / ".vibe-loop", *ignored_dirty_paths):
        try:
            relative = path.resolve().relative_to(repo)
        except ValueError:
            continue
        if relative.parts:
            excludes.append(f":(exclude){relative.as_posix()}")
    return tuple(dict.fromkeys(excludes))


def ahead_behind(repo: Path, upstream: str) -> tuple[int, int]:
    if not upstream:
        return 0, 0
    counts, error = git_text(
        repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
    )
    if error:
        return 0, 0
    ahead_text, _separator, behind_text = counts.partition("\t")
    try:
        return int(ahead_text), int(behind_text)
    except ValueError:
        return 0, 0


def collect_supervisor_status(
    run_store: RunStore,
    *,
    supervisor_lock: IntegrationLockStatus | None = None,
    process_exists: ProcessExists | None = None,
) -> SupervisorStatus:
    process_checker = process_exists if process_exists is not None else pid_exists
    records = run_store.read_records()
    supervisor_records = [
        record
        for record in records
        if record.get("record_type")
        in {
            AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
            AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
            AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE,
        }
    ]
    cycle_record = next(
        (
            record
            for record in reversed(records)
            if record.get("record_type") == AUTOPILOT_CYCLE_RECORD_TYPE
        ),
        None,
    )

    if supervisor_lock is not None:
        if supervisor_lock.locked and supervisor_lock.state in {"held", "unknown"}:
            lock_run_id = str(supervisor_lock.metadata.get("run_id") or "")
            lock_pid = int_value(supervisor_lock.metadata.get("pid"))
            matching_records = [
                record
                for record in supervisor_records
                if supervisor_record_matches_lock(
                    record,
                    run_id=lock_run_id,
                    pid=lock_pid,
                )
            ]
            newest_record = matching_records[-1] if matching_records else None
            log = next(
                (
                    path
                    for record in reversed(matching_records)
                    if (path := path_value(record.get("log"))) is not None
                ),
                None,
            )
            return SupervisorStatus(
                state=("running" if supervisor_lock.state == "held" else "observed"),
                pid=lock_pid,
                log=log,
                run_id=lock_run_id,
                cycle_id=(
                    str(newest_record.get("cycle_id") or "")
                    if newest_record is not None
                    else ""
                ),
                observed_at=(
                    str(newest_record.get("occurred_at") or "")
                    if newest_record is not None
                    else str(supervisor_lock.metadata.get("heartbeat_at") or "")
                ),
                record=(newest_record or supervisor_lock.metadata),
            )
        if supervisor_lock.locked and supervisor_lock.state == "stale":
            lock_run_id = str(supervisor_lock.metadata.get("run_id") or "")
            lock_pid = int_value(supervisor_lock.metadata.get("pid"))
            matching_records = [
                record
                for record in supervisor_records
                if supervisor_record_matches_lock(
                    record,
                    run_id=lock_run_id,
                    pid=lock_pid,
                )
            ]
            newest_matching = matching_records[-1] if matching_records else None
            record = dict(newest_matching or supervisor_lock.metadata)
            record["stale_reason"] = supervisor_lock.stale_reason or "unknown"
            return SupervisorStatus(
                state="stale",
                pid=lock_pid,
                log=(
                    path_value(newest_matching.get("log"))
                    if newest_matching is not None
                    else None
                ),
                run_id=lock_run_id,
                observed_at=str(
                    record.get("occurred_at")
                    or supervisor_lock.metadata.get("heartbeat_at")
                    or ""
                ),
                record=record,
            )
    newest_record = supervisor_records[-1] if supervisor_records else None
    # A clean "stopped" is only credible when an explicit terminal stop record
    # exists AND the recorded process is really gone. A record alone can be
    # written by a supervisor that then hangs, and an unlocked singleton lock
    # alone can mean the supervisor lost its lock while still running.
    if newest_record is not None:
        record_pid = int_value(newest_record.get("pid"))
        record_is_terminal = (
            newest_record.get("record_type") == AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE
        )
        # Absence is only verifiable against a recorded PID. A record without one
        # leaves the supervisor's fate unknown, so it can never justify "stopped".
        process_absent = record_pid is not None and not process_checker(record_pid)
        if record_is_terminal:
            if record_pid is None:
                return supervisor_status_from_record(
                    newest_record,
                    state="inconsistent",
                    blocker="autopilot_supervisor_stop_record_missing_pid",
                )
            if process_absent:
                return supervisor_status_from_record(newest_record, state="stopped")
            return supervisor_status_from_record(
                newest_record,
                state="inconsistent",
                blocker="autopilot_supervisor_stop_record_live_process",
            )
        if supervisor_lock is not None and not supervisor_lock.locked:
            if record_pid is None:
                return supervisor_status_from_record(
                    newest_record,
                    state="inconsistent",
                    blocker="autopilot_supervisor_record_missing_pid",
                )
            if process_absent:
                return supervisor_status_from_record(
                    newest_record,
                    state="inconsistent",
                    blocker="autopilot_supervisor_exited_without_stop_record",
                )
            return supervisor_status_from_record(
                newest_record,
                state="inconsistent",
                blocker="autopilot_supervisor_live_without_lock",
            )
    if supervisor_lock is None and newest_record is not None:
        pid = int_value(newest_record.get("pid"))
        if pid and process_checker(pid):
            return supervisor_status_from_record(newest_record, state="running")

    if cycle_record is not None:
        return supervisor_status_from_record(
            cycle_record,
            state=str(cycle_record.get("status") or "idle"),
        )
    if newest_record is not None:
        return supervisor_status_from_record(newest_record, state="observed")
    return SupervisorStatus()


def supervisor_record_matches_lock(
    record: dict[str, Any],
    *,
    run_id: str,
    pid: int | None,
) -> bool:
    record_run_id = str(record.get("run_id") or "")
    record_pid = int_value(record.get("pid"))
    if run_id and record_run_id != run_id:
        return False
    if pid is not None and record_pid != pid:
        return False
    return bool(run_id or pid is not None)


def supervisor_status_from_record(
    record: dict[str, Any],
    *,
    state: str,
    blocker: str = "",
) -> SupervisorStatus:
    return SupervisorStatus(
        state=state,
        pid=int_value(record.get("child_pid")) or int_value(record.get("pid")),
        log=path_value(record.get("child_log") or record.get("log")),
        run_id=str(record.get("run_id") or ""),
        cycle_id=str(record.get("cycle_id") or ""),
        observed_at=str(record.get("occurred_at") or ""),
        record=record,
        blocker=blocker,
    )


def collect_external_run_supervisor(
    run_store: RunStore,
    *,
    process_exists: ProcessExists | None = None,
) -> int | None:
    """PID of a live run-until-done supervisor, or None.

    run-until-done appends start/exit supervisor records to runs.jsonl. The
    newest record per PID wins: a started record whose process is still alive
    marks a live supervisor (whether launched manually or orphaned by a dead
    autopilot), so the autopilot can observe it instead of launching a
    duplicate. PIDs with an exit record or a dead process are ignored.
    """
    process_checker = process_exists if process_exists is not None else pid_exists
    seen_pids: set[int] = set()
    for record in reversed(run_store.read_records()):
        record_type = record.get("record_type")
        if record_type not in {
            RUN_SUPERVISOR_STARTED_RECORD_TYPE,
            RUN_SUPERVISOR_EXITED_RECORD_TYPE,
        }:
            continue
        pid = int_value(record.get("pid"))
        if not pid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        if record_type == RUN_SUPERVISOR_STARTED_RECORD_TYPE and process_checker(pid):
            return pid
    return None


def latest_cycle_summary(run_store: RunStore) -> CycleSummary | None:
    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_CYCLE_RECORD_TYPE:
            continue
        return CycleSummary(
            cycle_id=str(record.get("cycle_id") or ""),
            status=str(record.get("status") or ""),
            occurred_at=str(record.get("occurred_at") or ""),
            actions=string_tuple(record.get("actions")),
            blockers=string_tuple(record.get("blockers")),
            next_wake=str(record.get("next_wake") or ""),
            record=record,
        )
    return None


def recent_cycle_summaries(
    run_store: RunStore,
    *,
    limit: int = 20,
) -> list[CycleSummary]:
    """Return up to ``limit`` most-recent autopilot cycles, newest last."""

    summaries: list[CycleSummary] = []
    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_CYCLE_RECORD_TYPE:
            continue
        summaries.append(
            CycleSummary(
                cycle_id=str(record.get("cycle_id") or ""),
                status=str(record.get("status") or ""),
                occurred_at=str(record.get("occurred_at") or ""),
                actions=string_tuple(record.get("actions")),
                blockers=string_tuple(record.get("blockers")),
                next_wake=str(record.get("next_wake") or ""),
            )
        )
        if len(summaries) >= limit:
            break
    summaries.reverse()
    return summaries


def project_blockers(
    *,
    git_status: GitStatus,
    queue_status: TaskQueueStatus,
    stale_locks: tuple[StaleLock, ...],
    workspace_diagnostics: tuple[dict[str, object], ...],
    integration_lock: dict[str, object],
    agent_diagnostics: tuple[str, ...] = (),
    supervisor: SupervisorStatus | None = None,
) -> list[str]:
    blockers: list[str] = []
    if supervisor is not None and supervisor.blocker:
        blockers.append(supervisor.blocker)
    if not git_status.available:
        blockers.append(f"git_state_unavailable: {git_status.error}")
    if git_status.dirty:
        blockers.append("repo_dirty")
    if queue_status.source_error:
        blockers.append(f"task_source_unavailable: {queue_status.source_error}")
    for diagnostic in agent_diagnostics:
        blockers.append(f"agent_unavailable: {diagnostic}")
    if stale_locks:
        blockers.append("stale_locks_present")
    if any(
        diagnostic.get("severity") == "stale" for diagnostic in workspace_diagnostics
    ):
        blockers.append("stale_workspace_diagnostics_present")
    if integration_lock.get("locked") and integration_lock.get("state") != "available":
        blockers.append("main_integration_lock_unavailable")
    return blockers


def project_observations(
    *,
    queue_status: TaskQueueStatus,
    workers: tuple[WorkerView, ...] = (),
) -> list[str]:
    observations: list[str] = []
    if not queue_status.source_error and queue_status.runnable == 0:
        running_workers = active_conflict_worker_count(workers)
        if running_workers:
            observations.append(f"waiting_for_active_workers:{running_workers}")
        else:
            observations.append("no_runnable_work")
    return observations


def active_conflict_worker_count(workers: tuple[WorkerView, ...]) -> int:
    return sum(1 for worker in workers if worker_holds_active_conflict(worker))


def worker_holds_active_conflict(worker: WorkerView) -> bool:
    if worker.state == "running":
        return True
    return worker.state == "unknown" and worker.process_state == "foreign_host"


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def path_value(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


@dataclasses.dataclass(frozen=True)
class AutopilotRunSummary:
    repo: Path
    run_id: str
    started: bool
    cycles: tuple[AutopilotCycleResult, ...] = ()
    blocker: str = ""
    log: Path | None = None

    @property
    def exit_code(self) -> int:
        if not self.started:
            return 2
        for cycle in self.cycles:
            if cycle.status in {"restartable", "terminated"} or cycle.blockers:
                return 1
        return 0

    def to_json(self) -> dict[str, object]:
        return {
            "repo": str(self.repo),
            "run_id": self.run_id,
            "started": self.started,
            "blocker": self.blocker,
            "log": str(self.log) if self.log is not None else "",
            "cycles": [cycle.to_json() for cycle in self.cycles],
        }


@dataclasses.dataclass(frozen=True)
class DetachedAutopilotLaunch:
    repo: Path
    started: bool
    run_id: str = ""
    pid: int | None = None
    process_group_id: int | None = None
    session_id: int | None = None
    log: Path | None = None
    blocker: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.started else 2

    def to_json(self) -> dict[str, object]:
        return {
            "repo": str(self.repo),
            "started": self.started,
            "run_id": self.run_id,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "session_id": self.session_id,
            "log": str(self.log) if self.log is not None else "",
            "blocker": self.blocker,
        }


@dataclasses.dataclass(frozen=True)
class DetachedAutopilotIdentity:
    run_id: str
    pid: int
    process_group_id: int
    session_id: int
    process_birth_id: str
    record: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class AutopilotStopResult:
    repo: Path
    stopped: bool
    state: str
    run_id: str = ""
    pid: int | None = None
    process_exited: bool = False
    lock_released: bool = False
    recovered: bool = False
    blocker: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.stopped else 2

    def to_json(self) -> dict[str, object]:
        return {
            "repo": str(self.repo),
            "stopped": self.stopped,
            "state": self.state,
            "run_id": self.run_id,
            "pid": self.pid,
            "process_exited": self.process_exited,
            "lock_released": self.lock_released,
            "recovered": self.recovered,
            "blocker": self.blocker,
        }


def autopilot_child_command(
    config: VibeConfig,
    *,
    jobs: int,
    ask_agent: bool,
    continue_on_failure: bool,
    max_slices: int,
    max_tasks: int,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "vibe_loop",
        "run-until-done",
        "--repo",
        str(config.repo),
        "--jobs",
        str(jobs),
    ]
    if ask_agent:
        command.append("--ask-agent")
    if continue_on_failure:
        command.append("--continue-on-failure")
    if max_slices:
        command.extend(["--max-slices", str(max_slices)])
    if max_tasks:
        command.extend(["--max-tasks", str(max_tasks)])
    return command


def detached_autopilot_command(
    config: VibeConfig,
    *,
    jobs: int,
    interval: float,
    once: bool,
    max_cycles: int,
    ask_agent: bool,
    continue_on_failure: bool,
    max_slices: int,
    max_tasks: int,
    min_ready: int,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "vibe_loop",
        "autopilot",
        "run",
        "--repo",
        str(config.repo),
        "--jobs",
        str(jobs),
        "--interval",
        str(interval),
        "--min-ready",
        str(min_ready),
        "--worktree-disposition",
        config.autopilot.worktree_disposition,
    ]
    if once:
        command.append("--once")
    if max_cycles:
        command.extend(["--max-cycles", str(max_cycles)])
    if ask_agent:
        command.append("--ask-agent")
    if continue_on_failure:
        command.append("--continue-on-failure")
    if max_slices:
        command.extend(["--max-slices", str(max_slices)])
    if max_tasks:
        command.extend(["--max-tasks", str(max_tasks)])
    return command


def start_detached_autopilot(
    config: VibeConfig,
    *,
    jobs: int = 1,
    interval: float = 0.0,
    once: bool = False,
    max_cycles: int = 0,
    ask_agent: bool = False,
    continue_on_failure: bool = False,
    max_slices: int = 0,
    max_tasks: int = 0,
    min_ready: int = 1,
    verification_timeout: float = 5.0,
    verification_interval: float = 0.05,
) -> DetachedAutopilotLaunch:
    """Start and verify a detached POSIX autopilot supervisor."""

    if os.name != "posix" or not hasattr(os, "setsid"):
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            blocker=f"detached_autopilot_unsupported_platform:{sys.platform}",
        )

    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    existing = lock_manager.autopilot_status()
    if existing.locked:
        blocker = "autopilot_supervisor_active"
        if existing.state == "stale":
            blocker = (
                f"autopilot_supervisor_lock_stale:{existing.stale_reason or 'unknown'}"
            )
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            run_id=str(existing.metadata.get("run_id") or ""),
            pid=int_value(existing.metadata.get("pid")),
            blocker=blocker,
        )

    launch_id = new_run_id("autopilot-detached")
    log_path = config.state_path / "autopilot" / f"{launch_id}.log"
    command = detached_autopilot_command(
        config,
        jobs=jobs,
        interval=interval,
        once=once,
        max_cycles=max_cycles,
        ask_agent=ask_agent,
        continue_on_failure=continue_on_failure,
        max_slices=max_slices,
        max_tasks=max_tasks,
        min_ready=min_ready,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_environment, context_file = runtime_context_subprocess_transport(
        config.runtime_context
    )
    pass_fds = (context_file.fileno(),) if context_file is not None else ()
    try:
        with log_path.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=config.repo,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                close_fds=True,
                env=child_environment,
                pass_fds=pass_fds,
            )
    except OSError as exc:
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            log=log_path,
            blocker=f"detached_autopilot_launch_failed:{exc}",
        )
    finally:
        if context_file is not None:
            context_file.close()

    try:
        process_group_id = os.getpgid(process.pid)
        session_id = os.getsid(process.pid)
    except OSError:
        process_group_id = None
        session_id = None
    process_birth_id = process_birth_identity(process.pid)

    deadline = time_module.monotonic() + max(0.0, verification_timeout)
    blocker = "detached_autopilot_verification_timeout"
    verified = False
    run_store = RunStore(config.state_path / "runs.jsonl")
    try:
        while True:
            status = lock_manager.autopilot_status()
            lock_run_id = str(status.metadata.get("run_id") or "")
            lock_pid = int_value(status.metadata.get("pid"))
            if (
                status.locked
                and status.state in {"held", "unknown"}
                and lock_pid == process.pid
                and lock_run_id
                # Stop readiness is proven by the supervisor's own local started
                # record, which it writes only after installing termination
                # handlers. A lock-metadata flag would not survive backends that
                # quarantine unknown wire fields.
                and autopilot_supervisor_started_recorded(
                    run_store,
                    repo=config.repo,
                    run_id=lock_run_id,
                    pid=process.pid,
                )
            ):
                if (
                    process.poll() is not None
                    or process_group_id != process.pid
                    or session_id != process.pid
                ):
                    blocker = "detached_autopilot_process_identity_unverified"
                    break
                run_store.append_record(
                    {
                        "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                        "record_type": AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
                        "occurred_at": utc_now_iso(),
                        "repo": str(config.repo),
                        "run_id": lock_run_id,
                        "pid": process.pid,
                        "process_group_id": process_group_id,
                        "session_id": session_id,
                        "process_birth_id": process_birth_id,
                        "log": str(log_path),
                        "observed_state": status.state,
                        "launch_mode": "detached_posix_session",
                        "worktree_disposition_policy": (
                            config.autopilot.worktree_disposition
                        ),
                    }
                )
                verified = True
                return DetachedAutopilotLaunch(
                    repo=config.repo,
                    started=True,
                    run_id=lock_run_id,
                    pid=process.pid,
                    process_group_id=process_group_id,
                    session_id=session_id,
                    log=log_path,
                )
            if status.locked and lock_pid != process.pid:
                blocker = "autopilot_supervisor_active"
                break
            exit_code = process.poll()
            if exit_code is not None:
                blocker = f"detached_autopilot_exited_before_verification:{exit_code}"
                break
            if time_module.monotonic() >= deadline:
                break
            time_module.sleep(max(0.0, verification_interval))
    # Verification crosses pluggable lock backends and the append-only run store;
    # their operational exception sets are not closed over third-party adapters.
    except Exception as exc:
        detail = redact_runtime_context_text(str(exc), config.runtime_context)
        blocker = (
            f"detached_autopilot_verification_failed:{type(exc).__name__}:{detail}"
        )
    finally:
        if not verified:
            cleanup_error = cleanup_detached_candidate(
                process,
                lock_manager=lock_manager,
            )
            if cleanup_error:
                cleanup_error = redact_runtime_context_text(
                    cleanup_error,
                    config.runtime_context,
                )
                blocker = f"{blocker};cleanup_failed:{cleanup_error}"
    return DetachedAutopilotLaunch(
        repo=config.repo,
        started=False,
        pid=process.pid,
        process_group_id=process_group_id,
        session_id=session_id,
        log=log_path,
        blocker=blocker,
    )


def cleanup_detached_candidate(
    process: subprocess.Popen[str],
    *,
    lock_manager: LockManager,
) -> str:
    errors: list[str] = []
    try:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    except (OSError, ChildProcessError) as exc:
        errors.append(f"{type(exc).__name__}:{exc}")
    try:
        status = lock_manager.autopilot_status()
        lock_pid = int_value(status.metadata.get("pid"))
        lock_run_id = str(status.metadata.get("run_id") or "")
        if status.locked and lock_pid == process.pid and lock_run_id:
            lock_manager.release_autopilot(
                run_id=lock_run_id,
                fencing_token=str(status.metadata.get("fencing_token") or ""),
            )
    # Cleanup must preserve the original actionable verification failure even
    # when a third-party lock adapter has an unenumerated operational failure.
    except Exception as exc:
        errors.append(f"{type(exc).__name__}:{exc}")
    return ";".join(errors)


def process_birth_identity(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    if sys.platform != "linux" or pid <= 0:
        return ""
    try:
        boot_id = (
            (proc_root / "sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    _prefix, separator, suffix = stat_text.rpartition(")")
    fields = suffix.split() if separator else []
    if not boot_id or len(fields) <= 19 or not fields[19].isdigit():
        return ""
    return f"{boot_id}:{fields[19]}"


def open_process_pidfd(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return opener(pid, 0)
    if sys.platform != "linux":
        raise OSError("pidfd signaling is unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc_pidfd_open = getattr(libc, "pidfd_open", None)
    if libc_pidfd_open is None:
        raise OSError("pidfd signaling is unavailable")
    libc_pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc_pidfd_open.restype = ctypes.c_int
    pidfd = libc_pidfd_open(pid, 0)
    if pidfd < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return pidfd


def send_process_pidfd_signal(pidfd: int, signal_number: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(pidfd, signal_number)
        return
    if sys.platform != "linux":
        raise OSError("pidfd signaling is unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc_pidfd_send_signal = getattr(libc, "pidfd_send_signal", None)
    if libc_pidfd_send_signal is None:
        raise OSError("pidfd signaling is unavailable")
    libc_pidfd_send_signal.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    libc_pidfd_send_signal.restype = ctypes.c_int
    result = libc_pidfd_send_signal(pidfd, signal_number, None, 0)
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def process_pidfd_exited(pidfd: int) -> bool:
    readable, _writable, _exceptional = select.select([pidfd], [], [], 0)
    return bool(readable)


def detached_autopilot_identity(
    run_store: RunStore,
    *,
    run_id: str,
    pid: int,
) -> DetachedAutopilotIdentity | None:
    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE:
            continue
        if record.get("launch_mode") != "detached_posix_session":
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        if int_value(record.get("pid")) != pid:
            continue
        process_group_id = int_value(record.get("process_group_id"))
        session_id = int_value(record.get("session_id"))
        process_birth_id = str(record.get("process_birth_id") or "")
        if process_group_id is None or session_id is None or not process_birth_id:
            return None
        return DetachedAutopilotIdentity(
            run_id=run_id,
            pid=pid,
            process_group_id=process_group_id,
            session_id=session_id,
            process_birth_id=process_birth_id,
            record=record,
        )
    return None


def autopilot_supervisor_started_recorded(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
    pid: int,
) -> bool:
    """True once the supervisor recorded that its termination handlers are live.

    `run_autopilot` appends this record only after `enable_termination_signals`,
    so its presence is the local trusted contract that a detached supervisor can
    honor a stop signal through its normal cleanup path.
    """

    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE:
            continue
        if str(record.get("repo") or "") != str(repo):
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        if int_value(record.get("pid")) != pid:
            continue
        return True
    return False


def append_autopilot_stopped_record(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
    pid: int | None,
    stop_mode: str,
    signal_number: int | None = None,
    process_exited: bool = True,
    lock_released: bool = True,
) -> None:
    record: dict[str, object] = {
        "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
        "record_type": AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE,
        "occurred_at": utc_now_iso(),
        "repo": str(repo),
        "run_id": run_id,
        "pid": pid,
        "stop_mode": stop_mode,
        "process_exited": process_exited,
        "lock_released": lock_released,
    }
    if signal_number is not None:
        record["signal"] = signal.Signals(signal_number).name
    run_store.append_record(record)


def stop_detached_autopilot(
    config: VibeConfig,
    *,
    timeout: float = 10.0,
    recovery: bool = False,
    run_id: str = "",
    process_exists: ProcessExists | None = None,
    process_group_lookup: Callable[[int], int] | None = None,
    session_lookup: Callable[[int], int] | None = None,
    birth_identity_lookup: Callable[[int], str] | None = None,
    pidfd_open: Callable[[int], int] | None = None,
    pidfd_signal: Callable[[int, int], None] | None = None,
    pidfd_exited: Callable[[int], bool] | None = None,
    close_fd: Callable[[int], None] | None = None,
    sleep: Sleep | None = None,
    monotonic: Callable[[], float] | None = None,
) -> AutopilotStopResult:
    """Stop a verified detached supervisor or explicitly recover its stale lock."""

    checker = process_exists if process_exists is not None else pid_exists
    get_process_group = (
        process_group_lookup if process_group_lookup is not None else os.getpgid
    )
    get_session = session_lookup if session_lookup is not None else os.getsid
    get_birth_identity = (
        birth_identity_lookup
        if birth_identity_lookup is not None
        else process_birth_identity
    )
    open_pidfd = pidfd_open if pidfd_open is not None else open_process_pidfd
    send_pidfd_signal = (
        pidfd_signal if pidfd_signal is not None else send_process_pidfd_signal
    )
    check_pidfd_exited = (
        pidfd_exited if pidfd_exited is not None else process_pidfd_exited
    )
    close_process_fd = close_fd if close_fd is not None else os.close
    sleeper = sleep if sleep is not None else time_module.sleep
    clock = monotonic if monotonic is not None else time_module.monotonic
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    run_store = RunStore(config.state_path / "runs.jsonl")
    backend_deadline = time_module.monotonic() + max(0.0, timeout)
    stop_deadline = clock() + max(0.0, timeout)

    def backend_timeout() -> float:
        return max(0.001, backend_deadline - time_module.monotonic())

    try:
        status = lock_manager.autopilot_status(
            process_exists=checker,
            command_timeout_seconds=backend_timeout(),
        )
    except (LockBackendError, OSError):
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            blocker="autopilot_stop_backend_status_failed",
        )
    if not status.locked:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=True,
            state="already_stopped",
            process_exited=True,
            lock_released=True,
        )

    owner_run_id = str(status.metadata.get("run_id") or "")
    pid = int_value(status.metadata.get("pid"))
    owner_live = pid is not None and checker(pid)
    if recovery:
        owner_host = str(status.metadata.get("host") or "")
        if not owner_host or owner_host != socket.gethostname():
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                blocker=(
                    "autopilot_stale_recovery_identity_unverified:"
                    + ("foreign_host" if owner_host else "missing_host")
                ),
            )
        if owner_live:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                blocker="autopilot_stale_recovery_live_owner",
            )
        # Read the generation this installation last minted, not the one the
        # backend reports: comparing the backend's token against itself would
        # always succeed and fence nothing.
        local_fencing_token = lock_manager.local_fencing_token(AUTOPILOT_LOCK_NAME)
        if not run_id:
            blocker = "autopilot_stale_recovery_missing_run_id"
        elif pid is None:
            # Without a PID from either the lock or the local records, absence
            # cannot be verified and no terminal record could justify "stopped".
            blocker = "autopilot_stale_recovery_missing_pid"
        elif not local_fencing_token:
            blocker = "autopilot_stale_recovery_missing_fencing_token"
        else:
            blocker = ""
        if blocker:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                process_exited=True,
                blocker=blocker,
            )
        try:
            released = lock_manager.recover_stale_autopilot(
                run_id=run_id,
                fencing_token=local_fencing_token,
                process_exists=checker,
                command_timeout_seconds=backend_timeout(),
            )
        except LockOwnerMismatch:
            blocker = "autopilot_stale_recovery_owner_mismatch"
        except LockFencingMismatch:
            blocker = "autopilot_stale_recovery_fencing_mismatch"
        except LockBackendError:
            blocker = "autopilot_stale_recovery_backend_release_failed"
        except OSError:
            blocker = "autopilot_stale_recovery_backend_release_failed"
        else:
            if not released:
                blocker = "autopilot_stale_recovery_lock_changed"
            else:
                try:
                    current = lock_manager.autopilot_status(
                        process_exists=checker,
                        command_timeout_seconds=backend_timeout(),
                    )
                    lock_released = not current.locked or (
                        str(current.metadata.get("run_id") or "") != run_id
                    )
                except (LockBackendError, OSError):
                    lock_released = False
                if not lock_released:
                    blocker = "autopilot_stale_recovery_backend_release_failed"
                else:
                    append_autopilot_stopped_record(
                        run_store,
                        repo=config.repo,
                        run_id=run_id,
                        pid=pid,
                        stop_mode="fenced_stale_recovery",
                    )
                    return AutopilotStopResult(
                        repo=config.repo,
                        stopped=True,
                        state="recovered",
                        run_id=run_id,
                        pid=pid,
                        process_exited=True,
                        lock_released=True,
                        recovered=True,
                    )
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            process_exited=not owner_live,
            blocker=blocker,
        )

    if run_id:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_stop_recovery_identity_requires_recover_stale",
        )
    if not owner_live:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            process_exited=True,
            blocker=(
                "autopilot_supervisor_lock_stale:"
                f"{status.stale_reason or 'missing_process'}"
            ),
        )
    if sys.platform != "linux":
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker=f"autopilot_stop_unsupported_platform:{sys.platform}",
        )
    if status.process_state == "foreign_host":
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_stop_identity_unverified:foreign_host",
        )
    if not owner_run_id or pid is None:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_stop_identity_unverified:missing_lock_identity",
        )
    identity = detached_autopilot_identity(
        run_store,
        run_id=owner_run_id,
        pid=pid,
    )
    if identity is None:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_stop_identity_unverified:missing_detached_record",
        )
    return stop_verified_detached_autopilot(
        config=config,
        lock_manager=lock_manager,
        run_store=run_store,
        identity=identity,
        process_exists=checker,
        process_group_lookup=get_process_group,
        session_lookup=get_session,
        birth_identity_lookup=get_birth_identity,
        pidfd_open=open_pidfd,
        pidfd_signal=send_pidfd_signal,
        pidfd_exited=check_pidfd_exited,
        close_fd=close_process_fd,
        sleep=sleeper,
        monotonic=clock,
        backend_deadline=backend_deadline,
        stop_deadline=stop_deadline,
    )


def stop_verified_detached_autopilot(
    *,
    config: VibeConfig,
    lock_manager: LockManager,
    run_store: RunStore,
    identity: DetachedAutopilotIdentity,
    process_exists: ProcessExists,
    process_group_lookup: Callable[[int], int],
    session_lookup: Callable[[int], int],
    birth_identity_lookup: Callable[[int], str],
    pidfd_open: Callable[[int], int],
    pidfd_signal: Callable[[int, int], None],
    pidfd_exited: Callable[[int], bool],
    close_fd: Callable[[int], None],
    sleep: Sleep,
    monotonic: Callable[[], float],
    backend_deadline: float,
    stop_deadline: float,
) -> AutopilotStopResult:
    try:
        process_fd = pidfd_open(identity.pid)
    except ProcessLookupError:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=identity.run_id,
            pid=identity.pid,
            process_exited=True,
            blocker="autopilot_supervisor_lock_stale:missing_process",
        )
    except OSError:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            run_id=identity.run_id,
            pid=identity.pid,
            blocker="autopilot_stop_identity_unverified:pidfd_unavailable",
        )

    try:
        try:
            actual_process_group = process_group_lookup(identity.pid)
            actual_session = session_lookup(identity.pid)
            actual_birth_id = birth_identity_lookup(identity.pid)
        except OSError:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                process_exited=True,
                blocker="autopilot_supervisor_lock_stale:missing_process",
            )
        if (
            identity.process_group_id != identity.pid
            or identity.session_id != identity.pid
            or actual_process_group != identity.process_group_id
            or actual_session != identity.session_id
            or not actual_birth_id
            or actual_birth_id != identity.process_birth_id
        ):
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                blocker="autopilot_stop_identity_unverified:pid_reuse_or_mismatch",
            )

        try:
            pidfd_signal(process_fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                blocker="autopilot_stop_signal_failed",
            )

        while True:
            try:
                process_exited = pidfd_exited(process_fd)
                current = lock_manager.autopilot_status(
                    process_exists=process_exists,
                    command_timeout_seconds=max(
                        0.001,
                        backend_deadline - time_module.monotonic(),
                    ),
                )
            except KeyboardInterrupt:
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    blocker="autopilot_stop_interrupted",
                )
            except (LockBackendError, OSError):
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    blocker="autopilot_stop_backend_status_failed",
                )
            # The target's lock is gone once the lock is unheld or a different
            # run owns it; a successor supervisor acquiring the singleton between
            # polls must not read as a failed release.
            lock_released = not current.locked or (
                str(current.metadata.get("run_id") or "") != identity.run_id
            )
            if process_exited and lock_released:
                append_autopilot_stopped_record(
                    run_store,
                    repo=config.repo,
                    run_id=identity.run_id,
                    pid=identity.pid,
                    stop_mode="operator_verified",
                    signal_number=signal.SIGTERM,
                )
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=True,
                    state="stopped",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    process_exited=True,
                    lock_released=True,
                )
            if monotonic() >= stop_deadline:
                if process_exited and not lock_released:
                    blocker = "autopilot_stop_backend_release_failed"
                elif not process_exited and lock_released:
                    blocker = "autopilot_stop_process_exit_timeout"
                else:
                    blocker = "autopilot_stop_timeout"
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    process_exited=process_exited,
                    lock_released=lock_released,
                    blocker=blocker,
                )
            try:
                sleep(min(0.05, max(0.0, stop_deadline - monotonic())))
            except KeyboardInterrupt:
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    blocker="autopilot_stop_interrupted",
                )
    finally:
        close_fd(process_fd)


def runtime_context_subprocess_transport(
    runtime_context: tuple[tuple[str, str], ...],
) -> tuple[dict[str, str], BinaryIO | None]:
    environment = os.environ.copy()
    environment.pop(AUTOPILOT_RUNTIME_CONTEXT_FD_ENV, None)
    for name, _value in runtime_context:
        environment.pop(name, None)
    if not runtime_context:
        return environment, None
    encoded = json.dumps(
        dict(runtime_context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > AUTOPILOT_RUNTIME_CONTEXT_MAX_BYTES:
        raise ValueError("autopilot runtime context exceeds transport limit")
    context_file = tempfile.TemporaryFile(mode="w+b")
    context_file.write(encoded)
    context_file.seek(0)
    environment[AUTOPILOT_RUNTIME_CONTEXT_FD_ENV] = str(context_file.fileno())
    return environment, context_file


class AutopilotLockHeartbeat:
    def __init__(
        self,
        lock_manager: LockManager,
        *,
        run_id: str,
        fencing_token: str,
        lease_seconds: int | None,
    ) -> None:
        self.lock_manager = lock_manager
        self.run_id = run_id
        self.fencing_token = fencing_token
        self.interval = (
            max(0.1, min(30.0, lease_seconds / 3))
            if lease_seconds is not None
            else None
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval is None:
            return
        self.thread = threading.Thread(
            target=self._run,
            name=f"autopilot-heartbeat-{self.run_id}",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()

    def _run(self) -> None:
        assert self.interval is not None
        while not self.stop_event.wait(self.interval):
            try:
                self.lock_manager.heartbeat(
                    task_id=AUTOPILOT_LOCK_NAME,
                    run_id=self.run_id,
                    fencing_token=self.fencing_token,
                )
            except (LockOwnerMismatch, LockFencingMismatch):
                return
            except (LockBackendError, OSError):
                continue


def launch_run_until_done(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    on_start: Callable[[int], None] | None = None,
    runtime_context: tuple[tuple[str, str], ...] = (),
) -> int:
    """Run ``run-until-done`` as a child process, streaming output to a log.

    Returns the child exit code. stdout and stderr are merged into the log
    file under the configured state directory so the supervisor never holds
    worker output only in memory.
    """

    log_path.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    child_environment, context_file = runtime_context_subprocess_transport(
        runtime_context
    )
    if context_file is not None:
        popen_kwargs["pass_fds"] = (context_file.fileno(),)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_environment,
                **popen_kwargs,
            )
    finally:
        if context_file is not None:
            context_file.close()
    if on_start is not None:
        on_start(process.pid)
    try:
        return process.wait()
    except KeyboardInterrupt:
        terminate_command_process_group(process)
        raise


def classify_child_exit(exit_code: int) -> str:
    if exit_code == 0:
        return "completed"
    if exit_code < 0:
        return "terminated"
    return "restartable"


LIMIT_WALL_SCAN_MAX_RESULTS = 50


def limit_wall_pause_seconds(
    run_store: RunStore,
    *,
    since: str,
    default_backoff: float,
    now: datetime | None = None,
) -> float | None:
    """Dispatch backoff after a child stopped on a provider limit wall.

    Scans result records finished at or after ``since`` for a ``limit_wall``
    classification and returns the seconds to pause before the next cycle: the
    advertised reset delay when the recorded message carries one, otherwise
    ``default_backoff``. Returns None when no limit wall occurred this cycle, so
    the supervisor keeps its normal cadence. Pure decision function: it reads
    recorded state and never sleeps.
    """
    pause: float | None = None
    for record in run_store.recent_result_records(max_runs=LIMIT_WALL_SCAN_MAX_RESULTS):
        if record.get("classification") != "limit_wall":
            continue
        finished_at = str(record.get("finished_at") or "")
        if since and finished_at and finished_at < since:
            continue
        reset_delay = parse_limit_wall_reset_delay(
            str(record.get("message") or ""), now=now
        )
        candidate = (
            reset_delay if reset_delay is not None else max(0.0, default_backoff)
        )
        pause = candidate if pause is None else max(pause, candidate)
    return pause


AUTOPILOT_COMMAND_MAX_OUTPUT_BYTES = 128 * 1024
AUTOPILOT_COMMAND_TIMEOUT_SECONDS = 120.0
AUTOPILOT_MAINTENANCE_KINDS = ("health", "summary", "troubleshoot", "planning")


@dataclasses.dataclass(frozen=True)
class MaintenanceCommandResult:
    kind: str
    cycle_id: str
    exit_code: int | None
    duration_seconds: float
    output: str
    output_truncated: bool
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 6),
            "output": self.output,
            "output_truncated": self.output_truncated,
            "timed_out": self.timed_out,
        }


MaintenanceRunner = Callable[..., MaintenanceCommandResult]


def maintenance_command_env(
    config: VibeConfig,
    *,
    kind: str,
    cycle_id: str,
    runnable: int,
) -> dict[str, str]:
    return {
        "VIBE_LOOP_AUTOPILOT_COMMAND_KIND": kind,
        "VIBE_LOOP_AUTOPILOT_CYCLE_ID": cycle_id,
        "VIBE_LOOP_REPO": str(config.repo),
        "VIBE_LOOP_STATE_DIR": str(config.state_path),
        "VIBE_LOOP_AUTOPILOT_RUNNABLE": str(runnable),
    }


def terminate_command_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.terminate()
            process.wait(timeout=5.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            kill_command_process_group(process)
            process.wait()
            return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5.0)
    except ProcessLookupError:
        return
    except (OSError, subprocess.TimeoutExpired):
        kill_command_process_group(process)
        process.wait()


def kill_command_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        # process.kill() would reap only the shell, orphaning its children
        # (which then keep the cwd and pipes alive); taskkill /T kills the
        # whole tree.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
    except (ProcessLookupError, OSError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def run_maintenance_command(
    command: str,
    kind: str,
    cycle_id: str,
    *,
    cwd: Path,
    env_extra: dict[str, str],
    timeout: float,
    max_output_bytes: int,
) -> MaintenanceCommandResult:
    """Run a user-authored maintenance command with bounded time and output.

    Output is captured to a temporary file and the command runs in its own
    session so a flood (over ``max_output_bytes``) or a stall (over ``timeout``)
    kills the whole process group rather than orphaning descendants. Recorded
    output is truncated on a byte boundary.
    """

    env = os.environ.copy()
    env.update(env_extra)
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    start = time_module.monotonic()
    with tempfile.TemporaryFile() as buffer:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=True,
                stdout=buffer,
                stderr=subprocess.STDOUT,
                env=env,
                **popen_kwargs,
            )
        except OSError as exc:
            return MaintenanceCommandResult(
                kind=kind,
                cycle_id=cycle_id,
                exit_code=None,
                duration_seconds=time_module.monotonic() - start,
                output=f"could not start: {exc}"[:max_output_bytes],
                output_truncated=False,
                timed_out=False,
            )
        timed_out = False
        size_exceeded = False
        deadline = start + timeout
        try:
            while True:
                code = process.poll()
                if code is not None:
                    break
                buffer.seek(0, os.SEEK_END)
                if buffer.tell() > max_output_bytes:
                    # Brief grace so a command that already wrote its final output
                    # and is exiting reports its real exit code instead of being
                    # misclassified as a flood; a true flooder is still killed.
                    try:
                        process.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        size_exceeded = True
                        kill_command_process_group(process)
                        process.wait()
                    break
                if time_module.monotonic() >= deadline:
                    timed_out = True
                    kill_command_process_group(process)
                    process.wait()
                    break
                time_module.sleep(0.01)
        except KeyboardInterrupt:
            terminate_command_process_group(process)
            raise
        duration = time_module.monotonic() - start
        buffer.seek(0)
        raw = buffer.read()
    exit_code = None if (timed_out or size_exceeded) else process.returncode
    return MaintenanceCommandResult(
        kind=kind,
        cycle_id=cycle_id,
        exit_code=exit_code,
        duration_seconds=duration,
        output=raw[:max_output_bytes].decode("utf-8", errors="replace"),
        output_truncated=size_exceeded or len(raw) > max_output_bytes,
        timed_out=timed_out,
    )


# Returns the parsed JSON decision payload (or ``None``) for a disposition
# prompt. Defaults to ``VibeRunner.run_analysis_agent`` (PRD-AUT-009); injected
# in tests so the read-only analysis agent never runs as a real subprocess.
AnalysisRunner = Callable[[str, Path], dict[str, object] | None]


@dataclasses.dataclass(frozen=True)
class WorktreeDispositionCycleResult:
    """Outcome of one cycle's native worktree-disposition health step.

    Mirrors ``MaintenanceCommandResult`` so the step journals a single typed
    ``autopilot_worktree_reap`` record (PRD-AUT-010/011). ``policy``,
    ``evidence``, and ``outcomes`` carry the operator-selected mode, mechanical
    per-worktree evidence, and per-decision results for full-cycle logging.
    """

    cycle_id: str
    policy: str
    evidence: tuple[WorktreeDispositionEvidence, ...]
    outcomes: tuple[WorktreeDispositionOutcome, ...]
    agent_invoked: bool
    agent_error: str

    @property
    def reaped(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.applied == "reaped")

    @property
    def candidates(self) -> int:
        return sum(1 for item in self.evidence if item.reapable)

    @property
    def kept(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.applied == "kept")

    @property
    def refused(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.applied == "refused")

    @property
    def errors(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.applied == "failed")

    @property
    def status(self) -> str:
        if self.agent_error:
            return "agent_error"
        if self.errors:
            return "errors"
        return "ok"

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "policy": self.policy,
            "status": self.status,
            "candidates": self.candidates,
            "reaped": self.reaped,
            "kept": self.kept,
            "refused": self.refused,
            "errors": self.errors,
            "agent_invoked": self.agent_invoked,
            "agent_error": self.agent_error,
            "evidence": [item.to_json() for item in self.evidence],
            "outcomes": [outcome.to_json() for outcome in self.outcomes],
        }


WorktreeDispositionRunner = Callable[..., WorktreeDispositionCycleResult]


def build_worktree_disposition_prompt(
    candidates: Iterable[WorktreeDispositionEvidence],
) -> str:
    payload = {"worktrees": [item.to_json() for item in candidates]}
    return (
        "You are a read-only autopilot analysis agent deciding whether orphaned "
        "git worktrees may be reaped. Each candidate below already passed the "
        "mechanical safety guardrails (merged, clean, not claimed by a live "
        "run); the executor re-checks those guardrails independently, so a reap "
        "decision is honored only when they still hold. Return ONLY a JSON "
        'object of the form {"decisions": [{"worktree": "<path>", '
        '"action": "keep" | "reap", "reason": "<short reason>"}]}. Decide reap '
        "only for a safe-to-remove leftover of a worker that already finished or "
        "died; otherwise decide keep.\n\n"
        f"Candidates:\n{json.dumps(payload, indent=2)}\n"
    )


def validate_worktree_disposition_decisions(
    payload: object,
    candidates: Iterable[WorktreeDispositionEvidence],
) -> tuple[list[WorktreeDispositionDecision], str]:
    candidate_paths = {item.path.resolve() for item in candidates}
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        return [], "analysis agent returned an invalid disposition schema"
    raw_decisions = payload["decisions"]
    if len(raw_decisions) != len(candidate_paths):
        return [], (
            "analysis agent must return exactly one reasoned disposition decision "
            "per candidate"
        )
    decisions: list[WorktreeDispositionDecision] = []
    seen: set[Path] = set()
    for raw_decision in raw_decisions:
        decision = WorktreeDispositionDecision.from_json(raw_decision)
        if decision is None or not decision.reason.strip():
            return [], "analysis agent returned an invalid or unreasoned decision"
        worktree = decision.worktree.resolve()
        if worktree not in candidate_paths or worktree in seen:
            return [], "analysis agent returned a duplicate or out-of-scope decision"
        seen.add(worktree)
        decisions.append(decision)
    if seen != candidate_paths:
        return [], "analysis agent did not decide every disposition candidate"
    return decisions, ""


def run_worktree_disposition(
    config: VibeConfig,
    *,
    cycle_id: str,
    run_store: RunStore,
    process_exists: ProcessExists | None,
    analysis_runner: AnalysisRunner | None = None,
    remove_worktree: Callable[[Path], str] | None = None,
    delete_branch: Callable[[str], str] | None = None,
) -> WorktreeDispositionCycleResult:
    """Run the native, evidence-gated worktree-disposition health step.

    Gathers per-worktree evidence (AUTO-13). The default report-only policy
    journals eligible candidates without invoking an agent or mutating git. An
    explicit reap policy asks the read-only analysis agent (AUTO-12) for
    decisions and executes them within the mechanical guardrails. Git side
    effects and the analysis call are dependency-injected so tests never run
    real git or spawn an agent. Stays inside the bounded PRD-AUT-006 exception.
    """
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    evidence = collect_worktree_disposition_evidence(
        lock_manager,
        run_store,
        repo=config.repo,
        main_branch=config.main_branch,
        process_exists=process_exists,
        ignored_dirty_paths=(config.state_path,),
    )
    reapable = [item for item in evidence if item.reapable]
    agent_invoked = False
    agent_error = ""
    decisions = []
    if reapable and config.autopilot.worktree_disposition == "reap":
        agent_invoked = True
        runner = analysis_runner or VibeRunner(config).run_analysis_agent
        output_path = (
            config.state_path / "autopilot" / f"{cycle_id}-worktree-disposition.json"
        )
        try:
            payload = runner(build_worktree_disposition_prompt(reapable), output_path)
        except AgentResolutionError as exc:
            payload = None
            agent_error = str(exc)
        if payload is None and not agent_error:
            agent_error = "analysis agent returned no disposition decisions"
        if payload is not None:
            decisions, agent_error = validate_worktree_disposition_decisions(
                payload,
                reapable,
            )
        if agent_error:
            decisions = [
                WorktreeDispositionDecision(
                    worktree=item.path,
                    action="keep",
                    reason="analysis disposition response was rejected",
                )
                for item in reapable
            ]
    elif reapable:
        decisions = [
            WorktreeDispositionDecision(
                worktree=item.path,
                action="keep",
                reason="worktree disposition policy is report-only",
            )
            for item in reapable
        ]
    remover = remove_worktree or (
        lambda worktree: git_worktree_remove(config.repo, worktree)
    )
    deleter = delete_branch or (lambda branch: git_branch_delete(config.repo, branch))
    outcomes = execute_worktree_disposition(
        evidence,
        decisions,
        remove_worktree=remover,
        delete_branch=deleter,
    )
    return WorktreeDispositionCycleResult(
        cycle_id=cycle_id,
        policy=config.autopilot.worktree_disposition,
        evidence=tuple(evidence),
        outcomes=tuple(outcomes),
        agent_invoked=agent_invoked,
        agent_error=agent_error,
    )


NATIVE_PLANNING_TEXT_LIMIT = 4096
NATIVE_PLANNING_EVIDENCE_TASK_LIMIT = 50
NATIVE_PLANNING_EVIDENCE_WORKER_LIMIT = 50
NATIVE_PLANNING_DECISION_KEYS = frozenset({"should_plan", "reason", "objective"})


@dataclasses.dataclass(frozen=True)
class NativePlanningDecision:
    cycle_id: str
    runnable: int
    min_ready: int
    status: str
    should_plan: bool
    reason: str
    objective: str
    agent_invoked: bool
    agent_error: str = ""

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "stage": "read_only_detection",
            "runnable": self.runnable,
            "min_ready": self.min_ready,
            "status": self.status,
            "should_plan": self.should_plan,
            "reason": self.reason,
            "objective": self.objective,
            "agent_invoked": self.agent_invoked,
            "agent_error": self.agent_error,
        }


@dataclasses.dataclass(frozen=True)
class NativePlanningWorkerResult:
    cycle_id: str
    phase: str
    status: str
    requested: bool
    attempted: bool
    started: bool
    pid: int | None
    exit_code: int | None
    log_path: Path | None
    runnable_before: int
    runnable_after: int | None
    timeout_seconds: float = 0.0
    timed_out: bool = False
    task_source_error: str = ""
    error: str = ""

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "stage": "read_write_authoring",
            "phase": self.phase,
            "status": self.status,
            "requested": self.requested,
            "attempted": self.attempted,
            "started": self.started,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "log": str(self.log_path) if self.log_path is not None else "",
            "runnable_before": self.runnable_before,
            "runnable_after": self.runnable_after,
            "timeout_seconds": self.timeout_seconds,
            "timed_out": self.timed_out,
            "task_source_error": self.task_source_error,
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True)
class NativePlanningCycleResult:
    decision: NativePlanningDecision
    worker: NativePlanningWorkerResult


NativePlanningRunner = Callable[..., NativePlanningCycleResult]


@dataclasses.dataclass(frozen=True)
class NativePlanningProcessResult:
    exit_code: int
    pid: int
    timed_out: bool = False


PlanningWorkerLauncher = Callable[..., NativePlanningProcessResult]


class NativePlanningWorkerInterrupted(KeyboardInterrupt):
    def __init__(
        self,
        result: NativePlanningProcessResult,
        interruption: KeyboardInterrupt,
    ):
        super().__init__(*interruption.args)
        self.result = result
        self.interruption = interruption


def _bounded_planning_text(value: object) -> str:
    return str(value or "").strip()[:NATIVE_PLANNING_TEXT_LIMIT]


def build_native_planning_decision_prompt(
    status: ProjectStatus,
    *,
    min_ready: int,
) -> str:
    workers = []
    for worker in status.workers[:NATIVE_PLANNING_EVIDENCE_WORKER_LIMIT]:
        payload = worker.to_json()
        workers.append(
            {
                key: payload.get(key)
                for key in (
                    "task_id",
                    "run_id",
                    "state",
                    "process_state",
                    "lifecycle_state",
                )
            }
        )
    queue = status.queue.to_json()
    runnable_tasks = queue["runnable_tasks"]
    assert isinstance(runnable_tasks, list)
    queue["runnable_tasks"] = runnable_tasks[:NATIVE_PLANNING_EVIDENCE_TASK_LIMIT]
    evidence = {
        "queue": queue,
        "workers": workers,
        "min_ready": min_ready,
        "planning_evidence_task_limit": NATIVE_PLANNING_EVIDENCE_TASK_LIMIT,
        "runnable_tasks_omitted": max(
            0, len(runnable_tasks) - NATIVE_PLANNING_EVIDENCE_TASK_LIMIT
        ),
        "planning_evidence_worker_limit": NATIVE_PLANNING_EVIDENCE_WORKER_LIMIT,
        "workers_omitted": max(
            0, len(status.workers) - NATIVE_PLANNING_EVIDENCE_WORKER_LIMIT
        ),
    }
    return (
        "You are a read-only autopilot planning analyst. Inspect the repository's "
        "task source, PRDs/specs, roadmaps, TODOs, recent work evidence, and the "
        "bounded runtime evidence below. Decide whether the ready queue needs new "
        "task content and, if so, state a bounded planning objective. Do not edit "
        "files, mutate the task source, create tasks, change task status, or run a "
        "write-capable agent. Return ONLY a JSON object of the form "
        '{"should_plan": true | false, "reason": "<short reason>", '
        '"objective": "<what the separate read-write planning worker should plan>"}. '
        "Set objective to an empty string when should_plan is false.\n\n"
        f"Runtime evidence:\n{json.dumps(evidence, indent=2)}\n"
    )


def validate_native_planning_decision(
    payload: object,
) -> tuple[bool, str, str, str]:
    if not isinstance(payload, dict) or set(payload) != NATIVE_PLANNING_DECISION_KEYS:
        return False, "", "", "analysis agent returned an invalid planning schema"
    if (
        not isinstance(payload["should_plan"], bool)
        or not isinstance(payload["reason"], str)
        or not isinstance(payload["objective"], str)
    ):
        return False, "", "", "analysis agent returned an invalid planning schema"
    should_plan = payload["should_plan"]
    reason = _bounded_planning_text(payload.get("reason"))
    objective = _bounded_planning_text(payload.get("objective"))
    if not reason:
        return (
            False,
            "",
            "",
            "analysis agent returned a planning decision without a reason",
        )
    if should_plan and not objective:
        return False, "", "", "analysis agent requested planning without an objective"
    if not should_plan and objective:
        return (
            False,
            "",
            "",
            "analysis agent returned an objective for a no-plan decision",
        )
    return should_plan, reason, objective, ""


def build_native_planning_worker_prompt(
    config: VibeConfig,
    decision: NativePlanningDecision,
) -> str:
    skill_prefix = config.agent.require_skill_ref_prefix()
    return (
        f"{skill_prefix}orchestrated-vibe-loop\n\n"
        "You are the separate read-write planning worker for an autopilot cycle. "
        "The preceding read-only analysis agent decided that the runnable queue "
        "needs replenishment. Inspect the repository's authoritative task source "
        "and planning inputs, then author enough reviewed, dependency-aware ready "
        "task content to satisfy the objective below. Use isolated worktrees and "
        "the repository's normal review/integration workflow. Do not implement the "
        "planned product tasks. Do not mark unrelated or unfinished tasks complete. "
        "The task source remains authoritative; the autopilot supervisor will only "
        "observe your exit and re-read it afterward.\n\n"
        f"Analysis reason: {decision.reason}\n"
        f"Planning objective: {decision.objective}\n"
        f"Runnable depth before planning: {decision.runnable}/{decision.min_ready}\n"
    )


def launch_native_planning_worker(
    command: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    on_start: Callable[[int], None],
) -> NativePlanningProcessResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, use_shell = prepare_shell_command(command)
    popen_kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=use_shell,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
        on_start(process.pid)
        try:
            timeout = timeout_seconds if timeout_seconds > 0 else None
            exit_code = process.wait(timeout=timeout)
            return NativePlanningProcessResult(
                exit_code=exit_code,
                pid=process.pid,
            )
        except subprocess.TimeoutExpired:
            kill_command_process_group(process)
            exit_code = process.wait()
            return NativePlanningProcessResult(
                exit_code=exit_code,
                pid=process.pid,
                timed_out=True,
            )
        except KeyboardInterrupt as exc:
            terminate_command_process_group(process)
            raise NativePlanningWorkerInterrupted(
                NativePlanningProcessResult(
                    exit_code=process.returncode,
                    pid=process.pid,
                ),
                exc,
            ) from None


def run_native_planning(
    config: VibeConfig,
    *,
    cycle_id: str,
    status: ProjectStatus,
    min_ready: int,
    run_store: RunStore,
    analysis_runner: AnalysisRunner | None = None,
    worker_launcher: PlanningWorkerLauncher = launch_native_planning_worker,
) -> NativePlanningCycleResult:
    runner = analysis_runner or VibeRunner(config).run_analysis_agent
    output_path = config.state_path / "autopilot" / f"{cycle_id}-planning-decision.json"
    agent_error = ""
    try:
        payload = runner(
            build_native_planning_decision_prompt(status, min_ready=min_ready),
            output_path,
        )
    except (
        AgentResolutionError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        payload = None
        agent_error = _bounded_planning_text(exc)
    if payload is None and not agent_error:
        agent_error = "analysis agent returned no planning decision"
    should_plan = False
    reason = ""
    objective = ""
    if payload is not None:
        should_plan, reason, objective, agent_error = validate_native_planning_decision(
            payload
        )
    decision = NativePlanningDecision(
        cycle_id=cycle_id,
        runnable=status.queue.runnable,
        min_ready=min_ready,
        status="analysis_error" if agent_error else "decided",
        should_plan=should_plan,
        reason=reason,
        objective=objective,
        agent_invoked=True,
        agent_error=agent_error,
    )
    run_store.append_record(decision.to_record(config.repo))

    if agent_error or not should_plan:
        worker = NativePlanningWorkerResult(
            cycle_id=cycle_id,
            phase="terminal",
            status="skipped_analysis_error" if agent_error else "skipped_not_needed",
            requested=False,
            attempted=False,
            started=False,
            pid=None,
            exit_code=None,
            log_path=None,
            runnable_before=status.queue.runnable,
            runnable_after=status.queue.runnable,
            error=agent_error,
        )
        run_store.append_record(worker.to_record(config.repo))
        return NativePlanningCycleResult(decision=decision, worker=worker)

    log_path = config.state_path / "autopilot" / f"{cycle_id}-planning-worker.log"
    worker_error = ""
    launch_attempted = False
    started_pid: int | None = None
    process_result: NativePlanningProcessResult | None = None
    interruption: KeyboardInterrupt | None = None

    def record_worker_started(pid: int) -> None:
        nonlocal started_pid
        if started_pid is not None:
            return
        started_pid = pid
        run_store.append_record(
            NativePlanningWorkerResult(
                cycle_id=cycle_id,
                phase="started",
                status="started",
                requested=True,
                attempted=True,
                started=True,
                pid=pid,
                exit_code=None,
                log_path=log_path,
                runnable_before=status.queue.runnable,
                runnable_after=None,
                timeout_seconds=config.supervision.worker_timeout_seconds,
            ).to_record(config.repo)
        )

    try:
        command_template = config.agent.require_command()
        if not command_template_uses_field(command_template, "prompt"):
            raise AgentResolutionError(
                "agent.command must include {prompt} for native planning so the "
                "read-write worker receives its planning objective"
            )
        command = format_agent_command(
            command_template,
            prompt=build_native_planning_worker_prompt(config, decision),
            model=config.agent.model,
        )
        launch_attempted = True
        process_result = worker_launcher(
            command,
            cwd=config.repo,
            log_path=log_path,
            timeout_seconds=config.supervision.worker_timeout_seconds,
            on_start=record_worker_started,
        )
        record_worker_started(process_result.pid)
    except NativePlanningWorkerInterrupted as exc:
        process_result = exc.result
        record_worker_started(process_result.pid)
        interruption = exc.interruption
    except (
        AgentResolutionError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        worker_error = _bounded_planning_text(exc)

    task_source_error = ""
    runnable_after: int | None = status.queue.runnable
    if started_pid is not None and interruption is None:
        queue_after = collect_task_queue_status(config)
        task_source_error = _bounded_planning_text(queue_after.source_error)
        runnable_after = None if task_source_error else queue_after.runnable
    if interruption is not None:
        worker_status = "interrupted"
    elif worker_error:
        worker_status = "worker_error"
    elif process_result is not None and process_result.timed_out:
        worker_status = "timed_out"
    elif task_source_error:
        worker_status = "task_source_error"
    elif process_result is not None and process_result.exit_code == 0:
        worker_status = "completed"
    else:
        worker_status = "failed"
    worker = NativePlanningWorkerResult(
        cycle_id=cycle_id,
        phase="terminal",
        status=worker_status,
        requested=True,
        attempted=launch_attempted,
        started=started_pid is not None,
        pid=started_pid,
        exit_code=process_result.exit_code if process_result is not None else None,
        log_path=log_path if log_path.exists() else None,
        runnable_before=status.queue.runnable,
        runnable_after=runnable_after,
        timeout_seconds=config.supervision.worker_timeout_seconds,
        timed_out=process_result.timed_out if process_result is not None else False,
        task_source_error=task_source_error,
        error=worker_error,
    )
    run_store.append_record(worker.to_record(config.repo))
    if interruption is not None:
        raise interruption
    return NativePlanningCycleResult(decision=decision, worker=worker)


def execute_autopilot_cycle(
    config: VibeConfig,
    *,
    cycle_id: str,
    jobs: int,
    ask_agent: bool,
    continue_on_failure: bool,
    max_slices: int,
    max_tasks: int,
    min_ready: int,
    next_wake: str,
    process_exists: ProcessExists | None,
    launcher: RunUntilDoneLauncher,
    run_store: RunStore,
    maintenance_runner: MaintenanceRunner = run_maintenance_command,
    worktree_disposition_runner: WorktreeDispositionRunner = run_worktree_disposition,
    native_planning_runner: NativePlanningRunner = run_native_planning,
    command_timeout: float = AUTOPILOT_COMMAND_TIMEOUT_SECONDS,
    command_max_output_bytes: int = AUTOPILOT_COMMAND_MAX_OUTPUT_BYTES,
) -> AutopilotCycleResult:
    min_ready = require_positive_min_ready(min_ready)
    cycle_started_at = utc_now_iso()
    status = collect_project_status(config, process_exists=process_exists)
    runnable = status.queue.runnable
    actions: list[str] = []
    child_pid: int | None = None
    child_log: Path | None = None
    cleanup_errors = 0

    cleanup_candidates = tuple(
        lock for lock in status.stale_locks if lock.stale_reason == "missing_process"
    )
    if cleanup_candidates:
        lock_manager = build_lock_manager(
            config.repo,
            config.state_path / "locks",
            config.locks,
            runtime_context=config.runtime_environment,
        )
        clean_result = clean_stale_locks(list(cleanup_candidates), lock_manager)
        record_expired_locks(run_store, clean_result.cleaned)
        if clean_result.cleaned:
            actions.append(f"cleaned_stale_locks:{len(clean_result.cleaned)}")
        if clean_result.errors:
            cleanup_errors = len(clean_result.errors)
            actions.append(f"stale_lock_cleanup_errors:{cleanup_errors}")
        status = collect_project_status(config, process_exists=process_exists)
        runnable = status.queue.runnable

    disposition = worktree_disposition_runner(
        config,
        cycle_id=cycle_id,
        run_store=run_store,
        process_exists=process_exists,
    )
    run_store.append_record(disposition.to_record(config.repo))
    actions.append(f"worktree_disposition_policy:{disposition.policy}")
    actions.append(f"worktree_disposition_candidates:{disposition.candidates}")
    actions.append(f"reaped_worktrees:{disposition.reaped}")
    if disposition.errors:
        actions.append(f"worktree_reap_errors:{disposition.errors}")
    if disposition.agent_error:
        actions.append("worktree_disposition_agent_error")

    blocker_list = list(status.blockers)
    if not config.autopilot.require_clean_repo and "repo_dirty" in blocker_list:
        blocker_list.remove("repo_dirty")
        actions.append("repo_dirty_ignored")
    if cleanup_errors:
        blocker_list.append("stale_lock_cleanup_failed")

    def run_maintenance(kind: str) -> MaintenanceCommandResult | None:
        command = config.autopilot.maintenance_command(kind)
        if not command:
            return None
        result = maintenance_runner(
            command,
            kind,
            cycle_id,
            cwd=config.repo,
            env_extra=maintenance_command_env(
                config, kind=kind, cycle_id=cycle_id, runnable=runnable
            ),
            timeout=command_timeout,
            max_output_bytes=command_max_output_bytes,
        )
        run_store.append_record(result.to_record(config.repo))
        actions.append(f"ran_{kind}_command:exit={result.exit_code}")
        return result

    if not blocker_list:
        health = run_maintenance("health")
        if health is not None and not health.succeeded:
            blocker_list.append("autopilot_health_failed")

    blockers = tuple(blocker_list)
    if blockers:
        cycle_status = "blocked"
        actions.append("blocked_preflight")
    elif runnable < min_ready:
        cycle_status = "idle"
        active_conflict_workers = active_conflict_worker_count(status.workers)
        planning = run_maintenance("planning")
        if planning is None:
            native_planning = native_planning_runner(
                config,
                cycle_id=cycle_id,
                status=status,
                min_ready=min_ready,
                run_store=run_store,
            )
            if native_planning.decision.agent_error:
                actions.append("native_planning_analysis_error")
            elif native_planning.decision.should_plan:
                actions.append("native_planning_decision:plan")
            else:
                actions.append("native_planning_decision:no_plan")
            if native_planning.worker.attempted:
                actions.append(
                    "native_planning_worker:"
                    f"{native_planning.worker.status}:"
                    f"exit={native_planning.worker.exit_code}"
                )
                actions.append(
                    "native_planning_runnable:"
                    f"{native_planning.worker.runnable_before}/"
                    f"{native_planning.worker.runnable_after}"
                )
                if native_planning.worker.task_source_error:
                    actions.append("native_planning_task_source_error")
            else:
                actions.append(
                    f"native_planning_worker:{native_planning.worker.status}"
                )
            if runnable == 0 and active_conflict_workers:
                actions.append(f"waiting_for_active_workers:{active_conflict_workers}")
            elif runnable == 0:
                actions.append("no_runnable_work")
            else:
                actions.append(f"low_runnable_work:{runnable}/{min_ready}")
    elif (
        external_pid := collect_external_run_supervisor(
            run_store, process_exists=process_exists
        )
    ) is not None:
        cycle_status = "observing"
        child_pid = external_pid
        actions.append(f"observed_external_run_until_done:{external_pid}")
        run_maintenance("summary")
    else:
        child_log = config.state_path / "autopilot" / f"{cycle_id}.log"
        command = autopilot_child_command(
            config,
            jobs=jobs,
            ask_agent=ask_agent,
            continue_on_failure=continue_on_failure,
            max_slices=max_slices,
            max_tasks=max_tasks,
        )
        observed_pid: dict[str, int] = {}

        def _on_start(pid: int) -> None:
            observed_pid["pid"] = pid

        actions.append("launched_run_until_done")
        exit_code = launcher(
            command,
            cwd=config.repo,
            log_path=child_log,
            on_start=_on_start,
        )
        child_pid = observed_pid.get("pid")
        cycle_status = classify_child_exit(exit_code)
        actions.append(f"child_exit:{exit_code}")
        run_maintenance("summary")
        if cycle_status in {"restartable", "terminated"}:
            run_maintenance("troubleshoot")

    pause_seconds = limit_wall_pause_seconds(
        run_store,
        since=cycle_started_at,
        default_backoff=config.supervision.limit_wall_backoff_seconds,
    )
    if pause_seconds is not None:
        actions.append(f"limit_wall_pause:{pause_seconds:.0f}s")

    return AutopilotCycleResult(
        cycle_id=cycle_id,
        repo=config.repo,
        status=cycle_status,
        occurred_at=utc_now_iso(),
        project_status=status,
        actions=tuple(actions),
        blockers=blockers,
        child_pid=child_pid,
        child_log=child_log,
        next_wake=next_wake,
        limit_wall_pause_seconds=pause_seconds,
    )


_RECHECK_EPSILON = 1e-9
IDLE_WAIT_ERROR_LIMIT = 8
IDLE_WAIT_ERROR_TEXT_LIMIT = 256
IDLE_WAKE_REASONS = frozenset({"task_change", "operator_message"})
IDLE_WAKE_MAX_OUTPUT_BYTES = 64 * 1024
IDLE_WAKE_EVENT_FIELD_MAX_BYTES = 1024
IDLE_WAKE_EVENT_MAX_BYTES = 4096


def require_positive_min_ready(min_ready: int) -> int:
    if isinstance(min_ready, bool) or not isinstance(min_ready, int) or min_ready < 1:
        raise ValueError("min_ready must be a positive integer")
    return min_ready


def cycle_should_recheck(result: AutopilotCycleResult) -> bool:
    """Whether a finished cycle should poll for freshly planned tasks.

    An idle cycle is one that neither dispatched nor observed a child because
    runnable work was below ``min_ready``; it is also the only branch that runs
    the planning command. So an idle status captures exactly the cases where the
    board may gain runnable tasks out of band, and sleeping the full interval
    would strand them. Completed dispatch cycles use a separate fresh queue poll
    to detect a drained board; observing and blocked cycles keep the plain
    interval sleep.

    An idle cycle with no planning command configured still rechecks: that is
    deliberate, so out-of-band task additions (a peer or operator filling the
    board) are picked up without waiting the full interval.
    """
    return result.status == "idle"


def recheck_sleep_slices(interval: float, recheck_seconds: float) -> Iterator[float]:
    """Partition ``interval`` into poll slices of at most ``recheck_seconds``.

    Yields each slice duration in order; the final slice is shortened so the
    yielded durations sum to ``interval``. Yields nothing when ``interval`` is
    non-positive, so a drain-mode (no-interval) supervisor never polls.
    """
    if interval <= 0:
        return
    step = recheck_seconds if recheck_seconds > 0 else interval
    remaining = interval
    while remaining > _RECHECK_EPSILON:
        current = step if step < remaining else remaining
        yield current
        remaining -= current


def poll_runnable_count(config: VibeConfig) -> int:
    """Cheap runnable-task poll for post-planning and post-dispatch rechecks.

    Reuses the same task-source listing as cycle status collection. Any probe
    failure is reported as zero runnable so a transient error keeps the
    supervisor waiting rather than crashing it. ``collect_task_queue_status``
    already folds the parser trio (FileNotFoundError/RuntimeError/ValueError)
    into ``source_error``, but the production command-backed source shells out
    with ``check=True``: a nonzero ``loopyard`` exit raises
    ``subprocess.CalledProcessError`` and a spawn failure raises ``OSError``,
    neither of which that inner catch covers. This poll runs ~30 times per idle
    window under exactly the task-source-load conditions the recheck targets, so
    it must swallow those here too.
    """
    try:
        status = collect_task_queue_status(config)
    except (subprocess.SubprocessError, OSError):
        return 0
    if status.source_error:
        return 0
    return status.runnable


class IdleWakeAdapterError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclasses.dataclass(frozen=True)
class IdleWaitResult:
    cycle_id: str
    wake_reason: str
    deadline: str
    poll_count: int = 0
    runnable: int = 0
    adapter_calls: int = 0
    source_error_count: int = 0
    adapter_error_count: int = 0
    source_errors: tuple[str, ...] = ()
    adapter_errors: tuple[str, ...] = ()
    event: dict[str, object] | None = None

    def to_record(self, repo: Path) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "wake_reason": self.wake_reason,
            "deadline": self.deadline,
            "poll_count": self.poll_count,
            "runnable": self.runnable,
            "adapter_calls": self.adapter_calls,
            "source_error_count": self.source_error_count,
            "adapter_error_count": self.adapter_error_count,
            "source_errors": list(self.source_errors),
            "adapter_errors": list(self.adapter_errors),
        }
        if self.event is not None:
            payload["event"] = dict(self.event)
        return payload


IdleWakeAdapter = Callable[[float], dict[str, object] | None]
IdleRunnableProbe = Callable[[VibeConfig, float], TaskQueueStatus | int]


def poll_idle_wake_command(
    command: str,
    *,
    cycle_id: str,
    deadline: str,
    timeout: float,
    runtime_context: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, object] | None:
    """Run one trusted idle-wake adapter wait and validate its JSON envelope."""
    environment = os.environ.copy()
    environment["VIBE_LOOP_IDLE_CYCLE_ID"] = cycle_id
    environment["VIBE_LOOP_IDLE_DEADLINE"] = deadline
    environment["VIBE_LOOP_IDLE_WAIT_SECONDS"] = f"{timeout:.6f}"
    if runtime_context is not None:
        environment.update(runtime_context)
    stdout = _bounded_idle_wake_output(
        command,
        environment=environment,
        timeout=timeout,
        cwd=cwd,
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise IdleWakeAdapterError("invalid_json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("woke"), bool):
        raise IdleWakeAdapterError("invalid_schema")
    reason = payload.get("reason")
    event = payload.get("event")
    if not payload["woke"]:
        if reason is not None or event is not None:
            raise IdleWakeAdapterError("invalid_schema")
        return None
    if reason not in IDLE_WAKE_REASONS:
        raise IdleWakeAdapterError("invalid_schema")
    if event is not None and not isinstance(event, dict):
        raise IdleWakeAdapterError("invalid_schema")
    wake_event: dict[str, object] = {"kind": reason}
    if isinstance(event, dict):
        for key in ("id", "at", "sender", "session_ref"):
            value = event.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                if (
                    isinstance(value, str)
                    and len(value.encode("utf-8")) > IDLE_WAKE_EVENT_FIELD_MAX_BYTES
                ):
                    raise IdleWakeAdapterError("event_too_large")
                wake_event[key] = value
    if len(json.dumps(wake_event).encode("utf-8")) > IDLE_WAKE_EVENT_MAX_BYTES:
        raise IdleWakeAdapterError("event_too_large")
    return wake_event


def _bounded_idle_wake_output(
    command: str,
    *,
    environment: dict[str, str],
    timeout: float,
    cwd: Path | None,
) -> str:
    prepared, use_shell = prepare_shell_command(command)
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    deadline = time_module.monotonic() + max(timeout, 0.001)
    with tempfile.TemporaryFile() as buffer:
        try:
            process = subprocess.Popen(
                prepared,
                cwd=cwd,
                shell=use_shell,
                stdout=buffer,
                stderr=subprocess.DEVNULL,
                env=environment,
                **popen_kwargs,
            )
        except OSError as exc:
            raise IdleWakeAdapterError("execution_error") from exc
        try:
            while True:
                return_code = process.poll()
                buffer.seek(0, os.SEEK_END)
                output_size = buffer.tell()
                if output_size > IDLE_WAKE_MAX_OUTPUT_BYTES:
                    if return_code is None:
                        kill_command_process_group(process)
                        process.wait()
                    raise IdleWakeAdapterError("output_too_large")
                if return_code is not None:
                    break
                remaining = deadline - time_module.monotonic()
                if remaining <= 0:
                    kill_command_process_group(process)
                    process.wait()
                    raise IdleWakeAdapterError("timeout")
                time_module.sleep(min(0.01, remaining))
        except KeyboardInterrupt:
            terminate_command_process_group(process)
            raise
        if return_code != 0:
            raise IdleWakeAdapterError("nonzero_exit")
        buffer.seek(0)
        raw = buffer.read(IDLE_WAKE_MAX_OUTPUT_BYTES + 1)
    if len(raw) > IDLE_WAKE_MAX_OUTPUT_BYTES:
        raise IdleWakeAdapterError("output_too_large")
    return raw.decode("utf-8", errors="replace")


def _bounded_idle_error(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:IDLE_WAIT_ERROR_TEXT_LIMIT]


def _record_bounded_error(errors: list[str], value: object) -> None:
    if len(errors) < IDLE_WAIT_ERROR_LIMIT:
        errors.append(_bounded_idle_error(value))


def wait_for_idle_change(
    config: VibeConfig,
    *,
    cycle_id: str,
    deadline: str,
    interval: float,
    initial_poll_seconds: float,
    max_poll_seconds: float,
    sleeper: Sleep,
    should_stop: Callable[[], bool] | None = None,
    runnable_probe: IdleRunnableProbe | None = None,
    min_ready: int = 1,
    wake_adapter: IdleWakeAdapter | None = None,
    monotonic: Callable[[], float] | None = None,
    active_runs: tuple[ActiveRunState, ...] = (),
) -> IdleWaitResult:
    """Wait for idle work with a trusted wake adapter and adaptive fallback."""
    threshold = require_positive_min_ready(min_ready)
    if runnable_probe is None:

        def probe(probe_config: VibeConfig, timeout: float) -> TaskQueueStatus:
            return collect_task_queue_status(
                probe_config,
                timeout,
                active_runs=active_runs,
            )

    else:
        probe = runnable_probe
    clock = monotonic if monotonic is not None else time_module.monotonic
    remaining_budget = max(interval, 0.0)
    deadline_at = clock() + remaining_budget
    maximum = max(max_poll_seconds, 0.1)
    delay = min(max(initial_poll_seconds, 0.1), maximum)
    polls = 0
    adapter_calls = 0
    source_error_count = 0
    adapter_error_count = 0
    source_errors: list[str] = []
    adapter_errors: list[str] = []

    while remaining_budget > _RECHECK_EPSILON:
        remaining = min(remaining_budget, max(deadline_at - clock(), 0.0))
        if remaining <= _RECHECK_EPSILON:
            break
        wait_budget = min(delay, remaining)
        adapter_elapsed = 0.0
        if wake_adapter is not None:
            adapter_calls += 1
            adapter_started = clock()
            try:
                event = wake_adapter(wait_budget)
            except IdleWakeAdapterError as exc:
                adapter_error_count += 1
                _record_bounded_error(adapter_errors, exc.category)
                event = None
            adapter_elapsed = min(max(clock() - adapter_started, 0.0), wait_budget)
            if event is not None and clock() < deadline_at:
                return IdleWaitResult(
                    cycle_id=cycle_id,
                    wake_reason=str(event["kind"]),
                    deadline=deadline,
                    poll_count=polls,
                    adapter_calls=adapter_calls,
                    source_error_count=source_error_count,
                    adapter_error_count=adapter_error_count,
                    source_errors=tuple(source_errors),
                    adapter_errors=tuple(adapter_errors),
                    event=event,
                )
        sleep_for = wait_budget - adapter_elapsed
        if sleep_for > _RECHECK_EPSILON:
            sleeper(sleep_for)
        remaining_budget -= wait_budget
        if should_stop is not None and should_stop():
            return IdleWaitResult(
                cycle_id=cycle_id,
                wake_reason="stopped",
                deadline=deadline,
                poll_count=polls,
                adapter_calls=adapter_calls,
                source_error_count=source_error_count,
                adapter_error_count=adapter_error_count,
                source_errors=tuple(source_errors),
                adapter_errors=tuple(adapter_errors),
            )
        remaining = min(remaining_budget, max(deadline_at - clock(), 0.0))
        if remaining <= _RECHECK_EPSILON:
            break

        polls += 1
        status = probe(config, remaining)
        if isinstance(status, int):
            runnable = status
            source_error = ""
        else:
            runnable = status.runnable
            source_error = status.source_error
        if source_error:
            source_error_count += 1
            _record_bounded_error(source_errors, source_error)
        if clock() >= deadline_at:
            break
        if not source_error and runnable >= threshold:
            return IdleWaitResult(
                cycle_id=cycle_id,
                wake_reason="task_change",
                deadline=deadline,
                poll_count=polls,
                runnable=runnable,
                adapter_calls=adapter_calls,
                source_error_count=source_error_count,
                adapter_error_count=adapter_error_count,
                source_errors=tuple(source_errors),
                adapter_errors=tuple(adapter_errors),
            )
        delay = min(delay * 2.0, maximum)

    return IdleWaitResult(
        cycle_id=cycle_id,
        wake_reason="deadline",
        deadline=deadline,
        poll_count=polls,
        adapter_calls=adapter_calls,
        source_error_count=source_error_count,
        adapter_error_count=adapter_error_count,
        source_errors=tuple(source_errors),
        adapter_errors=tuple(adapter_errors),
    )


def recheck_interval_for_runnable(
    config: VibeConfig,
    *,
    interval: float,
    recheck_seconds: float,
    sleeper: Sleep,
    should_stop: Callable[[], bool] | None = None,
    runnable_probe: Callable[[VibeConfig], int] | None = None,
    min_ready: int = 1,
) -> bool:
    """Sleep up to ``interval`` while polling for enough runnable work to dispatch.

    Sleeps in ``recheck_seconds`` slices through the injected ``sleeper`` and
    probes the task source between slices. Returns ``True`` as soon as at least
    ``min_ready`` runnable tasks are present so the caller can start the next
    cycle early, and ``False`` when the full interval elapses without them (or a
    stop is requested). Used after idle/planning cycles so freshly planned work
    is picked up without waiting the whole interval.

    The ``min_ready`` threshold mirrors the dispatch gate
    (``runnable < min_ready`` is idle in :func:`execute_autopilot_cycle`). Waking
    on any ``runnable > 0`` count the next cycle still could not dispatch only
    starts another idle cycle, and a probe that keeps reporting a below-threshold
    count then spins the supervisor, re-running the planning command every slice
    instead of backing off for the full interval. Requiring the dispatch
    threshold keeps a below-threshold (or phantom) count from starving the
    interval backoff.
    """
    probe = runnable_probe if runnable_probe is not None else poll_runnable_count
    threshold = require_positive_min_ready(min_ready)
    for slice_seconds in recheck_sleep_slices(interval, recheck_seconds):
        sleeper(slice_seconds)
        if should_stop is not None and should_stop():
            return False
        if probe(config) >= threshold:
            return True
    return False


class AutopilotTerminationRequested(KeyboardInterrupt):
    def __init__(self, signal_number: int):
        self.signal_number = signal_number
        super().__init__(signal.Signals(signal_number).name)


@contextmanager
def autopilot_termination_signals() -> Iterator[Callable[[], None]]:
    def enable_immediately() -> None:
        return

    if threading.current_thread() is not threading.main_thread():
        yield enable_immediately
        return
    handled_signals = tuple(
        stop_signal
        for stop_signal in (getattr(signal, "SIGINT", None), signal.SIGTERM)
        if stop_signal is not None
    )
    previous_handlers = {
        stop_signal: signal.getsignal(stop_signal) for stop_signal in handled_signals
    }
    previous_mask: set[signal.Signals] | None = None
    signals_enabled = False
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if callable(pthread_sigmask):
        previous_mask = pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    stop_requested = False
    pending_signal: int | None = None

    def request_stop(signal_number: int, _frame: object) -> None:
        nonlocal pending_signal, stop_requested
        if stop_requested:
            return
        stop_requested = True
        if not signals_enabled:
            pending_signal = signal_number
            return
        raise AutopilotTerminationRequested(signal_number)

    installed_signals: list[signal.Signals] = []
    try:
        for stop_signal in handled_signals:
            signal.signal(stop_signal, request_stop)
            installed_signals.append(stop_signal)

        def enable_signals() -> None:
            nonlocal signals_enabled
            if signals_enabled:
                return
            signals_enabled = True
            if previous_mask is not None:
                assert pthread_sigmask is not None
                pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            if pending_signal is not None:
                raise AutopilotTerminationRequested(pending_signal)

        yield enable_signals
    finally:
        for stop_signal in installed_signals:
            signal.signal(stop_signal, previous_handlers[stop_signal])
        if previous_mask is not None and not signals_enabled:
            assert pthread_sigmask is not None
            pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_autopilot(
    config: VibeConfig,
    *,
    jobs: int = 1,
    interval: float = 0.0,
    once: bool = False,
    max_cycles: int = 0,
    ask_agent: bool = False,
    continue_on_failure: bool = False,
    max_slices: int = 0,
    max_tasks: int = 0,
    min_ready: int = 1,
    process_exists: ProcessExists | None = None,
    sleep: Sleep | None = None,
    launcher: RunUntilDoneLauncher | None = None,
    maintenance_runner: MaintenanceRunner = run_maintenance_command,
    worktree_disposition_runner: WorktreeDispositionRunner = run_worktree_disposition,
    native_planning_runner: NativePlanningRunner = run_native_planning,
    idle_waiter: Callable[..., IdleWaitResult] = wait_for_idle_change,
    idle_wake_command_runner: Callable[..., dict[str, object] | None] = (
        poll_idle_wake_command
    ),
    should_stop: Callable[[], bool] | None = None,
    install_signal_handlers: bool = True,
) -> AutopilotRunSummary:
    """Supervise ``run-until-done`` as a foreground persistent loop.

    A single autopilot supervisor lock prevents duplicate supervisors. A live
    supervisor is observed rather than duplicated, and a stale supervisor lock
    is reported without being stolen. Each cycle is append-recorded; launch is
    blocked, never force-recovered, when preflight diagnostics are unsafe.
    """
    min_ready = require_positive_min_ready(min_ready)

    process_checker = process_exists if process_exists is not None else pid_exists
    sleeper = sleep if sleep is not None else time_module.sleep
    if launcher is None:

        def launch(
            command: list[str],
            *,
            cwd: Path,
            log_path: Path,
            on_start: Callable[[int], None] | None = None,
        ) -> int:
            return launch_run_until_done(
                command,
                cwd=cwd,
                log_path=log_path,
                on_start=on_start,
                runtime_context=config.runtime_context,
            )

    else:
        launch = launcher
    run_store = RunStore(config.state_path / "runs.jsonl")
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    supervisor_run_id = new_run_id("autopilot")

    signal_stack = ExitStack()

    def enable_termination_signals() -> None:
        return

    if install_signal_handlers:
        enable_termination_signals = signal_stack.enter_context(
            autopilot_termination_signals()
        )
    try:
        existing = lock_manager.autopilot_status(process_exists=process_checker)
    except BaseException:
        signal_stack.close()
        raise
    if existing.locked and existing.state in {"held", "unknown"}:
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": str(existing.metadata.get("run_id") or ""),
                "pid": existing.metadata.get("pid"),
                "observed_state": existing.state,
                "worktree_disposition_policy": (config.autopilot.worktree_disposition),
            }
        )
        summary = AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker="autopilot_supervisor_active",
        )
        signal_stack.close()
        return summary
    if existing.locked and existing.state == "stale":
        summary = AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker=f"autopilot_supervisor_lock_stale:{existing.stale_reason or 'unknown'}",
        )
        signal_stack.close()
        return summary

    try:
        lock = lock_manager.acquire_autopilot(run_id=supervisor_run_id)
    except LockBusy:
        summary = AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker="autopilot_supervisor_active",
        )
        signal_stack.close()
        return summary
    except BaseException:
        signal_stack.close()
        raise

    fencing_token = str(lock.metadata.get("fencing_token") or "")
    supervisor_log = config.state_path / "autopilot" / f"{supervisor_run_id}.log"
    heartbeat = AutopilotLockHeartbeat(
        lock_manager,
        run_id=supervisor_run_id,
        fencing_token=fencing_token,
        lease_seconds=int_value(lock.metadata.get("lease_seconds")),
    )
    cycles: list[AutopilotCycleResult] = []
    termination_signal: int | None = None
    try:
        enable_termination_signals()
        heartbeat.start()
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": supervisor_run_id,
                "pid": os.getpid(),
                "log": str(supervisor_log),
                "worktree_disposition_policy": (config.autopilot.worktree_disposition),
            }
        )
        cycle_number = 0
        while True:
            if should_stop is not None and should_stop():
                break
            cycle_number += 1
            bounded_last = once or (max_cycles > 0 and cycle_number >= max_cycles)
            next_wake = "" if bounded_last else iso_after(interval)
            result = execute_autopilot_cycle(
                config,
                cycle_id=f"{supervisor_run_id}-c{cycle_number}",
                jobs=jobs,
                ask_agent=ask_agent,
                continue_on_failure=continue_on_failure,
                max_slices=max_slices,
                max_tasks=max_tasks,
                min_ready=min_ready,
                next_wake=next_wake,
                process_exists=process_exists,
                launcher=launch,
                run_store=run_store,
                maintenance_runner=maintenance_runner,
                worktree_disposition_runner=worktree_disposition_runner,
                native_planning_runner=native_planning_runner,
            )
            if (
                not bounded_last
                and interval > 0
                and cycle_should_recheck(result)
                and result.limit_wall_pause_seconds is None
            ):
                result = dataclasses.replace(result, next_wake=iso_after(interval))
            post_cycle_planning_delay: float | None = None
            if (
                not bounded_last
                and interval > 0
                and "launched_run_until_done" in result.actions
                and result.limit_wall_pause_seconds is None
            ):
                post_cycle_runnable = poll_runnable_count(config)
                threshold = min_ready
                post_cycle_action = (
                    f"post_cycle_runnable:{post_cycle_runnable}/{threshold}"
                )
                if post_cycle_runnable < threshold:
                    post_cycle_planning_delay = min(
                        interval,
                        config.autopilot.planning_recheck_seconds,
                    )
                    result = dataclasses.replace(
                        result,
                        actions=(*result.actions, post_cycle_action),
                        next_wake=iso_after(post_cycle_planning_delay),
                    )
                else:
                    result = dataclasses.replace(
                        result,
                        actions=(*result.actions, post_cycle_action),
                    )
            result.append_to(run_store)
            cycles.append(result)
            if bounded_last:
                break
            pause_seconds = result.limit_wall_pause_seconds
            if pause_seconds is not None:
                # A child stopped on a provider limit wall. Pause dispatch until
                # the advertised reset (or the configured backoff) instead of
                # re-dispatching straight into the same wall, in both persistent
                # and drain modes.
                print(
                    f"[vibe-loop] autopilot limit wall: pausing dispatch "
                    f"{pause_seconds:.0f}s before the next cycle",
                    flush=True,
                )
                sleeper(pause_seconds)
                continue
            if interval > 0:
                # Persistent watch: keep cycling and sleeping until a bound or
                # signal stops the loop, even across idle or blocked cycles.
                if cycle_should_recheck(result):
                    wake_adapter_callback: IdleWakeAdapter | None = None
                    idle_wake_command = config.autopilot.idle_wake_command
                    if idle_wake_command is not None:

                        def _wake_adapter(
                            timeout: float,
                        ) -> dict[str, object] | None:
                            return idle_wake_command_runner(
                                idle_wake_command,
                                cycle_id=result.cycle_id,
                                deadline=result.next_wake,
                                timeout=timeout,
                                runtime_context=config.runtime_environment,
                                cwd=config.repo,
                            )

                        wake_adapter_callback = _wake_adapter

                    wait_result = idle_waiter(
                        config,
                        cycle_id=result.cycle_id,
                        deadline=result.next_wake,
                        interval=interval,
                        initial_poll_seconds=(
                            config.autopilot.planning_recheck_seconds
                        ),
                        max_poll_seconds=config.autopilot.idle_poll_max_seconds,
                        sleeper=sleeper,
                        should_stop=should_stop,
                        min_ready=min_ready,
                        wake_adapter=wake_adapter_callback,
                        active_runs=tuple(
                            worker.active
                            for worker in result.project_status.workers
                            if worker_holds_active_conflict(worker)
                        ),
                    )
                    run_store.append_record(wait_result.to_record(config.repo))
                    if wait_result.wake_reason == "task_change":
                        print(
                            "[vibe-loop] autopilot idle wake: task source changed, "
                            "starting next cycle early",
                            flush=True,
                        )
                    elif wait_result.wake_reason == "operator_message":
                        print(
                            "[vibe-loop] autopilot idle wake: operator message, "
                            "starting next cycle early",
                            flush=True,
                        )
                elif post_cycle_planning_delay is not None:
                    print(
                        "[vibe-loop] autopilot post-dispatch recheck: queue "
                        "below min-ready, starting the next cycle after "
                        f"{post_cycle_planning_delay:.0f}s",
                        flush=True,
                    )
                    sleeper(post_cycle_planning_delay)
                else:
                    sleeper(interval)
                continue
            # Drain mode (no interval): continue only while cycles can still make
            # progress; an idle or blocked cycle cannot advance without waiting or
            # operator intervention, so the supervisor stops instead of spinning.
            if result.status not in {"completed", "restartable"}:
                break
    except AutopilotTerminationRequested as exc:
        termination_signal = exc.signal_number
    finally:
        try:
            heartbeat.stop()
            lock_manager.release_autopilot(
                run_id=supervisor_run_id,
                fencing_token=fencing_token,
                command_timeout_seconds=30.0,
            )
            append_autopilot_stopped_record(
                run_store,
                repo=config.repo,
                run_id=supervisor_run_id,
                pid=os.getpid(),
                stop_mode=(
                    "signal" if termination_signal is not None else "foreground_exit"
                ),
                signal_number=termination_signal,
                process_exited=False,
            )
        finally:
            signal_stack.close()

    return AutopilotRunSummary(
        repo=config.repo,
        run_id=supervisor_run_id,
        started=True,
        cycles=tuple(cycles),
        log=supervisor_log,
    )


def iso_after(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(0.0, seconds))).isoformat()


PROJECT_REGISTRY_SCHEMA_VERSION = 1
RUNTIME_CONTEXT_REDACTION = "<runtime-context-redacted>"


def default_registry_path() -> Path:
    return Path.home() / ".vibe-loop" / "projects.json"


def redact_runtime_context_text(
    value: str,
    runtime_context: tuple[tuple[str, str], ...],
) -> str:
    redacted = value
    context_values = sorted(
        (context_value for _name, context_value in runtime_context if context_value),
        key=len,
        reverse=True,
    )
    for context_value in context_values:
        redacted = redacted.replace(context_value, RUNTIME_CONTEXT_REDACTION)
    return redacted


def redact_runtime_context_payload(
    value: object,
    runtime_context: tuple[tuple[str, str], ...],
) -> object:
    if isinstance(value, str):
        return redact_runtime_context_text(value, runtime_context)
    if isinstance(value, dict):
        return {
            key: redact_runtime_context_payload(item, runtime_context)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_runtime_context_payload(item, runtime_context) for item in value]
    return value


def _entry_matches(entry: ProjectEntry, key: str) -> bool:
    if entry.name == key or str(entry.repo) == key:
        return True
    # Match a path-like key against the stored resolved repo path so a relative
    # or symlinked path resolves to the same entry that register recorded.
    try:
        return str(Path(key).resolve()) == str(entry.repo)
    except OSError:
        return False


@dataclasses.dataclass(frozen=True)
class ProjectEntry:
    name: str
    repo: Path
    runtime_context: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runtime_context",
            normalize_registry_runtime_context_assignments(self.runtime_context),
        )

    def to_json(self) -> dict[str, object]:
        payload = redact_runtime_context_payload(
            {"name": self.name, "repo": str(self.repo)},
            self.runtime_context,
        )
        assert isinstance(payload, dict)
        return payload

    def to_registry_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"name": self.name, "repo": str(self.repo)}
        if self.runtime_context:
            payload["context"] = dict(self.runtime_context)
        return payload


@dataclasses.dataclass(frozen=True)
class ProjectRegistry:
    """An optional global list of repositories for multi-project autopilot.

    Each entry records a repo path, display name, and optional validated runtime
    selectors for command task-source and lock adapters. Each project keeps its
    runtime state under its own configured state directory, and single-repo
    operation never requires the registry to exist.
    """

    path: Path
    entries: tuple[ProjectEntry, ...] = ()

    @classmethod
    def load(cls, path: Path) -> ProjectRegistry:
        if not path.exists():
            return cls(path=path, entries=())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"invalid project registry at {path}: {exc}") from exc
        raw_projects = data.get("projects", []) if isinstance(data, dict) else []
        entries: list[ProjectEntry] = []
        for raw in raw_projects:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
            repo = str(raw.get("repo") or "")
            if name and repo:
                try:
                    if "context" in raw and raw["context"] is None:
                        raise ValueError("registry entry context must be an object")
                    entries.append(
                        ProjectEntry(
                            name=name,
                            repo=Path(repo),
                            runtime_context=normalize_registry_runtime_context(
                                raw.get("context")
                            ),
                        )
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"invalid project registry entry {name!r}: {exc}"
                    ) from exc
        return cls(path=path, entries=tuple(entries))

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": PROJECT_REGISTRY_SCHEMA_VERSION,
            "projects": [entry.to_registry_json() for entry in self.entries],
        }

    def find(self, key: str) -> ProjectEntry | None:
        for entry in self.entries:
            if _entry_matches(entry, key):
                return entry
        return None

    def with_entry(self, entry: ProjectEntry) -> ProjectRegistry:
        kept = tuple(item for item in self.entries if item.name != entry.name)
        return ProjectRegistry(path=self.path, entries=(*kept, entry))

    def without(self, key: str) -> tuple[ProjectRegistry, bool]:
        kept = tuple(item for item in self.entries if not _entry_matches(item, key))
        return ProjectRegistry(path=self.path, entries=kept), len(kept) != len(
            self.entries
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8"
        )


@dataclasses.dataclass(frozen=True)
class AggregateProjectStatus:
    name: str
    repo: Path
    status: ProjectStatus | None = None
    error: str = ""
    runtime_context: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict[str, object]:
        payload = {
            "name": self.name,
            "repo": str(self.repo),
            "status": self.status.to_json() if self.status is not None else None,
            "error": self.error,
        }
        redacted = redact_runtime_context_payload(payload, self.runtime_context)
        assert isinstance(redacted, dict)
        return redacted


def collect_registry_status(
    registry: ProjectRegistry,
    *,
    process_exists: ProcessExists | None = None,
) -> list[AggregateProjectStatus]:
    results: list[AggregateProjectStatus] = []
    for entry in registry.entries:
        try:
            config = load_config(
                entry.repo,
                runtime_context=dict(entry.runtime_context),
            )
            status = collect_project_status(config, process_exists=process_exists)
            results.append(
                AggregateProjectStatus(
                    name=entry.name,
                    repo=entry.repo,
                    status=status,
                    runtime_context=entry.runtime_context,
                )
            )
        # Per-repo collection can fail many ways (missing/unreadable repo,
        # malformed config, git or task-source errors); isolate the failure so
        # one bad project never breaks the rest of the aggregate.
        except Exception as exc:
            results.append(
                AggregateProjectStatus(
                    name=entry.name,
                    repo=entry.repo,
                    error=redact_runtime_context_text(str(exc), entry.runtime_context),
                    runtime_context=entry.runtime_context,
                )
            )
    return results


DEFAULT_WAIT_CYCLE_SECONDS = 1800.0
DEFAULT_WAIT_POLL_SECONDS = 5.0
WallClock = Callable[[], float]
WaitMessagePoller = Callable[[], dict[str, object] | None]


class WaitMessageAdapterError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclasses.dataclass(frozen=True)
class WaitResult:
    wake_reason: str
    events: tuple[dict[str, object], ...] = ()
    deadline: str = ""
    session_ref: str = ""

    @property
    def wake_summary(self) -> str:
        parts: list[str] = []
        for event in self.events:
            kind = event.get("kind")
            if kind == "pid_exit":
                parts.append(f"pid_exit:{event.get('pid')}")
            elif kind == "user_message":
                continue
            else:
                parts.append(str(kind or "event"))
        message_count = sum(
            event.get("kind") == "user_message" for event in self.events
        )
        if message_count:
            return f"message:{message_count}"
        if parts:
            return ",".join(parts)
        if self.wake_reason == "deadline":
            return f"deadline:{self.deadline}"
        if self.wake_reason == "adapter_error":
            return "message_adapter_error"
        return self.wake_reason

    def to_json(self, *, at: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "wake_reason": self.wake_reason,
            "wake_summary": self.wake_summary,
            "at": at,
            "deadline": self.deadline,
            "events": [dict(event) for event in self.events],
        }
        if self.session_ref:
            payload["session_ref"] = self.session_ref
        return payload


def poll_wait_message_command(
    command: str,
    *,
    session_ref: str,
    timeout: float,
) -> dict[str, object] | None:
    """Run one trusted message adapter poll and validate its JSON envelope."""
    environment = os.environ.copy()
    environment["VIBE_LOOP_WAIT_SESSION_REF"] = session_ref
    prepared, use_shell = prepare_shell_command(command)
    try:
        completed = subprocess.run(
            prepared,
            shell=use_shell,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WaitMessageAdapterError("timeout") from exc
    except OSError as exc:
        raise WaitMessageAdapterError("execution_error") from exc
    if completed.returncode != 0:
        raise WaitMessageAdapterError("nonzero_exit")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WaitMessageAdapterError("invalid_json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("received"), bool):
        raise WaitMessageAdapterError("invalid_schema")
    message = payload.get("message")
    if not payload["received"]:
        if message is not None:
            raise WaitMessageAdapterError("invalid_schema")
        return None
    if not isinstance(message, dict):
        raise WaitMessageAdapterError("invalid_schema")
    message_id = message.get("id")
    content = message.get("content")
    if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
        raise WaitMessageAdapterError("invalid_schema")
    if not isinstance(content, str) or not content.strip():
        raise WaitMessageAdapterError("invalid_schema")
    event: dict[str, object] = {
        "kind": "user_message",
        "id": message_id,
        "text": content,
    }
    for source, target in (
        ("created_at", "at"),
        ("sender_name", "sender"),
        ("sender_actor_id", "sender_actor_id"),
        ("session_ref", "session_ref"),
    ):
        value = message.get(source)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            event[target] = value
    return event


def format_utc_timestamp(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def cycle_schedule_deadline(
    interval_seconds: float,
    *,
    now: float,
) -> tuple[str, float]:
    """Return the next UTC wall-clock ``*/interval`` boundary as (iso, epoch).

    Aligns to cron-style buckets rather than ``now + interval`` so cycles stay
    on a stable schedule across restarts.
    """

    if interval_seconds <= 0:
        raise ValueError("cycle schedule interval must be positive")
    deadline_epoch = (int(now // interval_seconds) + 1) * interval_seconds
    return format_utc_timestamp(deadline_epoch), deadline_epoch


def parse_wait_deadline(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def wait_for_processes(
    *,
    pids: list[int],
    deadline_epoch: float | None,
    deadline_text: str = "",
    mode: str = "any",
    interval: float = DEFAULT_WAIT_POLL_SECONDS,
    process_exists: ProcessExists | None = None,
    wallclock: WallClock | None = None,
    sleep: Sleep | None = None,
    message_poller: WaitMessagePoller | None = None,
    session_ref: str = "",
) -> WaitResult:
    """Block until a watched PID exits, deadline, or external message."""

    watched_pids = list(dict.fromkeys(pids))
    if not watched_pids and deadline_epoch is None:
        raise ValueError("wait requires at least one pid or a deadline")
    checker = process_exists if process_exists is not None else pid_exists
    now = wallclock if wallclock is not None else time_module.time
    sleeper = sleep if sleep is not None else time_module.sleep
    completed_pids: set[int] = set()
    all_events: list[dict[str, object]] = []

    while True:
        events: list[dict[str, object]] = []
        for pid in watched_pids:
            if pid not in completed_pids and not checker(pid):
                completed_pids.add(pid)
                events.append({"kind": "pid_exit", "pid": pid})
        all_events.extend(events)

        if mode == "any" and events:
            return WaitResult(wake_reason="pid", events=tuple(events))
        if mode == "all" and watched_pids and len(completed_pids) >= len(watched_pids):
            return WaitResult(wake_reason="all_complete", events=tuple(all_events))

        current = now()
        if deadline_epoch is not None and current >= deadline_epoch:
            return WaitResult(wake_reason="deadline", deadline=deadline_text)
        if message_poller is not None:
            message_event = message_poller()
            if message_event is not None:
                return WaitResult(
                    wake_reason="message",
                    events=tuple([*all_events, message_event]),
                    deadline=deadline_text,
                    session_ref=session_ref,
                )
        sleep_for = max(interval, 0.1)
        if deadline_epoch is not None:
            sleep_for = min(sleep_for, max(deadline_epoch - current, 0.1))
        sleeper(sleep_for)
