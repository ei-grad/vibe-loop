from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.evals import (
    EvalArtifactRef,
    EvalSourceFingerprint,
    SkillEvalRunRecord,
    validate_skill_eval_run_record,
)


class SkillEvalSchemaTests(unittest.TestCase):
    def test_valid_run_record_passes_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            record = valid_record(artifacts=artifacts).to_json()

            diagnostics = validate_skill_eval_run_record(
                record,
                artifact_root,
                current_source_fingerprints={
                    "PLAN.md": record["source_fingerprints"][0]
                },
            )

        self.assertEqual(diagnostics, ())

    def test_stale_source_fingerprint_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            record = valid_record(artifacts=artifacts).to_json()

            diagnostics = validate_skill_eval_run_record(
                record,
                artifact_root,
                current_source_fingerprints={"PLAN.md": "f" * 64},
            )

        self.assertIn("source fingerprint stale: PLAN.md", diagnostics)

    def test_missing_required_artifacts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            missing = artifact_root / "logs" / "run.log"
            missing.unlink()
            record = valid_record(artifacts=artifacts).to_json()

            diagnostics = validate_skill_eval_run_record(record, artifact_root)

        self.assertIn("required artifact missing: logs/run.log", diagnostics)

    def test_required_artifact_roles_cannot_be_marked_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            optional_artifacts = [
                EvalArtifactRef(
                    role=artifact.role,
                    path=f"missing/{artifact.path}",
                    sha256=artifact.sha256,
                    required=False,
                )
                for artifact in artifacts
            ]
            record = valid_record(artifacts=optional_artifacts).to_json()

            diagnostics = validate_skill_eval_run_record(record, artifact_root)

        self.assertIn("required artifact role marked optional: run_log", diagnostics)
        self.assertIn("required artifact role missing: run_log", diagnostics)

    def test_nested_contract_rejects_invalid_scores_and_contaminated_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            record = valid_record(artifacts=artifacts).to_json()
            record["scoring"] = {
                "passed": "yes",
                "task_score": 1.2,
                "workflow_score": -0.1,
                "trigger_score": 2.0,
                "excluded_from_primary": "no",
            }
            record["budget"] = {
                "timeout_seconds": 0,
                "max_commands": True,
                "max_output_bytes": -1,
            }
            record["reproducibility"] = {
                "fixture_sha256": "not-a-sha",
                "run_order": 0,
                "fresh_workspace": False,
                "state_reused": True,
            }

            diagnostics = validate_skill_eval_run_record(record, artifact_root)

        self.assertIn("scoring.passed must be a boolean", diagnostics)
        self.assertIn("scoring.task_score must be between 0.0 and 1.0", diagnostics)
        self.assertIn("scoring.workflow_score must be between 0.0 and 1.0", diagnostics)
        self.assertIn("scoring.trigger_score must be between 0.0 and 1.0", diagnostics)
        self.assertIn("scoring.excluded_from_primary must be a boolean", diagnostics)
        self.assertIn("budget.timeout_seconds must be a positive integer", diagnostics)
        self.assertIn("budget.max_commands must be a positive integer", diagnostics)
        self.assertIn("budget.max_output_bytes must be a positive integer", diagnostics)
        self.assertIn(
            "reproducibility.fixture_sha256 must be a SHA-256 digest",
            diagnostics,
        )
        self.assertIn(
            "reproducibility.run_order must be a positive integer", diagnostics
        )
        self.assertIn("reproducibility.fresh_workspace must be true", diagnostics)
        self.assertIn("reproducibility.state_reused must be false", diagnostics)

    def test_condition_must_match_skill_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            no_skill_record = valid_record(artifacts=artifacts).to_json()
            no_skill_record["condition"] = "no_skill"
            with_skill_unavailable = valid_record(artifacts=artifacts).to_json()
            with_skill_unavailable["skill_condition"] = {
                "id": "vibe_loop",
                "skills_available": False,
            }

            no_skill_diagnostics = validate_skill_eval_run_record(
                no_skill_record,
                artifact_root,
            )
            with_skill_diagnostics = validate_skill_eval_run_record(
                with_skill_unavailable,
                artifact_root,
            )

        self.assertIn(
            "no_skill condition must have skills_available=false",
            no_skill_diagnostics,
        )
        self.assertIn(
            "no_skill condition must not expose skill_id",
            no_skill_diagnostics,
        )
        self.assertIn(
            "vibe_loop condition must have skills_available=true",
            with_skill_diagnostics,
        )
        self.assertIn(
            "vibe_loop condition must expose skill_id=vibe-loop",
            with_skill_diagnostics,
        )

    def test_symlinked_artifacts_are_rejected_before_target_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            secret_dir = artifact_root / "secrets"
            secret_dir.mkdir()
            secret_target = secret_dir / "run.log"
            secret_target.write_text("TOKEN=secret\n", encoding="utf-8")
            symlink_path = artifact_root / "logs" / "run.log"
            symlink_path.unlink()
            symlink_path.symlink_to(secret_target)
            record = valid_record(artifacts=artifacts).to_json()
            original_open = Path.open

            def open_without_secret(path: Path, *args: object, **kwargs: object):
                if path.resolve() == secret_target:
                    raise AssertionError("symlinked secret artifact target was read")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", open_without_secret):
                diagnostics = validate_skill_eval_run_record(record, artifact_root)

        self.assertIn("artifact path must not be a symlink: logs/run.log", diagnostics)

    def test_secret_like_evidence_paths_are_rejected_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifacts = write_required_artifacts(artifact_root)
            artifacts.append(
                EvalArtifactRef(
                    role="raw_evidence",
                    path="logs/token.txt",
                    sha256="0" * 64,
                )
            )
            record = valid_record(
                artifacts=artifacts,
                source_fingerprints=(
                    EvalSourceFingerprint(
                        path=".env",
                        sha256="1" * 64,
                        size=12,
                    ),
                ),
            ).to_json()
            (artifact_root / "logs" / "token.txt").write_text(
                "TOKEN=secret\n",
                encoding="utf-8",
            )
            original_open = Path.open

            def open_without_secret(path: Path, *args: object, **kwargs: object):
                if path.name == "token.txt":
                    raise AssertionError("secret-like artifact path was read")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", open_without_secret):
                diagnostics = validate_skill_eval_run_record(record, artifact_root)

        self.assertIn("source fingerprint path is secret-like: <redacted>", diagnostics)
        self.assertIn("artifact path is secret-like: logs/<redacted>", diagnostics)


