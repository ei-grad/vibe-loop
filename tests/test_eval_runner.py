from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vibe_loop.autopilot import MaintenanceCommandResult
from vibe_loop.cli import main
from vibe_loop.eval_runner import (
    TrialResult,
    build_aggregate,
    build_eval_prompt,
    render_aggregate_markdown,
    write_task_source_evidence,
    workflow_taxonomy_labels,
)
from vibe_loop.eval_examples import materialize_eval_example
from vibe_loop.config import load_config
from vibe_loop.evals import EVAL_FAILURE_TAXONOMY
from vibe_loop.runner import VibeRunner


class EvalRunnerCliTests(unittest.TestCase):
    def test_task_source_evidence_uses_default_and_generated_runtime_resolution(
        self,
    ) -> None:
        expected = {
            "finite-py-plan-table": ("default_markdown_discovery", "FPY-01"),
            "generated-roadmap-profile": ("generated_cache", "ROAD-02"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case_id, (origin, task_id) in expected.items():
                with self.subTest(case=case_id):
                    repo = materialize_eval_example(case_id, root / case_id)
                    artifact_root = root / f"{case_id}-artifacts"
                    artifact_root.mkdir()

                    write_task_source_evidence(artifact_root, load_config(repo))
                    evidence = json.loads(
                        (artifact_root / "task-source-evidence.json").read_text(
                            encoding="utf-8"
                        )
                    )

                    self.assertEqual(evidence["origin"], origin)
                    self.assertEqual(evidence["selected_task"]["id"], task_id)

    def test_negative_case_passes_and_matches_golden_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_agent.py"
            write_negative_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
            )

        golden = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "eval"
                / "aggregate-negative-pass.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stable_aggregate(payload), golden)

    def test_positive_case_passes_with_stub_agent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "finite_agent.py"
            write_finite_agent(agent, pass_trial=True)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "finite-py-plan-table"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            git_before = json.loads(
                (trial_root / "git-state-before.json").read_text(encoding="utf-8")
            )
            run_log_exists = (trial_root / "logs" / "run.log").is_file()
            diff_exists = (trial_root / "diff.patch").is_file()
            repo = trial_root / "repo"
            grader_spec_visible = (repo / "eval" / "expected-artifacts.json").exists()
            grader_dir_visible = (repo / "eval" / "graders").exists()

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["scoring"]["workflow_score"], 1.0)
        self.assertEqual(len(git_before["head"]), 40)
        self.assertFalse(git_before["dirty"])
        self.assertTrue(run_log_exists)
        self.assertTrue(diff_exists)
        self.assertFalse(grader_spec_visible)
        self.assertFalse(grader_dir_visible)

    def test_command_backend_case_uses_configured_lock_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "command_hooks_agent.py"
            write_command_hooks_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "command-hooks-task-source",
                "--condition",
                "vibe_loop_cli",
                "--agent-command",
                f"vibe_loop_cli={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "command-hooks-task-source"
                / "vibe_loop_cli"
                / "trial-1"
            )
            lock_evidence = json.loads(
                (trial_root / "lock-evidence.json").read_text(encoding="utf-8")
            )
            task_source_evidence = json.loads(
                (trial_root / "task-source-evidence.json").read_text(encoding="utf-8")
            )
            hook_evidence = json.loads(
                (trial_root / "hook-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop_cli"]["pass_rate"], 1.0)
        self.assertEqual(lock_evidence["before"]["task_id"], "HOOK-02")
        self.assertEqual(lock_evidence["before"]["run_id"], "eval-run-command-hooks")
        selected = task_source_evidence["selected_task"]
        self.assertEqual(selected["id"], "HOOK-02")
        self.assertEqual(selected["status"], "Planned")
        self.assertEqual(
            selected["requirement_ids"],
            ["PRD-TSK-001", "PRD-TSK-003", "PRD-WRK-011"],
        )
        self.assertEqual(
            [event["kind"] for event in hook_evidence["events"]],
            ["completion", "worklog", "planning"],
        )
        self.assertEqual(
            [
                (result["kind"], result["index"], result["exit_code"])
                for result in hook_evidence["results"]
            ],
            [
                ("completion", 1, 0),
                ("completion", 2, 0),
                ("planning", 1, 0),
            ],
        )
        self.assertEqual(
            hook_evidence["runtime"],
            {
                "completion_classification": "completed",
                "completion_classification_source": "task_probe",
                "planning_actions": ["ran_planning_command:exit=0"],
                "planning_cycle_status": "idle",
            },
        )
        encoded = json.dumps(
            {
                "locks": lock_evidence,
                "tasks": task_source_evidence,
                "hooks": hook_evidence,
            }
        )
        self.assertNotIn("scripts/lock_adapter.py", encoded)
        self.assertNotIn("scripts/task_adapter.py", encoded)
        self.assertNotIn("scripts/completion_hook.py", encoded)
        self.assertNotIn("scripts/planning_hook.py", encoded)

    def test_command_backend_case_fails_when_production_completion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "command_hooks_agent.py"
            write_command_hooks_agent(agent)

            def fail_completion(_runner, log) -> str:
                log.write("completion check exit_code=7: redacted\n")
                return "completion check failed"

            with patch.object(
                VibeRunner,
                "run_completion_checks",
                autospec=True,
                side_effect=fail_completion,
            ):
                payload = run_eval(
                    root,
                    "--case",
                    "command-hooks-task-source",
                    "--condition",
                    "vibe_loop_cli",
                    "--agent-command",
                    f"vibe_loop_cli={agent}",
                )
            evidence = command_hook_evidence(root)

        self.assertEqual(payload["conditions"]["vibe_loop_cli"]["pass_rate"], 0.0)
        self.assertEqual(evidence["runtime"]["completion_classification"], "failed")
        self.assertEqual(evidence["results"][0]["exit_code"], 7)

    def test_command_backend_case_fails_when_production_planning_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "command_hooks_agent.py"
            write_command_hooks_agent(agent)

            def fail_planning(_command, kind, cycle_id, **_kwargs):
                return MaintenanceCommandResult(
                    kind=kind,
                    cycle_id=cycle_id,
                    exit_code=9,
                    duration_seconds=0.0,
                    output="failed",
                    output_truncated=False,
                    timed_out=False,
                )

            with patch(
                "vibe_loop.eval_runner.run_maintenance_command",
                side_effect=fail_planning,
            ):
                payload = run_eval(
                    root,
                    "--case",
                    "command-hooks-task-source",
                    "--condition",
                    "vibe_loop_cli",
                    "--agent-command",
                    f"vibe_loop_cli={agent}",
                )
            evidence = command_hook_evidence(root)

        self.assertEqual(payload["conditions"]["vibe_loop_cli"]["pass_rate"], 0.0)
        self.assertEqual(
            evidence["runtime"]["planning_actions"],
            ["ran_planning_command:exit=9"],
        )
        self.assertEqual(evidence["results"][-1]["exit_code"], 9)

    def test_completion_success_cannot_replace_task_source_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "command_hooks_agent.py"
            write_command_hooks_agent(agent, complete_task=False)

            def pass_completion(_runner, log) -> str:
                log.write("completion check exit_code=0: redacted\n" * 2)
                return ""

            with patch.object(
                VibeRunner,
                "run_completion_checks",
                autospec=True,
                side_effect=pass_completion,
            ):
                payload = run_eval(
                    root,
                    "--case",
                    "command-hooks-task-source",
                    "--condition",
                    "vibe_loop_cli",
                    "--agent-command",
                    f"vibe_loop_cli={agent}",
                )
            evidence = command_hook_evidence(root)

        self.assertEqual(payload["conditions"]["vibe_loop_cli"]["pass_rate"], 0.0)
        self.assertEqual(evidence["runtime"]["completion_classification"], "unknown")
        self.assertEqual(
            evidence["runtime"]["completion_classification_source"], "fallback"
        )

    def test_eval_command_refuses_nested_eval_worker(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        previous = os.environ.get("VIBE_LOOP_EVAL_ACTIVE")
        os.environ["VIBE_LOOP_EVAL_ACTIVE"] = "1"
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "eval",
                        "local-demo",
                        "--case",
                        "finite-py-plan-table",
                        "--condition",
                        "vibe_loop",
                    ]
                )
        finally:
            if previous is None:
                os.environ.pop("VIBE_LOOP_EVAL_ACTIVE", None)
            else:
                os.environ["VIBE_LOOP_EVAL_ACTIVE"] = previous

        self.assertEqual(exit_code, 2)
        self.assertIn("refusing nested vibe-loop eval", stderr.getvalue())

    def test_orchestrated_condition_rejects_labels_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "orchestrated_labels_only_agent.py"
            write_orchestrated_agent(agent, include_evidence=False)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "orchestrated_vibe_loop",
                "--agent-command",
                f"orchestrated_vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "finite-py-plan-table"
                / "orchestrated_vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            prompt = (trial_root / "eval-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(
            payload["conditions"]["orchestrated_vibe_loop"]["pass_rate"],
            0.0,
        )
        self.assertIn("workflow_contract", record["failure_taxonomy"])
        self.assertEqual(
            record["skill_condition"]["skill_id"],
            "orchestrated-vibe-loop",
        )
        self.assertEqual(record["task"]["expected_skill"], "orchestrated-vibe-loop")
        self.assertTrue(prompt.startswith("$orchestrated-vibe-loop "))

    def test_orchestrated_condition_rejects_wrong_delegation_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "orchestrated_wrong_order_agent.py"
            write_orchestrated_agent(agent, wrong_order=True)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "orchestrated_vibe_loop",
                "--agent-command",
                f"orchestrated_vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "finite-py-plan-table"
                / "orchestrated_vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(
            payload["conditions"]["orchestrated_vibe_loop"]["pass_rate"],
            0.0,
        )
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_orchestrated_condition_passes_with_delegation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "orchestrated_agent.py"
            write_orchestrated_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "orchestrated_vibe_loop",
                "--agent-command",
                f"orchestrated_vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "finite-py-plan-table"
                / "orchestrated_vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(
            payload["conditions"]["orchestrated_vibe_loop"]["pass_rate"],
            1.0,
        )
        self.assertEqual(record["task"]["expected_skill"], "orchestrated-vibe-loop")

    def test_orchestrated_review_remediation_requires_remediator_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "orchestrated_review_agent.py"
            write_orchestrated_review_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "review-remediation",
                "--condition",
                "orchestrated_vibe_loop",
                "--agent-command",
                f"orchestrated_vibe_loop={agent}",
            )

        self.assertEqual(
            payload["conditions"]["orchestrated_vibe_loop"]["pass_rate"],
            1.0,
        )

    def test_orchestrated_prompt_builder_uses_skill_reference(self) -> None:
        self.assertEqual(
            build_eval_prompt("Do the task", "orchestrated_vibe_loop"),
            "$orchestrated-vibe-loop Do the task",
        )

    def test_timeout_keeps_failed_trial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "sleep_agent.py"
            write_python_executable(
                agent,
                "import time\ntime.sleep(2)\n",
            )

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--timeout-seconds",
                "1",
                "--agent-command",
                f"no_skill={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            run_log_exists = (trial_root / "logs" / "run.log").is_file()

        self.assertEqual(record["status"], "timeout")
        self.assertIn("timeout", record["failure_taxonomy"])
        self.assertTrue(run_log_exists)
        self.assertEqual(payload["records"][0]["status"], "timeout")

    def test_unsafe_command_is_refused_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                "no_skill=git reset --hard",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            log = (trial_root / "logs" / "run.log").read_text(encoding="utf-8")
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertIn("refused unsafe command", log)
        self.assertIn("unsafe_git", record["failure_taxonomy"])
        self.assertEqual(
            payload["conditions"]["no_skill"]["failure_taxonomy"]["unsafe_git"], 1
        )

    def test_output_budget_failure_remains_in_primary_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "chatty_agent.py"
            write_python_executable(agent, "print('x' * 200)\n")

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--max-output-bytes",
                "20",
                "--agent-command",
                f"no_skill={agent}",
            )
            condition = payload["conditions"]["no_skill"]

        self.assertEqual(condition["primary_trials"], 1)
        self.assertEqual(condition["pass_rate"], 0.0)
        self.assertEqual(condition["failure_taxonomy"]["workflow_contract"], 1)
        self.assertNotIn("harness_error", condition["failure_taxonomy"])

    def test_negative_prompt_metrics_sum_per_prompt_usage_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_metrics_agent.py"
            write_negative_metrics_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
            )
            condition = payload["conditions"]["no_skill"]

        self.assertEqual(condition["command_count"]["mean"], 16.0)
        self.assertEqual(condition["token_total"], 24.0)
        self.assertEqual(condition["cost_total"], 0.8)

    def test_missing_worker_report_is_a_workflow_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "missing_report_agent.py"
            write_worker_agent_without_report(agent)

            payload = run_eval(
                root,
                "--case",
                "supervised-worker-report",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "supervised-worker-report"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            graders = json.loads(
                (trial_root / "grader-outputs.json").read_text(encoding="utf-8")
            )

        self.assertIn("workflow_contract", record["failure_taxonomy"])
        self.assertNotIn("task_outcome", record["failure_taxonomy"])
        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertIn("report_evidence.latest.run_id", json.dumps(graders))

    def test_seeded_worker_report_run_id_can_pass_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "worker_report_agent.py"
            write_worker_agent_with_report(agent)

            payload = run_eval(
                root,
                "--case",
                "supervised-worker-report",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "supervised-worker-report"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["run_id"], "eval-run-wrk-01")
        self.assertEqual(record["structured_result"]["run_id"], "eval-run-wrk-01")

    def test_main_integration_lock_evidence_from_agent_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "main_integration_agent.py"
            write_main_integration_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "main-integration-lock",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "main-integration-lock"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            lock_evidence = json.loads(
                (trial_root / "lock-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["run_id"], "eval-run-mil-01")
        self.assertEqual(lock_evidence["acquire"]["run_id"], "eval-run-mil-01")
        self.assertFalse(lock_evidence["final_status"]["locked"])

    def test_workspace_blocker_cases_pass_with_blocked_reports(self) -> None:
        scenarios = [
            (
                "workspace-duplicate-worktree",
                "DUP-01",
                "eval-run-dup-01",
                "blocked: duplicate_branch_worktrees",
                ["duplicate_branch_worktrees"],
            ),
            (
                "workspace-missing-worktree",
                "MISS-01",
                "eval-run-miss-01",
                "blocked: missing_claimed_worktree",
                ["missing_claimed_worktree"],
            ),
            (
                "workspace-merged-branch",
                "MERGED-01",
                "eval-run-merged-01",
                "blocked: branch_already_merged",
                ["branch_already_merged"],
            ),
            (
                "workspace-foreign-dirty",
                "DIRTY-01",
                "eval-run-dirty-01",
                "blocked: foreign_dirty_claimed_worktree",
                ["foreign_dirty_claimed_worktree"],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case_id, task_id, run_id, message, codes in scenarios:
                with self.subTest(case=case_id):
                    agent = root / f"{case_id}_agent.py"
                    write_blocked_report_agent(
                        agent,
                        task_id=task_id,
                        run_id=run_id,
                        message=message,
                        event="workspace_preflight_blocked",
                    )

                    payload = run_eval(
                        root,
                        "--case",
                        case_id,
                        "--condition",
                        "vibe_loop",
                        "--agent-command",
                        f"vibe_loop={agent}",
                    )
                    trial_root = (
                        root
                        / "eval-runs"
                        / "local-demo-v1"
                        / "cases"
                        / case_id
                        / "vibe_loop"
                        / "trial-1"
                    )
                    record = json.loads(
                        (trial_root / "run.json").read_text(encoding="utf-8")
                    )
                    workspace_evidence = json.loads(
                        (trial_root / "workspace-evidence.json").read_text(
                            encoding="utf-8"
                        )
                    )

                    self.assertEqual(
                        payload["conditions"]["vibe_loop"]["pass_rate"], 1.0
                    )
                    self.assertEqual(record["status"], "passed")
                    self.assertEqual(record["failure_taxonomy"], [])
                    self.assertEqual(
                        record["structured_result"]["task_status"], "blocked"
                    )
                    self.assertFalse(record["structured_result"]["task_completed"])
                    self.assertEqual(
                        workspace_evidence["by_task"][task_id]["diagnostic_codes"],
                        codes,
                    )

    def test_integration_lock_unavailable_passes_with_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "integration_blocked_agent.py"
            write_blocked_report_agent(
                agent,
                task_id="BUSY-01",
                run_id="eval-run-busy-01",
                message="blocked: main-integration lock unavailable",
                event="integration_lock_busy_observed",
            )

            payload = run_eval(
                root,
                "--case",
                "integration-lock-unavailable",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "integration-lock-unavailable"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            lock_evidence = json.loads(
                (trial_root / "lock-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["structured_result"]["task_status"], "blocked")
        self.assertEqual(
            lock_evidence["main_integration_status"]["owner_task_id"],
            "OTHER-01",
        )
        self.assertEqual(lock_evidence["main_integration_status"]["state"], "held")

    def test_integration_lock_case_rejects_hidden_main_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "illegal_merge_agent.py"
            write_illegal_main_merge_agent(agent, stream_events=True)

            payload = run_eval(
                root,
                "--case",
                "integration-lock-unavailable",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "integration-lock-unavailable"
                / "vibe_loop"
                / "trial-1"
            )
            workflow_events = json.loads(
                (trial_root / "workflow-events.json").read_text(encoding="utf-8")
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertIn("main_fast_forwarded", workflow_events["events"])
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_integration_lock_case_rejects_hidden_main_ref_movement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "hidden_ref_move_agent.py"
            write_illegal_main_merge_agent(
                agent,
                stream_events=True,
                hidden_branch=True,
            )

            payload = run_eval(
                root,
                "--case",
                "integration-lock-unavailable",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "integration-lock-unavailable"
                / "vibe_loop"
                / "trial-1"
            )
            workflow_events = json.loads(
                (trial_root / "workflow-events.json").read_text(encoding="utf-8")
            )
            final_state = json.loads(
                (trial_root / "final-repo-state.json").read_text(encoding="utf-8")
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertEqual(final_state["branch"], "hidden-worker")
        self.assertIn("main_fast_forwarded", workflow_events["events"])
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_integration_lock_case_rejects_hidden_main_ref_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "hidden_ref_remove_agent.py"
            write_illegal_main_merge_agent(
                agent,
                stream_events=True,
                hidden_branch=True,
                remove_main=True,
            )

            payload = run_eval(
                root,
                "--case",
                "integration-lock-unavailable",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "integration-lock-unavailable"
                / "vibe_loop"
                / "trial-1"
            )
            workflow_events = json.loads(
                (trial_root / "workflow-events.json").read_text(encoding="utf-8")
            )
            final_state = json.loads(
                (trial_root / "final-repo-state.json").read_text(encoding="utf-8")
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertNotIn("main", final_state["branch_heads"])
        self.assertIn("main_fast_forwarded", workflow_events["events"])
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_workspace_dirty_case_rejects_foreign_content_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "dirty_mutating_agent.py"
            write_dirty_mutating_blocked_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "workspace-foreign-dirty",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "workspace-foreign-dirty"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_malformed_blocked_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "malformed_report_agent.py"
            write_malformed_blocked_report_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "workspace-duplicate-worktree",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "workspace-duplicate-worktree"
                / "vibe_loop"
                / "trial-1"
            )
            report_evidence = json.loads(
                (trial_root / "report-evidence.json").read_text(encoding="utf-8")
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertIsNone(report_evidence["latest"])
        self.assertIn("workflow_contract", record["failure_taxonomy"])
        self.assertNotIn("task_outcome", record["failure_taxonomy"])

    def test_report_evidence_ignores_agent_spoofed_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "spoofed_report_evidence_agent.py"
            write_spoofed_report_evidence_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "workspace-duplicate-worktree",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "workspace-duplicate-worktree"
                / "vibe_loop"
                / "trial-1"
            )
            report_evidence = json.loads(
                (trial_root / "report-evidence.json").read_text(encoding="utf-8")
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertEqual(report_evidence["latest"]["message"], "blocked")
        self.assertIn("workflow_contract", record["failure_taxonomy"])

    def test_lock_evidence_ignores_agent_spoofed_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "spoofed_lock_evidence_agent.py"
            write_spoofed_lock_evidence_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "integration-lock-unavailable",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "integration-lock-unavailable"
                / "vibe_loop"
                / "trial-1"
            )
            lock_evidence = json.loads(
                (trial_root / "lock-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertTrue(lock_evidence["main_integration_status"]["locked"])
        self.assertEqual(lock_evidence["main_integration_status"]["state"], "held")

    def test_missing_blocked_report_is_workflow_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "workspace_no_report_agent.py"
            write_workspace_agent_without_report(agent)

            payload = run_eval(
                root,
                "--case",
                "workspace-missing-worktree",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "workspace-missing-worktree"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertIn("workflow_contract", record["failure_taxonomy"])
        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertEqual(
            payload["skill_quality"]["failure_categories"][
                "workflow_contract_failures"
            ]["count"],
            1,
        )

    def test_workspace_blocker_requires_explicit_skill_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "no_skill_event_agent.py"
            write_blocked_report_agent(
                agent,
                task_id="DUP-01",
                run_id="eval-run-dup-01",
                message="blocked: duplicate_branch_worktrees",
                event="workspace_preflight_blocked",
                include_skill_event=False,
            )

            payload = run_eval(
                root,
                "--case",
                "workspace-duplicate-worktree",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "workspace-duplicate-worktree"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            workflow_events = json.loads(
                (trial_root / "workflow-events.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertNotIn("skill_activated", workflow_events["events"])
        self.assertIn("trigger_false_negative", record["failure_taxonomy"])

    def test_transcript_grader_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_agent.py"
            grader = root / "transcript_grader.py"
            write_negative_agent(agent)
            write_python_executable(
                grader,
                "import json\n"
                "print(json.dumps({\n"
                "    'id': 'unsafe-scan',\n"
                "    'passed': False,\n"
                "    'failure_taxonomy': ['unsafe_git'],\n"
                "    'workflow_events': ['unsafe_git_command'],\n"
                "    'metrics': {'tokens': 42, 'cost_usd': 0.25},\n"
                "}))\n",
            )

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
                "--transcript-grader",
                str(grader),
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            graders = json.loads(
                (trial_root / "grader-outputs.json").read_text(encoding="utf-8")
            )

        self.assertIn("unsafe_git", record["failure_taxonomy"])
        self.assertEqual(
            payload["conditions"]["no_skill"]["failure_taxonomy"]["unsafe_git"], 1
        )
        self.assertEqual(payload["conditions"]["no_skill"]["token_total"], 42.0)
        self.assertEqual(payload["conditions"]["no_skill"]["cost_total"], 0.25)
        self.assertIn("unsafe-scan", json.dumps(graders))

    def test_flaky_trials_are_summarized_by_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "flaky_agent.py"
            write_finite_agent(agent, pass_trial=False)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "vibe_loop",
                "--trials",
                "2",
                "--agent-command",
                f"vibe_loop={agent}",
            )

        condition = payload["conditions"]["vibe_loop"]
        self.assertEqual(condition["pass_count"], 1)
        self.assertEqual(condition["pass_rate"], 0.5)
        self.assertEqual(condition["flaky_case_ids"], ["finite-py-plan-table"])
        self.assertEqual(condition["failure_taxonomy"]["flaky"], 1)

    def test_skill_quality_report_matches_snapshots_and_covers_taxonomy(self) -> None:
        records = load_skill_quality_records()
        aggregate = build_aggregate(
            [
                TrialResult(record=record, artifact_root=Path("."), repo=Path("."))
                for record in records
            ],
            output_root=Path("/tmp/eval-runs/local-demo-v1"),
            previous_aggregate=PRIOR_RUN_SNAPSHOT,
        )
        markdown = render_aggregate_markdown(aggregate)
        skill_quality_markdown = markdown[markdown.index("## Skill Quality") :]

        observed_labels = {
            label for record in records for label in record.get("failure_taxonomy", [])
        }

        self.assertEqual(observed_labels, EVAL_FAILURE_TAXONOMY)
        self.assertEqual(
            stable_quality_snapshot(aggregate["skill_quality"]), QUALITY_JSON_SNAPSHOT
        )
        self.assertEqual(skill_quality_markdown, QUALITY_MARKDOWN_SNAPSHOT)

    def test_overwrite_rerun_archives_prior_artifacts_for_regression_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_agent.py"
            write_negative_agent(agent)

            run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
            )
            external_secret = root / "outside-secret.txt"
            external_secret.write_text("do not archive this target\n", encoding="utf-8")
            prior_trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            symlink_path = prior_trial_root / "repo" / "archive-leak.txt"
            try:
                os.symlink(external_secret, symlink_path)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--overwrite",
                "--agent-command",
                "no_skill=git reset --hard",
            )
            regressions = payload["skill_quality"]["prior_run_regressions"]
            previous_root = regressions[0]["previous_records"][0]["artifact_root"]
            current_root = regressions[0]["records"][0]["artifact_root"]
            archived_log = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / previous_root
                / "logs"
                / "run.log"
            )
            archived_symlink = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / previous_root
                / "repo"
                / "archive-leak.txt"
            )
            archived_log_exists = archived_log.is_file()
            archived_symlink_exists = archived_symlink.exists()

        self.assertTrue(
            previous_root.startswith("history/previous-"),
            f"expected history/previous-... but got: {previous_root!r}",
        )
        self.assertTrue(archived_log_exists)
        self.assertFalse(archived_symlink_exists)
        self.assertEqual(current_root, "cases/negative-trigger-set/no_skill/trial-1")
        self.assertIn("pass_rate_regression", regressions[0]["regression_flags"])

    def test_legacy_prior_aggregate_metrics_and_records_are_normalized(self) -> None:
        records = load_skill_quality_records()
        legacy_previous = {
            "generated_at": "2026-05-08T00:00:00+00:00",
            "conditions": {
                "vibe_loop": {
                    "trials": 4,
                    "pass_rate": 0.5,
                    "latency_seconds": {"mean": 20.0},
                    "command_count": {"mean": 10.0},
                    "token_total": 320.0,
                    "cost_total": 0.4,
                }
            },
            "records": [
                {
                    "run_id": "legacy-skill-1",
                    "case_id": "finite-py-plan-table",
                    "condition": "vibe_loop",
                    "trial": 1,
                    "artifact_root": "cases/finite-py-plan-table/vibe_loop/trial-0",
                    "failure_taxonomy": [],
                }
            ],
        }
        aggregate = build_aggregate(
            [
                TrialResult(record=record, artifact_root=Path("."), repo=Path("."))
                for record in records
            ],
            output_root=Path("/tmp/eval-runs/local-demo-v1"),
            previous_aggregate=legacy_previous,
        )
        regression = aggregate["skill_quality"]["prior_run_regressions"][0]

        self.assertEqual(regression["deltas"]["cost_per_trial"], 0.13)
        self.assertEqual(regression["deltas"]["token_per_trial"], 17.5)
        self.assertEqual(
            regression["previous_records"][0]["artifact_root"],
            "cases/finite-py-plan-table/vibe_loop/trial-0",
        )

    def test_prior_run_regression_records_redact_raw_hook_commands(self) -> None:
        sentinel = "RAW_HOOK_COMMAND_MUST_NOT_LEAK"
        records = load_skill_quality_records()
        previous = {
            "generated_at": "2026-05-08T00:00:00+00:00",
            "skill_quality": {
                "conditions": {
                    "vibe_loop": {
                        "pass_rate": 1.0,
                        "task_score_mean": 1.0,
                        "workflow_score_mean": 1.0,
                        "trigger_score_mean": 1.0,
                        "workflow_violation_rate": 0.0,
                        "trigger_miss_rate": 0.0,
                        "latency_seconds_mean": 1.0,
                        "command_count_mean": 1.0,
                        "records": [
                            {
                                "run_id": "prior-hook-run",
                                "case_id": "command-hooks-task-source",
                                "condition": "vibe_loop",
                                "trial": 1,
                                "artifact_root": "cases/prior-hook-run",
                                "failure_taxonomy": [],
                                "harness": {"command": sentinel},
                                "task_source": {"list_command": sentinel},
                                "locks": {"acquire_command": sentinel},
                                "completion": {"commands": [sentinel]},
                                "autopilot": {"planning_command": sentinel},
                                "worklog": {"command": sentinel},
                            }
                        ],
                    }
                }
            },
        }

        aggregate = build_aggregate(
            [
                TrialResult(record=record, artifact_root=Path("."), repo=Path("."))
                for record in records
            ],
            output_root=Path("/tmp/eval-runs/local-demo-v1"),
            previous_aggregate=previous,
        )

        encoded = json.dumps(aggregate)
        self.assertNotIn(sentinel, encoded)
        regression = aggregate["skill_quality"]["prior_run_regressions"][0]
        previous_ref = regression["previous_records"][0]
        self.assertEqual(previous_ref["run_id"], "prior-hook-run")
        self.assertEqual(previous_ref["artifact_root"], "cases/prior-hook-run")

    def test_workflow_taxonomy_labels_are_derived_from_artifact_messages(self) -> None:
        self.assertEqual(
            workflow_taxonomy_labels(
                "missing events: review_requested, rereview_requested"
            ),
            {"review_missing"},
        )
        self.assertEqual(
            workflow_taxonomy_labels(
                "missing events: main_integration_lock_acquired, main_verification_ran"
            ),
            {"integration_missing"},
        )
        self.assertEqual(
            workflow_taxonomy_labels("forbidden events: unnecessary_user_prompt"),
            {"unnecessary_user_prompt"},
        )
        self.assertEqual(
            workflow_taxonomy_labels(
                "forbidden events: review_requested, main_fast_forwarded"
            ),
            set(),
        )
        self.assertEqual(
            workflow_taxonomy_labels(
                "workflow event order missing: instructions_inspected -> "
                "review_requested -> main_fast_forwarded"
            ),
            set(),
        )
        self.assertEqual(
            workflow_taxonomy_labels("missing events: integration_lock_busy_observed"),
            {"integration_missing"},
        )
        self.assertEqual(
            workflow_taxonomy_labels("forbidden events: destructive_workspace_cleanup"),
            {"unsafe_git"},
        )


def run_eval(root: Path, *args: str) -> dict[str, object]:
    stdout = StringIO()
    stderr = StringIO()
    output = root / "eval-runs"
    argv = [
        "eval",
        "local-demo",
        "--output",
        str(output),
        "--json",
        *args,
    ]
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    if exit_code != 0:
        raise AssertionError(stderr.getvalue() + stdout.getvalue())
    return json.loads(stdout.getvalue())


def load_skill_quality_records() -> list[dict[str, object]]:
    return json.loads(
        (
            Path(__file__).parent / "fixtures" / "eval" / "skill-quality-records.json"
        ).read_text(encoding="utf-8")
    )


def stable_quality_snapshot(payload: dict[str, object]) -> dict[str, object]:
    comparisons = payload["condition_comparisons"]
    categories = payload["failure_categories"]
    return {
        "condition_comparisons": {
            condition: {
                "deltas": comparison["deltas"],
                "regression_flags": comparison["regression_flags"],
                "baseline_records": record_locations(comparison["baseline_records"]),
                "condition_records": record_locations(comparison["condition_records"]),
            }
            for condition, comparison in comparisons.items()
        },
        "failure_categories": {
            category: {
                "count": summary["count"],
                "records": record_locations(summary["records"]),
            }
            for category, summary in categories.items()
            if summary["count"]
        },
        "overlong_trajectories": {
            "count": payload["overlong_trajectories"]["count"],
            "records": record_locations(payload["overlong_trajectories"]["records"]),
        },
        "cost_regressions": [
            {
                "condition": regression["condition"],
                "delta": regression["delta"],
                "baseline_records": record_locations(regression["baseline_records"]),
                "records": record_locations(regression["records"]),
            }
            for regression in payload["cost_regressions"]
        ],
        "prior_run_regressions": [
            {
                "condition": regression["condition"],
                "deltas": regression["deltas"],
                "regression_flags": regression["regression_flags"],
                "previous_records": record_locations(regression["previous_records"]),
                "records": record_locations(regression["records"]),
            }
            for regression in payload["prior_run_regressions"]
        ],
        "per_task_uplift": stable_uplift(payload["per_task_uplift"]),
        "per_domain_uplift": stable_uplift(payload["per_domain_uplift"]),
    }


def stable_uplift(payload: dict[str, object]) -> dict[str, object]:
    return {
        group: {
            condition: {
                "baseline_pass_rate": summary["baseline_pass_rate"],
                "pass_rate": summary["pass_rate"],
                "absolute_uplift": summary["absolute_uplift"],
                "normalized_gain": summary["normalized_gain"],
                "baseline_records": record_locations(summary["baseline_records"]),
                "condition_records": record_locations(summary["condition_records"]),
            }
            for condition, summary in conditions.items()
        }
        for group, conditions in payload.items()
    }


def record_locations(records: list[dict[str, object]]) -> list[str]:
    return [f"{record['run_id']}@{record['artifact_root']}" for record in records]


PRIOR_RUN_SNAPSHOT = {
    "generated_at": "2026-05-08T00:00:00+00:00",
    "skill_quality": {
        "conditions": {
            "vibe_loop": {
                "pass_rate": 0.5,
                "task_score_mean": 0.8,
                "workflow_score_mean": 0.8,
                "trigger_score_mean": 0.8,
                "workflow_violation_rate": 0.1,
                "trigger_miss_rate": 0.0,
                "latency_seconds_mean": 20.0,
                "command_count_mean": 10.0,
                "token_per_trial": 80.0,
                "cost_per_trial": 0.1,
                "records": [
                    {
                        "run_id": "prior-skill-1",
                        "artifact_root": "cases/finite-py-plan-table/vibe_loop/trial-0",
                    }
                ],
            }
        }
    },
}


QUALITY_JSON_SNAPSHOT = {
    "condition_comparisons": {
        "vibe_loop": {
            "baseline_records": [
                "base-finite-1@cases/finite-py-plan-table/no_skill/trial-1",
                "base-negative-1@cases/negative-trigger-set/no_skill/trial-1",
                "base-main-1@cases/main-integration-lock/no_skill/trial-1",
                "base-discovery-1@cases/generated-roadmap-profile/no_skill/trial-1",
            ],
            "condition_records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
            "deltas": {
                "command_count_mean": 21.75,
                "cost_per_trial": 0.1475,
                "latency_seconds_mean": 24.5,
                "pass_rate": -0.75,
                "task_score_mean": 0.0,
                "token_per_trial": 32.5,
                "trigger_miss_rate": 0.5,
                "trigger_score_mean": -0.5,
                "workflow_score_mean": -1.0,
                "workflow_violation_rate": 1.0,
            },
            "regression_flags": [
                "pass_rate_regression",
                "workflow_contract_regression",
                "skill_trigger_regression",
                "trajectory_length_regression",
                "cost_regression",
            ],
        }
    },
    "cost_regressions": [
        {
            "condition": "vibe_loop",
            "delta": 0.1475,
            "baseline_records": [
                "base-finite-1@cases/finite-py-plan-table/no_skill/trial-1",
                "base-negative-1@cases/negative-trigger-set/no_skill/trial-1",
                "base-main-1@cases/main-integration-lock/no_skill/trial-1",
                "base-discovery-1@cases/generated-roadmap-profile/no_skill/trial-1",
            ],
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        }
    ],
    "prior_run_regressions": [
        {
            "condition": "vibe_loop",
            "deltas": {
                "command_count_mean": 19.25,
                "cost_per_trial": 0.13,
                "latency_seconds_mean": 19.5,
                "pass_rate": -0.5,
                "task_score_mean": -0.05,
                "token_per_trial": 17.5,
                "trigger_miss_rate": 0.5,
                "trigger_score_mean": -0.3,
                "workflow_score_mean": -0.8,
                "workflow_violation_rate": 0.9,
            },
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
            "previous_records": [
                "prior-skill-1@cases/finite-py-plan-table/vibe_loop/trial-0",
            ],
            "regression_flags": [
                "pass_rate_regression",
                "task_outcome_regression",
                "workflow_contract_regression",
                "skill_trigger_regression",
                "trajectory_length_regression",
                "cost_regression",
            ],
        }
    ],
    "failure_categories": {
        "flaky_trials": {
            "count": 1,
            "records": [
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        },
        "infrastructure_failures": {
            "count": 1,
            "records": [
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        },
        "integration_discipline_failures": {
            "count": 1,
            "records": [
                "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
            ],
        },
        "review_discipline_failures": {
            "count": 1,
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
            ],
        },
        "secret_or_state_leaks": {
            "count": 1,
            "records": [
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        },
        "skill_trigger_misses": {
            "count": 2,
            "records": [
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        },
        "task_outcome_failures": {
            "count": 2,
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                "base-main-1@cases/main-integration-lock/no_skill/trial-1",
            ],
        },
        "unnecessary_user_prompts": {
            "count": 1,
            "records": [
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
            ],
        },
        "unsafe_git_behavior": {
            "count": 1,
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
            ],
        },
        "workflow_contract_failures": {
            "count": 4,
            "records": [
                "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
            ],
        },
    },
    "overlong_trajectories": {
        "count": 2,
        "records": [
            "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
            "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
        ],
    },
    "per_domain_uplift": {
        "finite_slice": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-finite-1@cases/finite-py-plan-table/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
        "main_integration": {
            "vibe_loop": {
                "absolute_uplift": 0.0,
                "baseline_pass_rate": 0.0,
                "baseline_records": [
                    "base-main-1@cases/main-integration-lock/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                ],
                "normalized_gain": 0.0,
                "pass_rate": 0.0,
            }
        },
        "skill_triggering": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-negative-1@cases/negative-trigger-set/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
        "task_discovery": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-discovery-1@cases/generated-roadmap-profile/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
    },
    "per_task_uplift": {
        "finite-py-plan-table": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-finite-1@cases/finite-py-plan-table/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-finite-1@cases/finite-py-plan-table/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
        "generated-roadmap-profile": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-discovery-1@cases/generated-roadmap-profile/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-discovery-1@cases/generated-roadmap-profile/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
        "main-integration-lock": {
            "vibe_loop": {
                "absolute_uplift": 0.0,
                "baseline_pass_rate": 0.0,
                "baseline_records": [
                    "base-main-1@cases/main-integration-lock/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-main-1@cases/main-integration-lock/vibe_loop/trial-1",
                ],
                "normalized_gain": 0.0,
                "pass_rate": 0.0,
            }
        },
        "negative-trigger-set": {
            "vibe_loop": {
                "absolute_uplift": -1.0,
                "baseline_pass_rate": 1.0,
                "baseline_records": [
                    "base-negative-1@cases/negative-trigger-set/no_skill/trial-1",
                ],
                "condition_records": [
                    "skill-negative-1@cases/negative-trigger-set/vibe_loop/trial-1",
                ],
                "normalized_gain": -1.0,
                "pass_rate": 0.0,
            }
        },
    },
}


QUALITY_MARKDOWN_SNAPSHOT = """## Skill Quality

Baseline condition: `no_skill`

| Condition | Pass delta | Task delta | Workflow delta | Trigger delta | Cost delta | Flags | Baseline records | Current records |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| vibe_loop | -0.75 | +0 | -1 | -0.5 | +0.1475 | pass_rate_regression, workflow_contract_regression, skill_trigger_regression, trajectory_length_regression, cost_regression | base-finite-1 (cases/finite-py-plan-table/no_skill/trial-1), base-negative-1 (cases/negative-trigger-set/no_skill/trial-1), base-main-1 (cases/main-integration-lock/no_skill/trial-1), base-discovery-1 (cases/generated-roadmap-profile/no_skill/trial-1) | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1), skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1), skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1), skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |

### Prior Run Regressions

| Condition | Previous generated at | Flags | Previous records | Current records |
| --- | --- | --- | --- | --- |
| vibe_loop | 2026-05-08T00:00:00+00:00 | pass_rate_regression, task_outcome_regression, workflow_contract_regression, skill_trigger_regression, trajectory_length_regression, cost_regression | prior-skill-1 (cases/finite-py-plan-table/vibe_loop/trial-0) | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1), skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1), skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1), skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |

### Failure Categories

| Category | Count | Conditions | Records |
| --- | ---: | --- | --- |
| task_outcome_failures | 2 | no_skill=1, vibe_loop=1 | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1), base-main-1 (cases/main-integration-lock/no_skill/trial-1) |
| workflow_contract_failures | 4 | vibe_loop=4 | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1), skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1), skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1), skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| skill_trigger_misses | 2 | vibe_loop=2 | skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1), skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| review_discipline_failures | 1 | vibe_loop=1 | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1) |
| integration_discipline_failures | 1 | vibe_loop=1 | skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1) |
| unsafe_git_behavior | 1 | vibe_loop=1 | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1) |
| unnecessary_user_prompts | 1 | vibe_loop=1 | skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1) |
| secret_or_state_leaks | 1 | vibe_loop=1 | skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| infrastructure_failures | 1 | vibe_loop=1 | skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| flaky_trials | 1 | vibe_loop=1 | skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| overlong_trajectories | 2 | vibe_loop=2 | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1), skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1) |

### Per Task Uplift

| Task | Condition | Baseline pass | Pass rate | Uplift | Baseline records | Current records |
| --- | --- | ---: | ---: | ---: | --- | --- |
| finite-py-plan-table | vibe_loop | 1 | 0 | -1 | base-finite-1 (cases/finite-py-plan-table/no_skill/trial-1) | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1) |
| generated-roadmap-profile | vibe_loop | 1 | 0 | -1 | base-discovery-1 (cases/generated-roadmap-profile/no_skill/trial-1) | skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
| main-integration-lock | vibe_loop | 0 | 0 | +0 | base-main-1 (cases/main-integration-lock/no_skill/trial-1) | skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1) |
| negative-trigger-set | vibe_loop | 1 | 0 | -1 | base-negative-1 (cases/negative-trigger-set/no_skill/trial-1) | skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1) |

### Per Domain Uplift

| Domain | Condition | Baseline pass | Pass rate | Uplift | Baseline records | Current records |
| --- | --- | ---: | ---: | ---: | --- | --- |
| finite_slice | vibe_loop | 1 | 0 | -1 | base-finite-1 (cases/finite-py-plan-table/no_skill/trial-1) | skill-finite-1 (cases/finite-py-plan-table/vibe_loop/trial-1) |
| main_integration | vibe_loop | 0 | 0 | +0 | base-main-1 (cases/main-integration-lock/no_skill/trial-1) | skill-main-1 (cases/main-integration-lock/vibe_loop/trial-1) |
| skill_triggering | vibe_loop | 1 | 0 | -1 | base-negative-1 (cases/negative-trigger-set/no_skill/trial-1) | skill-negative-1 (cases/negative-trigger-set/vibe_loop/trial-1) |
| task_discovery | vibe_loop | 1 | 0 | -1 | base-discovery-1 (cases/generated-roadmap-profile/no_skill/trial-1) | skill-discovery-1 (cases/generated-roadmap-profile/vibe_loop/trial-1) |
"""


def stable_aggregate(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": payload["schema_version"],
        "suite_id": payload["suite_id"],
        "total_trials": payload["total_trials"],
        "conditions": {
            condition: {
                key: value
                for key, value in condition_payload.items()
                if key
                in {
                    "trials",
                    "primary_trials",
                    "pass_count",
                    "pass_rate",
                    "confidence_interval_95",
                    "absolute_uplift",
                    "normalized_gain",
                    "failure_taxonomy",
                }
            }
            for condition, condition_payload in payload["conditions"].items()
        },
        "cases": payload["cases"],
        "records": [
            {
                "case_id": record["case_id"],
                "condition": record["condition"],
                "trial": record["trial"],
                "status": record["status"],
                "failure_taxonomy": record["failure_taxonomy"],
            }
            for record in payload["records"]
        ],
    }


def write_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    if sys.platform == "win32":
        cmd = path.with_name(path.name + ".cmd")
        cmd.write_text(
            f'@"{sys.executable}" "%~dp0{path.name}" %*\r\n', encoding="utf-8"
        )


def write_negative_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "prompt_path = os.environ['VIBE_LOOP_EVAL_PROMPT_PATH']\n"
        "if prompt_path.endswith('neg-small-edit-no-skill.txt'):\n"
        "    readme = repo / 'README.md'\n"
        "    readme.write_text(\n"
        "        readme.read_text(encoding='utf-8').replace('teh', 'the'),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "print('## main python workflow-contract task outcome def add multiple space the')\n",
    )


def write_negative_metrics_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "prompt_path = os.environ['VIBE_LOOP_EVAL_PROMPT_PATH']\n"
        "if prompt_path.endswith('neg-small-edit-no-skill.txt'):\n"
        "    readme = repo / 'README.md'\n"
        "    readme.write_text(\n"
        "        readme.read_text(encoding='utf-8').replace('teh', 'the'),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "transcript = '\\n'.join([\n"
        "    json.dumps({'type': 'tool_call', 'name': 'one'}),\n"
        "    json.dumps({'type': 'tool_call', 'name': 'two'}),\n"
        "]) + '\\n'\n"
        "(artifact / 'transcript.jsonl').write_text(transcript, encoding='utf-8')\n"
        "(artifact / 'agent-result.json').write_text(\n"
        "    json.dumps({'usage': {'tokens': 3, 'cost_usd': 0.1}}) + '\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print('## main python workflow-contract task outcome def add multiple space the')\n",
    )


def write_finite_agent(path: Path, *, pass_trial: bool) -> None:
    guard = "os.environ['VIBE_LOOP_EVAL_TRIAL'] == '1'" if not pass_trial else "True"
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(artifact / 'eval-prompt.txt').write_text(\n"
        "    os.environ['VIBE_LOOP_EVAL_PROMPT'], encoding='utf-8'\n"
        ")\n"
        f"if {guard}:\n"
        "    (repo / 'src' / 'finite_math' / 'calculator.py').write_text(\n"
        "        'from __future__ import annotations\\n\\n\\n'\n"
        "        'def loyalty_total(subtotal: int, *, member: bool) -> int:\\n'\n"
        "        '    discount = 10 if member else 0\\n'\n"
        "        '    return subtotal - discount\\n',\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "    plan = repo / 'PLAN.md'\n"
        "    plan.write_text(\n"
        "        plan.read_text(encoding='utf-8').replace(\n"
        "            '| FPY-01 | P0 | Planned |', '| FPY-01 | P0 | Done |'\n"
        "        ),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "    events = [\n"
        "        'skill_activated',\n"
        "        'instructions_inspected',\n"
        "        'worktree_state_inspected',\n"
        "        'branch_or_worktree_created',\n"
        "        'verification_ran',\n"
        "        'review_requested',\n"
        "        'commit_created',\n"
        "        'main_fast_forwarded',\n"
        "        'main_verification_ran',\n"
        "    ]\n"
        "    (artifact / 'workflow-events.json').write_text(\n"
        "        json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        "    )\n"
        "print('finite agent finished')\n",
    )


def command_hook_evidence(root: Path) -> dict[str, object]:
    path = (
        root
        / "eval-runs"
        / "local-demo-v1"
        / "cases"
        / "command-hooks-task-source"
        / "vibe_loop_cli"
        / "trial-1"
        / "hook-evidence.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def write_command_hooks_agent(path: Path, *, complete_task: bool = True) -> None:
    completion = (
        "for task in payload['tasks']:\n"
        "    if task['id'] == 'HOOK-02':\n"
        "        task['status'] = 'Done'\n"
        if complete_task
        else ""
    )
    script = (
        (
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
            "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
            "tasks_path = repo / 'tasks.json'\n"
            "payload = json.loads(tasks_path.read_text(encoding='utf-8'))\n"
        )
        + completion
        + (
            "tasks_path.write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')\n"
            "story = repo / 'docs' / 'selected-story.md'\n"
            "story.write_text('# HOOK-02\\n\\nRequirements: PRD-TSK-001, PRD-TSK-003, PRD-WRK-011\\n', encoding='utf-8')\n"
            "events = [\n"
            "    'skill_activated',\n"
            "    'task_source_inspected',\n"
            "    'task_lock_acquired',\n"
            "    'branch_or_worktree_created',\n"
            "    'verification_ran',\n"
            "    'review_requested',\n"
            "    'commit_created',\n"
            "    'main_fast_forwarded',\n"
            "]\n"
            "(artifact / 'workflow-events.json').write_text(\n"
            "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
            ")\n"
            "print('command hook agent finished')\n"
        )
    )
    write_python_executable(path, script)


def write_orchestrated_agent(
    path: Path,
    *,
    include_evidence: bool = True,
    wrong_order: bool = False,
) -> None:
    events = [
        "skill_activated",
        "instructions_inspected",
        "worktree_state_inspected",
        "exploration_delegated",
        "branch_or_worktree_created",
        "implementation_delegated",
        "verification_ran",
        "review_requested",
        "review_delegated",
        "commit_created",
        "main_fast_forwarded",
        "main_verification_ran",
    ]
    if wrong_order:
        events = [
            "skill_activated",
            "instructions_inspected",
            "worktree_state_inspected",
            "exploration_delegated",
            "branch_or_worktree_created",
            "implementation_delegated",
            "verification_ran",
            "review_requested",
            "commit_created",
            "review_delegated",
            "main_fast_forwarded",
            "main_verification_ran",
        ]
    evidence = {
        "agents": [
            {
                "role": "explorer",
                "agent_id": "explorer-1",
                "prompt": "Inspect FPY-01 scope and tests.",
                "result": "identified calculator and plan row scope",
            },
            {
                "role": "implementer",
                "agent_id": "implementer-1",
                "prompt": "Implement FPY-01 in assigned files.",
                "changed_paths": [
                    "src/finite_math/calculator.py",
                    "PLAN.md",
                ],
                "result": "implemented loyalty discount",
            },
            {
                "role": "reviewer",
                "agent_id": "reviewer-1",
                "prompt": "Review FPY-01 diff and verification evidence.",
                "result": "no material findings",
            },
        ]
    }
    evidence_write = ""
    if include_evidence:
        evidence_write = (
            "(artifact / 'delegation-evidence.json').write_text("
            f"{(json.dumps(evidence, sort_keys=True) + chr(10))!r}, "
            "encoding='utf-8')\n"
        )
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(artifact / 'eval-prompt.txt').write_text(\n"
        "    os.environ['VIBE_LOOP_EVAL_PROMPT'], encoding='utf-8'\n"
        ")\n"
        "(repo / 'src' / 'finite_math' / 'calculator.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def loyalty_total(subtotal: int, *, member: bool) -> int:\\n'\n"
        "    '    discount = 10 if member else 0\\n'\n"
        "    '    return subtotal - discount\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| FPY-01 | P0 | Planned |', '| FPY-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        f"events = {events!r}\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        f"{evidence_write}"
        "print('orchestrated agent finished')\n",
    )


def write_orchestrated_review_agent(path: Path) -> None:
    evidence = {
        "agents": [
            {
                "role": "explorer",
                "agent_id": "explorer-1",
                "prompt": "Inspect REV-01 scope, tests, and review expectations.",
                "result": "identified whitespace-only validation gap",
            },
            {
                "role": "implementer",
                "agent_id": "implementer-1",
                "prompt": "Implement initial REV-01 fix in assigned files.",
                "changed_paths": ["src/review_rules/codes.py"],
                "result": "added input normalization",
            },
            {
                "role": "reviewer",
                "agent_id": "reviewer-1",
                "prompt": "Review REV-01 diff and test evidence.",
                "result": "reported missing whitespace-only regression test",
            },
            {
                "role": "remediator",
                "agent_id": "implementer-1",
                "prompt": "Address review finding by adding regression coverage.",
                "changed_paths": ["tests/test_codes.py"],
                "result": "added whitespace-only regression test",
            },
        ]
    }
    review_evidence = {
        "initial": {"material_findings_count": 1},
        "rereview": {"material_findings_count": 0},
    }
    events = [
        "skill_activated",
        "instructions_inspected",
        "worktree_state_inspected",
        "exploration_delegated",
        "branch_or_worktree_created",
        "implementation_delegated",
        "verification_ran",
        "review_requested",
        "review_delegated",
        "review_finding_received",
        "remediation_delegated",
        "review_finding_addressed",
        "rereview_requested",
        "commit_created",
        "main_fast_forwarded",
    ]
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(artifact / 'eval-prompt.txt').write_text(\n"
        "    os.environ['VIBE_LOOP_EVAL_PROMPT'], encoding='utf-8'\n"
        ")\n"
        "(repo / 'src' / 'review_rules' / 'codes.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def is_valid_code(value: str) -> bool:\\n'\n"
        "    '    value = value.strip()\\n'\n"
        "    '    if not value:\\n'\n"
        "    '        return False\\n'\n"
        '    \'    return value.replace("-", "").isalnum() and "-" in value\\n\',\n'
        "    encoding='utf-8',\n"
        ")\n"
        "tests = repo / 'tests' / 'test_codes.py'\n"
        "tests.write_text(\n"
        "    tests.read_text(encoding='utf-8').replace(\n"
        "        '    def test_rejects_empty_code(self) -> None:\\n'\n"
        "        '        self.assertFalse(is_valid_code(\"\"))\\n',\n"
        "        '    def test_rejects_empty_code(self) -> None:\\n'\n"
        "        '        self.assertFalse(is_valid_code(\"\"))\\n\\n'\n"
        "        '    def test_rejects_whitespace_only_code(self) -> None:\\n'\n"
        "        '        self.assertFalse(is_valid_code(\"   \"))\\n',\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| REV-01 | P0 | Planned |', '| REV-01 | P0 | Done |'\n"
        "    ).replace('Not started.', '`python eval/stubs/reviewer.py`; '\n"
        "              '`python -m unittest discover`.'),\n"
        "    encoding='utf-8',\n"
        ")\n"
        f"events = {events!r}\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "(artifact / 'delegation-evidence.json').write_text("
        f"{(json.dumps(evidence, sort_keys=True) + chr(10))!r}, "
        "encoding='utf-8')\n"
        "(artifact / 'review-evidence.json').write_text("
        f"{(json.dumps(review_evidence, sort_keys=True) + chr(10))!r}, "
        "encoding='utf-8')\n"
        "print('orchestrated review agent finished')\n",
    )


def write_worker_agent_without_report(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'worker_demo' / 'reports.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def count_lines(value: str) -> int:\\n'\n"
        "    '    return sum(1 for line in value.splitlines() if line.strip())\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| WRK-01 | P0 | Planned |', '| WRK-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'worker_report_emitted',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('worker report intentionally omitted')\n",
    )


def write_worker_agent_with_report(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'worker_demo' / 'reports.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def count_lines(value: str) -> int:\\n'\n"
        "    '    return sum(1 for line in value.splitlines() if line.strip())\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| WRK-01 | P0 | Planned |', '| WRK-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "runs = repo / '.vibe-loop' / 'runs.jsonl'\n"
        "runs.parent.mkdir(parents=True, exist_ok=True)\n"
        "runs.write_text(json.dumps({\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-wrk-01',\n"
        "    'task_id': 'WRK-01',\n"
        "    'status': 'completed',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'completed',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}) + '\\n', encoding='utf-8')\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'worker_report_emitted',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('worker report emitted')\n",
    )


def write_blocked_report_agent(
    path: Path,
    *,
    task_id: str,
    run_id: str,
    message: str,
    event: str,
    include_skill_event: bool = True,
) -> None:
    report = {
        "schema_version": 1,
        "record_type": "worker_report",
        "run_id": run_id,
        "task_id": task_id,
        "status": "blocked",
        "commit": "HEAD",
        "message": message,
        "metadata": {},
        "reported_at": "2026-05-09T00:00:00+00:00",
    }
    events = [event, "worker_report_emitted"]
    if include_skill_event:
        events.insert(0, "skill_activated")
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        f"report = {report!r}\n"
        "runs = repo / '.vibe-loop' / 'runs.jsonl'\n"
        "runs.write_text(json.dumps(report) + '\\n', encoding='utf-8')\n"
        f"events = {events!r}\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('blocked report emitted')\n",
    )


def write_workspace_agent_without_report(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "events = ['skill_activated', 'workspace_preflight_blocked']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('workspace preflight blocked but no report emitted')\n",
    )


def write_illegal_main_merge_agent(
    path: Path,
    *,
    stream_events: bool = False,
    hidden_branch: bool = False,
    remove_main: bool = False,
) -> None:
    event_write = (
        "print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "    {'type': 'tool_use', 'name': 'Skill', 'input': {'skill': 'vibe-loop'}},\n"
        "    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'vibe-loop report'}},\n"
        "]}}))\n"
        "print(json.dumps({'type': 'result', 'result': 'vibe-loop-eval-event: integration_lock_busy_observed'}))\n"
        if stream_events
        else "events = ['skill_activated', 'integration_lock_busy_observed', 'worker_report_emitted']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
    )
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        f"hidden_branch = {hidden_branch!r}\n"
        f"remove_main = {remove_main!r}\n"
        "report = {\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-busy-01',\n"
        "    'task_id': 'BUSY-01',\n"
        "    'status': 'blocked',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'blocked: main-integration lock unavailable',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}\n"
        "if hidden_branch:\n"
        "    subprocess.run(['git', 'checkout', '-b', 'hidden-worker'], cwd=repo, check=True)\n"
        "(repo / 'illegal-merge.txt').write_text('merged anyway\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'illegal-merge.txt'], cwd=repo, check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'illegal main change'], cwd=repo, check=True)\n"
        "if hidden_branch:\n"
        "    if remove_main:\n"
        "        subprocess.run(['git', 'branch', '-D', 'main'], cwd=repo, check=True)\n"
        "    else:\n"
        "        subprocess.run(['git', 'branch', '-f', 'main', 'HEAD'], cwd=repo, check=True)\n"
        "(repo / '.vibe-loop' / 'runs.jsonl').write_text(\n"
        "    json.dumps(report) + '\\n', encoding='utf-8'\n"
        ")\n"
        f"{event_write}"
        "print('blocked report emitted after illegal main merge')\n",
    )


def write_dirty_mutating_blocked_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "lock = json.loads(\n"
        "    (repo / '.vibe-loop' / 'locks' / 'DIRTY-01.lock' / 'lock.json')\n"
        "    .read_text(encoding='utf-8')\n"
        ")\n"
        "worktree = Path(lock['workspace']['worktree'])\n"
        "(worktree / 'docs' / 'foreign-notes.md').write_text(\n"
        "    'mutated foreign draft\\n', encoding='utf-8'\n"
        ")\n"
        "report = {\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-dirty-01',\n"
        "    'task_id': 'DIRTY-01',\n"
        "    'status': 'blocked',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'blocked: foreign_dirty_claimed_worktree',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}\n"
        "(repo / '.vibe-loop' / 'runs.jsonl').write_text(\n"
        "    json.dumps(report) + '\\n', encoding='utf-8'\n"
        ")\n"
        "events = ['skill_activated', 'workspace_preflight_blocked', 'worker_report_emitted']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('blocked report emitted after dirty workspace mutation')\n",
    )


def write_malformed_blocked_report_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "report = {\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'run_result',\n"
        "    'run_id': 'eval-run-dup-01',\n"
        "    'task_id': 'DUP-01',\n"
        "    'status': 'blocked',\n"
        "}\n"
        "(repo / '.vibe-loop' / 'runs.jsonl').write_text(\n"
        "    json.dumps(report) + '\\n', encoding='utf-8'\n"
        ")\n"
        "events = ['skill_activated', 'workspace_preflight_blocked', 'worker_report_emitted']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('malformed blocked report emitted')\n",
    )


def write_spoofed_report_evidence_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "report = {\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-dup-01',\n"
        "    'task_id': 'DUP-01',\n"
        "    'status': 'blocked',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'blocked',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}\n"
        "(repo / '.vibe-loop' / 'runs.jsonl').write_text(\n"
        "    json.dumps(report) + '\\n', encoding='utf-8'\n"
        ")\n"
        "fake = {'latest': {**report, 'message': 'blocked: duplicate_branch_worktrees'}}\n"
        "(artifact / 'report-evidence.json').write_text(\n"
        "    json.dumps(fake) + '\\n', encoding='utf-8'\n"
        ")\n"
        "events = ['skill_activated', 'workspace_preflight_blocked', 'worker_report_emitted']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('spoofed report evidence emitted')\n",
    )


def write_spoofed_lock_evidence_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "report = {\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-busy-01',\n"
        "    'task_id': 'BUSY-01',\n"
        "    'status': 'blocked',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'blocked: main-integration lock unavailable',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}\n"
        "(repo / '.vibe-loop' / 'runs.jsonl').write_text(\n"
        "    json.dumps(report) + '\\n', encoding='utf-8'\n"
        ")\n"
        "(artifact / 'lock-evidence.json').write_text(\n"
        "    json.dumps({'main_integration_status': {'locked': False, 'state': 'available'}}) + '\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "events = ['skill_activated', 'integration_lock_busy_observed', 'worker_report_emitted']\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('spoofed lock evidence emitted')\n",
    )


def write_main_integration_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'mil_demo' / 'progress.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def clamp_percent(value: int) -> int:\\n'\n"
        "    '    return max(0, min(100, value))\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| MIL-01 | P0 | Planned |', '| MIL-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'main_integration_lock_acquired',\n"
        "    'main_fast_forwarded',\n"
        "    'main_verification_ran',\n"
        "    'main_integration_lock_released',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "(artifact / 'lock-evidence.json').write_text(json.dumps({\n"
        "    'acquire': {\n"
        "        'owner_task_id': 'MIL-01',\n"
        "        'run_id': 'eval-run-mil-01',\n"
        "        'pid_source': 'active_task_lock:worker_pid',\n"
        "    },\n"
        "    'release': {'released': True},\n"
        "    'final_status': {'locked': False},\n"
        "}) + '\\n', encoding='utf-8')\n"
        "print('main integration lock evidence emitted')\n",
    )


if __name__ == "__main__":
    unittest.main()
