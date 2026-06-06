from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main
from vibe_loop.config import TaskSourceConfig
from vibe_loop.evals import EvalArtifactRef, EvalSourceFingerprint, SkillEvalRunRecord
from vibe_loop.eval_examples import (
    EXAMPLE_SUITE_ID,
    EvalExampleCase,
    list_eval_example_cases,
    materialize_eval_example,
    run_eval_example_grader,
    teardown_eval_example,
)
from vibe_loop.locks import LockManager
from vibe_loop.runs import RunStore
from vibe_loop.tasks import build_task_source, runnable_tasks
from vibe_loop.workers import build_worker_views


EXPECTED_CASE_IDS = {
    "dirty-main-worktree",
    "finite-py-plan-table",
    "generated-roadmap-profile",
    "locked-task-selection",
    "integration-lock-unavailable",
    "main-advanced-before-merge",
    "main-integration-lock",
    "negative-trigger-set",
    "review-remediation",
    "supervised-worker-report",
    "workspace-duplicate-worktree",
    "workspace-foreign-dirty",
    "workspace-merged-branch",
    "workspace-missing-worktree",
}
REQUIRED_ARTIFACT_ROLES = {
    "prompt",
    "run_log",
    "transcript",
    "diff",
    "final_repo_state",
    "structured_result",
    "grader_outputs",
}


