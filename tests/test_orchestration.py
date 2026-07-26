from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import vibe_loop.runner as runner_module
import vibe_loop.workers as workers_module
from vibe_loop.config import (
    AgentConfig,
    AgentResolutionError,
    AgentSelection,
    CompletionConfig,
    OrchestrationConfig,
    TaskSourceConfig,
    VibeConfig,
    load_config,
    reject_generated_command_adapters,
)
from vibe_loop.orchestration import (
    CandidateBaseReanchorer,
    CandidateRecord,
    CandidateCollectionError,
    CandidateCollector,
    CandidateReanchorRetryExhausted,
    GateExecutionError,
    GateRemediationExhausted,
    GateResult,
    GateRunSummary,
    GateRunner,
    Integrator,
    IntegrationResult,
    LEGAL_STAGE_TRANSITIONS,
    RuntimeGateController,
    ReviewBudgetExhausted,
    ReviewConcurrencyBudget,
    ReviewDelegationPolicyError,
    ReviewExecutionError,
    ReviewFinding,
    ReviewLimitWallError,
    ReviewOutputMalformed,
    ReviewRouter,
    ReviewStageResultError,
    ReviewWaitIncomplete,
    STAGE_FAILURES,
    IllegalStageTransitionError,
    ProvisionedWorkspace,
    RunContractProposal,
    RunContractResolver,
    RunLifecycleStateMachine,
    RunStage,
    StageFailure,
    TaskSourceCompleter,
    TaskSourceCompletionError,
    TaskSourceSettler,
    WorkspaceProvisionError,
    WorkspaceProvisioner,
    bound_malformed_review_output,
    derive_stage_progress,
    inject_provider_continuation,
    plan_session_continuation,
    provider_capabilities,
)
from vibe_loop.runner import VibeRunner
from vibe_loop.locks import (
    MAIN_INTEGRATION_LOCK_NAME,
    LockFencingMismatch,
    LockManager,
    fencing_token_value,
)
from vibe_loop.runs import RunLifecycleEvent, RunResult, RunStore, WorkerReport
from vibe_loop import tasks as tasks_module
from vibe_loop.tasks import CommandTaskSource, Task, build_task_source
from vibe_loop.workers import claim_worker_workspace, git_dirty_snapshot


class OrchestrationConfigTests(unittest.TestCase):
    def test_defaults_select_runtime_owned_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertEqual(config.orchestration.mode, "runtime-owned")
        self.assertEqual(config.orchestration.gates, ())
        self.assertEqual(config.orchestration.verify_on_main, ())
        self.assertEqual(config.orchestration.max_candidate_reanchors, 2)
        self.assertTrue(config.orchestration.integration_enabled)
        self.assertEqual(
            config.orchestration.task_provenance_mode,
            "external-confirmed",
        )
        self.assertIsNone(config.orchestration.external_completion_actor)

    def test_parses_typed_allowlisted_contract_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[agent.profiles.review]\n"
                'kind = "codex"\n'
                'command = "codex review {prompt}"\n'
                "\n[completion]\n"
                'commands = ["uv run -m pytest", "uv run ruff check"]\n'
                "\n[orchestration]\n"
                'mode = "worker-owned"\n'
                'reviewer_profile = "review"\n'
                'gates = ["completion.commands[0]"]\n'
                'verify_on_main = ["completion.commands[1]"]\n'
                "max_initial_review_passes = 2\n"
                "max_closure_review_passes = 3\n"
                "reviewer_concurrency_budget = 2\n"
                "max_remediation_rounds = 4\n"
                "max_candidate_reanchors = 3\n"
                "integration_enabled = false\n"
                'task_provenance_mode = "external-confirmed"\n'
                'external_completion_actor = "operator"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.orchestration.reviewer_profile, "review")
        self.assertEqual(config.orchestration.gates, ("completion.commands[0]",))
        self.assertEqual(
            config.orchestration.verify_on_main,
            ("completion.commands[1]",),
        )
        self.assertEqual(config.orchestration.max_initial_review_passes, 2)
        self.assertEqual(config.orchestration.max_closure_review_passes, 3)
        self.assertEqual(config.orchestration.reviewer_concurrency_budget, 2)
        self.assertEqual(config.orchestration.max_remediation_rounds, 4)
        self.assertEqual(config.orchestration.max_candidate_reanchors, 3)
        self.assertFalse(config.orchestration.integration_enabled)
        self.assertEqual(config.orchestration.external_completion_actor, "operator")

    def test_accepts_runtime_owned_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[orchestration]\nmode = "runtime-owned"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.orchestration.mode, "runtime-owned")

    def test_rejects_invalid_modes_routes_and_non_allowlisted_executables(
        self,
    ) -> None:
        cases = (
            ('mode = "other"\n', "orchestration.mode must be one of"),
            ('mode = ""\n', "orchestration.mode must be one of"),
            (
                'reviewer_profile = "missing"\n',
                "must reference a configured agent.profiles entry",
            ),
            (
                'gates = ["uv run -m pytest"]\n',
                "allowlisted completion.commands",
            ),
            (
                'gates = ["completion.commands[9]"]\n',
                "references unconfigured command key",
            ),
            (
                'task_provenance_mode = ""\n',
                "orchestration.task_provenance_mode must be one of",
            ),
            (
                'external_completion_actor = ""\n',
                "orchestration.external_completion_actor must be one of",
            ),
        )
        for settings, diagnostic in cases:
            with self.subTest(settings=settings):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / ".vibe-loop.toml").write_text(
                        '[completion]\ncommands = ["check"]\n\n'
                        "[orchestration]\n" + settings,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, diagnostic):
                        load_config(repo)

    def test_generated_profiles_cannot_introduce_orchestration_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "profile.orchestration"):
            reject_generated_command_adapters(
                {"orchestration": {"mode": "worker-owned"}}
            )
        with self.assertRaisesRegex(ValueError, "profile.max_remediation_rounds"):
            reject_generated_command_adapters({"max_remediation_rounds": 9})


class RunContractResolverTests(unittest.TestCase):
    def test_runtime_contract_rejects_exact_codex_review_route_before_activation(
        self,
    ) -> None:
        implementer = AgentConfig(
            command="claude -p {prompt}", agent_kind="claude", profile_name="impl"
        )
        reviewer = AgentConfig(
            command="codex review {prompt}",
            command_source="explicit",
            model="gpt-5.6-terra",
            model_source="explicit",
            effort="xhigh",
            effort_source="explicit",
            agent_kind="codex",
            executable_kind="codex",
            profile_name="review",
        )
        config = VibeConfig(
            repo=Path("/repo"),
            agent=implementer,
            agent_profiles={"review": reviewer},
            orchestration=OrchestrationConfig(
                mode="runtime-owned",
                reviewer_profile="review",
                task_provenance_mode="external-confirmed",
                external_completion_actor="external-system",
                explicit_keys=frozenset(
                    {
                        "mode",
                        "reviewer_profile",
                        "task_provenance_mode",
                        "external_completion_actor",
                    }
                ),
            ),
            task_source=TaskSourceConfig(type="markdown-plan"),
        )

        with self.assertRaisesRegex(
            AgentResolutionError,
            "cannot safely receive.*no supported effective-route metadata",
        ):
            RunContractResolver(config).resolve(
                AgentSelection(implementer, "impl", "profile")
            )

        unbound = dataclasses.replace(
            reviewer,
            model=None,
            model_source="default:none",
            effort=None,
            effort_source="default:none",
        )
        contract = RunContractResolver(
            dataclasses.replace(config, agent_profiles={"review": unbound})
        ).resolve(AgentSelection(implementer, "impl", "profile"))
        reviewer_route = contract.payload["reviewer"]
        assert isinstance(reviewer_route, dict)
        self.assertEqual(reviewer_route["provider"], "codex")
        self.assertIsNone(reviewer_route["model"])
        self.assertIsNone(reviewer_route["effort"])

    def test_worker_owned_contract_keeps_placeholder_validation(self) -> None:
        implementer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        reviewer = AgentConfig(
            command="codex review {prompt}",
            command_source="explicit",
            model="gpt-5.6-terra",
            effort="xhigh",
            agent_kind="codex",
            executable_kind="codex",
            profile_name="review",
        )
        config = VibeConfig(
            repo=Path("/repo"),
            agent=implementer,
            agent_profiles={"review": reviewer},
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                reviewer_profile="review",
                explicit_keys=frozenset({"mode", "reviewer_profile"}),
            ),
        )

        with self.assertRaisesRegex(AgentResolutionError, "cannot receive"):
            RunContractResolver(config).resolve(
                AgentSelection(implementer, "", "default")
            )

    def test_explicit_config_wins_over_profile_and_profile_wins_over_skill(
        self,
    ) -> None:
        implementer = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        reviewer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=implementer,
            agent_profiles={"review": reviewer},
            completion=CompletionConfig(commands=("test", "lint")),
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                reviewer_profile="review",
                max_remediation_rounds=7,
                explicit_keys=frozenset(
                    {"mode", "reviewer_profile", "max_remediation_rounds"}
                ),
            ),
        )
        skill = RunContractProposal(
            kind="skill-proposal",
            source_id="skill:v1",
            values={
                "max_remediation_rounds": 1,
                "max_closure_review_passes": 1,
            },
        )
        profile = RunContractProposal(
            kind="profile",
            source_id="profile:v2",
            values={
                "max_remediation_rounds": 2,
                "max_closure_review_passes": 3,
            },
        )

        contract = RunContractResolver(config).resolve(
            AgentSelection(implementer, "", "default"),
            profile=profile,
            skill_proposal=skill,
        )

        self.assertEqual(contract.payload["remediation"], {"max_rounds": 7})
        self.assertEqual(
            contract.payload["candidate_stabilization"],
            {"max_reanchors": 2},
        )
        reviewer_payload = contract.payload["reviewer"]
        assert isinstance(reviewer_payload, dict)
        self.assertEqual(reviewer_payload["max_closure_passes"], 3)
        self.assertEqual(reviewer_payload["profile"], "review")
        source = contract.payload["source"]
        assert isinstance(source, dict)
        self.assertEqual(source["kind"], "config")
        self.assertEqual(
            [item["kind"] for item in source["inputs"]],
            ["skill-proposal", "profile", "config"],
        )

    def test_contract_contains_only_command_identities_and_stable_digests(self) -> None:
        command_canary = "codex exec --token secret-command-canary {prompt}"
        gate_canary = "uv run secret-gate-canary"
        agent = AgentConfig(command=command_canary, agent_kind="codex")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=agent,
            completion=CompletionConfig(commands=(gate_canary,)),
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                gates=("completion.commands[0]",),
                verify_on_main=("completion.commands[0]",),
                explicit_keys=frozenset({"mode", "gates", "verify_on_main"}),
            ),
        )
        selection = AgentSelection(agent, "", "default")

        first = RunContractResolver(config).resolve(selection)
        second = RunContractResolver(config).resolve(selection)
        encoded = json.dumps(first.to_record_payload(), sort_keys=True)

        self.assertEqual(first.digest, second.digest)
        self.assertTrue(first.digest.startswith("sha256:"))
        self.assertNotIn(command_canary, encoded)
        self.assertNotIn(gate_canary, encoded)
        self.assertIn('"command_key": "agent.command"', encoded)
        self.assertIn('"command_key": "completion.commands[0]"', encoded)

    def test_config_digest_and_payload_use_the_loaded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config_path = repo / ".vibe-loop.toml"
            original = (
                "[orchestration]\n"
                'mode = "worker-owned"\n'
                "max_remediation_rounds = 1\n"
                "max_closure_review_passes = 1\n"
            )
            config_path.write_text(original, encoding="utf-8")
            config = load_config(repo)
            config_path.write_text(
                "[orchestration]\nmax_remediation_rounds = 9\n",
                encoding="utf-8",
            )

            contract = RunContractResolver(config).resolve(
                AgentSelection(config.agent, "", "default")
            )

        source = contract.payload["source"]
        assert isinstance(source, dict)
        self.assertEqual(contract.payload["remediation"], {"max_rounds": 1})
        self.assertEqual(
            source["digest"],
            "sha256:" + hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )

    def test_proposal_digest_accepts_non_dict_mapping(self) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=agent,
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                explicit_keys=frozenset({"mode"}),
            ),
        )
        proposal = RunContractProposal(
            kind="profile",
            source_id="profile:mapping",
            values=MappingProxyType({"max_remediation_rounds": 4}),
        )

        contract = RunContractResolver(config).resolve(
            AgentSelection(agent, "", "default"),
            profile=proposal,
        )

        self.assertEqual(contract.payload["remediation"], {"max_rounds": 4})
        source = contract.payload["source"]
        assert isinstance(source, dict)
        self.assertTrue(str(source["digest"]).startswith("sha256:"))

    def test_runtime_owned_contract_requires_declared_completion_and_settlement(
        self,
    ) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        reviewer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        cases = (
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    explicit_keys=frozenset({"mode", "reviewer_profile"}),
                ),
                TaskSourceConfig(),
                "explicit.*task_provenance_mode",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="adapter",
                    explicit_keys=frozenset(
                        {"mode", "reviewer_profile", "task_provenance_mode"}
                    ),
                ),
                TaskSourceConfig(),
                "requires task_source.complete",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="adapter",
                    explicit_keys=frozenset(
                        {"mode", "reviewer_profile", "task_provenance_mode"}
                    ),
                ),
                TaskSourceConfig(
                    type="markdown-plan",
                    complete_command="complete {task_id}",
                ),
                "task_source.complete on a command task source",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="adapter",
                    external_completion_actor="operator",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "task_provenance_mode",
                            "external_completion_actor",
                        }
                    ),
                ),
                TaskSourceConfig(complete_command="complete {task_id}"),
                "external_completion_actor is only valid",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="external-confirmed",
                    external_completion_actor="operator",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "task_provenance_mode",
                            "external_completion_actor",
                        }
                    ),
                ),
                TaskSourceConfig(
                    type="command",
                    list_command="list",
                    probe_command="probe {task_id}",
                    activate_command="activate {task_id}",
                ),
                "requires task_source.reset",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="external-confirmed",
                    explicit_keys=frozenset(
                        {"mode", "reviewer_profile", "task_provenance_mode"}
                    ),
                ),
                TaskSourceConfig(type="markdown-plan"),
                "requires an explicit.*external_completion_actor",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="external-confirmed",
                    external_completion_actor="operator",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "task_provenance_mode",
                            "external_completion_actor",
                        }
                    ),
                ),
                TaskSourceConfig(type="command"),
                "requires a task source with probe capability",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="external-confirmed",
                    external_completion_actor="worker",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "task_provenance_mode",
                            "external_completion_actor",
                        }
                    ),
                ),
                TaskSourceConfig(type="markdown-plan"),
                "workers are forbidden from transitioning",
            ),
        )
        for orchestration, task_source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                config = VibeConfig(
                    repo=Path("/repo"),
                    agent=agent,
                    agent_profiles={"review": reviewer},
                    orchestration=orchestration,
                    task_source=task_source,
                )
                with self.assertRaisesRegex(ValueError, diagnostic):
                    RunContractResolver(config).resolve(
                        AgentSelection(agent, "", "default")
                    )

    def test_runtime_owned_contract_requires_independent_reviewer_profile(
        self,
    ) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        task_source = TaskSourceConfig()
        for orchestration, selection, diagnostic in (
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    task_provenance_mode="external-confirmed",
                    explicit_keys=frozenset({"mode", "task_provenance_mode"}),
                ),
                AgentSelection(agent, "", "default"),
                "requires an explicit independent.*reviewer_profile",
            ),
            (
                OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="implementer",
                    task_provenance_mode="external-confirmed",
                    explicit_keys=frozenset(
                        {"mode", "reviewer_profile", "task_provenance_mode"}
                    ),
                ),
                AgentSelection(agent, "implementer", "task.agent"),
                "reviewer_profile must differ from the implementer profile",
            ),
        ):
            with self.subTest(diagnostic=diagnostic):
                config = VibeConfig(
                    repo=Path("/repo"),
                    agent=agent,
                    agent_profiles={"implementer": agent},
                    orchestration=orchestration,
                    task_source=task_source,
                )
                with self.assertRaisesRegex(ValueError, diagnostic):
                    RunContractResolver(config).resolve(selection)

    def test_runtime_owned_contract_records_allowlisted_source_adapters(self) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        reviewer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=agent,
            agent_profiles={"review": reviewer},
            orchestration=OrchestrationConfig(
                mode="runtime-owned",
                reviewer_profile="review",
                task_provenance_mode="adapter",
                explicit_keys=frozenset(
                    {"mode", "reviewer_profile", "task_provenance_mode"}
                ),
            ),
            task_source=TaskSourceConfig(
                type="command",
                list_command="list",
                activate_command="activate {task_id}",
                complete_command="complete {task_id}",
                reset_command="reset {task_id}",
                park_command="park {task_id}",
            ),
        )

        contract = RunContractResolver(config).resolve(
            AgentSelection(agent, "", "default")
        )

        self.assertEqual(
            contract.payload["task_provenance"],
            {
                "mode": "adapter",
                "complete_adapter": "task_source.complete",
                "confirmation_adapter": "task_source.complete",
                "transition_actor": "runtime",
                "settlement": {
                    "requeue_adapter": "task_source.reset",
                    "park_adapter": "task_source.park",
                },
            },
        )

    def test_external_confirmation_accepts_native_markdown_probe(self) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        reviewer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | "
                "Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P1 | Done | none | Native source. | Works. | Test. |\n",
                encoding="utf-8",
            )
            task_source = TaskSourceConfig(type="markdown-plan")
            config = VibeConfig(
                repo=repo,
                agent=agent,
                agent_profiles={"review": reviewer},
                orchestration=OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    task_provenance_mode="external-confirmed",
                    external_completion_actor="operator",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "task_provenance_mode",
                            "external_completion_actor",
                        }
                    ),
                ),
                task_source=task_source,
            )

            contract = RunContractResolver(config).resolve(
                AgentSelection(agent, "", "default")
            )
            confirmed = build_task_source(repo, task_source).probe("TASK-01")

        self.assertEqual(
            contract.payload["task_provenance"]["confirmation_adapter"],
            "task_source.probe",
        )
        self.assertIsNotNone(confirmed)
        assert confirmed is not None
        self.assertTrue(confirmed.done)

    def test_worker_owned_contract_omits_runtime_confirmation_evidence(self) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=agent,
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                explicit_keys=frozenset({"mode"}),
            ),
            task_source=TaskSourceConfig(
                type="command",
                list_command="list",
                complete_command="complete {task_id}",
            ),
        )

        contract = RunContractResolver(config).resolve(
            AgentSelection(agent, "", "default")
        )

        self.assertEqual(
            contract.payload["task_provenance"],
            {
                "mode": "external-confirmed",
                "complete_adapter": "task_source.complete",
                "settlement": {
                    "requeue_adapter": None,
                    "park_adapter": None,
                },
            },
        )
        with self.assertRaisesRegex(
            ValueError,
            "external_completion_actor is only valid.*runtime-owned",
        ):
            RunContractResolver(
                dataclasses.replace(
                    config,
                    orchestration=OrchestrationConfig(
                        mode="worker-owned",
                        external_completion_actor="worker",
                        explicit_keys=frozenset({"mode", "external_completion_actor"}),
                    ),
                )
            ).resolve(AgentSelection(agent, "", "default"))

    def test_external_confirmation_preserves_available_complete_adapter(self) -> None:
        agent = AgentConfig(command="codex exec {prompt}", agent_kind="codex")
        reviewer = AgentConfig(command="claude -p {prompt}", agent_kind="claude")
        config = VibeConfig(
            repo=Path("/repo"),
            agent=agent,
            agent_profiles={"review": reviewer},
            orchestration=OrchestrationConfig(
                mode="runtime-owned",
                reviewer_profile="review",
                task_provenance_mode="external-confirmed",
                external_completion_actor="external-system",
                explicit_keys=frozenset(
                    {
                        "mode",
                        "reviewer_profile",
                        "task_provenance_mode",
                        "external_completion_actor",
                    }
                ),
            ),
            task_source=TaskSourceConfig(
                type="command",
                list_command="list",
                complete_command="complete {task_id}",
            ),
        )

        contract = RunContractResolver(config).resolve(
            AgentSelection(agent, "", "default")
        )

        self.assertEqual(
            contract.payload["task_provenance"]["complete_adapter"],
            "task_source.complete",
        )
        self.assertEqual(
            contract.payload["task_provenance"]["confirmation_adapter"],
            "task_source.probe",
        )

    def test_default_runtime_owned_contract_records_provider_role_matrix(self) -> None:
        providers = {
            "codex": AgentConfig(
                command="codex exec {prompt}",
                agent_kind="codex",
            ),
            "claude": AgentConfig(
                command="claude -p {prompt}",
                agent_kind="claude",
            ),
        }
        for implementer_kind, reviewer_kind in (
            ("codex", "codex"),
            ("codex", "claude"),
            ("claude", "codex"),
            ("claude", "claude"),
        ):
            with self.subTest(
                implementer=implementer_kind,
                reviewer=reviewer_kind,
            ):
                implementer = providers[implementer_kind]
                reviewer = providers[reviewer_kind]
                config = VibeConfig(
                    repo=Path("/repo"),
                    agent=implementer,
                    agent_profiles={"review": reviewer},
                    orchestration=OrchestrationConfig(
                        reviewer_profile="review",
                        task_provenance_mode="external-confirmed",
                        external_completion_actor="external-system",
                        explicit_keys=frozenset(
                            {
                                "reviewer_profile",
                                "task_provenance_mode",
                                "external_completion_actor",
                            }
                        ),
                    ),
                    task_source=TaskSourceConfig(type="markdown-plan"),
                )

                contract = RunContractResolver(config).resolve(
                    AgentSelection(implementer, "", "default")
                )

                self.assertEqual(contract.payload["mode"], "runtime-owned")
                self.assertEqual(
                    contract.payload["implementer"]["provider"], implementer_kind
                )
                self.assertEqual(
                    contract.payload["reviewer"]["provider"], reviewer_kind
                )
                self.assertEqual(
                    contract.payload["task_provenance"]["mode"],
                    "external-confirmed",
                )
                self.assertEqual(
                    contract.payload["task_provenance"]["transition_actor"],
                    "external-system",
                )
                self.assertEqual(
                    contract.payload["task_provenance"]["confirmation_adapter"],
                    "task_source.probe",
                )


