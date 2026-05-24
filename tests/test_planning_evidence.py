from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.config import load_config
from vibe_loop.planning_evidence import collect_planning_evidence, run_worklog_command
from vibe_loop.runs import (
    LOCK_EXPIRED_RECORD_TYPE,
    RunLifecycleEvent,
    RunStore,
    WorkerReport,
)

PYTHON = sys.executable.replace("\\", "/")


PLAN_TEMPLATE = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Done | none | Finished mapped task. | Works. | {task_01_evidence} |
| TASK-02 | P1 | Done | none | Finished without mapping. | Works. | Missing mapping. |
| TASK-03 | P1 | Planned | TASK-01 | Future task. | Works. | Not started. |
"""


class PlanningEvidenceTests(unittest.TestCase):
    def test_collects_markdown_tasks_run_records_trailers_and_commit_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                PLAN_TEMPLATE.format(task_01_evidence="pending"),
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(
                repo,
                "commit",
                "-m",
                "complete task",
                "-m",
                "Plan-Item: TASK-01",
            )
            mapped_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "PLAN.md").write_text(
                PLAN_TEMPLATE.format(
                    task_01_evidence=f"Finished in commit {mapped_commit[:12]}."
                ),
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(
                repo,
                "commit",
                "-m",
                "record task evidence",
                "-m",
                "Plan-Item: TASK-01",
            )
            (repo / "src").mkdir()
            (repo / "src" / "unmapped.py").write_text("value = 1\n", encoding="utf-8")
            git(repo, "add", "src/unmapped.py")
            git(repo, "commit", "-m", "unmapped implementation")
            unmapped_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            run_store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                    commit=mapped_commit,
                )
            )
            run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_EXPIRED_RECORD_TYPE,
                    run_id="run-stale",
                    task_id="BOGUS-LIFECYCLE",
                    lock_kind="task",
                    lock_path=repo / ".vibe-loop" / "locks" / "BOGUS.lock",
                    payload={"stale_reason": "missing_process"},
                )
            )

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertEqual(
            [task["id"] for task in evidence["tasks"]],
            ["TASK-01", "TASK-02", "TASK-03"],
        )
        self.assertEqual(evidence["task_source_origin"], "default_markdown_discovery")
        self.assertEqual(evidence["run_attempts"][0]["record_type"], "worker_report")
        self.assertFalse(
            any(
                attempt["task_id"] == "BOGUS-LIFECYCLE"
                for attempt in evidence["run_attempts"]
            )
        )
        mapping_sources = {
            (mapping["task_id"], mapping["commit"], mapping["source"])
            for mapping in evidence["commit_mappings"]
        }
        self.assertIn(
            ("TASK-01", mapped_commit, "task_evidence_commit_ref"),
            mapping_sources,
        )
        self.assertIn(("TASK-01", mapped_commit, "worker_report"), mapping_sources)
        warning_task_ids = {
            (warning["code"], warning.get("task_id"))
            for warning in evidence["warnings"]
        }
        self.assertNotIn(
            ("unknown_task_reference", "BOGUS-LIFECYCLE"),
            warning_task_ids,
        )
        self.assertTrue(
            any(
                mapping["task_id"] == "TASK-01"
                and mapping["source"] == "plan_item_trailer"
                for mapping in evidence["commit_mappings"]
            )
        )
        warnings = {
            (warning["code"], warning.get("task_id"), warning.get("commit"))
            for warning in evidence["warnings"]
        }
        self.assertIn(
            ("done_task_without_authoritative_mapping", "TASK-02", None),
            warnings,
        )
        self.assertIn(("unmapped_commit", None, unmapped_commit), warnings)

    def test_collects_command_tasks_and_worklog_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "src").mkdir()
            (repo / "src" / "feature.py").write_text(
                "enabled = True\n", encoding="utf-8"
            )
            git(repo, "add", "src/feature.py")
            git(repo, "commit", "-m", "implement command task")
            mapped_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'CMD-01','title':'Command task',"
                "'status':'Done','dependencies':[],"
                "'requirement_ids':['PRD-SDE-003'],"
                "'spec_paths':['docs/spec.md'],"
                "'design_refs':['ADR-1'],"
                "'approval_state':'approved',"
                "'source_fingerprints':[{'path':'docs/spec.md','size':10,"
                "'sha256':'" + "d" * 64 + "','redacted':False}]}]))\n",
                encoding="utf-8",
            )
            (repo / "worklog.py").write_text(
                "import json\n"
                f"print(json.dumps({{'task_id':'CMD-01','status':'completed',"
                f"'commit':'{mapped_commit}'}}))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                f'list = "{PYTHON} list_tasks.py"\n\n'
                "[planning_analytics]\n"
                f'worklog_command = "{PYTHON} worklog.py"\n',
                encoding="utf-8",
            )

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=1,
            ).to_json()

        self.assertEqual(evidence["task_source_origin"], "command_output")
        self.assertEqual(evidence["tasks"][0]["id"], "CMD-01")
        self.assertEqual(evidence["tasks"][0]["requirement_ids"], ["PRD-SDE-003"])
        self.assertEqual(evidence["tasks"][0]["spec_paths"], ["docs/spec.md"])
        self.assertEqual(evidence["tasks"][0]["design_refs"], ["ADR-1"])
        self.assertEqual(evidence["tasks"][0]["approval_state"], "approved")
        self.assertEqual(
            evidence["tasks"][0]["source_fingerprints"],
            [
                {
                    "path": "docs/spec.md",
                    "size": 10,
                    "sha256": "d" * 64,
                    "redacted": False,
                }
            ],
        )
        self.assertTrue(evidence["worklog"]["configured"])
        self.assertIn(
            {
                "task_id": "CMD-01",
                "commit": mapped_commit,
                "source": "worklog",
                "record_index": 0,
                "authoritative": True,
            },
            evidence["commit_mappings"],
        )
        self.assertNotIn(
            ("unmapped_commit", mapped_commit),
            {
                (warning["code"], warning.get("commit"))
                for warning in evidence["warnings"]
            },
        )

    def test_worklog_jsonl_records_are_joined_to_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Done | none | Done. | Works. | Worklog. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "TASK-01 work")
            mapped_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "worklog.py").write_text(
                "import json\n"
                f"print(json.dumps({{'task_id':'TASK-01','status':'completed'}}))\n"
                f"print(json.dumps({{'task_ids':['TASK-01'],'commits':['{mapped_commit}']}}))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[planning_analytics]\nworklog_command = "{PYTHON} worklog.py"\n',
                encoding="utf-8",
            )

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        worklog_evidence = [
            item
            for item in evidence["completion_evidence"]
            if item["source"] == "worklog"
        ]
        self.assertEqual(worklog_evidence[0]["task_id"], "TASK-01")
        self.assertIn(
            ("TASK-01", mapped_commit, "worklog"),
            {
                (mapping["task_id"], mapping["commit"], mapping["source"])
                for mapping in evidence["commit_mappings"]
            },
        )

    def test_subject_matching_is_diagnostic_and_does_not_satisfy_coverage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Planned | none | Planned. | Works. | Not started. |\n",
                encoding="utf-8",
            )
            (repo / "notes.txt").write_text("note\n", encoding="utf-8")
            git(repo, "add", "PLAN.md", "notes.txt")
            git(repo, "commit", "-m", "TASK-01 update notes")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertIn(
            {
                "task_id": "TASK-01",
                "commit": commit,
                "source": "subject_match",
                "authoritative": False,
            },
            evidence["diagnostic_commit_mappings"],
        )
        self.assertEqual(evidence["commit_mappings"], [])
        unmapped = [
            warning
            for warning in evidence["warnings"]
            if warning["code"] == "unmapped_commit"
        ]
        self.assertEqual(unmapped[0]["commit"], commit)
        self.assertEqual(unmapped[0]["diagnostic_task_ids"], ["TASK-01"])

    def test_metadata_only_commits_and_secret_paths_are_exempt_from_warnings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Planned | none | Planned. | Works. | Not started. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "baseline")
            (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            git(repo, "add", "-f", ".env")
            git(repo, "commit", "-m", "rotate token")
            (repo / ".vibe-loop").mkdir()
            (repo / ".vibe-loop" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
            git(repo, "add", "-f", ".vibe-loop/runs.jsonl")
            git(repo, "commit", "-m", "record run metadata")

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=2,
            ).to_json()

        skipped = {
            (item["path"], item["reason"]) for item in evidence["skipped_evidence"]
        }
        self.assertIn((".env", "secret_path"), skipped)
        exempt_reasons = {
            commit["coverage_exempt_reason"] for commit in evidence["commits"]
        }
        self.assertEqual(exempt_reasons, {"metadata_only", "secret_paths_only"})
        self.assertNotIn(
            "unmapped_commit",
            {warning["code"] for warning in evidence["warnings"]},
        )

    def test_unknown_worklog_task_does_not_satisfy_commit_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Planned | none | Planned. | Works. | Not started. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "baseline")
            (repo / "feature.txt").write_text("work\n", encoding="utf-8")
            git(repo, "add", "feature.txt")
            git(repo, "commit", "-m", "work without known task")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "worklog.py").write_text(
                "import json\n"
                f"print(json.dumps({{'task_id':'BOGUS','commit':'{commit}'}}))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[planning_analytics]\nworklog_command = "{PYTHON} worklog.py"\n',
                encoding="utf-8",
            )

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=1,
            ).to_json()

        self.assertEqual(evidence["commit_mappings"], [])
        warnings = {
            (warning["code"], warning.get("task_id"), warning.get("commit"))
            for warning in evidence["warnings"]
        }
        self.assertIn(("unknown_task_reference", "BOGUS", None), warnings)
        self.assertIn(("unmapped_commit", None, commit), warnings)

    def test_symbolic_worklog_commit_refs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Planned | none | Planned. | Works. | Not started. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "TASK-01 work")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "worklog.py").write_text(
                "import json\n"
                "print(json.dumps({'task_id':'TASK-01','commit_ref':'HEAD'}))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[planning_analytics]\nworklog_command = "{PYTHON} worklog.py"\n',
                encoding="utf-8",
            )

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertEqual(evidence["commit_mappings"], [])
        warnings = {
            (warning["code"], warning.get("commit")) for warning in evidence["warnings"]
        }
        self.assertIn(("invalid_commit_ref", None), warnings)
        self.assertIn(("unmapped_commit", commit), warnings)

    def test_failed_worker_report_does_not_satisfy_done_task_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Done | none | Done. | Works. | No final mapping. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "TASK-01 attempted work")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            RunStore(repo / ".vibe-loop" / "runs.jsonl").append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="failed",
                    commit=commit,
                )
            )

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertEqual(evidence["commit_mappings"][0]["authoritative"], False)
        warnings = {
            (warning["code"], warning.get("task_id"), warning.get("commit"))
            for warning in evidence["warnings"]
        }
        self.assertIn(
            ("done_task_without_authoritative_mapping", "TASK-01", None),
            warnings,
        )
        self.assertIn(("unmapped_commit", None, commit), warnings)

    def test_requirement_coverage_maps_reports_trailers_reviews_and_tests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([\n"
                "  {'id':'TASK-01','title':'Done task','status':'Done',"
                "'dependencies':[],'requirement_ids':['REQ-1']},\n"
                "  {'id':'TASK-02','title':'Attempted task','status':'Planned',"
                "'dependencies':[],'requirement_ids':['REQ-2']},\n"
                "  {'id':'TASK-03','title':'Missing evidence','status':'Done',"
                "'dependencies':[],'requirement_ids':['REQ-3']},\n"
                "  {'id':'TASK-04','title':'Future task','status':'Planned',"
                "'dependencies':[],'requirement_ids':['REQ-4']},\n"
                "]))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[task_source]\nlist = "{PYTHON} list_tasks.py"\n',
                encoding="utf-8",
            )
            git(repo, "add", "list_tasks.py", ".vibe-loop.toml")
            git(repo, "commit", "-m", "baseline")
            (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
            git(repo, "add", "feature.py")
            git(
                repo,
                "commit",
                "-m",
                "satisfy requirement",
                "-m",
                "Plan-Item: TASK-01\n"
                "Review: review-1\n"
                "Test: pytest tests/test_planning_evidence.py",
            )
            satisfied_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "attempt.py").write_text("value = 2\n", encoding="utf-8")
            git(repo, "add", "attempt.py")
            git(repo, "commit", "-m", "attempt requirement")
            attempted_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "unmapped.py").write_text("value = 3\n", encoding="utf-8")
            git(repo, "add", "unmapped.py")
            git(
                repo,
                "commit",
                "-m",
                "direct requirement evidence",
                "-m",
                "Requirement: REQ-X",
            )
            unmapped_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
            RunStore(repo / ".vibe-loop" / "runs.jsonl").append_report(
                WorkerReport(
                    run_id="run-2",
                    task_id="TASK-02",
                    status="failed",
                    commit=attempted_commit,
                    metadata={
                        "plan_items": ["TASK-02"],
                        "requirement_ids": ["REQ-2"],
                        "reviews": ["review-2"],
                        "tests": ["pytest attempted"],
                    },
                )
            )

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=3,
            ).to_json()

        coverage = {
            item["requirement_id"]: item for item in evidence["requirement_coverage"]
        }
        self.assertEqual(coverage["REQ-1"]["status"], "satisfied")
        self.assertEqual(coverage["REQ-1"]["satisfied_task_ids"], ["TASK-01"])
        self.assertIn(satisfied_commit, coverage["REQ-1"]["commits"])
        self.assertEqual(coverage["REQ-1"]["review_refs"], ["review-1"])
        self.assertEqual(
            coverage["REQ-1"]["test_refs"],
            ["pytest tests/test_planning_evidence.py"],
        )
        self.assertEqual(coverage["REQ-2"]["status"], "attempted")
        self.assertEqual(coverage["REQ-2"]["attempted_task_ids"], ["TASK-02"])
        self.assertIn(attempted_commit, coverage["REQ-2"]["commits"])
        self.assertEqual(coverage["REQ-2"]["review_refs"], ["review-2"])
        self.assertEqual(coverage["REQ-2"]["test_refs"], ["pytest attempted"])
        self.assertEqual(coverage["REQ-3"]["status"], "missing_evidence")
        self.assertEqual(
            coverage["REQ-3"]["missing_evidence_task_ids"],
            ["TASK-03"],
        )
        self.assertEqual(coverage["REQ-4"]["status"], "pending")
        self.assertEqual(coverage["REQ-X"]["status"], "unmapped")
        self.assertEqual(coverage["REQ-X"]["task_ids"], [])
        self.assertIn(unmapped_commit, coverage["REQ-X"]["commits"])
        mapping_sources = {
            (mapping["requirement_id"], mapping["source"])
            for mapping in evidence["requirement_mappings"]
        }
        self.assertIn(("REQ-1", "plan_item_trailer"), mapping_sources)
        self.assertIn(("REQ-2", "worker_report_metadata"), mapping_sources)
        attempt = next(
            item for item in evidence["run_attempts"] if item["run_id"] == "run-2"
        )
        self.assertEqual(
            attempt["metadata_evidence"],
            {
                "plan_items": ["TASK-02"],
                "requirement_ids": ["REQ-2"],
                "review_refs": ["review-2"],
                "test_refs": ["pytest attempted"],
            },
        )
        warning_keys = {
            (
                warning["code"],
                warning.get("requirement_id"),
                tuple(warning.get("diagnostic_task_ids", [])),
            )
            for warning in evidence["warnings"]
        }
        self.assertIn(("unmapped_requirement", "REQ-X", ()), warning_keys)
        self.assertIn(
            ("requirement_missing_evidence", "REQ-3", ("TASK-03",)),
            warning_keys,
        )

    def test_requirement_only_trailer_does_not_satisfy_done_plan_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'TASK-01','title':'Done task',"
                "'status':'Done','dependencies':[],"
                "'requirement_ids':['REQ-1']}]))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[task_source]\nlist = "{PYTHON} list_tasks.py"\n',
                encoding="utf-8",
            )
            git(repo, "add", "list_tasks.py", ".vibe-loop.toml")
            git(repo, "commit", "-m", "baseline")
            (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
            git(repo, "add", "feature.py")
            git(repo, "commit", "-m", "work", "-m", "Requirement: REQ-1")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=1,
            ).to_json()

        coverage = evidence["requirement_coverage"][0]
        self.assertEqual(coverage["requirement_id"], "REQ-1")
        self.assertEqual(coverage["status"], "missing_evidence")
        self.assertEqual(coverage["satisfied_task_ids"], [])
        self.assertEqual(coverage["missing_evidence_task_ids"], ["TASK-01"])
        self.assertIn(commit, coverage["commits"])

    def test_worklog_output_is_bounded_with_skipped_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_git_repo(repo)
            (repo / "PLAN.md").write_text(
                "# Plan\n\n"
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | P0 | Planned | none | Planned. | Works. | Not started. |\n",
                encoding="utf-8",
            )
            git(repo, "add", "PLAN.md")
            git(repo, "commit", "-m", "baseline")
            (repo / "worklog.py").write_text(
                "import sys\nsys.stdout.write('x' * 4096)\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                f'[planning_analytics]\nworklog_command = "{PYTHON} worklog.py"\n',
                encoding="utf-8",
            )

            with patch("vibe_loop.planning_evidence.MAX_WORKLOG_OUTPUT_BYTES", 64):
                evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertIn(
            ("worklog", "worklog_output_too_large"),
            {(item["path"], item["reason"]) for item in evidence["skipped_evidence"]},
        )
        self.assertIn(
            "worklog_output_too_large",
            {warning["code"] for warning in evidence["warnings"]},
        )

    def test_worklog_timeout_kills_children_holding_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "hold_stdout.py").write_text(
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            started = time.monotonic()
            result = run_worklog_command(
                f"{sys.executable} hold_stdout.py",
                repo,
                timeout_seconds=1,
                max_stdout_bytes=64,
            )
            elapsed = time.monotonic() - started

        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 5)


def init_git_repo(repo: Path) -> None:
    git(repo, "init")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.com")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
