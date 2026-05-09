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
                    ["condition_comparison:vibe_loop=EVAL-99"]
                ),
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["workflow_contract_regressions"]["unresolved"][0]["id"],
            "condition_comparison:vibe_loop",
        )
        self.assertEqual(parked["status"], "passed")
        self.assertEqual(
            parked["workflow_contract_regressions"]["parked"][0]["parked_task_ids"],
            ["EVAL-99"],
        )

    def test_coverage_gaps_block_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate(trials=1)
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["gate"]["blockers"][0]["id"], "local_demo_coverage")
        self.assertEqual(record["local_suite"]["coverage_gaps"][0]["trials"], 1)

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

    def test_case_coverage_requires_condition_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate()
            del aggregate["conditions"]["vibe_loop"]
            aggregate["skill_quality"]["condition_comparisons"] = {}
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(
            record["workflow_contract_regressions"]["evidence_gaps"][0],
            {
                "id": "missing_condition_comparison",
                "condition": "vibe_loop",
                "message": "skill_quality is missing comparison for vibe_loop",
            },
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
            record = build_release_readiness_record(
                passing_release_aggregate(trials=1, workflow_regression=True),
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
        self.assertIn('"long_text": {"length": 300, "omitted": "long_string"}', rendered)

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
        for condition in ("no_skill", "vibe_loop")
    }
    regression_flags = ["workflow_contract_regression"] if workflow_regression else []
    workflow_delta = -1.0 if workflow_regression else 0.0
    workflow_violation_delta = 1.0 if workflow_regression else 0.0
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
            "condition_comparisons": {
                "vibe_loop": {
                    "regression_flags": regression_flags,
                    "deltas": {
                        "workflow_score_mean": workflow_delta,
                        "workflow_violation_rate": workflow_violation_delta,
                    },
                    "baseline_records": [],
                    "condition_records": [],
                }
            },
            "prior_run_regressions": [],
            "failure_categories": {
                "workflow_contract_failures": {"count": 0, "records": []}
            },
        },
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