def valid_record(
    *,
    artifacts: list[EvalArtifactRef],
    source_fingerprints: tuple[EvalSourceFingerprint, ...] | None = None,
) -> SkillEvalRunRecord:
    return SkillEvalRunRecord(
        suite_id="local-demo",
        case_id="finite-slice",
        trial=1,
        condition="vibe_loop",
        run_id="local-demo-finite-slice-vibe-loop-1",
        task={
            "id": "DEMO-01",
            "prompt_sha256": "2" * 64,
            "expected_skill": "vibe-loop",
            "should_trigger": True,
        },
        skill_condition={
            "id": "vibe_loop",
            "skills_available": True,
            "skill_id": "vibe-loop",
            "skill_sha256": "3" * 64,
        },
        agent={
            "name": "codex",
            "command_source": "explicit",
        },
        model={
            "provider": "openai",
            "id": "gpt-5.5",
            "reasoning_effort": "xhigh",
        },
        harness={
            "name": "vibe-loop-eval",
            "version": "0.1",
            "command": "codex exec '$vibe-loop DEMO-01'",
        },
        budget={
            "timeout_seconds": 900,
            "max_commands": 200,
            "max_output_bytes": 2_000_000,
        },
        source_fingerprints=source_fingerprints
        or (
            EvalSourceFingerprint(
                path="PLAN.md",
                sha256="4" * 64,
                size=1024,
                mtime_ns=1,
            ),
        ),
        artifacts=artifacts,
        final_repo_state={
            "head": "abc123",
            "branch": "main",
            "dirty": False,
        },
        structured_result={
            "exit_code": 0,
            "timeout": False,
            "task_status": "completed",
            "task_completed": True,
            "workflow_contract_completed": True,
        },
        graders=(
            {
                "id": "repo-tests",
                "type": "deterministic",
                "passed": True,
            },
        ),
        scoring={
            "passed": True,
            "task_score": 1.0,
            "workflow_score": 1.0,
            "trigger_score": 1.0,
            "excluded_from_primary": False,
        },
        reproducibility={
            "fixture_sha256": "5" * 64,
            "run_order": 1,
            "fresh_workspace": True,
            "state_reused": False,
        },
        status="passed",
        started_at="2026-05-09T00:00:00+00:00",
        finished_at="2026-05-09T00:10:00+00:00",
    )


def write_required_artifacts(root: Path) -> list[EvalArtifactRef]:
    files = {
        "prompt": ("prompt.txt", "Run EVAL-01\n"),
        "run_log": ("logs/run.log", "log\n"),
        "transcript": ("transcript.jsonl", "{}\n"),
        "diff": ("diff.patch", "diff --git a/file b/file\n"),
        "final_repo_state": ("final-repo-state.json", "{}\n"),
        "structured_result": ("run-result.json", "{}\n"),
        "grader_outputs": ("grader-outputs.json", "[]\n"),
    }
    artifacts: list[EvalArtifactRef] = []
    for role, (relative_path, content) in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        artifacts.append(
            EvalArtifactRef(
                role=role,
                path=relative_path,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return artifacts
