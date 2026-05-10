from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentDetection,
    VibeConfig,
    shell_quote,
    prepare_shell_command,
)
from vibe_loop.generated_profiles import (
    RuntimeTaskSourceResolution,
    resolve_runtime_task_source,
)
from vibe_loop.locks import LockBusy, LockManager, TaskLock
from vibe_loop.runs import RunResult, RunStore, WorkerReport
from vibe_loop.tasks import Task, TaskSource, build_task_source, runnable_tasks
from vibe_loop.workers import ActiveRunState, WorkerView, build_worker_views

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


SESSION_ID_RE = re.compile(
    r"\bsession(?:[_ -]?id)\s*[:=]\s*"
    r"(?P<session_id>[A-Za-z0-9](?:[A-Za-z0-9_.:/+-]*[A-Za-z0-9])?)\b",
    re.IGNORECASE,
)
RESOURCE_SCHEDULER_LOCK_NAME = "resource-scheduler"

CLI_WORKER_ADDENDUM = """\

## vibe-loop CLI Coordination

You are running as a worker launched by the vibe-loop CLI. The following
environment variables identify this run:
- VIBE_LOOP_REPO - path to the repository
- VIBE_LOOP_RUN_ID - unique run identifier
- VIBE_LOOP_TASK_ID - task being worked on
- VIBE_LOOP_LOG - path to the run log file

### Worker Reports

Report your final status before exiting:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" \\
  --task-id "$VIBE_LOOP_TASK_ID" --status completed --commit HEAD \\
  --message "completed $VIBE_LOOP_TASK_ID"
```

Use "completed" only after the reviewed slice has been integrated when
integration is permitted, verified on main, and cleaned up. Use "blocked" for
missing access, required approval, an unavailable integration lock, or a
decision that cannot be made safely. Use "failed" when an attempted slice
cannot be left working despite reasonable debugging. Use "unknown" only when
you cannot classify the result. Include the best available commit reference
and a concise message; include --metadata-json only for structured facts that
help the supervisor or later review.

When a blocker or failure occurs after code was changed, commit or otherwise
stabilize the slice before reporting unless doing so would be unsafe. Do not
let the report replace the final user-facing summary; the report is supervisor
state, while the summary explains what happened.

### Integration Locking

Before the final fast-forward merge to main, acquire the advisory
main-integration lock:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \\
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

If the lock is held by another live worker, wait and retry or park the slice
as blocked; do not enter the final integration section without the lock. If
the lock appears stale, report the precise status and follow repo policy
rather than stealing it.

Release the lock after main verification or immediately when integration is
parked:

```bash
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \\
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

If release reports an owner mismatch, do not remove another worker's lock;
report the mismatch in the final summary and in the worker report.

### Task Source Context

Treat the task details as normalized work from the repository's active task
source. That source may be explicit configuration, a generated profile cache,
command-backed adapters, issue trackers, or Markdown planning docs. If task
details are insufficient, inspect repo-local sources and the vibe-loop task
CLI output before making assumptions.
"""
RESOURCE_SCHEDULER_LOCK_TIMEOUT_SECONDS = 5.0
RESOURCE_SCHEDULER_LOCK_POLL_SECONDS = 0.01


@dataclasses.dataclass(frozen=True)
class SessionIdObservation:
    session_id: str
    source: str


@dataclasses.dataclass(frozen=True)
class StreamingCommandResult:
    exit_code: int
    session_id: str | None = None
    session_id_source: str | None = None


@dataclasses.dataclass(frozen=True)
class ClassificationResult:
    status: str
    source: str


@dataclasses.dataclass(frozen=True)
class BatchSelectionValidation:
    tasks: tuple[Task, ...] = ()
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error


@dataclasses.dataclass(frozen=True)
class ConflictDomains:
    known: bool
    resources: frozenset[str] = dataclasses.field(default_factory=frozenset)
    paths: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SchedulerLock:
    path: Path
    handle: BinaryIO


class SchedulerLockBusy(RuntimeError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"resource scheduler lock is busy: {path}")


