from __future__ import annotations

import json
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.eval_benchmark_swe_rebench import (
    AGENT_FAILURE_EXIT_CODE,
    AgentPatchError,
    InfrastructureError,
    canonical_record_sha256,
    classify_report,
    materialize_harness_snapshot,
    validate_harness_checkout,
    validate_patch_file,
    validate_task_export,
)


class SweRebenchV2GraderTests(unittest.TestCase):
    def test_patch_file_requires_one_matching_nonempty_patch(self) -> None:
        invalid_payloads = (
            [],
            [{"instance_id": "wrong", "patch": "diff --git a/a b/a"}],
            [
                {"instance_id": "task-1", "patch": "first"},
                {"instance_id": "task-1", "patch": "second"},
            ],
            [{"instance_id": "task-1"}],
            [{"instance_id": "task-1", "patch": "  "}],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.json"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(AgentPatchError):
                        validate_patch_file(path, "task-1")

            path.write_text(
                json.dumps([{"instance_id": "task-1", "patch": "diff --git a/a b/a"}]),
                encoding="utf-8",
            )
            validate_patch_file(path, "task-1")

    def test_missing_patch_file_is_an_agent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AgentPatchError):
                validate_patch_file(Path(directory) / "patches.json", "task-1")

    def test_task_export_requires_exact_pinned_records(self) -> None:
        task = {
            "instance_id": "task-1",
            "repo": "example/project",
            "patch": "gold",
        }
        fingerprints = {"task-1": canonical_record_sha256(task)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(json.dumps([task]), encoding="utf-8")
            selected = validate_task_export(
                path,
                {"task-1"},
                fingerprints,
                "task-1",
            )
            self.assertEqual(selected, task)

            path.write_text(
                json.dumps([{**task, "patch": "different gold"}]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InfrastructureError, "fingerprint mismatch"):
                validate_task_export(path, {"task-1"}, fingerprints, "task-1")

    def test_task_export_rejects_missing_and_unexpected_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(
                json.dumps([{"instance_id": "unexpected"}]), encoding="utf-8"
            )
            with self.assertRaisesRegex(InfrastructureError, "instance set mismatch"):
                validate_task_export(
                    path,
                    {"task-1"},
                    {"task-1": "unused"},
                    "task-1",
                )

    def test_harness_checkout_requires_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory)
            evaluator = harness / "scripts" / "eval.py"
            evaluator.parent.mkdir()
            evaluator.write_text("pass\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="wrong-revision\n", stderr=""
            )
            with (
                patch("subprocess.run", return_value=completed),
                self.assertRaisesRegex(InfrastructureError, "revision mismatch"),
            ):
                validate_harness_checkout(harness, "pinned-revision")

    def test_harness_execution_uses_committed_archive_not_worktree_files(self) -> None:
        archive_buffer = io.BytesIO()
        committed_content = b"print('committed evaluator')\n"
        with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
            member = tarfile.TarInfo("scripts/eval.py")
            member.size = len(committed_content)
            archive.addfile(member, io.BytesIO(committed_content))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=archive_buffer.getvalue(), stderr=b""
        )
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "dirty-harness"
            evaluator = harness / "scripts" / "eval.py"
            evaluator.parent.mkdir(parents=True)
            evaluator.write_text("print('dirty evaluator')\n", encoding="utf-8")
            destination = Path(directory) / "snapshot"
            with patch("subprocess.run", return_value=completed) as run:
                materialize_harness_snapshot(harness, "pinned-revision", destination)

            self.assertEqual(
                (destination / "scripts" / "eval.py").read_bytes(), committed_content
            )
            self.assertEqual(
                run.call_args.args[0],
                [
                    "git",
                    "-C",
                    str(harness),
                    "archive",
                    "--format=tar",
                    "pinned-revision",
                ],
            )

    def test_report_distinguishes_mismatch_from_infrastructure_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "instance_id": "task-1",
                                "passed_match": False,
                                "error": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                classify_report(report, "task-1", 1), AGENT_FAILURE_EXIT_CODE
            )

            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "instance_id": "task-1",
                                "error": "docker daemon unavailable",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InfrastructureError, "docker daemon"):
                classify_report(report, "task-1", 1)

            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "instance_id": "task-1",
                                "passed_match": False,
                                "exit_code": 125,
                                "error": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InfrastructureError, "Docker failed"):
                classify_report(report, "task-1", 1)

    def test_report_requires_consistent_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "instance_id": "task-1",
                                "passed_match": True,
                                "error": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(classify_report(report, "task-1", 0), 0)
            with self.assertRaisesRegex(InfrastructureError, "inconsistent"):
                classify_report(report, "task-1", 1)


if __name__ == "__main__":
    unittest.main()
