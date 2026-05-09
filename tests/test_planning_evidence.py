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
from vibe_loop.runs import RunStore, WorkerReport


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
            RunStore(repo / ".vibe-loop" / "runs.jsonl").append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                    commit=mapped_commit,
                )
            )

            evidence = collect_planning_evidence(load_config(repo)).to_json()

        self.assertEqual(
            [task["id"] for task in evidence["tasks"]],
            ["TASK-01", "TASK-02", "TASK-03"],
        )
        self.assertEqual(evidence["task_source_origin"], "default_markdown_discovery")
        self.assertEqual(evidence["run_attempts"][0]["record_type"], "worker_report")
        mapping_sources = {
            (mapping["task_id"], mapping["commit"], mapping["source"])
            for mapping in evidence["commit_mappings"]
        }
        self.assertIn(
            ("TASK-01", mapped_commit, "task_evidence_commit_ref"),
            mapping_sources,
        )
        self.assertIn(("TASK-01", mapped_commit, "worker_report"), mapping_sources)
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
                "'status':'Done','dependencies':[]}]))\n",
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
                f'list = "{sys.executable} list_tasks.py"\n\n'
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
                encoding="utf-8",
            )

            evidence = collect_planning_evidence(
                load_config(repo),
                git_commit_limit=1,
            ).to_json()

        self.assertEqual(evidence["task_source_origin"], "command_output")
        self.assertEqual(evidence["tasks"][0]["id"], "CMD-01")
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
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
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
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
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
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
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
                "[planning_analytics]\n"
                f'worklog_command = "{sys.executable} worklog.py"\n',
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
