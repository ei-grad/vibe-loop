from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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
from vibe_loop.release_admission import (
    ReleaseAdmissionError,
    bundled_skill_fingerprints,
)


class EvalReleaseTests(unittest.TestCase):
    def test_default_release_matrix_includes_user_story_fixtures(self) -> None:
        matrix = release_gate_case_conditions()

        self.assertEqual(sum(len(conditions) for conditions in matrix.values()), 22)
        self.assertEqual(
            matrix["runtime-owned-implementation"],
            ("vibe_loop_cli",),
        )
        for case_id in (
            "explicit-list-profile",
            "kiro-user-story",
            "openspec-user-story",
            "spec-kit-user-story",
        ):
            self.assertEqual(matrix[case_id], ("vibe_loop",))
        self.assertEqual(matrix["command-hooks-task-source"], ("vibe_loop_cli",))

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

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["release_provenance"]["status"], "blocked")
        self.assertEqual(
            record["release_provenance"]["gaps"][0]["id"],
            "revision_binding_missing",
        )
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

    def test_exact_revision_record_requires_matching_trial_skill_fingerprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate_path = root / "suite" / "aggregate.json"
            relative_trial_root = Path(
                "cases/command-hooks-task-source/vibe_loop_cli/trial-1"
            )
            trial_root = aggregate_path.parent / relative_trial_root
            skill_sha = "a" * 64
            aggregate = passing_release_aggregate(trials=1)
            aggregate["records"] = [aggregate["records"][0]]
            aggregate["records"][0]["artifact_root"] = relative_trial_root.as_posix()
            aggregate["release_provenance"] = {
                "repository_head": "2" * 40,
                "bundled_skills": {
                    "src/vibe_loop/skills/vibe-loop/SKILL.md": skill_sha
                },
            }
            write_json(
                trial_root / "run.json",
                {
                    "source_fingerprints": [
                        {
                            "path": "skills/vibe-loop/SKILL.md",
                            "sha256": skill_sha,
                        }
                    ],
                    "skill_condition": {"skill_sha256": skill_sha},
                },
            )
            write_json(aggregate_path, aggregate)

            matching = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                required_case_conditions={
                    "command-hooks-task-source": ("vibe_loop_cli",)
                },
                revision_base="1" * 40,
                revision_head="2" * 40,
                bundled_skills={"src/vibe_loop/skills/vibe-loop/SKILL.md": skill_sha},
            )
            write_json(
                trial_root / "run.json",
                {
                    "source_fingerprints": [
                        {
                            "path": "skills/vibe-loop/SKILL.md",
                            "sha256": "b" * 64,
                        }
                    ],
                    "skill_condition": {"skill_sha256": "b" * 64},
                },
            )
            stale = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                required_case_conditions={
                    "command-hooks-task-source": ("vibe_loop_cli",)
                },
                revision_base="1" * 40,
                revision_head="2" * 40,
                bundled_skills={"src/vibe_loop/skills/vibe-loop/SKILL.md": skill_sha},
            )
            aggregate["records"][0]["artifact_root"] = "../outside-suite"
            write_json(
                root / "outside-suite" / "run.json",
                {
                    "source_fingerprints": [
                        {
                            "path": "skills/vibe-loop/SKILL.md",
                            "sha256": skill_sha,
                        }
                    ],
                    "skill_condition": {"skill_sha256": skill_sha},
                },
            )
            escaped = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                required_case_conditions={
                    "command-hooks-task-source": ("vibe_loop_cli",)
                },
                revision_base="1" * 40,
                revision_head="2" * 40,
                bundled_skills={"src/vibe_loop/skills/vibe-loop/SKILL.md": skill_sha},
            )

        self.assertEqual(matching["status"], "passed")
        self.assertEqual(stale["status"], "blocked")
        self.assertIn(
            "trial_skill_fingerprint_mismatch",
            {gap["id"] for gap in stale["release_provenance"]["gaps"]},
        )
        self.assertIn(
            "invalid_trial_artifact",
            {gap["id"] for gap in escaped["release_provenance"]["gaps"]},
        )

    def test_swe_rebench_evidence_is_attached_only_when_intentionally_supplied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate_path = root / "aggregate.json"
            external_path = root / "swe-rebench-v2-smoke.json"
            aggregate = passing_release_aggregate()
            write_json(aggregate_path, aggregate)
            write_json(
                external_path,
                {
                    "benchmark": "SWE-rebench V2 multilingual smoke",
                    "dataset": "nebius/SWE-rebench-V2",
                    "dataset_revision": "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0",
                    "split": "train",
                    "sample_size": 24,
                    "languages": ["go", "java", "js", "python", "rust", "ts"],
                    "non_leaderboard": True,
                    "caveats": ["directional smoke evidence only"],
                    "status": "completed",
                    "summary": {
                        "passed": 18,
                        "agent_failed": 4,
                        "infrastructure_failed": 2,
                    },
                },
            )

            omitted = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
            )
            attached = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                external_benchmarks=load_external_benchmark_evidence([external_path]),
            )

        self.assertEqual(
            omitted["external_benchmarks"]["status"], "optional_not_provided"
        )
        self.assertEqual(omitted["external_benchmarks"]["records"], [])
        evidence = attached["external_benchmarks"]["records"][0]
        self.assertEqual(evidence["summary"]["sample_size"], 24)
        self.assertTrue(evidence["summary"]["non_leaderboard"])
        self.assertEqual(
            evidence["summary"]["dataset_revision"],
            "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0",
        )
        self.assertEqual(evidence["summary"]["summary"]["agent_failed"], 4)
        self.assertEqual(evidence["summary"]["summary"]["infrastructure_failed"], 2)

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
        self.assertEqual(parked["status"], "blocked")
        self.assertIn(
            "revision_binding_missing",
            {blocker["id"] for blocker in parked["gate"]["blockers"]},
        )
        self.assertEqual(
            parked["workflow_contract_regressions"]["parked"][0]["parked_task_ids"],
            ["EVAL-99"],
        )

    def test_workflow_regression_records_redact_raw_hook_commands(self) -> None:
        sentinel = "RAW_HOOK_COMMAND_MUST_NOT_LEAK"
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "aggregate.json"
            aggregate = passing_release_aggregate(workflow_regression=True)
            comparison = aggregate["skill_quality"]["condition_comparisons"][
                "vibe_loop"
            ]
            full_record = {
                "run_id": "current-run",
                "case_id": "command-hooks-task-source",
                "condition": "vibe_loop",
                "trial": 1,
                "reproducibility": {"artifact_root": "cases/current"},
                "failure_taxonomy": ["workflow_contract"],
                "harness": {"command": sentinel},
                "task_source": {"list_command": sentinel},
                "locks": {"acquire_command": sentinel},
                "completion": {"commands": [sentinel]},
                "autopilot": {"planning_command": sentinel},
                "worklog": {"command": sentinel},
            }
            comparison["condition_records"] = [full_record]
            comparison["baseline_records"] = [
                {**full_record, "run_id": "baseline-run", "condition": "no_skill"}
            ]
            aggregate["skill_quality"]["prior_run_regressions"] = [
                {
                    "condition": "vibe_loop",
                    "regression_flags": ["workflow_contract_regression"],
                    "deltas": {
                        "workflow_score_mean": {"command": sentinel},
                        "workflow_violation_rate": 0.5,
                    },
                    "records": [full_record],
                    "previous_records": [{**full_record, "run_id": "previous-run"}],
                }
            ]
            write_json(aggregate_path, aggregate)

            record = build_release_readiness_record(
                aggregate,
                aggregate_path=aggregate_path,
                dry_run=True,
                generated_at="2026-05-09T00:00:00+00:00",
            )

        encoded = json.dumps(record)
        self.assertNotIn(sentinel, encoded)
        unresolved = record["workflow_contract_regressions"]["unresolved"]
        current = next(
            item
            for item in unresolved
            if item["id"] == "condition_comparison:vibe_loop"
        )
        prior = next(
            item for item in unresolved if item["source"] == "prior_run_regression"
        )
        self.assertEqual(current["records"][0]["run_id"], "current-run")
        self.assertEqual(current["records"][0]["artifact_root"], "cases/current")
        self.assertNotIn("workflow_score_mean", prior["deltas"])

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

        self.assertEqual(record["status"], "blocked")
        self.assertIn(
            "revision_binding_missing",
            {blocker["id"] for blocker in record["gate"]["blockers"]},
        )
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

    def test_post_eval_provenance_change_has_actionable_cli_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "config", "user.name", "Test"), cwd=root, check=True)
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                check=True,
            )
            skill_path = root / "src/vibe_loop/skills/vibe-loop/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("contract\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(("git", "commit", "-q", "-m", "base"), cwd=root, check=True)
            subprocess.run(("git", "tag", "v0.1.0"), cwd=root, check=True)
            (root / "README.md").write_text("release\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "commit", "-q", "-m", "release"), cwd=root, check=True
            )

            stdout = StringIO()
            stderr = StringIO()
            with (
                patch(
                    "vibe_loop.cli.run_local_demo_eval",
                    side_effect=ReleaseAdmissionError(
                        "release source revision changed during eval execution"
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "--repo",
                        str(root),
                        "eval",
                        "release-gate",
                        "--agent-command",
                        "*=stub {prompt}",
                        "--overwrite",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("trial artifacts remain available", stderr.getvalue())
        self.assertIn("restore the exact clean revision and rerun", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_dry_run_checks_existing_aggregate_and_writes_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate_path = root / "eval-runs" / "local-demo-v1" / "aggregate.json"
            external_path = root / "terminal-smoke.json"
            record_path = root / "release-readiness.json"
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "config", "user.name", "Test"), cwd=root, check=True)
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                check=True,
            )
            skill_path = root / "src/vibe_loop/skills/vibe-loop/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("contract\n", encoding="utf-8")
            (root / ".gitignore").write_text(
                "/eval-runs/\n/terminal-smoke.json\n/release-readiness.json\n",
                encoding="utf-8",
            )
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(("git", "commit", "-q", "-m", "base"), cwd=root, check=True)
            subprocess.run(("git", "tag", "v0.1.0"), cwd=root, check=True)
            (root / "README.md").write_text("release\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "commit", "-q", "-m", "release"), cwd=root, check=True
            )
            head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            aggregate = passing_release_aggregate()
            aggregate["release_provenance"] = {
                "repository_head": head,
                "bundled_skills": bundled_skill_fingerprints(root),
            }
            write_json(aggregate_path, aggregate)
            write_json(
                external_path,
                {
                    "benchmark": "terminal-bench-smoke",
                    "status": "skipped",
                    "summary": {"reason": "adapter not configured"},
                },
            )
            (root / ".tmp-dirty-probe").write_text("dirty\n", encoding="utf-8")

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

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(output["dry_run"])
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["local_suite"]["mode"], "existing_aggregate")
        self.assertEqual(written["record_type"], "skill_release_readiness")
        self.assertEqual(
            written["external_benchmarks"]["records"][0]["status"], "skipped"
        )


