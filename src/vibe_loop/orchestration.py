from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from vibe_loop.config import (
    AgentConfig,
    AgentResolutionError,
    AgentSelection,
    OrchestrationConfig,
    TaskSourceConfig,
    VibeConfig,
    agent_command_provider,
    command_template_uses_field,
    format_agent_command,
    parse_orchestration,
)
from vibe_loop.retry import LimitWallSignal, detect_limit_wall
from vibe_loop.tasks import BLOCKED_FAMILY_STATUSES, Task, TaskSource
from vibe_loop.telemetry import (
    ProviderUsage,
    ProviderUsageObserver,
    unavailable_usage,
)


RUN_CONTRACT_VERSION = 1
RUN_CONTRACT_SOURCE_KINDS = ("config", "profile", "skill-proposal")
WORKSPACE_BRANCH_PREFIX = "vibe-loop/"
WORKSPACE_NAME_MAX_LENGTH = 64
CANDIDATE_RECORD_SOURCE_KINDS = ("worker_command", "derived")
GATE_EXIT_CLASSES = ("passed", "failed", "candidate_changed", "execution_error")
REVIEW_VERDICTS = ("approve", "findings", "error")
REVIEW_RETRY_CLASSIFICATIONS = ("ok", "transient", "limit_wall", "timeout", "fatal")
FINDING_SEVERITIES = ("P0", "P1", "P2", "P3")
FINDING_STATES = ("open", "remediated", "accepted", "rejected")
CONTINUATION_FALLBACK_REASONS = (
    "provider_unsupported",
    "transcript_missing",
    "session_expired",
)
REVIEW_ROLES = ("implementer", "reviewer")
NESTED_REVIEW_EVENT_TYPES = frozenset(
    {
        "agent.spawned",
        "subagent.started",
        "subagent.spawned",
        "task.delegated",
        "workflow.started",
    }
)
NESTED_REVIEW_TOOL_NAMES = frozenset(
    {"agent", "task", "workflow", "spawn_agent", "delegate_agent"}
)


@dataclasses.dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    role: str
    session_injection: bool
    resume: bool
    structured_output: bool
    nested_delegation_disable: bool


PROVIDER_CAPABILITY_TABLE: Mapping[tuple[str, str], ProviderCapabilities] = {
    ("claude", "implementer"): ProviderCapabilities(
        provider="claude",
        role="implementer",
        session_injection=True,
        resume=True,
        structured_output=True,
        nested_delegation_disable=True,
    ),
    ("claude", "reviewer"): ProviderCapabilities(
        provider="claude",
        role="reviewer",
        session_injection=True,
        resume=True,
        structured_output=True,
        nested_delegation_disable=True,
    ),
    ("codex", "implementer"): ProviderCapabilities(
        provider="codex",
        role="implementer",
        session_injection=False,
        resume=True,
        structured_output=True,
        nested_delegation_disable=False,
    ),
    ("codex", "reviewer"): ProviderCapabilities(
        provider="codex",
        role="reviewer",
        session_injection=False,
        resume=False,
        structured_output=False,
        nested_delegation_disable=False,
    ),
}


def provider_capabilities(provider: str, role: str) -> ProviderCapabilities:
    if role not in REVIEW_ROLES:
        raise ValueError(f"unsupported continuation role: {role}")
    try:
        return PROVIDER_CAPABILITY_TABLE[(provider, role)]
    except KeyError:
        return ProviderCapabilities(
            provider=provider or "unknown",
            role=role,
            session_injection=False,
            resume=False,
            structured_output=False,
            nested_delegation_disable=False,
        )


class RunStage(enum.StrEnum):
    ACTIVATION = "activation"
    WORKSPACE = "workspace"
    IMPLEMENTING = "implementing"
    CANDIDATE = "candidate"
    GATES = "gates"
    REVIEW = "review"
    REMEDIATION = "remediation"
    CLOSURE = "closure"
    INTEGRATION = "integration"
    PROVENANCE = "provenance"
    CLASSIFICATION = "classification"
    FINALIZATION = "finalization"


class StageFailure(enum.StrEnum):
    LIMIT_WALL = "limit_wall"
    TIMED_OUT = "timed_out"
    STAGE_FAILED = "stage_failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


RUN_STAGES = tuple(stage.value for stage in RunStage)
STAGE_FAILURES = tuple(failure.value for failure in StageFailure)

# The worker-owned compatibility edge from implementing directly to
# classification is intentional: shadow mode cannot infer candidate, gate, or
# review boundaries that remain inside the worker process. Runtime-owned mode
# follows the longer path as those owners land in later ORC slices.
LEGAL_STAGE_TRANSITIONS: Mapping[RunStage, frozenset[RunStage]] = {
    RunStage.ACTIVATION: frozenset({RunStage.WORKSPACE}),
    RunStage.WORKSPACE: frozenset({RunStage.IMPLEMENTING}),
    RunStage.IMPLEMENTING: frozenset({RunStage.CANDIDATE, RunStage.CLASSIFICATION}),
    RunStage.CANDIDATE: frozenset({RunStage.GATES}),
    RunStage.GATES: frozenset(
        {RunStage.REVIEW, RunStage.REMEDIATION, RunStage.CLOSURE}
    ),
    RunStage.REVIEW: frozenset({RunStage.REMEDIATION, RunStage.INTEGRATION}),
    RunStage.REMEDIATION: frozenset({RunStage.CANDIDATE}),
    RunStage.CLOSURE: frozenset({RunStage.REMEDIATION, RunStage.INTEGRATION}),
    RunStage.INTEGRATION: frozenset({RunStage.PROVENANCE}),
    RunStage.PROVENANCE: frozenset({RunStage.CLASSIFICATION}),
    RunStage.CLASSIFICATION: frozenset({RunStage.FINALIZATION}),
    RunStage.FINALIZATION: frozenset(),
}


class StageTransitionError(RuntimeError):
    pass


class CandidateCollectionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class GateExecutionError(RuntimeError):
    pass


class ReviewExecutionError(RuntimeError):
    pass


class ReviewBudgetExhausted(ReviewExecutionError):
    def __init__(self, pass_kind: str, limit: int) -> None:
        self.pass_kind = pass_kind
        self.limit = limit
        super().__init__(f"review budget exhausted for {pass_kind}: limit={limit}")


class ReviewWaitIncomplete(ReviewExecutionError):
    def __init__(self, pass_kind: str, pass_ordinal: int, attempt_ordinal: int) -> None:
        self.pass_kind = pass_kind
        self.pass_ordinal = pass_ordinal
        self.attempt_ordinal = attempt_ordinal
        super().__init__(
            "review wait is incomplete for "
            f"{pass_kind} pass {pass_ordinal} attempt {attempt_ordinal}"
        )


class ReviewDelegationPolicyError(ReviewExecutionError):
    def __init__(self, nested_launches: int) -> None:
        self.nested_launches = nested_launches
        super().__init__(
            "reviewer violated the no-delegation policy: "
            f"nested_launches={nested_launches}"
        )


class ReviewSessionExpired(ReviewExecutionError):
    pass


class ReviewLimitWallError(ReviewExecutionError):
    def __init__(
        self,
        signal: LimitWallSignal,
        *,
        route: str,
        phase: str,
    ) -> None:
        self.signal = signal
        self.route = route
        self.phase = phase
        detail = f" ({signal.reset_text})" if signal.reset_text else ""
        super().__init__(f"reviewer limit wall on {route}: {signal.marker}{detail}")


class ReviewStageResultError(ReviewExecutionError):
    def __init__(self, retry_classification: str) -> None:
        self.retry_classification = retry_classification
        super().__init__(
            f"reviewer returned typed {retry_classification} error verdict"
        )


class GateRemediationExhausted(GateExecutionError):
    def __init__(self, max_rounds: int, failed_gate_keys: Sequence[str]) -> None:
        self.max_rounds = max_rounds
        self.failed_gate_keys = tuple(failed_gate_keys)
        super().__init__(
            "gate remediation exhausted after "
            f"{max_rounds} round(s): {', '.join(self.failed_gate_keys)}"
        )


class IllegalStageTransitionError(StageTransitionError):
    def __init__(
        self,
        from_stage: RunStage | None,
        to_stage: RunStage,
    ) -> None:
        self.from_stage = from_stage
        self.to_stage = to_stage
        source = from_stage.value if from_stage is not None else "<initial>"
        super().__init__(f"illegal run stage transition: {source} -> {to_stage.value}")


@dataclasses.dataclass(frozen=True)
class StageTransition:
    from_stage: RunStage | None
    to_stage: RunStage
    reason: str
    ordinal: int
    accepted: bool
    failure: StageFailure | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "from_stage": self.from_stage.value if self.from_stage is not None else "",
            "to_stage": self.to_stage.value,
            "reason": self.reason,
            "ordinal": self.ordinal,
            "accepted": self.accepted,
        }
        if self.failure is not None:
            payload["failure"] = self.failure.value
        return payload


StageJournal = Callable[[StageTransition], None]


@dataclasses.dataclass(frozen=True)
class DerivedStageProgress:
    stage: RunStage
    ordinal: int
    occurred_at: str = ""


class RunLifecycleStateMachine:
    def __init__(self, journal: StageJournal) -> None:
        self._journal = journal
        self._stage: RunStage | None = None
        self._ordinals: dict[RunStage, int] = {}

    @property
    def stage(self) -> RunStage | None:
        return self._stage

    @property
    def ordinal(self) -> int:
        if self._stage is None:
            return 0
        return self._ordinals.get(self._stage, 0)

    def ordinal_for(self, stage: RunStage) -> int:
        return self._ordinals.get(stage, 0)

    def transition(
        self,
        to_stage: RunStage,
        *,
        reason: str,
    ) -> StageTransition:
        accepted = (
            to_stage is RunStage.ACTIVATION
            if self._stage is None
            else to_stage in LEGAL_STAGE_TRANSITIONS[self._stage]
        )
        transition = StageTransition(
            from_stage=self._stage,
            to_stage=to_stage,
            reason=reason,
            ordinal=self._ordinals.get(to_stage, 0) + 1,
            accepted=accepted,
        )
        self._journal(transition)
        if not accepted:
            raise IllegalStageTransitionError(self._stage, to_stage)
        self._accept(transition)
        return transition

    def fail(
        self,
        failure: StageFailure,
        *,
        reason: str,
    ) -> StageTransition:
        if self._stage is None:
            raise StageTransitionError("cannot record a failure before activation")
        if self._stage is RunStage.FINALIZATION:
            destination = RunStage.FINALIZATION
        elif self._stage is RunStage.CLASSIFICATION:
            destination = RunStage.FINALIZATION
        else:
            destination = RunStage.CLASSIFICATION
        transition = StageTransition(
            from_stage=self._stage,
            to_stage=destination,
            reason=reason,
            ordinal=self._ordinals.get(destination, 0) + 1,
            accepted=True,
            failure=failure,
        )
        self._journal(transition)
        self._accept(transition)
        return transition

    def _accept(self, transition: StageTransition) -> None:
        self._stage = transition.to_stage
        self._ordinals[transition.to_stage] = transition.ordinal

    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, Any]],
        journal: StageJournal,
    ) -> RunLifecycleStateMachine:
        machine = cls(journal)
        for record in records:
            transition = accepted_stage_transition(record)
            if transition is None:
                continue
            machine._accept(transition)
        return machine


def accepted_stage_transition(
    record: Mapping[str, Any],
) -> StageTransition | None:
    if record.get("record_type") != "stage_transition":
        return None
    if record.get("accepted") is not True:
        return None
    try:
        to_stage = RunStage(record.get("to_stage"))
    except (TypeError, ValueError):
        return None
    raw_from = record.get("from_stage")
    try:
        from_stage = RunStage(raw_from) if raw_from else None
    except (TypeError, ValueError):
        return None
    raw_ordinal = record.get("ordinal")
    if isinstance(raw_ordinal, bool) or not isinstance(raw_ordinal, int):
        return None
    if raw_ordinal < 1:
        return None
    raw_failure = record.get("failure")
    try:
        failure = StageFailure(raw_failure) if raw_failure else None
    except (TypeError, ValueError):
        return None
    reason = record.get("reason")
    return StageTransition(
        from_stage=from_stage,
        to_stage=to_stage,
        reason=reason if isinstance(reason, str) else "",
        ordinal=raw_ordinal,
        accepted=True,
        failure=failure,
    )


def derive_stage_progress(
    records: Sequence[Mapping[str, Any]],
) -> DerivedStageProgress | None:
    latest: DerivedStageProgress | None = None
    for record in records:
        transition = accepted_stage_transition(record)
        if transition is None:
            continue
        occurred_at = record.get("occurred_at")
        latest = DerivedStageProgress(
            stage=transition.to_stage,
            ordinal=transition.ordinal,
            occurred_at=occurred_at if isinstance(occurred_at, str) else "",
        )
    return latest


