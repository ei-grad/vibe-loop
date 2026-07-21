from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from vibe_loop.config import (
    AgentConfig,
    AgentSelection,
    OrchestrationConfig,
    VibeConfig,
    agent_command_provider,
    parse_orchestration,
)


RUN_CONTRACT_VERSION = 1
RUN_CONTRACT_SOURCE_KINDS = ("config", "profile", "skill-proposal")


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
        reviewer_profile = effective.reviewer_profile
        if reviewer_profile is None:
            reviewer_agent = agent_selection.config
            reviewer_profile = agent_selection.profile
        else:
            reviewer_agent = self.config.agent_profiles[reviewer_profile]

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
                    "park_adapter": None,
                },
            },
            "remediation": {"max_rounds": effective.max_remediation_rounds},
        }
        return ResolvedRunContract(payload=payload, digest=sha256_digest(payload))


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
