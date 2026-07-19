from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import time as time_module
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vibe_loop.config import (
    AgentResolutionError,
    VibeConfig,
    load_config,
    normalize_registry_runtime_context,
    normalize_registry_runtime_context_assignments,
    prepare_shell_command,
    unresolved_agent_command_message,
    unresolved_prompt_dialect_message,
)
from vibe_loop.locks import IntegrationLockStatus, LockBusy, build_lock_manager
from vibe_loop.retry import parse_limit_wall_reset_delay
from vibe_loop.runner import VibeRunner, new_run_id
from vibe_loop.runs import (
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    RUN_SUPERVISOR_EXITED_RECORD_TYPE,
    RUN_SUPERVISOR_STARTED_RECORD_TYPE,
    RunStore,
    utc_now_iso,
)
from vibe_loop.tasks import BLOCKED_FAMILY_STATUSES, Task
from vibe_loop.workers import (
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

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "pid": self.pid,
            "log": str(self.log) if self.log is not None else "",
            "run_id": self.run_id,
            "cycle_id": self.cycle_id,
            "observed_at": self.observed_at,
            "record": self.record or {},
        }


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
        return redacted


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


def collect_task_queue_status(config: VibeConfig) -> TaskQueueStatus:
    runner = VibeRunner(config)
    try:
        tasks = runner.source.list_tasks()
        runnable = runner.list_candidates()
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
    elif supervisor_records:
        newest_record = supervisor_records[-1]
        pid = int_value(newest_record.get("pid"))
        if pid and process_checker(pid):
            return supervisor_status_from_record(newest_record, state="running")

    if cycle_record is not None:
        return supervisor_status_from_record(
            cycle_record,
            state=str(cycle_record.get("status") or "idle"),
        )
    if supervisor_records:
        return supervisor_status_from_record(supervisor_records[-1], state="observed")
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
) -> SupervisorStatus:
    return SupervisorStatus(
        state=state,
        pid=int_value(record.get("child_pid")) or int_value(record.get("pid")),
        log=path_value(record.get("child_log") or record.get("log")),
        run_id=str(record.get("run_id") or ""),
        cycle_id=str(record.get("cycle_id") or ""),
        observed_at=str(record.get("occurred_at") or ""),
        record=record,
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
) -> list[str]:
    blockers: list[str] = []
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


def launch_run_until_done(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    on_start: Callable[[int], None] | None = None,
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
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
        if on_start is not None:
            on_start(process.pid)
        try:
            return process.wait()
        except KeyboardInterrupt:
            # On interrupt, terminate the worker we spawned rather than orphan
            # it, then let the supervisor unwind and release its lock.
            process.terminate()
            process.wait()
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


def kill_command_process_group(process: subprocess.Popen[bytes]) -> None:
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
    command_timeout: float = AUTOPILOT_COMMAND_TIMEOUT_SECONDS,
    command_max_output_bytes: int = AUTOPILOT_COMMAND_MAX_OUTPUT_BYTES,
) -> AutopilotCycleResult:
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
            if runnable == 0 and active_conflict_workers:
                actions.append(f"waiting_for_active_workers:{active_conflict_workers}")
            elif runnable == 0:
                actions.append("planning_unconfigured")
                actions.append("no_runnable_work")
            else:
                actions.append("planning_unconfigured")
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
    threshold = max(1, min_ready)
    for slice_seconds in recheck_sleep_slices(interval, recheck_seconds):
        sleeper(slice_seconds)
        if should_stop is not None and should_stop():
            return False
        if probe(config) >= threshold:
            return True
    return False


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
    should_stop: Callable[[], bool] | None = None,
) -> AutopilotRunSummary:
    """Supervise ``run-until-done`` as a foreground persistent loop.

    A single autopilot supervisor lock prevents duplicate supervisors. A live
    supervisor is observed rather than duplicated, and a stale supervisor lock
    is reported without being stolen. Each cycle is append-recorded; launch is
    blocked, never force-recovered, when preflight diagnostics are unsafe.
    """

    process_checker = process_exists if process_exists is not None else pid_exists
    sleeper = sleep if sleep is not None else time_module.sleep
    launch = launcher if launcher is not None else launch_run_until_done
    run_store = RunStore(config.state_path / "runs.jsonl")
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    supervisor_run_id = new_run_id("autopilot")

    existing = lock_manager.autopilot_status(process_exists=process_checker)
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
        return AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker="autopilot_supervisor_active",
        )
    if existing.locked and existing.state == "stale":
        return AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker=f"autopilot_supervisor_lock_stale:{existing.stale_reason or 'unknown'}",
        )

    try:
        lock = lock_manager.acquire_autopilot(run_id=supervisor_run_id)
    except LockBusy:
        return AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker="autopilot_supervisor_active",
        )

    fencing_token = str(lock.metadata.get("fencing_token") or "")
    supervisor_log = config.state_path / "autopilot" / f"{supervisor_run_id}.log"
    run_store.append_record(
        {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(config.repo),
            "run_id": supervisor_run_id,
            "pid": os.getpid(),
            "log": str(supervisor_log),
            "worktree_disposition_policy": config.autopilot.worktree_disposition,
        }
    )

    cycles: list[AutopilotCycleResult] = []
    try:
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
            )
            post_cycle_planning_delay: float | None = None
            if (
                not bounded_last
                and interval > 0
                and "launched_run_until_done" in result.actions
                and result.limit_wall_pause_seconds is None
            ):
                post_cycle_runnable = poll_runnable_count(config)
                threshold = max(1, min_ready)
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
                    # An idle/planning cycle may gain runnable tasks out of band
                    # (a detached planning agent filling the queue). Poll the
                    # task source in recheck slices instead of sleeping the whole
                    # interval, and start the next cycle as soon as work appears.
                    woke_early = recheck_interval_for_runnable(
                        config,
                        interval=interval,
                        recheck_seconds=config.autopilot.planning_recheck_seconds,
                        sleeper=sleeper,
                        should_stop=should_stop,
                        min_ready=min_ready,
                    )
                    if woke_early:
                        print(
                            "[vibe-loop] autopilot recheck: runnable tasks "
                            "appeared, starting next cycle early",
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
    finally:
        lock_manager.release_autopilot(
            run_id=supervisor_run_id,
            fencing_token=fencing_token,
        )

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
