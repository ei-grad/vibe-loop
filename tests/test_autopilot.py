from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_loop.autopilot import (
    AutopilotCycleResult,
    autopilot_child_command,
    collect_project_status,
    collect_supervisor_status,
    run_autopilot,
)
from vibe_loop.config import load_config
from vibe_loop.locks import AUTOPILOT_LOCK_NAME, build_lock_manager
from vibe_loop.runs import (
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
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


class AutopilotRunTests(unittest.TestCase):
    def _recording_launcher(self):
        calls: list[dict[str, object]] = []

        def launcher(command, *, cwd, log_path, on_start=None):
            calls.append(
                {
                    "command": list(command),
                    "cwd": Path(cwd),
                    "log_path": Path(log_path),
                }
            )
            if on_start is not None:
                on_start(4242)
            return 0

        return launcher, calls

    def test_once_launches_child_and_records_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(config, once=True, jobs=2, launcher=launcher)

            manager = build_lock_manager(
                config.repo, config.state_path / "locks", config.locks
            )
            lock = manager.status(AUTOPILOT_LOCK_NAME)
            records = RunStore(config.state_path / "runs.jsonl").read_records()

        self.assertTrue(summary.started)
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(len(summary.cycles), 1)
        self.assertEqual(summary.cycles[0].status, "completed")
        self.assertEqual(summary.cycles[0].child_pid, 4242)
        self.assertEqual(len(calls), 1)
        self.assertIn("run-until-done", calls[0]["command"])
        self.assertIn("--jobs", calls[0]["command"])
        self.assertIn("2", calls[0]["command"])
        self.assertIsNone(lock)
        types = [record["record_type"] for record in records]
        self.assertIn(AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE, types)
        self.assertIn(AUTOPILOT_CYCLE_RECORD_TYPE, types)

    def test_blocks_launch_when_repo_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            run(repo, "git", "add", "tracked.txt")
            config = load_config(repo)
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(config, once=True, launcher=launcher)

        self.assertTrue(summary.started)
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(len(calls), 0)
        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "blocked")
        self.assertIsNone(cycle.child_pid)
        self.assertIn("repo_dirty", cycle.blockers)

    def test_low_ready_queue_is_idle_without_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(config, once=True, min_ready=2, launcher=launcher)

        self.assertTrue(summary.started)
        self.assertEqual(len(calls), 0)
        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertIn("no_runnable_work", summary.cycles[0].actions)

    def test_observes_live_supervisor_without_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo, config.state_path / "locks", config.locks
            )
            holder = manager.acquire_autopilot(run_id="holder-run")
            launcher, calls = self._recording_launcher()
            try:
                summary = run_autopilot(config, once=True, launcher=launcher)
                records = RunStore(config.state_path / "runs.jsonl").read_records()
            finally:
                manager.release_autopilot(
                    run_id="holder-run",
                    fencing_token=str(holder.metadata.get("fencing_token") or ""),
                )

        self.assertFalse(summary.started)
        self.assertEqual(summary.exit_code, 2)
        self.assertEqual(summary.blocker, "autopilot_supervisor_active")
        self.assertEqual(len(calls), 0)
        self.assertIn(
            AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
            [record["record_type"] for record in records],
        )

    def test_reports_stale_supervisor_lock_without_stealing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo, config.state_path / "locks", config.locks
            )
            manager.acquire_autopilot(run_id="holder-run")
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(
                config,
                once=True,
                launcher=launcher,
                process_exists=lambda pid: False,
            )
            lock_still_held = manager.status(AUTOPILOT_LOCK_NAME) is not None

        self.assertFalse(summary.started)
        self.assertEqual(len(calls), 0)
        self.assertTrue(summary.blocker.startswith("autopilot_supervisor_lock_stale"))
        self.assertTrue(lock_still_held)

    def test_max_cycles_runs_bounded_watch_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            launcher, calls = self._recording_launcher()
            sleeps: list[float] = []

            summary = run_autopilot(
                config,
                max_cycles=3,
                interval=5.0,
                launcher=launcher,
                sleep=sleeps.append,
            )

        self.assertTrue(summary.started)
        self.assertEqual(len(summary.cycles), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [5.0, 5.0])

    def test_child_command_includes_configured_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            command = autopilot_child_command(
                config,
                jobs=3,
                ask_agent=True,
                continue_on_failure=True,
                max_slices=4,
                max_tasks=2,
            )

        self.assertEqual(command[1:4], ["-m", "vibe_loop", "run-until-done"])
        self.assertIn("--ask-agent", command)
        self.assertIn("--continue-on-failure", command)
        self.assertEqual(command[command.index("--jobs") + 1], "3")
        self.assertEqual(command[command.index("--max-slices") + 1], "4")
        self.assertEqual(command[command.index("--max-tasks") + 1], "2")


def configured_repo(repo: Path, rows: list[tuple[str, str, str, str]]) -> None:
    init_repo(repo)
    (repo / ".vibe-loop.toml").write_text(
        '[agent]\ncommand = "codex exec {prompt}"\n',
        encoding="utf-8",
    )
    write_plan(repo, rows)
    run(repo, "git", "add", "PLAN.md", ".vibe-loop.toml")
    run(repo, "git", "commit", "-m", "initial")


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
