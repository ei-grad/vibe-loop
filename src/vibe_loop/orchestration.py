from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping

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