@dataclasses.dataclass(frozen=True)
class CandidateRecord:
    branch: str
    worktree: Path
    base_main: str
    head_commit: str
    changed_paths: tuple[str, ...]
    source: str

    def __post_init__(self) -> None:
        if self.source not in CANDIDATE_RECORD_SOURCE_KINDS:
            raise ValueError(
                "candidate source must be one of: "
                + ", ".join(CANDIDATE_RECORD_SOURCE_KINDS)
            )

    @property
    def fingerprint(self) -> str:
        return sha256_digest(
            {
                "head_commit": self.head_commit,
                "changed_paths": list(self.changed_paths),
            }
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_main": self.base_main,
            "head_commit": self.head_commit,
            "changed_paths": list(self.changed_paths),
            "source": self.source,
            "candidate_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> CandidateRecord | None:
        if record.get("record_type") != "candidate_recorded":
            return None
        branch = record.get("branch")
        worktree = record.get("worktree")
        base_main = record.get("base_main")
        head_commit = record.get("head_commit")
        changed_paths = record.get("changed_paths")
        source = record.get("source")
        if (
            not isinstance(branch, str)
            or not isinstance(worktree, str)
            or not isinstance(base_main, str)
            or not isinstance(head_commit, str)
            or not isinstance(changed_paths, list)
            or not all(isinstance(path, str) for path in changed_paths)
            or source not in CANDIDATE_RECORD_SOURCE_KINDS
        ):
            return None
        candidate = cls(
            branch=branch,
            worktree=Path(worktree),
            base_main=base_main,
            head_commit=head_commit,
            changed_paths=tuple(changed_paths),
            source=str(source),
        )
        if record.get("candidate_fingerprint") != candidate.fingerprint:
            return None
        return candidate


class CandidateCollector:
    def __init__(
        self,
        *,
        worktree: Path,
        branch: str,
        base_main: str,
        run_store: object,
        run_id: str,
        task_id: str,
    ) -> None:
        self.worktree = worktree.resolve()
        self.branch = branch
        self.base_main = base_main
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id

    def collect_derived(self) -> CandidateRecord:
        candidate = self.snapshot(source="derived")
        self._record(candidate)
        return candidate

    def collect_declared(
        self,
        *,
        head_commit: str,
        base_main: str = "",
        changed_paths: Sequence[str] = (),
    ) -> CandidateRecord:
        candidate = self.snapshot(source="worker_command")
        mismatches: dict[str, object] = {}
        if head_commit != candidate.head_commit:
            mismatches["head_commit"] = {
                "declared": head_commit,
                "observed": candidate.head_commit,
            }
        if base_main and base_main != candidate.base_main:
            mismatches["base_main"] = {
                "declared": base_main,
                "observed": candidate.base_main,
            }
        if changed_paths:
            declared_paths = tuple(sorted(set(changed_paths)))
            if declared_paths != candidate.changed_paths:
                mismatches["changed_paths"] = {
                    "declared": list(declared_paths),
                    "observed": list(candidate.changed_paths),
                }
        if mismatches:
            raise CandidateCollectionError(
                "candidate_declaration_mismatch",
                "candidate declaration does not match the claimed workspace",
                details={"mismatched_fields": sorted(mismatches)},
            )
        self._record(candidate)
        return candidate

    def snapshot(self, *, source: str) -> CandidateRecord:
        observed_branch = self._git_text("branch", "--show-current")
        if observed_branch != self.branch:
            raise CandidateCollectionError(
                "candidate_branch_mismatch",
                "candidate workspace branch does not match the active claim",
                details={"expected": self.branch, "observed": observed_branch},
            )
        head_commit = self._git_text("rev-parse", "--verify", "HEAD")
        resolved_base = self._git_text("rev-parse", "--verify", self.base_main)
        if (
            self._git_result(
                "merge-base", "--is-ancestor", resolved_base, head_commit
            ).returncode
            != 0
        ):
            raise CandidateCollectionError(
                "candidate_base_mismatch",
                "candidate head does not descend from the recorded base main",
                details={"base_main": resolved_base, "head_commit": head_commit},
            )
        tracked_status = self._git_text(
            "status", "--porcelain=v1", "--untracked-files=no"
        )
        if tracked_status:
            raise CandidateCollectionError(
                "candidate_tracked_changes",
                "candidate workspace has uncommitted tracked changes",
                details={"status": tracked_status.splitlines()[:20]},
            )
        changed = self._git_bytes(
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            "-z",
            f"{resolved_base}...{head_commit}",
        )
        changed_paths = tuple(
            sorted(
                path.decode("utf-8", "surrogateescape")
                for path in changed.split(b"\0")
                if path
            )
        )
        return CandidateRecord(
            branch=self.branch,
            worktree=self.worktree,
            base_main=resolved_base,
            head_commit=head_commit,
            changed_paths=changed_paths,
            source=source,
        )

    def matches(self, candidate: CandidateRecord) -> bool:
        try:
            return (
                self.snapshot(source=candidate.source).fingerprint
                == candidate.fingerprint
            )
        except CandidateCollectionError:
            return False

    def matches_during_gate(self, candidate: CandidateRecord) -> bool:
        try:
            if self._git_text("rev-parse", "--verify", "HEAD") != candidate.head_commit:
                return False
            return not self._git_text(
                "status", "--porcelain=v1", "--untracked-files=no"
            )
        except CandidateCollectionError:
            return False

    def tracked_state_marker(self) -> str:
        digest = hashlib.sha256()
        for raw_path in self._git_bytes("ls-files", "-z").split(b"\0"):
            if not raw_path:
                continue
            relative = raw_path.decode("utf-8", "surrogateescape")
            path = self.worktree / relative
            digest.update(raw_path)
            digest.update(b"\0")
            try:
                stat = path.lstat()
            except OSError as exc:
                digest.update(f"missing:{type(exc).__name__}".encode())
            else:
                digest.update(
                    (
                        f"{stat.st_mode}:{stat.st_ino}:{stat.st_size}:"
                        f"{stat.st_mtime_ns}:{stat.st_ctime_ns}"
                    ).encode()
                )
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def is_recorded(self, candidate: CandidateRecord) -> bool:
        for record in self.run_store.read_records():
            if (
                record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
            ):
                continue
            recorded = CandidateRecord.from_record(record)
            if (
                recorded is not None
                and self._belongs_to_claim(recorded)
                and recorded.fingerprint == candidate.fingerprint
            ):
                return True
        return False

    def ensure_recorded(self, candidate: CandidateRecord) -> None:
        if not self.matches(candidate):
            raise CandidateCollectionError(
                "candidate_changed",
                "candidate no longer matches the claimed workspace",
            )
        if not self.is_recorded(candidate):
            self._record(candidate)

    def latest_recorded(self) -> CandidateRecord | None:
        for record in reversed(self.run_store.read_records()):
            if (
                record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
            ):
                continue
            candidate = CandidateRecord.from_record(record)
            if candidate is not None and self._belongs_to_claim(candidate):
                return candidate
        return None

    def _belongs_to_claim(self, candidate: CandidateRecord) -> bool:
        return (
            candidate.branch == self.branch
            and candidate.worktree.resolve() == self.worktree
        )

    def _record(self, candidate: CandidateRecord) -> None:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.candidate_recorded(
                run_id=self.run_id,
                task_id=self.task_id,
                payload=candidate.to_payload(),
            )
        )

    def _git_text(self, *args: str) -> str:
        result = self._git_result(*args)
        if result.returncode != 0:
            raise CandidateCollectionError(
                "candidate_git_error",
                "candidate Git state could not be read",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )
        return result.stdout.strip()

    def _git_bytes(self, *args: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise CandidateCollectionError(
                "candidate_git_error",
                "candidate Git state could not be read",
                details={"error": str(exc)},
            ) from exc
        if result.returncode != 0:
            raise CandidateCollectionError(
                "candidate_git_error",
                "candidate Git state could not be read",
                details={
                    "git_args": list(args),
                    "stderr": result.stderr.decode("utf-8", "replace").strip(),
                },
            )
        return result.stdout

    def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise CandidateCollectionError(
                "candidate_git_error",
                "candidate Git state could not be read",
                details={"error": str(exc)},
            ) from exc


@dataclasses.dataclass(frozen=True)
class GateResult:
    config_key: str
    exit_class: str
    exit_code: int | None
    duration_seconds: float
    log_reference: str
    evidence_digest: str
    candidate_fingerprint: str
    resumed: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_class == "passed"

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "gate_id": self.config_key,
            "command_key": self.config_key,
            "exit_class": self.exit_class,
            "duration_seconds": self.duration_seconds,
            "log_reference": self.log_reference,
            "evidence_digest": self.evidence_digest,
            "candidate_fingerprint": self.candidate_fingerprint,
        }
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        return payload

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> GateResult | None:
        if record.get("record_type") != "gate_result":
            return None
        config_key = record.get("command_key")
        exit_class = record.get("exit_class")
        duration = record.get("duration_seconds")
        log_reference = record.get("log_reference")
        evidence_digest = record.get("evidence_digest")
        fingerprint = record.get("candidate_fingerprint")
        exit_code = record.get("exit_code")
        if (
            not isinstance(config_key, str)
            or exit_class not in GATE_EXIT_CLASSES
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not isinstance(log_reference, str)
            or not isinstance(evidence_digest, str)
            or not isinstance(fingerprint, str)
            or isinstance(exit_code, bool)
            or (exit_code is not None and not isinstance(exit_code, int))
        ):
            return None
        return cls(
            config_key=config_key,
            exit_class=str(exit_class),
            exit_code=exit_code,
            duration_seconds=float(duration),
            log_reference=log_reference,
            evidence_digest=evidence_digest,
            candidate_fingerprint=fingerprint,
            resumed=True,
        )


@dataclasses.dataclass(frozen=True)
class GateRunSummary:
    candidate: CandidateRecord
    results: tuple[GateResult, ...]
    candidate_recorded: bool

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failed_gate_keys(self) -> tuple[str, ...]:
        return tuple(result.config_key for result in self.results if not result.passed)

    def require_review_ready(self) -> None:
        if (
            not self.candidate_recorded
            or not self.passed
            or any(
                result.candidate_fingerprint != self.candidate.fingerprint
                for result in self.results
            )
        ):
            raise GateExecutionError(
                "review requires a recorded candidate and passing gate evidence "
                "for the exact candidate fingerprint"
            )


@dataclasses.dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    severity: str
    summary: str
    evidence: str
    files: tuple[str, ...]
    lines: tuple[str, ...] = ()
    state: str = "open"

    @classmethod
    def from_payload(cls, value: object) -> ReviewFinding:
        if not isinstance(value, Mapping):
            raise ReviewExecutionError("review finding must be a JSON object")
        finding_id = value.get("id")
        severity = value.get("severity")
        summary = value.get("summary")
        evidence = value.get("evidence")
        files = value.get("files")
        lines = value.get("lines", [])
        state = value.get("state", "open")
        if not isinstance(finding_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", finding_id
        ):
            raise ReviewExecutionError("review finding id is invalid")
        if severity not in FINDING_SEVERITIES:
            raise ReviewExecutionError(
                "review finding severity must be one of: "
                + ", ".join(FINDING_SEVERITIES)
            )
        if not isinstance(summary, str) or not summary.strip():
            raise ReviewExecutionError("review finding summary is required")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ReviewExecutionError("review finding evidence is required")
        if not isinstance(files, list) or not all(
            isinstance(path, str) and path for path in files
        ):
            raise ReviewExecutionError("review finding files must be a JSON array")
        if not isinstance(lines, list) or not all(
            isinstance(line, str) and line for line in lines
        ):
            raise ReviewExecutionError("review finding lines must be a JSON array")
        if state not in FINDING_STATES:
            raise ReviewExecutionError(
                "review finding state must be one of: " + ", ".join(FINDING_STATES)
            )
        return cls(
            finding_id=finding_id,
            severity=str(severity),
            summary=summary.strip(),
            evidence=evidence.strip(),
            files=tuple(files),
            lines=tuple(lines),
            state=str(state),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.finding_id,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
            "files": list(self.files),
            "lines": list(self.lines),
            "state": self.state,
        }


@dataclasses.dataclass(frozen=True)
class ReviewRequest:
    run_id: str
    task_id: str
    candidate: CandidateRecord
    gate_results: tuple[GateResult, ...]
    policy_references: tuple[str, ...]
    pass_kind: str = "initial"
    prior_findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.pass_kind != "initial"
            and re.fullmatch(r"closure:[1-9][0-9]*", self.pass_kind) is None
        ):
            raise ValueError("review pass kind must be initial or closure:<ordinal>")
        if self.pass_kind == "initial" and self.prior_findings:
            raise ValueError("initial review cannot carry prior findings")
        if self.pass_kind != "initial" and not self.prior_findings:
            raise ValueError("closure review requires prior findings")

    @property
    def phase(self) -> str:
        return "initial_review" if self.pass_kind == "initial" else "targeted_closure"

    @property
    def family(self) -> str:
        return "initial" if self.pass_kind == "initial" else "closure"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "candidate": {
                **self.candidate.to_payload(),
                "diff_source": (
                    f"git diff {self.candidate.base_main}..."
                    f"{self.candidate.head_commit}"
                ),
            },
            "gate_evidence": [result.to_payload() for result in self.gate_results],
            "policy_references": list(self.policy_references),
            "pass_kind": self.pass_kind,
            "prior_findings": [finding.to_payload() for finding in self.prior_findings],
        }


@dataclasses.dataclass(frozen=True)
class ContinuationContext:
    session_id: str = ""
    session_id_source: str = ""
    prior_session_id: str = ""
    continuation_ordinal: int = 0
    fallback_reason: str = ""
    resumed: bool = False

    def __post_init__(self) -> None:
        if (
            self.fallback_reason
            and self.fallback_reason not in CONTINUATION_FALLBACK_REASONS
        ):
            raise ValueError("invalid continuation fallback reason")
        if self.continuation_ordinal < 0:
            raise ValueError("continuation ordinal must be non-negative")


def inject_claude_session(command: str, session_id: str, *, resume: bool) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id) is None:
        raise ReviewExecutionError("runtime continuation session id is invalid")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ReviewExecutionError("reviewer command cannot be parsed") from exc
    executable = next(
        (
            Path(token).name
            for token in argv
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) is None
        ),
        "",
    )
    if executable != "claude":
        raise ReviewExecutionError("Claude session injection requires a claude command")
    if any(
        token in {"--resume", "-r", "--continue", "-c", "--session-id"}
        or token.startswith(("--resume=", "--session-id="))
        for token in argv
    ):
        raise ReviewExecutionError("reviewer command already controls session identity")
    option = "--resume" if resume else "--session-id"
    insertion = f"{option} {session_id}"
    if "{prompt}" in command:
        return command.replace("{prompt}", f"{insertion} {{prompt}}", 1)
    return f"{command.rstrip()} {insertion}"


def plan_session_continuation(
    *,
    provider: str,
    role: str,
    continuing: bool,
    prior_session_id: str = "",
    prior_ordinal: int = 0,
    availability_reason: str = "",
    session_id_factory: Callable[[], str] | None = None,
) -> ContinuationContext:
    capabilities = provider_capabilities(provider, role)
    factory = session_id_factory or (lambda: str(uuid.uuid4()))
    if not continuing:
        if capabilities.session_injection:
            return ContinuationContext(
                session_id=factory(), session_id_source="runtime_injected"
            )
        return ContinuationContext(
            session_id=factory(), session_id_source="runtime_launch"
        )
    if availability_reason and availability_reason not in CONTINUATION_FALLBACK_REASONS:
        raise ValueError("invalid continuation availability reason")
    reason = availability_reason
    if not prior_session_id:
        reason = "transcript_missing"
    elif not capabilities.resume:
        reason = "provider_unsupported"
    if reason:
        fresh_session = factory()
        return ContinuationContext(
            session_id=fresh_session,
            session_id_source=(
                "runtime_injected"
                if capabilities.session_injection
                else "runtime_launch"
            ),
            prior_session_id=prior_session_id,
            continuation_ordinal=prior_ordinal + 1,
            fallback_reason=reason,
        )
    return ContinuationContext(
        session_id=prior_session_id,
        session_id_source="runtime_resumed",
        prior_session_id=prior_session_id,
        continuation_ordinal=prior_ordinal + 1,
        resumed=True,
    )


def inject_provider_continuation(
    command: str,
    *,
    provider: str,
    role: str,
    continuation: ContinuationContext,
) -> str:
    capabilities = provider_capabilities(provider, role)
    if (
        not continuation.session_id
        or not capabilities.session_injection
        and not continuation.resumed
    ):
        return command
    if provider == "claude":
        return inject_claude_session(
            command, continuation.session_id, resume=continuation.resumed
        )
    if provider == "codex" and role == "implementer" and continuation.resumed:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", continuation.session_id)
            is None
        ):
            raise ReviewExecutionError("runtime continuation session id is invalid")
        if "{prompt}" not in command:
            raise ReviewExecutionError("Codex continuation requires {prompt}")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ReviewExecutionError("implementer command cannot be parsed") from exc
        if "exec" not in argv or "resume" in argv:
            raise ReviewExecutionError(
                "Codex implementer continuation requires an unresumed exec command"
            )
        return re.sub(
            r"(?<!\S)exec(?=\s|$)",
            f"exec resume {continuation.session_id}",
            command,
            count=1,
        )
    return command


def prepare_claude_review_command(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ReviewExecutionError("reviewer command cannot be parsed") from exc
    prompt_index = next(
        (index for index, token in enumerate(argv) if token == "{prompt}"), len(argv)
    )

    output_format = ""
    for index, token in enumerate(argv):
        if token.startswith("--output-format="):
            output_format = token.partition("=")[2]
            break
        if token == "--output-format" and index + 1 < len(argv):
            output_format = argv[index + 1]
            break
    if output_format and output_format != "stream-json":
        raise ReviewExecutionError(
            "Claude reviewer structured output requires stream-json"
        )
    additions: list[str] = []
    if not output_format:
        additions.extend(("--output-format", "stream-json"))
    if "--verbose" not in argv:
        additions.append("--verbose")
    if additions:
        argv[prompt_index:prompt_index] = additions

    disallowed = [
        index
        for index, token in enumerate(argv)
        if token in {"--disallowedTools", "--disallowed-tools"}
        or token.startswith(("--disallowedTools=", "--disallowed-tools="))
    ]
    if len(disallowed) > 1:
        raise ReviewExecutionError(
            "Claude reviewer command may specify disallowed tools only once"
        )
    existing_denials: list[str] = []
    if disallowed:
        index = disallowed[0]
        token = argv.pop(index)
        if "=" in token:
            _, _, values = token.partition("=")
            if values:
                existing_denials.append(values)
        else:
            while index < len(argv):
                value = argv[index]
                if value == "{prompt}" or value.startswith("-"):
                    break
                existing_denials.append(argv.pop(index))
    argv.extend(("--disallowedTools", "Agent,Task", *existing_denials))

    prepared = shlex.join(argv)
    return prepared.replace(shlex.quote("{prompt}"), "{prompt}")


@dataclasses.dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: tuple[ReviewFinding, ...]
    session_id: str
    session_id_source: str
    continuation_ordinal: int
    retry_classification: str
    usage: ProviderUsage
    duration_seconds: float
    pass_kind: str
    pass_ordinal: int
    attempt_ordinal: int
    continuation_resumed: bool = False
    nested_launches: int = 0

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"


class ReviewConcurrencyBudget:
    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("reviewer concurrency budget must be positive")
        self.limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)
        self._state_lock = threading.Lock()
        self._active = 0
        self._peak = 0

    @property
    def active(self) -> int:
        with self._state_lock:
            return self._active

    @property
    def peak(self) -> int:
        with self._state_lock:
            return self._peak

    @contextmanager
    def slot(self):
        self._semaphore.acquire()
        with self._state_lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            yield
        finally:
            with self._state_lock:
                self._active -= 1
            self._semaphore.release()


