from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TextIO

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentConfig,
    AgentDetection,
    AgentResolutionError,
    VibeConfig,
    command_template_uses_field,
    format_agent_command,
    require_project_binding,
    resolve_task_agent,
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
from vibe_loop.locks import (
    LockBackendError,
    LockBusy,
    LockFencingMismatch,
    LockManager,
    LockOwnerMismatch,
    SettledOutcomeNotPersisted,
    TaskLock,
    FENCING_TOKEN_REDACTION,
    build_lock_manager,
    fencing_token_value,
    redact_exact_fencing_token,
    redact_fencing_token_payload,
    redact_fencing_token_text,
)
from vibe_loop.orchestration import (
    RunContractResolver,
    RunLifecycleStateMachine,
    RunStage,
    StageFailure,
)
from vibe_loop.processes import read_process_node
from vibe_loop.retry import (
    LimitWallSignal,
    detect_limit_wall,
    is_transient_stderr,
    limit_wall_backoff_seconds,
    parse_quota_reset_delay,
    retry_subprocess_run,
)
from vibe_loop.runs import (
    LIFECYCLE_EVENT_SCHEMA_VERSION,
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_FINALIZATION_FAILED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    RUN_SUPERVISOR_EXITED_RECORD_TYPE,
    RUN_SUPERVISOR_STARTED_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    UNKNOWN_RUN_OUTCOME,
    WorkerReport,
    settled_run_outcome,
    utc_now_iso,
)
from vibe_loop.spec_diagnostics import ensure_spec_execution_gate
from vibe_loop.telemetry import (
    PHASES,
    WORK_KINDS,
    ProviderUsage,
    ProviderUsageObserver,
    parse_claude_transcript_usage,
    parse_codex_rollout_usage,
    unavailable_usage,
)
from vibe_loop.tasks import (
    BLOCKED_FAMILY_STATUSES,
    Task,
    TaskSource,
    build_task_source,
    runnable_tasks_from_snapshot,
)
from vibe_loop.workers import (
    ActiveRunState,
    WorkerView,
    WorkspaceClaim,
    active_run_is_live,
    build_worker_views,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


SESSION_ID_RE = re.compile(
    r"\b(?:session|thread)(?:[_ -]?id)[\"']?\s*[:=]\s*[\"']?"
    r"(?P<session_id>[A-Za-z0-9](?:[A-Za-z0-9_.:/+-]*[A-Za-z0-9])?)\b",
    re.IGNORECASE,
)
AGENT_CONTEXT_RE = re.compile(
    r"\b(?P<key>model(?:[_ -]?(?:provider|id))?|provider|"
    r"reasoning[_ -]?effort|effort)\s*[:=]\s*"
    r"(?P<value>\"[^\"]+\"|'[^']+'|[^\s,;]+)",
    re.IGNORECASE,
)
SHA256_HEX_RE = re.compile(r"^[a-fA-F0-9]{64}$")
# A bare top-level string `model` value is only a model identity inside these
# structured lifecycle events. Any other JSON object carrying a `model` key
# (tool payloads, task records, nested agent envelopes) is generic data.
MODEL_IDENTITY_EVENT_TYPES = frozenset(
    {
        "assistant",
        "init",
        "result",
        "session.created",
        "session.start",
        "session_configured",
        "system",
        "thread.started",
        "turn.completed",
        "turn.started",
    }
)
# Higher rank wins when two observations disagree about the same field. Command
# arguments are explicit operator intent; structured native events outrank both
# free-text log scraping and executable-name inference.
AGENT_CONTEXT_SOURCE_RANKS = (
    ("command_arg:", 40),
    ("command_config:", 35),
    ("native:", 30),
    ("command_executable:", 10),
)
# Free-text log scraping (`native:stdout:model`) is weaker than a structured
# native event (`native:stdout:json.model`) even though both are native.
AGENT_CONTEXT_UNSTRUCTURED_NATIVE_RANK = 20
CLAUDE_MODEL_ALIASES = frozenset({"haiku", "opus", "sonnet"})
AGENT_CONTEXT_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
REASONING_EFFORT_VALUES = frozenset({"minimal", "low", "medium", "high", "xhigh"})
SECRET_LIKE_CONTEXT_TOKENS = (
    "api_key",
    "apikey",
    "auth",
    "bearer",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
)
AGENT_STARTUP_OBSERVATION_LINE_LIMIT = 80
AGENT_CONTEXT_VALUE_MAX_CHARS = 160
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

FENCING_TOKEN_NONDISCLOSURE = """\
VIBE_LOOP_FENCING_TOKEN is a secret. Never print or echo its value, include it
in a prompt, report, command argument, tool payload, log, or summary, or expose
it by any other means. Use it only through the environment for the lock
protocol commands that require it."""

CURRENT_RUN_WORKSPACE_CLAIM_COMMAND = (
    'vibe-loop worker claim-workspace --repo "$VIBE_LOOP_REPO" \\\n'
    '  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \\\n'
    '  --branch "$(git branch --show-current)" \\\n'
    '  --worktree "$(git rev-parse --show-toplevel)"'
)

CLI_WORKER_ADDENDUM = f"""\

## vibe-loop CLI Coordination

You are running as a worker launched by the vibe-loop CLI. The following
environment variables identify this run:
- VIBE_LOOP_REPO - path to the repository
- VIBE_LOOP_RUN_ID - unique run identifier
- VIBE_LOOP_TASK_ID - task being worked on
- VIBE_LOOP_LOG - path to the run log file
- VIBE_LOOP_FENCING_TOKEN - optional lock generation token when present

{FENCING_TOKEN_NONDISCLOSURE}

### Task Activation

For command-backed task sources, the supervisor acquired this run's exact task
lock, invoked the configured task lifecycle adapter, and confirmed that the task
is in a non-runnable in-progress state before starting this worker process. The
activation is project task-source state; it is not a worker report and does not
complete the task. If repository evidence contradicts that confirmed state,
stop before workspace mutation and report the run as blocked.

### Headless Completion

A headless worker must not end its turn while any asynchronous
Agent/Task/Workflow subagent, gate, build, test, or other worker-started
operation remains in flight. Before returning, await or collect every result,
then finish review, integration, and reporting, or explicitly report the run as
blocked or failed. Launching background work and returning a progress summary
is not terminal completion.

### Workspace Claim

After creating or choosing your task branch/worktree, and before implementation
edits, verify its real branch and absolute path. Run this command from inside
that verified worktree to attach it to the active task lock:

```bash
{CURRENT_RUN_WORKSPACE_CLAIM_COMMAND}
```

The quoted Git-derived values keep branch names and paths as single literal
arguments. If the claim fails with an owner mismatch, missing active task lock,
mismatched branch/worktree, or unsafe workspace diagnostic, stop mutating
repository state and report the run as blocked through the worker report
protocol.
Workspace claims are advisory visibility metadata only;
they do not permit deleting,
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

After final review/re-review has passed and immediately before the final
fast-forward merge to main, acquire the advisory main-integration lock:

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

Do not hold the integration lock while waiting for review, re-review, human
input, long-running checks, remediation, or any other non-integration work. If
integration cannot proceed immediately after acquiring the lock, release it and
reacquire it only when the final main merge and main verification can run.

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
class AgentRuntimeContext:
    model_provider: str = ""
    model_provider_source: str = ""
    model_id: str = ""
    model_id_source: str = ""
    reasoning_effort: str = ""
    reasoning_effort_source: str = ""

    @property
    def empty(self) -> bool:
        return not (self.model_provider or self.model_id or self.reasoning_effort)

    def overlay(self, other: AgentRuntimeContext) -> AgentRuntimeContext:
        return AgentRuntimeContext(
            model_provider=other.model_provider or self.model_provider,
            model_provider_source=(
                other.model_provider_source or self.model_provider_source
            ),
            model_id=other.model_id or self.model_id,
            model_id_source=other.model_id_source or self.model_id_source,
            reasoning_effort=other.reasoning_effort or self.reasoning_effort,
            reasoning_effort_source=(
                other.reasoning_effort_source or self.reasoning_effort_source
            ),
        )

    def prefer(self, other: AgentRuntimeContext) -> AgentRuntimeContext:
        """Merge `other` over self, but only where `other` is at least as
        authoritative. Prevents a weak stream observation from overwriting an
        explicit command-line or structured model identity."""
        provider, provider_source = pick_agent_context_field(
            self.model_provider,
            self.model_provider_source,
            other.model_provider,
            other.model_provider_source,
        )
        model_id, model_id_source = pick_agent_model_field(
            self.model_id,
            self.model_id_source,
            other.model_id,
            other.model_id_source,
            current_provider=self.model_provider,
        )
        effort, effort_source = pick_agent_context_field(
            self.reasoning_effort,
            self.reasoning_effort_source,
            other.reasoning_effort,
            other.reasoning_effort_source,
        )
        return AgentRuntimeContext(
            model_provider=provider,
            model_provider_source=provider_source,
            model_id=model_id,
            model_id_source=model_id_source,
            reasoning_effort=effort,
            reasoning_effort_source=effort_source,
        )

    def missing_delta(self, candidate: AgentRuntimeContext) -> AgentRuntimeContext:
        """Fields `candidate` contributes that self does not already hold at an
        equal-or-stronger source rank."""
        merged = self.prefer(candidate)
        provider_changed = (
            merged.model_provider,
            merged.model_provider_source,
        ) != (self.model_provider, self.model_provider_source)
        model_changed = (merged.model_id, merged.model_id_source) != (
            self.model_id,
            self.model_id_source,
        )
        effort_changed = (
            merged.reasoning_effort,
            merged.reasoning_effort_source,
        ) != (self.reasoning_effort, self.reasoning_effort_source)
        return AgentRuntimeContext(
            model_provider=(merged.model_provider if provider_changed else ""),
            model_provider_source=(
                merged.model_provider_source if provider_changed else ""
            ),
            model_id=(merged.model_id if model_changed else ""),
            model_id_source=(merged.model_id_source if model_changed else ""),
            reasoning_effort=(merged.reasoning_effort if effort_changed else ""),
            reasoning_effort_source=(
                merged.reasoning_effort_source if effort_changed else ""
            ),
        )

    def to_record_fields(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.model_provider:
            payload["model_provider"] = self.model_provider
            payload["model_provider_source"] = self.model_provider_source
        if self.model_id:
            payload["model"] = self.model_id
            payload["model_source"] = self.model_id_source
            payload["model_id"] = self.model_id
            payload["model_id_source"] = self.model_id_source
        if self.reasoning_effort:
            payload["effort"] = self.reasoning_effort
            payload["effort_source"] = self.reasoning_effort_source
            payload["reasoning_effort"] = self.reasoning_effort
            payload["reasoning_effort_source"] = self.reasoning_effort_source
        return payload


def configured_agent_effort_context(agent: AgentConfig) -> AgentRuntimeContext:
    if agent.effort is None:
        return AgentRuntimeContext()
    return AgentRuntimeContext(
        reasoning_effort=agent.effort,
        reasoning_effort_source=f"config:agent.effort:{agent.effort_source}",
    )


@dataclasses.dataclass(frozen=True)
class AgentRuntimeObservation:
    session_id: str | None = None
    session_id_source: str | None = None
    runtime_context: AgentRuntimeContext = dataclasses.field(
        default_factory=AgentRuntimeContext
    )

    @property
    def empty(self) -> bool:
        return (
            self.session_id is None
            and self.session_id_source is None
            and self.runtime_context.empty
        )


@dataclasses.dataclass(frozen=True)
class StreamingCommandResult:
    exit_code: int
    session_id: str | None = None
    session_id_source: str | None = None
    runtime_context: AgentRuntimeContext = dataclasses.field(
        default_factory=AgentRuntimeContext
    )
    # True when the worker exceeded its configured wall-clock timeout and its
    # process group was force-killed rather than exiting on its own.
    timed_out: bool = False
    usage: ProviderUsage = dataclasses.field(
        default_factory=lambda: unavailable_usage(
            "unknown", "provider_usage_not_reported"
        )
    )


@dataclasses.dataclass(frozen=True)
class ClassificationResult:
    status: str
    source: str
    # Optional human-readable context for the outcome (e.g. the advertised
    # reset phrase for a limit_wall), persisted into the run result message.
    detail: str = ""


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


class TaskActivationError(RuntimeError):
    """A command task source could not confirm its pre-launch claim."""


class AgentLimitWallError(RuntimeError):
    """An agent subprocess refused work because an account limit was reached.

    Raised instead of returning a generic failure so callers can pause until
    ``pause_seconds`` rather than treating the refusal as a short transient.
    ``pause_seconds`` is the advertised reset delay when the wall carried one,
    otherwise the configured backoff. Deliberately not a ValueError/OSError
    subclass: callers catch those as ordinary agent errors, and a wall must
    stay distinguishable from them.
    """

    def __init__(self, signal: LimitWallSignal, *, default_backoff: float) -> None:
        self.signal = signal
        self.pause_seconds = limit_wall_backoff_seconds(signal, default_backoff)
        detail = f" ({signal.reset_text})" if signal.reset_text else ""
        super().__init__(f"agent limit wall: {signal.marker}{detail}")


class AgentOutputObserver:
    def __init__(self, provider: str = "unknown") -> None:
        self._lock = threading.Lock()
        self._session_observation: SessionIdObservation | None = None
        self._runtime_context = AgentRuntimeContext()
        self._line_count = 0
        self._usage_observer = ProviderUsageObserver(provider)

    @property
    def usage(self) -> ProviderUsage:
        return self._usage_observer.usage

    @property
    def observation(self) -> AgentRuntimeObservation:
        with self._lock:
            return AgentRuntimeObservation(
                session_id=(
                    self._session_observation.session_id
                    if self._session_observation is not None
                    else None
                ),
                session_id_source=(
                    self._session_observation.source
                    if self._session_observation is not None
                    else None
                ),
                runtime_context=self._runtime_context,
            )

    def observe_line(
        self,
        line: str,
        stream_name: str,
    ) -> AgentRuntimeObservation | None:
        self._usage_observer.observe_line(line)
        session_id = parse_worker_session_id(line)
        runtime_context = AgentRuntimeContext()
        with self._lock:
            self._line_count += 1
            should_parse_context = (
                self._line_count <= AGENT_STARTUP_OBSERVATION_LINE_LIMIT
            )
        if should_parse_context:
            runtime_context = parse_agent_runtime_context_from_line(
                line,
                stream_name,
            )
        with self._lock:
            delta_session_id = None
            delta_session_id_source = None
            if session_id is not None and self._session_observation is None:
                self._session_observation = SessionIdObservation(
                    session_id=session_id,
                    source=f"native:{stream_name}",
                )
                delta_session_id = session_id
                delta_session_id_source = f"native:{stream_name}"
            delta_context = self._runtime_context.missing_delta(runtime_context)
            if not delta_context.empty:
                self._runtime_context = self._runtime_context.overlay(delta_context)
            if delta_session_id is None and delta_context.empty:
                return None
            return AgentRuntimeObservation(
                session_id=delta_session_id,
                session_id_source=delta_session_id_source,
                runtime_context=delta_context,
            )


class VibeRunner:
    def __init__(self, config: VibeConfig):
        self.config = config
        self._source: TaskSource | None = None
        self._source_resolution: RuntimeTaskSourceResolution | None = None
        self._lock_manager: LockManager | None = None
        self.runs_dir = config.state_path / "runs"
        self.run_store = RunStore(config.state_path / "runs.jsonl")
        self._record_lock = threading.Lock()
        self._restart_context = threading.local()
        self.last_analysis_usage = unavailable_usage(
            "unknown", "provider_usage_not_reported"
        )
        self.last_analysis_runtime_context = AgentRuntimeContext()
        # Terminal results recorded by an exhausting recovery run before it
        # released its lock, keyed by run id and consumed by the recovery
        # driver so the verdict is written exactly once.
        self._exhausted_recovery_results: dict[str, RunResult] = {}

    @property
    def lock_manager(self) -> LockManager:
        if self._lock_manager is None:
            # Querying a command lock backend is as much a cross-project
            # effect as listing tasks, so construction is gated the same way.
            require_project_binding(self.config)
            self._lock_manager = build_lock_manager(
                self.config.repo,
                self.config.state_path / "locks",
                self.config.locks,
                runtime_context=self.config.runtime_environment,
            )
        return self._lock_manager

    @property
    def source_resolution(self) -> RuntimeTaskSourceResolution:
        if self._source_resolution is None:
            self._source_resolution = resolve_runtime_task_source(self.config)
        return self._source_resolution

    @property
    def source(self) -> TaskSource:
        if self._source is None:
            # Listing tasks is already an observable cross-project effect for
            # a command backend, so the binding gates construction rather than
            # only the dispatch entry points.
            require_project_binding(self.config)
            self._source = build_task_source(
                self.config.repo,
                self.source_resolution.task_source,
                runtime_context=self.config.runtime_environment,
            )
        return self._source

    def list_candidates(self, exclude: set[str] | None = None) -> list[Task]:
        return self.list_candidates_from_snapshot(
            self.source.list_tasks(), exclude=exclude
        )

    def list_candidates_from_snapshot(
        self,
        tasks: list[Task],
        exclude: set[str] | None = None,
        *,
        active_runs: tuple[ActiveRunState, ...] | None = None,
    ) -> list[Task]:
        excluded = exclude or set()
        candidates = runnable_tasks_from_snapshot(
            tasks,
            self.source_resolution.task_source.runnable_statuses,
            self.source_resolution.task_source.respect_source_order,
        )
        if active_runs is None:
            active_domains = active_lock_conflict_domains(self.lock_manager)
            locked_task_ids: set[str] | None = None
        else:
            active_domains = tuple(
                conflict_domains_from_task_like(active) for active in active_runs
            )
            locked_task_ids = {active.task_id for active in active_runs}
        enforce_conflicts = resource_conflicts_enabled(candidates, active_domains)
        return [
            task
            for task in candidates
            if task.task_id not in excluded
            and (
                locked_task_ids is not None
                and task.task_id not in locked_task_ids
                or locked_task_ids is None
                and not self.lock_manager.is_locked(task.task_id)
            )
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
        command_str = format_agent_command(
            command_template,
            prompt=prompt,
            model=self.config.agent.model,
            effort=self.config.agent.effort,
        )
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

    def _limit_wall_retry_options(self) -> dict[str, object]:
        supervision = self.config.supervision
        return {
            "detect_limit_walls": supervision.limit_wall_detection,
            "limit_wall_patterns": supervision.limit_wall_patterns or None,
        }

    def run_analysis_agent(
        self,
        prompt: str,
        output_path: Path,
    ) -> dict[str, object] | None:
        command_template = self.config.agent.require_analysis_command()
        report_status(
            "agent analysis command source: "
            f"{self.config.agent.analysis_command_source}"
        )
        validate_analysis_prompt_delivery(command_template)
        command_str = format_agent_command(
            command_template,
            prompt=prompt,
            model=self.config.agent.model,
            effort=self.config.agent.effort,
        )
        command_str = inject_structured_usage_output(
            command_str, self.config.agent.agent_kind
        )
        self.last_analysis_runtime_context = parse_agent_runtime_context_from_command(
            command_str
        )
        cmd, use_shell = prepare_shell_command(command_str)
        walls: list[LimitWallSignal] = []
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
                interrupt_process_group=True,
                on_retry=_analysis_retry_callback,
                on_limit_wall=walls.append,
                **self._limit_wall_retry_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report_status(f"analysis agent failed to start: {exc}")
            return None
        provider = self.last_analysis_runtime_context.model_provider or {
            "codex": "openai",
            "claude": "anthropic",
        }.get(self.config.agent.agent_kind, "unknown")
        usage_observer = ProviderUsageObserver(provider)
        for line in (result.stdout or "").splitlines():
            usage_observer.observe_line(line)
        self.last_analysis_usage = usage_observer.usage
        if walls:
            raise AgentLimitWallError(
                walls[0],
                default_backoff=self.config.supervision.limit_wall_backoff_seconds,
            )
        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-500:]
            report_status(f"analysis agent exited {result.returncode}: {stderr_tail}")
            return None
        payload = selection_payload_from_output(result.stdout)
        if not isinstance(payload, dict):
            report_status("analysis agent output contained no JSON object payload")
            return None
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

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
        command_str = format_agent_command(
            command_template,
            prompt=prompt,
            model=self.config.agent.model,
            effort=self.config.agent.effort,
        )
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

    def run_task_with_supervision(
        self,
        task: Task,
        *,
        restart_count: int = 0,
    ) -> RunResult:
        previous = getattr(self._restart_context, "value", None)
        self._restart_context.value = (task.task_id, restart_count)
        try:
            try:
                return self.run_task(task)
            except AgentResolutionError as exc:
                explicit_agent = (task.agent or "").strip()
                if not explicit_agent or explicit_agent in self.config.agent_profiles:
                    raise
                return self.record_agent_resolution_failure(task, exc)
        finally:
            if previous is None:
                try:
                    del self._restart_context.value
                except AttributeError:
                    pass
            else:
                self._restart_context.value = previous

    def record_agent_resolution_failure(
        self,
        task: Task,
        error: AgentResolutionError,
    ) -> RunResult:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(task.task_id)
        log_path = self.runs_dir / f"{run_id}.log"
        started_at = utc_now_iso()
        start_main = git_rev_parse(self.config.repo, "HEAD")
        message = str(error)
        with log_path.open("w", encoding="utf-8") as log:
            report_status(
                f"agent resolution failed for {task.task_id}: {message}",
                log,
            )
        result = RunResult(
            run_id=run_id,
            task_id=task.task_id,
            classification="failed",
            exit_code=1,
            log_path=log_path,
            start_main=start_main,
            end_main=git_rev_parse(self.config.repo, "HEAD"),
            message=message,
            started_at=started_at,
            classification_source="agent_resolution",
            restart_count=self.current_restart_count(task.task_id),
            max_restarts=self.config.supervision.max_restarts,
        )
        self.record_result(result)
        report_status(f"recorded failed result for {task.task_id}: {log_path}")
        return result

    def current_restart_count(self, task_id: str) -> int:
        value = getattr(self._restart_context, "value", None)
        if not isinstance(value, tuple) or len(value) != 2:
            return 0
        context_task_id, restart_count = value
        if context_task_id != task_id or not isinstance(restart_count, int):
            return 0
        return max(0, restart_count)

    def run_task(
        self,
        task: Task,
        *,
        recovery: RecoveryContext | None = None,
    ) -> RunResult:
        self.ensure_spec_execution_gate()
        agent_selection = resolve_task_agent(self.config, task)
        agent = agent_selection.config
        agent_profile = agent_selection.profile
        command_template = agent.require_command()
        agent_kind = agent.executable_kind or agent.agent_kind
        agent_kind_source = (
            agent.command_source
            if agent.agent_kind == "auto" and agent.executable_kind
            else agent.agent_kind_source
        )
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(task.task_id)
        log_path = self.runs_dir / f"{run_id}.log"
        start_main = git_rev_parse(self.config.repo, "HEAD")
        base_main = git_rev_parse(self.config.repo, self.config.main_branch)
        restart_count = self.current_restart_count(task.task_id)
        max_restarts = self.config.supervision.max_restarts
        exit_code = 1
        message = ""
        session_id = run_id
        session_id_source = "fallback:run_id"
        injected_session_id: str | None = None
        effective_template = command_template
        resume_session_id = (
            recovery.prior_session_id
            if recovery is not None and recovery.prior_session_id
            else ""
        )
        resuming = bool(
            resume_session_id
            and self.config.supervision.resume_unknown_runs
            and command_supports_session_resume(command_template, agent_kind)
        )
        if resuming:
            # Resume the prior run's captured session so the continuation turn
            # keeps its full context (e.g. background proofs it launched before
            # the previous headless turn ended) rather than re-investigating
            # from scratch. `injected_session_id` is reused by the transcript
            # resolution below — the id is known, only the flag differs.
            injected_session_id = resume_session_id
            effective_template = inject_claude_resume(
                command_template, resume_session_id
            )
            session_id = injected_session_id
            session_id_source = SESSION_OBSERVED_SOURCE
        elif command_supports_session_capture(command_template, agent_kind):
            injected_session_id = str(uuid.uuid4())
            effective_template = inject_claude_session_id(
                command_template, injected_session_id
            )
            session_id = injected_session_id
            session_id_source = SESSION_OBSERVED_SOURCE
        effective_template = inject_structured_usage_output(
            effective_template, agent_kind
        )
        skill_prefix = agent.require_skill_ref_prefix()
        worker_prompt = build_run_worker_prompt(
            skill_prefix,
            task,
            self.config,
            recovery=recovery,
            resuming=resuming,
        )
        validate_worker_prompt_delivery(command_template, task)
        command = format_agent_command(
            effective_template,
            prompt=worker_prompt,
            model=agent.model,
            effort=agent.effort,
            task=task,
            profile=agent_profile,
            task_id=task.task_id,
            run_id=run_id,
        )
        command_env = worker_command_env(
            run_id=run_id,
            task_id=task.task_id,
            repo=self.config.repo,
            log_path=log_path,
            agent_kind=agent_kind,
            agent_profile=agent_profile,
        )
        claude_home = (
            resolve_claude_home(command, command_env, self.config.repo)
            if injected_session_id
            else None
        )
        codex_home = (
            resolve_codex_home(command, command_env, self.config.repo)
            if agent_kind in {"auto", "codex"}
            else None
        )
        transcript_path = (
            str(
                predicted_claude_transcript(
                    injected_session_id, self.config.repo, claude_home
                )
            )
            if injected_session_id and claude_home is not None
            else ""
        )
        transcript_start_offset = 0
        if resuming and transcript_path:
            try:
                transcript_start_offset = Path(transcript_path).stat().st_size
            except OSError:
                transcript_start_offset = 0
        command_context = configured_agent_effort_context(agent).prefer(
            parse_agent_runtime_context_from_command(command)
        )
        agent_prompt_dialect = agent.prompt_dialect or ""
        agent_prompt_dialect_source = agent.prompt_dialect_source
        agent_skill_ref_prefix = agent.skill_ref_prefix or ""
        agent_skill_ref_prefix_source = agent.skill_ref_prefix_source
        worker_report: WorkerReport | None = None
        worker_timed_out = False
        # Defaults hold for any exit that never reaches a durable RunResult - an
        # interrupted supervisor, a crash, a pre-classification error, a failed
        # result append - which is an honestly unknown outcome, not a failure
        # and not a completion.
        settled_outcome = "unknown"
        settled_classification = ""
        active_state = ActiveRunState.new(
            task_id=task.task_id,
            run_id=run_id,
            log_path=log_path,
            base_main=base_main,
            command=command,
            resources=task.resources,
            paths=task.paths,
            conflict_domains_known=task.conflict_domains_known,
            session_id=session_id,
            session_id_source=session_id_source,
            agent_kind=agent_kind,
            agent_profile=agent_profile,
            agent_prompt_dialect=agent_prompt_dialect,
            agent_prompt_dialect_source=agent_prompt_dialect_source,
            agent_skill_ref_prefix=agent_skill_ref_prefix,
            agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
            model_provider=command_context.model_provider,
            model_provider_source=command_context.model_provider_source,
            model_id=command_context.model_id,
            model_id_source=command_context.model_id_source,
            reasoning_effort=command_context.reasoning_effort,
            reasoning_effort_source=command_context.reasoning_effort_source,
            restart_count=restart_count,
            max_restarts=max_restarts,
        )
        start_context_payload = build_run_context_payload(
            task_id=task.task_id,
            run_id=run_id,
            started_at=active_state.started_at,
            session_id=session_id,
            session_id_source=session_id_source,
            agent_kind=agent_kind,
            agent_kind_source=agent_kind_source,
            agent_prompt_dialect=agent_prompt_dialect,
            agent_prompt_dialect_source=agent_prompt_dialect_source,
            agent_skill_ref_prefix=agent_skill_ref_prefix,
            agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
            runtime_context=command_context,
            agent_profile=agent_profile,
            transcript_path=transcript_path,
        )
        start_trailer_context = start_context_payload["trailer_context"]
        start_trailer_context_sources = start_context_payload["trailer_context_sources"]
        active_state = active_state.with_trailer_context(
            trailer_context=(
                start_trailer_context if isinstance(start_trailer_context, dict) else {}
            ),
            trailer_context_sources=(
                start_trailer_context_sources
                if isinstance(start_trailer_context_sources, dict)
                else {}
            ),
        )
        task_lock = self.acquire_scheduled_task_lock(
            task,
            run_id,
            active_state,
        )
        fencing_token = fencing_token_value(task_lock.metadata.get("fencing_token"))
        if fencing_token:
            command_env["VIBE_LOOP_FENCING_TOKEN"] = fencing_token
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
                    "started_at": active_state.started_at,
                    "restart_count": restart_count,
                    "max_restarts": max_restarts,
                },
            )
        )
        stage_machine = RunLifecycleStateMachine(
            lambda transition: self.run_store.append_lifecycle_event(
                RunLifecycleEvent.stage_transition(
                    run_id=run_id,
                    task_id=task.task_id,
                    transition=transition,
                )
            )
        )
        continuation = recovery is not None or restart_count > 0
        pre_launch_failure_reason = "run_contract_resolution_failed"

        def finalize_pre_launch_failure(failure: StageFailure) -> None:
            if stage_machine.stage is not None:
                stage_machine.fail(
                    failure,
                    reason=pre_launch_failure_reason,
                )
                stage_machine.transition(
                    RunStage.FINALIZATION,
                    reason="pre_launch_failure",
                )
            self.lock_manager.release(task_lock)
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_RELEASED_RECORD_TYPE,
                    run_id=run_id,
                    task_id=task.task_id,
                    lock_kind="task",
                    lock_path=task_lock.path,
                    payload={
                        "started_at": active_state.started_at,
                        "reason": pre_launch_failure_reason,
                    },
                )
            )

        try:
            run_contract = RunContractResolver(self.config).resolve(agent_selection)
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.run_contract_resolved(
                    run_id=run_id,
                    task_id=task.task_id,
                    contract=run_contract.to_record_payload(),
                )
            )
            stage_machine.transition(
                RunStage.ACTIVATION,
                reason="run_contract_resolved",
            )
            pre_launch_failure_reason = "task_activation_failed"
            activated_task = self.activate_task_before_launch(
                task,
                run_id,
                command_env,
                continuation=continuation,
            )
        except KeyboardInterrupt:
            finalize_pre_launch_failure(StageFailure.CANCELLED)
            raise
        except Exception:
            # Any ordinary pre-launch failure after acquisition must release
            # this run's exact task lock. Activation adapters are external and
            # can fail outside the enumerated subprocess/config exceptions.
            finalize_pre_launch_failure(StageFailure.STAGE_FAILED)
            raise
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.run_started(
                run_id=run_id,
                task_id=task.task_id,
                payload={
                    **start_context_payload,
                    "log": str(log_path),
                    "start_main": start_main,
                    "base_main": base_main,
                    "resources": list(task.resources),
                    "paths": list(task.paths),
                    "conflict_domains_known": task.conflict_domains_known,
                    "restart_count": restart_count,
                    "max_restarts": max_restarts,
                    "run_contract_digest": run_contract.digest,
                    "run_contract_version": run_contract.payload["contract_version"],
                    "orchestration_mode": run_contract.payload["mode"],
                    "reason": (
                        "task_activation_confirmed"
                        if activated_task is not None
                        else "task_lock_acquired"
                    ),
                    "task_activation_status": (
                        activated_task.status if activated_task is not None else ""
                    ),
                },
            )
        )
        stage_machine.transition(
            RunStage.WORKSPACE,
            reason="worker_owned_workspace_handoff",
        )
        observation_lock = threading.Lock()
        observed_output_context = AgentRuntimeContext()
        observed_session_id = session_id
        observed_session_id_source = session_id_source
        session_observed_recorded = False

        def update_active_task_lock() -> None:
            nonlocal active_state
            nonlocal task_lock
            current_metadata = self.lock_manager.status(task.task_id) or {}
            current_active = ActiveRunState.from_lock_metadata(current_metadata)
            if current_active is not None and current_active.workspace is not None:
                active_state = dataclasses.replace(
                    active_state,
                    workspace=current_active.workspace,
                )
            task_lock = self.lock_manager.update(
                task_lock,
                active_state.to_lock_metadata(),
            )

        def settle_outcome_and_release() -> None:
            # A backend that mirrors run provenance finalizes the run from the
            # lock row it has already stored and discards release-time payloads,
            # so writing the settled outcome onto that row is the only operation
            # that can settle the run. It runs while this supervisor still owns
            # the lock: deferring to the enclosing run-until-done child's exit
            # would race the next dispatch.
            nonlocal active_state
            # Same guard the observation callbacks use: a reader thread that
            # outlives the streaming call would otherwise replace active_state
            # from a pre-publish snapshot and drop the settled outcome.
            with observation_lock:
                active_state = active_state.with_settled_outcome(
                    settled_outcome,
                    settled_classification,
                )
                metadata = active_state.to_lock_metadata()
            if settled_outcome == UNKNOWN_RUN_OUTCOME:
                # Unknown is what a backend records for a run it was told
                # nothing about, so a failed update loses no information and
                # must not strand the lock of an interrupted or report-less run.
                try:
                    self.lock_manager.update(task_lock, metadata)
                except (
                    LockBusy,
                    LockBackendError,
                    LockOwnerMismatch,
                    LockFencingMismatch,
                    OSError,
                    ValueError,
                ) as exc:
                    report_status(
                        f"could not publish settled outcome {settled_outcome} "
                        f"for {task.task_id} before lock release: {exc}"
                    )
                self.lock_manager.release(task_lock)
                return
            self.lock_manager.release_settled(
                task_lock,
                metadata,
                outcome=settled_outcome,
            )

        def record_agent_observation(observation: AgentRuntimeObservation) -> None:
            nonlocal active_state
            nonlocal observed_output_context
            nonlocal observed_session_id
            nonlocal observed_session_id_source
            nonlocal session_observed_recorded
            with observation_lock:
                if not observation.runtime_context.empty:
                    observed_output_context = observed_output_context.overlay(
                        observation.runtime_context
                    )
                if (
                    injected_session_id is None
                    and observation.session_id
                    and observation.session_id_source
                ):
                    observed_session_id = observation.session_id
                    observed_session_id_source = observation.session_id_source
                effective_context = command_context.prefer(observed_output_context)
                context_payload = build_run_context_payload(
                    task_id=task.task_id,
                    run_id=run_id,
                    started_at=active_state.started_at,
                    session_id=observed_session_id,
                    session_id_source=observed_session_id_source,
                    agent_kind=agent_kind,
                    agent_kind_source=agent_kind_source,
                    agent_prompt_dialect=agent_prompt_dialect,
                    agent_prompt_dialect_source=agent_prompt_dialect_source,
                    agent_skill_ref_prefix=agent_skill_ref_prefix,
                    agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
                    runtime_context=effective_context,
                    agent_profile=agent_profile,
                    transcript_path=transcript_path,
                )
                trailer_context = context_payload["trailer_context"]
                trailer_context_sources = context_payload["trailer_context_sources"]
                active_state = active_state.with_trailer_context(
                    session_id=observed_session_id,
                    session_id_source=observed_session_id_source,
                    model_provider=effective_context.model_provider,
                    model_provider_source=effective_context.model_provider_source,
                    model_id=effective_context.model_id,
                    model_id_source=effective_context.model_id_source,
                    reasoning_effort=effective_context.reasoning_effort,
                    reasoning_effort_source=effective_context.reasoning_effort_source,
                    trailer_context=(
                        trailer_context if isinstance(trailer_context, dict) else {}
                    ),
                    trailer_context_sources=(
                        trailer_context_sources
                        if isinstance(trailer_context_sources, dict)
                        else {}
                    ),
                )
                update_active_task_lock()
                if observation.session_id and not session_observed_recorded:
                    self.run_store.append_lifecycle_event(
                        RunLifecycleEvent.run_state_transition(
                            run_id=run_id,
                            task_id=task.task_id,
                            from_state="started",
                            to_state="session_observed",
                            reason=observed_session_id_source,
                            payload=context_payload,
                        )
                    )
                    session_observed_recorded = True
                    return
                if not observation.runtime_context.empty:
                    self.run_store.append_lifecycle_event(
                        RunLifecycleEvent.agent_context_observed(
                            run_id=run_id,
                            task_id=task.task_id,
                            payload={**context_payload, "reason": "agent_output"},
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
                    agent.command_source,
                    agent.selection_command_source,
                    agent.detected,
                    agent_kind,
                    agent.prompt_dialect,
                    agent.prompt_dialect_source,
                    agent.skill_ref_prefix,
                    agent.skill_ref_prefix_source,
                    fencing_token=fencing_token,
                )
                report_status(f"running {task.task_id}: {task.title}", log)
                report_status(f"run_id={run_id}", log)
                report_status(f"log: {log_path}", log)
                if restart_count:
                    report_status(
                        f"restart_count={restart_count}/{max_restarts}",
                        log,
                    )
                report_status(
                    f"agent command source: {agent.command_source}",
                    log,
                )
                report_status(
                    f"agent selection command source: {agent.selection_command_source}",
                    log,
                )
                report_status(
                    f"agent default policy source: {AGENT_DEFAULT_POLICY_SOURCE}",
                    log,
                )
                report_status(f"agent default policy: {AGENT_DEFAULT_POLICY}", log)
                report_status(
                    f"agent profile: {agent_profile or 'default'} "
                    f"({agent_selection.source})",
                    log,
                )
                report_status(f"agent kind: {agent_kind}", log)
                report_status(
                    f"agent prompt dialect source: {agent.prompt_dialect_source}",
                    log,
                )
                report_status(
                    f"agent skill ref prefix source: {agent.skill_ref_prefix_source}",
                    log,
                )
                for diagnostic in agent.compatibility_diagnostics:
                    report_status(f"agent diagnostic: {diagnostic}", log)
                report_status(
                    f"detected agents: {format_detected_agents(agent.detected)}",
                    log,
                )
                report_status("agent command started", log)

                def record_worker_pid(worker_pid: int) -> None:
                    nonlocal active_state
                    # Captured immediately after Popen, while the worker is
                    # still the process this supervisor started: after it
                    # execs its own session leader and reparents, the PID
                    # alone can no longer prove which process is ours.
                    identity = read_process_node(worker_pid)
                    active_state = active_state.with_worker_pid(
                        worker_pid,
                        process_group_id=(
                            identity.process_group_id if identity else None
                        ),
                        session_id=identity.session_id if identity else None,
                        process_birth_id=(
                            identity.process_birth_id if identity else ""
                        ),
                    )
                    self.run_store.append_lifecycle_event(
                        RunLifecycleEvent.worker_process_started(
                            run_id=run_id,
                            task_id=task.task_id,
                            worker_pid=worker_pid,
                            supervisor_pid=active_state.supervisor_pid or os.getpid(),
                            process_group_id=(
                                identity.process_group_id if identity else None
                            ),
                            session_id=identity.session_id if identity else None,
                            process_birth_id=(
                                identity.process_birth_id if identity else ""
                            ),
                            host=active_state.host,
                        )
                    )
                    update_active_task_lock()
                    report_status(
                        "worker process started "
                        f"task={task.task_id} run_id={run_id} pid={worker_pid}",
                        log,
                    )

                def worker_filed_terminal_report() -> bool:
                    # A filed report means the worker reached its reporting
                    # step and intends to exit; if it then hangs (e.g. held by
                    # orphaned background children) the watchdog reaps it so the
                    # slot and task lock are released instead of wedging for
                    # hours.
                    return (
                        self.run_store.latest_worker_report(run_id, task.task_id)
                        is not None
                    )

                stage_machine.transition(
                    RunStage.IMPLEMENTING,
                    reason="worker_process_launch",
                )
                stream_result = run_streaming_command(
                    command,
                    self.config.repo,
                    log,
                    env=command_env,
                    forward_stderr=agent.forward_stderr,
                    on_start=record_worker_pid,
                    on_observation=record_agent_observation,
                    reap_check=worker_filed_terminal_report,
                    timeout_seconds=self.config.supervision.worker_timeout_seconds,
                    provider=(
                        command_context.model_provider
                        or {"codex": "openai", "claude": "anthropic"}.get(
                            agent_kind, "unknown"
                        )
                    ),
                )
                exit_code = stream_result.exit_code
                worker_timed_out = stream_result.timed_out
                if injected_session_id is not None:
                    session_id = injected_session_id
                    session_id_source = SESSION_OBSERVED_SOURCE
                    if claude_home is not None:
                        resolved_transcript = resolve_claude_transcript(
                            injected_session_id, claude_home
                        )
                        if resolved_transcript is not None:
                            transcript_path = str(resolved_transcript)
                else:
                    session_id = stream_result.session_id or run_id
                    session_id_source = (
                        stream_result.session_id_source or "fallback:run_id"
                    )
                final_runtime_context = command_context.prefer(
                    stream_result.runtime_context
                )
                provider_usage = stream_result.usage
                if codex_home is not None and session_id != run_id:
                    rollout = resolve_codex_rollout(session_id, codex_home)
                    if rollout is not None:
                        rollout_usage = parse_codex_rollout_usage(rollout)
                        if rollout_usage.available:
                            provider_usage = dataclasses.replace(
                                rollout_usage,
                                values={
                                    **provider_usage.values,
                                    **rollout_usage.values,
                                },
                                raw={**provider_usage.raw, **rollout_usage.raw},
                            )
                        if rollout_usage.quota_snapshots:
                            provider_usage = dataclasses.replace(
                                provider_usage,
                                quota_snapshots=rollout_usage.quota_snapshots,
                                quota_unavailable_reason="",
                            )
                if (
                    not provider_usage.available
                    and agent_kind in {"auto", "claude"}
                    and transcript_path
                ):
                    provider_usage = parse_claude_transcript_usage(
                        Path(transcript_path),
                        start_offset=transcript_start_offset,
                    )
                final_context_payload = build_run_context_payload(
                    task_id=task.task_id,
                    run_id=run_id,
                    started_at=active_state.started_at,
                    session_id=session_id,
                    session_id_source=session_id_source,
                    agent_kind=agent_kind,
                    agent_kind_source=agent_kind_source,
                    agent_prompt_dialect=agent_prompt_dialect,
                    agent_prompt_dialect_source=agent_prompt_dialect_source,
                    agent_skill_ref_prefix=agent_skill_ref_prefix,
                    agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
                    runtime_context=final_runtime_context,
                    agent_profile=agent_profile,
                    transcript_path=transcript_path,
                )
                with observation_lock:
                    if not session_observed_recorded:
                        self.run_store.append_lifecycle_event(
                            RunLifecycleEvent.run_state_transition(
                                run_id=run_id,
                                task_id=task.task_id,
                                from_state="started",
                                to_state="session_observed",
                                reason=session_id_source,
                                payload=final_context_payload,
                            )
                        )
                        session_observed_recorded = True
                    final_trailer_context = final_context_payload["trailer_context"]
                    final_trailer_context_sources = final_context_payload[
                        "trailer_context_sources"
                    ]
                    active_state = active_state.with_trailer_context(
                        session_id=session_id,
                        session_id_source=session_id_source,
                        model_provider=final_runtime_context.model_provider,
                        model_provider_source=(
                            final_runtime_context.model_provider_source
                        ),
                        model_id=final_runtime_context.model_id,
                        model_id_source=final_runtime_context.model_id_source,
                        reasoning_effort=final_runtime_context.reasoning_effort,
                        reasoning_effort_source=(
                            final_runtime_context.reasoning_effort_source
                        ),
                        trailer_context=(
                            final_trailer_context
                            if isinstance(final_trailer_context, dict)
                            else {}
                        ),
                        trailer_context_sources=(
                            final_trailer_context_sources
                            if isinstance(final_trailer_context_sources, dict)
                            else {}
                        ),
                    )
                    update_active_task_lock()
                report_status(f"agent command exit_code={exit_code}", log)
                report_status(f"session_id={session_id}", log)
                report_status(f"session_id_source={session_id_source}", log)
                if transcript_path:
                    report_status(f"transcript={transcript_path}", log)
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
            try:
                output_tail = _read_log_tail(
                    log_path, LOG_TAIL_LINES_FOR_TRANSIENT_CHECK
                )
            except OSError:
                output_tail = ""
            classification = self.classify(
                task.task_id,
                exit_code,
                start_main,
                end_main,
                message,
                worker_report,
                output_tail,
                timed_out=worker_timed_out,
            )
            usage_phase, usage_work_kind = worker_usage_provenance(worker_report)
            if classification.status == "limit_wall" and classification.detail:
                # Persist the advertised reset phrase so the supervisor can size
                # its dispatch backoff from the recorded result alone.
                message = classification.detail
            failure_by_classification = {
                "limit_wall": StageFailure.LIMIT_WALL,
                "timed_out": StageFailure.TIMED_OUT,
                "failed": StageFailure.STAGE_FAILED,
                "blocked": StageFailure.BLOCKED,
            }
            stage_failure = failure_by_classification.get(classification.status)
            if stage_failure is None:
                stage_machine.transition(
                    RunStage.CLASSIFICATION,
                    reason=classification.source,
                )
            else:
                stage_machine.fail(
                    stage_failure,
                    reason=classification.source,
                )
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.run_state_transition(
                    run_id=run_id,
                    task_id=task.task_id,
                    from_state="session_observed",
                    to_state="classified",
                    reason=classification.source,
                    payload={
                        "classification": classification.status,
                        "started_at": active_state.started_at,
                    },
                )
            )
            stage_machine.transition(
                RunStage.FINALIZATION,
                reason="run_result_recording",
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
                started_at=active_state.started_at,
                session_id=session_id,
                session_id_source=session_id_source,
                transcript_path=transcript_path,
                agent_command_source=agent.command_source,
                agent_selection_command_source=agent.selection_command_source,
                agent_default_policy_source=AGENT_DEFAULT_POLICY_SOURCE,
                agent_default_policy=AGENT_DEFAULT_POLICY,
                agent_kind=agent_kind,
                agent_prompt_dialect=agent.prompt_dialect or "",
                agent_prompt_dialect_source=agent.prompt_dialect_source,
                agent_skill_ref_prefix=agent.skill_ref_prefix or "",
                agent_skill_ref_prefix_source=agent.skill_ref_prefix_source,
                model_provider=final_runtime_context.model_provider,
                model_provider_source=final_runtime_context.model_provider_source,
                model_id=final_runtime_context.model_id,
                model_id_source=final_runtime_context.model_id_source,
                reasoning_effort=final_runtime_context.reasoning_effort,
                reasoning_effort_source=final_runtime_context.reasoning_effort_source,
                trailer_context=(
                    final_context_payload["trailer_context"]
                    if isinstance(final_context_payload["trailer_context"], dict)
                    else {}
                ),
                trailer_context_sources=(
                    final_context_payload["trailer_context_sources"]
                    if isinstance(
                        final_context_payload["trailer_context_sources"],
                        dict,
                    )
                    else {}
                ),
                classification_source=classification.source,
                worker_report=(
                    worker_report.to_json() if worker_report is not None else None
                ),
                restart_count=restart_count,
                max_restarts=max_restarts,
                stats=provider_usage.to_stats(
                    phase=usage_phase,
                    wall_time_seconds=(
                        datetime.now(UTC)
                        - datetime.fromisoformat(
                            active_state.started_at.replace("Z", "+00:00")
                        )
                    ).total_seconds(),
                    candidate_fingerprint=end_main or start_main,
                    continuation=continuation,
                    flexible_provider=provider_selection_is_flexible(agent, task),
                    changed_lines=git_changed_lines(
                        self.config.repo, start_main, end_main
                    ),
                    work_kind=usage_work_kind,
                ),
            )
            self.record_result(result)
            # Only a durable local RunResult may be published externally. Until
            # the append succeeds there is nothing for provenance to agree with,
            # so an append that raises leaves the run settling as unknown.
            settled_classification = classification.status
            settled_outcome = settled_run_outcome(classification.status)
            if self.recovery_budget_exhausted_by(result, recovery):
                # This is the last permitted recovery attempt and it still could
                # not settle the task, so the supervisor will treat this run as
                # terminally failed. The lock is released below, before it
                # reaches the exhaustion branch, so the verdict is recorded and
                # published here - durably first, so external provenance never
                # claims a failure vibe-loop has not written down.
                self._exhausted_recovery_results[result.run_id] = (
                    self.record_recovery_budget_exhausted(result, recovery.attempt)
                )
                settled_outcome = "failed"
                settled_classification = "failed"
            report_status(
                f"recorded {classification.status} result for {task.task_id}: "
                f"{log_path}"
            )
            return result
        except KeyboardInterrupt:
            stage_machine.fail(
                StageFailure.CANCELLED,
                reason="worker_interrupted",
            )
            if stage_machine.stage is RunStage.CLASSIFICATION:
                stage_machine.transition(
                    RunStage.FINALIZATION,
                    reason="interrupted_finalization",
                )
            raise
        except Exception:
            stage_machine.fail(
                StageFailure.STAGE_FAILED,
                reason="run_task_exception",
            )
            if stage_machine.stage is RunStage.CLASSIFICATION:
                stage_machine.transition(
                    RunStage.FINALIZATION,
                    reason="exception_finalization",
                )
            raise
        finally:
            try:
                settle_outcome_and_release()
            except SettledOutcomeNotPersisted as exc:
                # No release happened, so no lock_released event may be claimed:
                # the lock is still held under this run id and fencing token and
                # is recoverable. The RunResult is already durable, so the
                # failure is surfaced rather than silently reconciled.
                report_status(str(exc))
                self.run_store.append_lifecycle_event(
                    RunLifecycleEvent.lock_event(
                        LOCK_FINALIZATION_FAILED_RECORD_TYPE,
                        run_id=run_id,
                        task_id=task.task_id,
                        lock_kind="task",
                        lock_path=task_lock.path,
                        payload={
                            "started_at": active_state.started_at,
                            "outcome": settled_outcome,
                            "classification": settled_classification,
                            "reason": str(exc.cause),
                            "released": False,
                        },
                    )
                )
                raise
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_RELEASED_RECORD_TYPE,
                    run_id=run_id,
                    task_id=task.task_id,
                    lock_kind="task",
                    lock_path=task_lock.path,
                    payload={
                        "started_at": active_state.started_at,
                        "outcome": settled_outcome,
                        "classification": settled_classification,
                    },
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

    def activate_task_before_launch(
        self,
        task: Task,
        run_id: str,
        command_env: dict[str, str],
        *,
        continuation: bool,
    ) -> Task | None:
        activate = getattr(self.source, "activate", None)
        if activate is None:
            return None
        runtime_context = {
            key: command_env[key]
            for key in (
                "VIBE_LOOP_RUN_ID",
                "VIBE_LOOP_TASK_ID",
                "VIBE_LOOP_REPO",
                "VIBE_LOOP_LOG",
                "VIBE_LOOP_FENCING_TOKEN",
            )
            if key in command_env
        }
        try:
            confirmed = activate(
                task.task_id,
                run_id,
                continuation=continuation,
                runtime_context=runtime_context,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            mode = "continuation confirmation" if continuation else "activation"
            raise TaskActivationError(
                f"task-source {mode} failed for {task.task_id}: {exc}; "
                "worker was not launched"
            ) from exc
        if confirmed is None:
            return None
        if confirmed.task_id != task.task_id:
            raise TaskActivationError(
                "task_source.activate returned task "
                f"{confirmed.task_id!r}, expected {task.task_id!r}; "
                "worker was not launched"
            )
        if not confirmed.status.strip():
            raise TaskActivationError(
                "task_source.activate returned an empty status for "
                f"{task.task_id}; worker was not launched"
            )
        if confirmed.done or confirmed.status.casefold() in BLOCKED_FAMILY_STATUSES:
            raise TaskActivationError(
                "task_source.activate returned terminal or blocked status "
                f"{confirmed.status!r} for {task.task_id}; worker was not launched"
            )
        runnable_statuses = self.source_resolution.task_source.runnable_statuses
        if confirmed.status in runnable_statuses:
            raise TaskActivationError(
                "task_source.activate left task "
                f"{task.task_id} runnable with status {confirmed.status!r}; "
                "worker was not launched"
            )
        return confirmed

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
        self,
        ask_agent: bool = False,
        exclude: set[str] | None = None,
        restart_counts: dict[str, int] | None = None,
    ) -> RunResult | None:
        require_project_binding(self.config)
        candidates = self.list_candidates(exclude=exclude)
        if not candidates:
            return None
        self.ensure_spec_execution_gate()
        self.require_worker_launch_config()
        task = self.select_from_candidates(candidates, ask_agent=ask_agent)
        try:
            restart_count = (restart_counts or {}).get(task.task_id, 0)
            return self.run_task_with_supervision(
                task,
                restart_count=restart_count,
            )
        except LockBusy:
            report_status(f"task locked during acquire, retrying: {task.task_id}")
            excluded = set(exclude or set())
            excluded.add(task.task_id)
            return self.run_next(
                ask_agent=ask_agent,
                exclude=excluded,
                restart_counts=restart_counts,
            )

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
        require_project_binding(self.config)
        supervisor_pid = os.getpid()
        supervisor_identity = read_process_node(supervisor_pid)
        self.run_store.append_record(
            {
                "schema_version": LIFECYCLE_EVENT_SCHEMA_VERSION,
                "record_type": RUN_SUPERVISOR_STARTED_RECORD_TYPE,
                "occurred_at": utc_now_iso(),
                "repo": str(self.config.repo),
                "pid": supervisor_pid,
                "process_group_id": (
                    supervisor_identity.process_group_id
                    if supervisor_identity
                    else None
                ),
                "session_id": (
                    supervisor_identity.session_id if supervisor_identity else None
                ),
                "process_birth_id": (
                    supervisor_identity.process_birth_id if supervisor_identity else ""
                ),
                "jobs": jobs,
            }
        )
        try:
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
        finally:
            self.run_store.append_record(
                {
                    "schema_version": LIFECYCLE_EVENT_SCHEMA_VERSION,
                    "record_type": RUN_SUPERVISOR_EXITED_RECORD_TYPE,
                    "occurred_at": utc_now_iso(),
                    "repo": str(self.config.repo),
                    "pid": os.getpid(),
                    "jobs": jobs,
                }
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
        recovery_attempts: dict[str, int] = {}
        completed_count = 0
        while max_slices <= 0 or len(results) < max_slices:
            result = self.run_next(
                ask_agent=ask_agent,
                exclude=skipped | yielded,
                restart_counts=transient_retries,
            )
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
            if result.classification == "limit_wall":
                # The task stays runnable and keeps its restart budget; stop
                # dispatching so the supervisor can back off until the reset
                # instead of tight-looping into the same wall.
                self._report_limit_wall_pause(result)
                break
            if result.classification == "timed_out":
                # A hung worker was killed. Return the task to runnable and skip
                # it for the rest of this invocation (so the batch keeps making
                # progress on other work instead of stalling), then let a later
                # cycle re-dispatch it.
                self._report_worker_timeout(result)
                skipped.add(result.task_id)
                continue
            if is_transient_worker_failure(result):
                count = transient_retries.get(result.task_id, 0) + 1
                transient_retries[result.task_id] = count
                if count <= self.config.supervision.max_restarts:
                    cooldown = transient_failure_cooldown(
                        result, self.config.supervision.cooldown_seconds
                    )
                    self.record_task_restart(
                        result, count, exhausted=False, cooldown_seconds=cooldown
                    )
                    report_status(
                        f"transient failure for {result.task_id} "
                        f"(restart {count}/{self.config.supervision.max_restarts}), "
                        f"cooling down {cooldown:.0f}s"
                    )
                    time.sleep(cooldown)
                    continue
                result = self.record_restart_budget_exhausted(result, count)
                results[-1] = result
                report_status(
                    f"transient retries exhausted for {result.task_id}, skipping"
                )
            if result.classification == "unknown":
                result = self.drive_unknown_recovery(
                    result,
                    attempts=recovery_attempts,
                    results=results,
                )
                if result.classification == "completed":
                    transient_retries.pop(result.task_id, None)
                    recovery_attempts.pop(result.task_id, None)
                    yielded.add(result.task_id)
                    completed_count += 1
                    if max_tasks > 0 and completed_count >= max_tasks:
                        break
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
        max_tasks: int = 0,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        transient_retries: dict[str, int] = {}
        recovery_attempts: dict[str, int] = {}
        retry_ready_at: dict[str, float] = {}
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
                    discard_ready_retries(retry_ready_at)
                    now = time.monotonic()
                    cooling_down = {
                        task_id
                        for task_id, ready_at in retry_ready_at.items()
                        if ready_at > now
                    }
                    candidates = self.list_candidates(
                        exclude=skipped | set(scheduled) | cooling_down
                    )
                    candidates = filter_scheduled_conflicts(
                        candidates,
                        list(scheduled.values()),
                    )
                    if not candidates:
                        break
                    self.ensure_spec_execution_gate()
                    if not command_validated:
                        self.require_worker_launch_config()
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
                        in_flight[
                            executor.submit(
                                self.run_task_with_supervision,
                                task,
                                restart_count=transient_retries.get(task.task_id, 0),
                            )
                        ] = task.task_id
                    if ask_agent and len(candidates) > 1 and len(tasks) < open_slots:
                        break

                if discard_ready_retries(retry_ready_at):
                    continue

                if not in_flight:
                    if (
                        retry_ready_at
                        and not stop_after_running
                        and (
                            max_slices <= 0
                            or len(results) + len(in_flight) < max_slices
                        )
                        and (
                            max_tasks <= 0
                            or completed_count + len(in_flight) < max_tasks
                        )
                    ):
                        now = time.monotonic()
                        cooling_down = {
                            task_id
                            for task_id, ready_at in retry_ready_at.items()
                            if ready_at > now
                        }
                        candidates = self.list_candidates(
                            exclude=skipped | set(scheduled) | cooling_down
                        )
                        candidates = filter_scheduled_conflicts(
                            candidates,
                            list(scheduled.values()),
                        )
                        if candidates or discard_ready_retries(retry_ready_at):
                            continue
                        retry_delay = next_retry_delay(retry_ready_at)
                        if retry_delay is not None:
                            time.sleep(retry_delay)
                            continue
                    break

                wait_timeout = None
                retry_delay = next_retry_delay(retry_ready_at)
                if (
                    retry_delay is not None
                    and not stop_after_running
                    and len(in_flight) < jobs
                    and (max_slices <= 0 or len(results) + len(in_flight) < max_slices)
                    and (max_tasks <= 0 or completed_count + len(in_flight) < max_tasks)
                ):
                    wait_timeout = retry_delay
                completed, _pending = wait(
                    in_flight,
                    return_when=FIRST_COMPLETED,
                    timeout=wait_timeout,
                )
                if not completed:
                    continue
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
                        retry_ready_at.pop(result.task_id, None)
                        completed_count += 1
                        if max_tasks > 0 and completed_count >= max_tasks:
                            stop_after_running = True
                        continue
                    if result.classification == "limit_wall":
                        # Keep the task runnable and its restart budget intact;
                        # stop scheduling new work and drain in-flight workers so
                        # the supervisor can back off before the next cycle.
                        self._report_limit_wall_pause(result)
                        stop_after_running = True
                        continue
                    if result.classification == "timed_out":
                        # A hung worker was killed. Return the task to runnable
                        # and skip it for the rest of this batch, but keep
                        # scheduling other work: one hung worker must not stall
                        # the remaining slots or cycles.
                        self._report_worker_timeout(result)
                        skipped.add(result.task_id)
                        retry_ready_at.pop(result.task_id, None)
                        continue
                    if is_transient_worker_failure(result):
                        count = transient_retries.get(result.task_id, 0) + 1
                        transient_retries[result.task_id] = count
                        if count <= self.config.supervision.max_restarts:
                            cooldown = transient_failure_cooldown(
                                result, self.config.supervision.cooldown_seconds
                            )
                            self.record_task_restart(
                                result,
                                count,
                                exhausted=False,
                                cooldown_seconds=cooldown,
                            )
                            retry_ready_at[result.task_id] = time.monotonic() + cooldown
                            report_status(
                                f"transient failure for {result.task_id} "
                                f"(restart {count}/"
                                f"{self.config.supervision.max_restarts}), "
                                f"will re-enqueue after {cooldown:.0f}s"
                            )
                            continue
                        result = self.record_restart_budget_exhausted(result, count)
                        results[-1] = result
                        retry_ready_at.pop(result.task_id, None)
                        report_status(
                            f"transient retries exhausted for {result.task_id}, "
                            "skipping"
                        )
                    if result.classification == "unknown":
                        # Recovery runs synchronously in the supervisor thread,
                        # by design: only this drain loop mutates results/
                        # counters, so a continuation worker cannot race the
                        # other in-flight workers (which never touch this
                        # state). It does briefly serialize new-work scheduling
                        # behind the recovery worker, which is acceptable for a
                        # bounded recovery.
                        result = self.drive_unknown_recovery(
                            result,
                            attempts=recovery_attempts,
                            results=results,
                        )
                        if result.classification == "completed":
                            transient_retries.pop(result.task_id, None)
                            retry_ready_at.pop(result.task_id, None)
                            recovery_attempts.pop(result.task_id, None)
                            completed_count += 1
                            if max_tasks > 0 and completed_count >= max_tasks:
                                stop_after_running = True
                            continue
                    skipped.add(result.task_id)
                    if result.classification in {"failed", "unknown"}:
                        stop_after_running = not continue_on_failure
        return results

    def require_worker_launch_config(self) -> None:
        self.config.agent.require_command()
        self.config.agent.require_skill_ref_prefix()

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

    def record_task_restart(
        self,
        result: RunResult,
        restart_count: int,
        *,
        exhausted: bool,
        attempted_restart_count: int | None = None,
        reason: str | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        if reason is None:
            reason = (
                "restart_budget_exhausted" if exhausted else "transient_worker_failure"
            )
        if cooldown_seconds is None:
            cooldown_seconds = self.config.supervision.cooldown_seconds
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_restart(
                run_id=result.run_id,
                task_id=result.task_id,
                restart_count=restart_count,
                max_restarts=self.config.supervision.max_restarts,
                cooldown_seconds=cooldown_seconds,
                reason=reason,
                exhausted=exhausted,
                attempted_restart_count=attempted_restart_count,
                started_at=result.started_at,
            )
        )

    def record_restart_budget_exhausted(
        self,
        result: RunResult,
        attempted_restart_count: int,
    ) -> RunResult:
        max_restarts = self.config.supervision.max_restarts
        restarts_used = min(max(0, attempted_restart_count - 1), max_restarts)
        self.record_task_restart(
            result,
            restarts_used,
            exhausted=True,
            attempted_restart_count=attempted_restart_count,
        )
        exhausted = dataclasses.replace(
            result,
            classification="failed",
            classification_source="restart_budget_exhausted",
            message=(
                "restart budget exhausted after "
                f"{max_restarts} restart(s) for {result.task_id}"
            ),
            restart_count=restarts_used,
            max_restarts=max_restarts,
        )
        self.record_result(exhausted)
        return exhausted

    def recovery_budget_exhausted_by(
        self,
        result: RunResult,
        recovery: RecoveryContext | None,
    ) -> bool:
        """Report whether this run is the one that exhausts the budget.

        The condition mirrors ``drive_unknown_recovery`` exactly: only a run
        that classifies ``unknown`` re-enters recovery, so any other
        unknown-settling classification - ``timed_out``, ``limit_wall`` - is
        terminal as itself and must not be published as failed.
        """

        if recovery is None or result.classification != "unknown":
            return False
        if not self.config.supervision.recover_unknown_runs:
            return False
        max_attempts = self.config.supervision.max_restarts
        return max_attempts > 0 and recovery.attempt >= max_attempts

    def record_recovery_budget_exhausted(
        self,
        result: RunResult,
        attempts_used: int,
    ) -> RunResult:
        max_attempts = self.config.supervision.max_restarts
        self.record_task_restart(
            result,
            attempts_used,
            exhausted=True,
            attempted_restart_count=attempts_used + 1,
            reason="recovery_budget_exhausted",
        )
        exhausted = dataclasses.replace(
            result,
            classification="failed",
            classification_source="recovery_budget_exhausted",
            message=(
                "unknown-run recovery budget exhausted after "
                f"{attempts_used} attempt(s) for {result.task_id}"
            ),
            restart_count=attempts_used,
            max_restarts=max_attempts,
        )
        self.record_result(exhausted)
        return exhausted

    def drive_unknown_recovery(
        self,
        result: RunResult,
        *,
        attempts: dict[str, int],
        results: list[RunResult],
    ) -> RunResult:
        """Deterministically recover a run that classified `unknown`.

        Launches bounded read-write continuation workers against the prior
        claimed workspace until the run reaches a clear terminal status or the
        per-task recovery budget is exhausted. Each recovery RunResult is
        appended to ``results``; the final (possibly terminal) result is
        returned.
        """
        if not self.config.supervision.recover_unknown_runs:
            return result
        max_attempts = self.config.supervision.max_restarts
        if max_attempts <= 0:
            return result
        current = result
        while current.classification == "unknown":
            attempt = attempts.get(current.task_id, 0) + 1
            if attempt > max_attempts:
                # The exhausting run recorded this verdict before releasing its
                # lock, so external provenance and the run store agree; reuse it
                # instead of writing a second terminal result for the same run.
                terminal = self._exhausted_recovery_results.pop(current.run_id, None)
                if terminal is None:
                    terminal = self.record_recovery_budget_exhausted(
                        current,
                        attempts.get(current.task_id, 0),
                    )
                results.append(terminal)
                report_status(
                    "unknown-run recovery budget exhausted for "
                    f"{current.task_id} after {max_attempts} attempt(s)"
                )
                return terminal
            attempts[current.task_id] = attempt
            recovered = self.recover_unknown_run(
                current,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            if recovered is None:
                return current
            results.append(recovered)
            current = recovered
        return current

    def recover_unknown_run(
        self,
        prior_result: RunResult,
        *,
        attempt: int,
        max_attempts: int,
    ) -> RunResult | None:
        try:
            task = self.source.probe(prior_result.task_id)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            # The classification probe falls through to "unknown" on a probe
            # failure, which routes here; a command-backed probe that keeps
            # failing (nonzero exit, spawn error, timeout, or invalid JSON) must
            # skip recovery rather than propagate, mirroring the task-absent
            # skip below.
            report_status(
                "unknown-run recovery skipped: task-source probe failed for "
                f"{prior_result.task_id}: {exc}"
            )
            return None
        if task is None:
            report_status(
                "unknown-run recovery skipped: task "
                f"{prior_result.task_id} no longer present in task source"
            )
            return None
        claim_record = self.run_store.latest_workspace_claim_record(
            prior_result.task_id,
            prior_result.run_id,
        )
        claim = (
            WorkspaceClaim.from_json(claim_record) if claim_record is not None else None
        )
        recovery = RecoveryContext(
            task_id=prior_result.task_id,
            prior_run_id=prior_result.run_id,
            prior_classification=prior_result.classification,
            branch=claim.branch if claim is not None else "",
            worktree=str(claim.worktree) if claim is not None else "",
            head_commit=claim.head_commit if claim is not None else "",
            transcript_path=prior_result.transcript_path,
            wrapper_log=str(prior_result.log_path),
            attempt=attempt,
            max_attempts=max_attempts,
            workspace_claimed=claim is not None,
            prior_session_id=resumable_prior_session_id(prior_result),
        )
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_recovery(
                run_id=prior_result.run_id,
                task_id=prior_result.task_id,
                phase="launched",
                prior_run_id=prior_result.run_id,
                attempt=attempt,
                max_attempts=max_attempts,
                branch=recovery.branch,
                worktree=recovery.worktree,
                transcript_path=recovery.transcript_path,
                wrapper_log=recovery.wrapper_log,
            )
        )
        report_status(
            f"launching unknown-run recovery for {prior_result.task_id} "
            f"(attempt {attempt}/{max_attempts}, prior run {prior_result.run_id})"
        )
        try:
            result = self.run_task(task, recovery=recovery)
        except LockBusy:
            report_status(
                "unknown-run recovery deferred: task locked during acquire: "
                f"{prior_result.task_id}"
            )
            return None
        # Reuse the task_restart counter/record so the recovery attempt is
        # visible in runs/workers output and an unknown->recover->unknown cycle
        # cannot loop past the configured budget.
        self.record_task_restart(
            result,
            attempt,
            exhausted=False,
            reason="unknown_run_recovery",
        )
        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_recovery(
                run_id=result.run_id,
                task_id=prior_result.task_id,
                phase="outcome",
                prior_run_id=prior_result.run_id,
                attempt=attempt,
                max_attempts=max_attempts,
                outcome=result.classification,
            )
        )
        report_status(
            f"unknown-run recovery for {prior_result.task_id} "
            f"(attempt {attempt}/{max_attempts}) classified {result.classification}"
        )
        return result

    def classify(
        self,
        task_id: str,
        exit_code: int,
        start_main: str,
        end_main: str,
        message: str,
        worker_report: WorkerReport | None = None,
        output_tail: str = "",
        *,
        timed_out: bool = False,
    ) -> ClassificationResult:
        # A wall-clock timeout force-killed the worker mid-run: its output is
        # inconclusive and any partial report is stale, so the run is neither a
        # completion nor a budget-consuming failure. Classify it distinctly so
        # dispatch returns the task to runnable without marking it done.
        if timed_out:
            return ClassificationResult("timed_out", "worker_timeout")
        if worker_report is not None:
            return ClassificationResult(worker_report.status, "worker_report")
        # A provider limit wall exits nonzero, so it must be caught before the
        # exit_code branch downgrades it to "failed" and burns restart budget.
        # A worker that filed a terminal report above already made progress, so
        # only wall-detect a report-less death. The exit_code != 0 gate keeps a
        # successful run whose output merely quotes a limit phrase (e.g. a worker
        # implementing limit handling) on the normal completion path.
        if (
            self.config.supervision.limit_wall_detection
            and exit_code != 0
            and output_tail
        ):
            signal = detect_limit_wall(
                output_tail,
                self.config.supervision.limit_wall_patterns or None,
            )
            if signal is not None:
                return ClassificationResult(
                    "limit_wall", "limit_wall", detail=signal.reset_text
                )
        if exit_code != 0 or message:
            return ClassificationResult("failed", "exit_code_or_completion_check")
        try:
            task = self.source.probe(task_id)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            # A command-backed probe can fail to shell out (OSError), exit
            # nonzero (CalledProcessError), or hang past its timeout
            # (TimeoutExpired), while malformed JSON or task data raises
            # ValueError. None of these confirm the run's outcome, so fall
            # through to the same "unknown" fallback an indeterminate probe
            # already yields: the run is reconciled by unknown-run recovery
            # instead of crashing the dispatch loop (run_next only catches
            # LockBusy).
            report_status(
                f"task-source probe failed while classifying {task_id}: {exc}; "
                "treating outcome as unknown"
            )
            return ClassificationResult("unknown", "task_probe_error")
        # Status comparisons must be case-insensitive: only done-statuses get
        # canonicalized at parse time (normalize_status), and command task
        # sources pass the adapter's wire status through verbatim (e.g.
        # lowercase "done"). A case miss here downgrades a finished run to
        # "unknown", which spawns a needless recovery worker.
        if task and task.done:
            return ClassificationResult("completed", "task_probe")
        if task and task.status.casefold() in BLOCKED_FAMILY_STATUSES:
            return ClassificationResult("blocked", "task_probe")
        if start_main != end_main and task is None:
            return ClassificationResult("completed", "main_change")
        return ClassificationResult("unknown", "fallback")

    def record_result(self, result: RunResult) -> None:
        with self._record_lock:
            self.run_store.append_result(result)

    def _report_limit_wall_pause(self, result: RunResult) -> None:
        detail = (result.message or "").strip()
        suffix = f" ({detail})" if detail else ""
        report_status(
            f"limit wall hit for {result.task_id}{suffix}: stopping dispatch "
            "without consuming restart budget; supervisor backs off before "
            "the next cycle"
        )
        # Pre-launch activation moved the task out of the runnable set, and the
        # worker died before any terminal transition. The vibe-loop task lock is
        # already released (run_task's finally), so the task now sits active in
        # the backend with no live lock and would never be re-dispatched. An
        # operator-configured reset hook returns it to its runnable state.
        self._reset_task_source_status(result.task_id)

    def _report_worker_timeout(self, result: RunResult) -> None:
        report_status(
            f"worker for {result.task_id} exceeded the configured wall-clock "
            f"timeout and was force-killed (run {result.run_id}); returning the "
            "task to its runnable state without consuming restart budget so the "
            "batch and other workers proceed"
        )
        # Pre-launch activation moved the task out of the runnable set, and the
        # worker was killed before any terminal transition. Its vibe-loop lock
        # is already released (run_task's finally), so the task now sits active
        # in the backend with no live lock and would never be re-dispatched. The
        # reset hook returns it to its runnable state, mirroring the limit-wall
        # recovery path.
        self._reset_task_source_status(result.task_id)

    def _reset_task_source_status(self, task_id: str) -> None:
        try:
            reset = self.source.reset(task_id)
        except (subprocess.SubprocessError, OSError) as exc:
            report_status(
                f"task-source reset hook failed for {task_id}: {exc}; "
                "leaving backend status unchanged"
            )
            return
        if reset:
            report_status(
                f"task-source reset hook returned {task_id} to its runnable state"
            )

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


@dataclasses.dataclass(frozen=True)
class RecoveryContext:
    """Bounded context handed to a continuation worker recovering an
    `unknown` run. Identifies the prior run, its claimed workspace, and the
    artifacts the continuation worker should inspect before finishing the work
    or emitting a proper terminal status."""

    task_id: str
    prior_run_id: str
    prior_classification: str
    branch: str
    worktree: str
    head_commit: str
    transcript_path: str
    wrapper_log: str
    attempt: int
    max_attempts: int
    workspace_claimed: bool
    # Captured claude session id of the prior run when it is a real, resumable
    # session (empty for a fallback id or a non-claude agent). When set and
    # resume is enabled, the continuation runs `claude -p --resume <id>` to keep
    # the prior turn's full context instead of a from-scratch fresh worker.
    prior_session_id: str = ""


def build_recovery_prompt_section(recovery: RecoveryContext) -> str:
    if recovery.workspace_claimed:
        workspace_lines = (
            f"- Prior run's recorded branch: `{recovery.branch}`\n"
            f"- Prior run's recorded worktree: `{recovery.worktree}`\n"
        )
        if recovery.head_commit:
            workspace_lines += (
                f"- Prior run's recorded HEAD at claim time: `{recovery.head_commit}`\n"
            )
    else:
        workspace_lines = (
            "- No `workspace_claim` record was found for the prior run; the "
            "prior session may have exited before claiming a branch/worktree. "
            "Inspect the repository and prior run log to determine what, if "
            "anything, was committed.\n"
        )
    transcript_line = (
        f"- Prior agent transcript: `{recovery.transcript_path}`\n"
        if recovery.transcript_path
        else "- Prior agent transcript: not captured for the prior run.\n"
    )
    return (
        "## Unknown-Run Recovery\n\n"
        f"You are a continuation worker for task `{recovery.task_id}`. The "
        f"previous run (`{recovery.prior_run_id}`) ended "
        f"`{recovery.prior_classification}` — it neither merged its work nor "
        "filed a clear terminal worker report. This is recovery attempt "
        f"{recovery.attempt} of {recovery.max_attempts}.\n\n"
        "### Prior run context\n\n"
        f"- Prior run id: `{recovery.prior_run_id}`\n"
        f"{workspace_lines}"
        f"{transcript_line}"
        f"- Prior wrapper log: `{recovery.wrapper_log}`\n\n"
        "### Current-run workspace claim\n\n"
        "Any workspace claim shown above belonged to the prior run's released "
        "lock generation. It is stale evidence only and does not attach that "
        "workspace to the CURRENT active task lock. Verify the real branch and "
        "absolute worktree path, then make the current-run claim before any new "
        "mutation, gate, build, test, review, or integration attempt:\n\n"
        "```bash\n"
        f"{CURRENT_RUN_WORKSPACE_CLAIM_COMMAND}\n"
        "```\n\n"
        "Run this command from inside the verified worktree; its quoted Git "
        "lookups derive the exact branch and worktree arguments. Do not claim a "
        "guessed path. If the claim fails or the preserved workspace cannot be "
        "verified safely, stop before mutation and report `blocked` through the "
        "worker report protocol.\n\n"
        "### What to do\n\n"
        "1. Investigate what the previous session did and why it ended without "
        "a proper status: read the prior transcript and wrapper log, and "
        "inspect the prior run's recorded branch/worktree for "
        "committed-but-unmerged work.\n"
        "2. After the current-run claim succeeds, continue on the verified "
        "existing branch/worktree — do not delete, reset, steal, or re-create "
        "another worker's workspace; build on the committed work rather than "
        "discarding it.\n"
        "3. Finish the slice through review and integration when permitted, "
        "then emit a proper status (`completed`/`blocked`/`failed`) via the "
        "worker report protocol.\n"
        "4. If the work is blocked on an external or authorization gate, report "
        "`blocked` with the precise reason — do NOT park and exit silently, "
        "which would leave the run `unknown` again.\n"
    )


def build_resume_continuation_prompt(recovery: RecoveryContext) -> str:
    """Continuation turn for a RESUMED prior session (`claude -p --resume`).

    The resumed conversation already holds the full task/skill context and
    whatever the prior turn did, so this is a short nudge to finish rather than
    the from-scratch recovery brief. Common cause of the prior `unknown`: the
    session launched long-running proofs/checks in the background and the
    headless turn ended before they finished.
    """
    workspace_lines = ""
    if recovery.workspace_claimed:
        workspace_lines = (
            f"- Prior run's recorded branch: `{recovery.branch}`\n"
            f"- Prior run's recorded worktree: `{recovery.worktree}`\n"
        )
    return (
        "## Continue this run (resumed session)\n\n"
        f"This is the SAME session for task `{recovery.task_id}`, resumed because "
        f"the previous turn ended `{recovery.prior_classification}` — it did not "
        "merge its work or file a terminal worker report. This is recovery "
        f"attempt {recovery.attempt} of {recovery.max_attempts}.\n\n"
        "The most likely cause: you launched long-running proofs/checks in the "
        "background and this headless turn ended before they finished. Do NOT "
        "restart from scratch.\n\n"
        f"{workspace_lines}"
        "Those prior-run workspace details are stale evidence only. Verify the "
        "real branch and absolute worktree path, then attach that workspace to "
        "the CURRENT active task lock before any new mutation, gate, build, "
        "test, review, or integration attempt:\n\n"
        "```bash\n"
        f"{CURRENT_RUN_WORKSPACE_CLAIM_COMMAND}\n"
        "```\n\n"
        "Run this command from inside the verified worktree; its quoted Git "
        "lookups derive the exact branch and worktree arguments. Do not claim a "
        "guessed path. If the claim fails or the workspace cannot be verified "
        "safely, stop before mutation and report `blocked`.\n\n"
        "1. Await or collect the results of any asynchronous Agent/Task/Workflow "
        "subagent or background command you started (including its log "
        "files / exit status); re-run any remaining required gates in the "
        "FOREGROUND so this turn does not end before they complete.\n"
        "2. After the current-run workspace claim succeeds, finish the slice "
        "through review and integration when permitted, "
        "building on your existing committed work — do not delete, reset, or "
        "re-create the workspace.\n"
        "3. Emit a proper terminal status via the worker report protocol, using "
        "the CURRENT environment run id: `vibe-loop report --repo "
        '"$VIBE_LOOP_REPO" --run-id "$VIBE_LOOP_RUN_ID" --task-id '
        '"$VIBE_LOOP_TASK_ID" --status <completed|blocked|failed> --commit HEAD '
        '--message "..."`. Report `blocked` with the precise reason if gated; do '
        "NOT exit silently, which would leave the run `unknown` again.\n"
    )


def build_run_worker_prompt(
    skill_prefix: str,
    task: Task,
    config: VibeConfig,
    *,
    recovery: RecoveryContext | None,
    resuming: bool,
) -> str:
    if recovery is not None and resuming:
        return append_worker_prompt_extension(
            f"{build_resume_continuation_prompt(recovery)}\n\n"
            f"{FENCING_TOKEN_NONDISCLOSURE}",
            config,
        )
    if recovery is not None:
        prompt = (
            f"{build_worker_prompt(skill_prefix, task, config, include_repo_extension=False)}\n\n"
            f"{build_recovery_prompt_section(recovery)}"
        )
        return append_worker_prompt_extension(prompt, config)
    return build_worker_prompt(skill_prefix, task, config)


def build_worker_prompt(
    skill_prefix: str,
    task: Task,
    config: VibeConfig | None = None,
    *,
    include_repo_extension: bool = True,
) -> str:
    prompt = f"{skill_prefix}vibe-loop {task.task_id}{CLI_WORKER_ADDENDUM}"
    if task.has_traceability:
        prompt = (
            f"{prompt}\n\n"
            "### Normalized Task Traceability\n\n"
            "This task includes optional traceability metadata from the task source:\n\n"
            "```json\n"
            f"{json.dumps(worker_traceability_json(task), indent=2, sort_keys=True)}\n"
            "```\n"
        )
        if config is not None:
            prompt = (
                f"{prompt}\n\n"
                "### Spec-Aware Worker Context\n\n"
                "Bounded repo-local spec context for this task:\n\n"
                "```json\n"
                f"{json.dumps(build_spec_worker_context(config, task), indent=2, sort_keys=True)}\n"
                "```\n"
            )
    if not include_repo_extension:
        return prompt
    return append_worker_prompt_extension(prompt, config)


def append_worker_prompt_extension(
    prompt: str,
    config: VibeConfig | None,
) -> str:
    if config is None or config.worker_prompt_extra is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "## Repository Worker Prompt Extension\n\n"
        "The following repository-defined instructions from "
        "`[agent].worker_prompt_extra` in `.vibe-loop.toml` OVERRIDE the "
        "generic vibe-loop CLI coordination protocol above wherever they "
        "conflict:\n\n"
        f"{config.worker_prompt_extra}"
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
    if path_has_symlink_component(config.repo, requested_path):
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
    base = repo if repo.is_absolute() else repo.absolute()
    candidate = path if path.is_absolute() else path.absolute()
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return False
    current = base
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


def validate_worker_prompt_delivery(command_template: str, task: Task) -> None:
    if not task.has_traceability:
        return
    if command_template_uses_field(command_template, "prompt"):
        return
    raise AgentResolutionError(
        "agent.command must include {prompt} for tasks with traceability "
        "metadata; otherwise the worker prompt addendum and spec context cannot "
        "be delivered. Set agent.command to a prompt-mode template such as "
        "`codex exec {prompt}` or `claude -p {prompt}`."
    )


def validate_analysis_prompt_delivery(command_template: str) -> None:
    if command_template_uses_field(command_template, "prompt"):
        return
    raise AgentResolutionError(
        "agent.analysis_command must include {prompt}; otherwise the analysis "
        "prompt cannot be delivered to the read-only agent. Set "
        "agent.analysis_command to a prompt-mode template such as "
        "`codex exec --sandbox read-only {prompt}` or "
        "`claude -p {prompt} --disallowedTools Edit Write NotebookEdit`."
    )


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
        "agent_kind": payload["agent_kind"],
        "agent_prompt_dialect": payload["agent_prompt_dialect"],
        "agent_prompt_dialect_source": payload["agent_prompt_dialect_source"],
        "agent_skill_ref_prefix_source": payload["agent_skill_ref_prefix_source"],
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
    def _candidate(value: object) -> object | None:
        if not isinstance(value, dict):
            return None
        if any(key in value for key in ("task_id", "task_ids", "should_plan")):
            return value
        result = value.get("result")
        if isinstance(result, str):
            nested = selection_payload_from_output(result)
            if nested is not None:
                return nested
        item = value.get("item")
        if isinstance(item, Mapping):
            text = item.get("text")
            if isinstance(text, str):
                nested = selection_payload_from_output(text)
                if nested is not None:
                    return nested
        if "type" not in value:
            return value
        return None

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = _candidate(parsed)
        if candidate is not None:
            return candidate
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return None
    return _candidate(parsed)


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
        # Only live runs hold their conflict-domain leases. A lock left behind
        # by a dead/expired run must not keep serializing unrelated work
        # against its (often broad) domain set.
        if not active_run_is_live(active):
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


def parse_agent_runtime_context_from_command(command: str) -> AgentRuntimeContext:
    try:
        argv = shlex.split(command)
    except ValueError:
        return AgentRuntimeContext()
    context = AgentRuntimeContext()
    index = 0
    while index < len(argv):
        token = argv[index]
        value = None
        source = ""
        if token in {"--model", "-m"} and index + 1 < len(argv):
            value = argv[index + 1]
            source = f"command_arg:{token}"
            index += 1
        elif token.startswith("--model="):
            value = token.split("=", 1)[1]
            source = "command_arg:--model"
        if value is not None:
            cleaned = clean_agent_context_value(value)
            if not cleaned:
                index += 1
                continue
            context = context.overlay(
                AgentRuntimeContext(
                    model_id=cleaned,
                    model_id_source=source,
                )
            )
            index += 1
            continue

        value = None
        source = ""
        if token in {"--model-provider", "--provider"} and index + 1 < len(argv):
            value = argv[index + 1]
            source = f"command_arg:{token}"
            index += 1
        elif token.startswith("--model-provider="):
            value = token.split("=", 1)[1]
            source = "command_arg:--model-provider"
        elif token.startswith("--provider="):
            value = token.split("=", 1)[1]
            source = "command_arg:--provider"
        if value is not None:
            cleaned = clean_agent_context_value(value)
            if not cleaned:
                index += 1
                continue
            context = context.overlay(
                AgentRuntimeContext(
                    model_provider=cleaned,
                    model_provider_source=source,
                )
            )
            index += 1
            continue

        value = None
        source = ""
        if token in {"--effort", "--reasoning-effort"} and index + 1 < len(argv):
            value = argv[index + 1]
            source = f"command_arg:{token}"
            index += 1
        elif token.startswith("--effort="):
            value = token.split("=", 1)[1]
            source = "command_arg:--effort"
        elif token.startswith("--reasoning-effort="):
            value = token.split("=", 1)[1]
            source = "command_arg:--reasoning-effort"
        if value is not None:
            cleaned = clean_reasoning_effort_value(value)
            if not cleaned:
                index += 1
                continue
            context = context.overlay(
                AgentRuntimeContext(
                    reasoning_effort=cleaned,
                    reasoning_effort_source=source,
                )
            )
            index += 1
            continue

        config_value = None
        if token in {"--config", "-c"} and index + 1 < len(argv):
            config_value = argv[index + 1]
            index += 1
        elif token.startswith("--config="):
            config_value = token.split("=", 1)[1]
        elif token.startswith("-c="):
            config_value = token.split("=", 1)[1]
        if config_value is not None:
            context = context.overlay(
                parse_agent_runtime_context_from_config_arg(config_value)
            )
        index += 1

    if not context.model_provider:
        provider, source = infer_model_provider_from_command(argv)
        if provider:
            context = context.overlay(
                AgentRuntimeContext(
                    model_provider=provider,
                    model_provider_source=source,
                )
            )
    return context


def parse_agent_runtime_context_from_config_arg(value: str) -> AgentRuntimeContext:
    key, separator, raw_value = value.partition("=")
    if not separator:
        return AgentRuntimeContext()
    normalized_key = normalize_agent_context_key(key)
    source = f"command_config:{normalized_key}"
    if normalized_key in {"model", "model_id"}:
        cleaned = clean_agent_context_value(raw_value)
        if not cleaned:
            return AgentRuntimeContext()
        return AgentRuntimeContext(model_id=cleaned, model_id_source=source)
    if normalized_key in {"model_provider", "provider"}:
        cleaned = clean_agent_context_value(raw_value)
        if not cleaned:
            return AgentRuntimeContext()
        return AgentRuntimeContext(
            model_provider=cleaned,
            model_provider_source=source,
        )
    if normalized_key in {"effort", "model_reasoning_effort", "reasoning_effort"}:
        cleaned = clean_reasoning_effort_value(raw_value)
        if not cleaned:
            return AgentRuntimeContext()
        return AgentRuntimeContext(
            reasoning_effort=cleaned,
            reasoning_effort_source=source,
        )
    return AgentRuntimeContext()


def infer_model_provider_from_command(argv: list[str]) -> tuple[str, str]:
    executable = command_executable_name(argv)
    if executable == "codex":
        return "openai", "command_executable:codex"
    if executable == "claude":
        return "anthropic", "command_executable:claude"
    return "", ""


def command_executable_name(argv: list[str]) -> str:
    for token in argv:
        if SHELL_ASSIGNMENT_RE.match(token):
            continue
        return Path(token).name
    return ""


# Only Claude commands accept a forced --session-id. Codex `exec --json`
# surfaces a native thread id, but it cannot be selected before launch.
SESSION_CAPTURE_AGENT_KINDS = frozenset({"auto", "claude"})
SESSION_OBSERVED_SOURCE = "observed"


def command_specifies_session_id(argv: list[str]) -> bool:
    return any(
        token == "--session-id" or token.startswith("--session-id=") for token in argv
    )


def command_supports_session_capture(command: str, agent_kind: str) -> bool:
    """Whether a known --session-id can be injected into this agent command."""
    if agent_kind not in SESSION_CAPTURE_AGENT_KINDS:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if command_executable_name(argv) != "claude":
        return False
    return not command_specifies_session_id(argv)


def inject_structured_usage_output(command: str, agent_kind: str) -> str:
    """Request native result events only for recognized first-party CLIs."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    executable = command_executable_name(argv)
    if executable == "codex" and agent_kind in {"auto", "codex"}:
        if "exec" not in argv or "--json" in argv:
            return command
        return re.sub(r"(?<!\S)exec(?=\s|$)", "exec --json", command, count=1)
    if executable == "claude" and agent_kind in {"auto", "claude"}:
        if "--output-format" in argv or any(
            token.startswith("--output-format=") for token in argv
        ):
            return command
        return command.replace(
            "claude ", "claude --output-format stream-json --verbose ", 1
        )
    return command


def worker_usage_provenance(worker_report: WorkerReport | None) -> tuple[str, str]:
    """Return allowlisted phase and work-kind metadata from a worker report."""
    if worker_report is None:
        return "implementation", ""
    phase_value = worker_report.metadata.get("phase")
    phase = (
        phase_value if isinstance(phase_value, str) and phase_value in PHASES else ""
    )
    work_kind_value = worker_report.metadata.get("work_kind")
    work_kind = (
        work_kind_value
        if isinstance(work_kind_value, str) and work_kind_value in WORK_KINDS
        else ""
    )
    if not phase:
        phase = "review" if work_kind == "review" else "implementation"
    if phase == "review" and not work_kind:
        work_kind = "review"
    return phase, work_kind


def provider_selection_is_flexible(agent: AgentConfig, task: Task) -> bool:
    """Whether dispatch could choose a provider rather than a pinned model."""
    return agent.agent_kind == "auto" and not task.model.strip()


def inject_claude_session_id(command: str, session_id: str) -> str:
    """Force a known Claude session id by inserting --session-id before {prompt}.

    The id is a generated uuid (safe, unquoted) so this does not change the
    streamed stdout format and leaves the {prompt} placeholder intact for the
    later .format() call.
    """
    if "{prompt}" in command:
        return command.replace("{prompt}", f"--session-id {session_id} {{prompt}}", 1)
    return f"{command.rstrip()} --session-id {session_id}"


def command_specifies_resume(argv: list[str]) -> bool:
    return any(
        token in ("--resume", "-r", "--continue", "-c") or token.startswith("--resume=")
        for token in argv
    )


def command_disables_session_persistence(argv: list[str]) -> bool:
    return "--no-session-persistence" in argv


def command_supports_session_resume(command: str, agent_kind: str) -> bool:
    """Whether `claude -p --resume <id>` can be injected into this command.

    Requires the claude executable with session persistence enabled and no
    session id / resume flag already pinned by the operator.
    """
    if agent_kind not in SESSION_CAPTURE_AGENT_KINDS:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if command_executable_name(argv) != "claude":
        return False
    if command_specifies_session_id(argv) or command_specifies_resume(argv):
        return False
    return not command_disables_session_persistence(argv)


def inject_claude_resume(command: str, session_id: str) -> str:
    """Insert --resume <session_id> before {prompt} to continue a prior session.

    The id is a captured uuid (safe, unquoted); the formatted {prompt} becomes
    the next turn of the resumed conversation. Relies on the claude CLI contract
    that `claude -p --resume <id>` continues the SAME session/transcript rather
    than forking a new id (so repeated recovery attempts keep resuming one
    session); if a future claude version forks on resume this assumption, and
    the stable-transcript resolution below, would need revisiting.
    """
    if "{prompt}" in command:
        return command.replace("{prompt}", f"--resume {session_id} {{prompt}}", 1)
    return f"{command.rstrip()} --resume {session_id}"


def resumable_prior_session_id(prior_result: RunResult) -> str:
    """Prior claude session id only when it is safely resumable.

    Requires a vibe-loop-injected ("observed") session whose transcript is
    actually on disk — this fails closed to the fresh-worker recovery path when
    the prior run exited before persisting its session, rather than handing
    `claude -p --resume` a dead id (which errors "No conversation found").
    """
    if prior_result.session_id_source != SESSION_OBSERVED_SOURCE:
        return ""
    if not prior_result.session_id:
        return ""
    transcript = prior_result.transcript_path
    if not transcript or not Path(transcript).exists():
        return ""
    return prior_result.session_id


def leading_env_assignment(command: str, name: str) -> str | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for token in argv:
        if not SHELL_ASSIGNMENT_RE.match(token):
            break
        key, _, value = token.partition("=")
        if key == name:
            return value
    return None


def claude_project_dir_name(cwd: Path) -> str:
    """Claude Code encodes a session's launch cwd into its project dir name by
    replacing every non-alphanumeric character with a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))


def resolve_claude_home(command: str, env: dict[str, str], cwd: Path) -> Path:
    inline = leading_env_assignment(command, "CLAUDE_HOME")
    if inline:
        candidate = Path(inline)
        return candidate if candidate.is_absolute() else Path(cwd) / candidate
    env_home = env.get("CLAUDE_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".claude"


def resolve_codex_home(command: str, env: dict[str, str], cwd: Path) -> Path:
    inline = leading_env_assignment(command, "CODEX_HOME")
    if inline:
        candidate = Path(inline)
        return candidate if candidate.is_absolute() else Path(cwd) / candidate
    env_home = env.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".codex"


def resolve_codex_rollout(session_id: str, codex_home: Path) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,159}", session_id):
        return None
    sessions = Path(codex_home) / "sessions"
    try:
        matches = sorted(
            candidate
            for candidate in sessions.glob("*/*/*/*.jsonl")
            if candidate.stem.endswith(f"-{session_id}")
        )
    except OSError:
        return None
    return matches[-1] if matches else None


def predicted_claude_transcript(
    session_id: str,
    cwd: Path,
    claude_home: Path,
) -> Path:
    return (
        Path(claude_home)
        / "projects"
        / claude_project_dir_name(cwd)
        / f"{session_id}.jsonl"
    )


def resolve_claude_transcript(session_id: str, claude_home: Path) -> Path | None:
    """Find the real transcript by globbing for the unique session id, so the
    result is correct regardless of the cwd-to-project-dir encoding."""
    projects = Path(claude_home) / "projects"
    try:
        matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def agent_context_source_rank(source: str) -> int:
    if not source:
        return 0
    for prefix, rank in AGENT_CONTEXT_SOURCE_RANKS:
        if source.startswith(prefix):
            if prefix == "native:" and ":json." not in source:
                return AGENT_CONTEXT_UNSTRUCTURED_NATIVE_RANK
            return rank
    return AGENT_CONTEXT_UNSTRUCTURED_NATIVE_RANK


def pick_agent_context_field(
    current: str,
    current_source: str,
    candidate: str,
    candidate_source: str,
) -> tuple[str, str]:
    if not candidate:
        return current, current_source
    if not current:
        return candidate, candidate_source
    candidate_rank = agent_context_source_rank(candidate_source)
    current_rank = agent_context_source_rank(current_source)
    if candidate == current:
        if candidate_rank > current_rank:
            return candidate, candidate_source
        return current, current_source
    if candidate_rank >= current_rank:
        return candidate, candidate_source
    return current, current_source


def pick_agent_model_field(
    current: str,
    current_source: str,
    candidate: str,
    candidate_source: str,
    *,
    current_provider: str,
) -> tuple[str, str]:
    if (
        current_provider == "anthropic"
        and current.lower() in CLAUDE_MODEL_ALIASES
        and current_source.startswith(("command_arg:", "command_config:"))
        and candidate_source.startswith("native:")
        and ":json." in candidate_source
    ):
        return candidate, candidate_source
    return pick_agent_context_field(
        current,
        current_source,
        candidate,
        candidate_source,
    )


def parse_agent_runtime_context_from_line(
    line: str,
    stream_name: str,
) -> AgentRuntimeContext:
    source_prefix = f"native:{stream_name}"
    json_payload = agent_context_json_payload(line)
    if json_payload is not None:
        # A structured line is parsed structurally only; rescanning its raw text
        # would harvest nested keys the structured reader deliberately rejected.
        return parse_agent_runtime_context_from_json_payload(
            json_payload, source_prefix
        )
    return parse_agent_runtime_context_from_text_line(line, source_prefix)


def agent_context_json_payload(line: str) -> dict[str, object] | None:
    text = line.strip()
    if text.startswith("data:"):
        text = text.removeprefix("data:").strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_agent_runtime_context_from_json_line(
    line: str,
    source_prefix: str,
) -> AgentRuntimeContext:
    payload = agent_context_json_payload(line)
    if payload is None:
        return AgentRuntimeContext()
    return parse_agent_runtime_context_from_json_payload(payload, source_prefix)


def parse_agent_runtime_context_from_json_payload(
    payload: dict[str, object],
    source_prefix: str,
) -> AgentRuntimeContext:
    model_value = payload.get("model")
    model_mapping = model_value if isinstance(model_value, dict) else {}
    model_provider = first_clean_agent_context_value(
        payload.get("model_provider"),
        model_mapping.get("provider"),
        payload.get("provider"),
    )
    bare_model = (
        model_value
        if isinstance(model_value, str) and payload_declares_model_identity(payload)
        else None
    )
    model_id = first_clean_agent_context_value(
        payload.get("model_id"),
        model_mapping.get("id"),
        bare_model,
    )
    reasoning_effort = first_clean_agent_context_value(
        payload.get("effort"),
        payload.get("reasoning_effort"),
        model_mapping.get("effort"),
        model_mapping.get("reasoning_effort"),
        clean_value=clean_reasoning_effort_value,
    )
    return AgentRuntimeContext(
        model_provider=model_provider,
        model_provider_source=(
            f"{source_prefix}:json.model_provider" if model_provider else ""
        ),
        model_id=model_id,
        model_id_source=f"{source_prefix}:json.model" if model_id else "",
        reasoning_effort=reasoning_effort,
        reasoning_effort_source=(
            f"{source_prefix}:json.reasoning_effort" if reasoning_effort else ""
        ),
    )


def parse_agent_runtime_context_from_text_line(
    line: str,
    source_prefix: str,
) -> AgentRuntimeContext:
    context = AgentRuntimeContext()
    for match in AGENT_CONTEXT_RE.finditer(line):
        key = normalize_agent_context_key(match.group("key"))
        value = clean_agent_context_value(match.group("value"))
        if not value:
            continue
        if key in {"effort", "reasoning_effort"}:
            value = clean_reasoning_effort_value(match.group("value"))
            if not value:
                continue
            context = context.overlay(
                AgentRuntimeContext(
                    reasoning_effort=value,
                    reasoning_effort_source=f"{source_prefix}:{key}",
                )
            )
    return context


def payload_declares_model_identity(payload: dict[str, object]) -> bool:
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return False
    return event_type.strip().lower() in MODEL_IDENTITY_EVENT_TYPES


def normalize_agent_context_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def first_clean_agent_context_value(
    *values: object,
    clean_value: Callable[[object], str] | None = None,
) -> str:
    cleaner = clean_value or clean_agent_context_value
    for value in values:
        cleaned = cleaner(value)
        if cleaned:
            return cleaned
    return ""


def clean_agent_context_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().strip("'\"")
    if not cleaned or "\0" in cleaned or "\n" in cleaned or "\r" in cleaned:
        return ""
    if len(cleaned) > AGENT_CONTEXT_VALUE_MAX_CHARS:
        return ""
    if not AGENT_CONTEXT_SAFE_VALUE_RE.fullmatch(cleaned):
        return ""
    if agent_context_value_is_secret_like(cleaned):
        return ""
    return cleaned


def clean_reasoning_effort_value(value: object) -> str:
    cleaned = clean_agent_context_value(value).lower()
    return cleaned if cleaned in REASONING_EFFORT_VALUES else ""


def agent_context_value_is_secret_like(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("sk-", "ghp_", "github_pat_", "xoxb-", "xoxp-")):
        return True
    normalized = lowered.replace("-", "_").replace(".", "_")
    return any(token in normalized for token in SECRET_LIKE_CONTEXT_TOKENS)


def build_trailer_context(
    *,
    task_id: str,
    run_id: str,
    session_id: str,
    session_id_source: str,
    agent_kind: str,
    agent_kind_source: str,
    agent_prompt_dialect: str,
    agent_prompt_dialect_source: str,
    agent_skill_ref_prefix: str,
    agent_skill_ref_prefix_source: str,
    runtime_context: AgentRuntimeContext,
    agent_profile: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    context: dict[str, object] = {
        "plan_item_candidates": [task_id],
        "run_id": run_id,
        "session_id": session_id,
        "session_id_source": session_id_source,
    }
    sources: dict[str, object] = {
        "plan_item_candidates": "task_id",
        "run_id": "run_id",
        "session_id": session_id_source,
        "session_id_source": "session_observation",
    }
    if agent_profile:
        context["agent_profile"] = agent_profile
        sources["agent_profile"] = "agent.routing"
    if agent_kind:
        context["agent_kind"] = agent_kind
        sources["agent_kind"] = agent_kind_source
    if agent_prompt_dialect:
        context["agent_prompt_dialect"] = agent_prompt_dialect
        sources["agent_prompt_dialect"] = agent_prompt_dialect_source
    if agent_skill_ref_prefix:
        context["agent_skill_ref_prefix"] = agent_skill_ref_prefix
        sources["agent_skill_ref_prefix"] = agent_skill_ref_prefix_source
    if runtime_context.model_provider:
        context["model_provider"] = runtime_context.model_provider
        sources["model_provider"] = runtime_context.model_provider_source
    if runtime_context.model_id:
        context["model"] = runtime_context.model_id
        sources["model"] = runtime_context.model_id_source
        context["model_id"] = runtime_context.model_id
        sources["model_id"] = runtime_context.model_id_source
    if runtime_context.reasoning_effort:
        context["effort"] = runtime_context.reasoning_effort
        sources["effort"] = runtime_context.reasoning_effort_source
        context["reasoning_effort"] = runtime_context.reasoning_effort
        sources["reasoning_effort"] = runtime_context.reasoning_effort_source
    return context, sources


def build_run_context_payload(
    *,
    task_id: str,
    run_id: str,
    started_at: str,
    session_id: str,
    session_id_source: str,
    agent_kind: str,
    agent_kind_source: str,
    agent_prompt_dialect: str,
    agent_prompt_dialect_source: str,
    agent_skill_ref_prefix: str,
    agent_skill_ref_prefix_source: str,
    runtime_context: AgentRuntimeContext,
    agent_profile: str = "",
    transcript_path: str = "",
) -> dict[str, object]:
    trailer_context, trailer_context_sources = build_trailer_context(
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        session_id_source=session_id_source,
        agent_kind=agent_kind,
        agent_kind_source=agent_kind_source,
        agent_prompt_dialect=agent_prompt_dialect,
        agent_prompt_dialect_source=agent_prompt_dialect_source,
        agent_skill_ref_prefix=agent_skill_ref_prefix,
        agent_skill_ref_prefix_source=agent_skill_ref_prefix_source,
        runtime_context=runtime_context,
        agent_profile=agent_profile,
    )
    payload: dict[str, object] = {
        "started_at": started_at,
        "session_id": session_id,
        "session_id_source": session_id_source,
        "agent_kind": agent_kind,
        "agent_kind_source": agent_kind_source,
        "agent_prompt_dialect": agent_prompt_dialect,
        "agent_prompt_dialect_source": agent_prompt_dialect_source,
        "agent_skill_ref_prefix": agent_skill_ref_prefix,
        "agent_skill_ref_prefix_source": agent_skill_ref_prefix_source,
        "trailer_context": trailer_context,
        "trailer_context_sources": trailer_context_sources,
    }
    if agent_profile:
        payload["agent_profile"] = agent_profile
    if transcript_path:
        payload["transcript_path"] = transcript_path
    payload.update(runtime_context.to_record_fields())
    return payload


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
    agent_kind: str,
    prompt_dialect: str | None,
    prompt_dialect_source: str,
    skill_ref_prefix: str | None,
    skill_ref_prefix_source: str,
    *,
    fencing_token: str = "",
) -> None:
    header = (
        f"[vibe-loop] run_id={run_id}\n"
        f"[vibe-loop] task_id={task.task_id}\n"
        f"[vibe-loop] title={task.title}\n"
        f"[vibe-loop] command={command}\n"
        f"[vibe-loop] agent_command_source={command_source}\n"
        "[vibe-loop] agent_selection_command_source="
        f"{selection_command_source}\n"
        "[vibe-loop] agent_default_policy_source="
        f"{AGENT_DEFAULT_POLICY_SOURCE}\n"
        f"[vibe-loop] agent_default_policy={AGENT_DEFAULT_POLICY}\n"
        f"[vibe-loop] agent_kind={agent_kind}\n"
        f"[vibe-loop] agent_prompt_dialect={prompt_dialect or ''}\n"
        f"[vibe-loop] agent_prompt_dialect_source={prompt_dialect_source}\n"
        f"[vibe-loop] agent_skill_ref_prefix={skill_ref_prefix or ''}\n"
        f"[vibe-loop] agent_skill_ref_prefix_source={skill_ref_prefix_source}\n"
        f"[vibe-loop] detected_agents={format_detected_agents(detected)}\n"
        f"[vibe-loop] start_main={start_main}\n\n"
    )
    log.write(redact_fencing_token_text(header, fencing_token))


def report_status(message: str, log: TextIO | None = None) -> None:
    line = f"[vibe-loop] {message}"
    print(line, file=sys.stderr)
    if log is not None:
        log.write(line + "\n")
        log.flush()


def terminate_worker_process_group(
    process: subprocess.Popen,
    log: TextIO,
    *,
    sigkill_after_seconds: float = 10.0,
) -> None:
    """Terminate a worker's whole process group (SIGTERM, then SIGKILL).

    Used to reap a worker that stays alive after it has already filed its
    terminal report -- typically held up by orphaned background children that
    keep its pipes open. Falls back to killing just the process where process
    groups are unavailable (non-POSIX).
    """
    if not hasattr(os, "killpg"):
        try:
            process.terminate()
            try:
                process.wait(timeout=sigkill_after_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        process.kill()
        return
    try:
        process.wait(timeout=sigkill_after_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


@dataclasses.dataclass(frozen=True)
class WaitOutcome:
    exit_code: int
    # True when the wall-clock deadline fired and the process group was killed.
    timed_out: bool = False


def wait_with_reap_watchdog(
    process: subprocess.Popen,
    log: TextIO,
    *,
    reap_check: Callable[[], bool] | None,
    grace_seconds: float,
    poll_seconds: float,
    timeout_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> WaitOutcome:
    """Wait for a worker, reaping it if it hangs.

    Two independent reap conditions apply:

    * Wall-clock deadline (``timeout_seconds``): an absolute upper bound on the
      run regardless of whether the worker ever filed a report. When it fires
      the worker's process group is force-killed and ``timed_out=True`` is
      returned so the caller can classify the run as ``timed_out`` and return
      the task to runnable. ``None`` or a non-positive value disables it,
      preserving the historical unbounded behavior.
    * Report-then-hang grace (``reap_check``): once ``reap_check`` first returns
      True (e.g. the worker filed a terminal report, so it intends to exit) a
      grace timer starts; if the process is still alive ``grace_seconds`` later
      its process group is terminated. A worker that exits on its own within
      grace is never force-killed.

    ``monotonic`` is injectable so tests can drive the deadline with a fake
    clock instead of a real wall-clock sleep.
    """
    deadline: float | None = None
    if timeout_seconds is not None and timeout_seconds > 0:
        deadline = monotonic() + timeout_seconds
    if reap_check is None and deadline is None:
        return WaitOutcome(process.wait())

    def _reap_for_timeout() -> WaitOutcome:
        report_status(
            f"worker pid={process.pid} exceeded its "
            f"{timeout_seconds:.0f}s wall-clock timeout; killing its process "
            "group so the task returns to runnable and the batch proceeds",
            log,
        )
        terminate_worker_process_group(process, log)
        return WaitOutcome(process.wait(), timed_out=True)

    reap_eligible_since: float | None = None
    while True:
        wait_for = poll_seconds
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _reap_for_timeout()
            wait_for = min(poll_seconds, remaining)
        try:
            return WaitOutcome(process.wait(timeout=wait_for))
        except subprocess.TimeoutExpired:
            pass
        if deadline is not None and monotonic() - deadline >= 0:
            return _reap_for_timeout()
        if reap_check is None:
            continue
        try:
            eligible = reap_check()
        # The watchdog must never crash the wait: a flaky report read should
        # leave the worker running, not abort supervision.
        except Exception:
            eligible = False
        if not eligible:
            continue
        now = monotonic()
        if reap_eligible_since is None:
            reap_eligible_since = now
            continue
        if now - reap_eligible_since >= grace_seconds:
            report_status(
                f"worker pid={process.pid} still alive "
                f"{grace_seconds:.0f}s after filing its terminal report; "
                "reaping process group to release its slot",
                log,
            )
            terminate_worker_process_group(process, log)
            return WaitOutcome(process.wait())


def run_streaming_command(
    command: str,
    cwd: Path,
    log: TextIO,
    *,
    env: dict[str, str] | None = None,
    forward_stderr: bool = False,
    on_start: Callable[[int], None] | None = None,
    on_observation: Callable[[AgentRuntimeObservation], None] | None = None,
    reap_check: Callable[[], bool] | None = None,
    reap_grace_seconds: float = 120.0,
    reap_poll_seconds: float = 10.0,
    timeout_seconds: float | None = None,
    provider: str = "unknown",
) -> StreamingCommandResult:
    cmd, use_shell = prepare_shell_command(command)
    popen_kwargs: dict[str, object] = {}
    if os.name != "nt":
        # Own session/process group so a worker that hangs after reporting can
        # be reaped as a unit, including any orphaned background grandchildren
        # that keep its stdout/stderr pipes open.
        popen_kwargs["start_new_session"] = True
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
        **popen_kwargs,
    )
    try:
        if on_start is not None:
            on_start(process.pid)
    # Startup callbacks persist ownership evidence. Every failure, including
    # interruption, must reap the new group before that evidence can be lost.
    except BaseException:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        except OSError:
            pass
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe is None:
                    continue
                try:
                    pipe.close()
                except OSError:
                    pass
        raise
    assert process.stdout is not None
    assert process.stderr is not None
    log_lock = threading.Lock()
    fencing_token = fencing_token_value((env or {}).get("VIBE_LOOP_FENCING_TOKEN"))
    output_observer = AgentOutputObserver(provider)
    stdout_thread = threading.Thread(
        target=stream_pipe,
        args=(
            process.stdout,
            log,
            log_lock,
            True,
            output_observer,
            "stdout",
            on_observation,
            fencing_token,
        ),
    )
    stderr_thread = threading.Thread(
        target=stream_pipe,
        args=(
            process.stderr,
            log,
            log_lock,
            forward_stderr,
            output_observer,
            "stderr",
            on_observation,
            fencing_token,
        ),
    )
    stdout_thread.start()
    stderr_thread.start()
    wait_outcome = wait_with_reap_watchdog(
        process,
        log,
        reap_check=reap_check,
        grace_seconds=reap_grace_seconds,
        poll_seconds=reap_poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    stdout_thread.join()
    stderr_thread.join()
    observation = output_observer.observation
    return StreamingCommandResult(
        exit_code=wait_outcome.exit_code,
        session_id=observation.session_id,
        session_id_source=observation.session_id_source,
        runtime_context=observation.runtime_context,
        timed_out=wait_outcome.timed_out,
        usage=output_observer.usage,
    )


def worker_command_env(
    *,
    run_id: str,
    task_id: str,
    repo: Path,
    log_path: Path,
    agent_kind: str,
    agent_profile: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "VIBE_LOOP_RUN_ID": run_id,
            "VIBE_LOOP_TASK_ID": task_id,
            "VIBE_LOOP_REPO": str(repo),
            "VIBE_LOOP_LOG": str(log_path),
            "VIBE_LOOP_AGENT_KIND": agent_kind,
            "VIBE_LOOP_AGENT_PROFILE": agent_profile,
        }
    )
    return env


def stream_pipe(
    pipe: TextIO,
    log: TextIO,
    log_lock: threading.Lock,
    forward: bool,
    output_observer: AgentOutputObserver,
    stream_name: str,
    on_observation: Callable[[AgentRuntimeObservation], None] | None = None,
    fencing_token: str = "",
) -> None:
    try:
        for line in pipe:
            redacted_line = redact_worker_stream_line(line, fencing_token)
            observation = output_observer.observe_line(redacted_line, stream_name)
            if observation is not None and on_observation is not None:
                on_observation(observation)
            if forward:
                sys.stderr.write(redacted_line)
                sys.stderr.flush()
            with log_lock:
                log.write(redacted_line)
                log.flush()
    finally:
        pipe.close()


def redact_worker_stream_line(line: str, fencing_token: str) -> str:
    if not fencing_token:
        return line
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return redact_fencing_token_text(line, fencing_token)
    if fencing_token_value(payload) == fencing_token:
        newline = "\n" if line.endswith("\n") else ""
        return FENCING_TOKEN_REDACTION + newline
    field_redacted = redact_fencing_token_payload(payload)
    redacted = redact_exact_fencing_token(field_redacted, fencing_token)
    if redacted == payload:
        return line
    newline = "\n" if line.endswith("\n") else ""
    return json.dumps(redacted, separators=(",", ":")) + newline


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


def _analysis_retry_callback(attempt: int, delay: float, reason: str) -> None:
    report_status(f"agent analysis retry {attempt} after {delay:.1f}s: {reason}")


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


def transient_failure_cooldown(
    result: RunResult,
    default_cooldown: float,
    log_tail_lines: int = LOG_TAIL_LINES_FOR_TRANSIENT_CHECK,
) -> float:
    """Cooldown before retrying a transient failure.

    Quota/usage-limit failures advertise a reset time (e.g. "resets 2:40am
    (UTC)"); retrying before that point burns restart budget for nothing, so
    the parsed reset delay extends the configured cooldown when present.
    """
    log_path = result.log_path
    if not isinstance(log_path, Path) or not log_path.exists():
        return default_cooldown
    try:
        tail = _read_log_tail(log_path, log_tail_lines)
    except OSError:
        return default_cooldown
    delay = parse_quota_reset_delay(tail)
    if delay is None:
        return default_cooldown
    return max(default_cooldown, delay)


def next_retry_delay(retry_ready_at: dict[str, float]) -> float | None:
    now = time.monotonic()
    future_times = [ready_at for ready_at in retry_ready_at.values() if ready_at > now]
    if not future_times:
        return None
    return max(0.0, min(future_times) - now)


def discard_ready_retries(retry_ready_at: dict[str, float]) -> bool:
    now = time.monotonic()
    ready_task_ids = [
        task_id for task_id, ready_at in retry_ready_at.items() if ready_at <= now
    ]
    for task_id in ready_task_ids:
        retry_ready_at.pop(task_id, None)
    return bool(ready_task_ids)


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


def git_changed_lines(repo: Path, start_rev: str, end_rev: str) -> int | None:
    if not start_rev or not end_rev:
        return None
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", start_rev, end_rev],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    changed = 0
    for line in result.stdout.splitlines():
        added, separator, remainder = line.partition("\t")
        deleted, separator, _path = remainder.partition("\t")
        if not separator or not added.isdigit() or not deleted.isdigit():
            continue
        changed += int(added) + int(deleted)
    return changed
