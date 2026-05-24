from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main
from vibe_loop.eval_examples import list_eval_example_cases
from vibe_loop.eval_release import (
    build_release_readiness_record,
    load_external_benchmark_evidence,
    load_json_mapping,
    parse_parked_regression_specs,
    render_release_readiness_summary,
    release_gate_case_conditions,
)


class EvalReleaseTests(unittest.TestCase):
    def test_release_record_passes_with_full_suite_and_optional_external_smoke(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate_path = root / "aggregate.json"
            external_path = root / "swe-smoke.json"
            write_json(aggregate_path, passing_release_aggregate())
            write_json(
                external_path,
                {
                    "benchmark": "swe-bench-pro-public-smoke",
                    "status": "passed",
                    "sample_size": 10,
                    "summary": {"resolved": 7},
                },
            )

            record = build_release_readiness_record(
                load_json_mapping(aggregate_path),
                aggregate_path=aggregate_path,
                dry_run=True,
                external_benchmarks=load_external_benchmark_evidence([external_path]),
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "passed")
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["local_suite"]["coverage_status"], "passed")
        self.assertEqual(record["workflow_contract_regressions"]["unresolved"], [])
        self.assertEqual(record["trial_failures"]["status"], "passed")
        self.assertEqual(record["external_benchmarks"]["status"], "recorded")
        self.assertEqual(
            record["checklist"][0],
            {
                "id": "run_local_demo_suite",
                "required": True,
                "status": "passed",
                "evidence": str(aggregate_path),
            },
        )

    def test_workflow_regression_blocks_until_parked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate(workflow_regression=True)
            write_json(aggregate_path, aggregate)

            blocked = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )
            parked = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                parked_regressions=parse_parked_regression_specs(
                    [
                        "condition_comparison:vibe_loop=EVAL-99",
                        "condition_comparison:vibe_loop_cli=EVAL-99",
                        "condition_comparison:orchestrated_vibe_loop=EVAL-99",
                    ]
                ),
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["workflow_contract_regressions"]["unresolved"][0]["id"],
            "condition_comparison:orchestrated_vibe_loop",
        )
        self.assertEqual(parked["status"], "passed")
        self.assertEqual(
            parked["workflow_contract_regressions"]["parked"][0]["parked_task_ids"],
            ["EVAL-99"],
        )

    def test_coverage_gaps_block_release_gate_for_required_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["cases"]["finite-py-plan-table"]["vibe_loop"]["trials"] = 0
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["gate"]["blockers"][0]["id"], "local_demo_coverage")
        self.assertEqual(record["local_suite"]["coverage_gaps"][0]["trials"], 0)

    def test_missing_skill_quality_blocks_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            del aggregate["skill_quality"]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertIn(
            "skill_quality_evidence",
            [blocker["id"] for blocker in record["gate"]["blockers"]],
        )
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"][0]["id"],
            "missing_skill_quality",
        )

    def test_release_gate_does_not_require_no_skill_condition_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["skill_quality"]["condition_comparisons"] = {}
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "passed")
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"],
            [],
        )

    def test_missing_required_condition_summary_blocks_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            del aggregate["skill_quality"]["conditions"]["vibe_loop"]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"][0]["id"],
            "missing_condition_summary",
        )

    def test_regression_flags_must_be_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["skill_quality"]["condition_comparisons"]["vibe_loop"][
                "regression_flags"
            ] = [123]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"][0]["id"],
            "invalid_regression_flags",
        )

    def test_prior_run_regression_flags_must_be_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["skill_quality"]["prior_run_regressions"] = [
                {
                    "condition": "vibe_loop",
                    "regression_flags": [123],
                    "deltas": {
                        "workflow_score_mean": 0.0,
                        "workflow_violation_rate": 0.0,
                    },
                    "records": [],
                    "previous_records": [],
                }
            ]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"][0]["id"],
            "invalid_prior_run_regression_flags",
        )

    def test_blocked_summary_includes_actionable_regression_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate(workflow_regression=True)
            aggregate["cases"]["finite-py-plan-table"]["vibe_loop"]["trials"] = 0
            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

            summary = render_release_readiness_summary(record)

        self.assertIn("blockers:", summary)
        self.assertIn("local_demo_coverage", summary)
        self.assertIn("coverage gaps:", summary)
        self.assertIn("unresolved workflow regressions:", summary)
        self.assertIn(
            "--parked-regression condition_comparison:vibe_loop=TASK-ID",
            summary,
        )

    def test_current_required_trial_failure_blocks_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["records"][0]["status"] = "failed"
            aggregate["records"][0]["failure_taxonomy"] = ["workflow_contract"]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["trial_failures"]["total"], 1)
        self.assertIn(
            "release_trial_failures",
            [blocker["id"] for blocker in record["gate"]["blockers"]],
        )

    def test_failed_required_case_summary_blocks_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            aggregate["records"] = []
            summary = aggregate["cases"]["finite-py-plan-table"]["vibe_loop"]
            summary["pass_count"] = 0
            summary["pass_rate"] = 0.0
            summary["failure_taxonomy"] = {"workflow_contract": 1}
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["trial_failures"]["total"], 1)
        self.assertEqual(
            record["trial_failures"]["records"][0]["failure_taxonomy"],
            ["workflow_contract"],
        )

    def test_external_benchmark_summary_omits_sensitive_nested_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external.json"
            write_json(
                path,
                {
                    "benchmark": "sample-smoke",
                    "status": "recorded",
                    "summary": {
                        "resolved": 3,
                        "stdout": "SECRET VALUE",
                        "details": {"nested": "not copied"},
                        "long_text": "x" * 300,
                    },
                },
            )

            evidence = load_external_benchmark_evidence([path])[0]

        rendered = json.dumps(evidence, sort_keys=True)
        self.assertIn('"resolved": 3', rendered)
        self.assertNotIn("SECRET VALUE", rendered)
        self.assertIn('"stdout": {"omitted": "sensitive_key"}', rendered)
        self.assertIn('"details": {"omitted": "nested_mapping"}', rendered)
        self.assertIn(
            '"long_text": {"length": 300, "omitted": "long_string"}', rendered
        )

    def test_cli_dry_run_checks_existing_aggregate_and_writes_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate_path = root / "eval-runs" / "local-demo-v1" / "aggregate.json"
            external_path = root / "terminal-smoke.json"
            record_path = root / "release-readiness.json"
            write_json(aggregate_path, passing_release_aggregate())
            write_json(
                external_path,
                {
                    "benchmark": "terminal-bench-smoke",
                    "status": "skipped",
                    "summary": {"reason": "adapter not configured"},
                },
            )

            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--repo",
                        str(root),
                        "eval",
                        "release-gate",
                        "--aggregate",
                        str(aggregate_path),
                        "--external-benchmark-json",
                        str(external_path),
                        "--record-output",
                        str(record_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            output = json.loads(stdout.getvalue())
            written = json.loads(record_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(output["dry_run"])
        self.assertEqual(output["local_suite"]["mode"], "existing_aggregate")
        self.assertEqual(written["record_type"], "skill_release_readiness")
        self.assertEqual(
            written["external_benchmarks"]["records"][0]["status"], "skipped"
        )


def passing_release_aggregate(
    *,
    trials: int = 3,
    workflow_regression: bool = False,
) -> dict[str, object]:
    required = release_gate_case_conditions()
    cases = {
        case.case_id: {
            condition: {
                "trials": trials,
                "pass_count": trials,
                "pass_rate": 1.0,
                "failure_taxonomy": {},
            }
            for condition in case.conditions
        }
        for case in list_eval_example_cases()
    }
    total_by_condition = {
        condition: sum(
            payload[condition]["trials"]
            for payload in cases.values()
            if condition in payload
        )
        for condition in (
            "no_skill",
            "vibe_loop",
            "vibe_loop_cli",
            "orchestrated_vibe_loop",
        )
    }
    regression_flags = ["workflow_contract_regression"] if workflow_regression else []
    workflow_delta = -1.0 if workflow_regression else 0.0
    workflow_violation_delta = 1.0 if workflow_regression else 0.0
    records = [
        {
            "case_id": case_id,
            "condition": condition,
            "trial": trial,
            "run_id": f"{case_id}-{condition}-{trial}",
            "status": "passed",
            "artifact_root": f"cases/{case_id}/{condition}/trial-{trial}",
            "failure_taxonomy": [],
        }
        for case_id, conditions in required.items()
        for condition in conditions
        for trial in range(1, trials + 1)
    ]
    quality_conditions = {
        condition: {
            "trials": count,
            "primary_trials": count,
            "pass_count": count,
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
                    "run_id": record["run_id"],
                    "case_id": record["case_id"],
                    "condition": record["condition"],
                    "trial": record["trial"],
                    "status": record["status"],
                    "artifact_root": record["artifact_root"],
                }
                for record in records
                if record["condition"] == condition
            ],
        }
        for condition, count in total_by_condition.items()
    }
    return {
        "schema_version": 1,
        "suite_id": "local-demo-v1",
        "generated_at": "2026-05-09T00:00:00+00:00",
        "artifact_root": "/tmp/eval-runs/local-demo-v1",
        "total_trials": sum(total_by_condition.values()),
        "conditions": {
            condition: {
                "trials": count,
                "primary_trials": count,
                "pass_count": count,
                "pass_rate": 1.0,
                "failure_taxonomy": {},
            }
            for condition, count in total_by_condition.items()
        },
        "cases": cases,
        "skill_quality": {
            "baseline_condition": "no_skill",
            "conditions": quality_conditions,
            "condition_comparisons": {
                "vibe_loop": {
                    "regression_flags": regression_flags,
                    "deltas": {
                        "workflow_score_mean": workflow_delta,
                        "workflow_violation_rate": workflow_violation_delta,
                    },
                    "baseline_records": [],
                    "condition_records": [],
                },
                "vibe_loop_cli": {
                    "regression_flags": regression_flags,
                    "deltas": {
                        "workflow_score_mean": workflow_delta,
                        "workflow_violation_rate": workflow_violation_delta,
                    },
                    "baseline_records": [],
                    "condition_records": [],
                },
                "orchestrated_vibe_loop": {
                    "regression_flags": regression_flags,
                    "deltas": {
                        "workflow_score_mean": workflow_delta,
                        "workflow_violation_rate": workflow_violation_delta,
                    },
                    "baseline_records": [],
                    "condition_records": [],
                },
            },
            "prior_run_regressions": [],
            "failure_categories": {
                "workflow_contract_failures": {"count": 0, "records": []}
            },
        },
        "records": records,
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
