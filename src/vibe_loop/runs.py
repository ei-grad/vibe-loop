from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from vibe_loop.locks import redact_exact_fencing_token, redact_fencing_token_payload
from vibe_loop.telemetry import sanitize_run_stats

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


RUN_SCHEMA_VERSION = 3
RUN_RECORD_TYPE = "run_result"
WORKER_REPORT_SCHEMA_VERSION = 1
WORKER_REPORT_RECORD_TYPE = "worker_report"
WORKER_REPORT_STATUSES = ("completed", "blocked", "failed", "unknown")
LIFECYCLE_EVENT_SCHEMA_VERSION = 1
LOCK_ACQUIRED_RECORD_TYPE = "lock_acquired"
LOCK_RELEASED_RECORD_TYPE = "lock_released"
LOCK_EXPIRED_RECORD_TYPE = "lock_expired"
LOCK_FINALIZATION_FAILED_RECORD_TYPE = "lock_finalization_failed"
RUN_STARTED_RECORD_TYPE = "run_started"
WORKER_PROCESS_STARTED_RECORD_TYPE = "worker_process_started"
AGENT_CONTEXT_OBSERVED_RECORD_TYPE = "agent_context_observed"
WORKSPACE_CLAIM_RECORD_TYPE = "workspace_claim"
WORKSPACE_CLAIMED_EVENT_TYPE = "workspace_claimed"
WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE = "workspace_claim_mismatch"
RUN_STATE_TRANSITION_RECORD_TYPE = "run_state_transition"
TASK_RESTART_RECORD_TYPE = "task_restart"
TASK_RECOVERY_RECORD_TYPE = "task_recovery"
RUN_SUPERVISOR_STARTED_RECORD_TYPE = "run_supervisor_started"
RUN_SUPERVISOR_EXITED_RECORD_TYPE = "run_supervisor_exited"
AUTOPILOT_CYCLE_RECORD_TYPE = "autopilot_cycle"
AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE = "autopilot_supervisor_started"
AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE = "autopilot_supervisor_observed"
AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE = "autopilot_supervisor_stopped"
AUTOPILOT_COMMAND_RESULT_RECORD_TYPE = "autopilot_command_result"
AUTOPILOT_WORKTREE_REAP_RECORD_TYPE = "autopilot_worktree_reap"
AUTOPILOT_PLANNING_DECISION_RECORD_TYPE = "autopilot_planning_decision"
AUTOPILOT_PLANNING_WORKER_RECORD_TYPE = "autopilot_planning_worker"
AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE = "autopilot_planning_launch"
AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE = "autopilot_planning_outcome"
AUTOPILOT_IDLE_WAIT_RECORD_TYPE = "autopilot_idle_wait"
AUTOPILOT_CHILD_STARTED_RECORD_TYPE = "autopilot_child_started"
AUTOPILOT_DISK_HEALTH_RECORD_TYPE = "autopilot_disk_health"
AUTOPILOT_RECORD_TYPES = frozenset(
    {
        AUTOPILOT_CYCLE_RECORD_TYPE,
        AUTOPILOT_CHILD_STARTED_RECORD_TYPE,
        AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
        AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
        AUTOPILOT_SUPERVISOR_STOPPED_RECORD_TYPE,
        AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
        AUTOPILOT_DISK_HEALTH_RECORD_TYPE,
        AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
        AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
        AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
        AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
        AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
        AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
    }
)
LIFECYCLE_RECORD_TYPES = frozenset(
    {
        LOCK_ACQUIRED_RECORD_TYPE,
        LOCK_RELEASED_RECORD_TYPE,
        LOCK_EXPIRED_RECORD_TYPE,
        LOCK_FINALIZATION_FAILED_RECORD_TYPE,
        RUN_STARTED_RECORD_TYPE,
        WORKER_PROCESS_STARTED_RECORD_TYPE,
        AGENT_CONTEXT_OBSERVED_RECORD_TYPE,
        WORKSPACE_CLAIM_RECORD_TYPE,
        WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
        RUN_STATE_TRANSITION_RECORD_TYPE,
        TASK_RESTART_RECORD_TYPE,
        TASK_RECOVERY_RECORD_TYPE,
        RUN_SUPERVISOR_STARTED_RECORD_TYPE,
        RUN_SUPERVISOR_EXITED_RECORD_TYPE,
    }
)
KNOWN_RECORD_TYPES = frozenset(
    {
        RUN_RECORD_TYPE,
        WORKER_REPORT_RECORD_TYPE,
        *LIFECYCLE_RECORD_TYPES,
        *AUTOPILOT_RECORD_TYPES,
    }
)
LIFECYCLE_STATES = (
    "scheduled",
    "started",
    "session_observed",
    "workspace_claimed",
    "reported",
    "classified",
    "finalized",
)
LIFECYCLE_PROTECTED_KEYS = frozenset(
    {"schema_version", "record_type", "occurred_at", "run_id"}
)
# The settled outcome family a run finalizes into. It is deliberately coarser
# than the classification vocabulary: external provenance backends record one
# terminal outcome per run, while classifications such as "timed_out" and
# "limit_wall" describe a run that ended without settling the task at all.
SETTLED_RUN_OUTCOMES = ("completed", "failed", "blocked", "unknown")
UNKNOWN_RUN_OUTCOME = "unknown"


