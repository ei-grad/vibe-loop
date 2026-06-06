from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_loop.autopilot import (
    AutopilotCycleResult,
    collect_project_status,
    collect_supervisor_status,
)
from vibe_loop.config import load_config
from vibe_loop.locks import build_lock_manager
from vibe_loop.runs import (
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    RunStore,
)
from vibe_loop.workers import ActiveRunState


class AutopilotStatusTests(unittest.TestCase):
    def test_collect_project_status_summarizes_repo_queue_workers_and_cycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(
                repo,
                [
                    ("TASK-01", "Active", "", "active slice"),
                    ("TASK-02", "Next", "", "ready slice"),
                    ("TASK-03", "Done", "", "finished slice"),
                ],
            )
            commit_all(repo)
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            run_store = RunStore(config.state_path / "runs.jsonl")
            active = ActiveRunState.new(
                task_id="TASK-01",
                run_id="run-1",
                log_path=config.state_path / "runs" / "run-1.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
            ).with_worker_pid(12345)
            manager.acquire(
                "TASK-01",
                "run-1",
                metadata=active.to_lock_metadata(),
            )
            (repo / "notes.txt").write_text("dirty\n", encoding="utf-8")

            status = collect_project_status(
                config,
                process_exists=lambda pid: pid == 12345,
            )
            AutopilotCycleResult(
                cycle_id="cycle-1",
                repo=config.repo,
                status="blocked",
                occurred_at="2026-05-09T00:00:00+00:00",
                project_status=status,
                actions=("observed",),
                blockers=status.blockers,
                next_wake="2026-05-09T00:05:00+00:00",
            ).append_to(run_store)

            updated = collect_project_status(
                config,
                process_exists=lambda pid: pid == 12345,
            )
            payload = updated.to_json()
            records = run_store.read_records()

        self.assertTrue(payload["git"]["dirty"])
        self.assertIn("repo_dirty", payload["blockers"])
        self.assertEqual(payload["queue"]["total"], 3)
        self.assertEqual(payload["queue"]["done"], 1)
        self.assertEqual(payload["queue"]["runnable"], 1)
        self.assertEqual(
            [task["id"] for task in payload["queue"]["runnable_tasks"]],
            ["TASK-02"],
        )
        self.assertEqual(len(payload["workers"]), 1)
        self.assertEqual(payload["workers"][0]["state"], "running")
        self.assertEqual(payload["last_cycle"]["cycle_id"], "cycle-1")
        self.assertEqual(payload["next_wake"], "2026-05-09T00:05:00+00:00")
        self.assertEqual(records[-1]["record_type"], AUTOPILOT_CYCLE_RECORD_TYPE)

    def test_collect_project_status_excludes_configured_state_dir_from_dirty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            (repo / ".vibe-loop.toml").write_text(
                'state_dir = ".state/vibe-loop"\n',
                encoding="utf-8",
            )
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            run(repo, "git", "add", "PLAN.md", ".vibe-loop.toml")
            run(repo, "git", "commit", "-m", "initial")
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            status = collect_project_status(config)
            AutopilotCycleResult(
                cycle_id="cycle-1",
                repo=config.repo,
                status="observed",
                occurred_at="2026-05-09T00:00:00+00:00",
                project_status=status,
            ).append_to(run_store)
            updated = collect_project_status(config).to_json()

        self.assertFalse(updated["git"]["dirty"])
        self.assertNotIn("repo_dirty", updated["blockers"])

    def test_collect_project_status_reports_unavailable_agent_as_blocker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "custom"\n',
                encoding="utf-8",
            )
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            run(repo, "git", "add", "PLAN.md", ".vibe-loop.toml")
            run(repo, "git", "commit", "-m", "initial")
            config = load_config(repo)

            payload = collect_project_status(config).to_json()

        self.assertEqual(payload["queue"]["runnable"], 1)
        self.assertTrue(
            any(
                blocker.startswith("agent_unavailable:")
                for blocker in payload["blockers"]
            )
        )
        self.assertTrue(payload["agent"]["diagnostics"])

    def test_collect_project_status_keeps_nonblocking_agent_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\ncommand = "codex exec {prompt}"\n'
                'selection_command = "codex exec {prompt}"\n',
                encoding="utf-8",
            )
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            run(repo, "git", "add", "PLAN.md", ".vibe-loop.toml")
            run(repo, "git", "commit", "-m", "initial")
            config = load_config(repo)

            payload = collect_project_status(config).to_json()

        self.assertTrue(payload["agent"]["diagnostics"])
        self.assertFalse(
            any(
                blocker.startswith("agent_unavailable:")
                for blocker in payload["blockers"]
            )
        )

    def test_collect_project_status_does_not_require_selection_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "custom"\n'
                'command = "custom-worker {prompt}"\n'
                'prompt_dialect = "codex"\n',
                encoding="utf-8",
            )
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            run(repo, "git", "add", "PLAN.md", ".vibe-loop.toml")
            run(repo, "git", "commit", "-m", "initial")
            config = load_config(repo)

            payload = collect_project_status(config).to_json()

        self.assertTrue(payload["agent"]["diagnostics"])
        self.assertEqual(payload["queue"]["runnable"], 1)
        self.assertFalse(
            any(
                blocker.startswith("agent_unavailable:")
                for blocker in payload["blockers"]
            )
        )

    def test_collect_project_status_observes_no_runnable_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, [("TASK-01", "Done", "", "finished slice")])
            commit_all(repo)
            config = load_config(repo)

            payload = collect_project_status(config).to_json()

        self.assertEqual(payload["queue"]["runnable"], 0)
        self.assertIn("no_runnable_work", payload["observations"])

    def test_collect_project_status_reports_task_source_errors_as_blockers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            config = load_config(repo)

            status = collect_project_status(config)
            payload = status.to_json()

        self.assertEqual(payload["queue"]["total"], 0)
        self.assertTrue(
            any(
                blocker.startswith("task_source_unavailable:")
                for blocker in payload["blockers"]
            )
        )

    def test_supervisor_status_skips_command_result_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_store = RunStore(Path(directory) / "runs.jsonl")
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                    "cycle_id": "cycle-1",
                    "repo": directory,
                    "pid": 999,
                    "log": str(Path(directory) / "autopilot.log"),
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                }
            )
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
                    "cycle_id": "cycle-1",
                    "repo": directory,
                    "status": "completed",
                    "occurred_at": "2026-05-09T00:00:10+00:00",
                }
            )

            payload = collect_supervisor_status(
                run_store,
                process_exists=lambda pid: pid == 999,
            ).to_json()

        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["pid"], 999)
        self.assertEqual(payload["cycle_id"], "cycle-1")


def init_repo(repo: Path) -> None:
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.com")
    run(repo, "git", "config", "user.name", "Test User")


def write_plan(
    repo: Path,
    rows: list[tuple[str, str, str, str]],
) -> None:
    lines = [
        "# Plan",
        "",
        "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task_id, status, dependencies, scope in rows:
        lines.append(
            f"| {task_id} | P0 | {status} | {dependencies} | {scope} | works | tests |"
        )
    (repo / "PLAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def commit_all(repo: Path) -> None:
    run(repo, "git", "add", "PLAN.md")
    run(repo, "git", "commit", "-m", "initial")


def git_text(repo: Path, *args: str) -> str:
    result = run(repo, "git", *args)
    return result.stdout.strip()


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