class EvalExampleTests(unittest.TestCase):
    def test_manifest_exposes_offline_paired_cases(self) -> None:
        cases = list_eval_example_cases()

        self.assertEqual({case.case_id for case in cases}, EXPECTED_CASE_IDS)
        for case in cases:
            with self.subTest(case=case.case_id):
                self.assertEqual(
                    case.conditions,
                    (
                        "no_skill",
                        "vibe_loop",
                        "vibe_loop_cli",
                        "orchestrated_vibe_loop",
                    ),
                )
                self.assertIs(case.budget["network"], False)
                self.assertGreater(case.budget["timeout_seconds"], 0)
                self.assertGreater(case.budget["max_commands"], 0)
                self.assertGreater(case.budget["max_output_bytes"], 0)
                self.assertTrue(
                    REQUIRED_ARTIFACT_ROLES.issubset(case.expected_artifact_roles)
                )
                self.assertTrue(case.repo_path.is_dir())
                self.assertTrue(
                    (case.repo_path / "eval" / "graders" / "grade.py").is_file()
                )
                self.assertTrue((case.repo_path / "eval" / "reference.patch").is_file())
                with (case.repo_path / "eval" / "expected-artifacts.json").open(
                    encoding="utf-8"
                ) as handle:
                    grader_spec = json.load(handle)
                self.assertEqual(grader_spec["suite_id"], EXAMPLE_SUITE_ID)
                self.assertEqual(grader_spec["case_id"], case.case_id)
                self.assertGreater(len(grader_spec["checks"]), 0)
                for prompt_path in case.prompt_paths:
                    self.assertTrue((case.repo_path / prompt_path).is_file())

    def test_materialize_and_teardown_copy_isolated_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "finite"

            repo = materialize_eval_example("finite-py-plan-table", destination)

            self.assertEqual(repo, destination)
            self.assertEqual(git_output(repo, "branch", "--show-current"), "main")
            self.assertEqual(git_output(repo, "status", "--short"), "")
            self.assertTrue((repo / "PLAN.md").is_file())
            self.assertTrue((repo / "eval" / "case.json").is_file())
            self.assertTrue((repo / "eval" / "expected-artifacts.json").is_file())
            self.assertFalse((repo / "eval" / "reference.patch").exists())

            teardown_eval_example(repo)

            self.assertFalse(repo.exists())

    def test_materialize_agent_workspace_hides_grader_internals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "finite"

            repo = materialize_eval_example(
                "finite-py-plan-table",
                destination,
                include_grader_internals=False,
            )

            self.assertTrue((repo / ".git").is_dir())
            self.assertEqual(len(git_output(repo, "rev-parse", "--verify", "HEAD")), 40)
            self.assertEqual(git_output(repo, "status", "--short"), "")
            self.assertTrue((repo / "eval" / "prompt.txt").is_file())
            self.assertFalse((repo / "eval" / "expected-artifacts.json").exists())
            self.assertFalse((repo / "eval" / "graders").exists())
            self.assertFalse((repo / "eval" / "reference.patch").exists())

    def test_materialize_seeds_dirty_state_and_live_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dirty = materialize_eval_example(
                "dirty-main-worktree",
                Path(directory) / "dirty",
            )
            locked = materialize_eval_example(
                "locked-task-selection",
                Path(directory) / "locked",
            )
            lock = json.loads(
                (
                    locked / ".vibe-loop" / "locks" / "SEL-01.lock" / "lock.json"
                ).read_text(encoding="utf-8")
            )

            dirty_status = git_output(dirty, "status", "--short")

        self.assertIn("M docs/local-notes.md", dirty_status)
        self.assertIn("?? scratch-note.txt", dirty_status)
        self.assertEqual(lock["pid"], os.getpid())
        self.assertEqual(lock["worker_pid"], os.getpid())
        self.assertEqual(lock["pid_source"], "popen")

    def test_materialize_seeds_workspace_ownership_regressions(self) -> None:
        expected = {
            "workspace-duplicate-worktree": (
                "DUP-01",
                "warning",
                ["duplicate_branch_worktrees"],
            ),
            "workspace-missing-worktree": (
                "MISS-01",
                "stale",
                ["missing_claimed_worktree"],
            ),
            "workspace-merged-branch": (
                "MERGED-01",
                "warning",
                ["branch_already_merged"],
            ),
            "workspace-foreign-dirty": (
                "DIRTY-01",
                "warning",
                ["foreign_dirty_claimed_worktree"],
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case_id, (task_id, status, codes) in expected.items():
                with self.subTest(case=case_id):
                    repo = materialize_eval_example(case_id, root / case_id)
                    views = build_worker_views(
                        LockManager(repo / ".vibe-loop" / "locks"),
                        RunStore(repo / ".vibe-loop" / "runs.jsonl"),
                        repo=repo,
                    )
                    by_task = {view.active.task_id: view for view in views}
                    view = by_task[task_id]
                    workspace_state = view.workspace_git_state
                    if workspace_state is None:
                        self.fail(f"workspace state missing for {case_id}")

                    self.assertEqual(workspace_state.status, status)
                    self.assertEqual(
                        [diagnostic.code for diagnostic in workspace_state.diagnostics],
                        codes,
                    )
                    self.assertEqual(
                        git_output(repo, "status", "--short").splitlines(),
                        [f"M .vibe-loop/locks/{task_id}.lock/lock.json"],
                    )

    def test_materialize_seeds_live_integration_lock_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "integration-lock-unavailable",
                Path(directory) / "busy",
            )
            lock = json.loads(
                (
                    repo
                    / ".vibe-loop"
                    / "locks"
                    / "main-integration.lock"
                    / "lock.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(lock["owner_task_id"], "OTHER-01")
        self.assertEqual(lock["run_id"], "eval-run-other-live")
        self.assertEqual(lock["pid"], os.getpid())
        self.assertEqual(lock["pid_source"], "fixture-live-holder")

    def test_materialize_workspace_case_overwrite_cleans_seed_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "duplicate"

            materialize_eval_example("workspace-duplicate-worktree", destination)
            workspaces = destination.parent / "duplicate-workspaces"
            self.assertTrue(workspaces.is_dir())

            materialize_eval_example(
                "workspace-duplicate-worktree",
                destination,
                overwrite=True,
            )
            views = build_worker_views(
                LockManager(destination / ".vibe-loop" / "locks"),
                RunStore(destination / ".vibe-loop" / "runs.jsonl"),
                repo=destination,
            )

            self.assertTrue(workspaces.is_dir())
            self.assertEqual(len(views), 1)
            self.assertEqual(
                [diagnostic.code for diagnostic in views[0].workspace_diagnostics],
                ["duplicate_branch_worktrees"],
            )

    def test_teardown_refuses_non_fixture_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-fixture"
            path.mkdir()

            with self.assertRaisesRegex(ValueError, "refusing to remove"):
                teardown_eval_example(path)

            self.assertTrue(path.exists())

    def test_finite_fixture_grader_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "finite-py-plan-table",
                Path(directory) / "finite",
            )

            failing = run_eval_example_grader(repo)
            self.assertFalse(failing.passed)

            calculator = repo / "src" / "finite_math" / "calculator.py"
            calculator.write_text(
                "from __future__ import annotations\n\n\n"
                "def loyalty_total(subtotal: int, *, member: bool) -> int:\n"
                "    discount = 10 if member else 0\n"
                "    return subtotal - discount\n",
                encoding="utf-8",
            )
            plan = repo / "PLAN.md"
            plan.write_text(
                plan.read_text(encoding="utf-8").replace(
                    "| FPY-01 | P0 | Planned |",
                    "| FPY-01 | P0 | Done |",
                ),
                encoding="utf-8",
            )

            passing = run_eval_example_grader(repo)
            payload = json.loads(passing.stdout)

        self.assertTrue(passing.passed, passing.stdout + passing.stderr)
        self.assertEqual(payload["case_id"], "finite-py-plan-table")
        self.assertTrue(all(check["passed"] for check in payload["checks"]))

    def test_generated_roadmap_profile_parses_tasks(self) -> None:
        generated = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "generated-roadmap-profile"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                generated.case_id,
                Path(directory) / "roadmap",
            )
            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type=generated.task_source["type"],
                    profile=generated.task_source["profile"],
                    runnable_statuses=tuple(generated.task_source["runnable_statuses"]),
                ),
            )

            tasks = source.list_tasks()
            candidates = runnable_tasks(source, ("Ready",))

        self.assertEqual([task.task_id for task in tasks], ["ROAD-01", "ROAD-02"])
        self.assertEqual(tasks[0].status, "Done")
        self.assertEqual(tasks[1].dependencies, ("ROAD-01",))
        self.assertEqual([task.task_id for task in candidates], ["ROAD-02"])

    def test_generated_roadmap_materialization_writes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "generated-roadmap-profile",
                Path(directory) / "roadmap",
            )
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )
            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="markdown-profile",
                    profile=cache["profile"],
                    runnable_statuses=("Ready",),
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(cache["status"], "profile")
        self.assertEqual(cache["source_fingerprints"][0]["path"], "docs/roadmap.md")
        self.assertEqual([task.task_id for task in tasks], ["ROAD-01", "ROAD-02"])

    def test_generated_cache_rejects_unsafe_source_fingerprint_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                "generated-roadmap-profile",
                root / "roadmap",
            )
            cache_path = repo / ".vibe-loop" / "generated-task-source.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["source_fingerprints"][0]["path"] = "../outside.md"
            cache_path.write_text(
                json.dumps(cache, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "outside.md").write_text("# Outside\n", encoding="utf-8")

            result = run_eval_example_grader(repo)

        self.assertFalse(result.passed)
        self.assertIn("generated source missing: ../outside.md", result.stdout)

    def test_generated_cache_rejects_unsafe_profile_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                "generated-roadmap-profile",
                root / "roadmap",
            )
            cache_path = repo / ".vibe-loop" / "generated-task-source.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["profile"]["source_paths"] = ["../outside.md"]
            cache_path.write_text(
                json.dumps(cache, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "outside.md").write_text("# Outside\n", encoding="utf-8")

            result = run_eval_example_grader(repo)

        self.assertFalse(result.passed)
        self.assertIn(
            "generated profile source path is unsafe: ../outside.md",
            result.stdout,
        )

    def test_negative_trigger_fixture_grades_clean_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "negative-trigger-set",
                Path(directory) / "negative",
            )

            result = run_eval_example_grader(repo)

        self.assertTrue(result.passed, result.stdout + result.stderr)

    def test_reference_patches_calibrate_positive_graders(self) -> None:
        positive_cases = [case for case in list_eval_example_cases() if case.positive]
        with tempfile.TemporaryDirectory() as directory:
            for case in positive_cases:
                with self.subTest(case=case.case_id):
                    repo = materialize_eval_example(
                        case.case_id,
                        Path(directory) / case.case_id,
                        include_reference_patch=True,
                    )
                    apply_reference_patch(repo)

                    result = run_eval_example_grader(repo)

                    self.assertTrue(result.passed, result.stdout + result.stderr)

    def test_artifact_bundle_grader_validates_workflow_trace(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "finite-py-plan-table"
        )
        required_events = [
            "skill_activated",
            "instructions_inspected",
            "worktree_state_inspected",
            "branch_or_worktree_created",
            "verification_ran",
            "review_requested",
            "commit_created",
            "main_fast_forwarded",
            "main_verification_ran",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                case.case_id,
                root / "repo",
                include_reference_patch=True,
            )
            apply_reference_patch(repo)
            valid_artifacts = root / "valid-artifacts"
            write_artifact_bundle(valid_artifacts, case, required_events)
            invalid_artifacts = root / "invalid-artifacts"
            write_artifact_bundle(
                invalid_artifacts,
                case,
                [event for event in required_events if event != "review_requested"],
            )

            valid = run_eval_example_grader(repo, artifact_root=valid_artifacts)
            invalid = run_eval_example_grader(repo, artifact_root=invalid_artifacts)

        self.assertTrue(valid.passed, valid.stdout + valid.stderr)
        self.assertFalse(invalid.passed)
        self.assertIn("missing events: review_requested", invalid.stdout)

    def test_artifact_bundle_grader_validates_case_event_order(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "review-remediation"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                case.case_id,
                root / "repo",
                include_reference_patch=True,
            )
            apply_reference_patch(repo)
            artifact_root = root / "artifacts"
            write_artifact_bundle(
                artifact_root,
                case,
                [
                    "skill_activated",
                    "verification_ran",
                    "review_finding_addressed",
                    "review_requested",
                    "review_finding_received",
                    "rereview_requested",
                    "commit_created",
                    "main_fast_forwarded",
                ],
            )

            result = run_eval_example_grader(repo, artifact_root=artifact_root)

        self.assertFalse(result.passed)
        self.assertIn("workflow event order missing", result.stdout)

    def test_artifact_bundle_grader_rejects_unsafe_paths_before_reading(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "finite-py-plan-table"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                case.case_id,
                root / "repo",
                include_reference_patch=True,
            )
            apply_reference_patch(repo)
            artifact_root = root / "artifacts"
            write_artifact_bundle(artifact_root, case, ["skill_activated"])
            run_record_path = artifact_root / "run.json"
            run_record = json.loads(run_record_path.read_text(encoding="utf-8"))
            for artifact in run_record["artifacts"]:
                if artifact["role"] == "workflow_events":
                    artifact["path"] = "../outside.json"
            run_record_path.write_text(
                json.dumps(run_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "outside.json").write_text(
                '{"events": ["review_requested"]}\n',
                encoding="utf-8",
            )

            result = run_eval_example_grader(repo, artifact_root=artifact_root)

        self.assertFalse(result.passed)
        self.assertIn("artifact path must be a safe relative path", result.stdout)
        self.assertNotIn("review_requested", result.stdout)

    def test_artifact_bundle_grader_reports_malformed_run_json(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "finite-py-plan-table"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                case.case_id,
                root / "repo",
                include_reference_patch=True,
            )
            apply_reference_patch(repo)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            (artifact_root / "run.json").write_text("{not-json", encoding="utf-8")

            result = run_eval_example_grader(repo, artifact_root=artifact_root)
            payload = json.loads(result.stdout)

        self.assertFalse(result.passed)
        self.assertEqual(payload["checks"][-1]["id"], "artifact-run-record-schema")
        self.assertIn(
            "run.json must contain an object",
            payload["checks"][-1]["message"],
        )

    def test_artifact_bundle_requires_case_specific_roles(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "finite-py-plan-table"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(
                case.case_id,
                root / "repo",
                include_reference_patch=True,
            )
            apply_reference_patch(repo)
            artifact_root = root / "artifacts"
            write_artifact_bundle(
                artifact_root,
                case,
                [
                    "skill_activated",
                    "instructions_inspected",
                    "worktree_state_inspected",
                    "branch_or_worktree_created",
                    "verification_ran",
                    "review_requested",
                    "commit_created",
                    "main_fast_forwarded",
                    "main_verification_ran",
                ],
            )
            run_record_path = artifact_root / "run.json"
            run_record = json.loads(run_record_path.read_text(encoding="utf-8"))
            for artifact in run_record["artifacts"]:
                if artifact["role"] == "test_results":
                    artifact["required"] = False
                    (artifact_root / artifact["path"]).unlink()
            run_record_path.write_text(
                json.dumps(run_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = run_eval_example_grader(repo, artifact_root=artifact_root)

        self.assertFalse(result.passed)
        self.assertIn(
            "required artifact role marked optional: test_results",
            result.stdout,
        )

    def test_negative_prompt_results_use_validated_artifact_role_path(self) -> None:
        case = next(
            case
            for case in list_eval_example_cases()
            if case.case_id == "negative-trigger-set"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = materialize_eval_example(case.case_id, root / "repo")
            artifact_root = root / "artifacts"
            write_artifact_bundle(
                artifact_root,
                case,
                [],
            )
            result_path = artifact_root / "negative_prompt_results.json"
            content = "{not-json"
            result_path.write_text(content, encoding="utf-8")
            run_record_path = artifact_root / "run.json"
            run_record = json.loads(run_record_path.read_text(encoding="utf-8"))
            for artifact in run_record["artifacts"]:
                if artifact["role"] == "negative_prompt_results":
                    artifact["sha256"] = hashlib.sha256(content.encode()).hexdigest()
            run_record_path.write_text(
                json.dumps(run_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = run_eval_example_grader(repo, artifact_root=artifact_root)
            payload = json.loads(result.stdout)

        self.assertFalse(result.passed)
        self.assertEqual(payload["checks"][-1]["id"], "artifact-negative-prompts")
        self.assertIn("results must be a list", payload["checks"][-1]["message"])

    def test_manifest_suite_id_is_stable(self) -> None:
        self.assertEqual(EXAMPLE_SUITE_ID, "local-demo-v1")

    def test_autopilot_status_and_one_cycle_run_on_generic_demo_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = materialize_eval_example(
                "finite-py-plan-table", Path(directory) / "demo"
            )

            status_out = StringIO()
            with redirect_stdout(status_out), redirect_stderr(StringIO()):
                status_code = main(
                    ["autopilot", "status", "--repo", str(repo), "--json"]
                )
            status = json.loads(status_out.getvalue())

            run_out = StringIO()
            with redirect_stdout(run_out), redirect_stderr(StringIO()):
                run_code = main(
                    [
                        "autopilot",
                        "run",
                        "--repo",
                        str(repo),
                        "--once",
                        "--min-ready",
                        "999",
                    ]
                )
            summary = json.loads(run_out.getvalue())
            records = RunStore(repo / ".vibe-loop" / "runs.jsonl").read_records()
            # Autopilot writes only untracked .vibe-loop/ runtime state; tracked
            # project files must be untouched.
            tracked_unchanged = (
                subprocess.run(["git", "diff", "--quiet"], cwd=repo).returncode == 0
            )

        self.assertEqual(status_code, 0)
        self.assertEqual(status["repo"], str(repo))
        for key in ("queue", "supervisor", "blockers", "git"):
            self.assertIn(key, status)
        self.assertNotIn("faceapp", status_out.getvalue().lower())

        self.assertTrue(summary["started"])
        self.assertEqual(len(summary["cycles"]), 1)
        cycle = summary["cycles"][0]
        # A high --min-ready keeps the supervisor from launching a child, so the
        # one cycle stays idle (agent available) or blocked (agent absent in CI);
        # either way it never spawns run-until-done and never mutates the repo.
        self.assertIn(cycle["status"], {"idle", "blocked"})
        self.assertEqual(cycle["child_log"], "")
        self.assertIn(run_code, {0, 1})
        self.assertEqual(
            sum(1 for record in records if record["record_type"] == "autopilot_cycle"),
            1,
        )
        self.assertTrue(tracked_unchanged)
        self.assertNotIn("faceapp", run_out.getvalue().lower())


def apply_reference_patch(repo: Path) -> None:
    subprocess.run(
        ["git", "apply", "--unidiff-zero", "eval/reference.patch"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write_artifact_bundle(
    root: Path,
    case: EvalExampleCase,
    workflow_events: list[str],
) -> None:
    root.mkdir()
    artifacts: list[EvalArtifactRef] = []
    for role in case.expected_artifact_roles:
        content = artifact_content(role, workflow_events)
        relative_path = artifact_path(role)
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content_bytes = content.encode("utf-8")
        path.write_bytes(content_bytes)
        artifacts.append(
            EvalArtifactRef(
                role=role,
                path=relative_path,
                sha256=hashlib.sha256(content_bytes).hexdigest(),
            )
        )
    record = SkillEvalRunRecord(
        suite_id=EXAMPLE_SUITE_ID,
        case_id=case.case_id,
        trial=1,
        condition="vibe_loop",
        run_id=f"{case.case_id}-vibe-loop-1",
        task={
            "id": case.task_id or case.case_id,
            "prompt_sha256": "1" * 64,
            "expected_skill": "vibe-loop",
            "should_trigger": case.positive,
        },
        skill_condition={
            "id": "vibe_loop",
            "skills_available": True,
            "skill_id": "vibe-loop",
            "skill_sha256": "2" * 64,
        },
        agent={"name": "codex", "command_source": "fixture"},
        model={"provider": "openai", "id": "gpt-5.5"},
        harness={
            "name": "vibe-loop-eval",
            "version": "0.1",
            "command": "codex exec",
        },
        budget={
            "timeout_seconds": case.budget["timeout_seconds"],
            "max_commands": case.budget["max_commands"],
            "max_output_bytes": case.budget["max_output_bytes"],
        },
        source_fingerprints=(
            EvalSourceFingerprint(path="PLAN.md", sha256="3" * 64, size=1),
        ),
        artifacts=artifacts,
        final_repo_state={"head": "abc123", "branch": "main", "dirty": False},
        structured_result={
            "exit_code": 0,
            "timeout": False,
            "task_status": "completed",
            "workflow_contract_completed": True,
        },
        graders=({"id": "fixture", "type": "deterministic", "passed": True},),
        scoring={
            "passed": True,
            "task_score": 1.0,
            "workflow_score": 1.0,
            "trigger_score": 1.0,
            "excluded_from_primary": False,
        },
        reproducibility={
            "fixture_sha256": "4" * 64,
            "run_order": 1,
            "fresh_workspace": True,
            "state_reused": False,
        },
        status="passed",
        started_at="2026-05-09T00:00:00+00:00",
        finished_at="2026-05-09T00:01:00+00:00",
    )
    (root / "run.json").write_text(
        json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def artifact_path(role: str) -> str:
    paths = {
        "prompt": "prompt.txt",
        "run_log": "logs/run.log",
        "transcript": "transcript.jsonl",
        "diff": "diff.patch",
        "final_repo_state": "final-repo-state.json",
        "structured_result": "run-result.json",
        "grader_outputs": "grader-outputs.json",
        "workflow_events": "workflow-events.json",
        "workspace_evidence": "workspace-evidence.json",
    }
    return paths.get(role, f"{role}.json")


def artifact_content(role: str, workflow_events: list[str]) -> str:
    if role == "workflow_events":
        return json.dumps({"events": workflow_events}, sort_keys=True) + "\n"
    if role == "transcript":
        return "{}\n"
    if role == "diff":
        return "diff --git a/file b/file\n"
    if role == "grader_outputs":
        return "[]\n"
    if (
        role.endswith("_state")
        or role.endswith("_results")
        or role.endswith("_evidence")
    ):
        return "{}\n"
    return f"{role}\n"


if __name__ == "__main__":
    unittest.main()
