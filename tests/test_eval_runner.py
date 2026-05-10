from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main
from vibe_loop.eval_runner import (
    TrialResult,
    build_aggregate,
    render_aggregate_markdown,
    workflow_taxonomy_labels,
)
from vibe_loop.evals import EVAL_FAILURE_TAXONOMY


class EvalRunnerCliTests(unittest.TestCase):
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
            run_log_exists = (trial_root / "logs" / "run.log").is_file()
            diff_exists = (trial_root / "diff.patch").is_file()

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["scoring"]["workflow_score"], 1.0)
        self.assertTrue(run_log_exists)
        self.assertTrue(diff_exists)

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
        self.assertIn("task_outcome", record["failure_taxonomy"])
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
