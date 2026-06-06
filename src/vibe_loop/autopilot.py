from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time as time_module
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vibe_loop.config import (
    VibeConfig,
    unresolved_agent_command_message,
    unresolved_prompt_dialect_message,
)
from vibe_loop.locks import LockBusy, build_lock_manager
from vibe_loop.runner import VibeRunner, new_run_id
from vibe_loop.runs import (
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    RunStore,
    utc_now_iso,
)
from vibe_loop.tasks import Task
from vibe_loop.workers import (
    ProcessExists,
    StaleLock,
    WorkerView,
    collect_stale_locks,
    pid_exists,
)

RunUntilDoneLauncher = Callable[..., int]
Sleep = Callable[[float], None]


AUTOPILOT_RECORD_SCHEMA_VERSION = 1


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
    cycle_id: str = ""
    observed_at: str = ""
    record: dict[str, Any] | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "pid": self.pid,
            "log": str(self.log) if self.log is not None else "",
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
    workspace_diagnostics: tuple[dict[str, object], ...] = ()
    supervisor: SupervisorStatus = dataclasses.field(default_factory=SupervisorStatus)
    blockers: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    last_cycle: CycleSummary | None = None
    next_wake: str = ""

    def to_json(self) -> dict[str, object]:
        return {
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
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "child_pid": self.child_pid,
            "child_log": str(self.child_log) if self.child_log is not None else "",
            "next_wake": self.next_wake,
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
    supervisor = collect_supervisor_status(run_store, process_exists=process_exists)
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
    observations = tuple(project_observations(queue_status=queue_status))
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
        workspace_diagnostics=workspace_diagnostics,
        supervisor=supervisor,
        blockers=blockers,
        observations=observations,
        last_cycle=last_cycle,
        next_wake=last_cycle.next_wake if last_cycle is not None else "",
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
    statuses: dict[str, int] = {}
    for task in tasks:
        statuses[task.status] = statuses.get(task.status, 0) + 1
    return TaskQueueStatus(
        total=len(tasks),
        runnable=len(runnable),
        active=statuses.get("Active", 0),
        done=sum(1 for task in tasks if task.done),
        blocked=sum(statuses.get(status, 0) for status in ("Gated", "Low")),
        statuses=statuses,
        runnable_tasks=tuple(task_summary(task) for task in runnable),
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
    process_exists: ProcessExists | None = None,
) -> SupervisorStatus:
    process_checker = process_exists if process_exists is not None else pid_exists
    for record in reversed(run_store.read_records()):
        if record.get("record_type") not in {
            AUTOPILOT_CYCLE_RECORD_TYPE,
            AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
            AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
        }:
            continue
        record_type = str(record.get("record_type"))
        pid = int_value(record.get("child_pid")) or int_value(record.get("pid"))
        alive = bool(pid and process_checker(pid))
        state = "running" if alive else "observed"
        if record_type == AUTOPILOT_CYCLE_RECORD_TYPE and not alive:
            state = str(record.get("status") or "idle")
        return SupervisorStatus(
            state=state,
            pid=pid,
            log=path_value(record.get("child_log") or record.get("log")),
            cycle_id=str(record.get("cycle_id") or ""),
            observed_at=str(record.get("occurred_at") or ""),
            record=record,
        )
    return SupervisorStatus()


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


def project_observations(*, queue_status: TaskQueueStatus) -> list[str]:
    observations: list[str] = []
    if not queue_status.source_error and queue_status.runnable == 0:
        observations.append("no_runnable_work")
    return observations


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
) -> AutopilotCycleResult:
    status = collect_project_status(config, process_exists=process_exists)
    blockers = tuple(status.blockers)
    actions: list[str] = []
    child_pid: int | None = None
    child_log: Path | None = None

    if blockers:
        cycle_status = "blocked"
        actions.append("blocked_preflight")
    elif status.queue.runnable < min_ready:
        cycle_status = "idle"
        actions.append("no_runnable_work")
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
    )


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
            )
            result.append_to(run_store)
            cycles.append(result)
            if bounded_last:
                break
            if interval > 0:
                # Persistent watch: keep cycling and sleeping until a bound or
                # signal stops the loop, even across idle or blocked cycles.
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