class FindingsLedger:
    def __init__(self, run_store: object, run_id: str, task_id: str) -> None:
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id

    def current(self, candidate_fingerprint: str = "") -> tuple[ReviewFinding, ...]:
        findings: dict[str, ReviewFinding] = {}
        for record in self.run_store.read_records():
            if (
                record.get("record_type") != "finding_recorded"
                or record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
                or (
                    candidate_fingerprint
                    and record.get("candidate_fingerprint") != candidate_fingerprint
                )
            ):
                continue
            payload = {
                "id": record.get("finding_id"),
                "severity": record.get("severity"),
                "summary": record.get("summary"),
                "evidence": record.get("evidence"),
                "files": record.get("files"),
                "lines": record.get("lines", []),
                "state": record.get("state"),
            }
            try:
                finding = ReviewFinding.from_payload(payload)
            except ReviewExecutionError:
                continue
            findings[finding.finding_id] = finding
        return tuple(findings.values())

    def open(self, candidate_fingerprint: str = "") -> tuple[ReviewFinding, ...]:
        return tuple(
            finding
            for finding in self.current(candidate_fingerprint)
            if finding.state == "open"
        )


ReviewExecutor = Callable[..., subprocess.CompletedProcess[str]]
ContinuationAvailability = Callable[[str, str, str], str]


class ReviewRouter:
    def __init__(
        self,
        *,
        reviewer: AgentConfig,
        reviewer_profile: str,
        run_store: object,
        run_id: str,
        task_id: str,
        worktree: Path,
        policy_references: Sequence[str],
        max_initial_passes: int,
        max_closure_passes: int,
        concurrency: ReviewConcurrencyBudget,
        stage_machine: RunLifecycleStateMachine | None = None,
        limit_wall_patterns: Sequence[str] | None = None,
        executor: ReviewExecutor = subprocess.run,
        continuation_availability: ContinuationAvailability | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_initial_passes <= 0:
            raise ValueError("max_initial_passes must be positive")
        if max_closure_passes < 0:
            raise ValueError("max_closure_passes must be non-negative")
        self.reviewer = reviewer
        self.reviewer_profile = reviewer_profile
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id
        self.worktree = worktree
        self.policy_references = tuple(policy_references)
        self.max_initial_passes = max_initial_passes
        self.max_closure_passes = max_closure_passes
        self.concurrency = concurrency
        self.stage_machine = stage_machine
        self.limit_wall_patterns = limit_wall_patterns
        self.executor = executor
        self.continuation_availability = continuation_availability
        self.session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))
        self.ledger = FindingsLedger(run_store, run_id, task_id)

    def review(
        self,
        gate_summary: GateRunSummary,
        *,
        pass_kind: str = "initial",
        prior_findings: Sequence[ReviewFinding] | None = None,
    ) -> ReviewResult:
        gate_summary.require_review_ready()
        candidate = gate_summary.candidate
        if prior_findings is None:
            prior = self.ledger.open() if pass_kind.startswith("closure:") else ()
        else:
            prior = tuple(prior_findings)
        request = ReviewRequest(
            run_id=self.run_id,
            task_id=self.task_id,
            candidate=candidate,
            gate_results=gate_summary.results,
            policy_references=self.policy_references,
            pass_kind=pass_kind,
            prior_findings=prior,
        )
        command_template = self.reviewer.require_reviewer_command()
        if not command_template_uses_field(command_template, "prompt"):
            raise AgentResolutionError(
                "reviewer command must include {prompt}; otherwise the typed "
                "review request cannot be delivered"
            )
        pass_ordinal = self._next_pass_ordinal(request)
        limit = (
            self.max_initial_passes
            if request.family == "initial"
            else self.max_closure_passes
        )
        self._transition_to_review(request)
        malformed: ReviewExecutionError | None = None
        continuation = self._continuation_context(request)
        for attempt_ordinal in (1, 2):
            try:
                try:
                    result = self._launch(
                        request,
                        pass_ordinal=pass_ordinal,
                        attempt_ordinal=attempt_ordinal,
                        reask=attempt_ordinal == 2,
                        continuation=continuation,
                    )
                except ReviewSessionExpired:
                    continuation = plan_session_continuation(
                        provider=str(self._route_payload()["provider"]),
                        role="reviewer",
                        continuing=True,
                        prior_session_id=(
                            continuation.prior_session_id or continuation.session_id
                        ),
                        prior_ordinal=max(0, continuation.continuation_ordinal - 1),
                        availability_reason="session_expired",
                        session_id_factory=self.session_id_factory,
                    )
                    result = self._launch(
                        request,
                        pass_ordinal=pass_ordinal,
                        attempt_ordinal=attempt_ordinal,
                        reask=attempt_ordinal == 2,
                        continuation=continuation,
                    )
            except ReviewExecutionError as exc:
                malformed = exc
                if attempt_ordinal == 1 and str(exc).startswith("malformed review"):
                    continuation = self._continuation_context(
                        request, previous=continuation
                    )
                    continue
                if str(exc).startswith("malformed review"):
                    self._fail_stage_for_result("fatal")
                raise
            pass_ordinal = result.pass_ordinal
            self._record_findings(request, result.findings)
            self._record_budget(
                request,
                action="consumed",
                pass_ordinal=pass_ordinal,
                limit=limit,
            )
            self._transition_from_review(result)
            return result
        assert malformed is not None
        raise malformed

    def _launch(
        self,
        request: ReviewRequest,
        *,
        pass_ordinal: int,
        attempt_ordinal: int,
        reask: bool,
        continuation: ContinuationContext,
    ) -> ReviewResult:
        command_template = self.reviewer.require_reviewer_command()
        if not command_template_uses_field(command_template, "prompt"):
            raise AgentResolutionError(
                "reviewer command must include {prompt}; otherwise the typed "
                "review request cannot be delivered"
            )
        effective_template = self._continuation_command(command_template, continuation)
        prompt = self._prompt(request, reask=reask, continuation=continuation)
        command = format_agent_command(
            effective_template,
            prompt=prompt,
            model=self.reviewer.model,
            effort=self.reviewer.effort,
            profile=self.reviewer_profile,
        )
        route = self._route_payload()
        pass_ordinal = self._claim_review_attempt(
            request,
            pass_ordinal=pass_ordinal,
            attempt_ordinal=attempt_ordinal,
            route=route,
            continuation=continuation,
        )
        started = time.monotonic()
        try:
            with self.concurrency.slot():
                completed = self.executor(
                    command,
                    cwd=self.worktree,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
        except OSError as exc:
            duration = max(0.0, time.monotonic() - started)
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "fatal",
                duration,
                unavailable_usage(
                    self._usage_provider(), "provider_usage_not_reported"
                ),
                continuation=continuation,
            )
            self._fail_stage_for_result("fatal")
            raise ReviewExecutionError(
                f"reviewer command could not be executed: {type(exc).__name__}"
            ) from exc
        except ReviewExecutionError:
            duration = max(0.0, time.monotonic() - started)
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "fatal",
                duration,
                unavailable_usage(
                    self._usage_provider(), "provider_usage_not_reported"
                ),
                continuation=continuation,
            )
            self._fail_stage_for_result("fatal")
            raise
        except subprocess.TimeoutExpired as exc:
            self._record_wait_incomplete(
                request, pass_ordinal, attempt_ordinal, continuation
            )
            self._fail_stage_for_result("timeout")
            raise ReviewWaitIncomplete(
                request.pass_kind, pass_ordinal, attempt_ordinal
            ) from exc
        duration = max(0.0, time.monotonic() - started)
        output = completed.stdout or ""
        observer = ProviderUsageObserver(self._usage_provider())
        for line in output.splitlines():
            observer.observe_line(line)
        usage = observer.usage
        nested_launches, nested_usage = self._nested_launch_evidence(output)
        if nested_launches:
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "fatal",
                duration,
                usage,
                continuation=continuation,
                nested_launches=nested_launches,
                nested_usage=nested_usage,
                policy_violation="nested_reviewer_delegation",
            )
            self._fail_stage_for_result("fatal")
            raise ReviewDelegationPolicyError(nested_launches)
        wall = detect_limit_wall(output, self.limit_wall_patterns)
        if wall is not None:
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "limit_wall",
                duration,
                usage,
                continuation=continuation,
            )
            self._fail_stage_for_result("limit_wall")
            raise ReviewLimitWallError(
                wall,
                route=str(route["command_key"]),
                phase=request.phase,
            )
        if completed.returncode != 0:
            session_expired = continuation.resumed and any(
                marker in output.casefold()
                for marker in (
                    "no conversation found",
                    "conversation not found",
                    "session expired",
                )
            )
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "fatal",
                duration,
                usage,
                continuation=continuation,
            )
            if session_expired:
                raise ReviewSessionExpired("reviewer session expired")
            self._fail_stage_for_result("fatal")
            raise ReviewExecutionError(
                f"reviewer command failed with exit code {completed.returncode}"
            )
        try:
            result = self._parse_result(
                output,
                request=request,
                pass_ordinal=pass_ordinal,
                attempt_ordinal=attempt_ordinal,
                usage=usage,
                duration=duration,
                continuation=continuation,
            )
        except ReviewExecutionError:
            self._record_error(
                request,
                route,
                pass_ordinal,
                attempt_ordinal,
                "transient" if not reask else "fatal",
                duration,
                usage,
                continuation=continuation,
            )
            raise
        self._append_event(
            "review_verdict",
            self._result_payload(result, request, route),
        )
        if result.verdict == "error":
            self._fail_stage_for_result(result.retry_classification)
            if result.retry_classification == "limit_wall":
                raise ReviewLimitWallError(
                    LimitWallSignal(marker="reviewer reported limit wall"),
                    route=str(route["command_key"]),
                    phase=request.phase,
                )
            raise ReviewStageResultError(result.retry_classification)
        return result

    def _parse_result(
        self,
        output: str,
        *,
        request: ReviewRequest,
        pass_ordinal: int,
        attempt_ordinal: int,
        usage: ProviderUsage,
        duration: float,
        continuation: ContinuationContext,
    ) -> ReviewResult:
        payload: object | None = None
        for line in reversed(output.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, Mapping) and "verdict" in candidate:
                payload = candidate
                break
            if isinstance(candidate, Mapping) and candidate.get("type") == "result":
                result_text = candidate.get("result")
                if isinstance(result_text, str):
                    try:
                        result_payload = json.loads(result_text)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(result_payload, Mapping)
                        and "verdict" in result_payload
                    ):
                        payload = result_payload
                        break
        if not isinstance(payload, Mapping):
            raise ReviewExecutionError("malformed review output: missing JSON verdict")
        verdict = payload.get("verdict")
        raw_findings = payload.get("findings", [])
        if verdict not in REVIEW_VERDICTS or not isinstance(raw_findings, list):
            raise ReviewExecutionError(
                "malformed review output: invalid verdict schema"
            )
        try:
            findings = tuple(ReviewFinding.from_payload(item) for item in raw_findings)
        except ReviewExecutionError as exc:
            raise ReviewExecutionError(f"malformed review output: {exc}") from exc
        if request.family == "initial" and verdict == "approve" and findings:
            raise ReviewExecutionError(
                "malformed review output: approve verdict cannot include findings"
            )
        if verdict == "findings" and not findings:
            raise ReviewExecutionError(
                "malformed review output: findings verdict requires findings"
            )
        if request.family == "initial" and any(
            finding.state != "open" for finding in findings
        ):
            raise ReviewExecutionError(
                "malformed review output: initial findings must be open"
            )
        if request.family == "closure":
            prior_ids = {finding.finding_id for finding in request.prior_findings}
            result_ids = {finding.finding_id for finding in findings}
            if result_ids != prior_ids:
                raise ReviewExecutionError(
                    "malformed review output: closure must return every prior finding exactly once"
                )
            has_open = any(finding.state == "open" for finding in findings)
            if (verdict == "approve" and has_open) or (
                verdict == "findings" and not has_open
            ):
                raise ReviewExecutionError(
                    "malformed review output: closure verdict must match finding states"
                )
        reported_session_id = payload.get("session_id", "")
        reported_session_id_source = payload.get("session_id_source", "")
        reported_continuation_ordinal = payload.get("continuation_ordinal", 0)
        retry_classification = payload.get(
            "retry_classification", "fatal" if verdict == "error" else "ok"
        )
        if not isinstance(reported_session_id, str) or not isinstance(
            reported_session_id_source, str
        ):
            raise ReviewExecutionError(
                "malformed review output: invalid session identity"
            )
        if (
            isinstance(reported_continuation_ordinal, bool)
            or not isinstance(reported_continuation_ordinal, int)
            or reported_continuation_ordinal < 0
        ):
            raise ReviewExecutionError(
                "malformed review output: invalid continuation ordinal"
            )
        if retry_classification not in REVIEW_RETRY_CLASSIFICATIONS:
            raise ReviewExecutionError(
                "malformed review output: invalid retry classification"
            )
        if verdict != "error" and retry_classification != "ok":
            raise ReviewExecutionError(
                "malformed review output: non-error verdict must classify as ok"
            )
        if (
            continuation.session_id_source in {"runtime_injected", "runtime_resumed"}
            and continuation.session_id
            and reported_session_id
            and reported_session_id != continuation.session_id
        ):
            raise ReviewExecutionError(
                "malformed review output: session identity differs from runtime launch"
            )
        if (
            reported_continuation_ordinal
            and reported_continuation_ordinal != continuation.continuation_ordinal
        ):
            raise ReviewExecutionError(
                "malformed review output: continuation ordinal differs from runtime journal"
            )
        session_id = (
            reported_session_id
            if continuation.session_id_source == "runtime_launch"
            and reported_session_id
            else continuation.session_id or reported_session_id
        )
        session_id_source = (
            reported_session_id_source
            if continuation.session_id_source == "runtime_launch"
            and reported_session_id_source
            else continuation.session_id_source or reported_session_id_source
        )
        return ReviewResult(
            verdict=str(verdict),
            findings=findings,
            session_id=session_id,
            session_id_source=session_id_source,
            continuation_ordinal=continuation.continuation_ordinal,
            retry_classification=str(retry_classification),
            usage=usage,
            duration_seconds=duration,
            pass_kind=request.pass_kind,
            pass_ordinal=pass_ordinal,
            attempt_ordinal=attempt_ordinal,
            continuation_resumed=continuation.resumed,
        )

    def _prompt(
        self,
        request: ReviewRequest,
        *,
        reask: bool,
        continuation: ContinuationContext,
    ) -> str:
        instruction = (
            "The previous response was malformed. Return only one JSON object. "
            if reask
            else ""
        )
        return (
            instruction
            + "Review this candidate directly. Do not launch, delegate to, or "
            "invoke any subagent, nested model, Task, Agent, or Workflow. Return "
            "exactly one "
            "JSON object with verdict (approve|findings|error), findings, session_id, "
            "and session_id_source. The runtime owns continuation ordinals and "
            "review budgets; do not propose or reset either. Each finding requires id, "
            "severity (P0-P3), summary, evidence, files, lines, and state.\n"
            + json.dumps(
                {
                    **request.to_payload(),
                    "continuation": {
                        "session_id": continuation.session_id,
                        "session_id_source": continuation.session_id_source,
                        "prior_session_id": continuation.prior_session_id,
                        "ordinal": continuation.continuation_ordinal,
                        "resumed": continuation.resumed,
                        "fallback_reason": continuation.fallback_reason,
                    },
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )

    def _continuation_context(
        self,
        request: ReviewRequest,
        *,
        previous: ContinuationContext | None = None,
    ) -> ContinuationContext:
        route = self._route_payload()
        provider = str(route["provider"])
        if request.family == "initial" and previous is None:
            return plan_session_continuation(
                provider=provider,
                role="reviewer",
                continuing=False,
                session_id_factory=self.session_id_factory,
            )

        prior_session_id = ""
        prior_ordinal = 0
        if previous is not None:
            prior_session_id = previous.session_id
            prior_ordinal = previous.continuation_ordinal
        else:
            prior = self._latest_review_session(provider)
            if prior is not None:
                prior_session_id, prior_ordinal = prior
        fallback_reason = ""
        if prior_session_id:
            availability = (
                self.continuation_availability
                or self._default_continuation_availability
            )
            fallback_reason = availability(provider, "reviewer", prior_session_id)
            if fallback_reason not in ("", *CONTINUATION_FALLBACK_REASONS):
                raise ReviewExecutionError(
                    "continuation availability returned an invalid reason"
                )
        return plan_session_continuation(
            provider=provider,
            role="reviewer",
            continuing=True,
            prior_session_id=prior_session_id,
            prior_ordinal=prior_ordinal,
            availability_reason=fallback_reason,
            session_id_factory=self.session_id_factory,
        )

    def _default_continuation_availability(
        self, provider: str, role: str, session_id: str
    ) -> str:
        if provider != "claude" or role != "reviewer":
            return ""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id) is None:
            return "transcript_missing"
        try:
            argv = shlex.split(self.reviewer.require_reviewer_command())
        except ValueError:
            return "transcript_missing"
        configured_home = ""
        for token in argv:
            if token.startswith("CLAUDE_HOME="):
                configured_home = token.partition("=")[2]
                break
            if "=" not in token:
                break
        if not configured_home:
            configured_home = os.environ.get("CLAUDE_HOME", "")
        claude_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".claude"
        )
        if not claude_home.is_absolute():
            claude_home = self.worktree / claude_home
        try:
            return (
                ""
                if any(
                    candidate.is_file()
                    for candidate in (claude_home / "projects").glob(
                        f"*/{session_id}.jsonl"
                    )
                )
                else "transcript_missing"
            )
        except OSError:
            return "transcript_missing"

    def _latest_review_session(self, provider: str) -> tuple[str, int] | None:
        for record in reversed(self.run_store.read_records()):
            route = record.get("route")
            if (
                record.get("record_type") != "review_verdict"
                or record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
                or record.get("verdict") not in {"approve", "findings"}
                or not isinstance(route, Mapping)
                or route.get("provider") != provider
            ):
                continue
            session_id = record.get("session_id")
            ordinal = record.get("continuation_ordinal", 0)
            if (
                isinstance(session_id, str)
                and session_id
                and isinstance(ordinal, int)
                and not isinstance(ordinal, bool)
                and ordinal >= 0
            ):
                return session_id, ordinal
        return None

    def _continuation_command(
        self, command_template: str, continuation: ContinuationContext
    ) -> str:
        route = self._route_payload()
        provider = str(route["provider"])
        capabilities = provider_capabilities(provider, "reviewer")
        effective = inject_provider_continuation(
            command_template,
            provider=provider,
            role="reviewer",
            continuation=continuation,
        )
        if capabilities.nested_delegation_disable:
            effective = prepare_claude_review_command(effective)
        return effective

    def _claim_review_attempt(
        self,
        request: ReviewRequest,
        *,
        pass_ordinal: int,
        attempt_ordinal: int,
        route: Mapping[str, object],
        continuation: ContinuationContext,
    ) -> int:
        from vibe_loop.runs import RunLifecycleEvent

        claim = self.run_store.claim_review_attempt(
            start_record=RunLifecycleEvent.review_started(
                run_id=self.run_id,
                task_id=self.task_id,
                payload={
                    "pass_kind": request.pass_kind,
                    "pass_ordinal": pass_ordinal,
                    "attempt_ordinal": attempt_ordinal,
                    "candidate_fingerprint": request.candidate.fingerprint,
                    "phase": request.phase,
                    "route": dict(route),
                    "session_id": continuation.session_id,
                    "session_id_source": continuation.session_id_source,
                    "continuation_ordinal": continuation.continuation_ordinal,
                    "continuation_resumed": continuation.resumed,
                },
            ).to_record(),
            max_initial_passes=self.max_initial_passes,
            max_closure_passes=self.max_closure_passes,
            lineage_fingerprint=request.candidate.fingerprint,
            before_start_record=(
                RunLifecycleEvent.continuation_fallback(
                    run_id=self.run_id,
                    task_id=self.task_id,
                    payload=self._continuation_fallback_payload(request, continuation),
                ).to_record()
                if continuation.fallback_reason
                else None
            ),
        )
        status = claim.get("status")
        if status == "claimed":
            claimed_ordinal = claim.get("pass_ordinal")
            if isinstance(claimed_ordinal, int) and not isinstance(
                claimed_ordinal, bool
            ):
                return claimed_ordinal
            raise ReviewExecutionError("review attempt claim returned no ordinal")
        if status == "pending":
            pending = claim.get("record")
            record = pending if isinstance(pending, Mapping) else {}
            pending_ordinal = record.get("pass_ordinal")
            pending_attempt = record.get("attempt_ordinal")
            resolved_ordinal = (
                pending_ordinal
                if isinstance(pending_ordinal, int)
                and not isinstance(pending_ordinal, bool)
                else pass_ordinal
            )
            resolved_attempt = (
                pending_attempt
                if isinstance(pending_attempt, int)
                and not isinstance(pending_attempt, bool)
                else attempt_ordinal
            )
            self._record_wait_incomplete(
                request,
                resolved_ordinal,
                resolved_attempt,
                self._continuation_from_start_record(record),
            )
            raise ReviewWaitIncomplete(
                request.pass_kind, resolved_ordinal, resolved_attempt
            )
        if status == "exhausted":
            limit = claim.get("limit")
            exhausted_limit = limit if isinstance(limit, int) else 0
            if self.stage_machine is not None:
                self.stage_machine.fail(
                    StageFailure.STAGE_FAILED,
                    reason=(
                        f"review_budget_exhausted:{request.family}:"
                        f"limit={exhausted_limit}"
                    ),
                )
            raise ReviewBudgetExhausted(request.family, exhausted_limit)
        raise ReviewExecutionError("review attempt claim returned invalid status")

    def _continuation_from_start_record(
        self, record: Mapping[str, object]
    ) -> ContinuationContext:
        session_id = record.get("session_id")
        session_id_source = record.get("session_id_source")
        continuation_ordinal = record.get("continuation_ordinal")
        resumed = record.get("continuation_resumed", False)
        return ContinuationContext(
            session_id=session_id if isinstance(session_id, str) else "",
            session_id_source=(
                session_id_source
                if isinstance(session_id_source, str)
                else "unavailable"
            ),
            continuation_ordinal=(
                continuation_ordinal
                if isinstance(continuation_ordinal, int)
                and not isinstance(continuation_ordinal, bool)
                and continuation_ordinal >= 0
                else 0
            ),
            resumed=resumed if isinstance(resumed, bool) else False,
        )

    def _record_budget(
        self,
        request: ReviewRequest,
        *,
        action: str,
        pass_ordinal: int,
        limit: int,
    ) -> None:
        self._append_event(
            "review_budget",
            {
                "action": action,
                "family": request.family,
                "pass_kind": request.pass_kind,
                "pass_ordinal": pass_ordinal,
                "limit": limit,
                "candidate_fingerprint": request.candidate.fingerprint,
            },
        )

    def _record_wait_incomplete(
        self,
        request: ReviewRequest,
        pass_ordinal: int,
        attempt_ordinal: int,
        continuation: ContinuationContext,
    ) -> None:
        self._append_event(
            "review_wait_incomplete",
            {
                "pass_kind": request.pass_kind,
                "pass_ordinal": pass_ordinal,
                "attempt_ordinal": attempt_ordinal,
                "candidate_fingerprint": request.candidate.fingerprint,
                "session_id": continuation.session_id,
                "session_id_source": continuation.session_id_source,
                "continuation_ordinal": continuation.continuation_ordinal,
                "reason": "verdict_not_recorded",
            },
        )

    def _continuation_fallback_payload(
        self, request: ReviewRequest, continuation: ContinuationContext
    ) -> dict[str, object]:
        return {
            "role": "reviewer",
            "pass_kind": request.pass_kind,
            "candidate_fingerprint": request.candidate.fingerprint,
            "reason": continuation.fallback_reason,
            "prior_session_id": continuation.prior_session_id,
            "session_id": continuation.session_id,
            "session_id_source": continuation.session_id_source,
            "continuation_ordinal": continuation.continuation_ordinal,
            "context_artifacts": {
                "findings": [
                    finding.to_payload() for finding in request.prior_findings
                ],
                "gate_evidence": [
                    result.to_payload() for result in request.gate_results
                ],
            },
        }

    def _nested_launch_evidence(
        self, output: str
    ) -> tuple[int, dict[str, int | float]]:
        count = 0
        usage: dict[str, int | float] = {}
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            event_type = event.get("type")
            nested = event.get("item")
            nested_type = nested.get("type") if isinstance(nested, Mapping) else None
            tool_names: list[str] = []
            if isinstance(nested, Mapping):
                for key in ("name", "tool_name", "server", "method"):
                    value = nested.get(key)
                    if isinstance(value, str):
                        tool_names.append(value)
            message = event.get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, Mapping):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name")
                        if isinstance(name, str):
                            tool_names.append(name)
            nested_tool = any(self._is_nested_review_tool(name) for name in tool_names)
            if (
                event_type not in NESTED_REVIEW_EVENT_TYPES
                and nested_type not in {"agent", "subagent", "task", "workflow"}
                and not nested_tool
            ):
                continue
            count += 1
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, Mapping) and isinstance(message, Mapping):
                raw_usage = message.get("usage")
            if isinstance(raw_usage, Mapping):
                for key, value in raw_usage.items():
                    if (
                        isinstance(key, str)
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and value >= 0
                    ):
                        usage[key] = usage.get(key, 0) + value
        return count, usage

    def _is_nested_review_tool(self, name: str) -> bool:
        normalized = name.casefold().replace("-", "_")
        parts = {part for part in re.split(r"[.:/]", normalized) if part}
        return bool(parts & NESTED_REVIEW_TOOL_NAMES) or any(
            marker in normalized
            for marker in ("spawn_agent", "delegate_agent", "delegate_to_agent")
        )

    def _next_pass_ordinal(self, request: ReviewRequest) -> int:
        count = 0
        for record in self.run_store.read_records():
            if (
                record.get("record_type") == "review_verdict"
                and record.get("run_id") == self.run_id
                and record.get("task_id") == self.task_id
                and record.get("verdict") in {"approve", "findings"}
            ):
                recorded_kind = record.get("pass_kind")
                family = (
                    "initial"
                    if recorded_kind == "initial"
                    else "closure"
                    if isinstance(recorded_kind, str)
                    and recorded_kind.startswith("closure:")
                    else ""
                )
                if family == request.family:
                    count += 1
        return count + 1

    def _record_findings(
        self,
        request: ReviewRequest,
        findings: Sequence[ReviewFinding],
    ) -> None:
        for finding in findings:
            self._append_event(
                "finding_recorded",
                {
                    "finding_id": finding.finding_id,
                    "severity": finding.severity,
                    "summary": finding.summary,
                    "evidence": finding.evidence,
                    "files": list(finding.files),
                    "lines": list(finding.lines),
                    "state": finding.state,
                    "candidate_fingerprint": request.candidate.fingerprint,
                    "pass_kind": request.pass_kind,
                },
            )

    def _record_error(
        self,
        request: ReviewRequest,
        route: Mapping[str, object],
        pass_ordinal: int,
        attempt_ordinal: int,
        retry_classification: str,
        duration: float,
        usage: ProviderUsage,
        *,
        continuation: ContinuationContext | None = None,
        nested_launches: int = 0,
        nested_usage: Mapping[str, int | float] | None = None,
        policy_violation: str = "",
    ) -> None:
        context = continuation or ContinuationContext()
        result = ReviewResult(
            verdict="error",
            findings=(),
            session_id=context.session_id,
            session_id_source=context.session_id_source,
            continuation_ordinal=context.continuation_ordinal,
            retry_classification=retry_classification,
            usage=usage,
            duration_seconds=duration,
            pass_kind=request.pass_kind,
            pass_ordinal=pass_ordinal,
            attempt_ordinal=attempt_ordinal,
            continuation_resumed=context.resumed,
            nested_launches=nested_launches,
        )
        payload = self._result_payload(result, request, route)
        if nested_usage:
            payload["nested_usage"] = dict(nested_usage)
        if policy_violation:
            payload["policy_violation"] = policy_violation
        self._append_event(
            "review_verdict",
            payload,
        )

    def _result_payload(
        self,
        result: ReviewResult,
        request: ReviewRequest,
        route: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "pass_kind": result.pass_kind,
            "pass_ordinal": result.pass_ordinal,
            "attempt_ordinal": result.attempt_ordinal,
            "candidate_fingerprint": request.candidate.fingerprint,
            "verdict": result.verdict,
            "findings_count": len(result.findings),
            "session_id": result.session_id,
            "session_id_source": result.session_id_source,
            "continuation_ordinal": result.continuation_ordinal,
            "continuation_resumed": result.continuation_resumed,
            "retry_classification": result.retry_classification,
            "nested_launches": result.nested_launches,
            "duration_seconds": result.duration_seconds,
            "phase": request.phase,
            "route": dict(route),
            "stats": result.usage.to_stats(
                phase=request.phase,
                wall_time_seconds=result.duration_seconds,
                candidate_fingerprint=request.candidate.fingerprint,
                continuation=result.continuation_resumed,
                work_kind="review",
            ),
        }

    def _route_payload(self) -> dict[str, object]:
        provider = agent_command_provider(
            self.reviewer.command or "",
            self.reviewer.executable_kind or self.reviewer.agent_kind,
        )
        payload: dict[str, object] = {
            "profile": self.reviewer_profile,
            "provider": provider or "unknown",
            "model": self.reviewer.model,
            "model_source": self.reviewer.model_source,
            "effort": self.reviewer.effort,
            "effort_source": self.reviewer.effort_source,
            "command_key": (
                f"agent.profiles.{self.reviewer_profile}.command"
                if self.reviewer_profile
                else "agent.command"
            ),
        }
        return payload

    def _usage_provider(self) -> str:
        provider = self._route_payload()["provider"]
        return {"codex": "openai", "claude": "anthropic"}.get(str(provider), "unknown")

    def _transition_to_review(self, request: ReviewRequest) -> None:
        if self.stage_machine is None:
            return
        stage = RunStage.REVIEW if request.family == "initial" else RunStage.CLOSURE
        if self.stage_machine.stage is stage:
            return
        self.stage_machine.transition(
            stage, reason=f"review_started:{request.pass_kind}"
        )

    def _transition_from_review(self, result: ReviewResult) -> None:
        if self.stage_machine is None:
            return
        destination = (
            RunStage.INTEGRATION
            if result.approved and not self.ledger.open()
            else RunStage.REMEDIATION
        )
        self.stage_machine.transition(
            destination,
            reason=f"review_verdict:{result.verdict}",
        )

    def _fail_stage_for_result(self, retry_classification: str) -> None:
        if self.stage_machine is None:
            return
        failure = {
            "limit_wall": StageFailure.LIMIT_WALL,
            "timeout": StageFailure.TIMED_OUT,
        }.get(retry_classification, StageFailure.STAGE_FAILED)
        self.stage_machine.fail(
            failure,
            reason=f"reviewer_error:{retry_classification}",
        )

    def _append_event(self, record_type: str, payload: Mapping[str, object]) -> None:
        from vibe_loop.runs import RunLifecycleEvent

        factory = getattr(RunLifecycleEvent, record_type)
        self.run_store.append_lifecycle_event(
            factory(run_id=self.run_id, task_id=self.task_id, payload=payload)
        )


