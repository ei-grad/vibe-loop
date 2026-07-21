from __future__ import annotations

import hashlib
import json
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
from vibe_loop.orchestration import RunContractProposal, RunContractResolver
from vibe_loop.runner import VibeRunner
from vibe_loop.runs import WorkerReport
from vibe_loop.tasks import Task


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


class RunContractJournalTests(unittest.TestCase):
    def test_contract_is_recorded_after_lock_and_before_activation(self) -> None:
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
            activation_record_types: list[str] = []

            def activate(*args, **kwargs):
                activation_record_types.extend(
                    str(record.get("record_type"))
                    for record in runner.run_store.read_records()
                )
                return None

            def worker(command, cwd, log, **kwargs):
                env = kwargs["env"]
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
                    with patch("vibe_loop.runner.git_rev_parse", return_value="abc123"):
                        with patch("vibe_loop.runner.run_streaming_command", worker):
                            runner.run_task(task)

            records = runner.run_store.read_records()

        record_types = [record.get("record_type") for record in records]
        self.assertEqual(
            activation_record_types,
            ["lock_acquired", "run_contract_resolved"],
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


if __name__ == "__main__":
    unittest.main()