# Absolute developer-machine paths and downstream/company project names that
# must never ship inside the package, docs, eval fixtures, or tests. Design
# references such as "ralphex" and "lightmetrics" are intentionally NOT
# forbidden.
FORBIDDEN_REFERENCE_PATTERNS = (
    re.compile(r"/home/"),
    re.compile(r"/Users/"),
    re.compile(r"[A-Za-z]:\\Users\\"),
    re.compile(r"faceapp", re.IGNORECASE),
)
SCANNED_REFERENCE_ROOTS = ("src", "docs", "eval", "scripts", "tests")
SKIPPED_REFERENCE_DIRS = frozenset({"__pycache__", ".git", ".venv"})
# These files spell out the forbidden literals on purpose — to define the guard
# and to assert command output stays clean — so they are exempt from it.
REFERENCE_GUARD_EXEMPT_FILES = frozenset(
    {
        "tests/test_eval_release.py",
        "tests/test_eval_examples.py",
    }
)


def find_forbidden_repository_references(root: Path) -> list[str]:
    missing_roots = [
        directory
        for directory in SCANNED_REFERENCE_ROOTS
        if not (root / directory).is_dir()
    ]
    if missing_roots:
        missing = ", ".join(missing_roots)
        raise AssertionError(f"reference guard scan roots missing: {missing}")

    targets = [root / "README.md", root / "PROMPT.md"]
    for directory in SCANNED_REFERENCE_ROOTS:
        for path in (root / directory).rglob("*"):
            if not path.is_file():
                continue
            if SKIPPED_REFERENCE_DIRS.intersection(path.parts):
                continue
            targets.append(path)

    offenders: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in REFERENCE_GUARD_EXEMPT_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable files cannot carry a textual leak.
            continue
        for pattern in FORBIDDEN_REFERENCE_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{relative}: {pattern.pattern}")
    return offenders