class RunLifecycleStateMachineTests(unittest.TestCase):
    @staticmethod
    def restored(stage: RunStage, records: list[dict[str, object]]):
        return RunLifecycleStateMachine.from_records(
            [
                {
                    "record_type": "stage_transition",
                    "from_stage": "",
                    "to_stage": stage.value,
                    "reason": "restore",
                    "ordinal": 1,
                    "accepted": True,
                }
            ],
            lambda transition: records.append(
                {"record_type": "stage_transition", **transition.to_payload()}
            ),
        )

    def test_every_legal_transition_is_accepted(self) -> None:
        initial_records: list[dict[str, object]] = []
        initial = RunLifecycleStateMachine(
            lambda transition: initial_records.append(transition.to_payload())
        )
        initial.transition(RunStage.ACTIVATION, reason="start")
        self.assertEqual(initial.stage, RunStage.ACTIVATION)

        for source, destinations in LEGAL_STAGE_TRANSITIONS.items():
            for destination in destinations:
                with self.subTest(source=source, destination=destination):
                    records: list[dict[str, object]] = []
                    machine = self.restored(source, records)
                    transition = machine.transition(destination, reason="test")
                    self.assertTrue(transition.accepted)
                    self.assertEqual(machine.stage, destination)

    def test_every_typed_failure_is_accepted_from_every_stage(self) -> None:
        self.assertEqual(
            set(STAGE_FAILURES),
            {"limit_wall", "timed_out", "stage_failed", "blocked", "cancelled"},
        )
        for stage in RunStage:
            for failure in StageFailure:
                with self.subTest(stage=stage, failure=failure):
                    records: list[dict[str, object]] = []
                    machine = self.restored(stage, records)
                    transition = machine.fail(failure, reason="test failure")
                    self.assertTrue(transition.accepted)
                    self.assertEqual(transition.failure, failure)
                    expected = (
                        RunStage.FINALIZATION
                        if stage in {RunStage.CLASSIFICATION, RunStage.FINALIZATION}
                        else RunStage.CLASSIFICATION
                    )
                    self.assertEqual(machine.stage, expected)

    def test_gate_failure_can_remediate_and_repeat_candidate_gates(self) -> None:
        records: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: records.append(transition.to_payload())
        )
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.REMEDIATION,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.CLOSURE,
        ):
            machine.transition(stage, reason="gate remediation")

        self.assertEqual(machine.stage, RunStage.CLOSURE)
        self.assertEqual(
            [record["ordinal"] for record in records if record["to_stage"] == "gates"],
            [1, 2],
        )

    def test_illegal_transition_is_journaled_before_typed_error(self) -> None:
        records: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: records.append(
                {"record_type": "stage_transition", **transition.to_payload()}
            )
        )
        machine.transition(RunStage.ACTIVATION, reason="start")

        with self.assertRaises(IllegalStageTransitionError) as raised:
            machine.transition(RunStage.REVIEW, reason="skip owners")

        self.assertEqual(raised.exception.from_stage, RunStage.ACTIVATION)
        self.assertEqual(raised.exception.to_stage, RunStage.REVIEW)
        self.assertFalse(records[-1]["accepted"])
        self.assertEqual(records[-1]["to_stage"], "review")
        self.assertEqual(machine.stage, RunStage.ACTIVATION)

    def test_journal_append_precedes_in_memory_transition(self) -> None:
        observed_stages: list[RunStage | None] = []
        machine: RunLifecycleStateMachine

        def journal(_transition) -> None:
            observed_stages.append(machine.stage)

        machine = RunLifecycleStateMachine(journal)
        machine.transition(RunStage.ACTIVATION, reason="start")
        machine.transition(RunStage.WORKSPACE, reason="activated")

        self.assertEqual(observed_stages, [None, RunStage.ACTIVATION])

    def test_state_reconstructs_after_each_recorded_boundary(self) -> None:
        records: list[dict[str, object]] = []

        def journal(transition) -> None:
            records.append(
                {
                    "record_type": "stage_transition",
                    "occurred_at": f"boundary-{len(records) + 1}",
                    **transition.to_payload(),
                }
            )

        machine = RunLifecycleStateMachine(journal)
        path = (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.REVIEW,
            RunStage.REMEDIATION,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.CLOSURE,
            RunStage.INTEGRATION,
            RunStage.PROVENANCE,
            RunStage.CLASSIFICATION,
            RunStage.FINALIZATION,
        )

        for expected in path:
            machine.transition(expected, reason="boundary")
            recovered = RunLifecycleStateMachine.from_records(records, lambda _: None)
            progress = derive_stage_progress(records)
            self.assertEqual(recovered.stage, expected)
            self.assertIsNotNone(progress)
            assert progress is not None
            self.assertEqual(progress.stage, expected)
            self.assertEqual(progress.ordinal, recovered.ordinal)
            self.assertEqual(progress.occurred_at, f"boundary-{len(records)}")

        candidate_records = [
            record for record in records if record["to_stage"] == "candidate"
        ]
        gate_records = [record for record in records if record["to_stage"] == "gates"]
        self.assertEqual([record["ordinal"] for record in candidate_records], [1, 2])
        self.assertEqual([record["ordinal"] for record in gate_records], [1, 2])


class RuntimeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        init_git_repo(self.repo)
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "checkout", "-b", "worker/task-01")
        (self.repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-m", "candidate")
        self.head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.store = RunStore(self.repo / ".vibe-loop" / "runs.jsonl")
        self.collector = CandidateCollector(
            worktree=self.repo,
            branch="worker/task-01",
            base_main=self.base,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
        )

    def test_candidate_declaration_and_derivation_are_validated_and_recorded(
        self,
    ) -> None:
        declared = self.collector.collect_declared(
            head_commit=self.head,
            base_main=self.base,
            changed_paths=("tracked.txt",),
        )
        derived = self.collector.collect_derived()

        self.assertEqual(declared.head_commit, self.head)
        self.assertEqual(declared.changed_paths, ("tracked.txt",))
        self.assertEqual(declared.source, "worker_command")
        self.assertEqual(derived.source, "derived")
        self.assertEqual(declared.fingerprint, derived.fingerprint)
        records = self.store.read_records()
        self.assertEqual(
            [record["record_type"] for record in records],
            ["candidate_recorded", "candidate_recorded"],
        )

        with self.assertRaisesRegex(
            CandidateCollectionError, "does not match the claimed workspace"
        ):
            self.collector.collect_declared(head_commit=self.base)

    def test_candidate_declaration_resolves_revisions_before_comparing(self) -> None:
        for revision in ("HEAD", self.head[:8], self.head):
            with self.subTest(revision=revision):
                declared = self.collector.collect_declared(
                    head_commit=revision, base_main="HEAD~1"
                )
                self.assertEqual(declared.head_commit, self.head)
                self.assertEqual(declared.base_main, self.base)

    def test_candidate_declaration_of_other_commit_is_still_a_mismatch(self) -> None:
        with self.assertRaisesRegex(
            CandidateCollectionError, "does not match the claimed workspace"
        ):
            self.collector.collect_declared(head_commit="HEAD~1")

        with self.assertRaisesRegex(
            CandidateCollectionError, "does not match the claimed workspace"
        ):
            self.collector.collect_declared(head_commit="HEAD", base_main=self.head)

    def test_candidate_declaration_of_unresolvable_revision_is_named(self) -> None:
        with self.assertRaises(CandidateCollectionError) as raised:
            self.collector.collect_declared(head_commit="no-such-revision")

        self.assertEqual(
            raised.exception.code, "candidate_declaration_unresolvable_revision"
        )
        self.assertEqual(raised.exception.details["field"], "head_commit")

        with self.assertRaises(CandidateCollectionError) as raised:
            self.collector.collect_declared(
                head_commit="HEAD", base_main="no-such-revision"
            )

        self.assertEqual(
            raised.exception.code, "candidate_declaration_unresolvable_revision"
        )
        self.assertEqual(raised.exception.details["field"], "base_main")

    def test_worker_prompt_template_candidate_command_is_accepted(self) -> None:
        from vibe_loop.runner import RUNTIME_OWNED_WORKER_ADDENDUM

        templated = re.search(
            r"vibe-loop worker candidate\b[^`]*?--head (\S+)",
            RUNTIME_OWNED_WORKER_ADDENDUM,
        )
        self.assertIsNotNone(templated, "worker prompt no longer declares a candidate")
        assert templated is not None

        declared = self.collector.collect_declared(head_commit=templated.group(1))

        self.assertEqual(declared.head_commit, self.head)

    def test_candidate_rejects_uncommitted_tracked_changes(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(
            CandidateCollectionError, "uncommitted tracked changes"
        ):
            self.collector.collect_derived()

    def test_candidate_fingerprint_uses_only_head_and_changed_paths(self) -> None:
        candidate = self.collector.collect_derived()
        relocated = dataclasses.replace(
            candidate,
            branch="worker/other",
            worktree=self.repo / "other",
            base_main="f" * 40,
        )

        self.assertEqual(candidate.fingerprint, relocated.fingerprint)
        self.assertNotEqual(
            candidate.fingerprint,
            dataclasses.replace(candidate, changed_paths=("other.txt",)).fingerprint,
        )

    def test_gate_records_redacted_evidence_and_invalidates_mutating_gate(
        self,
    ) -> None:
        command_canary = "printf 'mutated\\n' >> tracked.txt"
        candidate = self.collector.collect_derived()
        runner = GateRunner(
            completion_commands=(command_canary,),
            gate_keys=("completion.commands[0]",),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
        )

        summary = runner.run(candidate)

        self.assertFalse(summary.passed)
        self.assertEqual(summary.results[0].exit_class, "candidate_changed")
        with self.assertRaises(GateExecutionError):
            summary.require_review_ready()
        gate_record = self.store.read_records()[-1]
        self.assertEqual(gate_record["command_key"], "completion.commands[0]")
        self.assertNotIn(command_canary, json.dumps(gate_record))

    def test_gate_detects_tracked_mutation_that_is_restored_before_exit(self) -> None:
        mutation_script = (
            "from pathlib import Path; import time; "
            "path = Path('tracked.txt'); original = path.read_bytes(); "
            "path.write_bytes(b'temporary mutation\\n'); time.sleep(0.08); "
            "path.write_bytes(original); time.sleep(0.03)"
        )
        command = shlex.join([sys.executable, "-c", mutation_script])
        candidate = self.collector.collect_derived()
        summary = GateRunner(
            completion_commands=(command,),
            gate_keys=("completion.commands[0]",),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "transient-gates",
            candidate_poll_seconds=0.005,
        ).run(candidate)

        self.assertEqual(summary.results[0].exit_code, 0)
        self.assertEqual(summary.results[0].exit_class, "candidate_changed")
        self.assertTrue(self.collector.matches(candidate))

    def test_passing_gates_do_not_authorize_unrecorded_candidate(self) -> None:
        unrecorded_store = RunStore(self.repo / ".vibe-loop" / "unrecorded.jsonl")
        collector = CandidateCollector(
            worktree=self.repo,
            branch="worker/task-01",
            base_main=self.base,
            run_store=unrecorded_store,
            run_id="run-unrecorded",
            task_id="TASK-01",
        )
        candidate = collector.snapshot(source="derived")
        summary = GateRunner(
            completion_commands=("true",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=collector,
            run_store=unrecorded_store,
            run_id="run-unrecorded",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "unrecorded-gates",
        ).run(candidate)

        self.assertTrue(summary.passed)
        self.assertFalse(summary.candidate_recorded)
        with self.assertRaises(GateExecutionError):
            summary.require_review_ready()

    def test_gate_resume_skips_recorded_pass_and_preserves_recorded_failure(
        self,
    ) -> None:
        candidate = self.collector.collect_derived()
        calls: list[str] = []

        def crash_on_second(command, **kwargs):
            calls.append(command)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(command, 0)

        first = GateRunner(
            completion_commands=("first", "second"),
            gate_keys=("completion.commands[0]", "completion.commands[1]"),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
            executor=crash_on_second,
        )
        with self.assertRaises(KeyboardInterrupt):
            first.run(candidate)

        resumed_calls: list[str] = []

        def pass_gate(command, **kwargs):
            resumed_calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        resumed = GateRunner(
            completion_commands=("first", "second"),
            gate_keys=("completion.commands[0]", "completion.commands[1]"),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
            executor=pass_gate,
        ).run(candidate)

        self.assertTrue(resumed.passed)
        self.assertEqual(resumed_calls, ["second"])
        self.assertTrue(resumed.results[0].resumed)

        Path(resumed.results[0].log_reference).write_text(
            "tampered\n", encoding="utf-8"
        )
        resumed_calls.clear()
        after_tamper = GateRunner(
            completion_commands=("first", "second"),
            gate_keys=("completion.commands[0]", "completion.commands[1]"),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
            executor=pass_gate,
        ).run(candidate)
        self.assertTrue(after_tamper.passed)
        self.assertEqual(resumed_calls, ["first"])

        failed_store = RunStore(self.repo / ".vibe-loop" / "failed.jsonl")
        failed_collector = CandidateCollector(
            worktree=self.repo,
            branch="worker/task-01",
            base_main=self.base,
            run_store=failed_store,
            run_id="run-2",
            task_id="TASK-01",
        )
        failed_candidate = failed_collector.collect_derived()
        failed = GateRunner(
            completion_commands=("false",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=failed_collector,
            run_store=failed_store,
            run_id="run-2",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "failed-gates",
        )
        self.assertFalse(failed.run(failed_candidate).passed)

        def unexpected_executor(*args, **kwargs):
            self.fail("a recorded failed gate must route to remediation")

        replay = GateRunner(
            completion_commands=("false",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=failed_collector,
            run_store=failed_store,
            run_id="run-2",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "failed-gates",
            executor=unexpected_executor,
        ).run(failed_candidate)
        self.assertFalse(replay.passed)
        self.assertTrue(replay.results[0].resumed)

    def test_remediation_budget_is_journaled_and_exhaustion_is_typed(self) -> None:
        candidate = self.collector.collect_derived()
        stage_records: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: stage_records.append(transition.to_payload())
        )
        for stage in (RunStage.ACTIVATION, RunStage.WORKSPACE, RunStage.IMPLEMENTING):
            machine.transition(stage, reason="setup")
        runner = GateRunner(
            completion_commands=("false",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "controller-gates",
        )

        def remediate(round_number, _summary):
            (self.repo / "tracked.txt").write_text(
                f"candidate {round_number}\n", encoding="utf-8"
            )
            git(self.repo, "add", "tracked.txt")
            git(self.repo, "commit", "-m", f"remediation {round_number}")

        controller = RuntimeGateController(
            candidate_collector=self.collector,
            gate_runner=runner,
            stage_machine=machine,
            max_remediation_rounds=1,
            remediation_launcher=remediate,
        )

        with self.assertRaises(GateRemediationExhausted) as raised:
            controller.run(candidate)

        self.assertEqual(raised.exception.max_rounds, 1)
        self.assertEqual(
            [record["to_stage"] for record in stage_records],
            [
                "activation",
                "workspace",
                "implementing",
                "candidate",
                "gates",
                "remediation",
                "candidate",
                "gates",
                "classification",
            ],
        )
        self.assertEqual(stage_records[-1]["failure"], "stage_failed")

    def test_controller_resumes_candidate_and_gate_journal_boundaries(self) -> None:
        candidate_records: list[dict[str, object]] = []

        def journal(transition) -> None:
            candidate_records.append(
                {"record_type": "stage_transition", **transition.to_payload()}
            )

        candidate_machine = RunLifecycleStateMachine(journal)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
        ):
            candidate_machine.transition(stage, reason="setup")
        restored_candidate = RunLifecycleStateMachine.from_records(
            candidate_records, journal
        )
        candidate_summary = RuntimeGateController(
            candidate_collector=self.collector,
            gate_runner=GateRunner(
                completion_commands=("true",),
                gate_keys=("completion.commands[0]",),
                candidate_collector=self.collector,
                run_store=self.store,
                run_id="run-1",
                task_id="TASK-01",
                log_dir=self.repo / ".vibe-loop" / "candidate-resume",
            ),
            stage_machine=restored_candidate,
            max_remediation_rounds=1,
            remediation_launcher=lambda _round, _summary: self.fail(
                "passing candidate recovery must not remediate"
            ),
        ).run()
        self.assertTrue(candidate_summary.passed)

        gate_store = RunStore(self.repo / ".vibe-loop" / "gate-resume.jsonl")
        gate_collector = CandidateCollector(
            worktree=self.repo,
            branch="worker/task-01",
            base_main=self.base,
            run_store=gate_store,
            run_id="run-gates",
            task_id="TASK-01",
        )
        gate_candidate = gate_collector.collect_derived()
        gate_records: list[dict[str, object]] = []

        def gate_journal(transition) -> None:
            gate_records.append(
                {"record_type": "stage_transition", **transition.to_payload()}
            )

        gate_machine = RunLifecycleStateMachine(gate_journal)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            gate_machine.transition(stage, reason="setup")
        initial_calls: list[str] = []

        def crash_second(command, **kwargs):
            initial_calls.append(command)
            if len(initial_calls) == 2:
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaises(KeyboardInterrupt):
            GateRunner(
                completion_commands=("first", "second"),
                gate_keys=("completion.commands[0]", "completion.commands[1]"),
                candidate_collector=gate_collector,
                run_store=gate_store,
                run_id="run-gates",
                task_id="TASK-01",
                log_dir=self.repo / ".vibe-loop" / "gate-boundary",
                executor=crash_second,
            ).run(gate_candidate)
        pending_calls: list[str] = []

        def pass_pending(command, **kwargs):
            pending_calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        restored_gates = RunLifecycleStateMachine.from_records(
            gate_records, gate_journal
        )
        gate_summary = RuntimeGateController(
            candidate_collector=gate_collector,
            gate_runner=GateRunner(
                completion_commands=("first", "second"),
                gate_keys=("completion.commands[0]", "completion.commands[1]"),
                candidate_collector=gate_collector,
                run_store=gate_store,
                run_id="run-gates",
                task_id="TASK-01",
                log_dir=self.repo / ".vibe-loop" / "gate-boundary",
                executor=pass_pending,
            ),
            stage_machine=restored_gates,
            max_remediation_rounds=1,
            remediation_launcher=lambda _round, _summary: self.fail(
                "passing gate recovery must not remediate"
            ),
        ).run()
        self.assertTrue(gate_summary.passed)
        self.assertEqual(pending_calls, ["second"])

    def test_controller_routes_recorded_gate_failure_without_rerunning_it(
        self,
    ) -> None:
        failure_store = RunStore(self.repo / ".vibe-loop" / "failure-resume.jsonl")
        collector = CandidateCollector(
            worktree=self.repo,
            branch="worker/task-01",
            base_main=self.base,
            run_store=failure_store,
            run_id="run-failure",
            task_id="TASK-01",
        )
        candidate = collector.collect_derived()
        stage_records: list[dict[str, object]] = []

        def journal(transition) -> None:
            stage_records.append(
                {"record_type": "stage_transition", **transition.to_payload()}
            )

        machine = RunLifecycleStateMachine(journal)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")
        failing_runner = GateRunner(
            completion_commands=("false",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=collector,
            run_store=failure_store,
            run_id="run-failure",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "failure-boundary",
        )
        self.assertFalse(failing_runner.run(candidate).passed)
        executor_calls: list[str] = []
        remediation_summaries = []

        def unexpected_executor(command, **kwargs):
            executor_calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        def observe_remediation(_round, summary):
            remediation_summaries.append(summary)
            raise RuntimeError("remediation relaunched")

        restored = RunLifecycleStateMachine.from_records(stage_records, journal)
        with self.assertRaisesRegex(RuntimeError, "remediation relaunched"):
            RuntimeGateController(
                candidate_collector=collector,
                gate_runner=GateRunner(
                    completion_commands=("false",),
                    gate_keys=("completion.commands[0]",),
                    candidate_collector=collector,
                    run_store=failure_store,
                    run_id="run-failure",
                    task_id="TASK-01",
                    log_dir=self.repo / ".vibe-loop" / "failure-boundary",
                    executor=unexpected_executor,
                ),
                stage_machine=restored,
                max_remediation_rounds=1,
                remediation_launcher=observe_remediation,
            ).run()

        self.assertEqual(executor_calls, [])
        self.assertEqual(len(remediation_summaries), 1)
        self.assertTrue(remediation_summaries[0].results[0].resumed)


class CandidateBaseReanchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        init_git_repo(self.repo)
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.worktree = Path(self.directory.name) / "task-worktree"
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "worker/TASK-01",
            str(self.worktree),
        )
        self.store = RunStore(self.repo / ".vibe-loop" / "runs.jsonl")
        self.collector = CandidateCollector(
            worktree=self.worktree,
            branch="worker/TASK-01",
            base_main=self.base,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
        )

    def commit_candidate(self) -> CandidateRecord:
        (self.worktree / "candidate.txt").write_text(
            "candidate\n",
            encoding="utf-8",
        )
        git(self.worktree, "add", "candidate.txt")
        git(self.worktree, "commit", "-m", "candidate")
        return self.collector.collect_derived()

    def advance_main(self, content: str) -> str:
        (self.repo / "main.txt").write_text(content, encoding="utf-8")
        git(self.repo, "add", "main.txt")
        git(self.repo, "commit", "-m", "advance main")
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_clean_advance_reanchors_and_reruns_candidate_gates(self) -> None:
        candidate = self.commit_candidate()
        gate_calls: list[str] = []

        def pass_gate(command, **kwargs):
            gate_calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        old_summary = GateRunner(
            completion_commands=("verify",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
            executor=pass_gate,
        ).run(candidate)
        advanced_base = self.advance_main("advanced\n")

        reanchored = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        ).stabilize(candidate)
        fresh_summary = GateRunner(
            completion_commands=("verify",),
            gate_keys=("completion.commands[0]",),
            candidate_collector=self.collector,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "gates",
            executor=pass_gate,
        ).run(reanchored)

        self.assertTrue(old_summary.passed)
        self.assertTrue(fresh_summary.passed)
        self.assertEqual(gate_calls, ["verify", "verify"])
        self.assertEqual(reanchored.base_main, advanced_base)
        self.assertNotEqual(reanchored.fingerprint, candidate.fingerprint)
        self.assertEqual(reanchored.changed_paths, candidate.changed_paths)
        self.assertEqual(
            git(
                self.worktree,
                "merge-base",
                "--is-ancestor",
                advanced_base,
                reanchored.head_commit,
            ).returncode,
            0,
        )
        anchor_records = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "candidate_base_anchor"
        ]
        self.assertEqual(
            [record["outcome"] for record in anchor_records],
            ["re-anchored-clean"],
        )

    def test_unchanged_base_records_typed_noop(self) -> None:
        candidate = self.commit_candidate()

        stabilized = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        ).stabilize(candidate)

        self.assertEqual(stabilized, candidate)
        record = self.store.read_records()[-1]
        self.assertEqual(record["record_type"], "candidate_base_anchor")
        self.assertEqual(record["outcome"], "base-unchanged")
        self.assertEqual(record["candidate_base"], self.base)
        self.assertEqual(record["observed_base"], self.base)

    def test_conflicting_advance_refuses_and_preserves_candidate(self) -> None:
        (self.worktree / "README.md").write_text(
            "candidate version\n",
            encoding="utf-8",
        )
        git(self.worktree, "add", "README.md")
        git(self.worktree, "commit", "-m", "candidate")
        candidate = self.collector.collect_derived()
        (self.repo / "README.md").write_text("main version\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "advance main")

        stabilized = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        ).stabilize(candidate)

        # A conflicting advance is not a run failure: integration still merges
        # main after review and reports the conflicted paths from there.
        self.assertEqual(stabilized, candidate)
        self.assertEqual(
            git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
            candidate.head_commit,
        )
        self.assertEqual(
            git(self.worktree, "status", "--porcelain=v1").stdout.strip(),
            "",
        )
        record = self.store.read_records()[-1]
        self.assertEqual(record["record_type"], "candidate_base_anchor")
        self.assertEqual(record["outcome"], "refused-conflict")
        self.assertEqual(record["reason"], "merge_conflict")

    def test_advance_containing_the_candidate_patch_refuses_and_preserves(
        self,
    ) -> None:
        candidate = self.commit_candidate()
        # Main lands an equivalent patch, so the rebase drops the candidate
        # commit and the re-anchored branch carries a different diff.
        (self.repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        git(self.repo, "add", "candidate.txt")
        git(self.repo, "commit", "-m", "same change upstream")

        stabilized = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        ).stabilize(candidate)

        self.assertEqual(stabilized, candidate)
        self.assertEqual(
            git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
            candidate.head_commit,
        )
        record = self.store.read_records()[-1]
        self.assertEqual(record["outcome"], "refused-conflict")
        self.assertEqual(record["reason"], "content_divergence")

    def test_content_divergence_refuses_and_restores_candidate(self) -> None:
        candidate = self.commit_candidate()
        self.advance_main("advanced\n")
        reanchorer = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        )

        with patch.object(
            reanchorer,
            "_candidate_diff",
            side_effect=(b"original diff", b"diverged diff"),
        ):
            stabilized = reanchorer.stabilize(candidate)

        self.assertEqual(stabilized, candidate)
        self.assertEqual(
            git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
            candidate.head_commit,
        )
        record = self.store.read_records()[-1]
        self.assertEqual(record["outcome"], "refused-conflict")
        self.assertEqual(record["reason"], "content_divergence")

    def test_failed_restore_records_before_raising(self) -> None:
        candidate = self.commit_candidate()
        self.advance_main("advanced\n")
        reanchorer = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=2,
        )

        real_git_result = reanchorer._git_result

        def failing_reset(*args: str):
            if args[:2] == ("reset", "--hard"):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    stdout="",
                    stderr="fatal: unable to reset\n",
                )
            return real_git_result(*args)

        with patch.object(
            reanchorer,
            "_candidate_diff",
            side_effect=(b"original diff", b"diverged diff"),
        ):
            with patch.object(reanchorer, "_git_result", side_effect=failing_reset):
                with self.assertRaises(CandidateCollectionError) as raised:
                    reanchorer.stabilize(candidate)

        self.assertEqual(
            raised.exception.code,
            "candidate_reanchor_restore_failed",
        )
        record = self.store.read_records()[-1]
        self.assertEqual(record["record_type"], "candidate_base_anchor")
        self.assertEqual(record["outcome"], "refused-restore-failed")
        self.assertEqual(record["reason"], "reset_failed")

    def test_disabled_reanchoring_refuses_without_parking_the_run(self) -> None:
        candidate = self.commit_candidate()
        advanced_base = self.advance_main("advanced\n")

        stabilized = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=0,
        ).stabilize(candidate)

        self.assertEqual(stabilized, candidate)
        record = self.store.read_records()[-1]
        self.assertEqual(record["outcome"], "refused-disabled")
        self.assertEqual(record["observed_base"], advanced_base)

    def test_retry_bound_reports_concrete_base_drift(self) -> None:
        candidate = self.commit_candidate()
        first_advance = self.advance_main("first\n")
        reanchored = CandidateBaseReanchorer(
            candidate_collector=self.collector,
            main_branch="main",
            max_attempts=1,
        ).stabilize(candidate)
        self.advance_main("second\n")
        second_advance = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(CandidateReanchorRetryExhausted) as raised:
            CandidateBaseReanchorer(
                candidate_collector=self.collector,
                main_branch="main",
                max_attempts=1,
            ).stabilize(reanchored)

        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(raised.exception.details["candidate_base"], first_advance)
        self.assertEqual(raised.exception.details["observed_base"], second_advance)
        anchor_records = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "candidate_base_anchor"
        ]
        self.assertEqual(
            [record["outcome"] for record in anchor_records],
            ["re-anchored-clean", "refused-retry-bound"],
        )


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        init_git_repo(self.repo)
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.worktree = Path(self.directory.name) / "task-worktree"
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "worker/TASK-01",
            str(self.worktree),
        )
        (self.worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        git(self.worktree, "add", "candidate.txt")
        git(self.worktree, "commit", "-m", "candidate")
        self.candidate_head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        self.manager, self.store, token = acquire_run(self.repo, "TASK-01", "run-1")
        claim_worker_workspace(
            self.manager,
            self.store,
            task_id="TASK-01",
            run_id="run-1",
            branch="worker/TASK-01",
            worktree=self.worktree,
            repo=self.repo,
            base_commit=self.base,
            fencing_token=token,
        )

    def integrator(
        self,
        *,
        commands: tuple[str, ...] = ("true", "true"),
        integration_keys: tuple[str, ...] = ("completion.commands[0]",),
        main_keys: tuple[str, ...] = ("completion.commands[1]",),
        executor=subprocess.run,
        timeout_seconds: float = 0,
        stage_machine: RunLifecycleStateMachine | None = None,
    ) -> Integrator:
        return Integrator(
            repo=self.repo,
            main_branch="main",
            candidate=CandidateRecord(
                head_commit=self.candidate_head,
                base_main=self.base,
                changed_paths=("candidate.txt",),
                source="derived",
                branch="worker/TASK-01",
                worktree=self.worktree,
            ),
            completion_commands=commands,
            integration_keys=integration_keys,
            verify_on_main_keys=main_keys,
            lock_manager=self.manager,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            log_dir=self.repo / ".vibe-loop" / "integration",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=0.01,
            executor=executor,
            stage_machine=stage_machine,
        )

    def advance_main(self, *, content: str = "main\n") -> str:
        (self.repo / "main.txt").write_text(content, encoding="utf-8")
        git(self.repo, "add", "main.txt")
        git(self.repo, "commit", "-m", "advance main")
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_success_refreshes_verifies_fast_forwards_and_records_evidence(
        self,
    ) -> None:
        main_before = self.advance_main()

        result = self.integrator().run()

        self.assertTrue(result.completed)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(result.main_before, main_before)
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(),
            git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
        )
        self.assertEqual(
            [check.phase for check in result.verification],
            ["integration", "main"],
        )
        self.assertTrue(
            all(
                check.evidence_digest.startswith("sha256:")
                for check in result.verification
            )
        )
        self.assertFalse(self.manager.main_integration_status().locked)
        record_types = [record["record_type"] for record in self.store.read_records()]
        self.assertLess(
            record_types.index("lock_acquired"),
            record_types.index("integration_result"),
        )
        self.assertLess(
            record_types.index("integration_result"),
            record_types.index("lock_released"),
        )
        provenance = next(
            record
            for record in self.store.read_records()
            if record["record_type"] == "integration_provenance"
        )
        self.assertEqual(provenance["outcome"], "settled-directly")
        self.assertEqual(provenance["candidate_commit"], self.candidate_head)
        self.assertEqual(provenance["target_commit"], result.main_after)

    def test_exact_already_merged_branch_is_no_commit_noop(self) -> None:
        git(self.repo, "merge", "--ff-only", "worker/TASK-01")
        main_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.integrator(integration_keys=()).run()

        self.assertEqual(result.outcome, "branch_already_merged")
        self.assertEqual(result.main_before, main_head)
        self.assertEqual(result.main_after, main_head)
        self.assertEqual(
            git(self.repo, "rev-list", "--count", self.base + "..main").stdout.strip(),
            "1",
        )

    def test_strict_ancestor_candidate_reconciles_with_verification(self) -> None:
        git(self.repo, "merge", "--ff-only", self.candidate_head)
        target_head = self.advance_main()
        commit_count = git(self.repo, "rev-list", "--count", "main").stdout.strip()

        result = self.integrator().run()

        self.assertEqual(result.outcome, "branch_already_merged")
        self.assertTrue(result.recovered)
        self.assertEqual(result.candidate_head, self.candidate_head)
        self.assertEqual(result.main_after, target_head)
        self.assertNotEqual(result.candidate_head, result.main_after)
        self.assertEqual(
            [check.phase for check in result.verification],
            ["integration", "main"],
        )
        self.assertEqual(
            git(self.repo, "rev-list", "--count", "main").stdout.strip(),
            commit_count,
        )
        records = self.store.read_records()
        integration = [
            record
            for record in records
            if record["record_type"] == "integration_result"
        ]
        provenance = [
            record
            for record in records
            if record["record_type"] == "integration_provenance"
        ]
        self.assertEqual(len(integration), 1)
        self.assertEqual(len(provenance), 1)
        self.assertEqual(provenance[0]["outcome"], "settled-by-reconciliation")
        self.assertEqual(provenance[0]["candidate_commit"], self.candidate_head)
        self.assertEqual(provenance[0]["target_commit"], target_head)

    def test_concurrent_strict_ancestor_reconciliation_records_once(self) -> None:
        git(self.repo, "merge", "--ff-only", self.candidate_head)
        self.advance_main()
        calls = 0
        guard = threading.Lock()

        def count_success(command, **kwargs):
            nonlocal calls
            with guard:
                calls += 1
            return subprocess.CompletedProcess(command, 0)

        integrators = [
            self.integrator(executor=count_success, timeout_seconds=2) for _ in range(2)
        ]
        results: list[IntegrationResult] = []
        threads = [
            threading.Thread(target=lambda item=item: results.append(item.run()))
            for item in integrators
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.completed for result in results))
        self.assertEqual(calls, 2)
        records = self.store.read_records()
        self.assertEqual(
            sum(record["record_type"] == "integration_result" for record in records),
            1,
        )
        self.assertEqual(
            sum(
                record["record_type"] == "integration_provenance" for record in records
            ),
            1,
        )

    def test_merged_candidate_with_advanced_branch_refuses_reconciliation(
        self,
    ) -> None:
        git(self.repo, "merge", "--ff-only", self.candidate_head)
        target_head = self.advance_main()
        (self.worktree / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
        git(self.worktree, "add", "unreviewed.txt")
        git(self.worktree, "commit", "-m", "unreviewed branch advance")

        result = self.integrator().run()

        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.completed)
        records = self.store.read_records()
        self.assertFalse(
            any(
                record["record_type"] == "integration_result"
                and record.get("status") == "completed"
                for record in records
            )
        )
        refusal = next(
            record
            for record in records
            if record["record_type"] == "integration_provenance"
        )
        self.assertEqual(refusal["outcome"], "refused-unprovable")
        self.assertEqual(refusal["candidate_commit"], self.candidate_head)
        self.assertEqual(refusal["target_commit"], target_head)

    def test_merge_conflict_parks_workspace_and_releases_lock(self) -> None:
        (self.worktree / "README.md").write_text(
            "candidate conflict\n", encoding="utf-8"
        )
        git(self.worktree, "add", "README.md")
        git(self.worktree, "commit", "-m", "candidate conflict")
        self.candidate_head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "README.md").write_text("main conflict\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "main conflict")
        main_before = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.integrator().run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "merge_conflict")
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), main_before
        )
        self.assertIn("README.md", result.diagnostics["conflicted_paths"])
        self.assertTrue(
            git(
                self.worktree,
                "diff",
                "--name-only",
                "--diff-filter=U",
            ).stdout.strip()
        )
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_recovery_classifies_preserved_conflict_and_releases_stale_lock(
        self,
    ) -> None:
        (self.worktree / "README.md").write_text(
            "candidate conflict\n", encoding="utf-8"
        )
        git(self.worktree, "add", "README.md")
        git(self.worktree, "commit", "-m", "candidate conflict")
        self.candidate_head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "README.md").write_text("main conflict\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "main conflict")
        conflict = git(self.worktree, "merge", "--no-edit", "main", check=False)
        self.assertNotEqual(conflict.returncode, 0)
        self.manager.acquire_main_integration(
            task_id="TASK-01",
            run_id="run-1",
            metadata={"pid": 999_999_999},
        )

        result = self.integrator().run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "merge_conflict")
        self.assertIn("README.md", result.diagnostics["conflicted_paths"])
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_verification_failure_preserves_candidate_before_main_move(self) -> None:
        main_before = self.advance_main()

        result = self.integrator(
            commands=("false",),
            integration_keys=("completion.commands[0]",),
            main_keys=(),
        ).run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "integration_verification_failed")
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), main_before
        )
        self.assertTrue(self.worktree.exists())
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_verification_cannot_mutate_reviewed_candidate(self) -> None:
        main_before = self.advance_main()
        command = "printf 'mutated\\n' >> candidate.txt"

        result = self.integrator(
            commands=(command,),
            integration_keys=("completion.commands[0]",),
            main_keys=(),
        ).run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "integration_verification_failed")
        self.assertEqual(result.verification[0].exit_class, "candidate_changed")
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), main_before
        )
        self.assertIn("candidate.txt", git(self.worktree, "status", "--short").stdout)
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_lock_timeout_is_journaled_without_releasing_other_owner(self) -> None:
        holder = self.manager.acquire_main_integration(
            task_id="TASK-OTHER",
            run_id="run-other",
            metadata={"pid": os.getpid()},
        )
        self.addCleanup(self.manager.release, holder)

        result = self.integrator().run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "lock_timeout")
        status = self.manager.main_integration_status()
        self.assertTrue(status.locked)
        self.assertEqual(status.metadata["owner_task_id"], "TASK-OTHER")

    def test_lock_timeout_emits_typed_blocked_stage_transition(self) -> None:
        transitions = []
        machine = RunLifecycleStateMachine(transitions.append)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.REVIEW,
        ):
            machine.transition(stage, reason="setup")
        holder = self.manager.acquire_main_integration(
            task_id="TASK-OTHER",
            run_id="run-other",
            metadata={"pid": os.getpid()},
        )
        self.addCleanup(self.manager.release, holder)

        result = self.integrator(stage_machine=machine).run()

        self.assertEqual(result.reason, "lock_timeout")
        self.assertEqual(machine.stage, RunStage.CLASSIFICATION)
        self.assertEqual(transitions[-1].failure, StageFailure.BLOCKED)

    def test_two_same_run_recoveries_atomically_claim_one_stale_window(self) -> None:
        self.advance_main()
        self.manager.acquire_main_integration(
            task_id="TASK-01",
            run_id="run-1",
            metadata={"pid": 999_999_999},
        )
        active = 0
        maximum = 0
        calls = 0
        guard = threading.Lock()

        def slow_success(command, **kwargs):
            nonlocal active, maximum, calls
            with guard:
                active += 1
                calls += 1
                maximum = max(maximum, active)
            try:
                threading.Event().wait(0.05)
                return subprocess.CompletedProcess(command, 0)
            finally:
                with guard:
                    active -= 1

        integrators = [
            self.integrator(
                commands=("check",),
                integration_keys=("completion.commands[0]",),
                main_keys=(),
                executor=slow_success,
                timeout_seconds=2,
            )
            for _ in range(2)
        ]
        results: list[object] = []
        threads = [
            threading.Thread(target=lambda item=item: results.append(item.run()))
            for item in integrators
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(maximum, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.completed for result in results))
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_two_same_run_recoveries_refresh_expired_lease_atomically(self) -> None:
        self.advance_main()
        self.manager.acquire_main_integration(
            task_id="TASK-01",
            run_id="run-1",
            metadata={
                "pid": os.getpid(),
                "lease_seconds": 60,
                "heartbeat_at": "2000-01-01T00:00:00+00:00",
            },
        )
        active = 0
        maximum = 0
        calls = 0
        guard = threading.Lock()

        def slow_success(command, **kwargs):
            nonlocal active, maximum, calls
            with guard:
                active += 1
                calls += 1
                maximum = max(maximum, active)
            try:
                threading.Event().wait(0.05)
                return subprocess.CompletedProcess(command, 0)
            finally:
                with guard:
                    active -= 1

        integrators = [
            self.integrator(
                commands=("check",),
                integration_keys=("completion.commands[0]",),
                main_keys=(),
                executor=slow_success,
                timeout_seconds=2,
            )
            for _ in range(2)
        ]
        results: list[object] = []
        threads = [
            threading.Thread(target=lambda item=item: results.append(item.run()))
            for item in integrators
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(maximum, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.completed for result in results))
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_release_is_fenced_to_acquired_lock_generation(self) -> None:
        self.advance_main()
        replacement_token = ""

        def replace_lock(command, **kwargs):
            nonlocal replacement_token
            acquired = self.manager.main_integration_status()
            old_token = str(acquired.metadata["fencing_token"])
            self.manager.release_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                fencing_token=old_token,
            )
            replacement = self.manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={"pid": os.getpid()},
            )
            replacement_token = str(replacement.metadata["fencing_token"])
            self.assertNotEqual(replacement_token, old_token)
            return subprocess.CompletedProcess(command, 1)

        try:
            with self.assertRaises(LockFencingMismatch):
                self.integrator(
                    commands=("replace-lock",),
                    integration_keys=("completion.commands[0]",),
                    main_keys=(),
                    executor=replace_lock,
                ).run()
            status = self.manager.main_integration_status()
            self.assertTrue(status.locked)
            self.assertEqual(status.metadata["fencing_token"], replacement_token)
        finally:
            if self.manager.main_integration_status().locked:
                self.manager.release(
                    self.manager.current_lock(MAIN_INTEGRATION_LOCK_NAME)
                )

    def test_stale_owned_lock_recovers_after_main_ref_moved_without_duplicate_merge(
        self,
    ) -> None:
        self.advance_main()
        git(self.worktree, "merge", "--no-edit", "main")
        git(self.repo, "merge", "--ff-only", "worker/TASK-01")
        main_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        commit_count = git(self.repo, "rev-list", "--count", "main").stdout.strip()
        self.manager.acquire_main_integration(
            task_id="TASK-01",
            run_id="run-1",
            metadata={"pid": 999_999_999},
        )

        result = self.integrator(integration_keys=()).run()

        self.assertTrue(result.completed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.outcome, "branch_already_merged")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), main_head)
        self.assertEqual(
            git(self.repo, "rev-list", "--count", "main").stdout.strip(),
            commit_count,
        )
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_stale_owned_lock_recovers_refreshed_branch_before_main_move(self) -> None:
        self.advance_main()
        git(self.worktree, "merge", "--no-edit", "main")
        refreshed_head = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        self.manager.acquire_main_integration(
            task_id="TASK-01",
            run_id="run-1",
            metadata={"pid": 999_999_999},
        )

        result = self.integrator().run()

        self.assertTrue(result.completed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(result.refreshed_head, refreshed_head)
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), refreshed_head
        )
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_main_verification_failure_records_moved_ref_for_recovery(self) -> None:
        main_before = self.advance_main()

        result = self.integrator(
            commands=("true", "false"),
        ).run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "main_verification_failed")
        self.assertNotEqual(result.main_after, main_before)
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), result.main_after
        )
        self.assertTrue(
            git(
                self.repo,
                "merge-base",
                "--is-ancestor",
                result.refreshed_head,
                "main",
            ).returncode
            == 0
        )
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_non_main_checkout_is_blocked_without_moving_configured_ref(self) -> None:
        main_head = self.advance_main()
        git(self.repo, "checkout", "-b", "side")
        side_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.integrator().run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "main_worktree_unavailable")
        self.assertEqual(git(self.repo, "rev-parse", "main").stdout.strip(), main_head)
        self.assertEqual(git(self.repo, "rev-parse", "side").stdout.strip(), side_head)
        self.assertFalse(self.manager.main_integration_status().locked)

    def test_fast_forward_uses_verified_sha_if_candidate_ref_moves(self) -> None:
        self.advance_main()
        attacker_worktree = Path(self.directory.name) / "attacker-worktree"
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "attacker",
            str(attacker_worktree),
            self.candidate_head,
        )
        (attacker_worktree / "unverified.txt").write_text(
            "unverified\n", encoding="utf-8"
        )
        git(attacker_worktree, "add", "unverified.txt")
        git(attacker_worktree, "commit", "-m", "unverified")
        unverified_head = git(attacker_worktree, "rev-parse", "HEAD").stdout.strip()
        integrator = self.integrator()
        original_git = integrator._git
        moved = False

        def move_candidate_before_fast_forward(worktree, *args):
            nonlocal moved
            if (
                not moved
                and worktree == self.repo
                and args[:2] == ("merge", "--ff-only")
            ):
                moved = True
                git(
                    self.repo,
                    "update-ref",
                    "refs/heads/worker/TASK-01",
                    unverified_head,
                )
            return original_git(worktree, *args)

        with patch.object(
            integrator,
            "_git",
            side_effect=move_candidate_before_fast_forward,
        ):
            result = integrator.run()

        self.assertTrue(result.completed)
        self.assertTrue(moved)
        self.assertEqual(
            git(self.repo, "rev-parse", "main").stdout.strip(), result.refreshed_head
        )
        self.assertNotEqual(result.refreshed_head, unverified_head)
        self.assertNotEqual(
            git(
                self.repo,
                "merge-base",
                "--is-ancestor",
                unverified_head,
                "main",
                check=False,
            ).returncode,
            0,
        )

    def test_jobs_two_integration_windows_are_serialized(self) -> None:
        second_worktree = Path(self.directory.name) / "second-worktree"
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "worker/TASK-02",
            str(second_worktree),
            self.base,
        )
        (second_worktree / "second.txt").write_text("second\n", encoding="utf-8")
        git(second_worktree, "add", "second.txt")
        git(second_worktree, "commit", "-m", "second candidate")
        second_head = git(second_worktree, "rev-parse", "HEAD").stdout.strip()
        second_lock = self.manager.acquire(
            "TASK-02",
            "run-2",
            metadata=run_lock_metadata(self.repo, "TASK-02", "run-2"),
        )
        claim_worker_workspace(
            self.manager,
            self.store,
            task_id="TASK-02",
            run_id="run-2",
            branch="worker/TASK-02",
            worktree=second_worktree,
            repo=self.repo,
            base_commit=self.base,
            fencing_token=str(second_lock.metadata["fencing_token"]),
        )
        active = 0
        maximum = 0
        guard = threading.Lock()

        def slow_success(command, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                threading.Event().wait(0.05)
                return subprocess.CompletedProcess(command, 0)
            finally:
                with guard:
                    active -= 1

        first = self.integrator(
            commands=("check",),
            integration_keys=("completion.commands[0]",),
            main_keys=(),
            executor=slow_success,
            timeout_seconds=2,
        )
        second = Integrator(
            repo=self.repo,
            main_branch="main",
            candidate=CandidateRecord(
                head_commit=second_head,
                base_main=self.base,
                changed_paths=("second.txt",),
                source="derived",
                branch="worker/TASK-02",
                worktree=second_worktree,
            ),
            completion_commands=("check",),
            integration_keys=("completion.commands[0]",),
            verify_on_main_keys=(),
            lock_manager=self.manager,
            run_store=self.store,
            run_id="run-2",
            task_id="TASK-02",
            log_dir=self.repo / ".vibe-loop" / "integration-2",
            timeout_seconds=2,
            poll_interval_seconds=0.01,
            executor=slow_success,
        )
        results: list[object] = []

        threads = [
            threading.Thread(target=lambda item=item: results.append(item.run()))
            for item in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(maximum, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.completed for result in results))
        self.assertFalse(self.manager.main_integration_status().locked)


class MutableTaskSource:
    def __init__(self, status: str = "active") -> None:
        self.status = status
        self.complete_calls = 0
        self.reset_calls = 0
        self.park_calls = 0
        self.reset_context: dict[str, str] = {}
        self.reset_status = "ready"
        self.park_status = "on-hold"

    def probe(self, task_id: str) -> Task:
        return Task(task_id=task_id, title="Task", status=self.status)

    def complete(self, task_id: str, run_id: str, **kwargs: object) -> Task:
        self.complete_calls += 1
        self.status = "done"
        return self.probe(task_id)

    def reset(self, task_id: str, **kwargs: object) -> bool:
        self.reset_calls += 1
        runtime_context = kwargs.get("runtime_context")
        if isinstance(runtime_context, dict):
            self.reset_context = runtime_context
        self.status = self.reset_status
        return True

    def park(self, task_id: str, run_id: str, **kwargs: object) -> Task:
        self.park_calls += 1
        self.status = self.park_status
        return self.probe(task_id)


class TaskSourceProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        init_git_repo(self.repo)
        self.manager, self.store, _token = acquire_run(self.repo, "TASK-01", "run-1")
        self.task_lock = self.manager.current_lock("TASK-01")

    def record_integration(self) -> None:
        self.store.record_completed_integration(
            run_id="run-1",
            task_id="TASK-01",
            provenance_outcome="settled-directly",
            integration=RunLifecycleEvent.integration_result(
                run_id="run-1",
                task_id="TASK-01",
                payload=IntegrationResult(
                    outcome="merged",
                    status="completed",
                    reason="",
                    branch="worker/TASK-01",
                    candidate_head="a" * 40,
                    refreshed_head="b" * 40,
                    main_before="c" * 40,
                    main_after="b" * 40,
                ).to_payload(),
            ),
        )

    def record_candidate(self, head_commit: str) -> str:
        target = git(self.repo, "rev-parse", "main").stdout.strip()
        candidate = CandidateRecord(
            branch="worker/TASK-01",
            worktree=self.repo,
            base_main=target,
            head_commit=head_commit,
            changed_paths=(),
            source="derived",
        )
        self.store.append_lifecycle_event(
            RunLifecycleEvent.candidate_recorded(
                run_id="run-1",
                task_id="TASK-01",
                payload=candidate.to_payload(),
            )
        )
        return head_commit

    def completer(
        self,
        source: MutableTaskSource,
        *,
        mode: str = "adapter",
        stage_machine: RunLifecycleStateMachine | None = None,
    ) -> TaskSourceCompleter:
        return TaskSourceCompleter(
            source=source,
            task_source_config=TaskSourceConfig(
                type="command",
                list_command="list",
                activate_command="activate {task_id}",
                complete_command=(
                    "complete {task_id} --run {run_id}" if mode == "adapter" else None
                ),
                reset_command="reset {task_id}",
            ),
            mode=mode,
            lock_manager=self.manager,
            task_lock=self.task_lock,
            run_store=self.store,
            repo=self.repo,
            main_branch="main",
            run_id="run-1",
            task_id="TASK-01",
            stage_machine=stage_machine,
        )

    def settler(
        self,
        source: MutableTaskSource,
        *,
        park: bool = True,
        max_attempts: int = 3,
        runtime_context: MappingProxyType | None = None,
    ) -> TaskSourceSettler:
        return TaskSourceSettler(
            source=source,
            task_source_config=TaskSourceConfig(
                type="command",
                list_command="list",
                activate_command="activate {task_id}",
                reset_command="reset {task_id}",
                park_command="park {task_id}" if park else None,
                runnable_statuses=("ready",),
            ),
            lock_manager=self.manager,
            task_lock=self.task_lock,
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            runtime_context=dict(runtime_context or {}),
            max_attempts=max_attempts,
            backoff_seconds=0,
        )

    def test_completion_requires_durable_integration_before_adapter(self) -> None:
        source = MutableTaskSource()

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "durable completed integration_result"
        ):
            self.completer(source).complete()

        self.assertEqual(source.complete_calls, 0)
        self.assertEqual(self.store.read_records(), [])

    def test_wrong_stage_refuses_before_reconciliation_side_effects(self) -> None:
        candidate_head = git(self.repo, "rev-parse", "main").stdout.strip()
        self.record_candidate(candidate_head)
        transitions = []
        machine = RunLifecycleStateMachine(transitions.append)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.REVIEW,
        ):
            machine.transition(stage, reason="setup")
        source = MutableTaskSource()

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "may only follow the integration stage"
        ):
            self.completer(source, stage_machine=machine).complete()

        self.assertEqual(source.complete_calls, 0)
        self.assertEqual(
            [record["record_type"] for record in self.store.read_records()],
            ["candidate_recorded"],
        )

    def test_unmerged_candidate_records_refusal_and_fails_closed(self) -> None:
        base = git(self.repo, "rev-parse", "main").stdout.strip()
        git(self.repo, "checkout", "-b", "unmerged-candidate")
        (self.repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        git(self.repo, "add", "candidate.txt")
        git(self.repo, "commit", "-m", "unmerged candidate")
        candidate_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "checkout", "main")
        self.record_candidate(candidate_head)
        source = MutableTaskSource()

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "durable completed integration_result"
        ):
            self.completer(source).complete()

        self.assertEqual(source.complete_calls, 0)
        records = self.store.read_records()
        self.assertNotIn(
            "integration_result",
            [record["record_type"] for record in records],
        )
        refusal = [
            record
            for record in records
            if record["record_type"] == "integration_provenance"
        ]
        self.assertEqual(len(refusal), 1)
        self.assertEqual(refusal[0]["outcome"], "refused-unprovable")
        self.assertEqual(refusal[0]["candidate_commit"], candidate_head)
        self.assertEqual(refusal[0]["target_commit"], base)

    def test_completion_rejects_completed_report_before_provenance(self) -> None:
        self.record_integration()
        self.store.append_result(
            RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=self.repo / "run.log",
                start_main="a" * 40,
                end_main="b" * 40,
            )
        )

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "cannot precede task provenance"
        ):
            self.completer(MutableTaskSource()).complete()

    def test_adapter_completion_records_order_and_recovers_without_duplicate(
        self,
    ) -> None:
        self.record_integration()
        source = MutableTaskSource()
        completer = self.completer(source)

        first = completer.complete()
        second = completer.complete()

        self.assertEqual(first.confirmed_status, "done")
        self.assertEqual(first, second)
        self.assertEqual(source.complete_calls, 1)
        records = self.store.read_records()
        record_types = [record["record_type"] for record in records]
        self.assertLess(
            record_types.index("integration_result"),
            record_types.index("task_provenance_committed"),
        )
        settlement = next(
            record
            for record in records
            if record["record_type"] == "integration_provenance"
        )
        self.assertEqual(settlement["outcome"], "settled-directly")
        self.assertEqual(settlement["candidate_commit"], "a" * 40)
        self.assertEqual(settlement["target_commit"], "b" * 40)

    def test_completion_repairs_missing_integration_provenance(self) -> None:
        integration = IntegrationResult(
            outcome="merged",
            status="completed",
            reason="",
            branch="worker/TASK-01",
            candidate_head="a" * 40,
            refreshed_head="b" * 40,
            main_before="c" * 40,
            main_after="b" * 40,
        )
        self.store.append_lifecycle_event(
            RunLifecycleEvent.integration_result(
                run_id="run-1",
                task_id="TASK-01",
                payload=integration.to_payload(),
            )
        )

        self.completer(MutableTaskSource()).complete()

        records = self.store.read_records()
        self.assertEqual(
            sum(record["record_type"] == "integration_result" for record in records),
            1,
        )
        provenance = next(
            record
            for record in records
            if record["record_type"] == "integration_provenance"
        )
        self.assertEqual(provenance["outcome"], "settled-directly")

    def test_loopyard_style_adapter_enforces_transition_evidence_end_to_end(
        self,
    ) -> None:
        self.record_integration()
        state_path = self.repo / "loopyard-state.json"
        script_path = self.repo / "loopyard-adapter.py"
        state_path.write_text(
            json.dumps({"status": "active", "commits": []}), encoding="utf-8"
        )
        script_path.write_text(
            "import json, pathlib, sys\n"
            f"state_path = pathlib.Path({str(state_path)!r})\n"
            "state = json.loads(state_path.read_text())\n"
            "if sys.argv[1] == 'complete':\n"
            "    if not state['commits']:\n"
            "        raise SystemExit(3)\n"
            "    state['status'] = 'done'\n"
            "    state_path.write_text(json.dumps(state))\n"
            "print(json.dumps({'id': sys.argv[2], 'title': 'Task', "
            "'status': state['status']}))\n",
            encoding="utf-8",
        )
        command_prefix = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"
        )
        config = TaskSourceConfig(
            type="command",
            list_command=f"{command_prefix} probe TASK-01",
            probe_command=f"{command_prefix} probe {{task_id}}",
            activate_command=f"{command_prefix} activate {{task_id}}",
            complete_command=f"{command_prefix} complete {{task_id}} --run {{run_id}}",
            reset_command=f"{command_prefix} reset {{task_id}}",
        )
        source = CommandTaskSource(self.repo, config)
        completer = TaskSourceCompleter(
            source=source,
            task_source_config=config,
            mode="adapter",
            lock_manager=self.manager,
            task_lock=self.task_lock,
            run_store=self.store,
            repo=self.repo,
            main_branch="main",
            run_id="run-1",
            task_id="TASK-01",
        )

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "task_source.complete failed"
        ):
            completer.complete()

        state_path.write_text(
            json.dumps({"status": "active", "commits": ["a" * 40]}),
            encoding="utf-8",
        )
        result = completer.complete()

        self.assertEqual(result.confirmed_status, "done")
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"],
            "done",
        )

    def test_crash_after_external_transition_is_probe_confirmed(self) -> None:
        self.record_integration()
        source = MutableTaskSource("done")

        result = self.completer(source).complete()

        self.assertTrue(result.recovered)
        self.assertEqual(source.complete_calls, 0)

    def test_idempotent_completion_revalidates_exact_lock_generation(self) -> None:
        self.record_integration()
        completer = self.completer(MutableTaskSource())
        completer.complete()
        self.manager.release(self.task_lock)
        replacement = self.manager.acquire(
            "TASK-01",
            "run-1",
            metadata=run_lock_metadata(self.repo, "TASK-01", "run-1"),
        )
        self.assertNotEqual(
            replacement.metadata["fencing_token"],
            self.task_lock.metadata["fencing_token"],
        )

        with self.assertRaises(LockFencingMismatch):
            completer.complete()

    def test_external_confirmed_path_fails_closed_while_source_is_active(self) -> None:
        self.record_integration()

        with self.assertRaisesRegex(
            TaskSourceCompletionError, "does not confirm completion"
        ):
            self.completer(
                MutableTaskSource("active"), mode="external-confirmed"
            ).complete()

        self.assertNotIn(
            "task_provenance_committed",
            [record["record_type"] for record in self.store.read_records()],
        )

    def test_requeue_and_park_settlement_confirm_authoritative_status(self) -> None:
        source = MutableTaskSource()

        parked = self.settler(source).settle("park")

        self.assertTrue(parked.settled)
        self.assertEqual(parked.confirmed_status, "on-hold")
        self.assertEqual(source.park_calls, 1)
        self.assertEqual(source.reset_calls, 0)

    def test_requeue_receives_dynamic_fenced_runtime_context(self) -> None:
        source = MutableTaskSource()
        context = MappingProxyType(
            {
                "VIBE_LOOP_RUN_ID": "run-1",
                "VIBE_LOOP_FENCING_TOKEN": "dynamic-generation",
            }
        )

        result = self.settler(source, runtime_context=context).settle("requeue")

        self.assertTrue(result.settled)
        self.assertEqual(source.reset_context, dict(context))

    def test_missing_park_adapter_records_requeue_fallback(self) -> None:
        source = MutableTaskSource()

        result = self.settler(source, park=False).settle("park")

        self.assertTrue(result.settled)
        self.assertEqual(result.intent, "park")
        self.assertTrue(result.fallback_to_requeue)
        self.assertEqual(source.reset_calls, 1)
        record = self.store.read_records()[-1]
        self.assertEqual(record["record_type"], "task_source_settled")
        self.assertTrue(record["fallback_to_requeue"])

    def test_idempotent_settlement_revalidates_exact_lock_generation(self) -> None:
        settler = self.settler(MutableTaskSource())
        settler.settle("park")
        self.manager.release(self.task_lock)
        replacement = self.manager.acquire(
            "TASK-01",
            "run-1",
            metadata=run_lock_metadata(self.repo, "TASK-01", "run-1"),
        )
        self.assertNotEqual(
            replacement.metadata["fencing_token"],
            self.task_lock.metadata["fencing_token"],
        )

        with self.assertRaises(LockFencingMismatch):
            settler.settle("park")

    def test_unconfirmed_settlement_retains_lock_until_fenced_recovery(self) -> None:
        source = MutableTaskSource()
        source.reset_status = "active"

        pending = self.settler(source, max_attempts=2).settle("requeue")

        self.assertTrue(pending.settlement_pending)
        self.assertTrue(self.manager.is_locked("TASK-01"))
        attempted = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "task_source_settlement_attempted"
        ]
        self.assertEqual(len(attempted), 2)
        self.assertNotIn(
            "task_source_settled",
            [record["record_type"] for record in self.store.read_records()],
        )

        source.reset_status = "ready"
        with self.assertRaisesRegex(
            TaskSourceCompletionError, "cannot release before the durable run result"
        ):
            self.settler(source).recover_and_release("requeue")
        self.assertTrue(self.manager.is_locked("TASK-01"))

        self.store.append_result(
            RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="failed",
                exit_code=1,
                log_path=self.repo / "run.log",
                start_main="a" * 40,
                end_main="a" * 40,
            )
        )
        recovered = self.settler(source).recover_and_release("requeue")

        self.assertTrue(recovered.settled)
        self.assertTrue(recovered.recovered)
        self.assertFalse(self.manager.is_locked("TASK-01"))
        record_types = [record["record_type"] for record in self.store.read_records()]
        self.assertLess(
            record_types.index("task_source_settled"),
            len(record_types) - 1,
        )
        self.assertEqual(record_types[-1], "lock_released")

    def test_failed_settlement_records_exit_code_and_redacted_stderr(self) -> None:
        source = MutableTaskSource()
        token = "fencing-generation-8125"

        def refusing_reset(task_id: str, **kwargs: object) -> bool:
            raise subprocess.CalledProcessError(
                3,
                f"task-source-reset {task_id}",
                stderr=(
                    f'task-source: task "{task_id}" has an unreleased lock; '
                    f"refusing unfenced reset (token {token})\n"
                ),
            )

        source.reset = refusing_reset  # type: ignore[method-assign]
        settler = self.settler(
            source,
            park=False,
            max_attempts=1,
            runtime_context=MappingProxyType({"VIBE_LOOP_FENCING_TOKEN": token}),
        )

        result = settler.settle("requeue")

        self.assertTrue(result.settlement_pending)
        attempted = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "task_source_settlement_attempted"
        ]
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0]["error_class"], "CalledProcessError")
        self.assertEqual(attempted[0]["exit_code"], 3)
        # The diagnosis survives redaction; only the token is removed.
        self.assertIn("refusing unfenced reset", attempted[0]["stderr"])
        self.assertNotIn(token, json.dumps(attempted[0]))
        self.assertEqual(attempted[0]["confirmed_status"], "active")

    def test_post_release_diagnostics_redact_a_token_the_call_never_sent(
        self,
    ) -> None:
        # The post-release attempt carries no fencing claim, but the refusal it
        # provokes quotes the generation the backend has stored - which, after
        # a re-acquire, belongs to whichever run holds the lock now.
        stored_token = fencing_token_value(self.task_lock.metadata["fencing_token"])
        self.assertTrue(stored_token)
        source = MutableTaskSource()

        def refusing_reset(task_id: str, **kwargs: object) -> bool:
            raise subprocess.CalledProcessError(
                3,
                "reset",
                stderr=(
                    f"refusing reset: expected fencing token {stored_token}, "
                    f"caller sent none (token {stored_token})\n"
                ),
            )

        source.reset = refusing_reset  # type: ignore[method-assign]
        settler = self.settler(source, park=False, max_attempts=1)
        self.manager.release(self.task_lock)

        settler.settle_after_release("requeue")

        attempted = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "task_source_settlement_attempted"
        ]
        stderr = str(attempted[-1]["stderr"])
        self.assertNotIn(stored_token, stderr)
        self.assertIn("refusing reset", stderr)

    def test_truncated_diagnostics_cannot_leak_a_token_at_the_boundary(self) -> None:
        # Bounding before redacting would cut the token's left boundary off and
        # leave the lookbehind unable to match it.
        token = "fencing-generation-8125"
        source = MutableTaskSource()
        limit = tasks_module.TASK_SOURCE_ERROR_STREAM_LIMIT
        # Place the token so the kept tail begins four characters into it.
        # Bounding first would keep the fragment "ing-generation-8125", whose
        # left boundary is gone, so neither the exact-value lookbehind nor the
        # named-field patterns can match it - the shape the reviewer
        # reproduced as a stderr field starting "...8125 is not the current".
        trailing = "y" * (limit + 4 - len(token) - 1)
        stderr_text = f"xxxxx {token} {trailing}"

        def refusing_reset(task_id: str, **kwargs: object) -> bool:
            raise subprocess.CalledProcessError(3, "reset", stderr=f"{stderr_text}\n")

        source.reset = refusing_reset  # type: ignore[method-assign]
        settler = self.settler(
            source,
            park=False,
            max_attempts=1,
            runtime_context=MappingProxyType({"VIBE_LOOP_FENCING_TOKEN": token}),
        )

        settler.settle("requeue")

        attempted = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "task_source_settlement_attempted"
        ]
        stderr = str(attempted[-1]["stderr"])
        self.assertLessEqual(
            len(stderr),
            limit + len(tasks_module.TASK_SOURCE_ERROR_TRUNCATION_MARKER),
        )
        self.assertNotIn(token, stderr)
        # No surviving fragment either: a truncated token is still a token.
        self.assertNotIn("generation-8125", stderr)
        self.assertNotIn("8125", stderr)

    def test_settlement_recognizes_a_source_settled_under_a_failing_adapter(
        self,
    ) -> None:
        source = MutableTaskSource()

        def failing_reset(task_id: str, **kwargs: object) -> bool:
            source.status = "ready"
            raise subprocess.CalledProcessError(1, "reset", stderr="lost response\n")

        source.reset = failing_reset  # type: ignore[method-assign]

        result = self.settler(source, park=False, max_attempts=1).settle("requeue")

        self.assertTrue(result.settled)
        self.assertEqual(result.confirmed_status, "ready")
        self.assertNotIn(
            "task_source_settlement_attempted",
            [record["record_type"] for record in self.store.read_records()],
        )

    def test_post_release_settlement_runs_unfenced_and_names_the_command(self) -> None:
        source = MutableTaskSource()
        contexts: list[dict[str, str]] = []

        def refuse_while_locked(task_id: str, **kwargs: object) -> bool:
            contexts.append(dict(kwargs.get("runtime_context") or {}))  # type: ignore[arg-type]
            if self.manager.is_locked(task_id):
                raise subprocess.CalledProcessError(
                    3,
                    "reset",
                    stderr="unreleased lock; refusing unfenced reset\n",
                )
            source.status = "ready"
            return True

        source.reset = refuse_while_locked  # type: ignore[method-assign]
        settler = self.settler(
            source,
            park=False,
            max_attempts=1,
            runtime_context=MappingProxyType({"VIBE_LOOP_FENCING_TOKEN": "7"}),
        )
        self.assertTrue(settler.settle("requeue").settlement_pending)

        self.manager.release(self.task_lock)
        settled = settler.settle_after_release("requeue")

        self.assertTrue(settled.settled)
        self.assertTrue(settled.recovered)
        self.assertEqual(source.status, "ready")
        # The fenced attempt carried the token; the post-release one must not.
        self.assertEqual(contexts[0]["VIBE_LOOP_FENCING_TOKEN"], "7")
        self.assertEqual(contexts[-1], {})
        self.assertEqual(settler.recovery_command("requeue"), "reset TASK-01")

    def test_post_release_settlement_failure_records_the_operator_command(
        self,
    ) -> None:
        source = MutableTaskSource()

        def always_refuse(task_id: str, **kwargs: object) -> bool:
            raise subprocess.CalledProcessError(3, "reset", stderr="still refusing\n")

        source.reset = always_refuse  # type: ignore[method-assign]
        settler = self.settler(source, park=False, max_attempts=1)
        self.manager.release(self.task_lock)

        result = settler.settle_after_release("requeue")

        self.assertFalse(result.settled)
        attempted = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "task_source_settlement_attempted"
        ]
        self.assertEqual(attempted[-1]["phase"], "post_release")
        self.assertEqual(attempted[-1]["recovery_command"], "reset TASK-01")
        self.assertIn("still refusing", attempted[-1]["stderr"])


class ReviewRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.store = RunStore(self.repo / "runs.jsonl")
        self.candidate = CandidateRecord(
            branch="vibe-loop/TASK-01",
            worktree=self.repo,
            base_main="a" * 40,
            head_commit="b" * 40,
            changed_paths=("src/example.py",),
            source="derived",
        )
        self.gates = GateRunSummary(
            candidate=self.candidate,
            results=(
                GateResult(
                    config_key="completion.commands[0]",
                    exit_class="passed",
                    exit_code=0,
                    duration_seconds=0.5,
                    log_reference=str(self.repo / "gate.log"),
                    evidence_digest="sha256:" + "c" * 64,
                    candidate_fingerprint=self.candidate.fingerprint,
                ),
            ),
            candidate_recorded=True,
        )

    def agent(self, provider: str, *, command: str | None = None) -> AgentConfig:
        return AgentConfig(
            command=command
            or f"{provider} review --model {{model}} --effort {{effort}} {{prompt}}",
            command_source="explicit",
            model="review-model",
            model_source="explicit",
            effort="high",
            effort_source="explicit",
            agent_kind=provider,
            agent_kind_source="explicit",
            executable_kind=provider,
            profile_name="review",
        )

    def router(
        self,
        provider: str,
        executor,
        *,
        initial: int = 1,
        closure: int = 2,
    ) -> ReviewRouter:
        return ReviewRouter(
            reviewer=self.agent(provider),
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=initial,
            max_closure_passes=closure,
            concurrency=ReviewConcurrencyBudget(1),
            executor=executor,
            continuation_availability=lambda _provider, _role, _session: "",
            session_id_factory=lambda: f"{provider}-session",
        )

    def gates_for(self, candidate: CandidateRecord) -> GateRunSummary:
        return dataclasses.replace(
            self.gates,
            candidate=candidate,
            results=tuple(
                dataclasses.replace(result, candidate_fingerprint=candidate.fingerprint)
                for result in self.gates.results
            ),
        )

    def exact_codex_router(self, executor) -> ReviewRouter:
        reviewer = dataclasses.replace(
            self.agent("codex", command="codex review {prompt}"),
            model="gpt-5.6-terra",
            effort="xhigh",
        )
        return ReviewRouter(
            reviewer=reviewer,
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=2,
            concurrency=ReviewConcurrencyBudget(1),
            executor=executor,
            session_id_factory=lambda: "runtime-placeholder",
        )

    def test_exact_codex_review_without_route_settings_launches_unchanged(self) -> None:
        reviewer = dataclasses.replace(
            self.agent("codex", command="codex review {prompt}"),
            model=None,
            model_source="default:none",
            effort=None,
            effort_source="default:none",
        )
        observed: dict[str, object] = {}

        def execute(command: str, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            verdict = {
                "verdict": "approve",
                "findings": [],
                "session_id": "",
                "session_id_source": "",
                "continuation_ordinal": 0,
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(verdict))

        router = ReviewRouter(
            reviewer=reviewer,
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=2,
            concurrency=ReviewConcurrencyBudget(1),
            executor=execute,
            session_id_factory=lambda: "runtime-placeholder",
        )

        result = router.review(self.gates)

        self.assertTrue(result.approved)
        self.assertEqual(shlex.split(str(observed["command"]))[:2], ["codex", "review"])
        self.assertNotIn("--model", str(observed["command"]))
        self.assertNotIn("--effort", str(observed["command"]))
        self.assertEqual(observed["cwd"], self.repo)

    def test_exact_codex_review_route_rejects_before_provider_disclosure(self) -> None:
        codex_home = self.repo / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model_provider = "untrusted-proxy"\n'
            "[model_providers.untrusted-proxy]\n"
            'base_url = "https://example.invalid"\n',
            encoding="utf-8",
        )
        calls: list[str] = []

        def execute(command: str, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="{}")

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            with self.assertRaisesRegex(
                AgentResolutionError,
                "cannot safely receive.*model_provider",
            ):
                self.exact_codex_router(execute).review(self.gates)

        self.assertEqual(calls, [])
        self.assertEqual(self.store.read_records(), [])

    def test_exact_codex_review_route_never_reads_rollout_transcripts(self) -> None:
        codex_home = self.repo / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "07" / "22" / "secret.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            '{"type":"session_meta","payload":{"instructions":"secret"}}\n',
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}),
            patch.object(Path, "glob", side_effect=AssertionError("rollout read")),
            self.assertRaisesRegex(
                AgentResolutionError, "no supported effective-route"
            ),
        ):
            self.exact_codex_router(lambda *_args, **_kwargs: None).review(self.gates)

        self.assertEqual(self.store.read_records(), [])

    def test_exact_codex_review_route_rejects_relative_codex_home_without_state(
        self,
    ) -> None:
        calls: list[str] = []

        def execute(command: str, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="{}")

        with patch.dict(os.environ, {"CODEX_HOME": ".codex-relative"}):
            with self.assertRaisesRegex(AgentResolutionError, "cannot safely receive"):
                self.exact_codex_router(execute).review(self.gates)
            self.assertEqual(os.environ["CODEX_HOME"], ".codex-relative")

        self.assertEqual(calls, [])
        self.assertFalse((self.repo / ".vibe-loop" / "reviewer-bindings").exists())
        self.assertFalse((self.repo / ".codex-relative").exists())

    def test_routes_cross_provider_matrices_with_provenance_and_usage(self) -> None:
        cases = (
            (
                "claude",
                "codex",
                {"type": "turn.completed", "usage": {"input_tokens": 12}},
            ),
            (
                "codex",
                "claude",
                {
                    "type": "result",
                    "usage": {"input_tokens": 13},
                    "num_turns": 1,
                },
            ),
        )
        for implementer, reviewer, usage_event in cases:
            with self.subTest(implementer=implementer, reviewer=reviewer):
                commands: list[str] = []

                def execute(command: str, **kwargs):
                    commands.append(command)
                    output = "\n".join(
                        (
                            json.dumps(usage_event),
                            json.dumps(
                                {
                                    "verdict": "approve",
                                    "findings": [],
                                    "session_id": f"{reviewer}-session",
                                    "session_id_source": "provider",
                                    "continuation_ordinal": 0,
                                }
                            ),
                        )
                    )
                    return subprocess.CompletedProcess(command, 0, stdout=output)

                self.store = RunStore(self.repo / f"{reviewer}.jsonl")
                result = self.router(reviewer, execute).review(self.gates)

                self.assertTrue(result.approved)
                self.assertIn("review-model", commands[0])
                self.assertIn("high", commands[0])
                records = self.store.read_records()
                self.assertEqual(
                    [record["record_type"] for record in records],
                    [
                        "review_budget",
                        "review_started",
                        "review_verdict",
                        "review_budget",
                    ],
                )
                verdict = records[-2]
                self.assertEqual(verdict["route"]["provider"], reviewer)
                self.assertEqual(verdict["route"]["model"], "review-model")
                self.assertEqual(verdict["route"]["effort"], "high")
                self.assertEqual(verdict["stats"]["phase"], "initial_review")
                self.assertIn("input_tokens", verdict["stats"])

    def test_malformed_reask_findings_ledger_and_budget(self) -> None:
        outputs = iter(
            (
                "not json",
                json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "candidate can bypass review",
                                "evidence": "reproduction",
                                "files": ["src/example.py"],
                                "lines": ["12"],
                                "state": "open",
                            }
                        ],
                        "session_id": "session-1",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
            )
        )

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        router = self.router("codex", execute)
        result = router.review(self.gates)
        verdicts = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_verdict"
        ]

        self.assertEqual(result.verdict, "findings")
        self.assertEqual(result.attempt_ordinal, 2)
        self.assertEqual(
            [record["output_classification"] for record in verdicts],
            ["malformed", "parsed"],
        )
        self.assertEqual(verdicts[0]["malformed_output"]["text"], "not json")
        self.assertEqual(
            [
                finding.finding_id
                for finding in router.ledger.open(self.candidate.fingerprint)
            ],
            ["F1"],
        )
        transitions: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: transitions.append(transition.to_payload())
        )
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")
        router.stage_machine = machine
        remediated_gates = self.gates_for(
            dataclasses.replace(self.candidate, head_commit="c" * 40)
        )
        with self.assertRaises(ReviewBudgetExhausted):
            router.review(remediated_gates)
        self.assertEqual(machine.stage, RunStage.CLASSIFICATION)
        self.assertIn("review_budget_exhausted", transitions[-1]["reason"])
        self.assertEqual(
            [record["record_type"] for record in self.store.read_records()],
            [
                "review_budget",
                "review_started",
                "review_verdict",
                "continuation_fallback",
                "review_started",
                "review_verdict",
                "finding_recorded",
                "review_budget",
                "review_budget",
            ],
        )

    def test_malformed_reask_exhaustion_blocks_with_redacted_output(self) -> None:
        outputs = iter(
            (
                "x" * 5000 + "\napi_token=super-secret",
                json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "candidate can bypass review",
                                "evidence": "reproduction",
                                "files": ["src/example.py"],
                                "lines": [{"line": 12}],
                                "state": "open",
                            }
                        ],
                    }
                ),
            )
        )
        commands: list[str] = []

        def execute(command: str, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        transitions: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: transitions.append(transition.to_payload())
        )
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")
        router = self.router("codex", execute)
        router.stage_machine = machine

        with self.assertRaises(ReviewOutputMalformed) as raised:
            router.review(self.gates)

        verdicts = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_verdict"
        ]
        self.assertEqual(raised.exception.attempts, 2)
        self.assertIn("review finding lines", raised.exception.reason)
        self.assertEqual(machine.stage, RunStage.CLASSIFICATION)
        self.assertEqual(transitions[-1]["failure"], "blocked")
        self.assertEqual(transitions[-1]["reason"], "review_output_malformed")
        self.assertEqual(len(verdicts), 2)
        self.assertTrue(
            all(record["output_classification"] == "malformed" for record in verdicts)
        )
        self.assertTrue(
            all(
                record["candidate_fingerprint"] == self.candidate.fingerprint
                for record in verdicts
            )
        )
        first_output = verdicts[0]["malformed_output"]
        self.assertTrue(first_output["truncated"])
        self.assertTrue(first_output["redacted"])
        self.assertLessEqual(len(first_output["text"]), 4096)
        self.assertNotIn("super-secret", first_output["text"])
        self.assertIn("<redacted>", first_output["text"])
        self.assertNotIn("previous response was malformed", commands[0])
        self.assertIn("previous response was malformed", commands[1])
        # The re-ask must name the specific violation; a bare "that was
        # malformed" makes the reviewer re-emit the identical bad shape.
        self.assertNotIn("missing JSON verdict", commands[0])
        self.assertIn("missing JSON verdict", commands[1])
        self.assertIn("Fix exactly that violation", commands[1])

    def test_scalar_finding_line_shapes_no_longer_discard_the_run(self) -> None:
        # The shape that destroyed six runs across both reviewer providers:
        # "lines" emitted as a bare string or as integers instead of an array
        # of strings. It must now parse on the first attempt.
        verdict = {
            "verdict": "findings",
            "findings": [
                {
                    "id": "F1",
                    "severity": "P1",
                    "summary": "candidate can bypass review",
                    "evidence": "reproduction",
                    "files": "src/example.py",
                    "lines": 12,
                    "state": "open",
                }
            ],
            "session_id": "session-1",
            "session_id_source": "provider",
        }
        commands: list[str] = []

        def execute(command: str, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(verdict))

        router = self.router("codex", execute)
        result = router.review(self.gates)

        self.assertEqual(result.verdict, "findings")
        self.assertEqual(result.attempt_ordinal, 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(result.findings[0].files, ("src/example.py",))
        self.assertEqual(result.findings[0].lines, ("12",))
        self.assertEqual(
            [
                record["output_classification"]
                for record in self.store.read_records()
                if record["record_type"] == "review_verdict"
            ],
            ["parsed"],
        )

    def test_review_prompt_states_the_files_and_lines_array_contract(self) -> None:
        commands: list[str] = []

        def execute(command: str, **kwargs):
            commands.append(command)
            verdict = {
                "verdict": "approve",
                "findings": [],
                "session_id": "session-1",
                "session_id_source": "provider",
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(verdict))

        self.router("codex", execute).review(self.gates)

        self.assertIn("files and lines are JSON arrays of strings", commands[0])
        self.assertIn("never a bare string and never numbers", commands[0])

    def test_finding_line_normalization_keeps_rejecting_unusable_shapes(self) -> None:
        base = {
            "id": "F1",
            "severity": "P1",
            "summary": "candidate can bypass review",
            "evidence": "reproduction",
            "files": ["src/example.py"],
            "state": "open",
        }
        for lines in ([{"line": 12}], [None], [True], {"line": "12"}, [""], ""):
            with self.subTest(lines=lines):
                with self.assertRaises(ReviewExecutionError) as raised:
                    ReviewFinding.from_payload({**base, "lines": lines})
                self.assertIn("lines must be a JSON array", str(raised.exception))
        for files in ([{"path": "a"}], [None], ""):
            with self.subTest(files=files):
                with self.assertRaises(ReviewExecutionError) as raised:
                    ReviewFinding.from_payload({**base, "files": files, "lines": []})
                self.assertIn("files must be a JSON array", str(raised.exception))

    def test_malformed_output_evidence_bounds_unbounded_reviewer_stdout(self) -> None:
        # redact_evidence_text is quadratic in line length. Unbounded, a 64KB
        # single-line answer costs ~83s of CPU while holding the task lock; the
        # bound must keep it flat regardless of how much the reviewer wrote.
        prose = "The candidate looks acceptable overall and the gates passed. "
        stdout = (prose * 4000)[:240_000] + "\napi_token=super-secret\n"
        router = self.router("codex", lambda command, **kwargs: None)

        started = time.perf_counter()
        evidence = router._malformed_output_evidence(stdout)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual(evidence["original_chars"], len(stdout))
        self.assertTrue(evidence["truncated"])
        self.assertLessEqual(len(evidence["text"]), 4096)
        self.assertNotIn("super-secret", evidence["text"])
        self.assertIn("<redacted>", evidence["text"])

    def test_malformed_output_evidence_redacts_quoted_fencing_token(self) -> None:
        router = self.router("codex", lambda command, **kwargs: None)

        evidence = router._malformed_output_evidence(
            "I could not review this.\nfencing_token: lock-token-9f3a\n"
        )

        self.assertNotIn("lock-token-9f3a", evidence["text"])
        self.assertTrue(evidence["redacted"])

    def test_bound_malformed_review_output_drops_cut_leading_line(self) -> None:
        # A mid-line cut can orphan a secret value from the key= label the
        # redactor keys on, so the partial leading line is dropped entirely.
        filler = "F" * 99
        secret_line = "api_token=super-secret-value"
        # Place the secret so the 8192-char tail window starts 10 chars into it,
        # i.e. immediately after "api_token=" and before the value.
        suffix_length = 8192 + 10 - len(secret_line) - 1
        suffix_lines = suffix_length // (len(filler) + 1)
        suffix = "\n".join([filler] * suffix_lines)
        suffix += "P" * (suffix_length - len(suffix))
        stdout = "\n".join([filler] * 300) + "\n" + secret_line + "\n" + suffix

        naive_tail = stdout[-8192:]
        bounded = bound_malformed_review_output(stdout)

        self.assertTrue(naive_tail.startswith("super-secret-value\n"))
        self.assertNotIn("super-secret-value", bounded)
        self.assertLessEqual(len(bounded), 8192)
        self.assertEqual(bounded.split("\n")[0], filler)
        self.assertTrue(all(len(line) <= 1032 for line in bounded.split("\n")))

    def test_bound_malformed_review_output_preserves_short_output(self) -> None:
        self.assertEqual(bound_malformed_review_output("not json"), "not json")
        self.assertEqual(bound_malformed_review_output(""), "")

    def test_targeted_closure_updates_ledger_and_phase(self) -> None:
        initial = ReviewFinding(
            finding_id="F1",
            severity="P1",
            summary="candidate can bypass review",
            evidence="reproduction",
            files=("src/example.py",),
        )
        self.store.append_lifecycle_event(
            RunLifecycleEvent.finding_recorded(
                run_id="run-1",
                task_id="TASK-01",
                payload={
                    "finding_id": initial.finding_id,
                    "severity": initial.severity,
                    "summary": initial.summary,
                    "evidence": initial.evidence,
                    "files": list(initial.files),
                    "lines": [],
                    "state": initial.state,
                    "candidate_fingerprint": self.candidate.fingerprint,
                    "pass_kind": "initial",
                },
            )
        )

        def execute(command: str, **kwargs):
            output = json.dumps(
                {
                    "verdict": "approve",
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "P1",
                            "summary": "candidate can bypass review",
                            "evidence": "focused reproduction now passes",
                            "files": ["src/example.py"],
                            "lines": ["12"],
                            "state": "remediated",
                        }
                    ],
                    "session_id": "claude-session",
                    "session_id_source": "provider",
                    "continuation_ordinal": 1,
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        router = self.router("claude", execute)
        remediated_candidate = dataclasses.replace(
            self.candidate,
            head_commit="c" * 40,
        )
        remediated_gates = self.gates_for(remediated_candidate)
        result = router.review(remediated_gates, pass_kind="closure:1")

        self.assertTrue(result.approved)
        self.assertEqual(router.ledger.open(), ())
        verdict = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_verdict"
        ][0]
        self.assertEqual(verdict["phase"], "targeted_closure")
        self.assertEqual(verdict["continuation_ordinal"], 1)

    def test_limit_wall_is_typed_to_reviewer_route_without_reask(self) -> None:
        outputs = iter(
            (
                (1, "You've hit your usage limit; resets at 3pm UTC"),
                (
                    0,
                    json.dumps(
                        {
                            "verdict": "approve",
                            "findings": [],
                            "session_id": "review-1",
                            "session_id_source": "provider",
                            "continuation_ordinal": 0,
                        }
                    ),
                ),
            )
        )

        def execute(command: str, **kwargs):
            returncode, output = next(outputs)
            return subprocess.CompletedProcess(command, returncode, stdout=output)

        router = self.router("codex", execute)
        with self.assertRaises(ReviewLimitWallError) as raised:
            router.review(self.gates)

        self.assertEqual(raised.exception.phase, "initial_review")
        result = router.review(self.gates)
        self.assertEqual(result.pass_ordinal, 1)
        records = self.store.read_records()
        wall = next(
            record
            for record in records
            if record.get("record_type") == "review_verdict"
            and record.get("retry_classification") == "limit_wall"
        )
        self.assertEqual(wall["retry_classification"], "limit_wall")
        self.assertEqual(wall["route"]["provider"], "codex")

    def test_reviewer_that_read_limit_wall_fixtures_is_not_a_wall(self) -> None:
        # The observed false positive. Reviewing a limit-wall slice means reading
        # its classifier fixtures, so the reviewer transcript quotes the phrases
        # verbatim; the reviewer then finished and approved. Text below is
        # verbatim from run 20260725T192937Z-...-58298d25-remediation-1, which
        # paused dispatch 1800s and was reported to the operator as an exhausted
        # provider quota.
        transcript = "\n".join(
            (
                "Reading tests/test_retry.py",
                '        ("Error 429: Too Many Requests. usage limit exceeded, '
                'retry after 30s", True),',
                '        ("503: weekly limit reached, service temporarily '
                'unavailable", True),',
                "You've hit your usage limit is the phrase the detector matches.",
                json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [],
                        "session_id": "review-1",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
            )
        )

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=transcript)

        result = self.router("codex", execute).review(self.gates)

        self.assertTrue(result.approved)
        self.assertEqual(
            [
                record.get("retry_classification")
                for record in self.store.read_records()
                if record.get("record_type") == "review_verdict"
            ],
            ["ok"],
        )

    def test_reviewer_wall_in_a_provider_envelope_still_pauses(self) -> None:
        # The real thing, in the shape the provider emits it: the reviewer
        # process failed and its terminal record carries the refusal. Matches
        # tests/fixtures/provider_usage/claude-limit-wall.json.
        envelope = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "result": "usage limit reached",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
        transcript = "\n".join(("reading src/vibe_loop/retry.py", envelope))

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout=transcript)

        with self.assertRaises(ReviewLimitWallError) as raised:
            self.router("codex", execute).review(self.gates)

        message = str(raised.exception)
        self.assertIn("usage limit reached", message)
        # The diagnostic must name what qualified the match, so an operator
        # reading a recorded pause can tell it from a quoted phrase.
        self.assertIn(f"[exit=1; scanned all {len(transcript)} chars]", message)

    def test_missing_prompt_delivery_fails_before_launch(self) -> None:
        router = ReviewRouter(
            reviewer=self.agent(
                "codex",
                command="codex review --model {model} --effort {effort}",
            ),
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=(),
            max_initial_passes=1,
            max_closure_passes=0,
            concurrency=ReviewConcurrencyBudget(1),
        )

        with self.assertRaisesRegex(AgentResolutionError, "must include.*prompt"):
            router.review(self.gates)
        self.assertEqual(self.store.read_records(), [])

    def test_open_finding_routes_to_remediation_and_cannot_reach_integration(
        self,
    ) -> None:
        transitions: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: transitions.append(transition.to_payload())
        )
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "completion can bypass review",
                                "evidence": "worker reported completed",
                                "files": ["src/example.py"],
                                "lines": ["12"],
                                "state": "open",
                            }
                        ],
                        "session_id": "review-1",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
            )

        router = self.router("codex", execute)
        router.stage_machine = machine
        result = router.review(self.gates)

        self.assertEqual(result.verdict, "findings")
        self.assertEqual(machine.stage, RunStage.REMEDIATION)
        self.assertNotIn(
            RunStage.INTEGRATION.value,
            [transition["to_stage"] for transition in transitions],
        )

    def test_reviewer_concurrency_budget_is_independent_and_bounded(self) -> None:
        budget = ReviewConcurrencyBudget(1)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with budget.slot():
                first_entered.set()
                release_first.wait(2)

        def second() -> None:
            with budget.slot():
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(first_entered.wait(1))
        second_thread.start()
        self.assertFalse(second_entered.wait(0.05))
        release_first.set()
        first_thread.join(1)
        second_thread.join(1)

        self.assertTrue(second_entered.is_set())
        self.assertEqual(budget.peak, 1)
        self.assertEqual(budget.active, 0)

    def test_later_approval_cannot_bypass_open_finding(self) -> None:
        outputs = iter(
            (
                json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "completion can bypass review",
                                "evidence": "reproduction",
                                "files": ["src/example.py"],
                                "lines": ["12"],
                                "state": "open",
                            }
                        ],
                        "session_id": "review-1",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
                json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [],
                        "session_id": "review-2",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
            )
        )

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        first = self.router("codex", execute, initial=2)
        first.review(self.gates)
        machine = RunLifecycleStateMachine(lambda _transition: None)
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")
        second = self.router("codex", execute, initial=2)
        second.stage_machine = machine

        second.review(self.gates)

        self.assertEqual(machine.stage, RunStage.REMEDIATION)
        self.assertEqual(
            [finding.finding_id for finding in second.ledger.open()], ["F1"]
        )

    def test_error_verdict_is_typed_failure_not_remediation(self) -> None:
        transitions: list[dict[str, object]] = []
        machine = RunLifecycleStateMachine(
            lambda transition: transitions.append(transition.to_payload())
        )
        for stage in (
            RunStage.ACTIVATION,
            RunStage.WORKSPACE,
            RunStage.IMPLEMENTING,
            RunStage.CANDIDATE,
            RunStage.GATES,
        ):
            machine.transition(stage, reason="setup")

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "verdict": "error",
                        "findings": [],
                        "session_id": "review-1",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                        "retry_classification": "fatal",
                    }
                ),
            )

        router = self.router("codex", execute)
        router.stage_machine = machine
        with self.assertRaises(ReviewStageResultError):
            router.review(self.gates)

        self.assertEqual(machine.stage, RunStage.CLASSIFICATION)
        self.assertNotIn(
            RunStage.REMEDIATION.value,
            [transition["to_stage"] for transition in transitions],
        )

    def test_provider_capabilities_cover_both_roles_and_shared_resume_paths(
        self,
    ) -> None:
        claude_reviewer = provider_capabilities("claude", "reviewer")
        codex_reviewer = provider_capabilities("codex", "reviewer")
        codex_implementer = provider_capabilities("codex", "implementer")

        self.assertTrue(claude_reviewer.session_injection)
        self.assertTrue(claude_reviewer.resume)
        self.assertTrue(claude_reviewer.structured_output)
        self.assertFalse(codex_reviewer.resume)
        self.assertFalse(codex_reviewer.structured_output)
        self.assertTrue(codex_implementer.resume)

        continuation = plan_session_continuation(
            provider="codex",
            role="implementer",
            continuing=True,
            prior_session_id="thread-123",
            prior_ordinal=0,
        )
        self.assertEqual(
            inject_provider_continuation(
                "codex exec --json {prompt}",
                provider="codex",
                role="implementer",
                continuation=continuation,
            ),
            "codex exec resume thread-123 --json {prompt}",
        )

    def test_review_refuses_gate_evidence_from_an_older_candidate(self) -> None:
        changed = dataclasses.replace(self.candidate, head_commit="f" * 40)
        stale_gates = dataclasses.replace(self.gates, candidate=changed)

        with self.assertRaisesRegex(GateExecutionError, "exact candidate"):
            self.router("codex", lambda *_args, **_kwargs: None).review(stale_gates)

    def test_claude_route_augments_existing_tool_denial_and_parses_stream_result(
        self,
    ) -> None:
        commands: list[str] = []

        def execute(command: str, **kwargs):
            commands.append(command)
            output = "\n".join(
                (
                    json.dumps(
                        {
                            "type": "result",
                            "result": json.dumps(
                                {
                                    "verdict": "approve",
                                    "findings": [],
                                    "session_id": "claude-session",
                                    "session_id_source": "provider",
                                }
                            ),
                            "usage": {"input_tokens": 17, "output_tokens": 5},
                            "num_turns": 1,
                        }
                    ),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        router = ReviewRouter(
            reviewer=self.agent(
                "claude",
                command=(
                    "claude -p --model {model} --effort {effort} "
                    "--disallowedTools Edit {prompt}"
                ),
            ),
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=1,
            concurrency=ReviewConcurrencyBudget(1),
            executor=execute,
            continuation_availability=lambda _provider, _role, _session: "",
            session_id_factory=lambda: "claude-session",
        )
        result = router.review(self.gates)

        argv = shlex.split(commands[0])
        denied = argv.index("--disallowedTools")
        self.assertEqual(argv[denied + 1 : denied + 3], ["Agent,Task", "Edit"])
        self.assertEqual(argv[-3:], ["--disallowedTools", "Agent,Task", "Edit"])
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertTrue(result.approved)
        self.assertEqual(result.usage.values["input_tokens"], 17)

    def test_default_claude_availability_detects_missing_transcript(self) -> None:
        home = self.repo / "claude-home"
        initial = ReviewFinding(
            finding_id="F1",
            severity="P2",
            summary="missing guard",
            evidence="reproduction",
            files=("src/example.py",),
        )
        self.store.append_lifecycle_event(
            RunLifecycleEvent.review_verdict(
                run_id="run-1",
                task_id="TASK-01",
                payload={
                    "pass_kind": "initial",
                    "pass_ordinal": 1,
                    "attempt_ordinal": 1,
                    "candidate_fingerprint": self.candidate.fingerprint,
                    "verdict": "findings",
                    "session_id": "old-session",
                    "session_id_source": "runtime_injected",
                    "continuation_ordinal": 0,
                    "route": {"provider": "claude"},
                },
            )
        )
        self.store.append_lifecycle_event(
            RunLifecycleEvent.finding_recorded(
                run_id="run-1",
                task_id="TASK-01",
                payload={
                    "finding_id": initial.finding_id,
                    "severity": initial.severity,
                    "summary": initial.summary,
                    "evidence": initial.evidence,
                    "files": list(initial.files),
                    "lines": [],
                    "state": "open",
                    "candidate_fingerprint": self.candidate.fingerprint,
                    "pass_kind": "initial",
                },
            )
        )

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [
                            {
                                **initial.to_payload(),
                                "evidence": "guard now passes",
                                "state": "remediated",
                            }
                        ],
                        "session_id": "fresh-session",
                        "session_id_source": "provider",
                        "continuation_ordinal": 1,
                    }
                ),
            )

        router = ReviewRouter(
            reviewer=self.agent(
                "claude",
                command=(
                    f"CLAUDE_HOME={home} claude -p --model {{model}} "
                    "--effort {effort} {prompt}"
                ),
            ),
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=1,
            concurrency=ReviewConcurrencyBudget(1),
            executor=execute,
            session_id_factory=lambda: "fresh-session",
        )
        result = router.review(
            self.gates_for(dataclasses.replace(self.candidate, head_commit="2" * 40)),
            pass_kind="closure:1",
        )

        self.assertTrue(result.approved)
        fallback = next(
            record
            for record in self.store.read_records()
            if record["record_type"] == "continuation_fallback"
        )
        self.assertEqual(fallback["reason"], "transcript_missing")

    def test_claude_closure_resumes_runtime_owned_reviewer_session(self) -> None:
        commands: list[str] = []
        outputs = iter(
            (
                json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "unsafe completion",
                                "evidence": "reproduction",
                                "files": ["src/example.py"],
                                "lines": ["12"],
                                "state": "open",
                            }
                        ],
                        "session_id": "claude-session",
                        "session_id_source": "provider",
                        "continuation_ordinal": 0,
                    }
                ),
                json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "summary": "unsafe completion",
                                "evidence": "reproduction passes",
                                "files": ["src/example.py"],
                                "lines": ["12"],
                                "state": "remediated",
                            }
                        ],
                        "session_id": "claude-session",
                        "session_id_source": "provider",
                        "continuation_ordinal": 1,
                    }
                ),
            )
        )

        def execute(command: str, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        router = self.router("claude", execute)
        router.review(self.gates)
        closure_gates = self.gates_for(
            dataclasses.replace(self.candidate, head_commit="d" * 40)
        )
        result = router.review(closure_gates, pass_kind="closure:1")

        self.assertTrue(result.approved)
        self.assertIn("--session-id claude-session", commands[0])
        self.assertIn("--resume claude-session", commands[1])
        self.assertIn("--disallowedTools Agent,Task", commands[0])
        self.assertIn("--output-format stream-json --verbose", commands[0])
        self.assertEqual(
            shlex.split(commands[0])[-2:], ["--disallowedTools", "Agent,Task"]
        )
        records = self.store.read_records()
        self.assertFalse(
            any(record["record_type"] == "continuation_fallback" for record in records)
        )
        starts = [
            record for record in records if record["record_type"] == "review_started"
        ]
        self.assertEqual(
            [
                (record["session_id"], record["continuation_ordinal"])
                for record in starts
            ],
            [("claude-session", 0), ("claude-session", 1)],
        )

    def test_codex_closure_records_fallback_before_fresh_launch(self) -> None:
        commands: list[str] = []
        outputs = iter(
            (
                json.dumps(
                    {
                        "verdict": "findings",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P2",
                                "summary": "missing guard",
                                "evidence": "reproduction",
                                "files": ["src/example.py"],
                                "lines": [],
                                "state": "open",
                            }
                        ],
                        "session_id": "codex-old",
                        "session_id_source": "provider",
                    }
                ),
                json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P2",
                                "summary": "missing guard",
                                "evidence": "guard now passes",
                                "files": ["src/example.py"],
                                "lines": [],
                                "state": "remediated",
                            }
                        ],
                        "session_id": "codex-new",
                        "session_id_source": "provider",
                        "continuation_ordinal": 1,
                    }
                ),
            )
        )

        def execute(command: str, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        router = self.router("codex", execute)
        router.review(self.gates)
        closure_gates = self.gates_for(
            dataclasses.replace(self.candidate, head_commit="e" * 40)
        )
        result = router.review(closure_gates, pass_kind="closure:1")

        self.assertTrue(result.approved)
        self.assertNotIn("--resume", shlex.split(commands[1]))
        self.assertNotIn("exec resume", commands[1])
        records = self.store.read_records()
        fallback_index = next(
            index
            for index, record in enumerate(records)
            if record["record_type"] == "continuation_fallback"
        )
        closure_start_index = next(
            index
            for index, record in enumerate(records)
            if record["record_type"] == "review_started"
            and record["pass_kind"] == "closure:1"
        )
        fallback = records[fallback_index]
        self.assertLess(fallback_index, closure_start_index)
        self.assertEqual(fallback["reason"], "provider_unsupported")
        self.assertEqual(fallback["prior_session_id"], "codex-old")
        self.assertEqual(
            [finding["id"] for finding in fallback["context_artifacts"]["findings"]],
            ["F1"],
        )
        closure_verdict = next(
            record
            for record in records
            if record["record_type"] == "review_verdict"
            and record["pass_kind"] == "closure:1"
            and record["verdict"] == "approve"
        )
        self.assertFalse(closure_verdict["continuation_resumed"])
        self.assertFalse(closure_verdict["stats"]["session_continuation"])

    def test_continuation_fallback_reasons_are_runtime_owned(self) -> None:
        missing = plan_session_continuation(
            provider="claude",
            role="reviewer",
            continuing=True,
            session_id_factory=lambda: "fresh-session",
        )
        expired = plan_session_continuation(
            provider="claude",
            role="reviewer",
            continuing=True,
            prior_session_id="old-session",
            availability_reason="session_expired",
            session_id_factory=lambda: "fresh-session",
        )

        self.assertEqual(missing.fallback_reason, "transcript_missing")
        self.assertEqual(expired.fallback_reason, "session_expired")
        self.assertEqual(expired.prior_session_id, "old-session")
        self.assertFalse(expired.resumed)

    def test_expired_claude_session_records_fallback_before_retry(self) -> None:
        commands: list[str] = []
        session_ids = iter(("old-session", "fresh-session"))
        outputs = iter(
            (
                (
                    0,
                    json.dumps(
                        {
                            "verdict": "findings",
                            "findings": [
                                {
                                    "id": "F1",
                                    "severity": "P2",
                                    "summary": "missing guard",
                                    "evidence": "reproduction",
                                    "files": ["src/example.py"],
                                    "lines": [],
                                    "state": "open",
                                }
                            ],
                            "session_id": "old-session",
                            "session_id_source": "provider",
                        }
                    ),
                ),
                (1, "No conversation found for session old-session"),
                (
                    0,
                    json.dumps(
                        {
                            "verdict": "approve",
                            "findings": [
                                {
                                    "id": "F1",
                                    "severity": "P2",
                                    "summary": "missing guard",
                                    "evidence": "guard now passes",
                                    "files": ["src/example.py"],
                                    "lines": [],
                                    "state": "remediated",
                                }
                            ],
                            "session_id": "fresh-session",
                            "session_id_source": "provider",
                            "continuation_ordinal": 1,
                        }
                    ),
                ),
            )
        )

        def execute(command: str, **kwargs):
            commands.append(command)
            returncode, output = next(outputs)
            return subprocess.CompletedProcess(command, returncode, stdout=output)

        router = self.router("claude", execute)
        router.session_id_factory = lambda: next(session_ids)
        router.review(self.gates)
        result = router.review(
            self.gates_for(dataclasses.replace(self.candidate, head_commit="1" * 40)),
            pass_kind="closure:1",
        )

        self.assertTrue(result.approved)
        self.assertIn("--resume old-session", commands[1])
        self.assertIn("--session-id fresh-session", commands[2])
        fallback = next(
            record
            for record in self.store.read_records()
            if record["record_type"] == "continuation_fallback"
        )
        self.assertEqual(fallback["reason"], "session_expired")
        self.assertEqual(fallback["prior_session_id"], "old-session")

    def test_unfinished_review_wait_never_relaunches(self) -> None:
        self.store.append_lifecycle_event(
            RunLifecycleEvent.review_started(
                run_id="run-1",
                task_id="TASK-01",
                payload={
                    "pass_kind": "initial",
                    "pass_ordinal": 1,
                    "attempt_ordinal": 1,
                    "candidate_fingerprint": self.candidate.fingerprint,
                    "session_id": "pending-session",
                    "session_id_source": "provider",
                    "continuation_ordinal": 0,
                },
            )
        )
        launches = 0

        def execute(command: str, **kwargs):
            nonlocal launches
            launches += 1
            raise AssertionError("unfinished review must not relaunch")

        with self.assertRaises(ReviewWaitIncomplete):
            self.router("codex", execute).review(self.gates)

        self.assertEqual(launches, 0)
        wait = self.store.read_records()[-1]
        self.assertEqual(wait["record_type"], "review_wait_incomplete")
        self.assertEqual(wait["session_id"], "pending-session")

    def test_review_attempt_claim_prevents_concurrent_duplicate_launch(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        launches = 0
        first_results: list[object] = []

        def execute(command: str, **kwargs):
            nonlocal launches
            launches += 1
            first_entered.set()
            release_first.wait(2)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [],
                        "session_id": "codex-session",
                        "session_id_source": "provider",
                    }
                ),
            )

        first = self.router("codex", execute)
        second = self.router("codex", execute)

        def run_first() -> None:
            first_results.append(first.review(self.gates))

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(first_entered.wait(1))
        with self.assertRaises(ReviewWaitIncomplete):
            second.review(self.gates)
        release_first.set()
        thread.join(2)

        self.assertEqual(launches, 1)
        self.assertEqual(len(first_results), 1)
        self.assertFalse(isinstance(first_results[0], Exception))

    def test_review_budget_resets_only_with_a_new_dispatch_contract(self) -> None:
        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "verdict": "approve",
                        "findings": [],
                        "session_id": "codex-session",
                        "session_id_source": "provider",
                    }
                ),
            )

        first = self.router("codex", execute, initial=1)
        first.review(self.gates)
        with self.assertRaises(ReviewBudgetExhausted):
            first.review(self.gates)

        second = ReviewRouter(
            reviewer=self.agent("codex"),
            reviewer_profile="review",
            run_store=self.store,
            run_id="run-2",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=2,
            concurrency=ReviewConcurrencyBudget(1),
            executor=execute,
            session_id_factory=lambda: "codex-session",
        )
        result = second.review(self.gates)

        self.assertTrue(result.approved)
        initialized = [
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_budget"
            and record["action"] == "initialized"
        ]
        self.assertEqual(
            [(record["run_id"], record["source"]) for record in initialized],
            [("run-1", "dispatch_contract"), ("run-2", "dispatch_contract")],
        )

    def test_nested_reviewer_launch_is_rejected_and_usage_is_separate(self) -> None:
        def execute(command: str, **kwargs):
            output = "\n".join(
                (
                    json.dumps(
                        {
                            "type": "subagent.started",
                            "usage": {"input_tokens": 31_012},
                        }
                    ),
                    json.dumps(
                        {
                            "verdict": "approve",
                            "findings": [],
                            "session_id": "codex-session",
                            "session_id_source": "provider",
                        }
                    ),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with self.assertRaises(ReviewDelegationPolicyError):
            self.router("codex", execute).review(self.gates)

        verdict = next(
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_verdict"
        )
        self.assertEqual(verdict["policy_violation"], "nested_reviewer_delegation")
        self.assertEqual(verdict["nested_launches"], 1)
        self.assertEqual(verdict["nested_usage"]["input_tokens"], 31_012)

    def test_claude_native_agent_tool_event_is_rejected(self) -> None:
        def execute(command: str, **kwargs):
            output = "\n".join(
                (
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "name": "Agent",
                                        "input": {"description": "closure"},
                                    }
                                ],
                                "usage": {"input_tokens": 31_012},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "result": json.dumps(
                                {
                                    "verdict": "approve",
                                    "findings": [],
                                    "session_id": "claude-session",
                                    "session_id_source": "provider",
                                }
                            ),
                        }
                    ),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with self.assertRaises(ReviewDelegationPolicyError):
            self.router("claude", execute).review(self.gates)

        verdict = next(
            record
            for record in self.store.read_records()
            if record["record_type"] == "review_verdict"
        )
        self.assertEqual(verdict["nested_launches"], 1)
        self.assertEqual(verdict["nested_usage"]["input_tokens"], 31_012)