GateExecutor = Callable[..., subprocess.CompletedProcess[str]]


class GateRunner:
    def __init__(
        self,
        *,
        completion_commands: Sequence[str],
        gate_keys: Sequence[str],
        candidate_collector: CandidateCollector,
        run_store: object,
        run_id: str,
        task_id: str,
        log_dir: Path,
        executor: GateExecutor = subprocess.run,
        candidate_poll_seconds: float = 0.25,
    ) -> None:
        self.completion_commands = tuple(completion_commands)
        self.gate_keys = tuple(gate_keys)
        self.candidate_collector = candidate_collector
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id
        self.log_dir = log_dir
        self.executor = executor
        if candidate_poll_seconds <= 0:
            raise ValueError("candidate_poll_seconds must be positive")
        self.candidate_poll_seconds = candidate_poll_seconds

    def run(self, candidate: CandidateRecord) -> GateRunSummary:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        prior = self._prior_results(candidate)
        results: list[GateResult] = []
        for index, config_key in enumerate(self.gate_keys):
            existing = prior.get(config_key)
            if existing is not None:
                results.append(existing)
                if not existing.passed:
                    break
                continue
            result = self._run_gate(index, config_key, candidate)
            self._record(result)
            results.append(result)
            if not result.passed:
                break
        return GateRunSummary(
            candidate=candidate,
            results=tuple(results),
            candidate_recorded=self.candidate_collector.is_recorded(candidate),
        )

    def _run_gate(
        self,
        index: int,
        config_key: str,
        candidate: CandidateRecord,
    ) -> GateResult:
        command = self._command(config_key)
        fingerprint = candidate.fingerprint.removeprefix("sha256:")[:16]
        log_path = self.log_dir / f"gate-{fingerprint}-{index + 1}.log"
        started = time.monotonic()
        exit_code: int | None = None
        if not self.candidate_collector.matches(candidate):
            exit_class = "candidate_changed"
            log_path.write_text(
                "candidate changed before gate execution\n", encoding="utf-8"
            )
        else:
            tracked_state_before = self.candidate_collector.tracked_state_marker()
            candidate_changed = threading.Event()
            monitor_stop = threading.Event()

            def monitor_candidate() -> None:
                while not monitor_stop.wait(self.candidate_poll_seconds):
                    if not self.candidate_collector.matches_during_gate(candidate):
                        candidate_changed.set()
                        return

            monitor = threading.Thread(
                target=monitor_candidate,
                name=f"vibe-loop-gate-candidate-{index + 1}",
                daemon=True,
            )
            monitor.start()
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    result = run_configured_command(
                        command,
                        worktree=self.candidate_collector.worktree,
                        log=log,
                        executor=self.executor,
                    )
                exit_code = result.returncode
                exit_class = "passed" if exit_code == 0 else "failed"
            except OSError:
                log_path.write_text(
                    "gate command could not be executed\n", encoding="utf-8"
                )
                exit_class = "execution_error"
            finally:
                monitor_stop.set()
                monitor.join()
            if candidate_changed.is_set() or not self.candidate_collector.matches(
                candidate
            ):
                exit_class = "candidate_changed"
            else:
                try:
                    tracked_state_changed = (
                        self.candidate_collector.tracked_state_marker()
                        != tracked_state_before
                    )
                except CandidateCollectionError:
                    tracked_state_changed = True
                if tracked_state_changed:
                    exit_class = "candidate_changed"
        duration = max(0.0, time.monotonic() - started)
        evidence_digest = "sha256:" + hashlib.sha256(log_path.read_bytes()).hexdigest()
        return GateResult(
            config_key=config_key,
            exit_class=exit_class,
            exit_code=exit_code,
            duration_seconds=duration,
            log_reference=str(log_path),
            evidence_digest=evidence_digest,
            candidate_fingerprint=candidate.fingerprint,
        )

    def _prior_results(self, candidate: CandidateRecord) -> dict[str, GateResult]:
        prior: dict[str, GateResult] = {}
        for record in self.run_store.read_records():
            if (
                record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
            ):
                continue
            result = GateResult.from_record(record)
            if result is None or result.candidate_fingerprint != candidate.fingerprint:
                continue
            if not gate_evidence_is_valid(result):
                continue
            prior[result.config_key] = result
        return prior

    def _command(self, config_key: str) -> str:
        match = re.fullmatch(r"completion\.commands\[(\d+)]", config_key)
        if match is None:
            raise GateExecutionError(
                f"gate key is not an allowlisted completion command reference: {config_key}"
            )
        index = int(match.group(1))
        if index >= len(self.completion_commands):
            raise GateExecutionError(
                f"gate key references an unavailable command: {config_key}"
            )
        return self.completion_commands[index]

    def _record(self, result: GateResult) -> None:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.gate_result(
                run_id=self.run_id,
                task_id=self.task_id,
                payload=result.to_payload(),
            )
        )