class RepoAgnosticGuardTests(unittest.TestCase):
    def test_task_layer_contracts_remain_repository_agnostic(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "PROMPT.md").read_text(encoding="utf-8")
        task_discovery = (root / "docs" / "prd" / "task-discovery.md").read_text(
            encoding="utf-8"
        )
        normalized_prompt = " ".join(prompt.split()).casefold()
        normalized_task_discovery = " ".join(task_discovery.split()).casefold()

        self.assertNotIn("loopyard", normalized_prompt)
        self.assertIn(
            "external, adapter-bound surface rather than a repository-prescribed "
            "backend or file format",
            normalized_prompt,
        )
        self.assertNotIn("planning analytics", normalized_prompt)
        self.assertIn(
            "without requiring a repository to adopt a prescribed task filename "
            "or layout",
            normalized_task_discovery,
        )
        self.assertNotIn("this repository", normalized_task_discovery)
        self.assertNotIn("local `plan.md` shape", normalized_task_discovery)

    def test_prd_density_budget_matches_the_seed_policy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "PROMPT.md").read_text(encoding="utf-8")
        prd_index = (root / "docs" / "prd" / "README.md").read_text(encoding="utf-8")

        for document in (prompt, prd_index):
            normalized_document = " ".join(document.split())
            self.assertIn("above four times", normalized_document)
            self.assertIn("over budget", normalized_document)
        self.assertIn("An over-budget PRD has no growth allowance.", prd_index)

    def test_shipped_artifacts_have_no_downstream_references(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders = find_forbidden_repository_references(root)

        self.assertEqual(offenders, [], f"downstream references leaked: {offenders}")

    def test_shipped_script_references_are_reported_by_relative_path(self) -> None:
        forbidden_references = (
            "/home/developer/project",
            "/Users/developer/project",
            r"C:\Users\developer\project",
            "FaceApp-internal",
        )

        for pattern, forbidden_reference in zip(
            FORBIDDEN_REFERENCE_PATTERNS, forbidden_references, strict=True
        ):
            with self.subTest(pattern=pattern.pattern):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    for scan_root in SCANNED_REFERENCE_ROOTS:
                        (root / scan_root).mkdir()
                    script_path = root / "scripts" / "nested" / "release-guard-probe"
                    script_path.parent.mkdir()
                    script_path.write_text(forbidden_reference, encoding="utf-8")
                    (root / "scripts" / "__pycache__").mkdir()
                    (root / "scripts" / "__pycache__" / "generated").write_text(
                        forbidden_reference, encoding="utf-8"
                    )
                    (root / "scripts" / ".venv").mkdir()
                    (root / "scripts" / ".venv" / "generated").write_text(
                        forbidden_reference, encoding="utf-8"
                    )
                    (root / "scripts" / ".git").mkdir()
                    (root / "scripts" / ".git" / "metadata").write_text(
                        forbidden_reference, encoding="utf-8"
                    )
                    (root / "scripts" / "binary").write_bytes(
                        b"\xff" + forbidden_reference.encode()
                    )

                    offenders = find_forbidden_repository_references(root)

                self.assertEqual(
                    offenders,
                    [f"scripts/nested/release-guard-probe: {pattern.pattern}"],
                )

    def test_reference_guard_rejects_a_missing_scan_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scan_root in SCANNED_REFERENCE_ROOTS:
                if scan_root != "scripts":
                    (root / scan_root).mkdir()

            with self.assertRaisesRegex(
                AssertionError, "reference guard scan roots missing: scripts"
            ):
                find_forbidden_repository_references(root)


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