class RunContractJournalTests(unittest.TestCase):
    @staticmethod
    def worker_owned_config(repo: Path, agent: AgentConfig) -> VibeConfig:
        return VibeConfig(
            repo=repo,
            agent=agent,
            orchestration=OrchestrationConfig(
                mode="worker-owned",
                explicit_keys=frozenset({"mode"}),
            ),
        )

    def test_contract_is_recorded_after_lock_and_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(self.worker_owned_config(repo, agent))
            task = Task(task_id="T-1", title="Task", status="Next")
            activation_record_types: list[str] = []

            def activate(*args, **kwargs):
                activation_record_types.extend(
                    str(record.get("record_type"))
                    for record in runner.run_store.read_records()
                )
                return None

            def worker(command, cwd, log, **kwargs):
                env = kwargs["env"]
                launch_record_types = [
                    record.get("record_type")
                    for record in runner.run_store.read_records()
                ]
                self.assertIn("workspace_provisioned", launch_record_types)
                self.assertIn("workspace_claim", launch_record_types)
                self.assertNotEqual(cwd, repo)
                self.assertEqual(env["VIBE_LOOP_REPO"], str(cwd.resolve()))
                self.assertEqual(env["VIBE_LOOP_WORKTREE"], str(cwd))
                self.assertNotIn("VIBE_LOOP_PRIMARY_REPO", env)
                self.assertEqual(
                    env["VIBE_LOOP_BRANCH"],
                    git(cwd, "branch", "--show-current").stdout.strip(),
                )
                kwargs["on_start"](12345)
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="failed",
                    )
                )
                return runner_module.StreamingCommandResult(exit_code=1)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(runner, "activate_task_before_launch", activate):
                    with patch("vibe_loop.runner.run_streaming_command", worker):
                        runner.run_task(task)

            records = runner.run_store.read_records()

        record_types = [record.get("record_type") for record in records]
        self.assertEqual(
            activation_record_types,
            [
                "lock_acquired",
                "run_contract_resolved",
                "stage_transition",
                "attempt_circuit_attempt",
            ],
        )
        self.assertLess(
            record_types.index("run_contract_resolved"),
            record_types.index("run_started"),
        )
        contract = next(
            record
            for record in records
            if record.get("record_type") == "run_contract_resolved"
        )
        started = next(
            record for record in records if record.get("record_type") == "run_started"
        )
        self.assertEqual(started["run_contract_digest"], contract["contract_digest"])
        self.assertEqual(started["orchestration_mode"], "worker-owned")
        stage_records = [
            record
            for record in records
            if record.get("record_type") == "stage_transition"
        ]
        self.assertEqual(
            [record["to_stage"] for record in stage_records],
            [
                "activation",
                "workspace",
                "implementing",
                "classification",
                "finalization",
            ],
        )
        self.assertEqual(stage_records[-2]["failure"], "stage_failed")

    def test_activation_cancellation_is_journaled_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(self.worker_owned_config(repo, agent))
            task = Task(task_id="T-1", title="Task", status="Next")

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    side_effect=KeyboardInterrupt,
                ):
                    with patch("vibe_loop.runner.git_rev_parse", return_value="abc123"):
                        with self.assertRaises(KeyboardInterrupt):
                            runner.run_task(task)

            records = runner.run_store.read_records()
            task_lock_exists = (repo / ".vibe-loop" / "locks" / "T-1.lock").exists()

        self.assertFalse(task_lock_exists)
        stage_records = [
            record
            for record in records
            if record.get("record_type") == "stage_transition"
        ]
        self.assertEqual(
            [record["to_stage"] for record in stage_records],
            ["activation", "classification", "finalization"],
        )
        self.assertEqual(stage_records[1]["failure"], "cancelled")
        self.assertEqual(records[-1]["record_type"], "lock_released")
        self.assertEqual(records[-1]["reason"], "task_activation_failed")

    def test_process_start_failure_compensates_workspace_and_releases_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(self.worker_owned_config(repo, agent))
            task = Task(task_id="T-1", title="Task", status="Next")

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch(
                        "vibe_loop.runner.run_streaming_command",
                        side_effect=OSError("spawn failed"),
                    ):
                        with self.assertRaisesRegex(OSError, "spawn failed"):
                            runner.run_task(task)

            worktrees = git(repo, "worktree", "list", "--porcelain").stdout
            branches = git(
                repo, "branch", "--format=%(refname:short)"
            ).stdout.splitlines()
            records = runner.run_store.read_records()

        self.assertEqual(worktrees.count("worktree "), 1)
        self.assertEqual(branches, ["main"])
        self.assertFalse(runner.lock_manager.is_locked(task.task_id))
        self.assertIn(
            "workspace_provisioned", [record.get("record_type") for record in records]
        )
        self.assertEqual(records[-1]["record_type"], "lock_released")

    def test_relative_claude_home_resolves_from_provisioned_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="CLAUDE_HOME=.claude claude -p {prompt}",
                agent_kind="claude",
            )
            runner = VibeRunner(self.worker_owned_config(repo, agent))
            task = Task(task_id="T-1", title="Task", status="Next")

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch(
                        "vibe_loop.runner.resolve_claude_home",
                        wraps=runner_module.resolve_claude_home,
                    ) as resolve_claude:
                        with patch(
                            "vibe_loop.runner.run_streaming_command",
                            side_effect=OSError("spawn failed"),
                        ):
                            with self.assertRaisesRegex(OSError, "spawn failed"):
                                runner.run_task(task)

            provisioned = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "workspace_provisioned"
            )
            launch_worktree = Path(str(provisioned["worktree"]))

        self.assertEqual(resolve_claude.call_args.args[2], launch_worktree)
        self.assertEqual(
            runner_module.resolve_claude_home(*resolve_claude.call_args.args),
            launch_worktree / ".claude",
        )

    def test_relative_codex_home_resolves_from_provisioned_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="CODEX_HOME=.codex codex exec {prompt}",
                agent_kind="codex",
            )
            runner = VibeRunner(self.worker_owned_config(repo, agent))
            task = Task(task_id="T-1", title="Task", status="Next")

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch(
                        "vibe_loop.runner.resolve_codex_home",
                        wraps=runner_module.resolve_codex_home,
                    ) as resolve_codex:
                        with patch(
                            "vibe_loop.runner.run_streaming_command",
                            side_effect=OSError("spawn failed"),
                        ):
                            with self.assertRaisesRegex(OSError, "spawn failed"):
                                runner.run_task(task)

            provisioned = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "workspace_provisioned"
            )
            launch_worktree = Path(str(provisioned["worktree"]))

        self.assertEqual(resolve_codex.call_args.args[2], launch_worktree)
        self.assertEqual(
            runner_module.resolve_codex_home(*resolve_codex.call_args.args),
            launch_worktree / ".codex",
        )


