from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from vibe_loop.eval_release import release_gate_case_conditions
from vibe_loop.release_admission import (
    ReleaseAdmissionError,
    build_release_admission,
    bundled_skill_fingerprints,
    classify_release_changes,
    eval_release_provenance,
    parse_name_status,
    verify_release_admission,
)


class ReleaseAdmissionTests(unittest.TestCase):
    def test_real_workflow_gates_both_publish_targets_and_revalidates_transfer(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        admission_start = workflow.index("  admission:")
        testpypi_start = workflow.index("  publish-testpypi:")
        pypi_start = workflow.index("  publish-pypi:")
        admission = workflow[admission_start:testpypi_start]
        testpypi = workflow[testpypi_start:pypi_start]
        pypi = workflow[pypi_start:]

        self.assertIn("github.event_name == 'workflow_dispatch'", admission)
        self.assertNotIn("github.event_name == 'workflow_run'", admission)
        for publish_job in (testpypi, pypi):
            self.assertIn("- admission", publish_job)
            self.assertIn("- test", publish_job)
            self.assertLess(
                publish_job.index("Verify transferred evidence and distributions"),
                publish_job.index("Publish distributions"),
            )
            self.assertIn("--verify", publish_job)
        evidence_workflow = (
            root / ".github/workflows/skill-readiness-evidence.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("skill-release-readiness-${{ github.sha }}", evidence_workflow)
        self.assertIn('record.get("status") != "passed"', evidence_workflow)

    def test_classifier_covers_owned_changes_rename_deletion_and_mixed_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "old\n")
            write(repo / "src/vibe_loop/activity.py", "old\n")
            write(repo / "eval/fixture.json", "{}\n")
            base = commit_all(repo, "base")

            (repo / "src/vibe_loop/skills/vibe-loop/SKILL.md").rename(
                repo / "src/vibe_loop/skills/vibe-loop/CONTRACT.md"
            )
            (repo / "eval/fixture.json").unlink()
            write(repo / "src/vibe_loop/activity.py", "new\n")
            commit_all(repo, "change")

            record = classify_release_changes(repo, base=base)

        self.assertEqual(record["status"], "readiness_required")
        self.assertIn("eval/fixture.json", record["owned_paths"])
        self.assertIn("src/vibe_loop/skills/vibe-loop/SKILL.md", record["owned_paths"])
        self.assertNotIn("src/vibe_loop/activity.py", record["owned_paths"])
        self.assertEqual(
            {change["status"][0] for change in record["changed_paths"]},
            {"D", "M", "R"},
        )

    def test_eval_provenance_rejects_untracked_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "contract\n")
            commit_all(repo, "base")
            write(repo / "src/vibe_loop/skills/new-skill/SKILL.md", "untracked\n")

            with self.assertRaisesRegex(ReleaseAdmissionError, "clean worktree"):
                eval_release_provenance(repo)

    def test_unrelated_source_change_gets_exact_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/activity.py", "old\n")
            base = commit_all(repo, "base")
            write(repo / "src/vibe_loop/activity.py", "new\n")
            head = commit_all(repo, "change")

            record = classify_release_changes(repo, base=base)

        self.assertEqual(record["status"], "unrelated_exemption")
        self.assertEqual(record["base"], base)
        self.assertEqual(record["head"], head)
        self.assertEqual(record["owned_paths"], [])

    def test_malformed_abbreviated_and_nonancestor_bases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "file", "one\n")
            base = commit_all(repo, "base")
            write(repo / "file", "two\n")
            head = commit_all(repo, "head")
            with self.assertRaisesRegex(ReleaseAdmissionError, "full Git commit"):
                classify_release_changes(repo, base=base[:12])

            main_branch = run(repo, "branch", "--show-current")
            run(repo, "switch", "-q", "-c", "other", base)
            write(repo / "other", "value\n")
            other = commit_all(repo, "other")
            run(repo, "switch", "-q", main_branch)
            with self.assertRaisesRegex(ReleaseAdmissionError, "not an ancestor"):
                classify_release_changes(repo, head=head, base=other)

    def test_missing_release_tag_base_stops_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/activity.py", "value\n")
            commit_all(repo, "head")

            with self.assertRaisesRegex(ReleaseAdmissionError, "no prior reachable"):
                classify_release_changes(repo)

    def test_unknown_or_incomplete_diff_status_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ReleaseAdmissionError, "unknown"):
            parse_name_status(b"Q\0path\0")
        with self.assertRaisesRegex(ReleaseAdmissionError, "incomplete"):
            parse_name_status(b"R100\0old\0")

    def test_shallow_history_never_produces_unrelated_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = initialize_repo(root / "source")
            write(source / "src/vibe_loop/activity.py", "one\n")
            base = commit_all(source, "base")
            write(source / "src/vibe_loop/activity.py", "two\n")
            commit_all(source, "head")
            clone = root / "clone"
            subprocess.run(
                ("git", "clone", "-q", "--depth", "2", source.as_uri(), str(clone)),
                check=True,
            )

            record = classify_release_changes(clone, base=base)

        self.assertEqual(record["status"], "readiness_required")
        self.assertEqual(record["uncertainty"], ["shallow_repository"])

    def test_readiness_admission_binds_revisions_skills_and_distributions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "src/vibe_loop/skills/vibe-loop/SKILL.md", "contract\n")
            fingerprints = bundled_skill_fingerprints(root)
            wheel = root / "dist/vibe_loop-1-py3-none-any.whl"
            build_wheel(wheel, fingerprints, root)
            base = "1" * 40
            head = "2" * 40
            classification = classification_record(base, head, required=True)
            readiness = readiness_record(base, head, fingerprints)

            admission = build_release_admission(
                classification,
                readiness_record=readiness,
                distributions=(wheel,),
            )

            self.assertEqual(admission["status"], "passed")
            self.assertEqual(
                verify_release_admission(
                    admission,
                    classification=classification,
                    readiness_record=readiness,
                    distributions=(wheel,),
                ),
                (),
            )

            build_wheel(wheel, fingerprints, root, content_override=b"changed\n")
            diagnostics = verify_release_admission(
                admission,
                classification=classification,
                readiness_record=readiness,
                distributions=(wheel,),
            )

        self.assertTrue(
            any(
                "bundled skills do not match" in diagnostic
                for diagnostic in diagnostics
            )
        )
        self.assertTrue(
            any("transferred artifacts" in diagnostic for diagnostic in diagnostics)
        )

    def test_wrong_revision_missing_fingerprints_and_blocked_status_block(self) -> None:
        classification = classification_record("1" * 40, "2" * 40, required=True)
        record = readiness_record("1" * 40, "3" * 40, {})
        record["status"] = "blocked"

        admission = build_release_admission(
            classification,
            readiness_record=record,
            distributions=(),
        )

        self.assertEqual(admission["status"], "blocked")
        self.assertIn("readiness status is not passed", admission["diagnostics"])
        self.assertIn(
            "readiness head revision does not match classification",
            admission["diagnostics"],
        )
        self.assertIn(
            "bundled skill fingerprints are missing", admission["diagnostics"]
        )

    def test_incomplete_matrix_and_invalid_parked_evidence_block(self) -> None:
        base = "1" * 40
        head = "2" * 40
        classification = classification_record(base, head, required=True)
        record = readiness_record(
            base, head, {"src/vibe_loop/skills/x/SKILL.md": "a" * 64}
        )
        record["gate"]["required_case_conditions"].pop("runtime-owned-implementation")
        record["workflow_contract_regressions"]["parked"] = [
            {"id": "regression", "parked_task_ids": []}
        ]

        admission = build_release_admission(
            classification,
            readiness_record=record,
            distributions=(),
        )

        self.assertIn(
            "readiness release matrix is incomplete or altered",
            admission["diagnostics"],
        )
        self.assertIn(
            "readiness parked-regression evidence is invalid",
            admission["diagnostics"],
        )

    def test_exemption_rejects_owned_paths_uncertainty_and_tampered_paths(self) -> None:
        classification = classification_record("1" * 40, "2" * 40, required=False)
        classification["owned_paths"] = ["eval/fixture.json"]
        classification["uncertainty"] = ["shallow_repository"]

        admission = build_release_admission(
            classification,
            readiness_record=None,
            distributions=(),
        )

        self.assertEqual(admission["status"], "blocked")
        self.assertEqual(len(admission["diagnostics"]), 2)