def settled_run_outcome(classification: str) -> str:
    """Map a run classification onto its settled outcome.

    Only a classification that is itself a settled outcome maps through
    verbatim. Everything else - an indeterminate probe, a wall-clock kill, a
    provider limit wall, an interrupted supervisor - is genuinely unknown and
    must not be promoted to a completion.
    """

    if classification in SETTLED_RUN_OUTCOMES:
        return classification
    return UNKNOWN_RUN_OUTCOME


_APPEND_LOCK = threading.Lock()
LOCK_POLL_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 30.0


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def redact_run_record(
    record: Mapping[str, object],
    *,
    fencing_token: str = "",
) -> dict[str, object]:
    payload = dict(record)
    if "stats" in payload:
        payload["stats"] = sanitize_run_stats(payload["stats"])
    redacted = redact_fencing_token_payload(payload)
    assert isinstance(redacted, dict)
    exact_redacted = redact_exact_fencing_token(redacted, fencing_token)
    assert isinstance(exact_redacted, dict)
    return exact_redacted


def autopilot_child_started_record(
    *,
    repo: Path | str,
    run_id: str,
    cycle_id: str,
    pid: int,
    process_group_id: int | None,
    session_id: int | None,
    process_birth_id: str,
) -> dict[str, object]:
    """Durable identity of the run-until-done child while it is still running.

    The launcher only returns the child's exit code, so without this record a
    stop request arriving mid-cycle has no verified way to name the in-flight
    child, and can only signal the supervisor while the child's own descendants
    survive reparenting to PID 1.
    """

    return {
        "schema_version": LIFECYCLE_EVENT_SCHEMA_VERSION,
        "record_type": AUTOPILOT_CHILD_STARTED_RECORD_TYPE,
        "occurred_at": utc_now_iso(),
        "repo": str(repo),
        "run_id": run_id,
        "cycle_id": cycle_id,
        "pid": pid,
        "process_group_id": process_group_id,
        "session_id": session_id,
        "process_birth_id": process_birth_id,
    }


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    classification: str
    exit_code: int
    log_path: Path
    start_main: str
    end_main: str
    message: str = ""
    started_at: str = ""
    session_id: str | None = None
    session_id_source: str = "fallback:run_id"
    transcript_path: str = ""
    agent_command_source: str = ""
    agent_selection_command_source: str = ""
    agent_default_policy_source: str = ""
    agent_default_policy: str = ""
    agent_kind: str = ""
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
    classification_source: str = ""
    worker_report: dict[str, object] | None = None
    restart_count: int = 0
    max_restarts: int = 0
    stats: dict[str, object] = dataclasses.field(default_factory=dict)
    finished_at: str = dataclasses.field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": self.run_id,
            "session_id": self.session_id or self.run_id,
            "session_id_source": self.session_id_source,
            "task_id": self.task_id,
            "classification": self.classification,
            "exit_code": self.exit_code,
            "log": str(self.log_path),
            "start_main": self.start_main,
            "end_main": self.end_main,
            "message": self.message,
            "started_at": self.started_at,
            "agent_command_source": self.agent_command_source,
            "agent_selection_command_source": self.agent_selection_command_source,
            "agent_default_policy_source": self.agent_default_policy_source,
            "agent_default_policy": self.agent_default_policy,
            "agent_kind": self.agent_kind,
            "agent_prompt_dialect": self.agent_prompt_dialect,
            "agent_prompt_dialect_source": self.agent_prompt_dialect_source,
            "agent_skill_ref_prefix": self.agent_skill_ref_prefix,
            "agent_skill_ref_prefix_source": self.agent_skill_ref_prefix_source,
            "classification_source": self.classification_source,
            "worker_report": self.worker_report,
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
            "finished_at": self.finished_at,
        }
        if self.model_provider:
            payload["model_provider"] = self.model_provider
        if self.model_provider_source:
            payload["model_provider_source"] = self.model_provider_source
        if self.model_id:
            payload["model_id"] = self.model_id
        if self.model_id_source:
            payload["model_id_source"] = self.model_id_source
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.reasoning_effort_source:
            payload["reasoning_effort_source"] = self.reasoning_effort_source
        if self.trailer_context:
            payload["trailer_context"] = self.trailer_context
        if self.trailer_context_sources:
            payload["trailer_context_sources"] = self.trailer_context_sources
        if self.transcript_path:
            payload["transcript_path"] = self.transcript_path
        if self.stats:
            payload["stats"] = sanitize_run_stats(self.stats)
        return payload

    def to_record(self) -> dict[str, object]:
        record = self.to_json()
        record.update(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "record_type": RUN_RECORD_TYPE,
                "status": self.classification,
            }
        )
        return record