class WorkspaceProvisionerTests(unittest.TestCase):
    def test_creates_claims_and_records_workspace_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            primary_before = primary_snapshot(repo)

            workspace = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )

            records = store.read_records()
            self.assertEqual(workspace.mode, "created")
            self.assertNotEqual(workspace.worktree, repo)
            self.assertEqual(
                git(workspace.worktree, "branch", "--show-current").stdout.strip(),
                workspace.branch,
            )
            self.assertEqual(
                [record["record_type"] for record in records],
                ["workspace_preflight", "workspace_provisioned", "workspace_claim"],
            )
            self.assertEqual(primary_snapshot(repo), primary_before)
            self.assertEqual(
                manager.status("TASK-01")["workspace"]["worktree"],
                str(workspace.worktree),
            )

    def test_adopts_clean_workspace_with_prior_task_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            first = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            adopted = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-2",
                base_commit=base,
                fencing_token=token,
            )

            self.assertEqual(adopted.mode, "adopted")
            self.assertEqual(adopted.worktree, first.worktree)
            self.assertEqual(adopted.owner_run_id, "run-1")
            preflight = store.read_records()[-3]
            self.assertEqual(preflight["record_type"], "workspace_preflight")
            self.assertEqual(preflight["decision"], "reusable")
            self.assertEqual(preflight["retry_disposition"], "not_needed")
            self.assertTrue(preflight["worker_launch_allowed"])

    def test_adopts_legacy_path_with_matching_task_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            worktree = Path(directory) / "legacy-worker"
            branch = "legacy/task-01"
            git(repo, "worktree", "add", "-b", branch, str(worktree), base)
            claim_worker_workspace(
                manager,
                store,
                task_id="TASK-01",
                run_id="run-1",
                branch=branch,
                worktree=worktree,
                repo=repo,
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            adopted = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-2",
                base_commit=base,
                fencing_token=token,
            )

            self.assertEqual(adopted.mode, "adopted")
            self.assertEqual(adopted.branch, branch)
            self.assertEqual(adopted.worktree, worktree)

    def test_dirty_existing_workspace_fails_closed_outside_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            first = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            (first.worktree / "uncommitted.txt").write_text("keep\n", encoding="utf-8")
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            with self.assertRaises(WorkspaceProvisionError) as raised:
                WorkspaceProvisioner(
                    repo=repo,
                    main_branch="main",
                    lock_manager=manager,
                    run_store=store,
                ).provision(
                    task_id="TASK-01",
                    run_id="run-2",
                    base_commit=base,
                    fencing_token=token,
                )

            self.assertEqual(raised.exception.code, "dirty_existing_workspace")
            self.assertTrue((first.worktree / "uncommitted.txt").exists())

    def test_recovery_preserves_exact_dirty_prior_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            first = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            (first.worktree / "uncommitted.txt").write_text("keep\n", encoding="utf-8")
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            recovered = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-2",
                base_commit=base,
                fencing_token=token,
                recovery_run_id="run-1",
                recovery_branch=first.branch,
                recovery_worktree=first.worktree,
            )

            self.assertEqual(recovered.mode, "preserved")
            self.assertTrue(recovered.dirty_at_adoption)
            self.assertTrue((first.worktree / "uncommitted.txt").exists())

    def test_recovery_accepts_matching_staged_and_unstaged_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            first = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            (first.worktree / "staged.txt").write_text("staged\n", encoding="utf-8")
            git(first.worktree, "add", "staged.txt")
            (first.worktree / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
            snapshot_lines, dirty_fingerprint = git_dirty_snapshot(first.worktree)
            snapshot = tuple(snapshot_lines)
            common_dir = Path(
                git(first.worktree, "rev-parse", "--git-common-dir").stdout.strip()
            )
            if not common_dir.is_absolute():
                common_dir = first.worktree / common_dir
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            recovered = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-2",
                base_commit=base,
                fencing_token=token,
                recovery_run_id="run-1",
                recovery_branch=first.branch,
                recovery_worktree=first.worktree,
                recovery_git_common_dir=common_dir.resolve(),
                recovery_base_commit=base,
                recovery_head_commit=base,
                recovery_dirty_snapshot=snapshot,
                recovery_dirty_fingerprint=dirty_fingerprint,
            )

            self.assertEqual(recovered.mode, "preserved")
            self.assertTrue((first.worktree / "staged.txt").exists())
            self.assertTrue((first.worktree / "unstaged.txt").exists())

    def test_recovery_rejects_head_common_dir_and_dirty_snapshot_changes(self) -> None:
        cases = (
            ("head", "recovery_head_changed"),
            ("common_dir", "recovery_git_common_dir_changed"),
            ("dirty", "recovery_dirty_snapshot_changed"),
            ("dirty_content", "recovery_dirty_content_changed"),
        )
        for mutation, expected_code in cases:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory) / "repo"
                    init_git_repo(repo)
                    manager, store, token = acquire_run(repo, "TASK-01", "run-1")
                    base = git(repo, "rev-parse", "HEAD").stdout.strip()
                    first = WorkspaceProvisioner(
                        repo=repo,
                        main_branch="main",
                        lock_manager=manager,
                        run_store=store,
                    ).provision(
                        task_id="TASK-01",
                        run_id="run-1",
                        base_commit=base,
                        fencing_token=token,
                    )
                    manager.release(manager.current_lock("TASK-01"))
                    expected_head = base
                    expected_snapshot: tuple[str, ...] = ()
                    expected_fingerprint = git_dirty_snapshot(first.worktree)[1]
                    raw_common = Path(
                        git(
                            first.worktree, "rev-parse", "--git-common-dir"
                        ).stdout.strip()
                    )
                    expected_common = (
                        raw_common
                        if raw_common.is_absolute()
                        else first.worktree / raw_common
                    ).resolve()
                    if mutation == "head":
                        (first.worktree / "commit.txt").write_text(
                            "moved\n", encoding="utf-8"
                        )
                        git(first.worktree, "add", "commit.txt")
                        git(first.worktree, "commit", "-m", "move head")
                    elif mutation == "common_dir":
                        expected_common = Path(directory) / "different.git"
                    elif mutation == "dirty":
                        (first.worktree / "foreign.txt").write_text(
                            "foreign\n", encoding="utf-8"
                        )
                    else:
                        (first.worktree / "tracked.txt").write_text(
                            "first\n", encoding="utf-8"
                        )
                        git(first.worktree, "add", "tracked.txt")
                        (first.worktree / "README.md").write_text(
                            "first unstaged\n", encoding="utf-8"
                        )
                        expected_snapshot, expected_fingerprint = git_dirty_snapshot(
                            first.worktree
                        )
                        (first.worktree / "tracked.txt").write_text(
                            "second\n", encoding="utf-8"
                        )
                        git(first.worktree, "add", "tracked.txt")
                        (first.worktree / "README.md").write_text(
                            "second unstaged\n", encoding="utf-8"
                        )
                    manager, _, token = acquire_run(
                        repo,
                        "TASK-01",
                        "run-2",
                        store=store,
                    )

                    with self.assertRaises(WorkspaceProvisionError) as raised:
                        WorkspaceProvisioner(
                            repo=repo,
                            main_branch="main",
                            lock_manager=manager,
                            run_store=store,
                        ).provision(
                            task_id="TASK-01",
                            run_id="run-2",
                            base_commit=base,
                            fencing_token=token,
                            recovery_run_id="run-1",
                            recovery_branch=first.branch,
                            recovery_worktree=first.worktree,
                            recovery_git_common_dir=expected_common,
                            recovery_base_commit=base,
                            recovery_head_commit=expected_head,
                            recovery_dirty_snapshot=expected_snapshot,
                            recovery_dirty_fingerprint=expected_fingerprint,
                        )

                    self.assertEqual(raised.exception.code, expected_code)

    @staticmethod
    def _stale_workspace(
        directory: str,
        *,
        ignored_dirty_paths: tuple[Path, ...] | None = None,
        seed: Callable[[Path], None] | None = None,
    ) -> tuple[Path, object, ProvisionedWorkspace, str, str]:
        """Build a task worktree left behind at an older main.

        ``seed`` runs in the repo before the worktree is created; the returned
        tuple is (repo, run store, workspace, stale base, advanced base).
        """

        repo = Path(directory) / "repo"
        init_git_repo(repo)
        if seed is not None:
            seed(repo)
        manager, store, token = acquire_run(repo, "TASK-01", "run-1")
        stale_base = git(repo, "rev-parse", "HEAD").stdout.strip()
        first = WorkspaceProvisioner(
            repo=repo,
            main_branch="main",
            lock_manager=manager,
            run_store=store,
            ignored_dirty_paths=ignored_dirty_paths or (),
        ).provision(
            task_id="TASK-01",
            run_id="run-1",
            base_commit=stale_base,
            fencing_token=token,
        )
        manager.release(manager.current_lock("TASK-01"))
        (repo / "main-change.txt").write_text("advanced\n", encoding="utf-8")
        git(repo, "add", "main-change.txt")
        git(repo, "commit", "-m", "advance main")
        current_base = git(repo, "rev-parse", "HEAD").stdout.strip()
        return repo, store, first, stale_base, current_base

    @staticmethod
    def _adopt_stale(
        repo: Path,
        store: object,
        current_base: str,
        *,
        ignored_dirty_paths: tuple[Path, ...] | None = None,
    ) -> ProvisionedWorkspace:
        manager, _, token = acquire_run(repo, "TASK-01", "run-2", store=store)
        return WorkspaceProvisioner(
            repo=repo,
            main_branch="main",
            lock_manager=manager,
            run_store=store,
            ignored_dirty_paths=ignored_dirty_paths or (),
        ).provision(
            task_id="TASK-01",
            run_id="run-2",
            base_commit=current_base,
            fencing_token=token,
        )

    def _last_rejected_preflight(self, store: object) -> dict[str, object]:
        rejected = [
            record
            for record in store.read_records()
            if record.get("record_type") == "workspace_preflight"
            and record.get("decision") == "rejected"
        ]
        self.assertTrue(rejected)
        return rejected[-1]

    def test_clean_stale_workspace_is_refreshed_and_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, store, first, stale_base, current_base = self._stale_workspace(
                directory
            )
            self.assertEqual(
                git(first.worktree, "rev-parse", "HEAD").stdout.strip(), stale_base
            )
            self.assertFalse((first.worktree / "main-change.txt").exists())

            adopted = self._adopt_stale(repo, store, current_base)

            # The refresh, not merely a successful adoption: HEAD moved onto the
            # advanced base and the commit's content is materially present.
            self.assertEqual(
                git(first.worktree, "rev-parse", "HEAD").stdout.strip(), current_base
            )
            self.assertTrue((first.worktree / "main-change.txt").exists())
            self.assertEqual(adopted.refreshed_from, stale_base)
            self.assertEqual(adopted.head_commit, current_base)
            self.assertEqual(adopted.base_commit, current_base)
            self.assertEqual(adopted.mode, "adopted")
            self.assertEqual(adopted.worktree, first.worktree)
            provisioned = [
                record
                for record in store.read_records()
                if record.get("record_type") == "workspace_provisioned"
            ]
            self.assertEqual(provisioned[-1]["refreshed_from"], stale_base)
            preflight = [
                record
                for record in store.read_records()
                if record.get("record_type") == "workspace_preflight"
                and record.get("run_id") == "run-2"
            ]
            self.assertEqual(preflight[-1]["decision"], "reusable")
            self.assertTrue(preflight[-1]["worker_launch_allowed"])

    def test_stale_workspace_refresh_refusals_leave_the_worktree_untouched(
        self,
    ) -> None:
        def commit_unique(worktree: Path) -> None:
            (worktree / "candidate.txt").write_text("unique\n", encoding="utf-8")
            git(worktree, "add", "candidate.txt")
            git(worktree, "commit", "-m", "interrupted candidate")

        def leave_untracked(worktree: Path) -> None:
            (worktree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        def modify_tracked(worktree: Path) -> None:
            (worktree / "README.md").write_text("modified\n", encoding="utf-8")

        cases: tuple[tuple[str, Callable[[Path], None], str], ...] = (
            ("unique_commits", commit_unique, "unique_commits"),
            ("untracked_dirt", leave_untracked, "dirty_workspace"),
            ("tracked_dirt", modify_tracked, "dirty_workspace"),
        )
        for name, disturb, expected_refusal in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                repo, store, first, stale_base, current_base = self._stale_workspace(
                    directory
                )
                disturb(first.worktree)
                head_before = git(
                    first.worktree, "rev-parse", "HEAD"
                ).stdout.strip()
                status_before = git(
                    first.worktree, "status", "--short"
                ).stdout

                with self.assertRaises(WorkspaceProvisionError) as raised:
                    self._adopt_stale(repo, store, current_base)

                self.assertEqual(
                    raised.exception.code, "workspace_stale_current_base"
                )
                self.assertEqual(
                    raised.exception.retry_disposition,
                    "defer_until_workspace_changes",
                )
                self.assertEqual(
                    raised.exception.details["refresh_refused"], expected_refusal
                )
                # An operator reads the journal, not the exception.
                self.assertEqual(
                    self._last_rejected_preflight(store)["refresh_refused"],
                    expected_refusal,
                )
                # The half that matters: a guard that refuses after refreshing
                # is worse than no guard.
                self.assertEqual(
                    git(first.worktree, "rev-parse", "HEAD").stdout.strip(),
                    head_before,
                )
                self.assertEqual(
                    git(first.worktree, "status", "--short").stdout, status_before
                )
                self.assertFalse((first.worktree / "main-change.txt").exists())
                self.assertTrue(first.worktree.exists())
                if name == "unique_commits":
                    self.assertNotEqual(head_before, stale_base)

    def test_stale_workspace_refresh_fails_closed_on_unreadable_git_state(
        self,
    ) -> None:
        real = WorkspaceProvisioner._git_result_at
        # (matched git args, which matching call to disturb, replacement stdout
        # or None for a hard failure, expected refusal). The HEAD read is
        # matched twice -- once by the ordinary adoption path and once by the
        # refresh guard's own re-read -- so only the second is disturbed.
        cases: tuple[tuple[tuple[str, ...], int, str | None, str], ...] = (
            (("rev-parse", "--verify", "HEAD"), 2, None, "head_unreadable_or_moved"),
            (
                ("rev-parse", "--verify", "HEAD"),
                2,
                "0" * 40,
                "head_unreadable_or_moved",
            ),
            (("rev-list",), 1, None, "unique_commits_unreadable"),
            (("status", "--short"), 1, None, "status_unreadable"),
            (("merge", "--ff-only"), 1, None, "fast_forward_refused"),
        )
        for matched, ordinal, replacement, expected_refusal in cases:
            with (
                self.subTest(git_args=matched, ordinal=ordinal, stdout=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                repo, store, first, stale_base, current_base = self._stale_workspace(
                    directory
                )
                worktree = first.worktree
                seen = 0

                def break_git(
                    cwd: Path,
                    *args: str,
                    _matched: tuple[str, ...] = matched,
                    _ordinal: int = ordinal,
                    _replacement: str | None = replacement,
                    _worktree: Path = worktree,
                ) -> subprocess.CompletedProcess[str]:
                    nonlocal seen
                    if Path(cwd) == _worktree and args[: len(_matched)] == _matched:
                        seen += 1
                        if seen == _ordinal:
                            if _replacement is None:
                                return subprocess.CompletedProcess(
                                    ["git", *args], 128, "", "simulated git failure"
                                )
                            return subprocess.CompletedProcess(
                                ["git", *args], 0, _replacement, ""
                            )
                    return real(cwd, *args)

                with patch.object(
                    WorkspaceProvisioner,
                    "_git_result_at",
                    staticmethod(break_git),
                ):
                    with self.assertRaises(WorkspaceProvisionError) as raised:
                        self._adopt_stale(repo, store, current_base)

                self.assertEqual(
                    raised.exception.details["refresh_refused"], expected_refusal
                )
                self.assertEqual(
                    self._last_rejected_preflight(store)["refresh_refused"],
                    expected_refusal,
                )
                self.assertEqual(
                    git(worktree, "rev-parse", "HEAD").stdout.strip(), stale_base
                )
                self.assertFalse((worktree / "main-change.txt").exists())

    def test_stale_workspace_refresh_defers_when_the_dirty_snapshot_is_unreadable(
        self,
    ) -> None:
        # git_dirty_snapshot reports failure by raising its own error type
        # rather than a returncode, and it runs through workers.run_git, so the
        # returncode matrix above cannot reach this path.
        real_run_git = workers_module.run_git
        with tempfile.TemporaryDirectory() as directory:
            repo, store, first, stale_base, current_base = self._stale_workspace(
                directory
            )
            worktree = first.worktree

            def break_diff(
                path: Path, *args: str
            ) -> subprocess.CompletedProcess[str]:
                if Path(path) == worktree and args[:1] == ("diff",):
                    return subprocess.CompletedProcess(
                        ["git", *args], 128, "", "simulated git failure"
                    )
                return real_run_git(path, *args)

            with patch("vibe_loop.workers.run_git", side_effect=break_diff):
                with self.assertRaises(WorkspaceProvisionError) as raised:
                    self._adopt_stale(repo, store, current_base)

            self.assertEqual(raised.exception.code, "workspace_stale_current_base")
            self.assertEqual(
                raised.exception.retry_disposition,
                "defer_until_workspace_changes",
            )
            self.assertEqual(
                raised.exception.details["refresh_refused"],
                "dirty_snapshot_unreadable",
            )
            # The point of the wrap: a recorded deferral, not an untyped escape
            # past the handler that writes it.
            self.assertEqual(
                self._last_rejected_preflight(store)["refresh_refused"],
                "dirty_snapshot_unreadable",
            )
            self.assertEqual(
                git(worktree, "rev-parse", "HEAD").stdout.strip(), stale_base
            )
            self.assertFalse((worktree / "main-change.txt").exists())

    def test_stale_workspace_refresh_rejects_head_that_did_not_reach_the_base(
        self,
    ) -> None:
        real = WorkspaceProvisioner._git_result_at
        with tempfile.TemporaryDirectory() as directory:
            repo, store, first, _stale_base, current_base = self._stale_workspace(
                directory
            )
            worktree = first.worktree
            seen = 0

            def break_git(
                cwd: Path, *args: str
            ) -> subprocess.CompletedProcess[str]:
                nonlocal seen
                if Path(cwd) == worktree and args[:3] == (
                    "rev-parse",
                    "--verify",
                    "HEAD",
                ):
                    seen += 1
                    if seen == 3:
                        return subprocess.CompletedProcess(
                            ["git", *args], 0, "0" * 40, ""
                        )
                return real(cwd, *args)

            with patch.object(
                WorkspaceProvisioner, "_git_result_at", staticmethod(break_git)
            ):
                with self.assertRaises(WorkspaceProvisionError) as raised:
                    self._adopt_stale(repo, store, current_base)

            self.assertEqual(
                raised.exception.details["refresh_refused"],
                "refresh_did_not_reach_base",
            )
            # The fast-forward itself did land; only the post-condition read
            # disagreed. Deferring is still the fail-closed answer, and the next
            # dispatch finds a workspace that is no longer stale.
            self.assertEqual(
                git(worktree, "rev-parse", "HEAD").stdout.strip(), current_base
            )

    def test_stale_workspace_refresh_refuses_tracked_dirt_behind_ignored_path(
        self,
    ) -> None:
        def seed(repo: Path) -> None:
            vendor = repo / "vendor"
            vendor.mkdir()
            (vendor / "pinned.txt").write_text("pinned\n", encoding="utf-8")
            git(repo, "add", "vendor/pinned.txt")
            git(repo, "commit", "-m", "vendor")

        with tempfile.TemporaryDirectory() as directory:
            repo, store, first, stale_base, current_base = self._stale_workspace(
                directory,
                seed=seed,
            )
            ignored = (first.worktree / "vendor",)
            (first.worktree / "vendor" / "pinned.txt").write_text(
                "locally rewritten\n", encoding="utf-8"
            )
            # The exclusion hides the modification from the dirty snapshot, so
            # only the tracked/untracked distinction can catch it.
            self.assertEqual(
                git_dirty_snapshot(first.worktree, ignored_dirty_paths=ignored)[0],
                [],
            )

            with self.assertRaises(WorkspaceProvisionError) as raised:
                self._adopt_stale(
                    repo, store, current_base, ignored_dirty_paths=ignored
                )

            self.assertEqual(
                raised.exception.details["refresh_refused"],
                "tracked_modification_behind_ignored_path",
            )
            self.assertEqual(
                git(first.worktree, "rev-parse", "HEAD").stdout.strip(), stale_base
            )
            self.assertEqual(
                (first.worktree / "vendor" / "pinned.txt").read_text(encoding="utf-8"),
                "locally rewritten\n",
            )

    def test_stale_workspace_refresh_tolerates_untracked_ignored_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, store, first, stale_base, current_base = self._stale_workspace(
                directory
            )
            state = first.worktree / ".vibe-loop"
            state.mkdir()
            (state / "scratch.json").write_text("{}\n", encoding="utf-8")

            adopted = self._adopt_stale(repo, store, current_base)

            self.assertEqual(adopted.refreshed_from, stale_base)
            # A fast-forward never deletes an untracked file, which is why an
            # untracked state directory may count as clean.
            self.assertTrue((state / "scratch.json").exists())
            self.assertTrue((first.worktree / "main-change.txt").exists())

    def test_recovery_rejects_workspace_that_does_not_contain_current_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            first_base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            first = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=first_base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-01"))
            (repo / "main-change.txt").write_text("advanced\n", encoding="utf-8")
            git(repo, "add", "main-change.txt")
            git(repo, "commit", "-m", "advance main")
            current_base = git(repo, "rev-parse", "HEAD").stdout.strip()
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-2",
                store=store,
            )

            with self.assertRaises(WorkspaceProvisionError) as raised:
                WorkspaceProvisioner(
                    repo=repo,
                    main_branch="main",
                    lock_manager=manager,
                    run_store=store,
                ).provision(
                    task_id="TASK-01",
                    run_id="run-2",
                    base_commit=current_base,
                    fencing_token=token,
                    recovery_run_id="run-1",
                    recovery_branch=first.branch,
                    recovery_worktree=first.worktree,
                )

            self.assertEqual(raised.exception.code, "workspace_stale_current_base")
            self.assertEqual(
                raised.exception.retry_disposition,
                "defer_until_workspace_changes",
            )
            self.assertEqual(
                git(first.worktree, "rev-parse", "HEAD").stdout.strip(), first_base
            )
            self.assertTrue(first.worktree.exists())
            preflight = store.read_records()[-1]
            self.assertEqual(preflight["record_type"], "workspace_preflight")
            self.assertEqual(preflight["decision"], "rejected")
            self.assertEqual(preflight["reason"], "workspace_stale_current_base")
            self.assertEqual(
                preflight["retry_disposition"], "defer_until_workspace_changes"
            )
            self.assertFalse(preflight["worker_launch_allowed"])

    def test_runner_rejects_stale_adoption_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(
                RunContractJournalTests.worker_owned_config(repo, agent)
            )
            task = Task(task_id="T-1", title="Task", status="Next")
            manager, store, token = acquire_run(
                repo,
                task.task_id,
                "prior-run",
                store=runner.run_store,
            )
            old_base = git(repo, "rev-parse", "HEAD").stdout.strip()
            workspace = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id=task.task_id,
                run_id="prior-run",
                base_commit=old_base,
                fencing_token=token,
            )
            (workspace.worktree / "candidate.txt").write_text(
                "interrupted candidate\n", encoding="utf-8"
            )
            git(workspace.worktree, "add", "candidate.txt")
            git(workspace.worktree, "commit", "-m", "interrupted candidate")
            stale_head = git(workspace.worktree, "rev-parse", "HEAD").stdout.strip()
            manager.release(manager.current_lock(task.task_id))
            (repo / "current-base.txt").write_text("new base\n", encoding="utf-8")
            git(repo, "add", "current-base.txt")
            git(repo, "commit", "-m", "advance main")

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch("vibe_loop.runner.run_streaming_command") as launch:
                        with self.assertRaises(WorkspaceProvisionError) as raised:
                            runner.run_task(task)

            self.assertEqual(raised.exception.code, "workspace_stale_current_base")
            launch.assert_not_called()
            self.assertTrue(workspace.worktree.exists())
            self.assertEqual(
                git(workspace.worktree, "rev-parse", "HEAD").stdout.strip(),
                stale_head,
            )
            self.assertFalse(runner.lock_manager.is_locked(task.task_id))
            records = runner.run_store.read_records()
            rejected = [
                record
                for record in records
                if record.get("record_type") == "workspace_preflight"
                and record.get("decision") == "rejected"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(
                rejected[0]["retry_disposition"],
                "defer_until_workspace_changes",
            )
            self.assertNotIn(
                "worker_process_started",
                [record.get("record_type") for record in records],
            )

    def test_rejection_suppression_survives_ignored_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(
                RunContractJournalTests.worker_owned_config(repo, agent)
            )
            task = Task(task_id="T-1", title="Task", status="Next")
            manager, store, token = acquire_run(
                repo,
                task.task_id,
                "prior-run",
                store=runner.run_store,
            )
            old_base = git(repo, "rev-parse", "HEAD").stdout.strip()
            workspace = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id=task.task_id,
                run_id="prior-run",
                base_commit=old_base,
                fencing_token=token,
            )
            (workspace.worktree / "candidate.txt").write_text(
                "interrupted candidate\n", encoding="utf-8"
            )
            git(workspace.worktree, "add", "candidate.txt")
            git(workspace.worktree, "commit", "-m", "interrupted candidate")
            manager.release(manager.current_lock(task.task_id))
            (repo / "current-base.txt").write_text("new base\n", encoding="utf-8")
            git(repo, "add", "current-base.txt")
            git(repo, "commit", "-m", "advance main")
            scratch = workspace.worktree / "scratch"
            scratch.mkdir()
            (scratch / "note.txt").write_text("excluded dirt\n", encoding="utf-8")
            runner.workspace_ignored_dirty_paths = (scratch,)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch("vibe_loop.runner.run_streaming_command") as launch:
                        with self.assertRaises(WorkspaceProvisionError) as raised:
                            runner.run_task(task)

            self.assertEqual(raised.exception.code, "workspace_stale_current_base")
            launch.assert_not_called()
            rejected = [
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "workspace_preflight"
                and record.get("decision") == "rejected"
            ]
            self.assertEqual(len(rejected), 1)
            recorded_fingerprint = rejected[0]["workspace_state_fingerprint"]
            selected_base = git(repo, "rev-parse", "HEAD").stdout.strip()
            # The exclusion has to be load-bearing, or the suppression assertion
            # below would hold even if the recompute dropped the argument.
            self.assertNotEqual(
                recorded_fingerprint,
                runner_module.workspace_state_fingerprint(
                    repo=repo,
                    main_branch="main",
                    branch=str(rejected[0]["branch"]),
                    worktree=workspace.worktree,
                    expected_base=selected_base,
                ),
            )
            self.assertEqual(
                runner.unchanged_workspace_dispatch_deferrals(),
                {task.task_id},
            )

    def test_runner_rejects_head_change_between_preflight_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            agent = AgentConfig(
                command="worker {prompt}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner = VibeRunner(
                RunContractJournalTests.worker_owned_config(repo, agent)
            )
            task = Task(task_id="T-1", title="Task", status="Next")
            manager, store, token = acquire_run(
                repo,
                task.task_id,
                "prior-run",
                store=runner.run_store,
            )
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            workspace = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id=task.task_id,
                run_id="prior-run",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock(task.task_id))
            original_claim = claim_worker_workspace

            def mutate_then_claim(*args, **kwargs):
                (workspace.worktree / "raced.txt").write_text(
                    "preserve raced commit\n", encoding="utf-8"
                )
                git(workspace.worktree, "add", "raced.txt")
                git(workspace.worktree, "commit", "-m", "race workspace claim")
                return original_claim(*args, **kwargs)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    runner,
                    "activate_task_before_launch",
                    return_value=None,
                ):
                    with patch(
                        "vibe_loop.workers.claim_worker_workspace",
                        side_effect=mutate_then_claim,
                    ):
                        with patch("vibe_loop.runner.run_streaming_command") as launch:
                            with self.assertRaises(WorkspaceProvisionError) as raised:
                                runner.run_task(task)

            self.assertEqual(raised.exception.code, "workspace_changed_during_claim")
            launch.assert_not_called()
            self.assertTrue((workspace.worktree / "raced.txt").exists())
            claims = [
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "workspace_claim"
            ]
            self.assertEqual([record["run_id"] for record in claims], ["prior-run"])
            rejected = [
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "workspace_preflight"
                and record.get("decision") == "rejected"
            ]
            self.assertEqual(rejected[-1]["reason"], "workspace_changed_during_claim")
            self.assertFalse(rejected[-1]["worker_launch_allowed"])

    def test_recovery_rejects_latest_foreign_ownership_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-A", "run-a1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            first = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-A",
                run_id="run-a1",
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-A"))
            manager, _, token = acquire_run(repo, "TASK-B", "run-b1", store=store)
            claim_worker_workspace(
                manager,
                store,
                task_id="TASK-B",
                run_id="run-b1",
                branch=first.branch,
                worktree=first.worktree,
                repo=repo,
                base_commit=base,
                fencing_token=token,
            )
            manager.release(manager.current_lock("TASK-B"))
            (first.worktree / "foreign.txt").write_text("preserve\n", encoding="utf-8")
            manager, _, token = acquire_run(repo, "TASK-A", "run-a2", store=store)

            with self.assertRaises(WorkspaceProvisionError) as raised:
                WorkspaceProvisioner(
                    repo=repo,
                    main_branch="main",
                    lock_manager=manager,
                    run_store=store,
                ).provision(
                    task_id="TASK-A",
                    run_id="run-a2",
                    base_commit=base,
                    fencing_token=token,
                    recovery_run_id="run-a1",
                    recovery_branch=first.branch,
                    recovery_worktree=first.worktree,
                )

            self.assertEqual(raised.exception.code, "workspace_foreign_owner")
            self.assertEqual(
                (first.worktree / "foreign.txt").read_text(encoding="utf-8"),
                "preserve\n",
            )

    def test_branch_collision_fails_without_mutating_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            branch, _ = provisioner._workspace_identity(
                task_id="TASK-01",
                recovery_branch="",
                recovery_worktree=None,
            )
            git(repo, "branch", branch)
            before = primary_snapshot(repo)

            with self.assertRaises(WorkspaceProvisionError) as raised:
                provisioner.provision(
                    task_id="TASK-01",
                    run_id="run-1",
                    base_commit=base,
                    fencing_token=token,
                )

            self.assertEqual(raised.exception.code, "workspace_collision")
            self.assertEqual(primary_snapshot(repo), before)

    def test_dirty_primary_fails_before_workspace_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "dirty.txt").write_text("keep\n", encoding="utf-8")

            with self.assertRaises(WorkspaceProvisionError) as raised:
                WorkspaceProvisioner(
                    repo=repo,
                    main_branch="main",
                    lock_manager=manager,
                    run_store=store,
                ).provision(
                    task_id="TASK-01",
                    run_id="run-1",
                    base_commit=base,
                    fencing_token=token,
                )

            self.assertEqual(raised.exception.code, "dirty_primary_worktree")
            self.assertEqual(
                git(repo, "worktree", "list", "--porcelain").stdout.count("worktree "),
                1,
            )

    def test_two_tasks_receive_distinct_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            first_lock = manager.acquire(
                "TASK-01",
                "run-1",
                metadata=run_lock_metadata(repo, "TASK-01", "run-1"),
            )
            second_lock = manager.acquire(
                "TASK-02",
                "run-2",
                metadata=run_lock_metadata(repo, "TASK-02", "run-2"),
            )
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )

            first = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=str(first_lock.metadata["fencing_token"]),
            )
            second = provisioner.provision(
                task_id="TASK-02",
                run_id="run-2",
                base_commit=base,
                fencing_token=str(second_lock.metadata["fencing_token"]),
            )

            self.assertNotEqual(first.branch, second.branch)
            self.assertNotEqual(first.worktree, second.worktree)
            self.assertNotEqual(first.worktree, repo)
            self.assertNotEqual(second.worktree, repo)

    def test_compensation_removes_only_unchanged_created_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )
            workspace = provisioner.provision(
                task_id="TASK-01",
                run_id="run-1",
                base_commit=base,
                fencing_token=token,
            )

            provisioner.compensate_created(workspace)

            self.assertFalse(workspace.worktree.exists())
            self.assertNotEqual(
                git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{workspace.branch}",
                    check=False,
                ).returncode,
                0,
            )

    def test_failed_worktree_add_compensates_partial_git_state(self) -> None:
        class PartialFailureProvisioner(WorkspaceProvisioner):
            def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
                result = super()._git_result(*args)
                if args[:2] == ("worktree", "add"):
                    return subprocess.CompletedProcess(
                        result.args,
                        1,
                        result.stdout,
                        "injected post-create failure",
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = PartialFailureProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )

            with self.assertRaises(WorkspaceProvisionError) as raised:
                provisioner.provision(
                    task_id="TASK-01",
                    run_id="run-1",
                    base_commit=base,
                    fencing_token=token,
                )

            self.assertEqual(raised.exception.code, "workspace_create_failed")
            self.assertEqual(
                git(repo, "worktree", "list", "--porcelain").stdout.count("worktree "),
                1,
            )
            self.assertEqual(
                git(repo, "branch", "--format=%(refname:short)").stdout.splitlines(),
                ["main"],
            )

    def test_failed_worktree_add_preserves_unverified_racing_directory(self) -> None:
        class RacingFailureProvisioner(WorkspaceProvisioner):
            def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
                if args[:2] == ("worktree", "add"):
                    worktree = Path(args[4])
                    worktree.mkdir(parents=True)
                    (worktree / "foreign.txt").write_text(
                        "preserve\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(
                        ["git", *args],
                        1,
                        "",
                        "target appeared during add",
                    )
                return super()._git_result(*args)

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = RacingFailureProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )

            with self.assertRaises(WorkspaceProvisionError) as raised:
                provisioner.provision(
                    task_id="TASK-01",
                    run_id="run-1",
                    base_commit=base,
                    fencing_token=token,
                )

            _, worktree = provisioner._workspace_identity(
                task_id="TASK-01",
                recovery_branch="",
                recovery_worktree=None,
            )
            self.assertEqual(raised.exception.code, "workspace_collision")
            self.assertEqual(
                (worktree / "foreign.txt").read_text(encoding="utf-8"),
                "preserve\n",
            )

    def test_journal_failure_compensates_created_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_git_repo(repo)
            manager, store, token = acquire_run(repo, "TASK-01", "run-1")
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            provisioner = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            )

            with patch.object(
                store,
                "append_lifecycle_event",
                side_effect=OSError("journal unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "journal unavailable"):
                    provisioner.provision(
                        task_id="TASK-01",
                        run_id="run-1",
                        base_commit=base,
                        fencing_token=token,
                    )

            self.assertEqual(
                git(repo, "worktree", "list", "--porcelain").stdout.count("worktree "),
                1,
            )
            self.assertEqual(
                git(repo, "branch", "--format=%(refname:short)").stdout.splitlines(),
                ["main"],
            )


def init_git_repo(repo: Path) -> None:
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.com")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "baseline")


def acquire_run(
    repo: Path,
    task_id: str,
    run_id: str,
    *,
    store: RunStore | None = None,
) -> tuple[LockManager, RunStore, str]:
    manager = LockManager(repo / ".vibe-loop" / "locks")
    run_store = store or RunStore(repo / ".vibe-loop" / "runs.jsonl")
    lock = manager.acquire(
        task_id,
        run_id,
        metadata=run_lock_metadata(repo, task_id, run_id),
    )
    return manager, run_store, str(lock.metadata["fencing_token"])


def run_lock_metadata(repo: Path, task_id: str, run_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "base_main": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "started_at": "2026-07-21T00:00:00+00:00",
    }


def primary_snapshot(repo: Path) -> tuple[str, str, str]:
    return (
        git(repo, "branch", "--show-current").stdout.strip(),
        git(repo, "rev-parse", "HEAD").stdout.strip(),
        git(repo, "status", "--short", "--", ".", ":(exclude).vibe-loop").stdout,
    )


def git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


if __name__ == "__main__":
    unittest.main()