def initialize_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    run(repo, "init", "-q")
    run(repo, "config", "user.name", "Test")
    run(repo, "config", "user.email", "test@example.com")
    return repo


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def classification_record(base: str, head: str, *, required: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "skill_release_classification",
        "ownership_contract_version": 1,
        "base": base,
        "head": head,
        "status": "readiness_required" if required else "unrelated_exemption",
        "changed_paths": [{"status": "M", "paths": ["README.md"]}],
        "owned_paths": ["eval/fixture.json"] if required else [],
        "uncertainty": [],
    }


def readiness_record(
    base: str, head: str, fingerprints: dict[str, str]
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "record_type": "skill_release_readiness",
        "status": "passed",
        "revision": {"base": base, "head": head},
        "bundled_skills": fingerprints,
        "gate": {
            "name": "bundled_skill_release_readiness",
            "minimum_trials_per_case_condition": 1,
            "required_case_conditions": {
                case_id: list(conditions)
                for case_id, conditions in sorted(
                    release_gate_case_conditions().items()
                )
            },
            "blockers": [],
        },
        "local_suite": {"coverage_status": "passed"},
        "trial_failures": {"status": "passed", "total": 0},
        "workflow_contract_regressions": {
            "evidence_status": "passed",
            "unresolved": [],
            "invalid_parked_ids": [],
            "parked": [],
        },
        "release_provenance": {"status": "passed", "gaps": []},
    }


def build_wheel(
    path: Path,
    fingerprints: dict[str, str],
    root: Path,
    *,
    content_override: bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for source in fingerprints:
            content = content_override or (root / source).read_bytes()
            archive.writestr(source.removeprefix("src/"), content)