@dataclasses.dataclass(frozen=True)
class WorkerReport:
    run_id: str
    task_id: str
    status: str
    commit: str = ""
    message: str = ""
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    reported_at: str = dataclasses.field(default_factory=utc_now_iso)
    fencing_token: str = dataclasses.field(
        default="",
        repr=False,
        compare=False,
        kw_only=True,
    )

    def __post_init__(self) -> None:
        if self.status not in WORKER_REPORT_STATUSES:
            raise ValueError(
                "worker report status must be one of: "
                f"{', '.join(WORKER_REPORT_STATUSES)}"
            )
        if not self.run_id:
            raise ValueError("worker report run_id is required")
        if not self.task_id:
            raise ValueError("worker report task_id is required")

    def to_json(self) -> dict[str, object]:
        payload = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "commit": self.commit,
            "message": self.message,
            "metadata": self.metadata,
            "reported_at": self.reported_at,
        }
        redacted = redact_fencing_token_payload(payload)
        assert isinstance(redacted, dict)
        exact_redacted = redact_exact_fencing_token(redacted, self.fencing_token)
        assert isinstance(exact_redacted, dict)
        return exact_redacted

    def to_record(self) -> dict[str, object]:
        record = self.to_json()
        record.update(
            {
                "schema_version": WORKER_REPORT_SCHEMA_VERSION,
                "record_type": WORKER_REPORT_RECORD_TYPE,
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> WorkerReport | None:
        if record.get("record_type") != WORKER_REPORT_RECORD_TYPE:
            return None
        run_id = record.get("run_id")
        task_id = record.get("task_id")
        status = record.get("status")
        if not isinstance(run_id, str) or not run_id:
            return None
        if not isinstance(task_id, str) or not task_id:
            return None
        if not isinstance(status, str) or status not in WORKER_REPORT_STATUSES:
            return None
        metadata = record.get("metadata")
        return cls(
            run_id=run_id,
            task_id=task_id,
            status=status,
            commit=string_value(record.get("commit")),
            message=string_value(record.get("message")),
            metadata=metadata if isinstance(metadata, dict) else {},
            reported_at=string_value(record.get("reported_at")),
        )


@dataclasses.dataclass(frozen=True)
class RunLifecycleEvent:
    record_type: str
    run_id: str
    task_id: str = ""
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    occurred_at: str = dataclasses.field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.record_type not in LIFECYCLE_RECORD_TYPES:
            raise ValueError(
                "lifecycle event record_type must be one of: "
                f"{', '.join(sorted(LIFECYCLE_RECORD_TYPES))}"
            )
        if not self.run_id:
            raise ValueError("lifecycle event run_id is required")
        protected = LIFECYCLE_PROTECTED_KEYS.intersection(self.payload)
        if protected:
            raise ValueError(
                "lifecycle event payload cannot override core keys: "
                f"{', '.join(sorted(protected))}"
            )

    @classmethod
    def lock_event(
        cls,
        record_type: str,
        *,
        run_id: str,
        task_id: str,
        lock_kind: str,
        lock_path: Path | str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "lock_kind": lock_kind,
            "lock_path": str(lock_path),
        }
        if payload is not None:
            event_payload.update(payload)
        return cls(record_type=record_type, run_id=run_id, payload=event_payload)

    @classmethod
    def workspace_claim_mismatch(
        cls,
        *,
        run_id: str,
        task_id: str,
        reason: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "reason": reason,
            "message": message,
            "details": dict(details or {}),
        }
        if payload is not None:
            event_payload.update(payload)
        return cls(
            record_type=WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
            run_id=run_id,
            payload=event_payload,
        )

    @classmethod
    def run_state_transition(
        cls,
        *,
        run_id: str,
        task_id: str,
        to_state: str,
        from_state: str = "",
        reason: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "to_state": to_state,
        }
        if from_state:
            event_payload["from_state"] = from_state
        if reason:
            event_payload["reason"] = reason
        if payload is not None:
            event_payload.update(payload)
        return cls(
            record_type=RUN_STATE_TRANSITION_RECORD_TYPE,
            run_id=run_id,
            payload=event_payload,
        )

    @classmethod
    def run_started(
        cls,
        *,
        run_id: str,
        task_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {"status": "started"}
        if payload is not None:
            event_payload.update(payload)
        return cls(
            record_type=RUN_STARTED_RECORD_TYPE,
            run_id=run_id,
            task_id=task_id,
            payload=event_payload,
        )

    @classmethod
    def worker_process_started(
        cls,
        *,
        run_id: str,
        task_id: str,
        worker_pid: int,
        supervisor_pid: int,
        process_group_id: int | None,
        session_id: int | None,
        process_birth_id: str,
        host: str,
    ) -> RunLifecycleEvent:
        return cls(
            record_type=WORKER_PROCESS_STARTED_RECORD_TYPE,
            run_id=run_id,
            task_id=task_id,
            payload={
                "worker_pid": worker_pid,
                "supervisor_pid": supervisor_pid,
                "worker_process_group_id": process_group_id,
                "worker_session_id": session_id,
                "worker_process_birth_id": process_birth_id,
                "pid_source": "popen",
                "pid_scope": "configured_command_process",
                "host": host,
            },
        )

    @classmethod
    def agent_context_observed(
        cls,
        *,
        run_id: str,
        task_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {"status": "observed"}
        if payload is not None:
            event_payload.update(payload)
        return cls(
            record_type=AGENT_CONTEXT_OBSERVED_RECORD_TYPE,
            run_id=run_id,
            task_id=task_id,
            payload=event_payload,
        )

    @classmethod
    def task_restart(
        cls,
        *,
        run_id: str,
        task_id: str,
        restart_count: int,
        max_restarts: int,
        cooldown_seconds: float,
        reason: str,
        exhausted: bool = False,
        attempted_restart_count: int | None = None,
        started_at: str = "",
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "restart_count": restart_count,
            "max_restarts": max_restarts,
            "cooldown_seconds": cooldown_seconds,
            "reason": reason,
            "exhausted": exhausted,
        }
        if started_at:
            event_payload["started_at"] = started_at
        if attempted_restart_count is not None:
            event_payload["attempted_restart_count"] = attempted_restart_count
        return cls(
            record_type=TASK_RESTART_RECORD_TYPE,
            run_id=run_id,
            payload=event_payload,
        )

    @classmethod
    def task_recovery(
        cls,
        *,
        run_id: str,
        task_id: str,
        phase: str,
        prior_run_id: str,
        attempt: int,
        max_attempts: int,
        reason: str = "unknown_run_recovery",
        branch: str = "",
        worktree: str = "",
        transcript_path: str = "",
        wrapper_log: str = "",
        outcome: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> RunLifecycleEvent:
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "phase": phase,
            "prior_run_id": prior_run_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "reason": reason,
        }
        if branch:
            event_payload["branch"] = branch
        if worktree:
            event_payload["worktree"] = worktree
        if transcript_path:
            event_payload["transcript_path"] = transcript_path
        if wrapper_log:
            event_payload["wrapper_log"] = wrapper_log
        if outcome:
            event_payload["outcome"] = outcome
        if payload is not None:
            event_payload.update(payload)
        return cls(
            record_type=TASK_RECOVERY_RECORD_TYPE,
            run_id=run_id,
            payload=event_payload,
        )

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": LIFECYCLE_EVENT_SCHEMA_VERSION,
            "record_type": self.record_type,
            "occurred_at": self.occurred_at,
            "run_id": self.run_id,
        }
        if self.task_id:
            record["task_id"] = self.task_id
        record.update(dict(self.payload))
        return record


@dataclasses.dataclass(frozen=True)
class RunLifecycleTransition:
    state: str
    observed: bool
    record_type: str = ""
    occurred_at: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "observed": self.observed,
            "record_type": self.record_type,
            "occurred_at": self.occurred_at,
        }


@dataclasses.dataclass(frozen=True)
class RunLifecycleProgress:
    state: str
    transitions: tuple[RunLifecycleTransition, ...]

    @property
    def missing_states(self) -> tuple[str, ...]:
        return tuple(
            transition.state
            for transition in self.transitions
            if not transition.observed
        )

    def to_json(self) -> dict[str, object]:
        return {
            "lifecycle_state": self.state,
            "lifecycle_transitions": [
                transition.to_json() for transition in self.transitions
            ],
            "missing_lifecycle_transitions": list(self.missing_states),
        }


@dataclasses.dataclass(frozen=True)
class RunHistoryView:
    run_id: str
    task_id: str
    status: str
    record_type: str
    updated_at: str
    started_at: str
    log_path: Path | None
    exit_code: int | None
    session_id: str
    session_id_source: str
    transcript_path: str
    message: str
    agent_kind: str
    agent_prompt_dialect: str
    agent_prompt_dialect_source: str
    agent_skill_ref_prefix: str
    agent_skill_ref_prefix_source: str
    model_provider: str
    model_provider_source: str
    model_id: str
    model_id_source: str
    reasoning_effort: str
    reasoning_effort_source: str
    trailer_context: dict[str, Any]
    trailer_context_sources: dict[str, Any]
    classification_source: str
    worker_report: dict[str, Any] | None
    stats: dict[str, Any]
    restart_count: int
    max_restarts: int
    restart_exhausted: bool
    restart_exhausted_reason: str
    record_count: int
    latest_record: dict[str, Any]
    lifecycle_progress: RunLifecycleProgress

    @classmethod
    def from_records(
        cls,
        run_id: str,
        records: list[dict[str, Any]],
    ) -> RunHistoryView:
        valid_records = run_history_view_records(records)
        latest = valid_records[-1]
        return cls(
            run_id=run_id,
            task_id=latest_text(valid_records, "task_id"),
            status=record_status(latest),
            record_type=record_type_label(latest),
            updated_at=record_updated_at(latest),
            started_at=latest_text(valid_records, "started_at"),
            log_path=latest_log_path(valid_records),
            exit_code=record_exit_code(latest),
            session_id=latest_text(valid_records, "session_id") or run_id,
            session_id_source=latest_text(valid_records, "session_id_source"),
            transcript_path=latest_text(valid_records, "transcript_path"),
            message=latest_text(valid_records, "message"),
            agent_kind=latest_text(valid_records, "agent_kind"),
            agent_prompt_dialect=latest_text(valid_records, "agent_prompt_dialect"),
            agent_prompt_dialect_source=latest_text(
                valid_records,
                "agent_prompt_dialect_source",
            ),
            agent_skill_ref_prefix=latest_text(
                valid_records,
                "agent_skill_ref_prefix",
            ),
            agent_skill_ref_prefix_source=latest_text(
                valid_records,
                "agent_skill_ref_prefix_source",
            ),
            model_provider=latest_text(valid_records, "model_provider"),
            model_provider_source=latest_text(valid_records, "model_provider_source"),
            model_id=latest_text(valid_records, "model_id"),
            model_id_source=latest_text(valid_records, "model_id_source"),
            reasoning_effort=latest_text(valid_records, "reasoning_effort"),
            reasoning_effort_source=latest_text(
                valid_records,
                "reasoning_effort_source",
            ),
            trailer_context=latest_mapping(valid_records, "trailer_context"),
            trailer_context_sources=latest_mapping(
                valid_records,
                "trailer_context_sources",
            ),
            classification_source=latest_text(valid_records, "classification_source"),
            worker_report=latest_worker_report_payload(valid_records),
            stats=latest_mapping(valid_records, "stats"),
            restart_count=latest_int(records, "restart_count") or 0,
            max_restarts=latest_int(records, "max_restarts") or 0,
            restart_exhausted=latest_restart_exhausted(records),
            restart_exhausted_reason=latest_restart_exhausted_reason(records),
            record_count=len(records),
            latest_record=latest,
            lifecycle_progress=derive_run_lifecycle(records),
        )

    def to_json(self) -> dict[str, object]:
        payload = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "record_type": self.record_type,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "log": str(self.log_path) if self.log_path is not None else "",
            "exit_code": self.exit_code,
            "session_id": self.session_id,
            "session_id_source": self.session_id_source,
            "transcript_path": self.transcript_path,
            "message": self.message,
            "agent_kind": self.agent_kind,
            "agent_prompt_dialect": self.agent_prompt_dialect,
            "agent_prompt_dialect_source": self.agent_prompt_dialect_source,
            "agent_skill_ref_prefix": self.agent_skill_ref_prefix,
            "agent_skill_ref_prefix_source": self.agent_skill_ref_prefix_source,
            "model_provider": self.model_provider,
            "model_provider_source": self.model_provider_source,
            "model_id": self.model_id,
            "model_id_source": self.model_id_source,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_effort_source": self.reasoning_effort_source,
            "trailer_context": self.trailer_context,
            "trailer_context_sources": self.trailer_context_sources,
            "classification_source": self.classification_source,
            "worker_report": self.worker_report,
            "stats": self.stats,
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
            "restart_exhausted": self.restart_exhausted,
            "restart_exhausted_reason": self.restart_exhausted_reason,
            "record_count": self.record_count,
            "latest_record": self.latest_record,
        }
        payload.update(self.lifecycle_progress.to_json())
        return payload


@dataclasses.dataclass(frozen=True)
class RunInspection:
    view: RunHistoryView
    records: list[dict[str, Any]]

    def to_json(self) -> dict[str, object]:
        payload = self.view.to_json()
        payload["records"] = self.records
        return payload


class RunStore:
    def __init__(self, path: Path):
        self.path = path

    def append_result(self, result: RunResult) -> None:
        self.append_record(result.to_record())

    def append_report(self, report: WorkerReport) -> None:
        self.append_record(report.to_record(), fencing_token=report.fencing_token)

    def append_lifecycle_event(self, event: RunLifecycleEvent) -> None:
        self.append_record(event.to_record())

    def append_record(
        self,
        record: dict[str, object],
        *,
        fencing_token: str = "",
    ) -> None:
        redacted = redact_run_record(record, fencing_token=fencing_token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(redacted, separators=(",", ":")) + "\n")
                    handle.flush()

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and is_known_record_type(payload):
                redacted = redact_run_record(payload)
                records.append(redacted)
        return records

    def recent_records(self, max_runs: int = 5) -> list[dict[str, Any]]:
        return self.read_records()[-max_runs:]

    def recent_result_records(self, max_runs: int = 5) -> list[dict[str, Any]]:
        return [
            record
            for record in self.read_records()
            if record.get("record_type") in {None, RUN_RECORD_TYPE}
        ][-max_runs:]

    def latest_worker_report(
        self,
        run_id: str,
        task_id: str | None = None,
    ) -> WorkerReport | None:
        for record in reversed(self.read_records()):
            report = WorkerReport.from_record(record)
            if report is None or report.run_id != run_id:
                continue
            if task_id is not None and report.task_id != task_id:
                continue
            return report
        return None

    def latest_workspace_claim_record(
        self,
        task_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        for record in reversed(self.read_records()):
            if record.get("record_type") != WORKSPACE_CLAIM_RECORD_TYPE:
                continue
            if not workspace_claim_is_observed(record):
                continue
            if string_value(record.get("task_id")) != task_id:
                continue
            if string_value(record.get("run_id")) != run_id:
                continue
            return record
        return None

    def list_runs(self, limit: int = 20) -> list[RunHistoryView]:
        return build_run_history_views(self.read_records(), limit=limit)

    def inspect_run(self, run_id: str) -> RunInspection | None:
        records = [
            record
            for record in self.read_records()
            if string_value(record.get("run_id")) == run_id
        ]
        if not run_history_view_records(records):
            return None
        return RunInspection(
            view=RunHistoryView.from_records(run_id, records),
            records=records,
        )

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        records = self.recent_result_records(max_runs)
        if not records:
            return "No prior vibe-loop runs recorded."
        chunks = ["Recent vibe-loop runs:"]
        for record in records:
            chunks.append(json.dumps(record, sort_keys=True))
            log_path = record_log_path(record)
            if log_path is not None:
                chunks.append(f"Log tail for {log_path}:")
                chunks.extend(tail(log_path, tail_lines))
        return "\n".join(chunks)


def record_log_path(record: dict[str, Any]) -> Path | None:
    record_type = record.get("record_type")
    if record_type not in {None, RUN_RECORD_TYPE}:
        return None
    log = record.get("log")
    if not isinstance(log, str) or not log:
        return None
    path = Path(log)
    if not path.is_file():
        return None
    return path


def build_run_history_views(
    records: list[dict[str, Any]],
    *,
    limit: int = 20,
) -> list[RunHistoryView]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, record in enumerate(records):
        run_id = string_value(record.get("run_id"))
        if not run_id:
            continue
        if not is_run_history_record(record):
            continue
        grouped.setdefault(run_id, []).append((index, record))
    ordered = sorted(
        grouped,
        key=lambda run_id: run_history_order_index(grouped[run_id]),
        reverse=True,
    )
    ordered = ordered[:limit]
    return [
        RunHistoryView.from_records(
            run_id,
            [record for _index, record in grouped[run_id]],
        )
        for run_id in ordered
    ]


def empty_run_lifecycle() -> RunLifecycleProgress:
    return derive_run_lifecycle([])


def derive_run_lifecycle(records: list[dict[str, Any]]) -> RunLifecycleProgress:
    observed: dict[str, RunLifecycleTransition] = {}
    for record in records:
        for state in observed_lifecycle_states(record):
            observed.setdefault(
                state,
                RunLifecycleTransition(
                    state=state,
                    observed=True,
                    record_type=record_type_label(record),
                    occurred_at=record_updated_at(record),
                ),
            )
    transitions = tuple(
        observed.get(state)
        or RunLifecycleTransition(
            state=state,
            observed=False,
        )
        for state in LIFECYCLE_STATES
    )
    current_state = ""
    for transition in transitions:
        if transition.observed:
            current_state = transition.state
    return RunLifecycleProgress(state=current_state, transitions=transitions)


def observed_lifecycle_states(record: dict[str, Any]) -> tuple[str, ...]:
    record_type = record.get("record_type")
    states: list[str] = []
    if record_type in {
        LOCK_ACQUIRED_RECORD_TYPE,
        LOCK_RELEASED_RECORD_TYPE,
        LOCK_EXPIRED_RECORD_TYPE,
    } and lock_record_is_task_scoped(record):
        states.append("scheduled")
    if record_type == RUN_STARTED_RECORD_TYPE:
        states.append("started")
    if record_type == AGENT_CONTEXT_OBSERVED_RECORD_TYPE and string_value(
        record.get("session_id")
    ):
        states.append("session_observed")
    if record_type == RUN_STATE_TRANSITION_RECORD_TYPE:
        to_state = string_value(record.get("to_state"))
        if to_state in LIFECYCLE_STATES:
            states.append(to_state)
    if record_type == WORKSPACE_CLAIM_RECORD_TYPE and workspace_claim_is_observed(
        record
    ):
        states.append("workspace_claimed")
    if WorkerReport.from_record(record) is not None:
        states.append("reported")
    if record_type in {None, RUN_RECORD_TYPE}:
        if isinstance(record.get("worker_report"), dict):
            states.append("reported")
        states.extend(("classified", "finalized"))
    return tuple(dict.fromkeys(states))


def lock_record_is_task_scoped(record: dict[str, Any]) -> bool:
    lock_kind = string_value(record.get("lock_kind"))
    return lock_kind in {"", "task"}


def workspace_claim_is_observed(record: dict[str, Any]) -> bool:
    event_type = string_value(record.get("event_type"))
    return event_type in {"", WORKSPACE_CLAIMED_EVENT_TYPE}


def run_history_order_index(records: list[tuple[int, dict[str, Any]]]) -> int:
    status_records = [
        (index, record) for index, record in records if is_run_status_record(record)
    ]
    if status_records:
        return status_records[-1][0]
    return records[-1][0]


def run_history_view_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_records = [record for record in records if is_run_status_record(record)]
    if status_records:
        return status_records
    return [record for record in records if is_lifecycle_event_record(record)]


def is_run_history_record(record: dict[str, Any]) -> bool:
    return is_run_status_record(record) or is_lifecycle_event_record(record)


def is_run_status_record(record: dict[str, Any]) -> bool:
    record_type = record.get("record_type")
    if record_type in {None, RUN_RECORD_TYPE}:
        return True
    if record_type == WORKER_REPORT_RECORD_TYPE:
        return WorkerReport.from_record(record) is not None
    return False


def is_lifecycle_event_record(record: dict[str, Any]) -> bool:
    return record.get("record_type") in LIFECYCLE_RECORD_TYPES


def is_known_record_type(record: dict[str, Any]) -> bool:
    record_type = record.get("record_type")
    return record_type is None or record_type in KNOWN_RECORD_TYPES


def record_type_label(record: dict[str, Any]) -> str:
    record_type = string_value(record.get("record_type"))
    return record_type or RUN_RECORD_TYPE


def record_status(record: dict[str, Any]) -> str:
    status = string_value(record.get("status")) or string_value(
        record.get("classification")
    )
    if status:
        return status
    record_type = record.get("record_type")
    if record_type == RUN_STATE_TRANSITION_RECORD_TYPE:
        return string_value(record.get("to_state"))
    if record_type == RUN_STARTED_RECORD_TYPE:
        return "started"
    if record_type == WORKER_PROCESS_STARTED_RECORD_TYPE:
        return "started"
    if record_type == AGENT_CONTEXT_OBSERVED_RECORD_TYPE:
        return string_value(record.get("status")) or "observed"
    if record_type == WORKSPACE_CLAIM_RECORD_TYPE:
        return string_value(record.get("event_type")) or WORKSPACE_CLAIMED_EVENT_TYPE
    if record_type == WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE:
        return string_value(record.get("reason")) or "mismatch"
    if record_type == TASK_RESTART_RECORD_TYPE:
        if record.get("exhausted") is True:
            return string_value(record.get("reason")) or "restart_budget_exhausted"
        return "restart_scheduled"
    if record_type in {
        LOCK_ACQUIRED_RECORD_TYPE,
        LOCK_RELEASED_RECORD_TYPE,
        LOCK_EXPIRED_RECORD_TYPE,
    }:
        return str(record_type).removeprefix("lock_")
    return ""


def record_updated_at(record: dict[str, Any]) -> str:
    return (
        string_value(record.get("finished_at"))
        or string_value(record.get("reported_at"))
        or string_value(record.get("occurred_at"))
        or string_value(record.get("claimed_at"))
    )


def record_exit_code(record: dict[str, Any]) -> int | None:
    value = record.get("exit_code")
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def latest_log_path(records: list[dict[str, Any]]) -> Path | None:
    for record in reversed(records):
        log = string_value(record.get("log"))
        if log:
            return Path(log)
    return None


def latest_worker_report_payload(
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for record in reversed(records):
        worker_report = record.get("worker_report")
        if isinstance(worker_report, dict):
            return worker_report
        report = WorkerReport.from_record(record)
        if report is not None:
            return report.to_json()
    return None


def latest_text(records: list[dict[str, Any]], key: str) -> str:
    for record in reversed(records):
        value = string_value(record.get(key))
        if value:
            return value
    return ""


def latest_mapping(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for record in reversed(records):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def latest_int(records: list[dict[str, Any]], key: str) -> int | None:
    for record in reversed(records):
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def latest_restart_exhausted(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("record_type") == TASK_RESTART_RECORD_TYPE
        and record.get("exhausted") is True
        for record in records
    )


def latest_restart_exhausted_reason(records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        if (
            record.get("record_type") == TASK_RESTART_RECORD_TYPE
            and record.get("exhausted") is True
        ):
            return string_value(record.get("reason"))
    return ""


def tail(path: Path, line_count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-line_count:]


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


@contextmanager
def append_record_lock(path: Path):
    if fcntl is None and msvcrt is None:
        with append_record_directory_lock(path):
            yield
        return

    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        ensure_lock_byte(handle)
        lock_file(handle)
        try:
            yield
        finally:
            unlock_file(handle)


@contextmanager
def append_record_directory_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lockdir")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_path.mkdir()
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring append lock: {lock_path}")
            time.sleep(LOCK_POLL_SECONDS)
        else:
            break
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def lock_file(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def unlock_file(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
