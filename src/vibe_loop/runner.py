from __future__ import annotations

import dataclasses
import hashlib
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
from pathlib import Path, PurePosixPath
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
from vibe_loop.generated_discovery import (
    is_allowed_evidence_file,
    is_secret_like_directory_name,
    is_secret_like_path,
    is_webhook_like_evidence_path,
    redact_evidence_text,
    redact_manifest_text,
)
from vibe_loop.locks import LockBusy, LockManager, TaskLock
from vibe_loop.retry import (
    is_transient_stderr,
    retry_subprocess_run,
)
from vibe_loop.runs import (
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    WorkerReport,
)
from vibe_loop.spec_diagnostics import ensure_spec_execution_gate
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
SHA256_HEX_RE = re.compile(r"^[a-fA-F0-9]{64}$")
RESOURCE_SCHEDULER_LOCK_NAME = "resource-scheduler"
SPEC_WORKER_CONTEXT_SCHEMA_VERSION = 1
SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS = 12_000
SPEC_WORKER_CONTEXT_MAX_ARTIFACT_CHARS = 4_000
SPEC_WORKER_CONTEXT_MAX_FILE_BYTES = 512 * 1024
SPEC_WORKER_CONTEXT_MAX_ARTIFACTS = 8
SPEC_WORKER_CONTEXT_MAX_FIELD_CHARS = 1_500
SPEC_WORKER_CONTEXT_MAX_REF_CHARS = 300
SPEC_WORKER_CONTEXT_MAX_LIST_ITEMS = 20
SPEC_WORKER_CONTEXT_MAX_FINGERPRINTS = 20
SPEC_WORKER_CONTEXT_LINE_CONTEXT = 3