RemediationLauncher = Callable[[int, GateRunSummary], None]


def run_configured_command(
    command: str,
    *,
    worktree: Path,
    log: TextIO,
    executor: GateExecutor = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return executor(
        command,
        cwd=worktree,
        shell=True,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def gate_evidence_is_valid(result: GateResult) -> bool:
    try:
        digest = (
            "sha256:"
            + hashlib.sha256(Path(result.log_reference).read_bytes()).hexdigest()
        )
    except OSError:
        return False
    return digest == result.evidence_digest


class RuntimeGateController:
    def __init__(
        self,
        *,
        candidate_collector: CandidateCollector,
        gate_runner: GateRunner,
        stage_machine: RunLifecycleStateMachine,
        max_remediation_rounds: int,
        remediation_launcher: RemediationLauncher,
    ) -> None:
        if max_remediation_rounds < 0:
            raise ValueError("max_remediation_rounds must be non-negative")
        self.candidate_collector = candidate_collector
        self.gate_runner = gate_runner
        self.stage_machine = stage_machine
        self.max_remediation_rounds = max_remediation_rounds
        self.remediation_launcher = remediation_launcher

    def run(self, candidate: CandidateRecord | None = None) -> GateRunSummary:
        remediation_round = self.stage_machine.ordinal_for(RunStage.REMEDIATION)
        if self.stage_machine.stage is RunStage.REMEDIATION:
            previous_candidate = self._recorded_candidate()
            previous = self.gate_runner.run(previous_candidate)
            if previous.passed:
                raise GateExecutionError(
                    "remediation recovery requires recorded failing gate evidence"
                )
            self.remediation_launcher(remediation_round, previous)
            self.stage_machine.transition(
                RunStage.CANDIDATE,
                reason=f"remediation_candidate_collection:{remediation_round}",
            )
        while True:
            if self.stage_machine.stage is RunStage.IMPLEMENTING:
                self.stage_machine.transition(
                    RunStage.CANDIDATE,
                    reason="candidate_collection_started",
                )
            if self.stage_machine.stage is RunStage.CANDIDATE:
                if candidate is None:
                    current = self.candidate_collector.collect_derived()
                else:
                    self.candidate_collector.ensure_recorded(candidate)
                    current = candidate
                candidate = None
                self.stage_machine.transition(
                    RunStage.GATES, reason="runtime_gates_started"
                )
            elif self.stage_machine.stage is RunStage.GATES:
                current = self._recorded_candidate()
                if (
                    candidate is not None
                    and candidate.fingerprint != current.fingerprint
                ):
                    raise GateExecutionError(
                        "resume candidate does not match the durable candidate record"
                    )
                candidate = None
            else:
                stage = self.stage_machine.stage
                raise GateExecutionError(
                    "runtime gate controller cannot resume from stage "
                    f"{stage.value if stage is not None else '<initial>'}"
                )
            summary = self.gate_runner.run(current)
            if summary.passed:
                summary.require_review_ready()
                return summary
            if remediation_round >= self.max_remediation_rounds:
                self.stage_machine.fail(
                    StageFailure.STAGE_FAILED,
                    reason=(
                        "gate_remediation_exhausted:"
                        f"max_rounds={self.max_remediation_rounds}"
                    ),
                )
                raise GateRemediationExhausted(
                    self.max_remediation_rounds, summary.failed_gate_keys
                )
            remediation_round += 1
            self.stage_machine.transition(
                RunStage.REMEDIATION,
                reason=(
                    f"gate_failure:round={remediation_round}/"
                    f"{self.max_remediation_rounds}"
                ),
            )
            self.remediation_launcher(remediation_round, summary)
            self.stage_machine.transition(
                RunStage.CANDIDATE,
                reason=f"remediation_candidate_collection:{remediation_round}",
            )

    def _recorded_candidate(self) -> CandidateRecord:
        candidate = self.candidate_collector.latest_recorded()
        if candidate is None:
            raise GateExecutionError(
                "runtime gates require a durable candidate_recorded event"
            )
        return candidate


INTEGRATION_OUTCOMES = ("merged", "branch_already_merged", "failed")
INTEGRATION_FAILURE_REASONS = (
    "lock_timeout",
    "lock_unavailable",
    "workspace_preflight_failed",
    "merge_conflict",
    "merge_failed",
    "integration_verification_failed",
    "main_worktree_unavailable",
    "main_fast_forward_failed",
    "main_verification_failed",
)


@dataclasses.dataclass(frozen=True)
class IntegrationCheckResult:
    phase: str
    command_key: str
    exit_class: str
    exit_code: int | None
    duration_seconds: float
    log_reference: str
    evidence_digest: str

    @property
    def passed(self) -> bool:
        return self.exit_class == "passed"

    def to_payload(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> IntegrationCheckResult | None:
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(
                phase=str(value["phase"]),
                command_key=str(value["command_key"]),
                exit_class=str(value["exit_class"]),
                exit_code=(
                    int(value["exit_code"])
                    if value.get("exit_code") is not None
                    else None
                ),
                duration_seconds=float(value["duration_seconds"]),
                log_reference=str(value["log_reference"]),
                evidence_digest=str(value["evidence_digest"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclasses.dataclass(frozen=True)
class IntegrationResult:
    outcome: str
    status: str
    reason: str
    branch: str
    candidate_head: str
    refreshed_head: str
    main_before: str
    main_after: str
    verification: tuple[IntegrationCheckResult, ...] = ()
    recovered: bool = False
    diagnostics: Mapping[str, object] = dataclasses.field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "status": self.status,
            "reason": self.reason,
            "branch": self.branch,
            "candidate_head": self.candidate_head,
            "refreshed_head": self.refreshed_head,
            "main_before": self.main_before,
            "main_after": self.main_after,
            "verification": [item.to_payload() for item in self.verification],
            "recovered": self.recovered,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_record(cls, value: object) -> IntegrationResult | None:
        if (
            not isinstance(value, Mapping)
            or value.get("record_type") != "integration_result"
        ):
            return None
        outcome = value.get("outcome")
        status = value.get("status")
        reason = value.get("reason")
        verification = value.get("verification", [])
        if (
            outcome not in INTEGRATION_OUTCOMES
            or status not in {"completed", "blocked", "failed"}
            or not isinstance(reason, str)
            or not isinstance(verification, list)
        ):
            return None
        checks = tuple(
            check
            for item in verification
            if (check := IntegrationCheckResult.from_payload(item)) is not None
        )
        if len(checks) != len(verification):
            return None
        diagnostics = value.get("diagnostics")
        return cls(
            outcome=str(outcome),
            status=str(status),
            reason=reason,
            branch=str(value.get("branch") or ""),
            candidate_head=str(value.get("candidate_head") or ""),
            refreshed_head=str(value.get("refreshed_head") or ""),
            main_before=str(value.get("main_before") or ""),
            main_after=str(value.get("main_after") or ""),
            verification=checks,
            recovered=bool(value.get("recovered")),
            diagnostics=dict(diagnostics) if isinstance(diagnostics, Mapping) else {},
        )


@dataclasses.dataclass(frozen=True)
class _RecoveredIntegrationLock:
    status: object
    timed_out: bool = False


class Integrator:
    def __init__(
        self,
        *,
        repo: Path,
        main_branch: str,
        candidate: CandidateRecord,
        completion_commands: Sequence[str],
        integration_keys: Sequence[str],
        verify_on_main_keys: Sequence[str],
        lock_manager: object,
        run_store: object,
        run_id: str,
        task_id: str,
        log_dir: Path,
        wait: bool = True,
        timeout_seconds: float | None = 300,
        poll_interval_seconds: float = 1,
        executor: GateExecutor = subprocess.run,
        stage_machine: RunLifecycleStateMachine | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.main_branch = main_branch
        self.candidate = candidate
        self.completion_commands = tuple(completion_commands)
        self.integration_keys = tuple(integration_keys)
        self.verify_on_main_keys = tuple(verify_on_main_keys)
        self.lock_manager = lock_manager
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id
        self.log_dir = log_dir
        self.wait = wait
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.executor = executor
        self.stage_machine = stage_machine

    def run(self) -> IntegrationResult:
        prior = self._prior_result()
        if prior is not None and self._prior_result_is_consistent(prior):
            self._release_recovered_lock()
            return prior

        preflight = self._workspace_preflight()
        if preflight is not None:
            return self._record_preflight_failure(preflight)

        acquired, recovered_lock, lock_status = self._acquire_lock()
        if not acquired:
            reason = "lock_timeout" if lock_status.timed_out else "lock_unavailable"
            return self._record_failure(
                reason,
                status="blocked",
                diagnostics={"lock_state": lock_status.status.state},
            )

        self._record_lock_event("lock_acquired", lock_status.status.path)
        if (
            self.stage_machine is not None
            and self.stage_machine.stage is not RunStage.INTEGRATION
        ):
            self.stage_machine.transition(
                RunStage.INTEGRATION,
                reason="main_integration_lock_acquired",
            )
        try:
            concurrent_result = self._prior_result()
            if concurrent_result is not None and self._prior_result_is_consistent(
                concurrent_result
            ):
                return concurrent_result
            post_acquire_preflight = self._workspace_preflight()
            if post_acquire_preflight is not None:
                return self._record_preflight_failure(post_acquire_preflight)
            return self._run_locked(recovered_lock=recovered_lock)
        finally:
            fencing_token = lock_status.status.metadata.get("fencing_token")
            released = self.lock_manager.release_main_integration(
                task_id=self.task_id,
                run_id=self.run_id,
                fencing_token=(
                    fencing_token if isinstance(fencing_token, str) else None
                ),
            )
            if released:
                self._record_lock_event(
                    "lock_released",
                    lock_status.status.path,
                )

    def _run_locked(self, *, recovered_lock: bool) -> IntegrationResult:
        main_before = self._rev_parse(self.repo, self.main_branch)
        branch_head = self._rev_parse(self.candidate.worktree, "HEAD")
        current_main_checkout = self._current_branch(self.repo)
        if current_main_checkout != self.main_branch:
            return self._record_failure(
                "main_worktree_unavailable",
                status="blocked",
                main_before=main_before,
                refreshed_head=branch_head,
                diagnostics={
                    "expected_branch": self.main_branch,
                    "current_branch": current_main_checkout,
                },
                recovered=recovered_lock,
            )
        if not self._is_ancestor(self.candidate.head_commit, branch_head):
            return self._record_failure(
                "workspace_preflight_failed",
                status="blocked",
                main_before=main_before,
                refreshed_head=branch_head,
                diagnostics={"code": "candidate_not_ancestor_of_branch"},
                recovered=recovered_lock,
            )
        if branch_head == main_before:
            integration_checks = self._run_checks(
                phase="integration",
                keys=self.integration_keys,
                worktree=self.candidate.worktree,
            )
            if not all(check.passed for check in integration_checks):
                return self._record_failure(
                    "integration_verification_failed",
                    status="failed",
                    main_before=main_before,
                    main_after=main_before,
                    refreshed_head=branch_head,
                    verification=integration_checks,
                    recovered=recovered_lock,
                )
            main_checks = self._run_checks(
                phase="main",
                keys=self.verify_on_main_keys,
                worktree=self.repo,
            )
            checks = (*integration_checks, *main_checks)
            if not all(check.passed for check in main_checks):
                return self._record_failure(
                    "main_verification_failed",
                    status="failed",
                    main_before=main_before,
                    main_after=main_before,
                    refreshed_head=branch_head,
                    verification=checks,
                    recovered=recovered_lock,
                )
            return self._record_result(
                IntegrationResult(
                    outcome="branch_already_merged",
                    status="completed",
                    reason="branch_already_merged",
                    branch=self.candidate.branch,
                    candidate_head=self.candidate.head_commit,
                    refreshed_head=branch_head,
                    main_before=main_before,
                    main_after=main_before,
                    verification=checks,
                    recovered=recovered_lock,
                )
            )

        if branch_head == self.candidate.head_commit:
            merge = self._git(
                self.candidate.worktree, "merge", "--no-edit", self.main_branch
            )
            if merge.returncode != 0:
                conflicts = self._unmerged_paths()
                reason = "merge_conflict" if conflicts else "merge_failed"
                return self._record_failure(
                    reason,
                    status="blocked",
                    main_before=main_before,
                    refreshed_head=self._rev_parse(self.candidate.worktree, "HEAD"),
                    diagnostics={
                        "conflicted_paths": conflicts,
                        "git_output": self._bounded_git_output(merge),
                    },
                    recovered=recovered_lock,
                )
            branch_head = self._rev_parse(self.candidate.worktree, "HEAD")
            if not self._refresh_is_valid(branch_head):
                return self._record_failure(
                    "workspace_preflight_failed",
                    status="blocked",
                    main_before=main_before,
                    refreshed_head=branch_head,
                    diagnostics={"code": "refresh_result_not_reviewed_candidate"},
                    recovered=recovered_lock,
                )
        elif not self._is_recoverable_refresh(branch_head):
            return self._record_failure(
                "workspace_preflight_failed",
                status="blocked",
                main_before=main_before,
                refreshed_head=branch_head,
                diagnostics={"code": "unrecognized_refreshed_candidate"},
                recovered=recovered_lock,
            )

        integration_checks = self._run_checks(
            phase="integration",
            keys=self.integration_keys,
            worktree=self.candidate.worktree,
        )
        if not all(check.passed for check in integration_checks):
            return self._record_failure(
                "integration_verification_failed",
                status="failed",
                main_before=main_before,
                refreshed_head=branch_head,
                verification=integration_checks,
                recovered=recovered_lock,
            )

        main_head = self._rev_parse(self.repo, self.main_branch)
        if not self._is_ancestor(branch_head, main_head):
            merge_main = self._git(
                self.repo,
                "merge",
                "--ff-only",
                branch_head,
            )
            if merge_main.returncode != 0:
                return self._record_failure(
                    "main_fast_forward_failed",
                    status="blocked",
                    main_before=main_before,
                    main_after=self._rev_parse(self.repo, self.main_branch),
                    refreshed_head=branch_head,
                    verification=integration_checks,
                    diagnostics={"git_output": self._bounded_git_output(merge_main)},
                    recovered=recovered_lock,
                )
        main_after = self._rev_parse(self.repo, self.main_branch)
        main_checks = self._run_checks(
            phase="main",
            keys=self.verify_on_main_keys,
            worktree=self.repo,
        )
        checks = (*integration_checks, *main_checks)
        if not all(check.passed for check in main_checks):
            return self._record_failure(
                "main_verification_failed",
                status="failed",
                main_before=main_before,
                main_after=main_after,
                refreshed_head=branch_head,
                verification=checks,
                recovered=recovered_lock,
            )
        return self._record_result(
            IntegrationResult(
                outcome="merged",
                status="completed",
                reason="",
                branch=self.candidate.branch,
                candidate_head=self.candidate.head_commit,
                refreshed_head=branch_head,
                main_before=main_before,
                main_after=main_after,
                verification=checks,
                recovered=recovered_lock,
            )
        )

    def _workspace_preflight(self) -> dict[str, object] | None:
        from vibe_loop.workers import build_worker_views

        views = build_worker_views(
            self.lock_manager,
            self.run_store,
            repo=self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=(self.repo / ".vibe-loop",),
        )
        view = next(
            (
                item
                for item in views
                if item.active.task_id == self.task_id
                and item.active.run_id == self.run_id
            ),
            None,
        )
        if view is None or view.active.workspace is None:
            return {"code": "workspace_claim_missing", "diagnostics": []}
        claim = view.active.workspace
        if (
            claim.branch != self.candidate.branch
            or claim.worktree.resolve() != self.candidate.worktree.resolve()
        ):
            return {"code": "workspace_claim_mismatch", "diagnostics": []}
        if not view.workspace_diagnostics:
            return None
        if self._is_exact_merged_noop(view):
            return None
        conflicts = self._unmerged_paths()
        return {
            "code": "merge_conflict" if conflicts else "workspace_preflight_failed",
            "diagnostics": [
                diagnostic.to_json() for diagnostic in view.workspace_diagnostics
            ],
            "conflicted_paths": conflicts,
        }

    def _is_exact_merged_noop(self, view: object) -> bool:
        diagnostics = view.workspace_diagnostics
        state = view.workspace_git_state
        claim = view.active.workspace
        return bool(
            len(diagnostics) == 1
            and diagnostics[0].code == "branch_already_merged"
            and state is not None
            and claim is not None
            and state.worktree_exists
            and state.worktree_listed
            and not state.dirty
            and state.current_branch == claim.branch
            and state.head_commit
            and state.head_commit == self._rev_parse(self.repo, self.main_branch)
        )

    def _acquire_lock(self) -> tuple[bool, bool, object]:
        from vibe_loop.locks import LockBusy

        status = self.lock_manager.main_integration_status()
        if (
            status.locked
            and status.state == "stale"
            and status.metadata.get("owner_task_id") == self.task_id
            and status.metadata.get("run_id") == self.run_id
        ):
            try:
                self.lock_manager.recover_stale_main_integration(
                    task_id=self.task_id,
                    run_id=self.run_id,
                    metadata={
                        "pid": os.getpid(),
                        "pid_source": "runtime_integrator_recovery",
                    },
                )
            except LockBusy:
                pass
            else:
                return (
                    True,
                    True,
                    _RecoveredIntegrationLock(
                        self.lock_manager.main_integration_status()
                    ),
                )
        result = self.lock_manager.acquire_main_integration_with_wait(
            task_id=self.task_id,
            run_id=self.run_id,
            metadata={"pid": os.getpid(), "pid_source": "runtime_integrator"},
            wait=self.wait,
            timeout_seconds=self.timeout_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
        )
        return result.acquired, False, result

    def _run_checks(
        self,
        *,
        phase: str,
        keys: Sequence[str],
        worktree: Path,
    ) -> tuple[IntegrationCheckResult, ...]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        results: list[IntegrationCheckResult] = []
        for index, command_key in enumerate(keys):
            command = resolve_completion_command(
                self.completion_commands,
                command_key,
            )
            log_path = self.log_dir / f"{phase}-{index + 1}.log"
            started = time.monotonic()
            exit_code: int | None = None
            tracked_before = self._tracked_state(worktree)
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    completed = run_configured_command(
                        command,
                        worktree=worktree,
                        log=log,
                        executor=self.executor,
                    )
                exit_code = completed.returncode
                exit_class = "passed" if exit_code == 0 else "failed"
            except OSError:
                log_path.write_text(
                    "integration verification command could not be executed\n",
                    encoding="utf-8",
                )
                exit_class = "execution_error"
            if self._tracked_state(worktree) != tracked_before:
                exit_class = "candidate_changed"
            result = IntegrationCheckResult(
                phase=phase,
                command_key=command_key,
                exit_class=exit_class,
                exit_code=exit_code,
                duration_seconds=max(0.0, time.monotonic() - started),
                log_reference=str(log_path),
                evidence_digest=(
                    "sha256:" + hashlib.sha256(log_path.read_bytes()).hexdigest()
                ),
            )
            results.append(result)
            if not result.passed:
                break
        return tuple(results)

    def _tracked_state(self, worktree: Path) -> tuple[str, str]:
        return (
            self._rev_parse(worktree, "HEAD"),
            self._git(
                worktree,
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
            ).stdout,
        )

    def _prior_result(self) -> IntegrationResult | None:
        result = None
        for record in self.run_store.read_records():
            if (
                record.get("run_id") == self.run_id
                and record.get("task_id") == self.task_id
            ):
                result = IntegrationResult.from_record(record) or result
        return result

    def _prior_result_is_consistent(self, result: IntegrationResult) -> bool:
        if not result.completed:
            return True
        if not result.refreshed_head:
            return False
        return self._is_ancestor(
            result.refreshed_head,
            self._rev_parse(self.repo, self.main_branch),
        )

    def _release_recovered_lock(self) -> None:
        status = self.lock_manager.main_integration_status()
        if (
            status.locked
            and status.state == "stale"
            and status.metadata.get("owner_task_id") == self.task_id
            and status.metadata.get("run_id") == self.run_id
        ):
            fencing_token = status.metadata.get("fencing_token")
            if self.lock_manager.release_main_integration(
                task_id=self.task_id,
                run_id=self.run_id,
                fencing_token=(
                    fencing_token if isinstance(fencing_token, str) else None
                ),
            ):
                self._record_lock_event("lock_released", status.path)

    def _record_preflight_failure(
        self, preflight: Mapping[str, object]
    ) -> IntegrationResult:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.workspace_claim_mismatch(
                run_id=self.run_id,
                task_id=self.task_id,
                reason="workspace_preflight_failed",
                message="claimed workspace is not safe for runtime integration",
                details=preflight,
            )
        )
        reason = str(preflight.get("code") or "workspace_preflight_failed")
        if reason not in INTEGRATION_FAILURE_REASONS:
            reason = "workspace_preflight_failed"
        result = self._record_failure(
            reason,
            status="blocked",
            diagnostics=preflight,
        )
        self._release_recovered_lock()
        return result

    def _record_failure(
        self,
        reason: str,
        *,
        status: str,
        main_before: str = "",
        main_after: str = "",
        refreshed_head: str = "",
        verification: Sequence[IntegrationCheckResult] = (),
        recovered: bool = False,
        diagnostics: Mapping[str, object] | None = None,
    ) -> IntegrationResult:
        result = self._record_result(
            IntegrationResult(
                outcome="failed",
                status=status,
                reason=reason,
                branch=self.candidate.branch,
                candidate_head=self.candidate.head_commit,
                refreshed_head=refreshed_head,
                main_before=main_before,
                main_after=main_after,
                verification=tuple(verification),
                recovered=recovered,
                diagnostics=dict(diagnostics or {}),
            )
        )
        if self.stage_machine is not None:
            self.stage_machine.fail(
                StageFailure.BLOCKED
                if status == "blocked"
                else StageFailure.STAGE_FAILED,
                reason=reason,
            )
        return result

    def _record_result(self, result: IntegrationResult) -> IntegrationResult:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.integration_result(
                run_id=self.run_id,
                task_id=self.task_id,
                payload=result.to_payload(),
            )
        )
        return result

    def _record_lock_event(self, record_type: str, path: Path) -> None:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.lock_event(
                record_type,
                run_id=self.run_id,
                task_id=self.task_id,
                lock_kind="integration",
                lock_path=path,
                payload={"resource": "main-integration"},
            )
        )

    def _is_recoverable_refresh(self, head: str) -> bool:
        parents = self._git(
            self.candidate.worktree,
            "show",
            "-s",
            "--format=%P",
            head,
        )
        if parents.returncode != 0:
            return False
        parent_ids = parents.stdout.strip().split()
        if (
            len(parent_ids) != 2
            or parent_ids[0] != self.candidate.head_commit
            or not self._is_ancestor(parent_ids[1], self.main_branch)
        ):
            return False
        expected_tree = self._git(
            self.candidate.worktree,
            "merge-tree",
            "--write-tree",
            parent_ids[0],
            parent_ids[1],
        )
        if expected_tree.returncode != 0:
            return False
        actual_tree = self._git(
            self.candidate.worktree,
            "rev-parse",
            "--verify",
            f"{head}^{{tree}}",
        )
        return bool(
            actual_tree.returncode == 0
            and expected_tree.stdout.splitlines()[0].strip()
            == actual_tree.stdout.strip()
        )

    def _refresh_is_valid(self, head: str) -> bool:
        return bool(
            head == self.candidate.head_commit
            or (
                head == self._rev_parse(self.repo, self.main_branch)
                and self._is_ancestor(self.candidate.head_commit, head)
            )
            or self._is_recoverable_refresh(head)
        )

    def _unmerged_paths(self) -> list[str]:
        result = self._git(
            self.candidate.worktree,
            "diff",
            "--name-only",
            "--diff-filter=U",
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return (
            self._git(
                self.repo,
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ).returncode
            == 0
        )

    def _rev_parse(self, worktree: Path, ref: str) -> str:
        result = self._git(worktree, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if result.returncode != 0:
            raise RuntimeError(f"git ref is unavailable for integration: {ref}")
        return result.stdout.strip()

    def _current_branch(self, worktree: Path) -> str:
        result = self._git(worktree, "branch", "--show-current")
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _bounded_git_output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout + result.stderr).strip()[:2000]

    @staticmethod
    def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


class TaskSourceCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class TaskProvenanceResult:
    mode: str
    adapter: str
    confirmed_status: str
    integration_outcome: str
    recovered: bool = False

    def to_payload(self) -> dict[str, object]:
        return dataclasses.asdict(self)


class TaskSourceCompleter:
    """Commit project-owned completion after durable runtime integration."""

    def __init__(
        self,
        *,
        source: TaskSource,
        task_source_config: TaskSourceConfig,
        mode: str,
        lock_manager: object,
        task_lock: object,
        run_store: object,
        run_id: str,
        task_id: str,
        runtime_context: Mapping[str, str] | None = None,
        stage_machine: RunLifecycleStateMachine | None = None,
    ) -> None:
        if mode not in {"adapter", "external-confirmed"}:
            raise ValueError(f"unsupported task provenance mode: {mode}")
        self.source = source
        self.task_source_config = task_source_config
        self.mode = mode
        self.lock_manager = lock_manager
        self.task_lock = task_lock
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id
        self.runtime_context = dict(runtime_context or {})
        self.stage_machine = stage_machine

    def complete(self) -> TaskProvenanceResult:
        self._validate_lock_owner()
        prior = self._prior_result()
        if prior is not None:
            return prior
        if self._completed_report_exists():
            raise TaskSourceCompletionError(
                "completed_report_precedes_provenance",
                "completed run result cannot precede task provenance",
            )
        integration = self._completed_integration()
        if integration is None:
            raise TaskSourceCompletionError(
                "integration_not_recorded",
                "task provenance requires a durable completed integration_result",
            )
        if (
            self.stage_machine is not None
            and self.stage_machine.stage is not RunStage.PROVENANCE
        ):
            if self.stage_machine.stage is not RunStage.INTEGRATION:
                raise TaskSourceCompletionError(
                    "integration_stage_not_current",
                    "task provenance may only follow the integration stage",
                )
            self.stage_machine.transition(
                RunStage.PROVENANCE,
                reason="integration_confirmed",
            )

        confirmed = self._probe("completion_probe_failed")
        recovered = confirmed is not None and confirmed.done
        if not recovered and self.mode == "adapter":
            if not self.task_source_config.complete_command:
                raise self._blocked_error(
                    "completion_adapter_unconfigured",
                    "runtime-owned adapter completion requires task_source.complete",
                )
            try:
                confirmed = self.source.complete(
                    self.task_id,
                    self.run_id,
                    runtime_context=self.runtime_context,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise self._blocked_error(
                    "completion_adapter_failed",
                    "task_source.complete failed; integrated candidate preserved: "
                    f"{type(exc).__name__}",
                ) from exc
        if confirmed is None or confirmed.task_id != self.task_id or not confirmed.done:
            code = (
                "external_completion_unconfirmed"
                if self.mode == "external-confirmed"
                else "completion_adapter_unconfirmed"
            )
            raise self._blocked_error(
                code,
                "authoritative task source does not confirm completion; "
                "integrated candidate preserved",
            )
        result = TaskProvenanceResult(
            mode=self.mode,
            adapter=(
                "task_source.complete"
                if self.mode == "adapter"
                else "task_source.probe"
            ),
            confirmed_status=confirmed.status,
            integration_outcome=integration.outcome,
            recovered=recovered,
        )
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_provenance_committed(
                run_id=self.run_id,
                task_id=self.task_id,
                payload=result.to_payload(),
            )
        )
        return result

    def _validate_lock_owner(self) -> None:
        token = getattr(self.task_lock, "metadata", {}).get("fencing_token")
        self.lock_manager.validate_owner(
            task_id=self.task_id,
            run_id=self.run_id,
            fencing_token=token if isinstance(token, str) else None,
        )

    def _probe(self, code: str) -> Task | None:
        try:
            return self.source.probe(self.task_id)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise self._blocked_error(
                code,
                f"authoritative task-source probe failed: {type(exc).__name__}",
            ) from exc

    def _blocked_error(self, code: str, message: str) -> TaskSourceCompletionError:
        if self.stage_machine is not None and self.stage_machine.stage not in {
            RunStage.CLASSIFICATION,
            RunStage.FINALIZATION,
        }:
            self.stage_machine.fail(StageFailure.BLOCKED, reason=code)
        return TaskSourceCompletionError(code, message)

    def _completed_integration(self) -> IntegrationResult | None:
        result = None
        for record in self.run_store.read_records():
            if (
                record.get("run_id") == self.run_id
                and record.get("task_id") == self.task_id
            ):
                candidate = IntegrationResult.from_record(record)
                if candidate is not None and candidate.completed:
                    result = candidate
        return result

    def _completed_report_exists(self) -> bool:
        return any(
            record.get("record_type") in {None, "run_result"}
            and record.get("run_id") == self.run_id
            and record.get("task_id") == self.task_id
            and record.get("classification") == "completed"
            for record in self.run_store.read_records()
        )

    def _prior_result(self) -> TaskProvenanceResult | None:
        for record in reversed(self.run_store.read_records()):
            if (
                record.get("record_type") != "task_provenance_committed"
                or record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
            ):
                continue
            try:
                return TaskProvenanceResult(
                    mode=str(record["mode"]),
                    adapter=str(record["adapter"]),
                    confirmed_status=str(record["confirmed_status"]),
                    integration_outcome=str(record["integration_outcome"]),
                    recovered=bool(record.get("recovered")),
                )
            except KeyError:
                return None
        return None


@dataclasses.dataclass(frozen=True)
class TaskSourceSettlementResult:
    intent: str
    adapter: str
    confirmed_status: str
    fallback_to_requeue: bool
    attempts: int
    settled: bool
    recovered: bool = False

    @property
    def settlement_pending(self) -> bool:
        return not self.settled

    def to_payload(self) -> dict[str, object]:
        return dataclasses.asdict(self)


class TaskSourceSettler:
    """Settle an activated task source before its fenced lock may release."""

    def __init__(
        self,
        *,
        source: TaskSource,
        task_source_config: TaskSourceConfig,
        lock_manager: object,
        task_lock: object,
        run_store: object,
        run_id: str,
        task_id: str,
        runtime_context: Mapping[str, str] | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("settlement max_attempts must be positive")
        if backoff_seconds < 0:
            raise ValueError("settlement backoff_seconds cannot be negative")
        self.source = source
        self.task_source_config = task_source_config
        self.lock_manager = lock_manager
        self.task_lock = task_lock
        self.run_store = run_store
        self.run_id = run_id
        self.task_id = task_id
        self.runtime_context = dict(runtime_context or {})
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper

    def settle(self, intent: str) -> TaskSourceSettlementResult:
        if intent not in {"requeue", "park"}:
            raise ValueError(f"unsupported task-source settlement intent: {intent}")
        self._validate_lock_owner()
        prior = self._prior_result()
        if prior is not None:
            return prior
        if not self.task_source_config.activate_command:
            return self._record_settled(
                TaskSourceSettlementResult(
                    intent="not_applicable",
                    adapter="none",
                    confirmed_status="not_applicable",
                    fallback_to_requeue=False,
                    attempts=0,
                    settled=True,
                )
            )
        if not self.task_source_config.reset_command:
            raise TaskSourceCompletionError(
                "settlement_adapter_unconfigured",
                "activation-capable task source requires task_source.reset",
            )
        fallback = intent == "park" and not self.task_source_config.park_command
        effective_intent = "requeue" if fallback else intent
        adapter = (
            "task_source.park" if effective_intent == "park" else "task_source.reset"
        )
        try:
            confirmed = self._probe_for_settlement()
        except (OSError, subprocess.SubprocessError, ValueError):
            confirmed = None
        if self._confirmed(confirmed, effective_intent):
            return self._record_settled(
                TaskSourceSettlementResult(
                    intent=intent,
                    adapter=adapter,
                    confirmed_status=confirmed.status if confirmed is not None else "",
                    fallback_to_requeue=fallback,
                    attempts=0,
                    settled=True,
                    recovered=True,
                )
            )

        for attempt in range(1, self.max_attempts + 1):
            error_class = "unconfirmed_status"
            try:
                if effective_intent == "park":
                    self.source.park(
                        self.task_id,
                        self.run_id,
                        runtime_context=self.runtime_context,
                    )
                else:
                    self.source.reset(
                        self.task_id,
                        runtime_context=self.runtime_context,
                    )
                confirmed = self._probe_for_settlement()
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                error_class = type(exc).__name__
                confirmed = None
            if self._confirmed(confirmed, effective_intent):
                return self._record_settled(
                    TaskSourceSettlementResult(
                        intent=intent,
                        adapter=adapter,
                        confirmed_status=(
                            confirmed.status if confirmed is not None else ""
                        ),
                        fallback_to_requeue=fallback,
                        attempts=attempt,
                        settled=True,
                    )
                )
            self._record_attempt(
                intent=intent,
                adapter=adapter,
                fallback=fallback,
                attempt=attempt,
                error_class=error_class,
                confirmed_status=confirmed.status if confirmed is not None else "",
            )
            if attempt < self.max_attempts:
                self.sleeper(self.backoff_seconds * attempt)
        return TaskSourceSettlementResult(
            intent=intent,
            adapter=adapter,
            confirmed_status=confirmed.status if confirmed is not None else "",
            fallback_to_requeue=fallback,
            attempts=self.max_attempts,
            settled=False,
        )

    def recover_and_release(self, intent: str) -> TaskSourceSettlementResult:
        result = self.settle(intent)
        if not result.settled:
            return result
        classification = self._durable_classification()
        if classification is None:
            raise TaskSourceCompletionError(
                "durable_outcome_not_recorded",
                "task-source settlement recovery cannot release before the "
                "durable run result",
            )
        self._validate_lock_owner()
        from vibe_loop.runs import (
            LOCK_RELEASED_RECORD_TYPE,
            RunLifecycleEvent,
            settled_run_outcome,
        )

        outcome = settled_run_outcome(classification)
        metadata = dict(self.task_lock.metadata)
        metadata["outcome"] = outcome
        metadata["classification"] = classification
        self.lock_manager.release_settled(
            self.task_lock,
            metadata,
            outcome=outcome,
        )

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.lock_event(
                LOCK_RELEASED_RECORD_TYPE,
                run_id=self.run_id,
                task_id=self.task_id,
                lock_kind="task",
                lock_path=self.task_lock.path,
                payload={"reason": "task_source_settlement_recovered"},
            )
        )
        return dataclasses.replace(result, recovered=True)

    def _durable_classification(self) -> str | None:
        for record in reversed(self.run_store.read_records()):
            if (
                record.get("record_type") in {None, "run_result"}
                and record.get("run_id") == self.run_id
                and record.get("task_id") == self.task_id
            ):
                classification = record.get("classification")
                return classification if isinstance(classification, str) else None
        return None

    def _validate_lock_owner(self) -> None:
        token = getattr(self.task_lock, "metadata", {}).get("fencing_token")
        self.lock_manager.validate_owner(
            task_id=self.task_id,
            run_id=self.run_id,
            fencing_token=token if isinstance(token, str) else None,
        )

    def _probe_for_settlement(self) -> Task | None:
        return self.source.probe(self.task_id)

    def _confirmed(self, task: Task | None, intent: str) -> bool:
        if task is None or task.task_id != self.task_id:
            return False
        status = task.status.casefold()
        if intent == "requeue":
            return status in {
                candidate.casefold()
                for candidate in self.task_source_config.runnable_statuses
            }
        return status in BLOCKED_FAMILY_STATUSES or status in {
            "on-hold",
            "on_hold",
            "parked",
        }

    def _record_attempt(
        self,
        *,
        intent: str,
        adapter: str,
        fallback: bool,
        attempt: int,
        error_class: str,
        confirmed_status: str,
    ) -> None:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_source_settlement_attempted(
                run_id=self.run_id,
                task_id=self.task_id,
                payload={
                    "intent": intent,
                    "adapter": adapter,
                    "fallback_to_requeue": fallback,
                    "retry_ordinal": attempt,
                    "error_class": error_class,
                    "confirmed_status": confirmed_status,
                    "settlement_pending": True,
                },
            )
        )

    def _record_settled(
        self, result: TaskSourceSettlementResult
    ) -> TaskSourceSettlementResult:
        from vibe_loop.runs import RunLifecycleEvent

        self.run_store.append_lifecycle_event(
            RunLifecycleEvent.task_source_settled(
                run_id=self.run_id,
                task_id=self.task_id,
                payload=result.to_payload(),
            )
        )
        return result

    def _prior_result(self) -> TaskSourceSettlementResult | None:
        for record in reversed(self.run_store.read_records()):
            if (
                record.get("record_type") != "task_source_settled"
                or record.get("run_id") != self.run_id
                or record.get("task_id") != self.task_id
            ):
                continue
            try:
                return TaskSourceSettlementResult(
                    intent=str(record["intent"]),
                    adapter=str(record["adapter"]),
                    confirmed_status=str(record["confirmed_status"]),
                    fallback_to_requeue=bool(record["fallback_to_requeue"]),
                    attempts=int(record["attempts"]),
                    settled=bool(record["settled"]),
                    recovered=bool(record.get("recovered")),
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None


def resolve_completion_command(
    completion_commands: Sequence[str], command_key: str
) -> str:
    match = re.fullmatch(r"completion\.commands\[(\d+)]", command_key)
    if match is None:
        raise GateExecutionError(
            "command key is not an allowlisted completion command reference: "
            f"{command_key}"
        )
    index = int(match.group(1))
    if index >= len(completion_commands):
        raise GateExecutionError(
            f"command key references an unavailable command: {command_key}"
        )
    return completion_commands[index]


@dataclasses.dataclass(frozen=True)
class RunContractProposal:
    kind: str
    source_id: str
    values: Mapping[str, object]
    digest: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RUN_CONTRACT_SOURCE_KINDS[1:]:
            raise ValueError(
                "run contract proposal kind must be profile or skill-proposal"
            )
        if not self.source_id:
            raise ValueError("run contract proposal source_id is required")
        if self.digest and not is_sha256_digest(self.digest):
            raise ValueError("run contract proposal digest must be a sha256 digest")

    @property
    def source_digest(self) -> str:
        if self.digest:
            return self.digest
        return sha256_digest(
            {"kind": self.kind, "id": self.source_id, "values": dict(self.values)}
        )


@dataclasses.dataclass(frozen=True)
class ResolvedRunContract:
    payload: Mapping[str, object]
    digest: str

    def to_record_payload(self) -> dict[str, object]:
        return {**self.payload, "contract_digest": self.digest}


class RunContractResolver:
    def __init__(self, config: VibeConfig) -> None:
        self.config = config

    def resolve(
        self,
        agent_selection: AgentSelection,
        *,
        profile: RunContractProposal | None = None,
        skill_proposal: RunContractProposal | None = None,
    ) -> ResolvedRunContract:
        if profile is not None and profile.kind != "profile":
            raise ValueError("profile proposal must have kind='profile'")
        if skill_proposal is not None and skill_proposal.kind != "skill-proposal":
            raise ValueError("skill proposal must have kind='skill-proposal'")

        effective = parse_orchestration(
            {},
            completion=self.config.completion,
            agent_profiles=self.config.agent_profiles,
        )
        contributors: list[dict[str, str]] = []
        for proposal in (skill_proposal, profile):
            if proposal is None:
                continue
            parsed = parse_orchestration(
                dict(proposal.values),
                completion=self.config.completion,
                agent_profiles=self.config.agent_profiles,
            )
            effective = overlay_explicit_orchestration(effective, parsed)
            contributors.append(
                {
                    "kind": proposal.kind,
                    "id": proposal.source_id,
                    "digest": proposal.source_digest,
                }
            )
        effective = overlay_explicit_orchestration(
            effective,
            self.config.orchestration,
        )

        config_source = config_source_identity(self.config, effective)
        if self.config.orchestration.explicit_keys or not contributors:
            contributors.append(config_source)
            primary_source = config_source
        else:
            primary_source = contributors[-1]

        implementer = route_payload(agent_selection.config, agent_selection.profile)
        configured_reviewer_profile = effective.reviewer_profile
        reviewer_profile = configured_reviewer_profile
        if reviewer_profile is None:
            reviewer_agent = agent_selection.config
            reviewer_profile = agent_selection.profile
        else:
            reviewer_agent = self.config.agent_profiles[reviewer_profile]
        reviewer_command = reviewer_agent.command
        if configured_reviewer_profile is not None:
            reviewer_command = (
                reviewer_agent.require_reviewer_command()
                if effective.mode == "runtime-owned"
                else reviewer_agent.require_command()
            )
        if configured_reviewer_profile is not None and not command_template_uses_field(
            reviewer_command or "", "prompt"
        ):
            command_key = (
                f"agent.profiles.{reviewer_profile}.command"
                if reviewer_profile
                else "agent.command"
            )
            raise AgentResolutionError(
                f"{command_key} must include {{prompt}} for reviewer request delivery"
            )

        if effective.mode == "runtime-owned":
            if configured_reviewer_profile is None:
                raise ValueError(
                    "runtime-owned orchestration requires an explicit independent "
                    "orchestration.reviewer_profile"
                )
            if configured_reviewer_profile == agent_selection.profile:
                raise ValueError(
                    "runtime-owned orchestration reviewer_profile must differ "
                    "from the implementer profile"
                )
            if "task_provenance_mode" not in effective.explicit_keys:
                raise ValueError(
                    "runtime-owned orchestration requires an explicit "
                    "orchestration.task_provenance_mode completion path"
                )
            if (
                effective.task_provenance_mode == "adapter"
                and self.config.task_source.complete_command is None
            ):
                raise ValueError(
                    "runtime-owned adapter completion requires task_source.complete"
                )
            if (
                self.config.task_source.activate_command is not None
                and self.config.task_source.reset_command is None
            ):
                raise ValueError(
                    "runtime-owned activation-capable task source requires "
                    "task_source.reset"
                )

        payload: dict[str, object] = {
            "contract_version": RUN_CONTRACT_VERSION,
            "mode": effective.mode,
            "source": {
                **primary_source,
                "inputs": contributors,
            },
            "implementer": {
                **implementer,
                "timeout_seconds": self.config.supervision.worker_timeout_seconds,
            },
            "reviewer": {
                **route_payload(reviewer_agent, reviewer_profile),
                "timeout_seconds": 0,
                "max_initial_passes": effective.max_initial_review_passes,
                "max_closure_passes": effective.max_closure_review_passes,
                "concurrency_budget": effective.reviewer_concurrency_budget,
            },
            "gates": [
                {"id": command_ref, "command_key": command_ref}
                for command_ref in effective.gates
            ],
            "integration": {
                "enabled": effective.integration_enabled,
                "verify_on_main": list(effective.verify_on_main),
            },
            "task_provenance": {
                "mode": effective.task_provenance_mode,
                "complete_adapter": None,
                "settlement": {
                    "requeue_adapter": (
                        "task_source.reset"
                        if self.config.task_source.reset_command is not None
                        else None
                    ),
                    "park_adapter": (
                        "task_source.park"
                        if self.config.task_source.park_command is not None
                        else None
                    ),
                },
            },
            "remediation": {"max_rounds": effective.max_remediation_rounds},
        }
        task_provenance = payload["task_provenance"]
        assert isinstance(task_provenance, dict)
        if self.config.task_source.complete_command is not None:
            task_provenance["complete_adapter"] = "task_source.complete"
        return ResolvedRunContract(payload=payload, digest=sha256_digest(payload))


class WorkspaceProvisionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ProvisionedWorkspace:
    mode: str
    branch: str
    worktree: Path
    base_commit: str
    head_commit: str
    owner_run_id: str = ""
    dirty_at_adoption: bool = False

    def to_record_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "mode": self.mode,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "dirty_at_adoption": self.dirty_at_adoption,
        }
        if self.owner_run_id:
            payload["owner_run_id"] = self.owner_run_id
        return payload


class WorkspaceProvisioner:
    def __init__(
        self,
        *,
        repo: Path,
        main_branch: str,
        lock_manager: object,
        run_store: object,
        ignored_dirty_paths: Sequence[Path] = (),
    ) -> None:
        self.repo = repo.resolve()
        self.main_branch = main_branch
        self.lock_manager = lock_manager
        self.run_store = run_store
        self.ignored_dirty_paths = tuple(ignored_dirty_paths)

    def provision(
        self,
        *,
        task_id: str,
        run_id: str,
        base_commit: str,
        fencing_token: str | None = None,
        recovery_run_id: str = "",
        recovery_branch: str = "",
        recovery_worktree: Path | None = None,
        recovery_git_common_dir: Path | None = None,
        recovery_base_commit: str = "",
        recovery_head_commit: str = "",
        recovery_dirty_snapshot: Sequence[str] | None = None,
        recovery_dirty_fingerprint: str = "",
    ) -> ProvisionedWorkspace:
        from vibe_loop.runs import RunLifecycleEvent
        from vibe_loop.workers import claim_worker_workspace

        self._validate_primary(base_commit)
        branch, worktree = self._workspace_identity(
            task_id=task_id,
            recovery_branch=recovery_branch,
            recovery_worktree=recovery_worktree,
        )
        workspace = self._create_or_adopt(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            base_commit=base_commit,
            recovery_run_id=recovery_run_id,
            recovery_git_common_dir=recovery_git_common_dir,
            recovery_base_commit=recovery_base_commit,
            recovery_head_commit=recovery_head_commit,
            recovery_dirty_snapshot=recovery_dirty_snapshot,
            recovery_dirty_fingerprint=recovery_dirty_fingerprint,
        )
        try:
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.workspace_provisioned(
                    run_id=run_id,
                    task_id=task_id,
                    payload=workspace.to_record_payload(),
                )
            )
            claim_worker_workspace(
                self.lock_manager,
                self.run_store,
                task_id=task_id,
                run_id=run_id,
                branch=workspace.branch,
                worktree=workspace.worktree,
                repo=self.repo,
                base_commit=workspace.base_commit,
                fencing_token=fencing_token,
                ignored_dirty_paths=self.ignored_dirty_paths,
            )
        except KeyboardInterrupt:
            if workspace.mode == "created":
                self.compensate_created(workspace)
            raise
        except Exception:
            # Journal and claim backends are extensible local/command adapters;
            # any failure must compensate a workspace created by this run.
            if workspace.mode == "created":
                self.compensate_created(workspace)
            raise
        return workspace

    def compensate_created(self, workspace: ProvisionedWorkspace) -> None:
        if workspace.mode != "created":
            return
        from vibe_loop.workers import build_workspace_git_context, git_status_lines

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        entries = [
            entry
            for entry in context.worktrees
            if entry.path == workspace.worktree.resolve()
        ]
        if len(entries) != 1 or entries[0].branch != workspace.branch:
            raise WorkspaceProvisionError(
                "compensation_identity_mismatch",
                "refusing to compensate workspace whose git identity changed",
                details={
                    "branch": workspace.branch,
                    "worktree": str(workspace.worktree),
                },
            )
        if git_status_lines(
            workspace.worktree,
            ignored_dirty_paths=self.ignored_dirty_paths,
        ):
            raise WorkspaceProvisionError(
                "compensation_dirty_workspace",
                "refusing to remove a created workspace that became dirty",
                details={"worktree": str(workspace.worktree)},
            )
        self._git("worktree", "remove", str(workspace.worktree))
        self._git("branch", "-d", workspace.branch)

    def _validate_primary(self, base_commit: str) -> None:
        from vibe_loop.workers import build_workspace_git_context, git_status_lines

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if context.worktree_list_error:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "workspace provisioning requires readable git worktree state",
                details={"error": context.worktree_list_error},
            )
        if not context.worktrees or context.worktrees[0].path != self.repo:
            raise WorkspaceProvisionError(
                "primary_worktree_required",
                "workspace provisioning must run against the primary git worktree",
                details={"repo": str(self.repo)},
            )
        primary = context.worktrees[0]
        if primary.branch != self.main_branch:
            raise WorkspaceProvisionError(
                "primary_branch_mismatch",
                "primary worktree is not on the configured main branch",
                details={
                    "expected_branch": self.main_branch,
                    "current_branch": primary.branch,
                },
            )
        dirty = git_status_lines(
            self.repo,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if dirty:
            raise WorkspaceProvisionError(
                "dirty_primary_worktree",
                "primary worktree must be clean before worker provisioning",
                details={"dirty_summary": dirty[:20]},
            )
        resolved_base = self._git_text("rev-parse", "--verify", base_commit)
        main_head = self._git_text("rev-parse", "--verify", self.main_branch)
        if resolved_base != main_head:
            raise WorkspaceProvisionError(
                "base_main_mismatch",
                "workspace base no longer matches the configured main branch",
                details={"base_commit": resolved_base, "main_head": main_head},
            )

    def _workspace_identity(
        self,
        *,
        task_id: str,
        recovery_branch: str,
        recovery_worktree: Path | None,
    ) -> tuple[str, Path]:
        if bool(recovery_branch) != (recovery_worktree is not None):
            raise WorkspaceProvisionError(
                "incomplete_recovery_workspace",
                "recovery requires both a recorded branch and worktree",
            )
        if recovery_branch and recovery_worktree is not None:
            return recovery_branch, recovery_worktree.resolve()
        owned = self._existing_owned_identity(task_id)
        if owned is not None:
            return owned
        name = workspace_name(task_id)
        return (
            f"{WORKSPACE_BRANCH_PREFIX}{name}",
            self.repo.parent / f"{self.repo.name}-worktrees" / name,
        )

    def _create_or_adopt(
        self,
        *,
        task_id: str,
        run_id: str,
        branch: str,
        worktree: Path,
        base_commit: str,
        recovery_run_id: str,
        recovery_git_common_dir: Path | None,
        recovery_base_commit: str,
        recovery_head_commit: str,
        recovery_dirty_snapshot: Sequence[str] | None,
        recovery_dirty_fingerprint: str,
    ) -> ProvisionedWorkspace:
        from vibe_loop.workers import build_workspace_git_context, git_dirty_snapshot

        if branch == self.main_branch or worktree == self.repo:
            raise WorkspaceProvisionError(
                "primary_workspace_forbidden",
                "a worker cannot use the primary worktree or main branch",
                details={"branch": branch, "worktree": str(worktree)},
            )
        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        branch_entries = [
            entry for entry in context.worktrees if entry.branch == branch
        ]
        path_entries = [
            entry for entry in context.worktrees if entry.path == worktree.resolve()
        ]
        branch_exists = (
            self._git_returncode(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
            )
            == 0
        )
        path_exists = worktree.exists()
        if (
            not branch_entries
            and not path_entries
            and not branch_exists
            and not path_exists
        ):
            worktree.parent.mkdir(parents=True, exist_ok=True)
            result = self._git_result(
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                base_commit,
            )
            if result.returncode != 0:
                self._compensate_partial_creation(
                    branch=branch,
                    worktree=worktree,
                    base_commit=base_commit,
                )
                raise WorkspaceProvisionError(
                    "workspace_create_failed",
                    "git could not create the task workspace",
                    details={"stderr": result.stderr.strip()},
                )
            workspace = ProvisionedWorkspace(
                mode="created",
                branch=branch,
                worktree=worktree.resolve(),
                base_commit=base_commit,
                head_commit=base_commit,
            )
            try:
                head_commit = self._git_text_at(
                    worktree, "rev-parse", "--verify", "HEAD"
                )
            except Exception:
                # Git state inspection can fail for several OS/repository
                # reasons after add succeeds; all share the same compensation.
                self._compensate_partial_creation(
                    branch=branch,
                    worktree=worktree,
                    base_commit=base_commit,
                )
                raise
            return dataclasses.replace(workspace, head_commit=head_commit)
        if (
            len(branch_entries) != 1
            or len(path_entries) != 1
            or branch_entries[0].path != worktree.resolve()
            or path_entries[0].branch != branch
        ):
            raise WorkspaceProvisionError(
                "workspace_collision",
                "existing branch/worktree state is ambiguous or mismatched",
                details={
                    "branch": branch,
                    "worktree": str(worktree),
                    "branch_worktrees": [str(entry.path) for entry in branch_entries],
                    "path_branches": [entry.branch for entry in path_entries],
                    "branch_exists": branch_exists,
                    "path_exists": path_exists,
                },
            )
        owner = self._ownership_record(
            task_id=task_id,
            branch=branch,
            worktree=worktree,
            recovery_run_id=recovery_run_id,
        )
        if owner is None:
            raise WorkspaceProvisionError(
                "workspace_ownership_unverified",
                "existing workspace has no matching task ownership record",
                details={"branch": branch, "worktree": str(worktree)},
            )
        self._reject_live_foreign_claim(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
        )
        head = self._git_text_at(worktree, "rev-parse", "--verify", "HEAD")
        owner_base = owner.get("base_commit")
        if not isinstance(owner_base, str) or not owner_base:
            raise WorkspaceProvisionError(
                "workspace_base_unverified",
                "existing workspace ownership record has no base commit",
                details={"owner_run_id": str(owner.get("run_id") or "")},
            )
        if recovery_base_commit and owner_base != recovery_base_commit:
            raise WorkspaceProvisionError(
                "recovery_base_changed",
                "recovery workspace base no longer matches the pending intent",
                details={
                    "expected_base": recovery_base_commit,
                    "actual_base": owner_base,
                },
            )
        if recovery_head_commit and head != recovery_head_commit:
            raise WorkspaceProvisionError(
                "recovery_head_changed",
                "recovery workspace HEAD moved after the pending intent was recorded",
                details={
                    "expected_head": recovery_head_commit,
                    "actual_head": head,
                },
            )
        if recovery_git_common_dir is not None:
            actual_common_dir = self._git_common_dir_at(worktree)
            if actual_common_dir != recovery_git_common_dir.resolve():
                raise WorkspaceProvisionError(
                    "recovery_git_common_dir_changed",
                    "recovery workspace belongs to a different git repository",
                    details={
                        "expected_git_common_dir": str(
                            recovery_git_common_dir.resolve()
                        ),
                        "actual_git_common_dir": str(actual_common_dir),
                    },
                )
        if (
            self._git_returncode_at(
                worktree,
                "merge-base",
                "--is-ancestor",
                owner_base,
                head,
            )
            != 0
        ):
            raise WorkspaceProvisionError(
                "workspace_base_mismatch",
                "existing workspace does not descend from its recorded base",
                details={"base_commit": owner_base, "head_commit": head},
            )
        if (
            self._git_returncode(
                "merge-base",
                "--is-ancestor",
                owner_base,
                base_commit,
            )
            != 0
        ):
            raise WorkspaceProvisionError(
                "workspace_main_history_mismatch",
                "existing workspace base is not in the selected main history",
                details={
                    "workspace_base": owner_base,
                    "selected_base": base_commit,
                },
            )
        dirty, dirty_fingerprint = git_dirty_snapshot(
            worktree,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if recovery_dirty_snapshot is not None and tuple(dirty) != tuple(
            recovery_dirty_snapshot
        ):
            raise WorkspaceProvisionError(
                "recovery_dirty_snapshot_changed",
                "recovery workspace dirt changed after the pending intent was recorded",
                details={
                    "expected_dirty_summary": list(recovery_dirty_snapshot)[:20],
                    "actual_dirty_summary": dirty[:20],
                },
            )
        if (
            recovery_dirty_fingerprint
            and dirty_fingerprint != recovery_dirty_fingerprint
        ):
            raise WorkspaceProvisionError(
                "recovery_dirty_content_changed",
                "recovery workspace content changed after the pending intent",
                details={
                    "expected_dirty_fingerprint": recovery_dirty_fingerprint,
                    "actual_dirty_fingerprint": dirty_fingerprint,
                },
            )
        if dirty and not recovery_run_id:
            raise WorkspaceProvisionError(
                "dirty_existing_workspace",
                "dirty existing workspace is preserved and cannot be adopted",
                details={"dirty_summary": dirty[:20]},
            )
        return ProvisionedWorkspace(
            mode="preserved" if dirty else "adopted",
            branch=branch,
            worktree=worktree.resolve(),
            base_commit=owner_base,
            head_commit=head,
            owner_run_id=str(owner.get("run_id") or ""),
            dirty_at_adoption=bool(dirty),
        )

    def _existing_owned_identity(self, task_id: str) -> tuple[str, Path] | None:
        from vibe_loop.workers import build_workspace_git_context

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        listed = {(entry.branch, entry.path.resolve()) for entry in context.worktrees}
        candidates: set[tuple[str, Path]] = set()
        for record in self.run_store.read_records():
            if record.get("record_type") != "workspace_claim":
                continue
            if record.get("task_id") != task_id:
                continue
            branch = record.get("branch")
            raw_worktree = record.get("worktree")
            if not isinstance(branch, str) or not isinstance(raw_worktree, str):
                continue
            identity = (branch, Path(raw_worktree).resolve())
            if identity in listed:
                candidates.add(identity)
        if len(candidates) > 1:
            raise WorkspaceProvisionError(
                "ambiguous_owned_workspaces",
                "multiple existing workspaces have ownership records for the task",
                details={
                    "workspaces": [
                        {"branch": branch, "worktree": str(worktree)}
                        for branch, worktree in sorted(
                            candidates,
                            key=lambda item: (item[0], str(item[1])),
                        )
                    ]
                },
            )
        return next(iter(candidates), None)

    def _compensate_partial_creation(
        self,
        *,
        branch: str,
        worktree: Path,
        base_commit: str,
    ) -> None:
        from vibe_loop.workers import build_workspace_git_context

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        exact = [
            entry
            for entry in context.worktrees
            if entry.path == worktree.resolve() and entry.branch == branch
        ]
        if exact:
            remove = self._git_result("worktree", "remove", "--force", str(worktree))
            if remove.returncode != 0:
                raise WorkspaceProvisionError(
                    "partial_workspace_compensation_failed",
                    "git could not remove a partially created workspace",
                    details={"stderr": remove.stderr.strip()},
                )
        elif worktree.exists():
            raise WorkspaceProvisionError(
                "workspace_collision",
                "an unverified path appeared while creating the workspace; "
                "it was preserved",
                details={"branch": branch, "worktree": str(worktree)},
            )
        branch_ref = f"refs/heads/{branch}"
        if self._git_returncode("show-ref", "--verify", "--quiet", branch_ref) == 0:
            branch_head = self._git_text("rev-parse", "--verify", branch_ref)
            if branch_head != base_commit:
                raise WorkspaceProvisionError(
                    "partial_branch_changed",
                    "refusing to remove a partially created branch that changed",
                    details={"branch": branch},
                )
            delete = self._git_result("branch", "-D", branch)
            if delete.returncode != 0:
                raise WorkspaceProvisionError(
                    "partial_branch_compensation_failed",
                    "git could not remove a partially created branch",
                    details={"stderr": delete.stderr.strip()},
                )

    def _ownership_record(
        self,
        *,
        task_id: str,
        branch: str,
        worktree: Path,
        recovery_run_id: str,
    ) -> Mapping[str, object] | None:
        for record in reversed(self.run_store.read_records()):
            if record.get("record_type") != "workspace_claim":
                continue
            if record.get("branch") != branch:
                continue
            raw_worktree = record.get("worktree")
            if not isinstance(raw_worktree, str):
                continue
            if Path(raw_worktree).resolve() != worktree.resolve():
                continue
            owner_task_id = str(record.get("task_id") or "")
            owner_run_id = str(record.get("run_id") or "")
            if owner_task_id != task_id or (
                recovery_run_id and owner_run_id != recovery_run_id
            ):
                raise WorkspaceProvisionError(
                    "workspace_foreign_owner",
                    "the latest ownership record belongs to another task or run",
                    details={
                        "owner_task_id": owner_task_id,
                        "owner_run_id": owner_run_id,
                    },
                )
            return record
        return None

    def _reject_live_foreign_claim(
        self,
        *,
        task_id: str,
        run_id: str,
        branch: str,
        worktree: Path,
    ) -> None:
        from vibe_loop.workers import load_active_run_states

        for active in load_active_run_states(self.lock_manager):
            claim = active.workspace
            if claim is None or (active.task_id == task_id and active.run_id == run_id):
                continue
            if claim.branch == branch or claim.worktree.resolve() == worktree.resolve():
                raise WorkspaceProvisionError(
                    "workspace_live_owner",
                    "existing workspace is claimed by another active run",
                    details={
                        "owner_task_id": active.task_id,
                        "owner_run_id": active.run_id,
                    },
                )

    def _git(self, *args: str) -> None:
        result = self._git_result(*args)
        if result.returncode != 0:
            raise WorkspaceProvisionError(
                "git_command_failed",
                "git workspace operation failed",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )

    def _git_text(self, *args: str) -> str:
        return self._git_text_at(self.repo, *args)

    def _git_text_at(self, cwd: Path, *args: str) -> str:
        result = self._git_result_at(cwd, *args)
        if result.returncode != 0:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "git workspace state could not be read",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )
        return result.stdout.strip()

    def _git_common_dir_at(self, worktree: Path) -> Path:
        raw = Path(self._git_text_at(worktree, "rev-parse", "--git-common-dir"))
        if not raw.is_absolute():
            raw = worktree / raw
        return raw.resolve()

    def _git_returncode(self, *args: str) -> int:
        return self._git_returncode_at(self.repo, *args)

    def _git_returncode_at(self, cwd: Path, *args: str) -> int:
        return self._git_result_at(cwd, *args).returncode

    def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._git_result_at(self.repo, *args)

    @staticmethod
    def _git_result_at(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "git could not be executed for workspace provisioning",
                details={"error": str(exc)},
            ) from exc


def workspace_name(task_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-").lower()
    if not normalized:
        normalized = "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    prefix_limit = WORKSPACE_NAME_MAX_LENGTH - len(digest) - 1
    return f"{normalized[:prefix_limit]}-{digest}"


def overlay_explicit_orchestration(
    base: OrchestrationConfig,
    override: OrchestrationConfig,
) -> OrchestrationConfig:
    values = {
        field.name: getattr(override, field.name)
        for field in dataclasses.fields(OrchestrationConfig)
        if field.name in override.explicit_keys
    }
    return dataclasses.replace(
        base,
        **values,
        explicit_keys=base.explicit_keys | override.explicit_keys,
    )


def config_source_identity(
    config: VibeConfig,
    effective: OrchestrationConfig,
) -> dict[str, str]:
    source_id = (
        str(config.config_path) if config.config_path is not None else "defaults"
    )
    if config.config_digest:
        digest = config.config_digest
    else:
        digest_input = {
            "orchestration": effective.to_json(),
            "completion_command_keys": [
                f"completion.commands[{index}]"
                for index, _ in enumerate(config.completion.commands)
            ],
            "agent_profile_keys": sorted(config.agent_profiles),
        }
        digest = sha256_digest(digest_input)
    return {"kind": "config", "id": source_id, "digest": digest}


def route_payload(agent: AgentConfig, profile: str) -> dict[str, object]:
    provider = agent_command_provider(
        agent.command or "",
        agent.executable_kind or agent.agent_kind,
    )
    command_key = f"agent.profiles.{profile}.command" if profile else "agent.command"
    return {
        "profile": profile,
        "provider": provider or "unknown",
        "model": agent.model,
        "effort": agent.effort,
        "command_key": command_key,
    }


def sha256_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def is_sha256_digest(value: str) -> bool:
    prefix, separator, digest = value.partition(":")
    return (
        prefix == "sha256"
        and separator == ":"
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )
