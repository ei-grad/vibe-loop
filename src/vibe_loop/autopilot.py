from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time as time_module
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from vibe_loop.config import (
    AgentResolutionError,
    AUTOPILOT_MIN_INTERVAL_SECONDS,
    DISK_RESERVE_DEFAULT_HARD_STOP_FREE_BYTES,
    DISK_RESERVE_DEFAULT_MIN_FREE_INODE_FRACTION,
    DISK_RESERVE_DEFAULT_MIN_FREE_INODES,
    DISK_RESERVE_DEFAULT_WARN_FREE_BYTES,
    REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES,
    REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES,
    RUNTIME_CONTEXT_REDACTION,
    ResolvedProjectBinding,
    VibeConfig,
    command_template_uses_field,
    format_agent_command,
    git_main_worktree_path,
    structured_usage_observation,
    load_config,
    resolve_project_binding,
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
from vibe_loop.orchestration import (
    ConfigContractBlocker,
    config_contract_blockers,
    task_agent_dispatch_blocker,
)
from vibe_loop.processes import (
    ProcessNode,
    collect_owned_descendants,
    process_birth_identity,
    read_process_node,
    read_process_table,
)
from vibe_loop.retry import (
    is_provider_limit_classification,
    parse_provider_limit_reset_delay,
)
from vibe_loop.runtime_events import ACTIONABLE_RUNTIME_EVENT_KINDS
from vibe_loop.runner import (
    AgentProviderLimitError,
    AgentRuntimeContext,
    CandidateExclusion,
    ProviderUsageObserver,
    VibeRunner,
    inject_structured_usage_output,
    new_run_id,
    parse_agent_runtime_context_from_command,
    workspace_fingerprint_ignored_dirty_paths,
    workspace_preflight_deferral_is_unchanged,
)
from vibe_loop.runs import (
    AUTOPILOT_CHILD_STARTED_RECORD_TYPE,
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CONFIG_RELOAD_REQUESTED_RECORD_TYPE,
    AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_CYCLE_STARTED_RECORD_TYPE,
    AUTOPILOT_CYCLE_SUMMARY_RECORD_TYPE,
    AUTOPILOT_DISK_HEALTH_RECORD_TYPE,
    AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
    AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
    AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
    AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
    AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE,
    AUTOPILOT_TROUBLESHOOT_RECORD_TYPE,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    REVIEW_VERDICT_RECORD_TYPE,
    RUN_CONTRACT_RESOLVED_RECORD_TYPE,
    RUN_RECORD_TYPE,
    RUN_STARTED_RECORD_TYPE,
    RUN_SUPERVISOR_EXITED_RECORD_TYPE,
    RUN_SUPERVISOR_STARTED_RECORD_TYPE,
    TASK_ACTIVATION_FAILED_RECORD_TYPE,
    TASK_PROVENANCE_COMMITTED_RECORD_TYPE,
    TASK_RESTART_RECORD_TYPE,
    WORKSPACE_CLAIM_RECORD_TYPE,
    WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
    WORKSPACE_PREFLIGHT_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    autopilot_child_started_record,
    utc_now_iso,
)
from vibe_loop.tasks import (
    BLOCKED_FAMILY_STATUSES,
    WITHHELD_ADAPTER_ENV,
    Task,
    build_adapter_environment,
)
from vibe_loop.telemetry import (
    normalize_model_label,
    normalize_provider_label,
    merge_provider_usage,
    unavailable_usage,
)
from vibe_loop.upstream import check_upstream_sync
from vibe_loop.workers import (
    ActiveRunState,
    KEEP_EVIDENCE_CHANGED,
    ProcessExists,
    StaleLock,
    WorktreeDispositionDecision,
    WorktreeDispositionEvidence,
    WorktreeDispositionOutcome,
    WorkerView,
    TaskSourceSettlementRecovery,
    clean_stale_locks,
    collect_stale_locks,
    collect_worktree_disposition_evidence,
    execute_worktree_disposition,
    git_branch_delete,
    git_worktree_remove,
    pid_exists,
    record_expired_locks,
    restore_projected_worker_process_identity,
    worker_view_is_live,
    worktree_branch_delete_revalidation_guardrails,
)

RunUntilDoneLauncher = Callable[..., int]
Sleep = Callable[[float], None]


AUTOPILOT_RECORD_SCHEMA_VERSION = 1
TASK_SOURCE_HEALTH_COMMAND_KIND = "task_source_health"
AUTOPILOT_RUNTIME_CONTEXT_FD_ENV = "VIBE_LOOP_AUTOPILOT_RUNTIME_CONTEXT_FD"
AUTOPILOT_RUNTIME_CONTEXT_MAX_BYTES = (
    6 * REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES
    + 6 * REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES
    + 2
)
NON_CLOSURE_WINDOW_RUNS = 20
NON_CLOSURE_ALARM_THRESHOLD = 2
WORKTREE_DISPOSITION_STATUS_WORKTREE_LIMIT = 20
WORKTREE_DISPOSITION_STATUS_TEXT_LIMIT = 512
WORKTREE_DISPOSITION_STATUS_PATH_LIMIT = 4096
WORKTREE_DISPOSITION_STATUS_ACTION_ERROR_LIMIT = 4
WORKTREE_DISPOSITION_STATUS_GUARDRAIL_LIMIT = 16
ACTIVE_QUEUE_STATUSES = frozenset({"active"})
REVIEW_QUEUE_STATUSES = frozenset({"review"})
BLOCKED_QUEUE_STATUSES = BLOCKED_FAMILY_STATUSES


def require_autopilot_interval(interval: float) -> float:
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 0
        or 0 < interval < AUTOPILOT_MIN_INTERVAL_SECONDS
    ):
        raise ValueError(
            "autopilot interval must be zero for drain mode or at least "
            f"{AUTOPILOT_MIN_INTERVAL_SECONDS:.0f} seconds"
        )
    return float(interval)


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
    ready: int = 0
    runnable: int = 0
    active: int = 0
    done: int = 0
    blocked: int = 0
    statuses: dict[str, int] = dataclasses.field(default_factory=dict)
    runnable_tasks: tuple[dict[str, object], ...] = ()
    source_tasks: tuple[dict[str, object], ...] = ()
    gated_tasks: tuple[dict[str, object], ...] = ()
    dispatch_blockers: tuple[dict[str, object], ...] = ()
    source_error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "ready": self.ready,
            "runnable": self.runnable,
            "active": self.active,
            "done": self.done,
            "blocked": self.blocked,
            "statuses": dict(self.statuses),
            "runnable_tasks": [dict(task) for task in self.runnable_tasks],
            "gated_tasks": [dict(task) for task in self.gated_tasks],
            "dispatch_blockers": [dict(blocker) for blocker in self.dispatch_blockers],
            "source_error": self.source_error,
        }


@dataclasses.dataclass(frozen=True)
class StrandedReviewSnapshot:
    tasks: tuple[dict[str, object], ...] = ()
    cycle_id: str = ""
    occurred_at: str = ""
    available: bool = False


@dataclasses.dataclass(frozen=True)
class SupervisorStatus:
    state: str = "idle"
    dispatch_state: str = "idle"
    pid: int | None = None
    log: Path | None = None
    run_id: str = ""
    cycle_id: str = ""
    observed_at: str = ""
    record: dict[str, Any] | None = None
    blocker: str = ""
    config: dict[str, object] = dataclasses.field(default_factory=dict)
    advisories: tuple[dict[str, object], ...] = ()

    def to_json(self) -> dict[str, object]:
        record = dict(self.record or {})
        record.pop("config_key_fingerprints", None)
        payload = {
            "state": self.state,
            "dispatch_state": self.dispatch_state,
            "pid": self.pid,
            "log": str(self.log) if self.log is not None else "",
            "run_id": self.run_id,
            "cycle_id": self.cycle_id,
            "observed_at": self.observed_at,
            "record": record,
            "blocker": self.blocker,
            "config": dict(self.config),
            "advisories": [dict(advisory) for advisory in self.advisories],
        }
        redacted = redact_fencing_token_payload(payload)
        assert isinstance(redacted, dict)
        return redacted


NATIVE_PLANNING_PROVIDER_LIMIT_ACTION = "native_planning_provider_limit"
CHILD_PROVIDER_LIMIT_ACTION = "provider_limit_pause"
PLANNING_BACKOFF_ACTION = "planning_backoff"
PLANNING_OUTCOME_ACTION_PREFIX = "native_planning_outcome:"
DISPATCH_FLOOR_HOLD_ACTION = "dispatch_floor_hold"
PROVIDER_LIMIT_ACTION_PREFIXES = (
    f"{NATIVE_PLANNING_PROVIDER_LIMIT_ACTION}:",
    f"{CHILD_PROVIDER_LIMIT_ACTION}:",
)


@dataclasses.dataclass(frozen=True)
class CycleSummary:
    cycle_id: str
    status: str
    occurred_at: str
    actions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_wake: str = ""
    record: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def provider_limit_action(self) -> str:
        """The provider-limit action recorded for this cycle, if any.

        Derived from the recorded actions so a paused cycle stays
        distinguishable from a generic planning-analysis error, which shares
        the same ``idle`` cycle status.
        """
        for action in self.actions:
            if action.startswith(PROVIDER_LIMIT_ACTION_PREFIXES):
                return action
        return ""

    @property
    def planning_backoff_action(self) -> str:
        """The planning spend-backoff action recorded for this cycle, if any.

        Like the provider limit, a backed-off cycle keeps the plain ``idle`` status,
        so the reason has to be named explicitly or the cycle is
        indistinguishable from one that simply found nothing to do.
        """
        for action in self.actions:
            if action.startswith(f"{PLANNING_BACKOFF_ACTION}:"):
                return action
        return ""

    @property
    def dispatch_floor_action(self) -> str:
        """The explicit dispatch-floor hold recorded for this cycle, if any."""
        for action in self.actions:
            if action.startswith(f"{DISPATCH_FLOOR_HOLD_ACTION}:"):
                return action
        return ""

    @property
    def planning_outcome(self) -> str:
        for action in self.actions:
            if action.startswith(PLANNING_OUTCOME_ACTION_PREFIX):
                return action[len(PLANNING_OUTCOME_ACTION_PREFIX) :]
        return ""

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
class NonClosureSummary:
    window_runs: int = NON_CLOSURE_WINDOW_RUNS
    observed_runs: int = 0
    approved_candidates: int = 0
    count: int = 0
    consecutive: int = 0
    alarm_threshold: int = NON_CLOSURE_ALARM_THRESHOLD
    reasons: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def rate(self) -> float | None:
        if not self.approved_candidates:
            return None
        return self.count / self.approved_candidates

    @property
    def alarmed(self) -> bool:
        return self.consecutive >= self.alarm_threshold

    def to_json(self) -> dict[str, object]:
        return {
            "window_runs": self.window_runs,
            "observed_runs": self.observed_runs,
            "approved_candidates": self.approved_candidates,
            "count": self.count,
            "rate": self.rate,
            "consecutive": self.consecutive,
            "alarm_threshold": self.alarm_threshold,
            "alarmed": self.alarmed,
            "reasons": dict(self.reasons),
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
    disk_headroom: dict[str, object] = dataclasses.field(default_factory=dict)
    worktree_disposition_policy: str = "report-only"
    worktree_disposition: dict[str, object] = dataclasses.field(default_factory=dict)
    workspace_diagnostics: tuple[dict[str, object], ...] = ()
    supervisor: SupervisorStatus = dataclasses.field(default_factory=SupervisorStatus)
    blockers: tuple[str, ...] = ()
    advisories: tuple[dict[str, object], ...] = ()
    observations: tuple[str, ...] = ()
    stranded_review_tasks: tuple[dict[str, object], ...] = ()
    last_cycle: CycleSummary | None = None
    non_closure: NonClosureSummary = dataclasses.field(
        default_factory=NonClosureSummary
    )
    next_wake: str = ""
    attempt_circuit_breakers: tuple[dict[str, object], ...] = ()
    runtime_context: tuple[tuple[str, str], ...] = ()
    project_binding: ResolvedProjectBinding = dataclasses.field(
        default_factory=ResolvedProjectBinding
    )
    config_contract_blockers: tuple[ConfigContractBlocker, ...] = ()

    @property
    def alarms(self) -> tuple[str, ...]:
        if not self.non_closure.alarmed:
            return ()
        return (non_closure_alarm(self.non_closure),)

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
            "disk_headroom": self.disk_headroom,
            "worktree_disposition_policy": self.worktree_disposition_policy,
            "worktree_disposition": self.worktree_disposition,
            "workspace_diagnostics": [
                dict(diagnostic) for diagnostic in self.workspace_diagnostics
            ],
            "supervisor": self.supervisor.to_json(),
            "blockers": list(self.blockers),
            "alarms": list(self.alarms),
            "advisories": [dict(advisory) for advisory in self.advisories],
            "observations": list(self.observations),
            "stranded_review_tasks": [
                dict(task) for task in self.stranded_review_tasks
            ],
            "last_cycle": (
                self.last_cycle.to_json() if self.last_cycle is not None else None
            ),
            "non_closure": self.non_closure.to_json(),
            "next_wake": self.next_wake,
            "attempt_circuit_breakers": [
                dict(breaker) for breaker in self.attempt_circuit_breakers
            ],
            "config_contract_blockers": [
                blocker.to_json() for blocker in self.config_contract_blockers
            ],
        }
        redacted = redact_runtime_context_payload(payload, self.runtime_context)
        assert isinstance(redacted, dict)
        fencing_redacted = redact_fencing_token_payload(redacted)
        assert isinstance(fencing_redacted, dict)
        # Attached after redaction on purpose: declared binding names are
        # validated as namespace selectors, so their resolved values are the
        # routing fact operators need and are not secret-shaped. Anything else
        # is redacted by ResolvedBindingEntry.to_json itself.
        fencing_redacted["project_binding"] = self.project_binding.to_json()
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
    provider_limit_pause_seconds: float | None = None
    planning_backoff_seconds: float | None = None
    autopilot_run_id: str = ""
    dispatched_runs: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
            "cycle_id": self.cycle_id,
            "run_id": self.autopilot_run_id,
            "repo": str(self.repo),
            "status": self.status,
            "occurred_at": self.occurred_at,
            "queue": self.project_status.queue.to_json(),
            "workers": [worker.to_json() for worker in self.project_status.workers],
            "stranded_review_tasks": [
                dict(task) for task in self.project_status.stranded_review_tasks
            ],
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
            "provider_limit_pause_seconds": self.provider_limit_pause_seconds,
            "planning_backoff_seconds": self.planning_backoff_seconds,
            "dispatched_runs": self.dispatched_runs,
        }

    def append_to(self, run_store: RunStore) -> None:
        run_store.append_record(self.to_json())


def collect_project_status(
    config: VibeConfig,
    *,
    process_exists: ProcessExists | None = None,
    disk_health_result: DiskHealthCycleResult | None = None,
) -> ProjectStatus:
    project_binding = resolve_project_binding(config)
    contract_blockers = config_contract_blockers(config)
    run_store = RunStore(config.state_path / "runs.jsonl")
    non_closure = summarize_non_closures(run_store)
    troubleshoot = latest_native_troubleshoot(run_store)
    if disk_health_result is None:
        disk_health_result = run_disk_health(config, cycle_id="status")
    disk_headroom = disk_health_result.to_status_json()
    if project_binding.blocker is not None:
        # Querying the task source or lock adapter now would route this
        # repository's status through whatever project the ambient
        # environment names. Report the binding failure instead; git state is
        # local and stays safe to collect.
        return ProjectStatus(
            repo=config.repo,
            display_name=config.repo.name,
            state_dir=config.state_path,
            collected_at=utc_now_iso(),
            main_branch=config.main_branch,
            git=collect_git_status(
                config.repo,
                config.main_branch,
                ignored_dirty_paths=(config.state_path,),
            ),
            queue=TaskQueueStatus(source_error=project_binding.blocker),
            agent=config.agent.to_json(),
            disk_headroom=disk_headroom,
            worktree_disposition_policy=config.autopilot.worktree_disposition,
            worktree_disposition=latest_worktree_disposition(run_store),
            # Supervisor liveness is only observable through the lock adapter,
            # which is exactly what must not be queried here. Report it as
            # unknown rather than letting the "idle" default read as a checked
            # fact; the run journal below is local and stays accurate.
            supervisor=SupervisorStatus(
                state="unknown",
                dispatch_state="blocked",
                blocker=project_binding.blocker,
            ),
            last_cycle=latest_cycle_summary(run_store),
            non_closure=non_closure,
            blockers=(
                *(item.code for item in project_binding.diagnostics),
                *(item.code for item in contract_blockers),
                *((disk_health_result.blocker,) if disk_health_result.blocker else ()),
                *troubleshoot.blockers,
            ),
            observations=troubleshoot.observations,
            runtime_context=config.runtime_context,
            project_binding=project_binding,
            config_contract_blockers=contract_blockers,
        )
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
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
    live_active_runs = tuple(
        worker.active for worker in workers if worker_holds_active_conflict(worker)
    )
    queue_status = collect_task_queue_status(
        config,
        active_runs=live_active_runs,
        locked_task_ids=frozenset(worker.active.task_id for worker in workers),
    )
    records = run_store.read_records()
    preflight_blockers = workspace_preflight_dispatch_blockers(
        queue_status,
        workers,
        records,
        repo=config.repo,
        main_branch=config.main_branch,
    )
    if preflight_blockers:
        queue_status = apply_dispatch_blockers(
            queue_status,
            preflight_blockers,
        )
    stranded_reviews = stranded_review_tasks(
        queue_status,
        workers,
        records,
        runnable_statuses=config.task_source.runnable_statuses,
    )
    agent = config.agent.to_json()
    agent_blockers = agent_blocking_diagnostics(config)
    last_cycle = latest_cycle_summary(run_store)
    attempt_circuit_breakers = tuple(
        breaker.to_json()
        for breaker in run_store.attempt_circuit_states(
            threshold=config.supervision.cross_run_attempt_threshold
        )
    )
    supervisor_lock = lock_manager.autopilot_status(process_exists=process_exists)
    supervisor = collect_supervisor_status(
        run_store,
        supervisor_lock=supervisor_lock,
        process_exists=process_exists,
        current_config=config,
    )
    supervisor = dataclasses.replace(
        supervisor,
        dispatch_state=(
            "blocked"
            if contract_blockers
            or disk_health_result.blocker
            or troubleshoot.blockers
            or queue_status.source_error
            or queue_has_no_launchable_task(queue_status)
            else "idle"
            if queue_status.runnable == 0
            else "available"
        ),
    )
    workspace_diagnostics = tuple(
        diagnostic.to_json()
        for worker in workers
        for diagnostic in worker.workspace_diagnostics
    )
    blockers = project_blockers(
        project_binding=project_binding,
        git_status=git_status,
        queue_status=queue_status,
        stale_locks=stale_locks,
        workspace_diagnostics=workspace_diagnostics,
        integration_lock=integration_lock,
        agent_diagnostics=agent_blockers,
        supervisor=supervisor,
        config_contract_blockers=contract_blockers,
    )
    blockers.extend(
        f"stranded_review_task:{task['task_id']}" for task in stranded_reviews
    )
    if disk_health_result.blocker:
        blockers.append(disk_health_result.blocker)
    blockers.extend(troubleshoot.blockers)
    if config.autopilot.require_upstream_sync:
        upstream = check_upstream_sync(
            config.repo,
            config.main_branch,
            required=True,
            refresh=False,
        )
        assert upstream.blocker is not None
        blockers.append(f"upstream_sync:{upstream.blocker.code}")
        blockers.extend(
            active_unlocked_task_blockers(
                queue_status,
                workers,
                run_store.read_records(),
            )
        )
    observations = tuple(
        dict.fromkeys(
            (
                *project_observations(queue_status=queue_status, workers=workers),
                *troubleshoot.observations,
            )
        )
    )
    next_wake = (
        last_cycle.next_wake
        if supervisor.state == "sleeping"
        and last_cycle is not None
        and last_cycle.cycle_id == supervisor.cycle_id
        and str(last_cycle.record.get("run_id") or "") == supervisor.run_id
        else ""
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
        disk_headroom=disk_headroom,
        worktree_disposition_policy=config.autopilot.worktree_disposition,
        worktree_disposition=latest_worktree_disposition(run_store),
        workspace_diagnostics=workspace_diagnostics,
        supervisor=supervisor,
        blockers=tuple(blockers),
        advisories=supervisor.advisories,
        observations=observations,
        stranded_review_tasks=stranded_reviews,
        last_cycle=last_cycle,
        non_closure=non_closure,
        next_wake=next_wake,
        attempt_circuit_breakers=attempt_circuit_breakers,
        runtime_context=config.runtime_context,
        project_binding=project_binding,
        config_contract_blockers=contract_blockers,
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
    locked_task_ids: frozenset[str] | None = None,
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
        candidate_snapshot = runner.candidate_snapshot_from_tasks(
            tasks,
            active_runs=active_runs,
            locked_task_ids=locked_task_ids,
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
    candidate_blockers = tuple(
        candidate_exclusion_dispatch_blocker(exclusion)
        for exclusion in candidate_snapshot.exclusions
    )
    admission_blockers = tuple(
        {
            "task_id": task.task_id,
            "agent": task.agent,
            "mechanism": "admission",
            **blocker.to_json(),
        }
        for task in candidate_snapshot.runnable
        if (blocker := task_agent_dispatch_blocker(effective_config, task)) is not None
    )
    admission_blocked_ids = {str(blocker["task_id"]) for blocker in admission_blockers}
    runnable = tuple(
        task
        for task in candidate_snapshot.runnable
        if task.task_id not in admission_blocked_ids
    )
    return TaskQueueStatus(
        total=len(tasks),
        ready=len(candidate_snapshot.ready),
        runnable=len(runnable),
        active=count_statuses(statuses, ACTIVE_QUEUE_STATUSES),
        done=sum(1 for task in tasks if task.done),
        blocked=count_statuses(statuses, BLOCKED_QUEUE_STATUSES),
        statuses=statuses,
        runnable_tasks=tuple(task_summary(task) for task in runnable),
        source_tasks=tuple(task.to_json() for task in tasks),
        gated_tasks=tuple(
            {
                **task_summary(task),
                "reason": task.status_reason
                or "task source reported status 'gated'; the task is excluded "
                "from the runnable queue",
            }
            for task in tasks
            if task.status.casefold() == "gated"
        ),
        dispatch_blockers=(*candidate_blockers, *admission_blockers),
    )


def candidate_exclusion_dispatch_blocker(
    exclusion: CandidateExclusion,
) -> dict[str, object]:
    task_id = exclusion.task.task_id
    if exclusion.mechanism == "dependency":
        dependencies = ", ".join(exclusion.details)
        return {
            "task_id": task_id,
            "mechanism": "dependency",
            "code": "task_dependency_blocked",
            "key": "task.dependencies",
            "message": f"unmet dependencies: {dependencies}",
            "remedy": "Complete the named dependencies.",
        }
    if exclusion.mechanism == "lock":
        return {
            "task_id": task_id,
            "mechanism": "lock",
            "code": "task_lock_held",
            "key": "task.lock",
            "message": "an existing task lock prevents dispatch",
            "remedy": "Wait for the lock holder to finish or recover a stale lock.",
        }
    if exclusion.mechanism == "domain":
        return {
            "task_id": task_id,
            "mechanism": "domain",
            "code": "task_conflict_domain_held",
            "key": "task.conflict_domains",
            "message": "a conflict domain is held by an active run",
            "remedy": "Wait for the conflicting active run to finish.",
        }
    raise ValueError(f"unknown candidate exclusion mechanism: {exclusion.mechanism}")


def apply_dispatch_blockers(
    queue_status: TaskQueueStatus,
    blockers: tuple[dict[str, object], ...],
) -> TaskQueueStatus:
    blocked_task_ids = {str(blocker.get("task_id") or "") for blocker in blockers}
    runnable_tasks = tuple(
        task
        for task in queue_status.runnable_tasks
        if str(task.get("id") or task.get("task_id") or "") not in blocked_task_ids
    )
    return dataclasses.replace(
        queue_status,
        runnable=len(runnable_tasks),
        runnable_tasks=runnable_tasks,
        dispatch_blockers=(*queue_status.dispatch_blockers, *blockers),
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


def queue_has_no_launchable_task(queue_status: TaskQueueStatus) -> bool:
    operational_blockers = tuple(
        blocker
        for blocker in queue_status.dispatch_blockers
        if blocker.get("mechanism") not in {"dependency", "lock", "domain"}
    )
    return queue_status.runnable == 0 and bool(
        operational_blockers or (queue_status.gated_tasks and queue_status.ready == 0)
    )


def workspace_preflight_dispatch_blockers(
    queue_status: TaskQueueStatus,
    workers: tuple[WorkerView, ...],
    records: Sequence[Mapping[str, object]],
    *,
    repo: Path,
    main_branch: str,
) -> tuple[dict[str, object], ...]:
    runnable_task_ids = {
        str(task.get("id") or task.get("task_id") or "")
        for task in queue_status.runnable_tasks
    }
    locked_task_ids = {worker.active.task_id for worker in workers}
    already_blocked = {
        str(blocker.get("task_id") or "") for blocker in queue_status.dispatch_blockers
    }
    latest: dict[str, Mapping[str, object]] = {}
    for record in records:
        if record.get("record_type") != WORKSPACE_PREFLIGHT_RECORD_TYPE:
            continue
        task_id = str(record.get("task_id") or "")
        if task_id:
            latest[task_id] = record

    blockers: list[dict[str, object]] = []
    for task_id in sorted(runnable_task_ids):
        if task_id in locked_task_ids or task_id in already_blocked:
            continue
        record = latest.get(task_id)
        if record is None or not workspace_preflight_deferral_is_unchanged(
            record,
            repo=repo,
            main_branch=main_branch,
            ignored_dirty_paths=workspace_fingerprint_ignored_dirty_paths(),
        ):
            continue
        reason = str(record.get("reason") or "unknown")
        retry_disposition = str(record.get("retry_disposition") or "retry_later")
        blocker: dict[str, object] = {
            "task_id": task_id,
            "mechanism": "admission",
            "code": "workspace_preflight_rejected",
            "key": "workspace",
            "message": (
                "workspace preflight rejected worker launch: "
                f"{reason} ({retry_disposition})"
            ),
            "remedy": (
                "Repair or remove the recorded branch/worktree; the supervisor "
                "re-evaluates their state each cycle."
            ),
            "reason": reason,
            "retry_disposition": retry_disposition,
            "run_id": str(record.get("run_id") or ""),
        }
        for key in ("branch", "worktree"):
            value = record.get(key)
            if isinstance(value, str) and value:
                blocker[key] = value
        blockers.append(blocker)
    return tuple(blockers)


def stranded_review_tasks(
    queue_status: TaskQueueStatus,
    workers: Sequence[WorkerView],
    records: Sequence[Mapping[str, object]],
    *,
    runnable_statuses: Sequence[str],
) -> tuple[dict[str, object], ...]:
    live_task_ids = {
        worker.active.task_id for worker in workers if worker_view_is_live(worker)
    }
    latest_run_ids: dict[str, str] = {}
    findings: dict[tuple[str, str, str], dict[str, object]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "")
        run_id = str(record.get("run_id") or "")
        if not task_id or not run_id:
            continue
        if record.get("record_type") in {
            RUN_STARTED_RECORD_TYPE,
            RUN_RECORD_TYPE,
            REVIEW_VERDICT_RECORD_TYPE,
        }:
            latest_run_ids[task_id] = run_id
        if record.get("record_type") != "finding_recorded":
            continue
        finding_id = str(record.get("finding_id") or "")
        if finding_id:
            findings[(task_id, run_id, finding_id)] = {
                "id": finding_id,
                "severity": str(record.get("severity") or ""),
                "summary": str(record.get("summary") or ""),
                "state": str(record.get("state") or ""),
            }

    stranded: list[dict[str, object]] = []
    for task in queue_status.source_tasks:
        task_id = str(task.get("id") or task.get("task_id") or "")
        status = str(task.get("status") or "")
        if (
            not task_id
            or status.casefold() not in REVIEW_QUEUE_STATUSES
            or status in runnable_statuses
            or task_id in live_task_ids
        ):
            continue
        run_id = latest_run_ids.get(task_id, "")
        unresolved = [
            finding
            for (
                finding_task_id,
                finding_run_id,
                _finding_id,
            ), finding in findings.items()
            if finding_task_id == task_id
            and (not run_id or finding_run_id == run_id)
            and finding["state"] == "open"
        ]
        stranded.append(
            {
                "task_id": task_id,
                "title": str(task.get("title") or ""),
                "status": status,
                "run_id": run_id,
                "reason": "no_live_worker_or_reviewer",
                "unresolved_findings": unresolved,
            }
        )
    return tuple(stranded)


def latest_stranded_review_snapshot(
    run_store: RunStore,
) -> StrandedReviewSnapshot:
    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_CYCLE_RECORD_TYPE:
            continue
        raw_tasks = record.get("stranded_review_tasks")
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        return StrandedReviewSnapshot(
            tasks=tuple(dict(task) for task in raw_tasks if isinstance(task, Mapping)),
            cycle_id=str(record.get("cycle_id") or ""),
            occurred_at=str(record.get("occurred_at") or ""),
            available=True,
        )
    return StrandedReviewSnapshot()


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
    current_config: VibeConfig | None = None,
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

    def live_phase(run_id: str) -> dict[str, Any] | None:
        for record in reversed(records):
            record_type = record.get("record_type")
            if record_type not in {
                AUTOPILOT_CYCLE_STARTED_RECORD_TYPE,
                AUTOPILOT_CYCLE_RECORD_TYPE,
            }:
                continue
            if str(record.get("run_id") or "") == run_id:
                return record
        return None

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
            config_record = next(
                (
                    record
                    for record in reversed(matching_records)
                    if record.get("config_loaded_at")
                ),
                None,
            )
            log = next(
                (
                    path
                    for record in reversed(matching_records)
                    if (path := path_value(record.get("log"))) is not None
                ),
                None,
            )
            phase_record = live_phase(lock_run_id)
            state = "running" if supervisor_lock.state == "held" else "observed"
            if supervisor_lock.state == "held" and phase_record is not None:
                if (
                    phase_record.get("record_type")
                    == AUTOPILOT_CYCLE_STARTED_RECORD_TYPE
                ):
                    state = "active_cycle"
                elif phase_record.get("next_wake"):
                    state = "sleeping"
            applied_record = next(
                (
                    record
                    for record in reversed(records)
                    if record.get("record_type") == AUTOPILOT_CYCLE_STARTED_RECORD_TYPE
                    and str(record.get("run_id") or "") == lock_run_id
                    and record.get("config_reload_status") == "loaded"
                ),
                None,
            )
            config_report, advisories = supervisor_config_staleness(
                config_record,
                applied_record=applied_record,
                current_config=current_config,
                running=supervisor_lock.state == "held",
            )
            last_reload = latest_autopilot_reload_result(
                records,
                run_id=lock_run_id,
            )
            if last_reload:
                config_report["last_reload"] = last_reload
            return SupervisorStatus(
                state=state,
                pid=lock_pid,
                log=log,
                run_id=lock_run_id,
                cycle_id=(
                    str(phase_record.get("cycle_id") or "")
                    if phase_record is not None
                    else str(newest_record.get("cycle_id") or "")
                    if newest_record is not None
                    else ""
                ),
                observed_at=(
                    str(phase_record.get("occurred_at") or "")
                    if phase_record is not None
                    else str(newest_record.get("occurred_at") or "")
                    if newest_record is not None
                    else str(supervisor_lock.metadata.get("heartbeat_at") or "")
                ),
                record=(newest_record or supervisor_lock.metadata),
                config=config_report,
                advisories=advisories,
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


def supervisor_config_staleness(
    record: Mapping[str, Any] | None,
    *,
    applied_record: Mapping[str, Any] | None = None,
    current_config: VibeConfig | None,
    running: bool,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    if record is None or current_config is None:
        return {}, ()
    loaded_fingerprint = str(record.get("config_fingerprint") or "")
    loaded_at = str(record.get("config_loaded_at") or "")
    if not loaded_fingerprint or not loaded_at:
        return {}, ()
    current_fingerprint = config_snapshot_fingerprint(current_config)
    loaded_keys = {
        str(key): str(value)
        for key, value in dict(record.get("config_key_fingerprints") or {}).items()
    }
    applied_keys = (
        {
            str(key): str(value)
            for key, value in dict(
                applied_record.get("config_key_fingerprints") or {}
            ).items()
        }
        if applied_record is not None
        else loaded_keys
    )
    current_keys = dict(current_config.config_key_fingerprints)
    changed_keys = tuple(
        sorted(
            key
            for key in loaded_keys.keys() | applied_keys.keys() | current_keys.keys()
            if (
                applied_keys.get(key)
                if config_key_lifetime(key) == "per_cycle"
                else loaded_keys.get(key)
            )
            != current_keys.get(key)
        )
    )
    stale = running and bool(changed_keys)
    per_cycle_keys = tuple(
        key for key in changed_keys if config_key_lifetime(key) == "per_cycle"
    )
    restart_required_keys = tuple(
        key for key in changed_keys if config_key_lifetime(key) == "supervisor_start"
    )
    report: dict[str, object] = {
        "loaded_fingerprint": loaded_fingerprint,
        "loaded_at": loaded_at,
        "per_cycle_fingerprint": (
            str(applied_record.get("config_fingerprint") or "")
            if applied_record is not None
            else loaded_fingerprint
        ),
        "per_cycle_loaded_at": (
            str(applied_record.get("config_loaded_at") or "")
            if applied_record is not None
            else loaded_at
        ),
        "current_fingerprint": current_fingerprint,
        "current_path": (
            str(current_config.config_path) if current_config.config_path else ""
        ),
        "stale": stale,
        "changed_keys": list(changed_keys) if stale else [],
        "per_cycle_keys": list(per_cycle_keys) if stale else [],
        "restart_required_keys": list(restart_required_keys) if stale else [],
    }
    if not stale:
        return report, ()
    if per_cycle_keys and restart_required_keys:
        message = (
            "the running supervisor uses a different configuration; per-cycle "
            "keys apply on the next cycle, while supervisor-start keys require "
            "a restart"
        )
    elif per_cycle_keys:
        message = (
            "the running supervisor uses a different configuration; the changed "
            "per-cycle keys apply on the next cycle"
        )
    else:
        message = (
            "the running supervisor uses a different configuration; the changed "
            "supervisor-start keys require a restart"
        )
    advisory = {
        "code": "supervisor_config_stale",
        "severity": "warning",
        "changed_keys": list(changed_keys),
        "per_cycle_keys": list(per_cycle_keys),
        "restart_required_keys": list(restart_required_keys),
        "message": message,
    }
    return report, (advisory,)


def config_snapshot_fingerprint(config: VibeConfig) -> str:
    if config.config_digest:
        return config.config_digest
    encoded = json.dumps(
        config.config_key_fingerprints,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def config_key_lifetime(key: str) -> str:
    if key == "agent" or key.startswith("agent."):
        return "per_cycle"
    if key == "autopilot.jobs":
        return "per_cycle"
    return "supervisor_start"


def config_key_reload_safe(key: str, *, reload_config_jobs: bool) -> bool:
    if config_key_lifetime(key) != "per_cycle":
        return False
    return key != "autopilot.jobs" or reload_config_jobs


def changed_config_keys(
    previous: Mapping[str, str] | Sequence[tuple[str, str]],
    current: Mapping[str, str] | Sequence[tuple[str, str]],
) -> tuple[str, ...]:
    previous_keys = dict(previous)
    current_keys = dict(current)
    return tuple(
        sorted(
            key
            for key in previous_keys.keys() | current_keys.keys()
            if previous_keys.get(key) != current_keys.get(key)
        )
    )


def latest_autopilot_reload_result(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, object]:
    for record in reversed(records):
        if (
            record.get("record_type") == AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE
            and str(record.get("run_id") or "") == run_id
        ):
            return {
                "state": str(record.get("state") or ""),
                "loaded_at": str(record.get("loaded_at") or ""),
                "fingerprint": str(record.get("config_fingerprint") or ""),
                "changed_keys": [
                    str(key) for key in record.get("changed_keys", []) if str(key)
                ],
                "blocker": str(record.get("blocker") or ""),
            }
    return {}


def pending_autopilot_reload_requests(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> tuple[Mapping[str, Any], ...]:
    completed = {
        str(record.get("request_id") or "")
        for record in records
        if record.get("record_type") == AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE
        and str(record.get("run_id") or "") == run_id
    }
    return tuple(
        record
        for record in records
        if (
            record.get("record_type") == AUTOPILOT_CONFIG_RELOAD_REQUESTED_RECORD_TYPE
            and str(record.get("run_id") or "") == run_id
            and str(record.get("request_id") or "") not in completed
        )
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


def latest_worktree_disposition(run_store: RunStore) -> dict[str, object]:
    records = run_store.recent_records_matching(
        record_types=frozenset({AUTOPILOT_WORKTREE_REAP_RECORD_TYPE}),
        max_runs=1,
    )
    if not records:
        return {}
    return project_worktree_disposition_status(records[-1])


def project_worktree_disposition_status(
    record: Mapping[str, object],
) -> dict[str, object]:
    raw_evidence = record.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    raw_outcomes = record.get("outcomes")
    outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
    outcome_by_path = {
        str(outcome.get("worktree") or ""): outcome
        for outcome in outcomes
        if isinstance(outcome, Mapping)
    }

    ranked: list[tuple[int, int, Mapping[str, object]]] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            continue
        outcome = outcome_by_path.get(str(item.get("path") or ""), {})
        applied = str(outcome.get("applied") or "unrecorded")
        priority = (
            0 if applied in {"failed", "refused"} else 1 if item.get("reapable") else 2
        )
        ranked.append((priority, index, item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    selected = ranked[:WORKTREE_DISPOSITION_STATUS_WORKTREE_LIMIT]
    worktrees = [
        project_worktree_disposition_item(item, outcome_by_path)
        for _, _, item in selected
    ]
    total_worktrees = len(ranked)
    return {
        "schema_version": int_value(record.get("schema_version")) or 1,
        "cycle_id": bounded_worktree_disposition_text(record.get("cycle_id")),
        "occurred_at": bounded_worktree_disposition_text(record.get("occurred_at")),
        "policy": bounded_worktree_disposition_text(record.get("policy")),
        "status": bounded_worktree_disposition_text(record.get("status")),
        "candidates": int_value(record.get("candidates")) or 0,
        "reaped": int_value(record.get("reaped")) or 0,
        "kept": int_value(record.get("kept")) or 0,
        "refused": int_value(record.get("refused")) or 0,
        "errors": int_value(record.get("errors")) or 0,
        "agent_invoked": bool(record.get("agent_invoked")),
        "agent_error": bounded_worktree_disposition_text(record.get("agent_error")),
        "total_worktrees": total_worktrees,
        "worktrees_truncated": max(0, total_worktrees - len(worktrees)),
        "worktrees": worktrees,
    }


def project_worktree_disposition_item(
    item: Mapping[str, object],
    outcome_by_path: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    path = bounded_worktree_disposition_text(
        item.get("path"),
        limit=WORKTREE_DISPOSITION_STATUS_PATH_LIMIT,
    )
    outcome = outcome_by_path.get(str(item.get("path") or ""), {})
    keep_guardrails = bounded_worktree_disposition_strings(
        item.get("keep_guardrails"),
        limit=WORKTREE_DISPOSITION_STATUS_GUARDRAIL_LIMIT,
    )
    outcome_guardrails = bounded_worktree_disposition_strings(
        outcome.get("guardrails"),
        limit=WORKTREE_DISPOSITION_STATUS_GUARDRAIL_LIMIT,
    )
    raw_actions = outcome.get("actions")
    actions = raw_actions if isinstance(raw_actions, list) else []
    action_errors = [
        bounded_worktree_disposition_text(action.get("error"))
        for action in actions
        if isinstance(action, Mapping) and action.get("error")
    ][:WORKTREE_DISPOSITION_STATUS_ACTION_ERROR_LIMIT]
    applied = bounded_worktree_disposition_text(outcome.get("applied")) or "unrecorded"
    decision_reason = bounded_worktree_disposition_text(outcome.get("reason"))
    if applied == "failed":
        non_removal_reason = " | ".join(action_errors or outcome_guardrails) or (
            "reap action failed without a recorded diagnostic"
        )
    elif applied == "refused":
        non_removal_reason = " | ".join(outcome_guardrails or keep_guardrails) or (
            "reap was refused without a recorded guardrail"
        )
    elif applied == "kept":
        non_removal_reason = (
            decision_reason
            or " | ".join(keep_guardrails)
            or ("worktree was kept without a recorded reason")
        )
    elif applied == "unrecorded":
        non_removal_reason = " | ".join(keep_guardrails) or (
            "no disposition outcome was recorded"
        )
    else:
        non_removal_reason = ""
    return {
        "path": path,
        "branch": bounded_worktree_disposition_text(item.get("branch")),
        "reapable": bool(item.get("reapable")),
        "keep_guardrails": keep_guardrails,
        "requested": bounded_worktree_disposition_text(outcome.get("requested"))
        or "none",
        "applied": applied,
        "decision_reason": decision_reason,
        "outcome_guardrails": outcome_guardrails,
        "action_errors": action_errors,
        "non_removal_reason": bounded_worktree_disposition_text(non_removal_reason),
    }


def bounded_worktree_disposition_strings(
    value: object,
    *,
    limit: int,
) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = bounded_worktree_disposition_text(item)
        if text:
            result.append(text)
    return result


def bounded_worktree_disposition_text(
    value: object,
    *,
    limit: int = WORKTREE_DISPOSITION_STATUS_TEXT_LIMIT,
) -> str:
    return str(value or "")[:limit]


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


def summarize_non_closures(
    run_store: RunStore,
    *,
    window_runs: int = NON_CLOSURE_WINDOW_RUNS,
    alarm_threshold: int = NON_CLOSURE_ALARM_THRESHOLD,
) -> NonClosureSummary:
    """Summarize approved candidates that did not close their task."""

    records = run_store.read_records()
    terminal_runs: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for record in reversed(records):
        if record.get("record_type") not in {None, RUN_RECORD_TYPE}:
            continue
        run_id = str(record.get("run_id") or "")
        if not run_id or run_id in seen_run_ids:
            continue
        terminal_runs.append(record)
        seen_run_ids.add(run_id)
        if len(terminal_runs) >= window_runs:
            break
    terminal_runs.reverse()

    selected_run_ids = {str(record.get("run_id") or "") for record in terminal_runs}
    approved: set[tuple[str, str]] = set()
    closed: set[tuple[str, str]] = set()
    for record in records:
        run_id = str(record.get("run_id") or "")
        if run_id not in selected_run_ids:
            continue
        identity = (run_id, str(record.get("task_id") or ""))
        if record.get("record_type") == REVIEW_VERDICT_RECORD_TYPE and record.get(
            "verdict"
        ) in {"approve", "clean"}:
            approved.add(identity)
        elif record.get("record_type") == TASK_PROVENANCE_COMMITTED_RECORD_TYPE:
            closed.add(identity)

    approved_outcomes: list[bool] = []
    reasons: dict[str, int] = {}
    approved_candidates = 0
    non_closures = 0
    for record in terminal_runs:
        identity = (
            str(record.get("run_id") or ""),
            str(record.get("task_id") or ""),
        )
        was_approved = identity in approved
        reached_done = identity in closed
        is_non_closure = was_approved and not reached_done
        if was_approved:
            approved_candidates += 1
            approved_outcomes.append(is_non_closure)
        if is_non_closure:
            non_closures += 1
            reason = str(
                record.get("classification_source")
                or record.get("classification")
                or record.get("status")
                or "unknown"
            )
            reasons[reason] = reasons.get(reason, 0) + 1

    consecutive = 0
    for is_non_closure in reversed(approved_outcomes):
        if not is_non_closure:
            break
        consecutive += 1

    ordered_reasons = dict(
        sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    )
    return NonClosureSummary(
        window_runs=window_runs,
        observed_runs=len(terminal_runs),
        approved_candidates=approved_candidates,
        count=non_closures,
        consecutive=consecutive,
        alarm_threshold=alarm_threshold,
        reasons=ordered_reasons,
    )


def non_closure_alarm(summary: NonClosureSummary) -> str:
    return (
        "non_closure_alarm:"
        f"{summary.consecutive}_consecutive_approved_candidates_not_done"
    )


def project_blockers(
    *,
    git_status: GitStatus,
    queue_status: TaskQueueStatus,
    stale_locks: tuple[StaleLock, ...],
    workspace_diagnostics: tuple[dict[str, object], ...],
    integration_lock: dict[str, object],
    agent_diagnostics: tuple[str, ...] = (),
    supervisor: SupervisorStatus | None = None,
    project_binding: ResolvedProjectBinding | None = None,
    config_contract_blockers: tuple[ConfigContractBlocker, ...] = (),
) -> list[str]:
    blockers: list[str] = []
    if project_binding is not None:
        blockers.extend(item.code for item in project_binding.diagnostics)
    if supervisor is not None and supervisor.blocker:
        blockers.append(supervisor.blocker)
    blockers.extend(
        blocker.code
        for blocker in config_contract_blockers
        if blocker.code not in blockers
    )
    if not git_status.available:
        blockers.append(f"git_state_unavailable: {git_status.error}")
    if git_status.dirty:
        blockers.append("repo_dirty")
    if queue_status.source_error:
        blockers.append(f"task_source_unavailable: {queue_status.source_error}")
    if queue_has_no_launchable_task(queue_status):
        blockers.extend(
            "task_dispatch_blocked:"
            f"{blocker.get('task_id')}: "
            f"{str(blocker.get('message') or 'no reason reported')[:512]}"
            for blocker in queue_status.dispatch_blockers
        )
    for diagnostic in agent_diagnostics:
        blockers.append(f"agent_unavailable: {diagnostic}")
    if stale_locks:
        blockers.append("stale_locks_present")
        blockers.extend(
            "unrecoverable_stale_lock: "
            f"task={lock.task_id} run_state={lock.run_state or 'unknown'} "
            f"worker_process_state={lock.process_state or 'unknown'}; "
            "no supported command can clear it without proof that its run "
            "finished"
            for lock in stale_locks
            if not lock.recovery_supported
        )
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
    observations = [
        "source_gated_task:"
        f"{task.get('id') or task.get('task_id')}: "
        f"{str(task.get('reason') or 'no reason reported')[:512]}"
        for task in queue_status.gated_tasks
    ]
    if not queue_status.source_error and queue_status.runnable == 0:
        running_workers = active_conflict_worker_count(workers)
        if running_workers:
            observations.append(f"waiting_for_active_workers:{running_workers}")
        elif queue_status.ready == 0:
            observations.append("no_runnable_work")
    return observations


def active_unlocked_task_blockers(
    queue_status: TaskQueueStatus,
    workers: tuple[WorkerView, ...],
    records: Sequence[Mapping[str, object]],
) -> list[str]:
    locked_task_ids = {worker.active.task_id for worker in workers}
    latest_run_ids: dict[str, str] = {}
    classifications: dict[tuple[str, str], str] = {}
    runtime_owned_runs: set[tuple[str, str]] = set()
    provenance_committed_runs: set[tuple[str, str]] = set()
    for record in records:
        task_id = str(record.get("task_id") or "")
        run_id = str(record.get("run_id") or "")
        if not task_id or not run_id:
            continue
        if record.get("record_type") == RUN_STARTED_RECORD_TYPE:
            latest_run_ids[task_id] = run_id
        elif (
            record.get("record_type") == RUN_CONTRACT_RESOLVED_RECORD_TYPE
            and record.get("mode") == "runtime-owned"
        ):
            runtime_owned_runs.add((task_id, run_id))
        elif record.get("record_type") == RUN_RECORD_TYPE and str(
            record.get("classification") or ""
        ):
            latest_run_ids[task_id] = run_id
            classifications[(task_id, run_id)] = str(record["classification"])
        elif record.get("record_type") == TASK_PROVENANCE_COMMITTED_RECORD_TYPE:
            provenance_committed_runs.add((task_id, run_id))
    blockers: list[str] = []
    source_tasks: dict[str, Mapping[str, object]] = {}
    for task in queue_status.source_tasks:
        task_id = str(task.get("id") or task.get("task_id") or "")
        source_tasks[task_id] = task
        if str(task.get("status") or "").casefold() not in ACTIVE_QUEUE_STATUSES:
            continue
        if task_id in locked_task_ids:
            continue
        latest_run_id = latest_run_ids.get(task_id, "")
        if not latest_run_id:
            continue
        classification = classifications.get((task_id, latest_run_id), "")
        if classification and classification != "completed":
            continue
        code = (
            "active_unlocked_task_completion_unsettled"
            if classification == "completed"
            else "active_unlocked_without_terminal_classification"
        )
        blockers.append(f"{code}:{task_id}")
    for task_id, run_id in latest_run_ids.items():
        if classifications.get((task_id, run_id)) != "completed":
            continue
        if (task_id, run_id) not in runtime_owned_runs:
            continue
        task = source_tasks.get(task_id, {})
        task_done = task.get("done") is True or str(
            task.get("status") or ""
        ).casefold() in {"done", "completed"}
        if not task_done:
            blocker = f"task_completion_unsettled:{task_id}:{run_id}"
            if blocker not in blockers:
                blockers.append(blocker)
        if (task_id, run_id) not in provenance_committed_runs:
            blockers.append(f"task_provenance_unsettled:{task_id}:{run_id}")
    return blockers


def active_conflict_worker_count(workers: tuple[WorkerView, ...]) -> int:
    return sum(1 for worker in workers if worker_holds_active_conflict(worker))


def worker_holds_active_conflict(worker: WorkerView) -> bool:
    return worker_view_is_live(worker)


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
    config_contract_blockers: tuple[ConfigContractBlocker, ...] = ()

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
            "config_contract_blockers": [
                blocker.to_json() for blocker in self.config_contract_blockers
            ],
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
class AutopilotReloadResult:
    repo: Path
    reloaded: bool
    state: str
    run_id: str = ""
    pid: int | None = None
    request_id: str = ""
    changed_keys: tuple[str, ...] = ()
    loaded_at: str = ""
    fingerprint: str = ""
    blocker: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.reloaded or self.state == "pending" else 2

    def to_json(self) -> dict[str, object]:
        return {
            "repo": str(self.repo),
            "reloaded": self.reloaded,
            "state": self.state,
            "run_id": self.run_id,
            "pid": self.pid,
            "request_id": self.request_id,
            "changed_keys": list(self.changed_keys),
            "loaded_at": self.loaded_at,
            "fingerprint": self.fingerprint,
            "blocker": self.blocker,
        }


OWNED_PROCESS_ROLE_SUPERVISOR = "supervisor"
OWNED_PROCESS_ROLE_CHILD = "run_until_done_child"
OWNED_PROCESS_ROLE_WORKER = "worker"
OWNED_PROCESS_ROLE_DESCENDANT = "descendant"
AUTOPILOT_STOP_TERMINATED_STATUS = "terminated"
AUTOPILOT_STOP_TERMINATION_REASON = "autopilot_stop"


@dataclasses.dataclass(frozen=True)
class OwnedProcessIdentity:
    """One process this installation recorded starting, with its birth proof."""

    role: str
    pid: int
    process_group_id: int
    session_id: int
    process_birth_id: str
    parent_pid: int | None = None
    run_id: str = ""
    task_id: str = ""

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "role": self.role,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "session_id": self.session_id,
            "parent_pid": self.parent_pid,
        }
        # The birth ID embeds the host boot ID; the identity is diagnostic, so
        # only its presence is reported, never the value itself.
        payload["process_birth_id_known"] = bool(self.process_birth_id)
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.task_id:
            payload["task_id"] = self.task_id
        return payload


@dataclasses.dataclass(frozen=True)
class OwnedProcessDrainResult:
    drained: tuple[OwnedProcessIdentity, ...] = ()
    remaining: tuple[OwnedProcessIdentity, ...] = ()
    blocker: str = ""

    @property
    def complete(self) -> bool:
        return not self.blocker and not self.remaining


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
    drained: tuple[OwnedProcessIdentity, ...] = ()
    remaining: tuple[OwnedProcessIdentity, ...] = ()
    reconciled_task_ids: tuple[str, ...] = ()
    reconciliation_blockers: tuple[str, ...] = ()

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
            "drained": [identity.to_json() for identity in self.drained],
            "remaining": [identity.to_json() for identity in self.remaining],
            "reconciled_task_ids": list(self.reconciled_task_ids),
            "reconciliation_blockers": list(self.reconciliation_blockers),
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
    reload_config_jobs: bool,
    interval: float,
    once: bool,
    max_cycles: int,
    ask_agent: bool,
    continue_on_failure: bool,
    max_slices: int,
    max_tasks: int,
    min_ready: int,
    dispatch_min_ready: int = 1,
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
        "--dispatch-min-ready",
        str(dispatch_min_ready),
        "--worktree-disposition",
        config.autopilot.worktree_disposition,
        "--detached-reload-signal",
    ]
    if reload_config_jobs:
        command.append("--reload-config-jobs")
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
    reload_config_jobs: bool = False,
    interval: float = 0.0,
    once: bool = False,
    max_cycles: int = 0,
    ask_agent: bool = False,
    continue_on_failure: bool = False,
    max_slices: int = 0,
    max_tasks: int = 0,
    min_ready: int = 1,
    dispatch_min_ready: int = 1,
    verification_timeout: float = 5.0,
    verification_interval: float = 0.05,
) -> DetachedAutopilotLaunch:
    """Start and verify a detached POSIX autopilot supervisor."""

    interval = require_autopilot_interval(interval)
    if os.name != "posix" or not hasattr(os, "setsid"):
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            blocker=f"detached_autopilot_unsupported_platform:{sys.platform}",
        )

    binding = resolve_project_binding(config)
    if binding.blocker is not None:
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            blocker=binding.blocker,
        )

    contract_blockers = config_contract_blockers(config)
    if contract_blockers:
        return DetachedAutopilotLaunch(
            repo=config.repo,
            started=False,
            blocker=contract_blockers[0].code,
            config_contract_blockers=contract_blockers,
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
        reload_config_jobs=reload_config_jobs,
        interval=interval,
        once=once,
        max_cycles=max_cycles,
        ask_agent=ask_agent,
        continue_on_failure=continue_on_failure,
        max_slices=max_slices,
        max_tasks=max_tasks,
        min_ready=min_ready,
        dispatch_min_ready=dispatch_min_ready,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_environment, context_file = runtime_context_subprocess_transport(
        config.runtime_context,
        bound_names=config.project_binding.require,
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
    candidate_run_id = ""
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
                candidate_run_id = lock_run_id
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
                run_id=candidate_run_id,
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
    run_id: str = "",
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
        matching_candidate = (
            status.locked
            and lock_run_id
            and (
                (run_id and lock_run_id == run_id)
                or (not run_id and lock_pid == process.pid)
            )
            and (lock_pid is None or lock_pid == process.pid)
        )
        if matching_candidate:
            lock_manager.release_autopilot(
                run_id=lock_run_id,
                fencing_token=str(status.metadata.get("fencing_token") or ""),
            )
            status = lock_manager.autopilot_status()
            if (
                status.locked
                and str(status.metadata.get("run_id") or "") == lock_run_id
            ):
                errors.append("candidate_autopilot_lock_persisted")
        elif status.locked and run_id and lock_run_id == run_id:
            errors.append("candidate_autopilot_lock_pid_mismatch")
    # Cleanup must preserve the original actionable verification failure even
    # when a third-party lock adapter has an unenumerated operational failure.
    except Exception as exc:
        errors.append(f"{type(exc).__name__}:{exc}")
    return ";".join(errors)


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


def wait_for_verified_process_stop(
    identity: DetachedAutopilotIdentity,
    *,
    pidfd: int,
    pidfd_exited: Callable[[int], bool],
    process_node: Callable[[int], ProcessNode | None],
    sleep: Sleep,
    monotonic: Callable[[], float],
    deadline: float,
) -> str:
    """Wait until the exact supervisor is kernel-observed stopped or exited."""

    while True:
        if pidfd_exited(pidfd):
            return ""
        current = process_node(identity.pid)
        if current is None:
            if pidfd_exited(pidfd):
                return ""
            return "autopilot_stop_identity_unverified:supervisor_state_missing"
        if (
            current.process_birth_id != identity.process_birth_id
            or current.process_group_id != identity.process_group_id
            or current.session_id != identity.session_id
        ):
            return "autopilot_stop_identity_unverified:supervisor_state_changed"
        if current.state in {"T", "t"}:
            return ""
        if monotonic() >= deadline:
            return "autopilot_stop_timeout"
        try:
            sleep(min(0.05, max(0.0, deadline - monotonic())))
        except KeyboardInterrupt:
            return "autopilot_stop_interrupted"


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


def autopilot_child_identity(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
) -> OwnedProcessIdentity | None:
    """The run-until-done child this supervisor run last recorded starting.

    Returns None when no child was recorded, which is the normal state for a
    supervisor between cycles. A recorded child with incomplete identity is
    surfaced with an empty birth ID so the caller can fail closed rather than
    signalling an unverifiable PID.
    """

    if not run_id:
        return None
    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_CHILD_STARTED_RECORD_TYPE:
            continue
        if str(record.get("repo") or "") != str(repo):
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        pid = int_value(record.get("pid"))
        if pid is None:
            return None
        return OwnedProcessIdentity(
            role=OWNED_PROCESS_ROLE_CHILD,
            pid=pid,
            process_group_id=int_value(record.get("process_group_id")) or 0,
            session_id=int_value(record.get("session_id")) or 0,
            process_birth_id=str(record.get("process_birth_id") or ""),
            run_id=run_id,
        )
    return None


def autopilot_child_pids(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
) -> frozenset[int]:
    """Every run-until-done child PID this supervisor run recorded starting.

    A supervisor runs many cycles, and a worker orphaned by an earlier cycle can
    still be alive during a later one. Attribution therefore spans all recorded
    children, not just the current one: comparing such a worker against only the
    latest child would reject a worker this run demonstrably launched. Admission
    still requires the worker's own birth identity to verify, so a PID reused
    across cycles cannot smuggle in an unrelated process.
    """

    if not run_id:
        return frozenset()
    pids: set[int] = set()
    for record in run_store.read_records():
        if record.get("record_type") != AUTOPILOT_CHILD_STARTED_RECORD_TYPE:
            continue
        if str(record.get("repo") or "") != str(repo):
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        pid = int_value(record.get("pid"))
        if pid is not None:
            pids.add(pid)
    return frozenset(pids)


def live_active_run_states(
    lock_manager: LockManager,
    run_store: RunStore,
    *,
    process_exists: ProcessExists,
    timeout_seconds: float | None = None,
) -> tuple[tuple[ActiveRunState, ...], str]:
    """Active-run locks in this repository whose recorded worker PID is live.

    Locks are read from this installation's own lock root, never from a command
    name match or an ambient process listing, so a peer installation's workers
    can never appear here.
    """

    try:
        locks = lock_manager.list_locks(timeout_seconds=timeout_seconds)
    except (LockBackendError, OSError):
        return (), "autopilot_stop_worker_lock_enumeration_failed"
    records = run_store.read_records()
    live: list[ActiveRunState] = []
    for metadata in locks:
        active = ActiveRunState.from_lock_metadata(dict(metadata))
        if active is None or active.worker_pid is None:
            continue
        active = restore_projected_worker_process_identity(active, records)
        if not process_exists(active.worker_pid):
            continue
        live.append(active)
    return tuple(live), ""


def collect_owned_stop_roots(
    run_store: RunStore,
    lock_manager: LockManager,
    *,
    repo: Path,
    run_id: str,
    process_exists: ProcessExists,
    birth_identity_lookup: Callable[[int], str],
    timeout_seconds: float | None = None,
) -> tuple[tuple[OwnedProcessIdentity, ...], str, tuple[str, ...]]:
    """Verified non-supervisor roots, a fail-closed blocker, and diagnostics.

    A worker is an independently verifiable root: its own recorded birth
    identity proves which process it is, and the active-run lock in this
    repository's lock root proves the run owns it. Its liveness is therefore
    never inferred from its supervising child. That distinction is the whole
    point of this pass: a worker orphans to PID 1 precisely *because* its child
    died, so gating worker roots on a live child would skip exactly the
    processes that outlive a stop.

    The recorded children only supply attribution, and attribution spans every
    child this run recorded rather than the current one, so a worker orphaned by
    an earlier cycle is still recognized as this run's. A live worker this run
    cannot attribute to any recorded child, or whose birth identity was never
    recorded, is unverifiable rather than absent, so it blocks the stop instead
    of being silently left running.
    """

    child = autopilot_child_identity(run_store, repo=repo, run_id=run_id)
    attributable_pids = autopilot_child_pids(run_store, repo=repo, run_id=run_id)
    child_live = child is not None and process_exists(child.pid)
    if child_live:
        assert child is not None
        if not child.process_birth_id:
            return (), "autopilot_stop_identity_unverified:child_birth_id_missing", ()
        if birth_identity_lookup(child.pid) != child.process_birth_id:
            return (), "autopilot_stop_identity_unverified:child_birth_id_mismatch", ()

    live_states, enumeration_blocker = live_active_run_states(
        lock_manager,
        run_store,
        process_exists=process_exists,
        timeout_seconds=timeout_seconds,
    )
    if enumeration_blocker:
        return (), enumeration_blocker, ()

    roots: list[OwnedProcessIdentity] = []
    diagnostics: list[str] = []
    for active in live_states:
        label = active.task_id or active.run_id or str(active.worker_pid)
        if active.supervisor_pid not in attributable_pids:
            # A live worker matching no recorded child cannot be proven to
            # belong to this run, and cannot be proven not to either.
            return (
                (),
                f"autopilot_stop_identity_unverified:worker_unattributable:{label}",
                (),
            )
        if not active.worker_process_birth_id:
            return (
                (),
                f"autopilot_stop_identity_unverified:worker_birth_id_missing:{label}",
                (),
            )
        if birth_identity_lookup(active.worker_pid) != active.worker_process_birth_id:
            # The same PID remains inside the child's ancestry snapshot, but it
            # no longer names the recorded worker. Treating it as an ordinary
            # descendant would signal a recycled process after the exact worker
            # identity already disproved ownership.
            return (
                (),
                f"autopilot_stop_identity_unverified:worker_birth_id_mismatch:{label}",
                (),
            )
        roots.append(
            OwnedProcessIdentity(
                role=OWNED_PROCESS_ROLE_WORKER,
                pid=active.worker_pid,
                process_group_id=active.worker_process_group_id or 0,
                session_id=active.worker_session_id or 0,
                process_birth_id=active.worker_process_birth_id,
                run_id=active.run_id,
                task_id=active.task_id,
            )
        )
    if child_live:
        assert child is not None
        roots.append(child)
    return tuple(roots), "", tuple(diagnostics)


def drain_owned_process_tree(
    roots: Sequence[OwnedProcessIdentity],
    *,
    pidfd_open: Callable[[int], int],
    pidfd_signal: Callable[[int, int], None],
    pidfd_exited: Callable[[int], bool],
    close_fd: Callable[[int], None],
    sleep: Sleep,
    monotonic: Callable[[], float],
    deadline: float,
    process_table: Callable[[], dict[int, ProcessNode]],
    process_node: Callable[[int], ProcessNode | None],
    before_signal: Callable[[], str] | None = None,
) -> OwnedProcessDrainResult:
    """Terminate the exact recorded process tree, deepest descendants first.

    Every pidfd is opened and re-verified against the snapshot before any
    signal is sent, so a single unverifiable candidate aborts the drain with
    zero signals rather than leaving a half-signalled tree. The pidfds are then
    retained for the whole wait: they stay bound to the original process even
    after it reparents to PID 1, which a PID-based recheck cannot do.
    """

    if not roots:
        return OwnedProcessDrainResult()
    root_births = {root.pid: root.process_birth_id for root in roots}
    role_by_pid = {root.pid: root for root in roots}
    table = process_table()
    candidates = collect_owned_descendants(table, root_births)
    if not candidates:
        return OwnedProcessDrainResult()

    def identity_for(node: ProcessNode) -> OwnedProcessIdentity:
        recorded = role_by_pid.get(node.pid)
        return OwnedProcessIdentity(
            role=recorded.role if recorded else OWNED_PROCESS_ROLE_DESCENDANT,
            pid=node.pid,
            process_group_id=node.process_group_id,
            session_id=node.session_id,
            process_birth_id=node.process_birth_id,
            parent_pid=node.parent_pid,
            run_id=recorded.run_id if recorded else "",
            task_id=recorded.task_id if recorded else "",
        )

    opened: list[tuple[OwnedProcessIdentity, int]] = []
    blocker = ""
    exited_before_open: list[OwnedProcessIdentity] = []
    try:
        for node in candidates:
            identity = identity_for(node)
            if not node.process_birth_id:
                blocker = (
                    "autopilot_stop_identity_unverified:"
                    f"descendant_birth_id_missing:{node.pid}"
                )
                break
            try:
                process_fd = pidfd_open(node.pid)
            except ProcessLookupError:
                # Exiting between snapshot and open is the drain succeeding
                # early for that process, not an unverifiable identity.
                exited_before_open.append(identity)
                continue
            except OSError:
                blocker = (
                    "autopilot_stop_identity_unverified:"
                    f"descendant_pidfd_unavailable:{node.pid}"
                )
                break
            opened.append((identity, process_fd))
            current = process_node(node.pid)
            if current is None:
                # The pidfd already pins this process; a vanished /proc entry
                # means it exited after the open, so the retained fd still
                # observes its exit.
                continue
            if (
                current.process_birth_id != node.process_birth_id
                or current.parent_pid != node.parent_pid
                or current.process_group_id != node.process_group_id
                or current.session_id != node.session_id
            ):
                blocker = (
                    "autopilot_stop_identity_unverified:"
                    f"descendant_identity_changed:{node.pid}"
                )
                break
        if blocker:
            return OwnedProcessDrainResult(blocker=blocker)

        if monotonic() >= deadline:
            return OwnedProcessDrainResult(blocker="autopilot_stop_drain_timeout")

        if before_signal is not None:
            try:
                before_signal_blocker = before_signal()
            except OSError:
                return OwnedProcessDrainResult(
                    blocker="autopilot_stop_supervisor_quiesce_failed"
                )
            if before_signal_blocker:
                return OwnedProcessDrainResult(blocker=before_signal_blocker)
            if monotonic() >= deadline:
                return OwnedProcessDrainResult(blocker="autopilot_stop_drain_timeout")

        for identity, process_fd in opened:
            try:
                pidfd_signal(process_fd, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError:
                # Report only what is actually still alive: a process that
                # already exited is not something the operator must chase.
                return OwnedProcessDrainResult(
                    remaining=tuple(
                        entry
                        for entry, entry_fd in opened
                        if not pidfd_exited(entry_fd)
                    ),
                    blocker=(
                        f"autopilot_stop_signal_failed:{identity.role}:{identity.pid}"
                    ),
                )

        pending = list(opened)
        drained: list[OwnedProcessIdentity] = list(exited_before_open)
        while True:
            still_pending: list[tuple[OwnedProcessIdentity, int]] = []
            for identity, process_fd in pending:
                if pidfd_exited(process_fd):
                    drained.append(identity)
                else:
                    still_pending.append((identity, process_fd))
            pending = still_pending
            if not pending:
                return OwnedProcessDrainResult(
                    drained=tuple(drained),
                )
            if monotonic() >= deadline:
                return OwnedProcessDrainResult(
                    drained=tuple(drained),
                    remaining=tuple(identity for identity, _fd in pending),
                    blocker="autopilot_stop_drain_timeout",
                )
            try:
                sleep(min(0.05, max(0.0, deadline - monotonic())))
            except KeyboardInterrupt:
                return OwnedProcessDrainResult(
                    drained=tuple(drained),
                    remaining=tuple(identity for identity, _fd in pending),
                    blocker="autopilot_stop_interrupted",
                )
    finally:
        for _identity, process_fd in opened:
            close_fd(process_fd)


def reconcile_drained_workers(
    run_store: RunStore,
    lock_manager: LockManager,
    *,
    repo: Path,
    drained: Sequence[OwnedProcessIdentity],
    backend_timeout: Callable[[], float] | None = None,
    deadline_expired: Callable[[], bool] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Record terminated runs and release only their exact task locks.

    A worker killed by a stop never filed a report, so its run is classified
    terminated. Success is never synthesized: the authoritative task stays
    active and its committed worktree is preserved, so the work can be picked
    up again rather than being silently marked finished.
    """

    reconciled: list[str] = []
    blockers: list[str] = []
    remaining_timeout = backend_timeout or (lambda: 30.0)
    expired = deadline_expired or (lambda: False)
    for identity in drained:
        if identity.role != OWNED_PROCESS_ROLE_WORKER:
            continue
        if not identity.run_id or not identity.task_id:
            blockers.append(
                f"autopilot_stop_reconcile_identity_incomplete:{identity.pid}"
            )
            continue
        if expired():
            blockers.append(f"autopilot_stop_reconcile_timeout:{identity.task_id}")
            continue
        worker_report = run_store.latest_worker_report(
            identity.run_id, identity.task_id
        )
        try:
            metadata = lock_manager.status_with_timeout(
                identity.task_id,
                timeout_seconds=remaining_timeout(),
            )
        except (LockBackendError, OSError):
            blockers.append(
                f"autopilot_stop_reconcile_status_failed:{identity.task_id}"
            )
            continue
        if expired():
            blockers.append(f"autopilot_stop_reconcile_timeout:{identity.task_id}")
            continue
        if metadata is None:
            continue
        active = ActiveRunState.from_lock_metadata(dict(metadata))
        if active is None or active.run_id != identity.run_id:
            blockers.append(f"autopilot_stop_reconcile_lock_changed:{identity.task_id}")
            continue
        # Compare against the generation this installation recorded when it
        # acquired the lock, not the one the backend now reports: a lock
        # re-created out of band would otherwise pass by agreeing with itself.
        local_fencing_token = lock_manager.local_fencing_token(identity.task_id)
        if (
            not active.fencing_token
            or not local_fencing_token
            or local_fencing_token != active.fencing_token
        ):
            blockers.append(
                f"autopilot_stop_reconcile_fencing_mismatch:{identity.task_id}"
            )
            continue
        if worker_report is None:
            existing_terminal_result = any(
                record.get("record_type") in {None, "run_result"}
                and str(record.get("run_id") or "") == identity.run_id
                and str(record.get("task_id") or "") == identity.task_id
                and str(record.get("status") or record.get("classification") or "")
                not in {"", "unknown"}
                for record in run_store.read_records()
            )
            try:
                if not existing_terminal_result:
                    run_store.append_result(
                        RunResult(
                            run_id=identity.run_id,
                            task_id=identity.task_id,
                            classification=AUTOPILOT_STOP_TERMINATED_STATUS,
                            exit_code=-signal.SIGTERM,
                            log_path=active.log_path,
                            start_main=active.base_main,
                            end_main=active.base_main,
                            message="worker terminated by autopilot stop",
                            started_at=active.started_at,
                            session_id=active.session_id or None,
                            session_id_source=(
                                active.session_id_source or "fallback:run_id"
                            ),
                            agent_kind=active.agent_kind,
                            agent_prompt_dialect=active.agent_prompt_dialect,
                            agent_prompt_dialect_source=(
                                active.agent_prompt_dialect_source
                            ),
                            agent_skill_ref_prefix=active.agent_skill_ref_prefix,
                            agent_skill_ref_prefix_source=(
                                active.agent_skill_ref_prefix_source
                            ),
                            model_provider=active.model_provider,
                            model_provider_source=active.model_provider_source,
                            model_id=active.model_id,
                            model_id_source=active.model_id_source,
                            reasoning_effort=active.reasoning_effort,
                            reasoning_effort_source=active.reasoning_effort_source,
                            trailer_context=dict(active.trailer_context),
                            trailer_context_sources=dict(
                                active.trailer_context_sources
                            ),
                            classification_source=AUTOPILOT_STOP_TERMINATION_REASON,
                            restart_count=active.restart_count,
                            max_restarts=active.max_restarts,
                        )
                    )
                run_store.append_lifecycle_event(
                    RunLifecycleEvent.run_state_transition(
                        run_id=identity.run_id,
                        task_id=identity.task_id,
                        to_state=AUTOPILOT_STOP_TERMINATED_STATUS,
                        reason=AUTOPILOT_STOP_TERMINATION_REASON,
                    )
                )
            except OSError:
                blockers.append(
                    f"autopilot_stop_reconcile_result_failed:{identity.task_id}"
                )
                continue
        if expired():
            blockers.append(f"autopilot_stop_reconcile_timeout:{identity.task_id}")
            continue
        try:
            released = lock_manager.release_stale_lock(
                task_id=identity.task_id,
                run_id=identity.run_id,
                path=active.lock_path or Path(str(metadata.get("path") or "")),
                kind="task",
                timeout_seconds=remaining_timeout(),
            )
        except (LockBackendError, OSError):
            blockers.append(
                f"autopilot_stop_reconcile_release_failed:{identity.task_id}"
            )
            continue
        if expired():
            blockers.append(f"autopilot_stop_reconcile_timeout:{identity.task_id}")
            continue
        if not released:
            blockers.append(f"autopilot_stop_reconcile_lock_changed:{identity.task_id}")
            continue
        reconciled.append(identity.task_id)
    return tuple(reconciled), tuple(blockers)


def supervisor_lock_released(status: IntegrationLockStatus, run_id: str) -> bool:
    """True when the singleton lock is no longer held by `run_id`.

    A successor supervisor that acquires the lock between polls must not read as
    a failed release. An owner the backend reports without a run ID is not such a
    successor: it is an unidentified holder, so it fails closed rather than
    letting a still-held lock pass as released.
    """

    if not status.locked:
        return True
    current_run_id = str(status.metadata.get("run_id") or "")
    return bool(current_run_id) and current_run_id != run_id


def autopilot_supervisor_started_pid(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
) -> int | None:
    """PID this installation recorded for a supervisor run, or None.

    `run_autopilot` appends this record only after `enable_termination_signals`,
    so its presence is the local trusted contract that a detached supervisor can
    honor a stop signal through its normal cleanup path. It is also the only
    local witness of which process held the lock when the backend keeps no PID.
    """

    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE:
            continue
        if str(record.get("repo") or "") != str(repo):
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        return int_value(record.get("pid"))
    return None


def autopilot_supervisor_started_recorded(
    run_store: RunStore,
    *,
    repo: Path,
    run_id: str,
    pid: int,
) -> bool:
    """True once the supervisor recorded that its termination handlers are live."""

    return autopilot_supervisor_started_pid(run_store, repo=repo, run_id=run_id) == pid


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
    drained: Sequence[OwnedProcessIdentity] = (),
    reconciled_task_ids: Sequence[str] = (),
    reconciliation_blockers: Sequence[str] = (),
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
        "drained": [identity.to_json() for identity in drained],
        "reconciled_task_ids": list(reconciled_task_ids),
        "reconciliation_blockers": list(reconciliation_blockers),
    }
    if signal_number is not None:
        record["signal"] = signal.Signals(signal_number).name
    run_store.append_record(record)


def reload_detached_autopilot(
    config: VibeConfig,
    *,
    timeout: float = 10.0,
    process_exists: ProcessExists | None = None,
    process_group_lookup: Callable[[int], int] | None = None,
    session_lookup: Callable[[int], int] | None = None,
    birth_identity_lookup: Callable[[int], str] | None = None,
    pidfd_open: Callable[[int], int] | None = None,
    pidfd_signal: Callable[[int, int], None] | None = None,
    close_fd: Callable[[int], None] | None = None,
    sleep: Sleep | None = None,
    monotonic: Callable[[], float] | None = None,
) -> AutopilotReloadResult:
    """Request and verify a configuration reload from the detached supervisor."""

    binding = resolve_project_binding(config)
    if binding.blocker is not None:
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            blocker=binding.blocker,
        )
    if sys.platform != "linux" or not hasattr(signal, "SIGHUP"):
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            blocker=f"autopilot_reload_unsupported_platform:{sys.platform}",
        )

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
    close_process_fd = close_fd if close_fd is not None else os.close
    sleeper = sleep if sleep is not None else time_module.sleep
    clock = monotonic if monotonic is not None else time_module.monotonic
    deadline = clock() + max(0.0, timeout)
    lock_manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
        runtime_context=config.runtime_environment,
    )
    run_store = RunStore(config.state_path / "runs.jsonl")
    try:
        status = lock_manager.autopilot_status(process_exists=checker)
    except (LockBackendError, OSError):
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            blocker="autopilot_reload_backend_status_failed",
        )
    owner_run_id = str(status.metadata.get("run_id") or "")
    pid = int_value(status.metadata.get("pid"))
    if not status.locked:
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="not_running",
            blocker="autopilot_supervisor_not_running",
        )
    if status.state != "held" or status.process_state == "foreign_host":
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker=(
                "autopilot_reload_identity_unverified:"
                + (
                    "foreign_host"
                    if status.process_state == "foreign_host"
                    else status.state
                )
            ),
        )
    if not owner_run_id or pid is None:
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_reload_identity_unverified:missing_lock_identity",
        )
    identity = detached_autopilot_identity(
        run_store,
        run_id=owner_run_id,
        pid=pid,
    )
    if identity is None:
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_reload_identity_unverified:missing_detached_record",
        )
    started_record = next(
        (
            record
            for record in reversed(run_store.read_records())
            if record.get("record_type") == AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE
            and str(record.get("run_id") or "") == owner_run_id
        ),
        None,
    )
    if started_record is None:
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_reload_missing_start_config",
        )

    start_keys = {
        str(key): str(value)
        for key, value in dict(
            started_record.get("config_key_fingerprints") or {}
        ).items()
    }
    changed_keys = changed_config_keys(start_keys, config.config_key_fingerprints)
    reload_config_jobs = bool(started_record.get("reload_config_jobs"))
    refused_keys = tuple(
        key
        for key in changed_keys
        if not config_key_reload_safe(
            key,
            reload_config_jobs=reload_config_jobs,
        )
    )
    request_id = new_run_id("autopilot-reload")
    if refused_keys:
        requested_at = utc_now_iso()
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CONFIG_RELOAD_REQUESTED_RECORD_TYPE,
                "occurred_at": requested_at,
                "repo": str(config.repo),
                "run_id": owner_run_id,
                "pid": pid,
                "request_id": request_id,
                "requested_fingerprint": config_snapshot_fingerprint(config),
                "changed_keys": list(changed_keys),
            }
        )
        blocker = "autopilot_reload_requires_restart:" + ",".join(refused_keys)
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": owner_run_id,
                "request_id": request_id,
                "state": "refused",
                "changed_keys": list(changed_keys),
                "blocker": blocker,
            }
        )
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="refused",
            run_id=owner_run_id,
            pid=pid,
            request_id=request_id,
            changed_keys=changed_keys,
            blocker=blocker,
        )

    try:
        process_fd = open_pidfd(identity.pid)
    except (OSError, ProcessLookupError):
        return AutopilotReloadResult(
            repo=config.repo,
            reloaded=False,
            state="blocked",
            run_id=owner_run_id,
            pid=pid,
            blocker="autopilot_reload_identity_unverified:pidfd_unavailable",
        )
    try:
        try:
            actual_process_group = get_process_group(identity.pid)
            actual_session = get_session(identity.pid)
            actual_birth_id = get_birth_identity(identity.pid)
        except OSError:
            return AutopilotReloadResult(
                repo=config.repo,
                reloaded=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                blocker="autopilot_reload_identity_unverified:missing_process",
            )
        if (
            identity.process_group_id != identity.pid
            or identity.session_id != identity.pid
            or actual_process_group != identity.process_group_id
            or actual_session != identity.session_id
            or not actual_birth_id
            or actual_birth_id != identity.process_birth_id
        ):
            return AutopilotReloadResult(
                repo=config.repo,
                reloaded=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                blocker="autopilot_reload_identity_unverified:identity_changed",
            )
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CONFIG_RELOAD_REQUESTED_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": owner_run_id,
                "pid": pid,
                "request_id": request_id,
                "requested_fingerprint": config_snapshot_fingerprint(config),
                "changed_keys": list(changed_keys),
            }
        )
        try:
            send_pidfd_signal(process_fd, signal.SIGHUP)
        except (OSError, ProcessLookupError):
            return AutopilotReloadResult(
                repo=config.repo,
                reloaded=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                request_id=request_id,
                changed_keys=changed_keys,
                blocker="autopilot_reload_signal_failed",
            )
        while clock() < deadline:
            for record in reversed(run_store.read_records()):
                if (
                    record.get("record_type")
                    == AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE
                    and str(record.get("request_id") or "") == request_id
                ):
                    state = str(record.get("state") or "")
                    return AutopilotReloadResult(
                        repo=config.repo,
                        reloaded=state in {"loaded", "unchanged"},
                        state=state,
                        run_id=owner_run_id,
                        pid=pid,
                        request_id=request_id,
                        changed_keys=tuple(
                            str(key) for key in record.get("changed_keys", [])
                        ),
                        loaded_at=str(record.get("loaded_at") or ""),
                        fingerprint=str(record.get("config_fingerprint") or ""),
                        blocker=str(record.get("blocker") or ""),
                    )
            sleeper(min(0.05, max(0.0, deadline - clock())))
    finally:
        close_process_fd(process_fd)
    return AutopilotReloadResult(
        repo=config.repo,
        reloaded=False,
        state="pending",
        run_id=owner_run_id,
        pid=pid,
        request_id=request_id,
        changed_keys=changed_keys,
    )


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
    process_table: Callable[[], dict[int, ProcessNode]] | None = None,
    process_node: Callable[[int], ProcessNode | None] | None = None,
) -> AutopilotStopResult:
    """Stop a verified detached supervisor or explicitly recover its stale lock."""

    binding = resolve_project_binding(config)
    if binding.blocker is not None:
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            blocker=binding.blocker,
        )

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
    snapshot_process_table = (
        process_table if process_table is not None else read_process_table
    )
    lookup_process_node = (
        process_node if process_node is not None else read_process_node
    )
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

    def deadline_expired() -> bool:
        return clock() >= stop_deadline or time_module.monotonic() >= backend_deadline

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
    if deadline_expired():
        return AutopilotStopResult(
            repo=config.repo,
            stopped=False,
            state="blocked",
            blocker="autopilot_stop_deadline_exceeded",
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
        if run_id and owner_run_id and owner_run_id != run_id:
            # Reject a run the lock does not name before deriving any identity
            # from it, so the operator sees the real mismatch rather than a
            # downstream consequence of it.
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=owner_run_id,
                pid=pid,
                process_exited=not owner_live,
                blocker="autopilot_stale_recovery_owner_mismatch",
            )
        if pid is None and run_id:
            # A command-backed singleton may record no PID at all. The exact
            # identity then comes from this installation's own started record
            # for that run, the only local witness of which process held the
            # lock. Absence is still verified against that exact PID below.
            pid = autopilot_supervisor_started_pid(
                run_store,
                repo=config.repo,
                run_id=run_id,
            )
            owner_live = pid is not None and checker(pid)
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
            # Neither the lock nor the local started record named a process, so
            # absence cannot be verified and no terminal record written here
            # could ever justify "stopped".
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
                verified_pid=pid,
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
                    lock_released = supervisor_lock_released(current, run_id)
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
        process_table=snapshot_process_table,
        process_node=lookup_process_node,
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
    process_table: Callable[[], dict[int, ProcessNode]],
    process_node: Callable[[int], ProcessNode | None],
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

        # The supervisor's own descendants are drained first: signalling only
        # the supervisor lets its run-until-done child and that child's workers
        # reparent to PID 1 and keep burning quota after stop reports success.
        roots, roots_blocker, retained_lock_diagnostics = collect_owned_stop_roots(
            run_store,
            lock_manager,
            repo=config.repo,
            run_id=identity.run_id,
            process_exists=process_exists,
            birth_identity_lookup=birth_identity_lookup,
            timeout_seconds=max(0.001, backend_deadline - time_module.monotonic()),
        )
        if roots_blocker:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                blocker=roots_blocker,
            )
        if monotonic() >= stop_deadline or time_module.monotonic() >= backend_deadline:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                blocker="autopilot_stop_deadline_exceeded",
            )
        supervisor_quiesced = False

        def quiesce_supervisor() -> str:
            nonlocal supervisor_quiesced
            pidfd_signal(process_fd, signal.SIGSTOP)
            supervisor_quiesced = True
            return wait_for_verified_process_stop(
                identity,
                pidfd=process_fd,
                pidfd_exited=pidfd_exited,
                process_node=process_node,
                sleep=sleep,
                monotonic=monotonic,
                deadline=stop_deadline,
            )

        drain = drain_owned_process_tree(
            roots,
            pidfd_open=pidfd_open,
            pidfd_signal=pidfd_signal,
            pidfd_exited=pidfd_exited,
            close_fd=close_fd,
            sleep=sleep,
            monotonic=monotonic,
            deadline=stop_deadline,
            process_table=process_table,
            process_node=process_node,
            before_signal=quiesce_supervisor if roots else None,
        )
        if not roots:
            try:
                quiesce_blocker = quiesce_supervisor()
            except OSError:
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    blocker="autopilot_stop_supervisor_quiesce_failed",
                )
            if quiesce_blocker:
                try:
                    pidfd_signal(process_fd, signal.SIGCONT)
                except (OSError, ProcessLookupError):
                    pass
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    blocker=quiesce_blocker,
                )
        if not drain.complete:
            blocker = drain.blocker or "autopilot_stop_drain_incomplete"
            if supervisor_quiesced:
                try:
                    pidfd_signal(process_fd, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except OSError:
                    blocker = "autopilot_stop_supervisor_resume_failed"
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                drained=drain.drained,
                remaining=drain.remaining,
                blocker=blocker,
            )

        if monotonic() >= stop_deadline or time_module.monotonic() >= backend_deadline:
            try:
                pidfd_signal(process_fd, signal.SIGCONT)
            except (OSError, ProcessLookupError):
                pass
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                drained=drain.drained,
                blocker="autopilot_stop_deadline_exceeded",
            )

        # The supervisor is now unable to begin another cycle. Re-read its
        # synchronous child-start records and task locks to close the interval
        # between the first snapshot and SIGSTOP. A child that started in that
        # interval is drained in this second pass before supervisor termination.
        post_roots, post_blocker, post_diagnostics = collect_owned_stop_roots(
            run_store,
            lock_manager,
            repo=config.repo,
            run_id=identity.run_id,
            process_exists=process_exists,
            birth_identity_lookup=birth_identity_lookup,
            timeout_seconds=max(0.001, backend_deadline - time_module.monotonic()),
        )
        drained_pids = {entry.pid for entry in drain.drained}
        post_roots = tuple(root for root in post_roots if root.pid not in drained_pids)
        retained_lock_diagnostics += post_diagnostics
        if post_blocker:
            try:
                pidfd_signal(process_fd, signal.SIGCONT)
            except (OSError, ProcessLookupError):
                pass
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                drained=drain.drained,
                blocker=post_blocker,
            )
        if monotonic() >= stop_deadline or time_module.monotonic() >= backend_deadline:
            try:
                pidfd_signal(process_fd, signal.SIGCONT)
            except (OSError, ProcessLookupError):
                pass
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                drained=drain.drained,
                blocker="autopilot_stop_deadline_exceeded",
            )
        if post_roots:
            post_drain = drain_owned_process_tree(
                post_roots,
                pidfd_open=pidfd_open,
                pidfd_signal=pidfd_signal,
                pidfd_exited=pidfd_exited,
                close_fd=close_fd,
                sleep=sleep,
                monotonic=monotonic,
                deadline=stop_deadline,
                process_table=process_table,
                process_node=process_node,
            )
            drain = OwnedProcessDrainResult(
                drained=drain.drained + post_drain.drained,
                remaining=post_drain.remaining,
                blocker=post_drain.blocker,
            )
            if not drain.complete:
                blocker = drain.blocker or "autopilot_stop_drain_incomplete"
                try:
                    pidfd_signal(process_fd, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except OSError:
                    blocker = "autopilot_stop_supervisor_resume_failed"
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    drained=drain.drained,
                    remaining=drain.remaining,
                    blocker=blocker,
                )

        supervisor_signal_blocker = ""
        try:
            pidfd_signal(process_fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            supervisor_signal_blocker = "autopilot_stop_signal_failed"
        if supervisor_quiesced:
            try:
                # SIGTERM is pending while the supervisor is stopped. Resuming
                # it lets the installed handler unwind without a scheduling
                # window in which the completed child can trigger a new cycle.
                pidfd_signal(process_fd, signal.SIGCONT)
            except ProcessLookupError:
                pass
            except OSError:
                supervisor_signal_blocker = "autopilot_stop_supervisor_resume_failed"
        if supervisor_signal_blocker:
            return AutopilotStopResult(
                repo=config.repo,
                stopped=False,
                state="blocked",
                run_id=identity.run_id,
                pid=identity.pid,
                drained=drain.drained,
                remaining=(
                    OwnedProcessIdentity(
                        role=OWNED_PROCESS_ROLE_SUPERVISOR,
                        pid=identity.pid,
                        process_group_id=identity.process_group_id,
                        session_id=identity.session_id,
                        process_birth_id=identity.process_birth_id,
                        run_id=identity.run_id,
                    ),
                ),
                blocker=supervisor_signal_blocker,
            )

        process_exited = False
        lock_released = False
        while True:
            if (
                monotonic() >= stop_deadline
                or time_module.monotonic() >= backend_deadline
            ):
                blocker = "autopilot_stop_timeout"
                if process_exited and not lock_released:
                    blocker = "autopilot_stop_backend_release_failed"
                elif not process_exited and lock_released:
                    blocker = "autopilot_stop_process_exit_timeout"
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=False,
                    state="blocked",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    process_exited=process_exited,
                    lock_released=lock_released,
                    drained=drain.drained,
                    remaining=(
                        ()
                        if process_exited
                        else (
                            OwnedProcessIdentity(
                                role=OWNED_PROCESS_ROLE_SUPERVISOR,
                                pid=identity.pid,
                                process_group_id=identity.process_group_id,
                                session_id=identity.session_id,
                                process_birth_id=identity.process_birth_id,
                                run_id=identity.run_id,
                            ),
                        )
                    ),
                    blocker=blocker,
                )
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
            lock_released = supervisor_lock_released(current, identity.run_id)
            if process_exited and lock_released:
                reconciled, reconcile_blockers = reconcile_drained_workers(
                    run_store,
                    lock_manager,
                    repo=config.repo,
                    drained=drain.drained,
                    backend_timeout=lambda: max(
                        0.001,
                        backend_deadline - time_module.monotonic(),
                    ),
                    deadline_expired=lambda: (
                        monotonic() >= stop_deadline
                        or time_module.monotonic() >= backend_deadline
                    ),
                )
                reconcile_blockers = retained_lock_diagnostics + reconcile_blockers
                if reconcile_blockers:
                    return AutopilotStopResult(
                        repo=config.repo,
                        stopped=False,
                        state="blocked",
                        run_id=identity.run_id,
                        pid=identity.pid,
                        process_exited=True,
                        lock_released=True,
                        drained=drain.drained,
                        reconciled_task_ids=reconciled,
                        reconciliation_blockers=reconcile_blockers,
                        blocker=reconcile_blockers[0],
                    )
                append_autopilot_stopped_record(
                    run_store,
                    repo=config.repo,
                    run_id=identity.run_id,
                    pid=identity.pid,
                    stop_mode="operator_verified",
                    signal_number=signal.SIGTERM,
                    drained=drain.drained,
                    reconciled_task_ids=reconciled,
                    reconciliation_blockers=reconcile_blockers,
                )
                return AutopilotStopResult(
                    repo=config.repo,
                    stopped=True,
                    state="stopped",
                    run_id=identity.run_id,
                    pid=identity.pid,
                    process_exited=True,
                    lock_released=True,
                    drained=drain.drained,
                    reconciled_task_ids=reconciled,
                    reconciliation_blockers=reconcile_blockers,
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
                    drained=drain.drained,
                    remaining=(
                        ()
                        if process_exited
                        else (
                            OwnedProcessIdentity(
                                role=OWNED_PROCESS_ROLE_SUPERVISOR,
                                pid=identity.pid,
                                process_group_id=identity.process_group_id,
                                session_id=identity.session_id,
                                process_birth_id=identity.process_birth_id,
                                run_id=identity.run_id,
                            ),
                        )
                    ),
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
    *,
    bound_names: Sequence[str] = (),
) -> tuple[dict[str, str], BinaryIO | None]:
    environment = os.environ.copy()
    environment.pop(AUTOPILOT_RUNTIME_CONTEXT_FD_ENV, None)
    for name, _value in runtime_context:
        environment.pop(name, None)
    # A declared binding is re-resolved by the child from repository config or
    # the transported context; dropping the ambient copy keeps a stale shell
    # export from reaching adapters if that resolution ever changes.
    for name in bound_names:
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
    bound_names: Sequence[str] = (),
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
        runtime_context,
        bound_names=bound_names,
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


PROVIDER_LIMIT_SCAN_MAX_RESULTS = 50


def provider_limit_pause_seconds(
    run_store: RunStore,
    *,
    since: str,
    default_backoff: float,
    now: datetime | None = None,
) -> float | None:
    """Dispatch backoff after a child stopped on a provider limit.

    Scans result records finished at or after ``since`` for a ``provider_limit``
    classification and returns the seconds to pause before the next cycle: the
    advertised reset delay when the recorded message carries one, otherwise
    ``default_backoff``. Returns None when no provider limit occurred this cycle, so
    the supervisor keeps its normal cadence. Pure decision function: it reads
    recorded state and never sleeps.
    """
    pause: float | None = None
    for record in run_store.recent_result_records(
        max_runs=PROVIDER_LIMIT_SCAN_MAX_RESULTS
    ):
        if not is_provider_limit_classification(record.get("classification")):
            continue
        finished_at = str(record.get("finished_at") or "")
        if since and finished_at and finished_at < since:
            continue
        reset_delay = parse_provider_limit_reset_delay(
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

    if kind == TASK_SOURCE_HEALTH_COMMAND_KIND:
        env = build_adapter_environment(env_extra)
    else:
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


# Byte headroom uses absolute warning and hard-stop thresholds because build
# capacity is measured in bytes. Inode pressure retains paired absolute and
# proportional floors to avoid false positives across filesystem sizes.
AUTOPILOT_DISK_WARN_FREE_BYTES = DISK_RESERVE_DEFAULT_WARN_FREE_BYTES
AUTOPILOT_DISK_HARD_STOP_FREE_BYTES = DISK_RESERVE_DEFAULT_HARD_STOP_FREE_BYTES
AUTOPILOT_DISK_MIN_FREE_INODES = DISK_RESERVE_DEFAULT_MIN_FREE_INODES
AUTOPILOT_DISK_MIN_FREE_INODE_FRACTION = DISK_RESERVE_DEFAULT_MIN_FREE_INODE_FRACTION
DISK_HEALTH_OK = "ok"
DISK_HEALTH_WARNING = "warning"
DISK_HEALTH_CRITICAL = "critical"
AUTOPILOT_DISK_CAPACITY_BLOCKER = "autopilot_disk_capacity_low"


@dataclasses.dataclass(frozen=True)
class DiskHealthThresholds:
    """Bounded free-space/inode floors the disk-health check compares against."""

    warn_free_bytes: int = AUTOPILOT_DISK_WARN_FREE_BYTES
    hard_stop_free_bytes: int = AUTOPILOT_DISK_HARD_STOP_FREE_BYTES
    min_free_inodes: int = AUTOPILOT_DISK_MIN_FREE_INODES
    min_free_inode_fraction: float = AUTOPILOT_DISK_MIN_FREE_INODE_FRACTION

    def to_json(self) -> dict[str, object]:
        return {
            "warn_free_bytes": self.warn_free_bytes,
            "hard_stop_free_bytes": self.hard_stop_free_bytes,
            "min_free_inodes": self.min_free_inodes,
            "min_free_inode_fraction": self.min_free_inode_fraction,
        }


DEFAULT_DISK_HEALTH_THRESHOLDS = DiskHealthThresholds()


def disk_health_thresholds_for(config: VibeConfig) -> DiskHealthThresholds:
    """Resolve the effective disk-health floors for a project's cycle.

    Each ``[autopilot.disk_reserve]`` override replaces one native default; an
    unset override keeps the reviewed AUTO-15 value, so a configuration-free
    project's thresholds are unchanged.
    """
    reserve = config.autopilot.disk_reserve
    return DiskHealthThresholds(
        warn_free_bytes=reserve.effective_warn_free_bytes,
        hard_stop_free_bytes=reserve.effective_hard_stop_free_bytes,
        min_free_inodes=reserve.effective_min_free_inodes,
        min_free_inode_fraction=reserve.effective_min_free_inode_fraction,
    )


@dataclasses.dataclass(frozen=True)
class DiskCapacitySample:
    """A capacity reading for one filesystem path.

    ``total_inodes == 0`` marks a filesystem that does not expose an inode
    count (some FUSE/network mounts); inode pressure is treated as not
    applicable there rather than as exhaustion.
    """

    path: str
    total_bytes: int
    free_bytes: int
    total_inodes: int
    free_inodes: int
    mount: str = ""

    @property
    def free_bytes_fraction(self) -> float:
        if self.total_bytes <= 0:
            return 1.0
        return self.free_bytes / self.total_bytes

    @property
    def free_inodes_fraction(self) -> float:
        if self.total_inodes <= 0:
            return 1.0
        return self.free_inodes / self.total_inodes

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "mount": self.mount,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "free_bytes_fraction": self.free_bytes_fraction,
            "total_inodes": self.total_inodes,
            "free_inodes": self.free_inodes,
            "free_inodes_fraction": self.free_inodes_fraction,
        }


# Reads a filesystem capacity sample for a path. Defaults to ``os.statvfs``;
# injected in tests so acceptance never depends on real disk state.
DiskCapacityProbe = Callable[[Path], DiskCapacitySample]


def statvfs_capacity_probe(path: Path) -> DiskCapacitySample:
    # ``os.statvfs`` exposes inode accounting but is POSIX-only. On platforms
    # without it (Windows), fall back to the portable ``shutil.disk_usage`` for
    # byte capacity and mark inodes as not accounted (``total_inodes == 0``), so
    # the OS-independent cycle keeps checking free space instead of aborting.
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        stat = statvfs(path)
        return DiskCapacitySample(
            path=str(path),
            mount=str(filesystem_mount_for(path)),
            total_bytes=stat.f_frsize * stat.f_blocks,
            free_bytes=stat.f_frsize * stat.f_bavail,
            total_inodes=stat.f_files,
            free_inodes=stat.f_favail,
        )
    usage = shutil.disk_usage(path)
    return DiskCapacitySample(
        path=str(path),
        mount=str(filesystem_mount_for(path)),
        total_bytes=usage.total,
        free_bytes=usage.free,
        total_inodes=0,
        free_inodes=0,
    )


def filesystem_mount_for(path: Path) -> Path:
    current = path.resolve()
    while not current.exists() and current.parent != current:
        current = current.parent
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        mountinfo = ""
    candidates: list[Path] = []
    for line in mountinfo.splitlines():
        fields = line.partition(" - ")[0].split()
        if len(fields) < 5:
            continue
        mount = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        try:
            current.relative_to(mount)
        except ValueError:
            continue
        candidates.append(mount)
    if candidates:
        return max(candidates, key=lambda candidate: len(candidate.parts))
    device = current.stat().st_dev
    while current.parent != current:
        parent = current.parent
        if parent.stat().st_dev != device:
            break
        current = parent
    return current


def _evaluate_capacity_pressure(
    sample: DiskCapacitySample, thresholds: DiskHealthThresholds
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    warnings: list[str] = []
    reasons: list[str] = []
    if sample.free_bytes < thresholds.hard_stop_free_bytes:
        reasons.append("free_bytes")
    elif sample.free_bytes < thresholds.warn_free_bytes:
        warnings.append("free_bytes")
    if (
        sample.total_inodes > 0
        and sample.free_inodes < thresholds.min_free_inodes
        and sample.free_inodes_fraction < thresholds.min_free_inode_fraction
    ):
        reasons.append("free_inodes")
    return tuple(warnings), tuple(reasons)


@dataclasses.dataclass(frozen=True)
class DiskCapacityTarget:
    """One probed path, its reading, and any exhausted-reserve reasons."""

    label: str
    path: str
    sample: DiskCapacitySample | None
    warnings: tuple[str, ...]
    pressure: tuple[str, ...]
    error: str

    @property
    def critical(self) -> bool:
        return bool(self.pressure)

    @property
    def warning(self) -> bool:
        return bool(self.warnings)

    def to_json(self) -> dict[str, object]:
        return {
            "label": self.label,
            "path": self.path,
            "sample": self.sample.to_json() if self.sample is not None else None,
            "warnings": list(self.warnings),
            "pressure": list(self.pressure),
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True)
class DiskHealthCycleResult:
    """Outcome of one cycle's native disk-health check (PRD-AUT-011/012).

    Mirrors the maintenance-command-result record shape: a single typed
    ``autopilot_disk_health`` record carries the thresholds and per-target
    evidence. A probe error is a non-blocking observation, not a capacity
    blocker: an unreadable path can never be a *genuine* exhaustion signal, and
    the non-destructive boundary (PRD-AUT-006) forbids acting on ambiguity.
    """

    cycle_id: str
    thresholds: DiskHealthThresholds
    targets: tuple[DiskCapacityTarget, ...]

    @property
    def status(self) -> str:
        if any(target.critical for target in self.targets):
            return DISK_HEALTH_CRITICAL
        if any(target.warning for target in self.targets):
            return DISK_HEALTH_WARNING
        return DISK_HEALTH_OK

    @property
    def blocker(self) -> str:
        if self.status == DISK_HEALTH_CRITICAL:
            return AUTOPILOT_DISK_CAPACITY_BLOCKER
        return ""

    @property
    def probe_errors(self) -> int:
        return sum(1 for target in self.targets if target.error)

    @property
    def blocker_details(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "code": AUTOPILOT_DISK_CAPACITY_BLOCKER,
                "mount": target.sample.mount or target.sample.path,
                "free_bytes": target.sample.free_bytes,
                "free_inodes": target.sample.free_inodes,
                "path": target.path,
                "measured_path": target.sample.path,
                "pressure": list(target.pressure),
            }
            for target in self.targets
            if target.critical and target.sample is not None
        )

    def to_status_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "blocker": self.blocker,
            "blocker_details": [dict(detail) for detail in self.blocker_details],
            "thresholds": self.thresholds.to_json(),
            "targets": [target.to_json() for target in self.targets],
        }

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_DISK_HEALTH_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "status": self.status,
            "blocker": self.blocker,
            "blocker_details": [dict(detail) for detail in self.blocker_details],
            "thresholds": self.thresholds.to_json(),
            "targets": [target.to_json() for target in self.targets],
        }


DiskHealthRunner = Callable[..., DiskHealthCycleResult]


def run_disk_health(
    config: VibeConfig,
    *,
    cycle_id: str,
    probe: DiskCapacityProbe | None = None,
    thresholds: DiskHealthThresholds | None = None,
) -> DiskHealthCycleResult:
    """Run the native, read-only disk-health check for the cycle.

    Probes the filesystem where native worker worktrees and their build outputs
    are provisioned. The probe is dependency-injected so tests never depend on
    real disk state. Thresholds default to the project's configured
    ``[autopilot.disk_reserve]`` floors (native defaults when unset); an
    explicit ``thresholds`` argument overrides them for focused tests. This step
    never deletes, truncates, or otherwise mutates anything: it only reports
    pressure and, on a genuine capacity blocker, signals the cycle to withhold
    launch (PRD-AUT-006).
    """
    if thresholds is None:
        thresholds = disk_health_thresholds_for(config)
    if probe is None:
        probe = statvfs_capacity_probe
    targets: list[DiskCapacityTarget] = []
    primary_repo = git_main_worktree_path(config.repo) or config.repo
    worktree_root = primary_repo.parent / f"{primary_repo.name}-worktrees"
    probe_path = worktree_root
    while not probe_path.exists() and probe_path.parent != probe_path:
        probe_path = probe_path.parent
    for label, path, measured_path in (("worktrees", worktree_root, probe_path),):
        try:
            sample = probe(measured_path)
        except OSError as error:
            targets.append(
                DiskCapacityTarget(
                    label=label,
                    path=str(path),
                    sample=None,
                    warnings=(),
                    pressure=(),
                    error=str(error),
                )
            )
            continue
        warnings, pressure = _evaluate_capacity_pressure(sample, thresholds)
        targets.append(
            DiskCapacityTarget(
                label=label,
                path=str(path),
                sample=sample,
                warnings=warnings,
                pressure=pressure,
                error="",
            )
        )
    return DiskHealthCycleResult(
        cycle_id=cycle_id,
        thresholds=thresholds,
        targets=tuple(targets),
    )


# Native "what landed" git-log summary (PRD-AUT-011/012). A configuration-free
# loop summarizes the commits merged into ``main`` since the previous cycle
# without requiring a project ``summary_command``. The span is the previous
# cycle's recorded ``main`` ref (read from the prior ``autopilot_cycle`` record)
# to the current ``main`` ref from status. The step is read-only: it runs
# ``git log`` and never mutates the repository.
LANDED_SUMMARY_MAX_COMMITS = 50
# Commit subjects are journaled verbatim up to this length; longer subjects are
# truncated so a single pathological commit message cannot bloat the record.
LANDED_SUMMARY_SUBJECT_LIMIT = 200
LANDED_SUMMARY_FIELD_SEP = "\x1f"
LANDED_SUMMARY_ACTION_PREFIX = "cycle_summary:"
CYCLE_SUMMARY_BOOTSTRAP = "bootstrap"
CYCLE_SUMMARY_LANDED = "landed"
CYCLE_SUMMARY_UNCHANGED = "unchanged"
CYCLE_SUMMARY_UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class LandedCommit:
    """One commit that landed on ``main`` between two cycle refs."""

    commit: str
    subject: str

    def to_json(self) -> dict[str, object]:
        return {"commit": self.commit, "subject": self.subject}


@dataclasses.dataclass(frozen=True)
class CycleLandedSummaryResult:
    """Outcome of one cycle's native "what landed" git-log summary.

    Mirrors the maintenance-command-result record shape: a single typed
    ``autopilot_cycle_summary`` record carries the derived span and bounded
    per-commit evidence. The step is non-blocking and never mutates the
    repository; ``bootstrap`` marks the first cycle (no prior recorded ref) and
    ``error`` marks an unreadable span, both of which record rather than fail.
    """

    cycle_id: str
    since_ref: str
    until_ref: str
    commits: tuple[LandedCommit, ...] = ()
    truncated: bool = False
    bootstrap: bool = False
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return CYCLE_SUMMARY_UNAVAILABLE
        if self.bootstrap:
            return CYCLE_SUMMARY_BOOTSTRAP
        if self.commits:
            return CYCLE_SUMMARY_LANDED
        return CYCLE_SUMMARY_UNCHANGED

    @property
    def commit_count(self) -> int:
        return len(self.commits)

    @property
    def action(self) -> str:
        # A trailing ``+`` marks a bounded, truncated span so a reader can tell
        # "50 commits" from "at least 50 commits".
        suffix = "+" if self.truncated else ""
        return (
            f"{LANDED_SUMMARY_ACTION_PREFIX}{self.status}:{self.commit_count}{suffix}"
        )

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_CYCLE_SUMMARY_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "status": self.status,
            "since_ref": self.since_ref,
            "until_ref": self.until_ref,
            "commit_count": self.commit_count,
            "truncated": self.truncated,
            "bootstrap": self.bootstrap,
            "error": self.error,
            "commits": [commit.to_json() for commit in self.commits],
        }


# Reads the commits reachable from ``until_ref`` but not ``since_ref``. Defaults
# to ``git log``; injected in tests so acceptance never depends on real history.
GitLandedProbe = Callable[..., tuple[tuple[LandedCommit, ...], bool, str]]


def git_landed_commits(
    repo: Path,
    *,
    since_ref: str,
    until_ref: str,
    max_commits: int,
) -> tuple[tuple[LandedCommit, ...], bool, str]:
    """Commits on ``since_ref..until_ref``, newest first, bounded and read-only.

    Requests one commit over ``max_commits`` so truncation is detectable without
    a second call. Returns ``(commits, truncated, error)``; a git failure (an
    unknown or rewritten ref) yields an empty tuple and the error text rather
    than raising, so the cycle records an unavailable span instead of aborting.
    """

    output, error = git_text(
        repo,
        "log",
        "--no-color",
        f"--max-count={max_commits + 1}",
        f"--format=%H{LANDED_SUMMARY_FIELD_SEP}%s",
        f"{since_ref}..{until_ref}",
    )
    if error:
        return (), False, error
    commits: list[LandedCommit] = []
    for line in output.splitlines():
        if not line:
            continue
        commit_hash, _sep, subject = line.partition(LANDED_SUMMARY_FIELD_SEP)
        commits.append(
            LandedCommit(
                commit=commit_hash[:12],
                subject=subject.strip()[:LANDED_SUMMARY_SUBJECT_LIMIT],
            )
        )
    truncated = len(commits) > max_commits
    return tuple(commits[:max_commits]), truncated, ""


CycleSummaryRunner = Callable[..., CycleLandedSummaryResult]


def latest_cycle_main_ref(run_store: RunStore) -> str | None:
    """The ``main`` ref recorded by the most recent ``autopilot_cycle`` record.

    The cycle status carries only the current ref, so the previous cycle's ref
    is read from its journaled ``git.main_head``. Returns ``None`` when no prior
    cycle exists (the first cycle); an empty string when a prior cycle exists but
    its record lacks a resolved main head. The two are deliberately distinct: the
    first is a bootstrap, the second an unavailable prior endpoint.
    """

    for record in reversed(run_store.read_records()):
        if record.get("record_type") != AUTOPILOT_CYCLE_RECORD_TYPE:
            continue
        git = record.get("git")
        if isinstance(git, dict):
            return str(git.get("main_head") or "")
        return ""
    return None


def run_cycle_summary(
    config: VibeConfig,
    *,
    cycle_id: str,
    prior_main_ref: str | None,
    current_main_ref: str,
    max_commits: int = LANDED_SUMMARY_MAX_COMMITS,
    git_landed: GitLandedProbe = git_landed_commits,
) -> CycleLandedSummaryResult:
    """Summarize commits merged into ``main`` since the previous cycle.

    Read-only: derives the span from the previous cycle's recorded ``main`` ref
    to the current one and journals a bounded commit summary. The first cycle
    (``prior_main_ref is None``) records a bootstrap summary regardless of the
    current ref, since there is no span to derive; an unresolved current ref, or
    a prior cycle whose recorded endpoint is empty, records an unavailable
    summary. None of these fail the cycle (PRD-AUT-006/011/012).
    """

    # The first cycle has no span to derive, so it bootstraps even when the
    # current ref cannot be resolved: the acceptance ties bootstrap to the
    # absence of a prior recorded ref, not to current-ref availability.
    if prior_main_ref is None:
        return CycleLandedSummaryResult(
            cycle_id=cycle_id,
            since_ref="",
            until_ref=current_main_ref,
            bootstrap=True,
        )
    if not current_main_ref:
        return CycleLandedSummaryResult(
            cycle_id=cycle_id,
            since_ref=prior_main_ref,
            until_ref=current_main_ref,
            error="main_ref_unavailable",
        )
    if not prior_main_ref:
        # A prior cycle exists but recorded no resolved endpoint, so no span can
        # be derived even though the current ref resolved.
        return CycleLandedSummaryResult(
            cycle_id=cycle_id,
            since_ref=prior_main_ref,
            until_ref=current_main_ref,
            error="prior_main_ref_unavailable",
        )
    if prior_main_ref == current_main_ref:
        return CycleLandedSummaryResult(
            cycle_id=cycle_id,
            since_ref=prior_main_ref,
            until_ref=current_main_ref,
        )
    commits, truncated, error = git_landed(
        config.repo,
        since_ref=prior_main_ref,
        until_ref=current_main_ref,
        max_commits=max_commits,
    )
    return CycleLandedSummaryResult(
        cycle_id=cycle_id,
        since_ref=prior_main_ref,
        until_ref=current_main_ref,
        commits=commits,
        truncated=truncated,
        error=error,
    )


NATIVE_TROUBLESHOOT_WINDOW_RECORDS = 200
NATIVE_TROUBLESHOOT_STATUS_RECORDS = 100
NATIVE_TROUBLESHOOT_RECURRENCE_THRESHOLD = 3
NATIVE_TROUBLESHOOT_ACTION_PREFIX = "native_troubleshoot:"
TROUBLESHOOT_OBSERVATION = "observation"
TROUBLESHOOT_BLOCKER = "blocker"


@dataclasses.dataclass(frozen=True)
class NativeTroubleshootFinding:
    kind: str
    level: str
    code: str
    task_id: str
    count: int
    threshold: int
    reason: str

    @classmethod
    def from_json(cls, payload: object) -> NativeTroubleshootFinding | None:
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("kind") or "")
        code = str(payload.get("code") or "")
        task_id = str(payload.get("task_id") or "")
        if not kind or not code or not task_id:
            return None
        level = str(payload.get("level") or TROUBLESHOOT_OBSERVATION)
        if kind == "restart_budget_exhausted":
            level = TROUBLESHOOT_OBSERVATION
        if level not in {TROUBLESHOOT_OBSERVATION, TROUBLESHOOT_BLOCKER}:
            return None
        return cls(
            kind=kind,
            level=level,
            code=code,
            task_id=task_id,
            count=int_value(payload.get("count")) or 0,
            threshold=int_value(payload.get("threshold")) or 0,
            reason=str(payload.get("reason") or ""),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "level": self.level,
            "code": self.code,
            "task_id": self.task_id,
            "count": self.count,
            "threshold": self.threshold,
            "reason": self.reason,
        }


@dataclasses.dataclass(frozen=True)
class NativeTroubleshootCycleResult:
    cycle_id: str
    records_scanned: int
    window_records: int
    findings: tuple[NativeTroubleshootFinding, ...] = ()

    @classmethod
    def from_record(
        cls, record: Mapping[str, object]
    ) -> NativeTroubleshootCycleResult | None:
        if record.get("record_type") != AUTOPILOT_TROUBLESHOOT_RECORD_TYPE:
            return None
        raw_findings = record.get("findings")
        findings = (
            tuple(
                finding
                for payload in raw_findings
                if (finding := NativeTroubleshootFinding.from_json(payload)) is not None
            )
            if isinstance(raw_findings, list)
            else ()
        )
        return cls(
            cycle_id=str(record.get("cycle_id") or ""),
            records_scanned=int_value(record.get("records_scanned")) or 0,
            window_records=int_value(record.get("window_records")) or 0,
            findings=findings,
        )

    @property
    def observations(self) -> tuple[str, ...]:
        return tuple(
            finding.code
            for finding in self.findings
            if finding.level == TROUBLESHOOT_OBSERVATION
        )

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            finding.code
            for finding in self.findings
            if finding.level == TROUBLESHOOT_BLOCKER
        )

    @property
    def status(self) -> str:
        if self.blockers:
            return "blocked"
        if self.observations:
            return "observed"
        return "ok"

    @property
    def action(self) -> str:
        return (
            f"{NATIVE_TROUBLESHOOT_ACTION_PREFIX}"
            f"observations={len(self.observations)}:"
            f"blockers={len(self.blockers)}"
        )

    def to_record(self, repo: Path) -> dict[str, object]:
        return {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_TROUBLESHOOT_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "repo": str(repo),
            "cycle_id": self.cycle_id,
            "status": self.status,
            "records_scanned": self.records_scanned,
            "window_records": self.window_records,
            "observations": list(self.observations),
            "blockers": list(self.blockers),
            "findings": [finding.to_json() for finding in self.findings],
        }


NativeTroubleshootRunner = Callable[..., NativeTroubleshootCycleResult]


def latest_native_troubleshoot(
    run_store: RunStore,
) -> NativeTroubleshootCycleResult:
    records = run_store.recent_records(max_runs=NATIVE_TROUBLESHOOT_STATUS_RECORDS)
    for record in reversed(records):
        result = NativeTroubleshootCycleResult.from_record(record)
        if result is not None:
            return result
    return NativeTroubleshootCycleResult(
        cycle_id="",
        records_scanned=0,
        window_records=NATIVE_TROUBLESHOOT_WINDOW_RECORDS,
    )


def run_native_troubleshoot(
    *,
    cycle_id: str,
    run_store: RunStore,
    window_records: int = NATIVE_TROUBLESHOOT_WINDOW_RECORDS,
    recurrence_threshold: int = NATIVE_TROUBLESHOOT_RECURRENCE_THRESHOLD,
) -> NativeTroubleshootCycleResult:
    """Derive bounded, read-only trouble findings from recent journal records."""

    if window_records <= 0:
        raise ValueError("troubleshoot record window must be positive")
    if recurrence_threshold <= 0:
        raise ValueError("troubleshoot recurrence threshold must be positive")

    records = run_store.recent_records_matching(
        record_types=frozenset(
            {
                None,
                RUN_RECORD_TYPE,
                TASK_RESTART_RECORD_TYPE,
                WORKSPACE_CLAIM_RECORD_TYPE,
                WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
            }
        ),
        max_runs=window_records,
    )
    failures: dict[tuple[str, str], set[str]] = {}
    claim_mismatches: dict[tuple[str, str], set[str]] = {}
    exhausted_restarts: dict[str, str] = {}
    for index, record in enumerate(records):
        record_type = record.get("record_type")
        task_id = str(record.get("task_id") or "")
        if not task_id:
            continue
        if record_type in {None, RUN_RECORD_TYPE}:
            classification = str(
                record.get("classification") or record.get("status") or ""
            )
            if classification == "completed":
                failures = {
                    key: value for key, value in failures.items() if key[0] != task_id
                }
                exhausted_restarts.pop(task_id, None)
            elif classification in {"failed", "blocked"}:
                run_identity = str(record.get("run_id") or f"record:{index}")
                failures.setdefault((task_id, classification), set()).add(run_identity)
        elif record_type == TASK_RESTART_RECORD_TYPE:
            if record.get("exhausted") is True:
                exhausted_restarts[task_id] = str(
                    record.get("reason") or "restart_budget_exhausted"
                )
        elif record_type == WORKSPACE_CLAIM_RECORD_TYPE:
            claim_mismatches = {
                key: value
                for key, value in claim_mismatches.items()
                if key[0] != task_id
            }
        elif record_type == WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE:
            reason = str(record.get("reason") or "mismatch")
            key = (task_id, reason)
            run_identity = str(record.get("run_id") or f"record:{index}")
            claim_mismatches.setdefault(key, set()).add(run_identity)

    findings: list[NativeTroubleshootFinding] = []
    for (task_id, classification), run_ids in sorted(failures.items()):
        count = len(run_ids)
        if count < recurrence_threshold:
            continue
        findings.append(
            NativeTroubleshootFinding(
                kind="recurring_task_failure",
                level=TROUBLESHOOT_OBSERVATION,
                code=f"recurring_task_failure:{task_id}:{classification}",
                task_id=task_id,
                count=count,
                threshold=recurrence_threshold,
                reason=classification,
            )
        )
    for task_id, reason in sorted(exhausted_restarts.items()):
        findings.append(
            NativeTroubleshootFinding(
                kind="restart_budget_exhausted",
                level=TROUBLESHOOT_OBSERVATION,
                code=f"restart_budget_exhausted:{task_id}",
                task_id=task_id,
                count=1,
                threshold=1,
                reason=reason,
            )
        )
    for (task_id, reason), run_ids in sorted(claim_mismatches.items()):
        count = len(run_ids)
        if count < recurrence_threshold:
            continue
        findings.append(
            NativeTroubleshootFinding(
                kind="persistent_claim_mismatch",
                level=TROUBLESHOOT_OBSERVATION,
                code=f"persistent_claim_mismatch:{task_id}:{reason}",
                task_id=task_id,
                count=count,
                threshold=recurrence_threshold,
                reason=reason,
            )
        )
    return NativeTroubleshootCycleResult(
        cycle_id=cycle_id,
        records_scanned=len(records),
        window_records=window_records,
        findings=tuple(findings),
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
        "mechanical safety guardrails (contained by local and remote main, "
        "clean, unambiguously owned by a completed run, and not claimed by a "
        "live or stale run); the executor re-checks those guardrails independently, so a reap "
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
    evidence_collector: Callable[[], list[WorktreeDispositionEvidence]] | None = None,
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

    def collect_evidence() -> list[WorktreeDispositionEvidence]:
        if evidence_collector is not None:
            return evidence_collector()
        return collect_worktree_disposition_evidence(
            lock_manager,
            run_store,
            repo=config.repo,
            main_branch=config.main_branch,
            process_exists=process_exists,
            ignored_dirty_paths=(config.state_path,),
        )

    evidence = collect_evidence()
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

    def revalidate(
        approved: WorktreeDispositionEvidence,
        action: str,
    ) -> tuple[str, ...]:
        refreshed = {item.path.resolve(): item for item in collect_evidence()}
        current = refreshed.get(approved.path.resolve())
        if action == "worktree_remove":
            if current != approved:
                return (KEEP_EVIDENCE_CHANGED,)
            return current.keep_guardrails
        return worktree_branch_delete_revalidation_guardrails(
            approved,
            refreshed.values(),
            lock_manager=lock_manager,
            run_store=run_store,
            repo=config.repo,
            main_branch=config.main_branch,
            process_exists=process_exists,
            ignored_dirty_paths=(config.state_path,),
        )

    outcomes = execute_worktree_disposition(
        evidence,
        decisions,
        remove_worktree=remover,
        delete_branch=deleter,
        revalidate=revalidate,
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
NATIVE_PLANNING_PROVIDER_LIMIT_STATUS = "provider_limit"


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
    agent_error_kind: str = ""
    provider_limit_reset_text: str = ""
    provider_limit_pause_seconds: float | None = None

    @property
    def provider_limit(self) -> bool:
        return self.status == NATIVE_PLANNING_PROVIDER_LIMIT_STATUS

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
            "agent_error_kind": self.agent_error_kind,
            "provider_limit_reset_text": self.provider_limit_reset_text,
            "provider_limit_pause_seconds": self.provider_limit_pause_seconds,
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
    # Task identities that appeared in the authoritative task source across the
    # launch, including tasks claimed before the post-launch snapshot.
    # ``None`` means the post-launch snapshot was unreadable, so nothing can be
    # claimed about what planning created; an empty tuple means it created
    # nothing. Identities are used instead of the runnable count delta because
    # concurrent claims and completions move that count independently of what
    # this planning launch authored.
    created_task_ids: tuple[str, ...] = ()
    created_count: int | None = 0

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
            "created_task_ids": list(self.created_task_ids),
            "created_count": self.created_count,
        }


@dataclasses.dataclass(frozen=True)
class NativePlanningCycleResult:
    decision: NativePlanningDecision
    worker: NativePlanningWorkerResult
    stats: dict[str, object] = dataclasses.field(default_factory=dict)
    model_provider: str = "unknown"
    model_id: str = "unknown"
    attribution_diagnostics: tuple[str, ...] = ()


PLANNING_OUTCOME_PRODUCTIVE = "productive"
PLANNING_OUTCOME_INVALID_PLAN = "invalid_plan"
PLANNING_OUTCOME_NO_TASKS = "no_tasks"
PLANNING_OUTCOME_ZERO_CREATED = "zero_created"
PLANNING_OUTCOME_PROVIDER_LIMIT = "provider_limit"
PLANNING_OUTCOME_WORKER_ERROR = "worker_error"
PLANNING_OUTCOME_TASK_SOURCE_ERROR = "task_source_error"
PLANNING_OUTCOME_ANALYSIS_ERROR = "analysis_error"

# Why the analysis stage produced no usable decision. Only ``invalid_plan``
# says anything about planning's ability to produce work; the rest are
# infrastructure faults that must stay out of the unproductive streak.
PLANNING_ERROR_INVALID_PLAN = "invalid_plan"
PLANNING_ERROR_EXECUTABLE_RESOLUTION = "executable_resolution"
PLANNING_ERROR_OS_ERROR = "os_error"
PLANNING_ERROR_SUBPROCESS = "subprocess_error"
PLANNING_ERROR_PROVIDER_LIMIT = "provider_limit"
# The three outcomes that spent provider budget and left the board no more
# runnable than before. Everything else is either productive or inconclusive.
PLANNING_UNPRODUCTIVE_OUTCOMES = frozenset(
    {
        PLANNING_OUTCOME_INVALID_PLAN,
        PLANNING_OUTCOME_NO_TASKS,
        PLANNING_OUTCOME_ZERO_CREATED,
    }
)


def classify_planning_outcome(result: NativePlanningCycleResult) -> str:
    """Name what one native planning launch actually achieved.

    Ordered so the inconclusive outcomes win over the unproductive ones: a
    provider limit, a crashed worker, or an unreadable task source says nothing
    about whether planning *can* produce work, and must not be charged to the
    unproductive streak that gates the spend backoff.
    """
    decision = result.decision
    worker = result.worker
    if decision.provider_limit or worker.status == "skipped_provider_limit":
        return PLANNING_OUTCOME_PROVIDER_LIMIT
    if decision.agent_error:
        if decision.agent_error_kind == PLANNING_ERROR_INVALID_PLAN:
            return PLANNING_OUTCOME_INVALID_PLAN
        return PLANNING_OUTCOME_ANALYSIS_ERROR
    if not decision.should_plan:
        return PLANNING_OUTCOME_NO_TASKS
    if not worker.attempted:
        return PLANNING_OUTCOME_WORKER_ERROR
    if worker.task_source_error or worker.created_count is None:
        return PLANNING_OUTCOME_TASK_SOURCE_ERROR
    if worker.status != "completed":
        return PLANNING_OUTCOME_WORKER_ERROR
    if worker.created_count > 0:
        return PLANNING_OUTCOME_PRODUCTIVE
    return PLANNING_OUTCOME_ZERO_CREATED


def planning_provider_launched(result: NativePlanningCycleResult) -> bool:
    """Whether this cycle actually spent provider budget.

    The rolling-day ceiling is a spend ceiling, so it counts launches that
    reached a provider. Failing to resolve the agent executable never reaches
    one and must not consume a day's planning budget; everything else -
    including a provider limit, a crash, or an unreadable task source - either
    reached the provider or cannot be proven not to have.
    """
    if result.worker.started:
        return True
    return result.decision.agent_error_kind != PLANNING_ERROR_EXECUTABLE_RESOLUTION


def planning_source_fingerprint(queue: TaskQueueStatus) -> str:
    """A compact stamp of the task-source state planning was asked to act on.

    Two planning attempts that saw the same fingerprint saw the same board, so
    repeating the second one cannot plausibly produce a different result. A
    changed fingerprint is the "task source materially changed" signal that
    releases the unproductive-outcome backoff.

    Built from every task's identity and content rather than lifecycle status
    or status counters: swapping one task for another keeps every count
    identical but is a genuinely different board, while unrelated workers
    claiming and finishing tasks must not manufacture new planning evidence.
    """
    source_tasks = queue.source_tasks or queue.runnable_tasks
    identity = "\n".join(
        sorted(
            json.dumps(
                {key: value for key, value in task.items() if key != "status"},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for task in source_tasks
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{len(source_tasks)}:{digest}"


PLANNING_LAUNCH_WINDOW_SECONDS = 86400.0


@dataclasses.dataclass(frozen=True)
class PlanningBackoff:
    reason: str
    outcome: str
    attempts: int
    launches_in_window: int
    remaining_seconds: float

    @property
    def action(self) -> str:
        return (
            f"{PLANNING_BACKOFF_ACTION}:{self.reason}:{self.outcome}:"
            f"attempts={self.attempts}:launches={self.launches_in_window}:"
            f"{self.remaining_seconds:.0f}s"
        )


PLANNING_RECORD_TYPES = (
    AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
    AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
)


def _planning_records(run_store: RunStore, repo: Path) -> list[dict[str, object]]:
    return [
        record
        for record in run_store.read_records()
        if record.get("record_type") in PLANNING_RECORD_TYPES
        and str(record.get("repo") or "") == str(repo)
    ]


def _parse_record_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def planning_outcome_backoff(
    run_store: RunStore,
    *,
    repo: Path,
    fingerprint: str,
    backoff_seconds: float,
    max_launches_per_day: int,
    unproductive_threshold: int,
    now: datetime | None = None,
) -> PlanningBackoff | None:
    """How long to withhold native planning after unproductive launches.

    Two independent gates, both read from recorded outcomes; the later deadline
    wins:

    * ``unproductive_outcomes`` - ``unproductive_threshold`` consecutive
      invalid/no-task/zero-created launches hold planning for
      ``backoff_seconds``. A productive launch clears the streak, and so does a
      task source whose fingerprint moved since the last unproductive launch:
      a changed board is new evidence, so planning gets to look again.
    * ``daily_launch_cap`` - a hard ceiling of ``max_launches_per_day`` launches
      in a rolling day. This is a spend ceiling, not an evidence gate, so a
      changed fingerprint does *not* lift it; it expires only when the oldest
      launch in the window ages out. It counts durable pre-launch records, so a
      launch that was interrupted or crashed before its outcome was appended
      still consumes a day's budget.

    Pure decision function: reads recorded state, returns seconds, never sleeps.
    Returns None when planning may run now.
    """
    moment = now if now is not None else datetime.now(UTC)
    records = _planning_records(run_store, repo)
    if not records:
        return None

    deadlines: list[tuple[float, str, str, int, int]] = []

    outcomes = [
        record
        for record in records
        if record.get("record_type") == AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE
    ]
    streak: list[dict[str, object]] = []
    for record in reversed(outcomes):
        if str(record.get("fingerprint") or "") != fingerprint:
            break
        outcome = str(record.get("outcome") or "")
        if outcome == PLANNING_OUTCOME_PRODUCTIVE:
            break
        if outcome not in PLANNING_UNPRODUCTIVE_OUTCOMES:
            # Inconclusive: neither evidence of futility nor of progress.
            continue
        streak.append(record)

    # One entry per planning cycle, timed by its earliest durable record. A
    # cycle whose outcome record proves the provider was never reached is not
    # spend and does not consume the ceiling; a cycle with no outcome record at
    # all crashed mid-launch and is counted, because it cannot be shown free.
    launch_times: list[datetime] = []
    cycle_times: dict[str, datetime] = {}
    cycle_free: dict[str, bool] = {}
    for record in records:
        occurred = _parse_record_time(record.get("occurred_at"))
        if occurred is None:
            continue
        cycle_id = str(record.get("cycle_id") or "")
        key = cycle_id or f"\x00{len(cycle_times)}"
        previous = cycle_times.get(key)
        if previous is None or occurred < previous:
            cycle_times[key] = occurred
        if record.get("record_type") == AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE:
            cycle_free[key] = record.get("provider_launched") is False
    for key, occurred in cycle_times.items():
        if cycle_free.get(key):
            continue
        if (moment - occurred).total_seconds() < PLANNING_LAUNCH_WINDOW_SECONDS:
            launch_times.append(occurred)
    launches_in_window = len(launch_times)

    if streak and len(streak) >= unproductive_threshold and backoff_seconds > 0:
        latest = streak[0]
        occurred = _parse_record_time(latest.get("occurred_at"))
        if occurred is not None:
            remaining = backoff_seconds - (moment - occurred).total_seconds()
            if remaining > 0:
                deadlines.append(
                    (
                        remaining,
                        "unproductive_outcomes",
                        str(latest.get("outcome") or ""),
                        len(streak),
                        launches_in_window,
                    )
                )

    if max_launches_per_day > 0 and launches_in_window >= max_launches_per_day:
        launch_times.sort()
        oldest_counted = launch_times[-max_launches_per_day]
        remaining = (
            PLANNING_LAUNCH_WINDOW_SECONDS - (moment - oldest_counted).total_seconds()
        )
        if remaining > 0:
            deadlines.append(
                (
                    remaining,
                    "daily_launch_cap",
                    str(outcomes[-1].get("outcome") or "") if outcomes else "",
                    len(streak),
                    launches_in_window,
                )
            )

    if not deadlines:
        return None
    remaining, reason, outcome, attempts, launches = max(deadlines)
    return PlanningBackoff(
        reason=reason,
        outcome=outcome,
        attempts=attempts,
        launches_in_window=launches,
        remaining_seconds=remaining,
    )


def planning_launch_record(
    repo: Path, *, cycle_id: str, fingerprint: str
) -> dict[str, object]:
    """Durable evidence that a planning launch was about to spend budget.

    Appended before the analysis agent runs so the rolling-day ceiling still
    counts a launch that is interrupted or crashed before it can classify its
    own outcome.
    """
    return {
        "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
        "record_type": AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
        "occurred_at": utc_now_iso(),
        "repo": str(repo),
        "cycle_id": cycle_id,
        "fingerprint": fingerprint,
    }


def planning_outcome_record(
    repo: Path,
    *,
    cycle_id: str,
    outcome: str,
    fingerprint: str,
    runnable_before: int,
    runnable_after: int | None,
    created_count: int | None = None,
    created_task_ids: Sequence[str] = (),
    provider_launched: bool = True,
    model_provider: str = "unknown",
    model_id: str = "unknown",
    attribution_diagnostics: Sequence[str] = (),
    stats: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized_provider, provider_rejected = normalize_provider_label(model_provider)
    normalized_model, model_rejected = normalize_model_label(model_id)
    record: dict[str, object] = {
        "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
        "record_type": AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
        "occurred_at": utc_now_iso(),
        "repo": str(repo),
        "cycle_id": cycle_id,
        "outcome": outcome,
        "fingerprint": fingerprint,
        "runnable_before": runnable_before,
        "runnable_after": runnable_after,
        "created_count": created_count,
        "created_task_ids": list(created_task_ids),
        "provider_launched": provider_launched,
        "model_provider": normalized_provider,
        "model_id": normalized_model,
    }
    rejected_fields = list(
        dict.fromkeys(
            field for field in attribution_diagnostics if field in {"provider", "model"}
        )
    )
    rejected_fields.extend(
        field
        for field, rejected in (
            ("provider", provider_rejected),
            ("model", model_rejected),
        )
        if rejected and field not in rejected_fields
    )
    if rejected_fields:
        record["attribution_diagnostics"] = [
            {
                "type": "invalid_attribution_label",
                "field": field,
                "normalized": "unknown",
            }
            for field in rejected_fields
        ]
    if stats:
        record["stats"] = dict(stats)
    return record


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


def _native_planning_status(
    agent_error: str, provider_limit_pause: float | None
) -> str:
    if provider_limit_pause is not None:
        return NATIVE_PLANNING_PROVIDER_LIMIT_STATUS
    return "analysis_error" if agent_error else "decided"


def planning_runtime_identity(
    contexts: Sequence[AgentRuntimeContext],
    *,
    configured_model: str | None,
) -> tuple[str, str, tuple[str, ...]]:
    providers = {
        context.model_provider for context in contexts if context.model_provider
    }
    models = {context.model_id for context in contexts if context.model_id}
    provider = next(iter(providers)) if len(providers) == 1 else "mixed"
    if not providers:
        provider = "unknown"
    model = next(iter(models)) if len(models) == 1 else "mixed"
    if not models:
        model = configured_model or "unknown"
    diagnostics = tuple(
        dict.fromkeys(
            field
            for context in contexts
            for field in context.attribution_diagnostics
            if field in {"provider", "model"}
        )
    )
    return provider, model, diagnostics


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
    planning_started = time_module.monotonic()
    analysis_vibe_runner = VibeRunner(config) if analysis_runner is None else None
    runner = (
        analysis_runner
        if analysis_runner is not None
        else analysis_vibe_runner.run_analysis_agent
    )
    analysis_usage = unavailable_usage("unknown", "provider_usage_not_reported")
    worker_usage = unavailable_usage("unknown", "provider_usage_not_reported")
    analysis_context = AgentRuntimeContext()
    worker_context = AgentRuntimeContext()
    output_path = config.state_path / "autopilot" / f"{cycle_id}-planning-decision.json"
    agent_error = ""
    agent_error_kind = ""
    provider_limit_reset_text = ""
    provider_limit_pause: float | None = None
    try:
        payload = runner(
            build_native_planning_decision_prompt(status, min_ready=min_ready),
            output_path,
        )
    except AgentProviderLimitError as exc:
        # An account provider limit is not a planning failure: the analysis agent
        # never got to decide. Surface it as its own status so the supervisor
        # pauses until the advertised reset instead of re-running planning into
        # the same wall every cycle.
        payload = None
        agent_error = _bounded_planning_text(exc)
        agent_error_kind = PLANNING_ERROR_PROVIDER_LIMIT
        provider_limit_reset_text = exc.signal.reset_text
        provider_limit_pause = exc.pause_seconds
    except AgentResolutionError as exc:
        payload = None
        agent_error = _bounded_planning_text(exc)
        agent_error_kind = PLANNING_ERROR_EXECUTABLE_RESOLUTION
    except subprocess.SubprocessError as exc:
        payload = None
        agent_error = _bounded_planning_text(exc)
        agent_error_kind = PLANNING_ERROR_SUBPROCESS
    except OSError as exc:
        payload = None
        agent_error = _bounded_planning_text(exc)
        agent_error_kind = PLANNING_ERROR_OS_ERROR
    except (KeyError, ValueError) as exc:
        # The analysis agent answered, but its output could not be read as a
        # planning decision: that is a plan/schema fault, not infrastructure.
        payload = None
        agent_error = _bounded_planning_text(exc)
        agent_error_kind = PLANNING_ERROR_INVALID_PLAN
    if payload is None and not agent_error:
        agent_error = "analysis agent returned no planning decision"
        agent_error_kind = PLANNING_ERROR_INVALID_PLAN
    if analysis_vibe_runner is not None:
        analysis_usage = analysis_vibe_runner.last_analysis_usage
        analysis_context = analysis_vibe_runner.last_analysis_runtime_context
    should_plan = False
    reason = ""
    objective = ""
    if payload is not None:
        should_plan, reason, objective, agent_error = validate_native_planning_decision(
            payload
        )
        if agent_error:
            agent_error_kind = PLANNING_ERROR_INVALID_PLAN
    decision = NativePlanningDecision(
        cycle_id=cycle_id,
        runnable=status.queue.runnable,
        min_ready=min_ready,
        status=_native_planning_status(agent_error, provider_limit_pause),
        should_plan=should_plan,
        reason=reason,
        objective=objective,
        agent_invoked=True,
        agent_error=agent_error,
        agent_error_kind=agent_error_kind,
        provider_limit_reset_text=provider_limit_reset_text,
        provider_limit_pause_seconds=provider_limit_pause,
    )
    run_store.append_record(decision.to_record(config.repo))

    if agent_error or not should_plan:
        worker = NativePlanningWorkerResult(
            cycle_id=cycle_id,
            phase="terminal",
            status=(
                "skipped_provider_limit"
                if provider_limit_pause is not None
                else ("skipped_analysis_error" if agent_error else "skipped_not_needed")
            ),
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
        model_provider, model_id, attribution_diagnostics = planning_runtime_identity(
            (analysis_context,), configured_model=config.agent.model
        )
        return NativePlanningCycleResult(
            decision=decision,
            worker=worker,
            stats=analysis_usage.to_stats(
                phase="planning",
                wall_time_seconds=max(0.0, time_module.monotonic() - planning_started),
            ),
            model_provider=(
                model_provider
                if model_provider != "unknown"
                else analysis_usage.provider
            ),
            model_id=model_id,
            attribution_diagnostics=attribution_diagnostics,
        )

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
            effort=config.agent.effort,
        )
        command = inject_structured_usage_output(command, config.agent.agent_kind)
        worker_context = parse_agent_runtime_context_from_command(command)
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

    usage_observer = ProviderUsageObserver(
        {"codex": "openai", "claude": "anthropic"}.get(
            config.agent.agent_kind, "unknown"
        ),
        unavailable_reason=structured_usage_observation(
            command if launch_attempted else None,
            "custom",
        ).unavailable_reason,
    )
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            usage_observer.observe_line(line)
    except OSError:
        pass
    worker_usage = usage_observer.usage

    task_source_error = ""
    runnable_after: int | None = status.queue.runnable
    created_task_ids: tuple[str, ...] = ()
    created_count: int | None = 0
    if started_pid is not None and interruption is None:
        queue_after = collect_task_queue_status(config)
        task_source_error = _bounded_planning_text(queue_after.source_error)
        runnable_after = None if task_source_error else queue_after.runnable
        if task_source_error:
            created_count = None
        else:
            before_tasks = status.queue.source_tasks or status.queue.runnable_tasks
            after_tasks = queue_after.source_tasks or queue_after.runnable_tasks
            before_ids = {str(task.get("id") or "") for task in before_tasks}
            created_task_ids = tuple(
                sorted(
                    task_id
                    for task in after_tasks
                    if (task_id := str(task.get("id") or ""))
                    and task_id not in before_ids
                )
            )
            created_count = len(created_task_ids)
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
        created_task_ids=created_task_ids,
        created_count=created_count,
    )
    run_store.append_record(worker.to_record(config.repo))
    if interruption is not None:
        raise interruption
    merged_usage = merge_provider_usage(analysis_usage, worker_usage)
    model_provider, model_id, attribution_diagnostics = planning_runtime_identity(
        (analysis_context, worker_context), configured_model=config.agent.model
    )
    return NativePlanningCycleResult(
        decision=decision,
        worker=worker,
        stats=merged_usage.to_stats(
            phase="planning",
            wall_time_seconds=max(0.0, time_module.monotonic() - planning_started),
        ),
        model_provider=(
            model_provider if model_provider != "unknown" else merged_usage.provider
        ),
        model_id=model_id,
        attribution_diagnostics=attribution_diagnostics,
    )


def execute_autopilot_cycle(
    config: VibeConfig,
    *,
    cycle_id: str,
    autopilot_run_id: str = "",
    jobs: int,
    ask_agent: bool,
    continue_on_failure: bool,
    max_slices: int,
    max_tasks: int,
    min_ready: int,
    process_exists: ProcessExists | None,
    launcher: RunUntilDoneLauncher,
    run_store: RunStore,
    maintenance_runner: MaintenanceRunner = run_maintenance_command,
    worktree_disposition_runner: WorktreeDispositionRunner = run_worktree_disposition,
    disk_health_runner: DiskHealthRunner = run_disk_health,
    cycle_summary_runner: CycleSummaryRunner = run_cycle_summary,
    native_troubleshoot_runner: NativeTroubleshootRunner = run_native_troubleshoot,
    native_planning_runner: NativePlanningRunner = run_native_planning,
    command_timeout: float = AUTOPILOT_COMMAND_TIMEOUT_SECONDS,
    command_max_output_bytes: int = AUTOPILOT_COMMAND_MAX_OUTPUT_BYTES,
    supervisor_blockers: tuple[str, ...] = (),
    dispatch_min_ready: int = 1,
) -> AutopilotCycleResult:
    min_ready = require_positive_min_ready(min_ready)
    dispatch_min_ready = require_positive_dispatch_min_ready(dispatch_min_ready)
    cycle_started_at = utc_now_iso()
    status = collect_project_status(config, process_exists=process_exists)
    runnable = status.queue.runnable
    actions: list[str] = []
    child_pid: int | None = None
    child_log: Path | None = None
    cleanup_errors = 0
    planning_provider_limit_pause: float | None = None
    planning_backoff_pause: float | None = None
    dispatched_runs = 0

    # Missing worker processes are normal after a runtime-owned terminal report.
    # Cleanup is eligible only when the run itself is durably terminal or its
    # exact runtime supervisor identity is verified gone.
    cleanup_candidates = tuple(
        lock
        for lock in status.stale_locks
        if lock.run_proven_finished
        and (
            lock.stale_reason == "missing_process"
            or (lock.stale_reason == "result_recorded" and lock.settlement_pending)
        )
    )
    if cleanup_candidates:
        lock_manager = build_lock_manager(
            config.repo,
            config.state_path / "locks",
            config.locks,
            runtime_context=config.runtime_environment,
        )

        def autopilot_task_source() -> tuple[object, object]:
            runner = VibeRunner(config)
            return runner.source, runner.source_resolution.task_source

        # The cycle never forces: a settlement-pending lock is released here
        # only when the authoritative source is confirmed settled or the fenced
        # settle-then-release path succeeds, so a stale latch cannot keep the
        # whole repository's dispatch blocked while an unsettled source still
        # holds its lock.
        clean_result = clean_stale_locks(
            list(cleanup_candidates),
            lock_manager,
            settlement_recovery=TaskSourceSettlementRecovery(autopilot_task_source),
            run_store=run_store,
        )
        record_expired_locks(run_store, clean_result.cleaned)
        if clean_result.cleaned:
            actions.append(f"cleaned_stale_locks:{len(clean_result.cleaned)}")
        if clean_result.recovered:
            actions.append(
                f"settled_stale_locks:{len(clean_result.recovered)}",
            )
        if clean_result.errors:
            cleanup_errors = len(clean_result.errors)
            actions.append(f"stale_lock_cleanup_errors:{cleanup_errors}")
        status = collect_project_status(config, process_exists=process_exists)
        runnable = status.queue.runnable

    def apply_fresh_upstream_sync(
        project_status: ProjectStatus,
        *,
        record_action: bool,
    ) -> ProjectStatus:
        if not config.autopilot.require_upstream_sync:
            return project_status
        upstream = check_upstream_sync(
            config.repo,
            config.main_branch,
            required=True,
            refresh=True,
        )
        retained_blockers = tuple(
            blocker
            for blocker in project_status.blockers
            if not blocker.startswith("upstream_sync:")
        )
        if upstream.satisfied:
            updated = dataclasses.replace(
                project_status,
                blockers=retained_blockers,
            )
            action = "upstream_sync:equal"
        else:
            assert upstream.blocker is not None
            updated = dataclasses.replace(
                project_status,
                blockers=(
                    *retained_blockers,
                    f"upstream_sync:{upstream.blocker.code}",
                ),
            )
            action = f"upstream_sync:{upstream.blocker.code}"
        if record_action:
            actions.append(action)
        return updated

    status = apply_fresh_upstream_sync(status, record_action=True)

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

    disk_health = disk_health_runner(config, cycle_id=cycle_id)
    status = apply_fresh_upstream_sync(
        collect_project_status(
            config,
            process_exists=process_exists,
            disk_health_result=disk_health,
        ),
        record_action=False,
    )
    runnable = status.queue.runnable
    run_store.append_record(disk_health.to_record(config.repo))
    actions.append(f"disk_health:{disk_health.status}")
    if disk_health.probe_errors:
        actions.append(f"disk_health_probe_errors:{disk_health.probe_errors}")

    # Read-only "what landed" summary: the span is the previous cycle's recorded
    # main ref to the current one. Runs unconditionally and never blocks; the
    # prior ref is read before this cycle's record is journaled.
    cycle_summary = cycle_summary_runner(
        config,
        cycle_id=cycle_id,
        prior_main_ref=latest_cycle_main_ref(run_store),
        current_main_ref=status.git.main_head,
    )
    run_store.append_record(cycle_summary.to_record(config.repo))
    actions.append(cycle_summary.action)

    previous_troubleshoot = latest_native_troubleshoot(run_store)
    troubleshoot = native_troubleshoot_runner(
        cycle_id=cycle_id,
        run_store=run_store,
    )
    run_store.append_record(troubleshoot.to_record(config.repo))
    actions.append(troubleshoot.action)

    def apply_troubleshoot_observations(
        project_status: ProjectStatus,
    ) -> ProjectStatus:
        previous_observations = set(previous_troubleshoot.observations)
        return dataclasses.replace(
            project_status,
            observations=tuple(
                dict.fromkeys(
                    (
                        *(
                            observation
                            for observation in project_status.observations
                            if observation not in previous_observations
                        ),
                        *troubleshoot.observations,
                    )
                )
            ),
        )

    status = apply_troubleshoot_observations(status)

    def current_blockers(project_status: ProjectStatus) -> list[str]:
        current = list(
            dict.fromkeys(
                (
                    *project_status.blockers,
                    *supervisor_blockers,
                    *troubleshoot.blockers,
                )
            )
        )
        if not config.autopilot.require_clean_repo and "repo_dirty" in current:
            current.remove("repo_dirty")
            if "repo_dirty_ignored" not in actions:
                actions.append("repo_dirty_ignored")
        if cleanup_errors:
            current.append("stale_lock_cleanup_failed")
        return current

    blocker_list = current_blockers(status)

    def run_maintenance(
        kind: str,
        *,
        command_override: str | None = None,
        runtime_context: Mapping[str, str] | None = None,
        timeout_override: float | None = None,
    ) -> MaintenanceCommandResult | None:
        command = (
            command_override
            if command_override is not None
            else config.autopilot.maintenance_command(kind)
        )
        if not command:
            return None
        command_environment = maintenance_command_env(
            config, kind=kind, cycle_id=cycle_id, runnable=runnable
        )
        if kind == TASK_SOURCE_HEALTH_COMMAND_KIND:
            for name in WITHHELD_ADAPTER_ENV:
                command_environment.pop(name, None)
        if runtime_context is not None:
            command_environment.update(runtime_context)
        result = maintenance_runner(
            command,
            kind,
            cycle_id,
            cwd=config.repo,
            env_extra=command_environment,
            timeout=(
                timeout_override if timeout_override is not None else command_timeout
            ),
            max_output_bytes=command_max_output_bytes,
        )
        run_store.append_record(result.to_record(config.repo))
        actions.append(f"ran_{kind}_command:exit={result.exit_code}")
        return result

    health = run_maintenance("health")
    if health is not None and not health.succeeded:
        blocker_list.append("autopilot_health_failed")
    task_source_health = run_maintenance(
        TASK_SOURCE_HEALTH_COMMAND_KIND,
        command_override=config.task_source.health_command,
        runtime_context=config.runtime_environment,
        timeout_override=config.task_source.command_timeout_seconds,
    )
    if task_source_health is not None and not task_source_health.succeeded:
        blocker_list.append("task_source_health_failed")

    blockers = tuple(blocker_list)
    blockers_checked_after_planning = False
    if not blockers and runnable < min_ready:
        active_conflict_workers = active_conflict_worker_count(status.workers)
        planning = run_maintenance("planning")
        if planning is None:
            fingerprint = planning_source_fingerprint(status.queue)
            planning_backoff = planning_outcome_backoff(
                run_store,
                repo=config.repo,
                fingerprint=fingerprint,
                backoff_seconds=config.autopilot.planning_backoff_seconds,
                max_launches_per_day=(config.autopilot.planning_max_launches_per_day),
                unproductive_threshold=(
                    config.autopilot.planning_unproductive_threshold
                ),
            )
        else:
            planning_backoff = None
        if planning is None and planning_backoff is not None:
            # Withhold the launch entirely: the analysis and authoring agents
            # are the spend this backoff exists to bound, so an "attempt but
            # skip" would defeat it.
            planning_backoff_pause = planning_backoff.remaining_seconds
            actions.append(planning_backoff.action)
            if runnable == 0 and active_conflict_workers:
                actions.append(f"waiting_for_active_workers:{active_conflict_workers}")
            elif runnable == 0:
                actions.append("no_runnable_work")
            else:
                actions.append(f"low_runnable_work:{runnable}/{min_ready}")
        elif planning is None:
            # Appended before the analysis agent runs: an interrupted or
            # crashed launch never reaches the outcome append below, and must
            # still consume one of the day's planning launches.
            run_store.append_record(
                planning_launch_record(
                    config.repo, cycle_id=cycle_id, fingerprint=fingerprint
                )
            )
            native_planning = native_planning_runner(
                config,
                cycle_id=cycle_id,
                status=status,
                min_ready=min_ready,
                run_store=run_store,
            )
            planning_outcome = classify_planning_outcome(native_planning)
            run_store.append_record(
                planning_outcome_record(
                    config.repo,
                    cycle_id=cycle_id,
                    outcome=planning_outcome,
                    fingerprint=fingerprint,
                    runnable_before=native_planning.worker.runnable_before,
                    runnable_after=native_planning.worker.runnable_after,
                    created_count=native_planning.worker.created_count,
                    created_task_ids=native_planning.worker.created_task_ids,
                    provider_launched=planning_provider_launched(native_planning),
                    model_provider=native_planning.model_provider,
                    model_id=native_planning.model_id,
                    attribution_diagnostics=(native_planning.attribution_diagnostics),
                    stats=native_planning.stats,
                )
            )
            actions.append(f"{PLANNING_OUTCOME_ACTION_PREFIX}{planning_outcome}")
            if native_planning.decision.provider_limit:
                planning_provider_limit_pause = (
                    native_planning.decision.provider_limit_pause_seconds
                )
                actions.append(
                    f"{NATIVE_PLANNING_PROVIDER_LIMIT_ACTION}:"
                    f"{planning_provider_limit_pause:.0f}s"
                )
            elif native_planning.decision.agent_error:
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
        status = apply_troubleshoot_observations(
            apply_fresh_upstream_sync(
                collect_project_status(
                    config,
                    process_exists=process_exists,
                    disk_health_result=disk_health,
                ),
                record_action=False,
            )
        )
        runnable = status.queue.runnable
        blocker_list = current_blockers(status)
        blockers = tuple(blocker_list)
        blockers_checked_after_planning = True

    if blockers:
        cycle_status = "blocked"
        actions.append(
            "blocked_post_planning"
            if blockers_checked_after_planning
            else "blocked_preflight"
        )
    elif planning_provider_limit_pause is not None:
        cycle_status = "idle"
    elif runnable < dispatch_min_ready:
        cycle_status = "idle"
        if runnable > 0:
            actions.append(
                f"{DISPATCH_FLOOR_HOLD_ACTION}:{runnable}/{dispatch_min_ready}"
            )
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
            # Appended before the launcher blocks on the child: a stop arriving
            # mid-cycle otherwise has no durable identity for the in-flight
            # child, and can only reach the supervisor.
            identity = read_process_node(pid)
            run_store.append_record(
                autopilot_child_started_record(
                    repo=config.repo,
                    run_id=autopilot_run_id,
                    cycle_id=cycle_id,
                    pid=pid,
                    process_group_id=(identity.process_group_id if identity else None),
                    session_id=identity.session_id if identity else None,
                    process_birth_id=(identity.process_birth_id if identity else ""),
                )
            )

        records_before_launch = run_store.read_records()
        run_started_before = sum(
            1
            for record in records_before_launch
            if record.get("record_type") == RUN_STARTED_RECORD_TYPE
        )
        actions.append("launched_run_until_done")
        exit_code = launcher(
            command,
            cwd=config.repo,
            log_path=child_log,
            on_start=_on_start,
        )
        child_pid = observed_pid.get("pid")
        records_after_launch = run_store.read_records()
        run_started_after = sum(
            1
            for record in records_after_launch
            if record.get("record_type") == RUN_STARTED_RECORD_TYPE
        )
        dispatched_runs = max(0, run_started_after - run_started_before)
        cycle_status = classify_child_exit(exit_code)
        actions.append(f"child_exit:{exit_code}")
        actions.append(f"dispatched_runs:{dispatched_runs}")
        if cycle_status == "completed" and dispatched_runs == 0:
            cycle_status = "idle"
            actions.append("child_completed_without_dispatch")
        run_maintenance("summary")
        if cycle_status in {"restartable", "terminated"}:
            run_maintenance("troubleshoot")

    activation_blockers = task_activation_failure_blockers(
        records_since_latest_autopilot_cycle(run_store.read_records())
    )
    if activation_blockers:
        blockers = tuple(dict.fromkeys((*blockers, *activation_blockers)))
        actions.append(f"task_source_activation_failures:{len(activation_blockers)}")

    pause_seconds = provider_limit_pause_seconds(
        run_store,
        since=cycle_started_at,
        default_backoff=config.supervision.provider_limit_backoff_seconds,
    )
    # A planning wall and a dispatched-child wall can both land in one cycle;
    # the longer advertised reset governs, since resuming earlier only walks
    # into the wall that is still standing.
    if planning_provider_limit_pause is not None:
        pause_seconds = (
            planning_provider_limit_pause
            if pause_seconds is None
            else max(pause_seconds, planning_provider_limit_pause)
        )
    if pause_seconds is not None:
        actions.append(f"{CHILD_PROVIDER_LIMIT_ACTION}:{pause_seconds:.0f}s")

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
        provider_limit_pause_seconds=pause_seconds,
        planning_backoff_seconds=planning_backoff_pause,
        autopilot_run_id=autopilot_run_id,
        dispatched_runs=dispatched_runs,
    )


def task_activation_failure_blockers(
    records: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    blockers: list[str] = []
    for record in records:
        if record.get("record_type") != TASK_ACTIVATION_FAILED_RECORD_TYPE:
            continue
        task_id = str(record.get("task_id") or "unknown")
        blocker = f"task_source_activation_failed:{task_id}"
        exit_code = record.get("exit_code")
        if isinstance(exit_code, int):
            blocker += f":exit={exit_code}"
        diagnostic = record.get("stderr_last_line") or record.get("message")
        if not isinstance(diagnostic, str) or not diagnostic:
            diagnostic = str(record.get("error_class") or "adapter failed")
        blockers.append(f"{blocker}: {diagnostic[:500]}")
    return tuple(dict.fromkeys(blockers))


def records_since_latest_autopilot_cycle(
    records: Sequence[Mapping[str, object]],
) -> Sequence[Mapping[str, object]]:
    for index in range(len(records) - 1, -1, -1):
        if records[index].get("record_type") == AUTOPILOT_CYCLE_RECORD_TYPE:
            return records[index + 1 :]
    return records


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


def require_positive_dispatch_min_ready(dispatch_min_ready: int) -> int:
    if (
        isinstance(dispatch_min_ready, bool)
        or not isinstance(dispatch_min_ready, int)
        or dispatch_min_ready < 1
    ):
        raise ValueError("dispatch_min_ready must be a positive integer")
    return dispatch_min_ready


def cycle_should_recheck(result: AutopilotCycleResult) -> bool:
    """Whether a finished cycle should use the adaptive idle waiter.

    An idle cycle is one that neither dispatched nor observed a child because
    runnable work was below the dispatch floor or because a successful child
    exited without durable ``run_started`` evidence. Both cases poll for
    task-source changes, but only the below-floor case wakes merely because the
    runnable count reaches the dispatch floor.

    An idle cycle with no planning command configured still rechecks: that is
    deliberate, so out-of-band task additions (a peer or operator filling the
    board) are picked up without waiting the full interval.
    """
    return result.status == "idle"


def cycle_should_wake_on_runnable(result: AutopilotCycleResult) -> bool:
    """Whether an idle wait may wake on runnable count without a source change.

    A child that just found zero dispatchable tasks is stronger evidence than
    the queue's coarse runnable count. Re-waking that child on the same count
    would spin on tasks suppressed by workspace or attempt-state deferrals.
    """
    return cycle_should_recheck(result) and (
        "launched_run_until_done" not in result.actions
    )


def cycle_should_poll_task_source(result: AutopilotCycleResult) -> bool:
    """Whether a cycle wait should wake on a material task-source change.

    Idle cycles and restartable cycles use the same bounded polling machinery.
    Only an idle cycle that never launched a child wakes on runnable count
    alone; post-child waits require a material source change.
    """
    return cycle_should_recheck(result) or result.status == "restartable"


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


def sleep_until_stop(
    total_seconds: float,
    *,
    sleeper: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
    slice_seconds: float,
) -> bool:
    """Sleep ``total_seconds`` in stop-checkable slices.

    A provider-limit pause can run for hours, so it must not become one
    uninterruptible sleep: a cooperative stop would otherwise only be observed
    after the wall cleared. Signal-driven stops already unwind through
    ``sleeper``; this adds the cooperative ``should_stop`` path. Returns False
    when a stop was observed, True when the full pause elapsed.

    Slicing exists only to poll ``should_stop``, so with no cooperative stop
    installed the pause stays a single sleep of the full duration.
    """
    if should_stop is None:
        if total_seconds > 0:
            sleeper(total_seconds)
        return True
    if should_stop():
        return False
    for slice_duration in recheck_sleep_slices(total_seconds, slice_seconds):
        sleeper(slice_duration)
        if should_stop():
            return False
    return True


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
    should_reload: Callable[[], bool] | None = None,
    runnable_probe: IdleRunnableProbe | None = None,
    min_ready: int = 1,
    wake_adapter: IdleWakeAdapter | None = None,
    monotonic: Callable[[], float] | None = None,
    active_runs: tuple[ActiveRunState, ...] = (),
    baseline_fingerprint: str = "",
    wake_on_runnable: bool = True,
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
        if should_reload is not None and should_reload():
            return IdleWaitResult(
                cycle_id=cycle_id,
                wake_reason="reload",
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
        if should_reload is not None and should_reload():
            return IdleWaitResult(
                cycle_id=cycle_id,
                wake_reason="reload",
                deadline=deadline,
                poll_count=polls,
                adapter_calls=adapter_calls,
                source_error_count=source_error_count,
                adapter_error_count=adapter_error_count,
                source_errors=tuple(source_errors),
                adapter_errors=tuple(adapter_errors),
            )
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
            source_changed = False
        else:
            runnable = status.runnable
            source_error = status.source_error
            source_changed = bool(
                baseline_fingerprint
                and not source_error
                and planning_source_fingerprint(status) != baseline_fingerprint
            )
        if source_error:
            source_error_count += 1
            _record_bounded_error(source_errors, source_error)
        if clock() >= deadline_at:
            break
        if not source_error and (
            (wake_on_runnable and runnable >= threshold) or source_changed
        ):
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
def autopilot_termination_signals(
    *,
    on_reload: Callable[[], None] | None = None,
) -> Iterator[Callable[[], None]]:
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
    reload_signal = getattr(signal, "SIGHUP", None)
    installed_signal_numbers = (
        *handled_signals,
        *(
            (reload_signal,)
            if reload_signal is not None and on_reload is not None
            else ()
        ),
    )
    previous_handlers = {
        signal_number: signal.getsignal(signal_number)
        for signal_number in installed_signal_numbers
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

    def request_reload(_signal_number: int, _frame: object) -> None:
        if on_reload is not None:
            on_reload()

    installed_signals: list[signal.Signals] = []
    try:
        for stop_signal in handled_signals:
            signal.signal(stop_signal, request_stop)
            installed_signals.append(stop_signal)
        if reload_signal is not None and on_reload is not None:
            signal.signal(reload_signal, request_reload)
            installed_signals.append(reload_signal)

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


def reload_autopilot_cycle_config(config: VibeConfig) -> VibeConfig:
    current = load_config(
        config.repo,
        runtime_context=dict(config.runtime_context),
    )
    return autopilot_cycle_config_from_current(config, current)


def autopilot_cycle_config_from_current(
    config: VibeConfig,
    current: VibeConfig,
) -> VibeConfig:
    return dataclasses.replace(
        config,
        agent=current.agent,
        agent_profiles=current.agent_profiles,
        agent_routing=current.agent_routing,
        worker_prompt_extra=current.worker_prompt_extra,
        autopilot=dataclasses.replace(
            config.autopilot,
            jobs=current.autopilot.jobs,
        ),
        config_path=current.config_path,
        config_source=current.config_source,
        config_digest=current.config_digest,
        config_key_fingerprints=current.config_key_fingerprints,
    )


def apply_autopilot_reload_request(
    config: VibeConfig,
    active_config: VibeConfig,
    *,
    reload_config_jobs: bool,
    run_store: RunStore,
    run_id: str,
    request: Mapping[str, Any],
) -> tuple[VibeConfig, str, str]:
    request_id = str(request.get("request_id") or "")
    try:
        current = load_config(
            config.repo,
            runtime_context=dict(config.runtime_context),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        blocker = f"autopilot_reload_invalid_config:{type(exc).__name__}"
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": run_id,
                "request_id": request_id,
                "state": "failed",
                "changed_keys": list(request.get("changed_keys", [])),
                "blocker": blocker,
            }
        )
        return active_config, "failed", blocker

    changed_from_start = changed_config_keys(
        config.config_key_fingerprints,
        current.config_key_fingerprints,
    )
    refused_keys = tuple(
        key
        for key in changed_from_start
        if not config_key_reload_safe(
            key,
            reload_config_jobs=reload_config_jobs,
        )
    )
    if refused_keys:
        blocker = "autopilot_reload_requires_restart:" + ",".join(refused_keys)
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": run_id,
                "request_id": request_id,
                "state": "refused",
                "changed_keys": list(changed_from_start),
                "blocker": blocker,
            }
        )
        return active_config, "refused", blocker

    loaded = autopilot_cycle_config_from_current(config, current)
    changed_keys = changed_config_keys(
        active_config.config_key_fingerprints,
        loaded.config_key_fingerprints,
    )
    loaded_at = utc_now_iso()
    run_store.append_record(
        {
            "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
            "record_type": AUTOPILOT_CONFIG_RELOAD_RESULT_RECORD_TYPE,
            "occurred_at": loaded_at,
            "repo": str(config.repo),
            "run_id": run_id,
            "request_id": request_id,
            "state": "loaded" if changed_keys else "unchanged",
            "loaded_at": loaded_at,
            "config_fingerprint": config_snapshot_fingerprint(loaded),
            "config_key_fingerprints": dict(loaded.config_key_fingerprints),
            "changed_keys": list(changed_keys),
        }
    )
    return loaded, ("loaded" if changed_keys else "unchanged"), ""


def run_autopilot(
    config: VibeConfig,
    *,
    jobs: int = 1,
    reload_config_jobs: bool = False,
    interval: float = 0.0,
    once: bool = False,
    max_cycles: int = 0,
    ask_agent: bool = False,
    continue_on_failure: bool = False,
    max_slices: int = 0,
    max_tasks: int = 0,
    min_ready: int = 1,
    dispatch_min_ready: int = 1,
    process_exists: ProcessExists | None = None,
    sleep: Sleep | None = None,
    launcher: RunUntilDoneLauncher | None = None,
    maintenance_runner: MaintenanceRunner = run_maintenance_command,
    worktree_disposition_runner: WorktreeDispositionRunner = run_worktree_disposition,
    disk_health_runner: DiskHealthRunner = run_disk_health,
    cycle_summary_runner: CycleSummaryRunner = run_cycle_summary,
    native_troubleshoot_runner: NativeTroubleshootRunner = run_native_troubleshoot,
    native_planning_runner: NativePlanningRunner = run_native_planning,
    idle_waiter: Callable[..., IdleWaitResult] = wait_for_idle_change,
    idle_wake_command_runner: Callable[..., dict[str, object] | None] = (
        poll_idle_wake_command
    ),
    should_stop: Callable[[], bool] | None = None,
    install_signal_handlers: bool = True,
    install_reload_signal: bool = False,
) -> AutopilotRunSummary:
    """Supervise ``run-until-done`` as a foreground persistent loop.

    A single autopilot supervisor lock prevents duplicate supervisors. A live
    supervisor is observed rather than duplicated, and a stale supervisor lock
    is reported without being stolen. Each cycle is append-recorded; launch is
    blocked, never force-recovered, when preflight diagnostics are unsafe.
    """
    interval = require_autopilot_interval(interval)
    min_ready = require_positive_min_ready(min_ready)
    dispatch_min_ready = require_positive_dispatch_min_ready(dispatch_min_ready)

    supervisor_run_id = new_run_id("autopilot")
    binding = resolve_project_binding(config)
    if binding.blocker is not None:
        return AutopilotRunSummary(
            repo=config.repo,
            run_id=supervisor_run_id,
            started=False,
            blocker=binding.blocker,
        )

    process_checker = process_exists if process_exists is not None else pid_exists
    reload_requested = threading.Event()
    sleeper = sleep if sleep is not None else time_module.sleep
    reload_sleeper = sleep if sleep is not None else reload_requested.wait
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
                bound_names=config.project_binding.require,
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

    signal_stack = ExitStack()

    def enable_termination_signals() -> None:
        return

    if install_signal_handlers:
        enable_termination_signals = signal_stack.enter_context(
            autopilot_termination_signals(
                on_reload=reload_requested.set if install_reload_signal else None
            )
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
    active_cycle_config = config
    termination_signal: int | None = None
    try:
        enable_termination_signals()
        heartbeat.start()
        config_loaded_at = utc_now_iso()
        run_store.append_record(
            {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                "occurred_at": config_loaded_at,
                "repo": str(config.repo),
                "run_id": supervisor_run_id,
                "pid": os.getpid(),
                "log": str(supervisor_log),
                "worktree_disposition_policy": (config.autopilot.worktree_disposition),
                "config_fingerprint": config_snapshot_fingerprint(config),
                "config_loaded_at": config_loaded_at,
                "config_path": str(config.config_path) if config.config_path else "",
                "config_key_fingerprints": dict(config.config_key_fingerprints),
                "reload_config_jobs": reload_config_jobs,
            }
        )
        cycle_number = 0
        while True:
            if should_stop is not None and should_stop():
                break
            cycle_number += 1
            config_reload_error = ""
            config_loaded_at = ""
            explicit_reload_status = ""
            explicit_reload_handled = False
            if reload_requested.is_set():
                reload_requested.clear()
                requests = pending_autopilot_reload_requests(
                    run_store.read_records(),
                    run_id=supervisor_run_id,
                )
                for request in requests:
                    active_cycle_config, reload_status, reload_blocker = (
                        apply_autopilot_reload_request(
                            config,
                            active_cycle_config,
                            reload_config_jobs=reload_config_jobs,
                            run_store=run_store,
                            run_id=supervisor_run_id,
                            request=request,
                        )
                    )
                    explicit_reload_status = reload_status
                    if reload_status == "failed":
                        config_reload_error = reload_blocker.rpartition(":")[2]
                if requests:
                    explicit_reload_handled = True
                    if not config_reload_error and explicit_reload_status in {
                        "loaded",
                        "unchanged",
                    }:
                        config_loaded_at = utc_now_iso()
            if not explicit_reload_handled:
                try:
                    active_cycle_config = reload_autopilot_cycle_config(config)
                    config_loaded_at = utc_now_iso()
                except (OSError, UnicodeError, ValueError) as exc:
                    config_reload_error = type(exc).__name__
                    print(
                        "[vibe-loop] autopilot config reload failed "
                        f"({config_reload_error}); retaining the last valid cycle "
                        "configuration and withholding dispatch until the file is "
                        "valid; run vibe-loop doctor after correcting the file",
                        file=sys.stderr,
                        flush=True,
                    )
            cycle_config = active_cycle_config
            cycle_jobs = jobs
            if reload_config_jobs:
                cycle_jobs = cycle_config.autopilot.jobs or 1
            bounded_last = once or (max_cycles > 0 and cycle_number >= max_cycles)
            cycle_id = f"{supervisor_run_id}-c{cycle_number}"
            cycle_reload_status = (
                "failed"
                if config_reload_error
                else "refused"
                if explicit_reload_status == "refused"
                else "loaded"
            )
            cycle_started_record: dict[str, object] = {
                "schema_version": AUTOPILOT_RECORD_SCHEMA_VERSION,
                "record_type": AUTOPILOT_CYCLE_STARTED_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(config.repo),
                "run_id": supervisor_run_id,
                "cycle_id": cycle_id,
                "config_fingerprint": config_snapshot_fingerprint(cycle_config),
                "config_reload_status": cycle_reload_status,
            }
            if config_reload_error:
                cycle_started_record["config_reload_error"] = config_reload_error
            elif cycle_reload_status == "loaded":
                cycle_started_record["config_loaded_at"] = config_loaded_at
                cycle_started_record["config_key_fingerprints"] = dict(
                    cycle_config.config_key_fingerprints
                )
            run_store.append_record(cycle_started_record)
            result = execute_autopilot_cycle(
                cycle_config,
                cycle_id=cycle_id,
                autopilot_run_id=supervisor_run_id,
                jobs=cycle_jobs,
                ask_agent=ask_agent,
                continue_on_failure=continue_on_failure,
                max_slices=max_slices,
                max_tasks=max_tasks,
                min_ready=min_ready,
                dispatch_min_ready=dispatch_min_ready,
                process_exists=process_exists,
                launcher=launch,
                run_store=run_store,
                maintenance_runner=maintenance_runner,
                worktree_disposition_runner=worktree_disposition_runner,
                disk_health_runner=disk_health_runner,
                cycle_summary_runner=cycle_summary_runner,
                native_troubleshoot_runner=native_troubleshoot_runner,
                native_planning_runner=native_planning_runner,
                supervisor_blockers=(
                    ("autopilot_config_reload_failed",) if config_reload_error else ()
                ),
            )
            if config_reload_error:
                result = dataclasses.replace(
                    result,
                    actions=(
                        *result.actions,
                        f"config_reload_failed:{config_reload_error}",
                    ),
                )
            idle_wait_seconds = interval
            poll_task_source = cycle_should_poll_task_source(result)
            if (
                not bounded_last
                and interval > 0
                and cycle_should_recheck(result)
                and result.provider_limit_pause_seconds is None
            ):
                planning_backoff_seconds = result.planning_backoff_seconds
                # The backoff extends the idle wait budget rather than adding a
                # blocking sleep, so the idle waiter keeps polling the task
                # source throughout: a new ready task still wakes the next
                # cycle early, and a stop request is still honoured per slice.
                # It never shortens the operator's interval, only lengthens it.
                if (
                    planning_backoff_seconds is not None
                    and planning_backoff_seconds > interval
                ):
                    idle_wait_seconds = planning_backoff_seconds
            post_cycle_planning_delay: float | None = None
            post_cycle_queue: TaskQueueStatus | None = None
            if (
                not bounded_last
                and interval > 0
                and "launched_run_until_done" in result.actions
                and (
                    result.provider_limit_pause_seconds is None
                    or (
                        result.status == "restartable"
                        and result.provider_limit_pause_seconds <= 0
                    )
                )
            ):
                post_cycle_queue = collect_task_queue_status(config)
                if result.provider_limit_pause_seconds is None:
                    post_cycle_runnable = (
                        0
                        if post_cycle_queue.source_error
                        else post_cycle_queue.runnable
                    )
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
                        result, actions=(*result.actions, post_cycle_action)
                    )
            stop_pending = should_stop is not None and should_stop()
            scheduled_wait_seconds: float | None = None
            provider_limit_pause = result.provider_limit_pause_seconds
            if not bounded_last and not stop_pending:
                if provider_limit_pause is not None and provider_limit_pause > 0:
                    scheduled_wait_seconds = provider_limit_pause
                elif interval > 0:
                    if cycle_should_recheck(result):
                        scheduled_wait_seconds = idle_wait_seconds
                    elif post_cycle_planning_delay is not None:
                        scheduled_wait_seconds = post_cycle_planning_delay
                    else:
                        scheduled_wait_seconds = interval
            result = dataclasses.replace(
                result,
                next_wake=(
                    iso_after(scheduled_wait_seconds)
                    if scheduled_wait_seconds is not None
                    else ""
                ),
            )
            result.append_to(run_store)
            cycles.append(result)
            if bounded_last or stop_pending:
                break
            pause_seconds = (
                scheduled_wait_seconds
                if result.provider_limit_pause_seconds is not None
                and result.provider_limit_pause_seconds > 0
                else None
            )
            # A non-positive pause would skip the interval sleep entirely and
            # spin the cycle; treat it as no pause at all.
            if pause_seconds is not None and pause_seconds > 0:
                # A child stopped on a provider limit. Pause dispatch until
                # the advertised reset (or the configured backoff) instead of
                # re-dispatching straight into the same wall, in both persistent
                # and drain modes.
                print(
                    f"[vibe-loop] autopilot provider limit: pausing dispatch "
                    f"{pause_seconds:.0f}s before the next cycle",
                    flush=True,
                )
                if not sleep_until_stop(
                    pause_seconds,
                    sleeper=sleeper,
                    should_stop=should_stop,
                    slice_seconds=config.autopilot.planning_recheck_seconds,
                ):
                    break
                continue
            if interval > 0:
                assert scheduled_wait_seconds is not None
                # Persistent watch: keep cycling and sleeping until a bound or
                # signal stops the loop, even across idle or blocked cycles.
                poll_during_wait = poll_task_source and (
                    cycle_should_recheck(result) or post_cycle_planning_delay is None
                )
                if poll_during_wait:
                    wake_adapter_callback: IdleWakeAdapter | None = None
                    idle_wake_command = config.autopilot.idle_wake_command
                    if cycle_should_recheck(result) and idle_wake_command is not None:

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

                    if cycle_should_recheck(result) and idle_wait_seconds > interval:
                        print(
                            "[vibe-loop] autopilot planning backoff: withholding "
                            f"planning for {idle_wait_seconds:.0f}s after "
                            "unproductive launches; a task source change still "
                            "wakes the next cycle early",
                            flush=True,
                        )
                    baseline_queue = (
                        post_cycle_queue
                        if result.status == "restartable"
                        and post_cycle_queue is not None
                        else result.project_status.queue
                    )
                    baseline_fingerprint = (
                        ""
                        if baseline_queue.source_error
                        else planning_source_fingerprint(baseline_queue)
                    )
                    wait_result = idle_waiter(
                        config,
                        cycle_id=result.cycle_id,
                        deadline=result.next_wake,
                        interval=scheduled_wait_seconds,
                        initial_poll_seconds=(
                            config.autopilot.planning_recheck_seconds
                        ),
                        max_poll_seconds=config.autopilot.idle_poll_max_seconds,
                        sleeper=reload_sleeper,
                        should_stop=should_stop,
                        should_reload=reload_requested.is_set,
                        min_ready=dispatch_min_ready,
                        wake_adapter=wake_adapter_callback,
                        active_runs=tuple(
                            worker.active
                            for worker in result.project_status.workers
                            if worker_holds_active_conflict(worker)
                        ),
                        baseline_fingerprint=baseline_fingerprint,
                        wake_on_runnable=cycle_should_wake_on_runnable(result),
                    )
                    run_store.append_record(wait_result.to_record(config.repo))
                    wait_name = (
                        "idle"
                        if cycle_should_recheck(result)
                        else "restartable backoff"
                    )
                    if wait_result.wake_reason == "task_change":
                        print(
                            f"[vibe-loop] autopilot {wait_name} wake: task source "
                            "changed, starting next cycle early",
                            flush=True,
                        )
                    elif wait_result.wake_reason == "operator_message":
                        print(
                            f"[vibe-loop] autopilot {wait_name} wake: operator "
                            "message, starting next cycle early",
                            flush=True,
                        )
                    elif wait_result.wake_reason == "reload":
                        print(
                            f"[vibe-loop] autopilot {wait_name} wake: reload "
                            "requested, starting next cycle early",
                            flush=True,
                        )
                elif post_cycle_planning_delay is not None:
                    print(
                        "[vibe-loop] autopilot post-dispatch recheck: queue "
                        "below min-ready, starting the next cycle after "
                        f"{post_cycle_planning_delay:.0f}s",
                        flush=True,
                    )
                    reload_sleeper(scheduled_wait_seconds)
                else:
                    reload_sleeper(scheduled_wait_seconds)
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
            released = lock_manager.release_autopilot(
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
                lock_released=released,
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
    """Redact runtime-context values from every nested payload value."""

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
        status_payload = self.status.to_json() if self.status is not None else None
        project_binding = None
        if status_payload is not None:
            project_binding = status_payload.pop("project_binding", None)
        payload = {
            "name": self.name,
            "repo": str(self.repo),
            "status": status_payload,
            "error": self.error,
        }
        redacted = redact_runtime_context_payload(payload, self.runtime_context)
        assert isinstance(redacted, dict)
        redacted_status = redacted.get("status")
        if project_binding is not None and isinstance(redacted_status, dict):
            redacted_status["project_binding"] = project_binding
        return redacted


def load_registry_entry_config(entry: ProjectEntry) -> VibeConfig:
    """Load the config for a target the command enumerated from the registry.

    The entry named this target and supplies its own selector context, so the
    caller's ambient environment is not a competing claim about it. Every
    registry-driven read must go through here, because the binding is
    re-resolved from the config by each downstream gate.
    """

    return dataclasses.replace(
        load_config(entry.repo, runtime_context=dict(entry.runtime_context)),
        ambient_selects_target=False,
    )


def collect_registry_status(
    registry: ProjectRegistry,
    *,
    process_exists: ProcessExists | None = None,
) -> list[AggregateProjectStatus]:
    results: list[AggregateProjectStatus] = []
    for entry in registry.entries:
        try:
            config = load_registry_entry_config(entry)
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
WaitRuntimeEventPoller = Callable[[], dict[str, object] | None]


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
            elif kind in ACTIONABLE_RUNTIME_EVENT_KINDS:
                parts.append(f"runtime_event:{kind}")
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
            if any(
                event.get("kind") == "runtime_event_adapter_error"
                for event in self.events
            ):
                return "runtime_event_adapter_error"
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
    runtime_event_poller: WaitRuntimeEventPoller | None = None,
    session_ref: str = "",
) -> WaitResult:
    """Block until a watched PID exits, deadline, or trusted external event."""

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
        if runtime_event_poller is not None:
            runtime_event = runtime_event_poller()
            if runtime_event is not None:
                return WaitResult(
                    wake_reason="runtime_event",
                    events=tuple([*all_events, runtime_event]),
                    deadline=deadline_text,
                    session_ref=session_ref,
                )
        sleep_for = max(interval, 0.1)
        if deadline_epoch is not None:
            sleep_for = min(sleep_for, max(deadline_epoch - current, 0.1))
        sleeper(sleep_for)