class SessionIdObserver:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observation: SessionIdObservation | None = None

    @property
    def observation(self) -> SessionIdObservation | None:
        with self._lock:
            return self._observation

    def observe_line(self, line: str, stream_name: str) -> None:
        session_id = parse_worker_session_id(line)
        if session_id is None:
            return
        with self._lock:
            if self._observation is None:
                self._observation = SessionIdObservation(
                    session_id=session_id,
                    source=f"native:{stream_name}",
                )


class VibeRunner:
    def __init__(self, config: VibeConfig):
        self.config = config
        self._source: TaskSource | None = None
        self._source_resolution: RuntimeTaskSourceResolution | None = None
        self.lock_manager = LockManager(config.state_path / "locks")
        self.runs_dir = config.state_path / "runs"
        self.run_store = RunStore(config.state_path / "runs.jsonl")
        self._record_lock = threading.Lock()

    @property
    def source_resolution(self) -> RuntimeTaskSourceResolution:
        if self._source_resolution is None:
            self._source_resolution = resolve_runtime_task_source(self.config)
        return self._source_resolution

    @property
    def source(self) -> TaskSource:
        if self._source is None:
            self._source = build_task_source(
                self.config.repo,
                self.source_resolution.task_source,
            )
        return self._source

    def list_candidates(self, exclude: set[str] | None = None) -> list[Task]:
        excluded = exclude or set()
        tasks = runnable_tasks(
            self.source,
            self.source_resolution.task_source.runnable_statuses,
        )
        active_domains = active_lock_conflict_domains(self.lock_manager)
        enforce_conflicts = resource_conflicts_enabled(tasks, active_domains)
        return [
            task
            for task in tasks
            if task.task_id not in excluded
            and not self.lock_manager.is_locked(task.task_id)
            and (
                not enforce_conflicts
                or not task_conflicts_with_domains(task, active_domains)
            )
        ]

    def select_task(
        self, ask_agent: bool = False, exclude: set[str] | None = None
    ) -> Task | None:
        candidates = self.list_candidates(exclude=exclude)
        if not candidates:
            return None
        return self.select_from_candidates(candidates, ask_agent=ask_agent)

    def select_from_candidates(
        self,
        candidates: list[Task],
        ask_agent: bool = False,
    ) -> Task:
        if ask_agent and len(candidates) > 1:
            report_status(
                f"asking agent to select next task from {len(candidates)} candidates"
            )
            selected = self.ask_agent_to_select(candidates)
            if selected is not None:
                report_status(f"agent selected {selected.task_id}: {selected.title}")
                return selected
            report_status("agent selection unavailable; using first ready task")
        return candidates[0]

    def select_batch_from_candidates(
        self,
        candidates: list[Task],
        *,
        limit: int,
        ask_agent: bool = False,
    ) -> list[Task]:
        batch_limit = min(max(limit, 0), len(candidates))
        if batch_limit == 0:
            return []
        if ask_agent and len(candidates) > 1:
            report_status(
                "asking agent to select batch of up to "
                f"{batch_limit} tasks from {len(candidates)} candidates"
            )
            selected = self.ask_agent_to_select_batch(candidates, batch_limit)
            if selected:
                task_ids = ", ".join(task.task_id for task in selected)
                report_status(f"agent selected batch: {task_ids}")
                return selected
            report_status(
                "agent batch selection unavailable or invalid; "
                "using deterministic ready order"
            )
        return deterministic_task_batch(
            candidates,
            batch_limit,
            is_locked=self.lock_manager.is_locked,
        )

    def ask_agent_to_select(self, candidates: list[Task]) -> Task | None:
        prompt = build_selection_prompt(candidates, self.recent_log_context())
        command_template = self.config.agent.require_selection_command()
        report_status(
            "agent selection command source: "
            f"{self.config.agent.selection_command_source}"
        )
        report_status(f"agent default policy source: {AGENT_DEFAULT_POLICY_SOURCE}")
        report_status(f"agent default policy: {AGENT_DEFAULT_POLICY}")
        command_str = command_template.format(prompt=shell_quote(prompt))
        cmd, use_shell = prepare_shell_command(command_str)
        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.repo,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        task_id = parse_selected_task_id(result.stdout)
        if task_id is None:
            return None
        return next((task for task in candidates if task.task_id == task_id), None)

    def ask_agent_to_select_batch(
        self,
        candidates: list[Task],
        limit: int,
    ) -> list[Task] | None:
        prompt = build_batch_selection_prompt(
            candidates,
            max_tasks=limit,
            recent_log_context=self.recent_log_context(),
            active_worker_context=self.active_worker_context(),
        )
        command_template = self.config.agent.require_selection_command()
        report_status(
            "agent selection command source: "
            f"{self.config.agent.selection_command_source}"
        )
        report_status(f"agent default policy source: {AGENT_DEFAULT_POLICY_SOURCE}")
        report_status(f"agent default policy: {AGENT_DEFAULT_POLICY}")
        command_str = command_template.format(prompt=shell_quote(prompt))
        cmd, use_shell = prepare_shell_command(command_str)
        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.repo,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        validation = validate_selected_task_batch(
            parse_selected_task_ids(result.stdout),
            candidates,
            limit=limit,
            is_locked=self.lock_manager.is_locked,
            enforce_resource_conflicts=resource_conflicts_enabled(candidates, ()),
        )
        if not validation.valid:
            report_status(f"agent batch selection rejected: {validation.error}")
            return None
        return list(validation.tasks)

    def active_worker_context(self) -> str:
        workers = [
            selection_worker_json(worker)
            for worker in build_worker_views(self.lock_manager, self.run_store)
        ]
        if not workers:
            return "No active vibe-loop workers recorded."
        return "Active vibe-loop workers:\n" + json.dumps(workers, indent=2)

    def run_task(self, task: Task) -> RunResult:
        command_template = self.config.agent.require_command()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(task.task_id)
        log_path = self.runs_dir / f"{run_id}.log"
        start_main = git_rev_parse(self.config.repo, "HEAD")
        base_main = git_rev_parse(self.config.repo, self.config.main_branch)
        exit_code = 1
        message = ""
        session_id = run_id
        session_id_source = "fallback:run_id"
        skill_prefix = self.config.agent.skill_ref_prefix
        worker_prompt = f"{skill_prefix}vibe-loop {task.task_id}{CLI_WORKER_ADDENDUM}"
        command = command_template.format(
            prompt=shell_quote(worker_prompt),
            task_id=task.task_id,
            run_id=run_id,
        )
        command_env = worker_command_env(
            run_id=run_id,
            task_id=task.task_id,
            repo=self.config.repo,
            log_path=log_path,
        )
        worker_report: WorkerReport | None = None
        active_state = ActiveRunState.new(
            task_id=task.task_id,
            run_id=run_id,
            log_path=log_path,
            base_main=base_main,
            command=command,
            resources=task.resources,
            paths=task.paths,
            conflict_domains_known=task.conflict_domains_known,
        )
        task_lock = self.acquire_scheduled_task_lock(
            task,
            run_id,
            active_state,
        )
        try:
            with log_path.open("w", encoding="utf-8") as log:
                write_log_header(
                    log,
                    task,
                    command,
                    start_main,
                    run_id,
                    self.config.agent.command_source,
                    self.config.agent.selection_command_source,
                    self.config.agent.detected,
                )
                report_status(f"running {task.task_id}: {task.title}", log)
                report_status(f"run_id={run_id}", log)
                report_status(f"log: {log_path}", log)
                report_status(
                    f"agent command source: {self.config.agent.command_source}",
                    log,
                )
                report_status(
                    "agent selection command source: "
                    f"{self.config.agent.selection_command_source}",
                    log,
                )
                report_status(
                    f"agent default policy source: {AGENT_DEFAULT_POLICY_SOURCE}",
                    log,
                )
                report_status(f"agent default policy: {AGENT_DEFAULT_POLICY}", log)
                report_status(
                    "detected agents: "
                    f"{format_detected_agents(self.config.agent.detected)}",
                    log,
                )
                report_status("agent command started", log)

                def record_worker_pid(worker_pid: int) -> None:
                    nonlocal active_state, task_lock
                    active_state = active_state.with_worker_pid(worker_pid)
                    task_lock = self.lock_manager.update(
                        task_lock,
                        active_state.to_lock_metadata(),
                    )
                    report_status(
                        "worker process started "
                        f"task={task.task_id} run_id={run_id} pid={worker_pid}",
                        log,
                    )

                stream_result = run_streaming_command(
                    command,
                    self.config.repo,
                    log,
                    env=command_env,
                    forward_stderr=self.config.agent.forward_stderr,
                    on_start=record_worker_pid,
                )
                exit_code = stream_result.exit_code
                session_id = stream_result.session_id or run_id
                session_id_source = stream_result.session_id_source or "fallback:run_id"
                report_status(f"agent command exit_code={exit_code}", log)
                report_status(f"session_id={session_id}", log)
                report_status(f"session_id_source={session_id_source}", log)
                worker_report = self.run_store.latest_worker_report(
                    run_id,
                    task.task_id,
                )
                if worker_report is not None:
                    report_status(
                        f"worker report status={worker_report.status}",
                        log,
                    )
                    if worker_report.commit:
                        report_status(
                            f"worker report commit={worker_report.commit}",
                            log,
                        )
                elif exit_code == 0:
                    message = self.run_completion_checks(log)
            end_main = git_rev_parse(self.config.repo, "HEAD")
            classification = self.classify(
                task.task_id,
                exit_code,
                start_main,
                end_main,
                message,
                worker_report,
            )
            result = RunResult(
                run_id=run_id,
                task_id=task.task_id,
                classification=classification.status,
                exit_code=exit_code,
                log_path=log_path,
                start_main=start_main,
                end_main=end_main,
                message=message,
                session_id=session_id,
                session_id_source=session_id_source,
                agent_command_source=self.config.agent.command_source,
                agent_selection_command_source=self.config.agent.selection_command_source,
                agent_default_policy_source=AGENT_DEFAULT_POLICY_SOURCE,
                agent_default_policy=AGENT_DEFAULT_POLICY,
                classification_source=classification.source,
                worker_report=(
                    worker_report.to_json() if worker_report is not None else None
                ),
            )
            self.record_result(result)
            report_status(
                f"recorded {classification.status} result for {task.task_id}: "
                f"{log_path}"
            )
            return result
        finally:
            self.lock_manager.release(task_lock)

    def acquire_scheduled_task_lock(
        self,
        task: Task,
        run_id: str,
        active_state: ActiveRunState,
    ) -> TaskLock:
        scheduler_lock = self.acquire_scheduler_lock(
            run_id,
            task.task_id,
        )
        try:
            active_domains = active_lock_conflict_domains(self.lock_manager)
            if resource_conflicts_enabled([task], active_domains) and (
                task_conflicts_with_domains(task, active_domains)
            ):
                raise LockBusy(
                    scheduler_lock.path,
                    {
                        "reason": "resource_conflict",
                        "task_id": task.task_id,
                        "resources": list(task.resources),
                        "paths": list(task.paths),
                        "conflict_domains_known": task.conflict_domains_known,
                    },
                )
            return self.lock_manager.acquire(
                task.task_id,
                run_id,
                metadata=active_state.to_lock_metadata(),
            )
        finally:
            self.release_scheduler_lock(scheduler_lock)

    def acquire_scheduler_lock(self, run_id: str, task_id: str) -> SchedulerLock:
        lock_path = (
            self.config.state_path
            / "internal-locks"
            / f"{RESOURCE_SCHEDULER_LOCK_NAME}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        deadline = time.monotonic() + RESOURCE_SCHEDULER_LOCK_TIMEOUT_SECONDS
        while True:
            if not try_lock_scheduler_file(handle):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise SchedulerLockBusy(lock_path)
                time.sleep(RESOURCE_SCHEDULER_LOCK_POLL_SECONDS)
                continue
            handle.seek(0)
            handle.truncate()
            payload = {
                "record_type": "resource_scheduler_lock",
                "run_id": run_id,
                "owner_task_id": task_id,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
            }
            handle.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            return SchedulerLock(path=lock_path, handle=handle)

    def release_scheduler_lock(self, scheduler_lock: SchedulerLock) -> None:
        try:
            unlock_scheduler_file(scheduler_lock.handle)
        finally:
            scheduler_lock.handle.close()

    def run_next(
        self, ask_agent: bool = False, exclude: set[str] | None = None
    ) -> RunResult | None:
        candidates = self.list_candidates(exclude=exclude)
        if not candidates:
            return None
        self.config.agent.require_command()
        task = self.select_from_candidates(candidates, ask_agent=ask_agent)
        try:
            return self.run_task(task)
        except LockBusy:
            report_status(f"task locked during acquire, retrying: {task.task_id}")
            excluded = set(exclude or set())
            excluded.add(task.task_id)
            return self.run_next(ask_agent=ask_agent, exclude=excluded)

    def run_until_done(
        self,
        ask_agent: bool = False,
        max_slices: int = 0,
        continue_on_failure: bool = False,
        jobs: int = 1,
    ) -> list[RunResult]:
        if jobs < 1:
            raise ValueError("run-until-done --jobs must be at least 1")
        if jobs == 1:
            return self.run_until_done_serial(
                ask_agent=ask_agent,
                max_slices=max_slices,
                continue_on_failure=continue_on_failure,
            )
        return self.run_until_done_parallel(
            ask_agent=ask_agent,
            max_slices=max_slices,
            continue_on_failure=continue_on_failure,
            jobs=jobs,
        )

    def run_until_done_serial(
        self,
        ask_agent: bool = False,
        max_slices: int = 0,
        continue_on_failure: bool = False,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        while max_slices <= 0 or len(results) < max_slices:
            result = self.run_next(ask_agent=ask_agent, exclude=skipped)
            if result is None:
                break
            results.append(result)
            if result.classification == "completed":
                continue
            skipped.add(result.task_id)
            if not continue_on_failure and result.classification in {
                "failed",
                "unknown",
            }:
                break
        return results

    def run_until_done_parallel(
        self,
        ask_agent: bool,
        max_slices: int,
        continue_on_failure: bool,
        jobs: int,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        in_flight: dict[Future[RunResult], str] = {}
        scheduled: dict[str, Task] = {}
        command_validated = False
        announced = False
        stop_after_running = False

        with ThreadPoolExecutor(
            max_workers=jobs,
            thread_name_prefix="vibe-loop-worker",
        ) as executor:
            while True:
                while (
                    not stop_after_running
                    and len(in_flight) < jobs
                    and (max_slices <= 0 or len(results) + len(in_flight) < max_slices)
                ):
                    candidates = self.list_candidates(exclude=skipped | set(scheduled))
                    candidates = filter_scheduled_conflicts(
                        candidates,
                        list(scheduled.values()),
                    )
                    if not candidates:
                        break
                    if not command_validated:
                        self.config.agent.require_command()
                        command_validated = True
                    open_slots = jobs - len(in_flight)
                    if max_slices > 0:
                        open_slots = min(
                            open_slots,
                            max_slices - len(results) - len(in_flight),
                        )
                    tasks = self.select_batch_from_candidates(
                        candidates,
                        limit=open_slots,
                        ask_agent=ask_agent,
                    )
                    if not tasks:
                        break
                    if not announced:
                        report_status(f"parallel supervisor jobs={jobs}")
                        announced = True
                    for task in tasks:
                        scheduled[task.task_id] = task
                        report_status(f"queueing {task.task_id}: {task.title}")
                        in_flight[executor.submit(self.run_task, task)] = task.task_id
                    if ask_agent and len(candidates) > 1 and len(tasks) < open_slots:
                        break

                if not in_flight:
                    break

                completed, _pending = wait(
                    in_flight,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    task_id = in_flight.pop(future)
                    scheduled.pop(task_id, None)
                    try:
                        result = future.result()
                    except LockBusy:
                        report_status(
                            f"task locked during acquire, skipping: {task_id}"
                        )
                        skipped.add(task_id)
                        continue
                    results.append(result)
                    if result.classification == "completed":
                        continue
                    skipped.add(result.task_id)
                    if result.classification in {"failed", "unknown"}:
                        stop_after_running = not continue_on_failure
        return results

    def run_completion_checks(self, log) -> str:
        for command in self.config.completion.commands:
            report_status(f"completion check started: {command}", log)
            result = subprocess.run(
                command,
                cwd=self.config.repo,
                shell=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            report_status(
                f"completion check exit_code={result.returncode}: {command}", log
            )
            if result.returncode != 0:
                return f"completion check failed: {command}"
        return ""

    def classify(
        self,
        task_id: str,
        exit_code: int,
        start_main: str,
        end_main: str,
        message: str,
        worker_report: WorkerReport | None = None,
    ) -> ClassificationResult:
        if worker_report is not None:
            return ClassificationResult(worker_report.status, "worker_report")
        if exit_code != 0 or message:
            return ClassificationResult("failed", "exit_code_or_completion_check")
        task = self.source.probe(task_id)
        if task and task.status == "Done":
            return ClassificationResult("completed", "task_probe")
        if task and task.status == "Gated":
            return ClassificationResult("blocked", "task_probe")
        if start_main != end_main and task is None:
            return ClassificationResult("completed", "main_change")
        return ClassificationResult("unknown", "fallback")

    def record_result(self, result: RunResult) -> None:
        with self._record_lock:
            self.run_store.append_result(result)

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        return self.run_store.recent_log_context(max_runs, tail_lines)


def build_selection_prompt(candidates: list[Task], recent_log_context: str) -> str:
    return (
        "Choose exactly one next task from the dependency-ready, unlocked "
        "candidates. Use the recent run logs to avoid retrying a task that is "
        "blocked or just failed for a persistent reason. Return JSON only: "
        '{"task_id":"...","reason":"..."}\n\n'
        "Candidates:\n"
        f"{json.dumps([task.to_json() for task in candidates], indent=2)}\n\n"
        f"{recent_log_context}\n"
    )


def build_batch_selection_prompt(
    candidates: list[Task],
    *,
    max_tasks: int,
    recent_log_context: str,
    active_worker_context: str,
) -> str:
    metadata = {
        "max_batch_size": max_tasks,
        "candidate_count": len(candidates),
        "selection_rules": [
            "choose between 1 and max_batch_size task IDs",
            "choose only IDs from candidates",
            "do not return duplicate IDs",
            "do not combine overlapping declared resources or paths",
            "do not combine undeclared conflict domains with declared ones",
            "avoid tasks blocked by recent run evidence",
            "consider active workers when choosing compatible work",
        ],
    }
    return (
        "Choose a compatible batch from the dependency-ready, unlocked "
        "candidates. Use recent run logs to avoid retrying a task that is "
        "blocked or just failed for a persistent reason. Use active worker "
        "state to avoid conflicting with work already in progress. Return "
        'JSON only: {"task_ids":["..."],"reason":"..."}\n\n'
        "Batch metadata:\n"
        f"{json.dumps(metadata, indent=2)}\n\n"
        "Candidates:\n"
        f"{json.dumps([task.to_json() for task in candidates], indent=2)}\n\n"
        f"{active_worker_context}\n\n"
        f"{recent_log_context}\n"
    )


def selection_worker_json(worker: WorkerView) -> dict[str, object]:
    payload = worker.to_json()
    return {
        "task_id": payload["task_id"],
        "run_id": payload["run_id"],
        "state": payload["state"],
        "process_state": payload["process_state"],
        "stale_reason": payload["stale_reason"],
        "result_status": payload["result_status"],
        "started_at": payload["started_at"],
        "log": payload["log"],
        "resources": payload["resources"],
        "paths": payload["paths"],
        "conflict_domains_known": payload["conflict_domains_known"],
    }


def parse_selected_task_id(output: str) -> str | None:
    payload = selection_payload_from_output(output)
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("task_id")
    return str(task_id) if task_id else None


def parse_selected_task_ids(output: str) -> list[str] | None:
    payload = selection_payload_from_output(output)
    if not isinstance(payload, dict):
        return None
    task_ids = payload.get("task_ids")
    if task_ids is None:
        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            return [task_id]
        return None
    if not isinstance(task_ids, list) or not task_ids:
        return None
    selected: list[str] = []
    for task_id in task_ids:
        if not isinstance(task_id, str) or not task_id:
            return None
        selected.append(task_id)
    return selected


def selection_payload_from_output(output: str) -> object | None:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        payload = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload


def validate_selected_task_batch(
    selected_task_ids: list[str] | None,
    candidates: list[Task],
    *,
    limit: int,
    is_locked: Callable[[str], bool] | None = None,
    enforce_resource_conflicts: bool | None = None,
) -> BatchSelectionValidation:
    if selected_task_ids is None:
        return BatchSelectionValidation(error="missing task_ids")
    if not selected_task_ids:
        return BatchSelectionValidation(error="empty task_ids")
    if limit < 1:
        return BatchSelectionValidation(error="batch limit must be at least 1")
    if len(selected_task_ids) > limit:
        return BatchSelectionValidation(error="too many task_ids")
    candidate_by_id = {task.task_id: task for task in candidates}
    seen: set[str] = set()
    tasks: list[Task] = []
    for task_id in selected_task_ids:
        if task_id in seen:
            return BatchSelectionValidation(error=f"duplicate task_id: {task_id}")
        task = candidate_by_id.get(task_id)
        if task is None:
            return BatchSelectionValidation(error=f"unknown task_id: {task_id}")
        if is_locked is not None and is_locked(task_id):
            return BatchSelectionValidation(error=f"locked task_id: {task_id}")
        seen.add(task_id)
        tasks.append(task)
    if should_enforce_resource_conflicts(
        tasks,
        candidates,
        enforce_resource_conflicts,
    ):
        conflict = first_task_conflict(tasks)
        if conflict is not None:
            left, right = conflict
            return BatchSelectionValidation(
                error=f"conflicting task_ids: {left.task_id}, {right.task_id}"
            )
    return BatchSelectionValidation(tasks=tuple(tasks))


def deterministic_task_batch(
    candidates: list[Task],
    limit: int,
    *,
    is_locked: Callable[[str], bool] | None = None,
    enforce_resource_conflicts: bool | None = None,
) -> list[Task]:
    selected: list[Task] = []
    enforce_conflicts = should_enforce_resource_conflicts(
        selected,
        candidates,
        enforce_resource_conflicts,
    )
    for task in candidates:
        if len(selected) >= limit:
            break
        if is_locked is not None and is_locked(task.task_id):
            continue
        if enforce_conflicts and task_conflicts_with_tasks(task, selected):
            continue
        selected.append(task)
    return selected


def should_enforce_resource_conflicts(
    selected: list[Task],
    candidates: list[Task],
    override: bool | None,
) -> bool:
    if override is not None:
        return override
    return resource_conflicts_enabled([*selected, *candidates], ())


def filter_scheduled_conflicts(
    candidates: list[Task],
    scheduled: list[Task],
) -> list[Task]:
    if not scheduled:
        return candidates
    if not resource_conflicts_enabled([*candidates, *scheduled], ()):
        return candidates
    return [
        candidate
        for candidate in candidates
        if not task_conflicts_with_tasks(candidate, scheduled)
    ]


def resource_conflicts_enabled(
    tasks: list[Task],
    active_domains: tuple[ConflictDomains, ...],
) -> bool:
    return any(task.conflict_domains_known for task in tasks) or any(
        domain.known for domain in active_domains
    )


def active_lock_conflict_domains(
    lock_manager: LockManager,
) -> tuple[ConflictDomains, ...]:
    domains: list[ConflictDomains] = []
    for metadata in lock_manager.list_locks():
        active = ActiveRunState.from_lock_metadata(metadata)
        if active is None:
            continue
        domains.append(conflict_domains_from_task_like(active))
    return tuple(domains)


def first_task_conflict(tasks: list[Task]) -> tuple[Task, Task] | None:
    for index, task in enumerate(tasks):
        for other in tasks[index + 1 :]:
            if task_conflicts_with_task(task, other):
                return task, other
    return None


def task_conflicts_with_domains(
    task: Task,
    active_domains: tuple[ConflictDomains, ...],
) -> bool:
    task_domains = conflict_domains_from_task_like(task)
    return any(
        conflict_domains_overlap(task_domains, domain) for domain in active_domains
    )


def task_conflicts_with_tasks(task: Task, selected: list[Task]) -> bool:
    return any(task_conflicts_with_task(task, other) for other in selected)


def task_conflicts_with_task(left: Task, right: Task) -> bool:
    return conflict_domains_overlap(
        conflict_domains_from_task_like(left),
        conflict_domains_from_task_like(right),
    )


def conflict_domains_from_task_like(task: Task | ActiveRunState) -> ConflictDomains:
    return ConflictDomains(
        known=task.conflict_domains_known,
        resources=frozenset(task.resources),
        paths=task.paths,
    )


def conflict_domains_overlap(left: ConflictDomains, right: ConflictDomains) -> bool:
    if not left.known or not right.known:
        return True
    if left.resources & right.resources:
        return True
    return path_domains_overlap(left.paths, right.paths)


def path_domains_overlap(
    left_paths: tuple[str, ...], right_paths: tuple[str, ...]
) -> bool:
    for left in left_paths:
        for right in right_paths:
            if path_domain_overlaps(left, right):
                return True
    return False


def path_domain_overlaps(left: str, right: str) -> bool:
    return (
        left == "."
        or right == "."
        or left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


def try_lock_scheduler_file(handle: BinaryIO) -> bool:
    ensure_scheduler_lock_byte(handle)
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is not None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    raise SchedulerLockBusy(Path("<unsupported-platform>"))


def unlock_scheduler_file(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_scheduler_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def parse_worker_session_id(line: str) -> str | None:
    match = SESSION_ID_RE.search(line)
    if match is None:
        return None
    return match.group("session_id")


def write_log_header(
    log,
    task: Task,
    command: str,
    start_main: str,
    run_id: str,
    command_source: str,
    selection_command_source: str,
    detected: AgentDetection,
) -> None:
    log.write(f"[vibe-loop] run_id={run_id}\n")
    log.write(f"[vibe-loop] task_id={task.task_id}\n")
    log.write(f"[vibe-loop] title={task.title}\n")
    log.write(f"[vibe-loop] command={command}\n")
    log.write(f"[vibe-loop] agent_command_source={command_source}\n")
    log.write(
        f"[vibe-loop] agent_selection_command_source={selection_command_source}\n"
    )
    log.write(
        f"[vibe-loop] agent_default_policy_source={AGENT_DEFAULT_POLICY_SOURCE}\n"
    )
    log.write(f"[vibe-loop] agent_default_policy={AGENT_DEFAULT_POLICY}\n")
    log.write(f"[vibe-loop] detected_agents={format_detected_agents(detected)}\n")
    log.write(f"[vibe-loop] start_main={start_main}\n\n")


def report_status(message: str, log: TextIO | None = None) -> None:
    line = f"[vibe-loop] {message}"
    print(line, file=sys.stderr)
    if log is not None:
        log.write(line + "\n")
        log.flush()


def run_streaming_command(
    command: str,
    cwd: Path,
    log: TextIO,
    *,
    env: dict[str, str] | None = None,
    forward_stderr: bool = False,
    on_start: Callable[[int], None] | None = None,
) -> StreamingCommandResult:
    cmd, use_shell = prepare_shell_command(command)
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=use_shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    if on_start is not None:
        on_start(process.pid)
    assert process.stdout is not None
    assert process.stderr is not None
    log_lock = threading.Lock()
    session_observer = SessionIdObserver()
    stdout_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stdout, log, log_lock, True, session_observer, "stdout"),
    )
    stderr_thread = threading.Thread(
        target=stream_pipe,
        args=(
            process.stderr,
            log,
            log_lock,
            forward_stderr,
            session_observer,
            "stderr",
        ),
    )
    stdout_thread.start()
    stderr_thread.start()
    exit_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    observation = session_observer.observation
    return StreamingCommandResult(
        exit_code=exit_code,
        session_id=observation.session_id if observation else None,
        session_id_source=observation.source if observation else None,
    )


def worker_command_env(
    *,
    run_id: str,
    task_id: str,
    repo: Path,
    log_path: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "VIBE_LOOP_RUN_ID": run_id,
            "VIBE_LOOP_TASK_ID": task_id,
            "VIBE_LOOP_REPO": str(repo),
            "VIBE_LOOP_LOG": str(log_path),
        }
    )
    return env


def stream_pipe(
    pipe: TextIO,
    log: TextIO,
    log_lock: threading.Lock,
    forward: bool,
    session_observer: SessionIdObserver,
    stream_name: str,
) -> None:
    try:
        for line in pipe:
            session_observer.observe_line(line, stream_name)
            if forward:
                sys.stderr.write(line)
                sys.stderr.flush()
            with log_lock:
                log.write(line)
                log.flush()
    finally:
        pipe.close()


def new_run_id(task_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    safe_task = "".join(
        char if char.isalnum() or char in "-._" else "_" for char in task_id
    )
    return f"{timestamp}-{safe_task}-{suffix}"


def format_detected_agents(detected: AgentDetection) -> str:
    return detected.summary()


def git_rev_parse(repo: Path, rev: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", rev],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