CLI_WORKER_ADDENDUM = """\

## vibe-loop CLI Coordination

You are running as a worker launched by the vibe-loop CLI. The following
environment variables identify this run:
- VIBE_LOOP_REPO - path to the repository
- VIBE_LOOP_RUN_ID - unique run identifier
- VIBE_LOOP_TASK_ID - task being worked on
- VIBE_LOOP_LOG - path to the run log file

### Workspace Claim

After creating or choosing your task branch/worktree, and before implementation
edits, attach that workspace to the active task lock:

```bash
vibe-loop worker claim-workspace --repo "$VIBE_LOOP_REPO" \\
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \\
  --branch <branch-name> --worktree <absolute-worktree-path>
```

Use the real branch name and absolute worktree path, not the placeholders. If
the claim fails with an owner mismatch, missing active task lock, mismatched
branch/worktree, or unsafe workspace diagnostic, stop mutating repository state
and report the run as blocked through the worker report protocol. Workspace
claims are advisory visibility metadata only; they do not permit deleting,
resetting, cleaning, merging, or stealing another worker's branch/worktree.

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

The report records the outcome of this worker run; it does not update the task
graph. Before reporting "completed", update the repository's active task source
so this task is no longer runnable there: for example, mark the Markdown plan
row `Done`, or ensure the configured command-backed task adapter will return a
completed/non-runnable status. If policy or tooling prevents that update,
report "blocked" or "unknown" with the precise reason instead of reporting a
completed run that the task source still exposes as runnable.

This boundary is intentional. Task status must remain project-owned so agents
and humans working without the vibe-loop CLI can manage the same backlog through
the repository's normal plan, tracker, or adapter workflow.

When a blocker or failure occurs after code was changed, commit or otherwise
stabilize the slice before reporting unless doing so would be unsafe. Do not
let the report replace the final user-facing summary; the report is supervisor
state, while the summary explains what happened.

### Integration Locking

Before the final fast-forward merge to main, acquire the advisory
main-integration lock:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \\
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \\
  --wait --timeout 300
```

If the command reports a live holder timeout, park the slice as blocked; do not
enter the final integration section without the lock. If the lock appears
stale, or workspace preflight reports unsafe claimed-workspace diagnostics,
report the run as blocked with the precise integration-lock or workspace reason
and follow repo policy rather than stealing or cleaning state.

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
MAX_TRANSIENT_TASK_RETRIES = 3
TRANSIENT_COOLDOWN_SECONDS = 30.0


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
            result = retry_subprocess_run(
                cmd,
                cwd=self.config.repo,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                on_retry=_selection_retry_callback,
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
            result = retry_subprocess_run(
                cmd,
                cwd=self.config.repo,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                on_retry=_selection_retry_callback,
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
        self.ensure_spec_execution_gate()
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
        worker_prompt = build_worker_prompt(skill_prefix, task, self.config)
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
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.lock_event(
                LOCK_ACQUIRED_RECORD_TYPE,
                run_id=run_id,
                task_id=task.task_id,
                lock_kind="task",
                lock_path=task_lock.path,
                payload={
                    "resources": list(task.resources),
                    "paths": list(task.paths),
                    "conflict_domains_known": task.conflict_domains_known,
                },
            )
        )
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.run_state_transition(
                run_id=run_id,
                task_id=task.task_id,
                to_state="started",
                reason="task_lock_acquired",
            )
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
                self.run_store.append_lifecycle_event(
                    RunLifecycleEvent.run_state_transition(
                        run_id=run_id,
                        task_id=task.task_id,
                        from_state="started",
                        to_state="session_observed",
                        reason=session_id_source,
                        payload={"session_id": session_id},
                    )
                )
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
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.run_state_transition(
                    run_id=run_id,
                    task_id=task.task_id,
                    from_state="session_observed",
                    to_state="classified",
                    reason=classification.source,
                    payload={"classification": classification.status},
                )
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
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_RELEASED_RECORD_TYPE,
                    run_id=run_id,
                    task_id=task.task_id,
                    lock_kind="task",
                    lock_path=task_lock.path,
                )
            )

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
        try:
            while True:
                if not try_lock_scheduler_file(handle):
                    if time.monotonic() >= deadline:
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
                handle.write(
                    (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
                )
                handle.flush()
                os.fsync(handle.fileno())
                return SchedulerLock(path=lock_path, handle=handle)
        except BaseException:
            handle.close()
            raise

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
        self.ensure_spec_execution_gate()
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
        max_tasks: int = 0,
    ) -> list[RunResult]:
        if jobs < 1:
            raise ValueError("run-until-done --jobs must be at least 1")
        if jobs == 1:
            return self.run_until_done_serial(
                ask_agent=ask_agent,
                max_slices=max_slices,
                continue_on_failure=continue_on_failure,
                max_tasks=max_tasks,
            )
        return self.run_until_done_parallel(
            ask_agent=ask_agent,
            max_slices=max_slices,
            continue_on_failure=continue_on_failure,
            jobs=jobs,
            max_tasks=max_tasks,
        )

    def run_until_done_serial(
        self,
        ask_agent: bool = False,
        max_slices: int = 0,
        continue_on_failure: bool = False,
        max_tasks: int = 0,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        # Completed tasks that remain runnable (multi-slice work). They are
        # deprioritized so every other ready task gets a turn before any single
        # task is re-selected, which keeps one large multi-slice task from
        # monopolizing the whole chain. The set is cleared once no other
        # candidate remains, so genuinely multi-slice work still progresses.
        yielded: set[str] = set()
        transient_retries: dict[str, int] = {}
        completed_count = 0
        while max_slices <= 0 or len(results) < max_slices:
            result = self.run_next(ask_agent=ask_agent, exclude=skipped | yielded)
            if result is None:
                if yielded:
                    yielded.clear()
                    continue
                break
            results.append(result)
            if result.classification == "completed":
                transient_retries.pop(result.task_id, None)
                yielded.add(result.task_id)
                completed_count += 1
                if max_tasks > 0 and completed_count >= max_tasks:
                    break
                continue
            if is_transient_worker_failure(result):
                count = transient_retries.get(result.task_id, 0) + 1
                transient_retries[result.task_id] = count
                if count <= MAX_TRANSIENT_TASK_RETRIES:
                    report_status(
                        f"transient failure for {result.task_id} "
                        f"(attempt {count}/{MAX_TRANSIENT_TASK_RETRIES}), "
                        f"cooling down {TRANSIENT_COOLDOWN_SECONDS:.0f}s"
                    )
                    time.sleep(TRANSIENT_COOLDOWN_SECONDS)
                    continue
                report_status(
                    f"transient retries exhausted for {result.task_id}, skipping"
                )
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
        max_tasks: int = 0,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        transient_retries: dict[str, int] = {}
        completed_count = 0
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
                    and (max_tasks <= 0 or completed_count + len(in_flight) < max_tasks)
                ):
                    candidates = self.list_candidates(exclude=skipped | set(scheduled))
                    candidates = filter_scheduled_conflicts(
                        candidates,
                        list(scheduled.values()),
                    )
                    if not candidates:
                        break
                    self.ensure_spec_execution_gate()
                    if not command_validated:
                        self.config.agent.require_command()
                        command_validated = True
                    open_slots = jobs - len(in_flight)
                    if max_slices > 0:
                        open_slots = min(
                            open_slots,
                            max_slices - len(results) - len(in_flight),
                        )
                    if max_tasks > 0:
                        open_slots = min(
                            open_slots,
                            max_tasks - completed_count - len(in_flight),
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
                    except SchedulerLockBusy as exc:
                        report_status(
                            "scheduler lock busy during acquire, skipping: "
                            f"{task_id} path={exc.path}"
                        )
                        skipped.add(task_id)
                        continue
                    except LockBusy:
                        report_status(
                            f"task locked during acquire, skipping: {task_id}"
                        )
                        skipped.add(task_id)
                        continue
                    results.append(result)
                    if result.classification == "completed":
                        transient_retries.pop(result.task_id, None)
                        completed_count += 1
                        if max_tasks > 0 and completed_count >= max_tasks:
                            stop_after_running = True
                        continue
                    if is_transient_worker_failure(result):
                        count = transient_retries.get(result.task_id, 0) + 1
                        transient_retries[result.task_id] = count
                        if count <= MAX_TRANSIENT_TASK_RETRIES:
                            report_status(
                                f"transient failure for {result.task_id} "
                                f"(attempt {count}/{MAX_TRANSIENT_TASK_RETRIES}), "
                                "will re-enqueue"
                            )
                            continue
                        report_status(
                            f"transient retries exhausted for {result.task_id}, "
                            "skipping"
                        )
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

    def ensure_spec_execution_gate(self) -> None:
        ensure_spec_execution_gate(self.config, self.source.list_tasks())

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


def build_worker_prompt(
    skill_prefix: str,
    task: Task,
    config: VibeConfig | None = None,
) -> str:
    prompt = f"{skill_prefix}vibe-loop {task.task_id}{CLI_WORKER_ADDENDUM}"
    if not task.has_traceability:
        return prompt
    prompt = (
        f"{prompt}\n\n"
        "### Normalized Task Traceability\n\n"
        "This task includes optional traceability metadata from the task source:\n\n"
        "```json\n"
        f"{json.dumps(worker_traceability_json(task), indent=2, sort_keys=True)}\n"
        "```\n"
    )
    if config is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "### Spec-Aware Worker Context\n\n"
        "Bounded repo-local spec context for this task:\n\n"
        "```json\n"
        f"{json.dumps(build_spec_worker_context(config, task), indent=2, sort_keys=True)}\n"
        "```\n"
    )


def build_spec_worker_context(config: VibeConfig, task: Task) -> dict[str, object]:
    artifact_refs = spec_context_artifact_refs(task)
    fingerprints_by_path = source_fingerprints_by_path(task)
    artifacts: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    remaining_chars = SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS

    for path, ref_payload in artifact_refs.items():
        if len(artifacts) >= SPEC_WORKER_CONTEXT_MAX_ARTIFACTS:
            skipped.append(
                skipped_spec_context_artifact(
                    path,
                    "artifact_count_limit",
                    f"{len(artifacts) + 1} > {SPEC_WORKER_CONTEXT_MAX_ARTIFACTS}",
                )
            )
            continue
        if remaining_chars <= 0:
            skipped.append(skipped_spec_context_artifact(path, "context_size_limit"))
            continue
        artifact = load_spec_context_artifact(
            config,
            task,
            path,
            roles=tuple(sorted(ref_payload["roles"])),
            refs=tuple(ref_payload["refs"]),
            fingerprints=fingerprints_by_path.get(path, ()),
            max_chars=min(SPEC_WORKER_CONTEXT_MAX_ARTIFACT_CHARS, remaining_chars),
        )
        if "skipped" in artifact:
            skipped.append(artifact["skipped"])
            continue
        artifacts.append(artifact)
        remaining_chars -= len(str(artifact.get("content", "")))

    if task.has_traceability and not artifact_refs:
        skipped.append(
            {
                "path": "",
                "reason": "no_linked_spec_artifacts",
                "detail": (
                    "task has traceability metadata but no spec_paths, design_refs "
                    "with repo-relative paths, or source_fingerprints paths"
                ),
            }
        )

    context = {
        "schema_version": SPEC_WORKER_CONTEXT_SCHEMA_VERSION,
        "task": worker_task_context_json(task),
        "required_verification_gates": required_worker_verification_gates(
            config,
            task,
        ),
        "limits": {
            "max_total_chars": SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS,
            "max_artifact_chars": SPEC_WORKER_CONTEXT_MAX_ARTIFACT_CHARS,
            "max_file_bytes": SPEC_WORKER_CONTEXT_MAX_FILE_BYTES,
            "max_artifacts": SPEC_WORKER_CONTEXT_MAX_ARTIFACTS,
        },
        "artifacts": artifacts,
        "skipped_artifacts": skipped,
    }
    return trim_spec_worker_context_to_limit(context)


def worker_traceability_json(task: Task) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": bounded_context_text(task.task_id, SPEC_WORKER_CONTEXT_MAX_REF_CHARS),
        "title": bounded_context_text(task.title),
        "status": bounded_context_text(task.status, SPEC_WORKER_CONTEXT_MAX_REF_CHARS),
        "source": bounded_context_text(task.source),
    }
    if task.requirement_ids:
        payload["requirement_ids"] = bounded_context_list(task.requirement_ids)
    if task.spec_paths:
        payload["spec_paths"] = bounded_path_context_list(task.spec_paths)
    if task.design_refs:
        payload["design_refs"] = bounded_path_context_list(task.design_refs)
    if task.approval_state:
        payload["approval_state"] = bounded_context_text(task.approval_state)
    if task.source_fingerprints:
        payload["source_fingerprints"] = bounded_source_fingerprints(
            task.source_fingerprints
        )
    return payload


def worker_task_context_json(task: Task) -> dict[str, object]:
    payload = worker_traceability_json(task)
    payload.update(
        {
            "priority": bounded_context_text(
                task.priority,
                SPEC_WORKER_CONTEXT_MAX_REF_CHARS,
            ),
            "scope": bounded_context_text(task.scope),
        }
    )
    if task.resources:
        payload["resources"] = bounded_context_list(task.resources)
    if task.paths:
        payload["paths"] = bounded_path_context_list(task.paths)
    if task.conflict_domains_known:
        payload["conflict_domains_known"] = True
    return payload


def bounded_context_text(
    value: str,
    max_chars: int = SPEC_WORKER_CONTEXT_MAX_FIELD_CHARS,
) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    truncated, _ = truncate_spec_context_text(text, max_chars)
    return truncated


def bounded_context_list(values: tuple[str, ...]) -> list[str]:
    bounded = [
        bounded_context_text(value, SPEC_WORKER_CONTEXT_MAX_REF_CHARS)
        for value in values[:SPEC_WORKER_CONTEXT_MAX_LIST_ITEMS]
    ]
    omitted = len(values) - len(bounded)
    if omitted > 0:
        bounded.append(f"...[{omitted} omitted]")
    return bounded


def bounded_path_context_list(values: tuple[str, ...]) -> list[str]:
    bounded = [
        bounded_context_path(value)
        for value in values[:SPEC_WORKER_CONTEXT_MAX_LIST_ITEMS]
    ]
    omitted = len(values) - len(bounded)
    if omitted > 0:
        bounded.append(f"...[{omitted} omitted]")
    return bounded


def bounded_context_path(value: str) -> str:
    sanitized = safe_path_text_for_prompt(value)
    return bounded_context_text(sanitized, SPEC_WORKER_CONTEXT_MAX_REF_CHARS)


def bounded_source_fingerprints(
    fingerprints: tuple[dict[str, object], ...],
) -> list[dict[str, object]]:
    bounded: list[dict[str, object]] = []
    for fingerprint in fingerprints[:SPEC_WORKER_CONTEXT_MAX_FINGERPRINTS]:
        entry = safe_source_fingerprint_for_prompt(fingerprint)
        if entry:
            bounded.append(entry)
    omitted = len(fingerprints) - len(bounded)
    if omitted > 0:
        bounded.append({"omitted": omitted})
    return bounded


def spec_context_artifact_refs(task: Task) -> dict[str, dict[str, object]]:
    refs: dict[str, dict[str, object]] = {}

    def add(path: str, role: str, ref: str = "") -> None:
        payload = refs.setdefault(path, {"roles": set(), "refs": []})
        payload["roles"].add(role)
        if ref:
            payload["refs"].append(ref)

    for path in task.spec_paths:
        normalized = normalize_context_reference_path(path, allow_bare_path=True)
        if normalized:
            add(normalized, "spec_path", path)
    for ref in task.design_refs:
        normalized = normalize_context_reference_path(ref, allow_bare_path=False)
        if normalized:
            add(normalized, "design_ref", ref)
    for fingerprint in task.source_fingerprints:
        raw_path = fingerprint.get("path")
        if isinstance(raw_path, str):
            normalized = normalize_context_reference_path(
                raw_path,
                allow_bare_path=True,
            )
            if normalized:
                add(normalized, "source_fingerprint", raw_path)
    return refs


def source_fingerprints_by_path(
    task: Task,
) -> dict[str, tuple[dict[str, object], ...]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for fingerprint in task.source_fingerprints:
        raw_path = fingerprint.get("path")
        if not isinstance(raw_path, str):
            continue
        normalized = normalize_context_reference_path(raw_path, allow_bare_path=True)
        if normalized:
            grouped.setdefault(normalized, []).append(fingerprint)
    return {path: tuple(items) for path, items in grouped.items()}


def normalize_context_reference_path(
    value: str,
    *,
    allow_bare_path: bool,
) -> str:
    raw_path = value.strip().replace("\\", "/").split("#", 1)[0].strip()
    if not raw_path:
        return ""
    if (
        not allow_bare_path
        and "/" not in raw_path
        and "." not in PurePosixPath(raw_path).name
    ):
        return ""
    return raw_path


def load_spec_context_artifact(
    config: VibeConfig,
    task: Task,
    path: str,
    *,
    roles: tuple[str, ...],
    refs: tuple[str, ...],
    fingerprints: tuple[dict[str, object], ...],
    max_chars: int,
) -> dict[str, object]:
    safe_path, path_error = safe_spec_context_path(path)
    if path_error:
        return {
            "skipped": skipped_spec_context_artifact(
                path,
                "unsafe_path",
                path_error,
            )
        }
    if not is_allowed_evidence_file(Path(safe_path)):
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "unsupported_file_type",
            )
        }
    repo = config.repo.resolve()
    requested_path = config.repo / safe_path
    if path_has_symlink_component(repo, requested_path):
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "symlink",
            )
        }
    source_path = requested_path.resolve()
    try:
        resolved_relative = source_path.relative_to(repo).as_posix()
    except ValueError:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "outside_repo",
            )
        }
    _resolved_path, resolved_path_error = safe_spec_context_path(resolved_relative)
    if resolved_path_error:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "unsafe_resolved_path",
                resolved_path_error,
            )
        }
    if not source_path.exists():
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "missing",
            )
        }
    try:
        stat_result = source_path.stat()
    except OSError as exc:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "unreadable",
                str(exc),
            )
        }
    if not source_path.is_file():
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "not_file",
            )
        }
    if stat_result.st_size > SPEC_WORKER_CONTEXT_MAX_FILE_BYTES:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "file_too_large",
                f"{stat_result.st_size} > {SPEC_WORKER_CONTEXT_MAX_FILE_BYTES}",
            )
        }
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "unreadable",
                str(exc),
            )
        }
    if b"\0" in raw[:4096]:
        return {
            "skipped": skipped_spec_context_artifact(
                safe_path,
                "binary_file",
            )
        }
    text = raw.decode("utf-8", errors="replace")
    redacted = redact_evidence_text(text)
    selected_text, matched_terms = select_spec_context_text(
        redacted,
        task,
        roles=roles,
        refs=refs,
    )
    content, truncated = truncate_spec_context_text(
        selected_text.strip(),
        max_chars,
    )
    return {
        "path": safe_path,
        "roles": list(roles),
        "refs": [safe_path_text_for_prompt(ref) for ref in refs],
        "size": stat_result.st_size,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "redacted": redacted != text,
        "matched_terms": [
            safe_context_value_for_prompt(term, SPEC_WORKER_CONTEXT_MAX_REF_CHARS)
            for term in matched_terms
        ],
        "truncated": truncated,
        "fingerprint_checks": [
            source_fingerprint_check(fingerprint, raw, stat_result.st_size)
            for fingerprint in fingerprints
        ],
        "content": content,
    }


def safe_spec_context_path(path: str) -> tuple[str, str]:
    normalized = path.strip().replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    if (
        pure_path.is_absolute()
        or any(part in {"", ".."} for part in pure_path.parts)
        or not pure_path.parts
    ):
        return normalized, "path must be safe and repo-relative"
    if is_webhook_like_evidence_path(normalized):
        return normalized, "path is secret-like"
    if any(is_secret_like_directory_name(part) for part in pure_path.parts[:-1]):
        return normalized, "path contains a secret-like directory"
    if is_secret_like_path(Path(pure_path.name)):
        return normalized, "path is secret-like"
    return str(pure_path), ""


def safe_path_text_for_prompt(value: str) -> str:
    raw_path, separator, fragment = value.strip().replace("\\", "/").partition("#")
    if not raw_path:
        return ""
    if raw_path == ".":
        return "."
    _safe_path, path_error = safe_spec_context_path(raw_path)
    if path_error:
        return "<redacted>"
    if separator:
        return f"{raw_path}#{safe_context_value_for_prompt(fragment, 80)}"
    return raw_path


def safe_context_value_for_prompt(value: str, max_chars: int) -> str:
    if secret_like_prompt_value(value):
        return "<redacted>"
    redacted = redact_evidence_text(value)
    return bounded_context_text(redacted, max_chars)


def secret_like_prompt_value(value: str) -> bool:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        is_webhook_like_evidence_path(normalized)
        or any(is_secret_like_directory_name(part) for part in path.parts[:-1])
        or is_secret_like_path(Path(path.name))
    )


def path_has_symlink_component(repo: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(repo)
    except ValueError:
        return False
    current = repo
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def source_fingerprint_check(
    fingerprint: dict[str, object],
    raw: bytes,
    actual_size: int,
) -> dict[str, object]:
    actual_sha = hashlib.sha256(raw).hexdigest()
    check: dict[str, object] = {
        "expected": safe_source_fingerprint_for_prompt(fingerprint),
        "actual": {
            "size": actual_size,
            "sha256": actual_sha,
        },
    }
    expected_size = fingerprint.get("size")
    expected_sha = fingerprint.get("sha256")
    expected_size_is_int = isinstance(expected_size, int) and not isinstance(
        expected_size,
        bool,
    )
    mismatches: list[str] = []
    if expected_size_is_int:
        if expected_size != actual_size:
            mismatches.append("size")
    if isinstance(expected_sha, str) and expected_sha != actual_sha:
        mismatches.append("sha256")
    if not isinstance(expected_sha, str) and not expected_size_is_int:
        check["status"] = "invalid"
        check["reason"] = "fingerprint must include sha256 or size"
    elif mismatches:
        check["status"] = "stale"
        check["mismatches"] = mismatches
    else:
        check["status"] = "current"
    return check


def safe_source_fingerprint_for_prompt(
    fingerprint: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    raw_path = fingerprint.get("path")
    if isinstance(raw_path, str):
        payload["path"] = bounded_context_path(raw_path)
    size = fingerprint.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        payload["size"] = size
    sha256 = fingerprint.get("sha256")
    if isinstance(sha256, str):
        payload["sha256"] = sha256 if SHA256_HEX_RE.fullmatch(sha256) else "<invalid>"
    redacted = fingerprint.get("redacted")
    if isinstance(redacted, bool):
        payload["redacted"] = redacted
    return payload


def select_spec_context_text(
    text: str,
    task: Task,
    *,
    roles: tuple[str, ...],
    refs: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    terms = spec_context_search_terms(task, roles=roles, refs=refs)
    if not terms:
        return text, ()
    sections = markdown_sections_matching_terms(text, terms)
    if sections:
        return "\n\n".join(section for _term, section in sections), tuple(
            dict.fromkeys(term for term, _section in sections)
        )
    line_context = line_context_matching_terms(text, terms)
    if line_context:
        return line_context[1], line_context[0]
    return text, ()


def spec_context_search_terms(
    task: Task,
    *,
    roles: tuple[str, ...],
    refs: tuple[str, ...],
) -> tuple[str, ...]:
    terms: list[str] = []
    if "spec_path" in roles or "source_fingerprint" in roles:
        terms.extend(task.requirement_ids)
    if "design_ref" in roles:
        for ref in refs:
            _path, separator, fragment = ref.partition("#")
            if separator and fragment.strip():
                terms.append(fragment.strip())
    return tuple(dict.fromkeys(term for term in terms if term.strip()))


def markdown_sections_matching_terms(
    text: str,
    terms: tuple[str, ...],
) -> list[tuple[str, str]]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$", line)
        if match is None:
            continue
        headings.append((index, len(match.group("marks")), match.group("title")))
    matches: list[tuple[str, str]] = []
    seen_starts: set[int] = set()
    for heading_index, (line_index, level, title) in enumerate(headings):
        term = first_contained_term(title, terms)
        if term is None or line_index in seen_starts:
            continue
        end = len(lines)
        for next_line, next_level, _next_title in headings[heading_index + 1 :]:
            if next_level <= level:
                end = next_line
                break
        seen_starts.add(line_index)
        matches.append((term, "\n".join(lines[line_index:end]).strip()))
    return matches


def line_context_matching_terms(
    text: str,
    terms: tuple[str, ...],
) -> tuple[tuple[str, ...], str] | None:
    lines = text.splitlines()
    ranges: list[tuple[int, int]] = []
    matched_terms: list[str] = []
    for index, line in enumerate(lines):
        term = first_contained_term(line, terms)
        if term is None:
            continue
        matched_terms.append(term)
        ranges.append(
            (
                max(0, index - SPEC_WORKER_CONTEXT_LINE_CONTEXT),
                min(len(lines), index + SPEC_WORKER_CONTEXT_LINE_CONTEXT + 1),
            )
        )
    if not ranges:
        return None
    merged = merge_line_ranges(ranges)
    chunks = ["\n".join(lines[start:end]).strip() for start, end in merged]
    return tuple(dict.fromkeys(matched_terms)), "\n\n".join(chunks)


def first_contained_term(value: str, terms: tuple[str, ...]) -> str | None:
    folded = value.casefold()
    for term in terms:
        if term.casefold() in folded:
            return term
    return None


def merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def truncate_spec_context_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    marker = "\n...[truncated]\n"
    if max_chars <= len(marker):
        return text[:max_chars], True
    return f"{text[: max_chars - len(marker)]}{marker}", True


def skipped_spec_context_artifact(
    path: str,
    reason: str,
    detail: str = "",
) -> dict[str, str]:
    payload = {
        "path": safe_path_text_for_prompt(path),
        "reason": reason,
    }
    if detail:
        payload["detail"] = redact_manifest_text(detail)
    return payload


def trim_spec_worker_context_to_limit(
    context: dict[str, object],
) -> dict[str, object]:
    context_len = spec_worker_context_json_length(context)
    if context_len <= SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
        return context

    artifacts = context.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in reversed(artifacts):
            if not isinstance(artifact, dict):
                continue
            content = artifact.get("content")
            if not isinstance(content, str) or not content:
                continue
            while context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS and content:
                excess = context_len - SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS
                target_chars = max(0, len(content) - excess - 128)
                content, _truncated = truncate_spec_context_text(
                    content,
                    target_chars,
                )
                artifact["content"] = content
                artifact["truncated"] = True
                context_len = spec_worker_context_json_length(context)
            if context_len <= SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
                return context

    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
        context["artifacts"] = []
        skipped = context.setdefault("skipped_artifacts", [])
        if isinstance(skipped, list):
            skipped.append(
                skipped_spec_context_artifact(
                    ".",
                    "context_size_limit",
                    "artifacts omitted because metadata used the context budget",
                )
            )
        context_len = spec_worker_context_json_length(context)
    skipped = context.get("skipped_artifacts")
    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS and isinstance(
        skipped,
        list,
    ):
        original_count = len(skipped)
        while skipped and context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
            skipped.pop()
            context_len = spec_worker_context_json_length(context)
        omitted = original_count - len(skipped)
        if omitted > 0:
            skipped.append(
                {
                    "path": ".",
                    "reason": "skipped_artifact_diagnostics_limit",
                    "detail": f"{omitted} skipped artifact diagnostics omitted",
                }
            )
        while (
            skipped
            and spec_worker_context_json_length(context)
            > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS
        ):
            skipped.pop()
    context_len = spec_worker_context_json_length(context)
    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
        trim_spec_context_strings(context)
        context_len = spec_worker_context_json_length(context)
    task = context.get("task")
    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS and isinstance(task, dict):
        for key in (
            "source_fingerprints",
            "design_refs",
            "spec_paths",
            "resources",
            "paths",
            "requirement_ids",
            "scope",
            "source",
        ):
            if key in task:
                task.pop(key)
                context_len = spec_worker_context_json_length(context)
                if context_len <= SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
                    break
    gates = context.get("required_verification_gates")
    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS and isinstance(gates, list):
        context["required_verification_gates"] = [
            {"id": str(gate.get("id", "")), "required": True}
            for gate in gates
            if isinstance(gate, dict)
        ]
        context_len = spec_worker_context_json_length(context)
    if context_len > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS:
        task = context.get("task")
        task_id = ""
        if isinstance(task, dict):
            raw_task_id = task.get("id")
            if isinstance(raw_task_id, str):
                task_id = bounded_context_text(raw_task_id, 128)
        context.clear()
        context.update(
            {
                "schema_version": SPEC_WORKER_CONTEXT_SCHEMA_VERSION,
                "task": {
                    "id": task_id,
                    "metadata_omitted": "context_size_limit",
                },
                "required_verification_gates": [
                    {
                        "id": "task.acceptance",
                        "required": True,
                        "omitted": "context_size_limit",
                    }
                ],
                "limits": {
                    "max_total_chars": SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS,
                    "max_artifact_chars": SPEC_WORKER_CONTEXT_MAX_ARTIFACT_CHARS,
                    "max_file_bytes": SPEC_WORKER_CONTEXT_MAX_FILE_BYTES,
                    "max_artifacts": SPEC_WORKER_CONTEXT_MAX_ARTIFACTS,
                },
                "artifacts": [],
                "skipped_artifacts": [
                    skipped_spec_context_artifact(
                        ".",
                        "context_size_limit",
                        "metadata omitted because it exceeded the context budget",
                    )
                ],
            }
        )
    return context


def spec_worker_context_json_length(context: dict[str, object]) -> int:
    return len(json.dumps(context, indent=2, sort_keys=True))


def trim_spec_context_strings(context: dict[str, object]) -> None:
    targets: list[dict[str, object]] = []
    task = context.get("task")
    if isinstance(task, dict):
        targets.append(task)
    gates = context.get("required_verification_gates")
    if isinstance(gates, list):
        targets.extend(gate for gate in gates if isinstance(gate, dict))
    for mapping in reversed(targets):
        for key in ("command", "evidence", "acceptance", "scope", "source", "title"):
            value = mapping.get(key)
            if not isinstance(value, str) or not value:
                continue
            while (
                spec_worker_context_json_length(context)
                > SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS
                and value
            ):
                excess = (
                    spec_worker_context_json_length(context)
                    - SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS
                )
                target_chars = max(0, len(value) - excess - 128)
                value, _truncated = truncate_spec_context_text(value, target_chars)
                mapping[key] = value
            if (
                spec_worker_context_json_length(context)
                <= SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS
            ):
                return


def required_worker_verification_gates(
    config: VibeConfig,
    task: Task,
) -> list[dict[str, object]]:
    gates: list[dict[str, object]] = [
        {
            "id": "task.acceptance",
            "required": True,
            "description": "Verify the task acceptance criteria before reporting completion.",
            "acceptance": bounded_context_text(task.acceptance),
        }
    ]
    if task.evidence:
        gates.append(
            {
                "id": "task.evidence",
                "required": True,
                "description": "Use the task evidence field to choose concrete checks and artifacts.",
                "evidence": bounded_context_text(task.evidence),
            }
        )
    if config.specs.require_approved:
        gates.append(
            {
                "id": "spec.require_approved",
                "required": True,
                "approved_states": list(config.specs.approved_states),
            }
        )
    if config.specs.require_current_fingerprints:
        gates.append({"id": "spec.require_current_fingerprints", "required": True})
    if config.specs.require_requirement_coverage:
        gates.append({"id": "spec.require_requirement_coverage", "required": True})
    if config.specs.require_completion_evidence:
        gates.append({"id": "spec.require_completion_evidence", "required": True})
    for command in config.completion.commands[:SPEC_WORKER_CONTEXT_MAX_LIST_ITEMS]:
        gates.append(
            {
                "id": "completion.command",
                "required": True,
                "command": bounded_context_text(
                    command,
                    SPEC_WORKER_CONTEXT_MAX_REF_CHARS,
                ),
            }
        )
    omitted_commands = (
        len(config.completion.commands) - SPEC_WORKER_CONTEXT_MAX_LIST_ITEMS
    )
    if omitted_commands > 0:
        gates.append(
            {
                "id": "completion.command",
                "required": True,
                "omitted": omitted_commands,
            }
        )
    return gates


def selection_worker_json(worker: WorkerView) -> dict[str, object]:
    payload = worker.to_json()
    return {
        "task_id": payload["task_id"],
        "run_id": payload["run_id"],
        "state": payload["state"],
        "process_state": payload["process_state"],
        "stale_reason": payload["stale_reason"],
        "lifecycle_state": payload["lifecycle_state"],
        "result_status": payload["result_status"],
        "started_at": payload["started_at"],
        "log": payload["log"],
        "resources": payload["resources"],
        "paths": payload["paths"],
        "conflict_domains_known": payload["conflict_domains_known"],
        "workspace": payload["workspace"],
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
    if fcntl is not None:
        ensure_scheduler_lock_byte(handle)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is not None:
        try:
            ensure_scheduler_lock_byte(handle)
            handle.seek(0)
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


def _selection_retry_callback(attempt: int, delay: float, reason: str) -> None:
    report_status(f"agent selection retry {attempt} after {delay:.1f}s: {reason}")


LOG_TAIL_LINES_FOR_TRANSIENT_CHECK = 50


def is_transient_worker_failure(
    result: RunResult,
    log_tail_lines: int = LOG_TAIL_LINES_FOR_TRANSIENT_CHECK,
) -> bool:
    if result.exit_code == 0:
        return False
    if result.classification == "completed":
        return False
    worker_report = result.worker_report
    if isinstance(worker_report, dict):
        status = worker_report.get("status")
        if status in {"completed", "blocked"}:
            return False
    log_path = result.log_path
    if not isinstance(log_path, Path) or not log_path.exists():
        return False
    try:
        tail = _read_log_tail(log_path, log_tail_lines)
    except OSError:
        return False
    return is_transient_stderr(tail)


def _read_log_tail(path: Path, max_lines: int) -> str:
    from collections import deque

    tail: deque[str] = deque(maxlen=max_lines)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            tail.append(line)
    return "".join(tail)


def git_rev_parse(repo: Path, rev: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", rev],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
