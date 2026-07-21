from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import vibe_loop.runner as runner_module
from vibe_loop.config import (
    AgentConfig,
    AgentSelection,
    CompletionConfig,
    OrchestrationConfig,
    VibeConfig,
    load_config,
    reject_generated_command_adapters,
)
from vibe_loop.orchestration import (
    CandidateCollectionError,
    CandidateCollector,
    GateExecutionError,
    GateRemediationExhausted,
    GateRunner,
    LEGAL_STAGE_TRANSITIONS,
    RuntimeGateController,
    STAGE_FAILURES,
    IllegalStageTransitionError,
    RunContractProposal,
    RunContractResolver,
    RunLifecycleStateMachine,
    RunStage,
    StageFailure,
    WorkspaceProvisionError,
    WorkspaceProvisioner,
    derive_stage_progress,
)
from vibe_loop.runner import VibeRunner
from vibe_loop.locks import LockManager
from vibe_loop.runs import RunStore, WorkerReport
from vibe_loop.tasks import Task
from vibe_loop.workers import claim_worker_workspace


class OrchestrationConfigTests(unittest.TestCase):
    def test_defaults_preserve_worker_owned_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertEqual(config.orchestration.mode, "worker-owned")
        self.assertEqual(config.orchestration.gates, ())
        self.assertEqual(config.orchestration.verify_on_main, ())
        self.assertTrue(config.orchestration.integration_enabled)
        self.assertEqual(
            config.orchestration.task_provenance_mode,
            "external-confirmed",
        )

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
                "integration_enabled = false\n"
                'task_provenance_mode = "external-confirmed"\n',
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
        self.assertFalse(config.orchestration.integration_enabled)

    def test_rejects_modes_routes_and_non_allowlisted_executables(self) -> None:
        cases = (
            ('mode = "other"\n', "orchestration.mode must be one of"),
            ('mode = ""\n', "orchestration.mode must be one of"),
            (
                'mode = "runtime-owned"\n',
                "runtime-owned.*not yet available.*orc-scheduler-separation",
            ),
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
                reviewer_profile="review",
                max_remediation_rounds=7,
                explicit_keys=frozenset({"reviewer_profile", "max_remediation_rounds"}),
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
                gates=("completion.commands[0]",),
                verify_on_main=("completion.commands[0]",),
                explicit_keys=frozenset({"gates", "verify_on_main"}),
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
        config = VibeConfig(repo=Path("/repo"), agent=agent)
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

    def test_candidate_rejects_uncommitted_tracked_changes(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(
            CandidateCollectionError, "uncommitted tracked changes"
        ):
            self.collector.collect_derived()

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


class RunContractJournalTests(unittest.TestCase):
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
            runner = VibeRunner(VibeConfig(repo=repo, agent=agent))
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
                self.assertEqual(env["VIBE_LOOP_WORKTREE"], str(cwd))
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
            ["lock_acquired", "run_contract_resolved", "stage_transition"],
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
            runner = VibeRunner(VibeConfig(repo=repo, agent=agent))
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
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="worker {prompt}",
                        agent_kind="custom",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    ),
                )
            )
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
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="CLAUDE_HOME=.claude claude -p {prompt}",
                        agent_kind="claude",
                    ),
                )
            )
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
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="CODEX_HOME=.codex codex exec {prompt}",
                        agent_kind="codex",
                    ),
                )
            )
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
                ["workspace_provisioned", "workspace_claim"],
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

    def test_recovery_allows_main_to_advance_from_recorded_base(self) -> None:
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

            recovered = WorkspaceProvisioner(
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

            self.assertEqual(recovered.mode, "adopted")
            self.assertEqual(recovered.base_commit, first_base)
            manager.release(manager.current_lock("TASK-01"))
            manager, _, token = acquire_run(
                repo,
                "TASK-01",
                "run-3",
                store=store,
            )

            recovered_again = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=manager,
                run_store=store,
            ).provision(
                task_id="TASK-01",
                run_id="run-3",
                base_commit=current_base,
                fencing_token=token,
                recovery_run_id="run-2",
                recovery_branch=first.branch,
                recovery_worktree=first.worktree,
            )

            self.assertEqual(recovered_again.mode, "adopted")
            self.assertEqual(recovered_again.base_commit, first_base)

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
