from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_loop.autopilot import (
    AutopilotCycleResult,
    MaintenanceCommandResult,
    ProjectEntry,
    ProjectRegistry,
    autopilot_child_command,
    collect_external_run_supervisor,
    collect_project_status,
    collect_registry_status,
    collect_task_queue_status,
    collect_supervisor_status,
    cycle_schedule_deadline,
    cycle_should_recheck,
    limit_wall_pause_seconds,
    parse_wait_deadline,
    poll_wait_message_command,
    poll_runnable_count,
    recheck_interval_for_runnable,
    recheck_sleep_slices,
    run_autopilot,
    run_maintenance_command,
    run_worktree_disposition,
    wait_for_processes,
    WaitMessageAdapterError,
)
from vibe_loop.config import load_config
from vibe_loop.locks import AUTOPILOT_LOCK_NAME, build_lock_manager
from vibe_loop.runs import (
    AUTOPILOT_COMMAND_RESULT_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
    AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    RunResult,
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

    def test_collect_project_status_reports_active_workers_when_queue_filtered(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            commit_all(repo)
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            active = ActiveRunState.new(
                task_id="ACTIVE-01",
                run_id="run-active",
                log_path=config.state_path / "runs" / "run-active.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
                resources=("api",),
                conflict_domains_known=True,
            ).with_worker_pid(os.getpid())
            manager.acquire(
                "ACTIVE-01",
                "run-active",
                metadata=active.to_lock_metadata(),
            )

            payload = collect_project_status(config).to_json()

        self.assertEqual(payload["queue"]["statuses"]["Next"], 1)
        self.assertEqual(payload["queue"]["runnable"], 0)
        self.assertIn("waiting_for_active_workers:1", payload["observations"])
        self.assertNotIn("no_runnable_work", payload["observations"])

    def test_collect_project_status_counts_foreign_host_workers_as_waiting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            commit_all(repo)
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            active = ActiveRunState.new(
                task_id="ACTIVE-01",
                run_id="run-active",
                log_path=config.state_path / "runs" / "run-active.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
                resources=("api",),
                conflict_domains_known=True,
            )
            metadata = active.to_lock_metadata()
            metadata["host"] = "other-host"
            manager.acquire("ACTIVE-01", "run-active", metadata=metadata)

            payload = collect_project_status(config).to_json()

        self.assertEqual(payload["workers"][0]["state"], "unknown")
        self.assertEqual(payload["workers"][0]["process_state"], "foreign_host")
        self.assertEqual(payload["queue"]["runnable"], 0)
        self.assertIn("waiting_for_active_workers:1", payload["observations"])
        self.assertNotIn("no_runnable_work", payload["observations"])

    def test_collect_project_status_counts_lowercase_queue_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(
                repo,
                [
                    ("TASK-01", "active", "", "active slice"),
                    ("TASK-02", "blocked", "", "blocked slice"),
                    ("TASK-03", "gated", "", "gated slice"),
                    ("TASK-04", "low", "", "low-priority slice"),
                    ("TASK-05", "done", "", "finished slice"),
                    ("TASK-06", "on-hold", "", "operator-held slice"),
                ],
            )
            commit_all(repo)
            config = load_config(repo)

            payload = collect_project_status(config).to_json()

        self.assertEqual(payload["queue"]["active"], 1)
        self.assertEqual(payload["queue"]["blocked"], 3)
        self.assertEqual(payload["queue"]["done"], 1)
        self.assertEqual(payload["queue"]["statuses"]["on-hold"], 1)

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

    def test_project_status_keeps_live_supervisor_across_pidless_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            manager.acquire_autopilot(
                run_id="autopilot-1",
                metadata={"pid": 999},
            )
            run_store = RunStore(config.state_path / "runs.jsonl")
            log_path = config.state_path / "autopilot" / "autopilot-1.log"
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                    "run_id": "autopilot-1",
                    "pid": 999,
                    "log": str(log_path),
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                }
            )
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE,
                    "run_id": "autopilot-1",
                    "pid": 999,
                    "occurred_at": "2026-05-09T00:00:05+00:00",
                }
            )
            for index in range(2):
                run_store.append_record(
                    {
                        "schema_version": 1,
                        "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
                        "cycle_id": f"autopilot-1-c{index + 1}",
                        "status": "idle",
                        "occurred_at": f"2026-05-09T00:00:1{index}+00:00",
                    }
                )

            payload = collect_project_status(
                config,
                process_exists=lambda pid: pid == 999,
            ).to_json()

        self.assertEqual(payload["supervisor"]["state"], "running")
        self.assertEqual(payload["supervisor"]["pid"], 999)
        self.assertEqual(payload["supervisor"]["run_id"], "autopilot-1")
        self.assertEqual(payload["supervisor"]["log"], str(log_path))
        self.assertEqual(payload["last_cycle"]["cycle_id"], "autopilot-1-c2")
        self.assertEqual(payload["last_cycle"]["status"], "idle")

    def test_project_status_does_not_treat_unlocked_supervisor_as_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                    "run_id": "autopilot-exited",
                    "pid": 999,
                    "log": str(config.state_path / "autopilot" / "exited.log"),
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                }
            )

            payload = collect_project_status(
                config,
                process_exists=lambda pid: pid == 999,
            ).to_json()

        self.assertNotEqual(payload["supervisor"]["state"], "running")

    def test_project_status_does_not_treat_stale_supervisor_lock_as_live(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            manager.acquire_autopilot(
                run_id="autopilot-stale",
                metadata={"pid": 999},
            )
            run_store = RunStore(config.state_path / "runs.jsonl")
            run_store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                    "run_id": "autopilot-stale",
                    "pid": 999,
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                }
            )

            payload = collect_project_status(
                config,
                process_exists=lambda _pid: False,
            ).to_json()

        self.assertNotEqual(payload["supervisor"]["state"], "running")
        self.assertEqual(payload["supervisor"]["run_id"], "autopilot-stale")


class ExternalRunSupervisorTests(unittest.TestCase):
    def _store_with_records(
        self, directory: str, records: list[dict[str, object]]
    ) -> RunStore:
        store = RunStore(Path(directory) / "runs.jsonl")
        for record in records:
            store.append_record(record)
        return store

    def test_live_started_record_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_records(
                directory,
                [
                    {
                        "record_type": "run_supervisor_started",
                        "pid": 4321,
                        "occurred_at": "2026-06-10T00:00:00+00:00",
                    }
                ],
            )
            pid = collect_external_run_supervisor(
                store, process_exists=lambda pid: pid == 4321
            )
        self.assertEqual(pid, 4321)

    def test_exited_record_clears_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_records(
                directory,
                [
                    {
                        "record_type": "run_supervisor_started",
                        "pid": 4321,
                        "occurred_at": "2026-06-10T00:00:00+00:00",
                    },
                    {
                        "record_type": "run_supervisor_exited",
                        "pid": 4321,
                        "occurred_at": "2026-06-10T01:00:00+00:00",
                    },
                ],
            )
            pid = collect_external_run_supervisor(
                store, process_exists=lambda pid: True
            )
        self.assertIsNone(pid)

    def test_dead_process_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_records(
                directory,
                [
                    {
                        "record_type": "run_supervisor_started",
                        "pid": 4321,
                        "occurred_at": "2026-06-10T00:00:00+00:00",
                    }
                ],
            )
            pid = collect_external_run_supervisor(
                store, process_exists=lambda pid: False
            )
        self.assertIsNone(pid)


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

    def test_observes_external_run_until_done_instead_of_launching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            launcher, calls = self._recording_launcher()
            run_store = RunStore(config.state_path / "runs.jsonl")
            run_store.append_record(
                {
                    "record_type": "run_supervisor_started",
                    "pid": 7777,
                    "occurred_at": "2026-06-10T00:00:00+00:00",
                }
            )

            summary = run_autopilot(
                config,
                once=True,
                launcher=launcher,
                process_exists=lambda pid: pid == 7777,
            )

        self.assertTrue(summary.started)
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(len(calls), 0)
        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "observing")
        self.assertEqual(cycle.child_pid, 7777)
        self.assertIn("observed_external_run_until_done:7777", cycle.actions)

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
        policy_records = [
            record
            for record in records
            if record["record_type"]
            in {
                AUTOPILOT_SUPERVISOR_STARTED_RECORD_TYPE,
                AUTOPILOT_CYCLE_RECORD_TYPE,
                AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
            }
        ]
        self.assertTrue(policy_records)
        self.assertTrue(
            all(
                record["worktree_disposition_policy"] == "report-only"
                if record["record_type"] != AUTOPILOT_WORKTREE_REAP_RECORD_TYPE
                else record["policy"] == "report-only"
                for record in policy_records
            )
        )

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

    def test_cleans_stale_worker_locks_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo, config.state_path / "locks", config.locks
            )
            run_store = RunStore(config.state_path / "runs.jsonl")
            active = ActiveRunState.new(
                task_id="STALE-01",
                run_id="run-stale",
                log_path=config.state_path / "runs" / "run-stale.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
            ).with_worker_pid(987654321)
            manager.acquire(
                "STALE-01",
                "run-stale",
                metadata=active.to_lock_metadata(),
            )
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(
                config,
                once=True,
                launcher=launcher,
                process_exists=lambda pid: False,
            )
            stale_lock = manager.status("STALE-01")
            records = run_store.read_records()

        self.assertTrue(summary.started)
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(stale_lock)
        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "completed")
        self.assertIn("cleaned_stale_locks:1", cycle.actions)
        self.assertNotIn("stale_locks_present", cycle.blockers)
        expired_records = [
            record for record in records if record.get("record_type") == "lock_expired"
        ]
        self.assertEqual(len(expired_records), 1)
        self.assertEqual(expired_records[0]["task_id"], "STALE-01")
        self.assertEqual(expired_records[0]["run_id"], "run-stale")
        self.assertEqual(expired_records[0]["stale_reason"], "missing_process")
        cycle_records = [
            record
            for record in records
            if record.get("record_type") == AUTOPILOT_CYCLE_RECORD_TYPE
        ]
        self.assertEqual(cycle_records[-1]["actions"][0], "cleaned_stale_locks:1")

    def test_does_not_clean_worker_lock_before_pid_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo, config.state_path / "locks", config.locks
            )
            run_store = RunStore(config.state_path / "runs.jsonl")
            active = ActiveRunState.new(
                task_id="STARTING-01",
                run_id="run-starting",
                log_path=config.state_path / "runs" / "run-starting.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
            )
            manager.acquire(
                "STARTING-01",
                "run-starting",
                metadata=active.to_lock_metadata(),
            )
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(
                config,
                once=True,
                launcher=launcher,
                process_exists=lambda pid: False,
            )
            starting_lock = manager.status("STARTING-01")
            records = run_store.read_records()

        self.assertTrue(summary.started)
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(len(calls), 0)
        self.assertIsNotNone(starting_lock)
        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "blocked")
        self.assertIn("stale_locks_present", cycle.blockers)
        self.assertNotIn("cleaned_stale_locks:1", cycle.actions)
        self.assertFalse(
            any(record.get("record_type") == "lock_expired" for record in records)
        )

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
        self.assertIn("planning_unconfigured", summary.cycles[0].actions)
        self.assertIn("low_runnable_work:1/2", summary.cycles[0].actions)

    def test_empty_queue_reports_no_runnable_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Done", "", "finished slice")])
            config = load_config(repo)
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(config, once=True, launcher=launcher)
            cycle_records = [
                record
                for record in RunStore(config.state_path / "runs.jsonl").read_records()
                if record.get("record_type") == "autopilot_cycle"
            ]

        self.assertTrue(summary.started)
        self.assertEqual(len(calls), 0)
        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertIn("planning_unconfigured", summary.cycles[0].actions)
        self.assertIn("no_runnable_work", summary.cycles[0].actions)
        self.assertEqual(len(cycle_records), 1)
        self.assertIn("planning_unconfigured", cycle_records[0]["actions"])

    def test_low_ready_queue_with_live_worker_reports_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            manager = build_lock_manager(
                config.repo,
                config.state_path / "locks",
                config.locks,
            )
            active = ActiveRunState.new(
                task_id="ACTIVE-01",
                run_id="run-active",
                log_path=config.state_path / "runs" / "run-active.log",
                base_main=git_text(repo, "rev-parse", "HEAD"),
                command="codex",
                resources=("api",),
                conflict_domains_known=True,
            ).with_worker_pid(os.getpid())
            manager.acquire(
                "ACTIVE-01",
                "run-active",
                metadata=active.to_lock_metadata(),
            )
            launcher, calls = self._recording_launcher()

            summary = run_autopilot(config, once=True, launcher=launcher)

        self.assertTrue(summary.started)
        self.assertEqual(len(calls), 0)
        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertIn("waiting_for_active_workers:1", summary.cycles[0].actions)
        self.assertNotIn("planning_unconfigured", summary.cycles[0].actions)
        self.assertNotIn("no_runnable_work", summary.cycles[0].actions)

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
        observed = next(
            record
            for record in records
            if record["record_type"] == AUTOPILOT_SUPERVISOR_OBSERVED_RECORD_TYPE
        )
        self.assertEqual(observed["worktree_disposition_policy"], "report-only")

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


class LimitWallPauseTests(unittest.TestCase):
    UTC = datetime.timezone.utc

    def _limit_wall_result(
        self,
        repo: Path,
        *,
        task_id: str = "TASK-01",
        message: str = "",
        finished_at: str = "",
    ) -> RunResult:
        kwargs: dict[str, object] = {
            "run_id": f"run-{task_id}",
            "task_id": task_id,
            "classification": "limit_wall",
            "exit_code": 1,
            "log_path": repo / f"{task_id}.log",
            "start_main": "aaa",
            "end_main": "aaa",
            "message": message,
        }
        if finished_at:
            kwargs["finished_at"] = finished_at
        return RunResult(**kwargs)  # type: ignore[arg-type]

    def test_no_limit_wall_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            pause = limit_wall_pause_seconds(store, since="", default_backoff=1800.0)
        self.assertIsNone(pause)

    def test_uses_reset_delay_from_recorded_message(self) -> None:
        now = datetime.datetime(2026, 7, 13, 0, 0, tzinfo=self.UTC)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                self._limit_wall_result(repo, message="resets 1am (UTC)")
            )
            pause = limit_wall_pause_seconds(
                store, since="", default_backoff=1800.0, now=now
            )
        self.assertIsNotNone(pause)
        self.assertAlmostEqual(pause, 3600 + 120.0, delta=1.0)

    def test_falls_back_to_default_backoff_without_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(self._limit_wall_result(repo, message=""))
            pause = limit_wall_pause_seconds(store, since="", default_backoff=1234.0)
        self.assertEqual(pause, 1234.0)

    def test_ignores_records_finished_before_since(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                self._limit_wall_result(repo, finished_at="2026-07-13T00:00:00+00:00")
            )
            pause = limit_wall_pause_seconds(
                store,
                since="2026-07-13T01:00:00+00:00",
                default_backoff=1800.0,
            )
        self.assertIsNone(pause)

    def test_takes_longest_pause_across_walls(self) -> None:
        now = datetime.datetime(2026, 7, 13, 0, 0, tzinfo=self.UTC)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                self._limit_wall_result(repo, task_id="TASK-01", message="")
            )
            store.append_result(
                self._limit_wall_result(
                    repo, task_id="TASK-02", message="resets 2am (UTC)"
                )
            )
            pause = limit_wall_pause_seconds(
                store, since="", default_backoff=1800.0, now=now
            )
        # 2am reset (2h + margin) beats the 1800s default from the other wall.
        self.assertAlmostEqual(pause, 2 * 3600 + 120.0, delta=1.0)

    def test_run_autopilot_pauses_dispatch_on_limit_wall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml="[supervision]\nlimit_wall_backoff_seconds = 42\n",
            )
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            def launcher(command, *, cwd, log_path, on_start=None):
                if on_start is not None:
                    on_start(4242)
                run_store.append_result(self._limit_wall_result(Path(cwd)))
                return 0

            sleeps: list[float] = []
            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=5.0,
                launcher=launcher,
                sleep=sleeps.append,
            )

        self.assertTrue(summary.started)
        # The limit-wall backoff replaces the normal interval between cycles.
        self.assertEqual(sleeps, [42.0])
        self.assertEqual(summary.cycles[0].limit_wall_pause_seconds, 42.0)
        self.assertIn("limit_wall_pause:42s", summary.cycles[0].actions)


class AutopilotRecheckTests(unittest.TestCase):
    def _recording_launcher(self):
        calls: list[list[str]] = []

        def launcher(command, *, cwd, log_path, on_start=None):
            calls.append(list(command))
            if on_start is not None:
                on_start(4242)
            return 0

        return launcher, calls

    def _planning_runner(self):
        kinds: list[str] = []

        def runner(
            command, kind, cycle_id, *, cwd, env_extra, timeout, max_output_bytes
        ):
            kinds.append(kind)
            return MaintenanceCommandResult(
                kind=kind,
                cycle_id=cycle_id,
                exit_code=0,
                duration_seconds=0.0,
                output=f"{kind}-output",
                output_truncated=False,
                timed_out=False,
            )

        return runner, kinds

    def _future_limit_wall_result(self, repo: Path) -> RunResult:
        return RunResult(  # type: ignore[arg-type]
            run_id="run-limit",
            task_id="TASK-LW",
            classification="limit_wall",
            exit_code=1,
            log_path=repo / "limit.log",
            start_main="aaa",
            end_main="aaa",
            message="",
            finished_at="2099-01-01T00:00:00+00:00",
        )

    def test_recheck_sleep_slices_partition_interval(self) -> None:
        self.assertEqual(list(recheck_sleep_slices(100.0, 10.0)), [10.0] * 10)
        self.assertEqual(list(recheck_sleep_slices(25.0, 10.0)), [10.0, 10.0, 5.0])
        # A non-positive recheck collapses to a single full-interval slice.
        self.assertEqual(list(recheck_sleep_slices(30.0, 0.0)), [30.0])
        # A non-positive interval yields nothing so drain mode never polls.
        self.assertEqual(list(recheck_sleep_slices(0.0, 10.0)), [])
        self.assertEqual(list(recheck_sleep_slices(-5.0, 10.0)), [])

    def test_recheck_interval_wakes_early_when_runnable_appears(self) -> None:
        sleeps: list[float] = []
        probes = iter([0, 0, 1])

        woke_early = recheck_interval_for_runnable(
            object(),  # config is unused; the runnable probe is injected
            interval=100.0,
            recheck_seconds=10.0,
            sleeper=sleeps.append,
            runnable_probe=lambda _config: next(probes),
        )

        self.assertTrue(woke_early)
        self.assertEqual(sleeps, [10.0, 10.0, 10.0])

    def test_recheck_interval_falls_through_when_no_runnable(self) -> None:
        sleeps: list[float] = []

        woke_early = recheck_interval_for_runnable(
            object(),  # config is unused; the runnable probe is injected
            interval=100.0,
            recheck_seconds=10.0,
            sleeper=sleeps.append,
            runnable_probe=lambda _config: 0,
        )

        self.assertFalse(woke_early)
        self.assertEqual(sleeps, [10.0] * 10)

    def test_recheck_interval_stops_when_requested(self) -> None:
        sleeps: list[float] = []

        woke_early = recheck_interval_for_runnable(
            object(),  # config is unused; the runnable probe is injected
            interval=100.0,
            recheck_seconds=10.0,
            sleeper=sleeps.append,
            should_stop=lambda: True,
            runnable_probe=lambda _config: 0,
        )

        self.assertFalse(woke_early)
        self.assertEqual(sleeps, [10.0])

    def test_recheck_interval_requires_min_ready_to_wake_early(self) -> None:
        # A steady below-threshold runnable count (1 task, dispatch needs 2)
        # never wakes the recheck early: the next cycle could not dispatch it, so
        # waking only re-runs planning and spins. The full interval elapses in
        # slices instead.
        sleeps: list[float] = []
        woke_early = recheck_interval_for_runnable(
            object(),  # config is unused; the runnable probe is injected
            interval=100.0,
            recheck_seconds=10.0,
            sleeper=sleeps.append,
            runnable_probe=lambda _config: 1,
            min_ready=2,
        )
        self.assertFalse(woke_early)
        self.assertEqual(sleeps, [10.0] * 10)

    def test_recheck_interval_wakes_when_min_ready_reached(self) -> None:
        # Once enough runnable work appears to cross the dispatch threshold the
        # recheck wakes early so the next cycle dispatches.
        sleeps: list[float] = []
        probes = iter([1, 1, 2])
        woke_early = recheck_interval_for_runnable(
            object(),  # config is unused; the runnable probe is injected
            interval=100.0,
            recheck_seconds=10.0,
            sleeper=sleeps.append,
            runnable_probe=lambda _config: next(probes),
            min_ready=2,
        )
        self.assertTrue(woke_early)
        self.assertEqual(sleeps, [10.0, 10.0, 10.0])

    def test_below_threshold_runnable_does_not_spin_recheck(self) -> None:
        # A phantom / below-dispatch-threshold runnable count must not leave the
        # supervisor spinning early cycles without ever backing off: planning
        # still fires on each idle cycle, and the recheck sleeps the full
        # interval in slices rather than waking early on a count the dispatch
        # gate would reject.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready scope")],
                extra_toml=(
                    "[autopilot]\n"
                    "planning_recheck_seconds = 10.0\n"
                    'planning_command = "plan"\n'
                ),
            )
            config = load_config(repo)
            runner, kinds = self._planning_runner()
            launcher, launcher_calls = self._recording_launcher()
            sleeps: list[float] = []

            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=100.0,
                min_ready=2,
                launcher=launcher,
                maintenance_runner=runner,
                sleep=sleeps.append,
            )

        # One ready task with min_ready=2 stays idle: the cycle never dispatches.
        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertEqual(len(launcher_calls), 0)
        # Planning fires on every idle cycle (it is never starved) ...
        self.assertEqual(kinds, ["planning", "planning"])
        # ... and the recheck does not wake early on the below-threshold count;
        # it sleeps the full interval in slices instead of spinning.
        self.assertEqual(sleeps, [10.0] * 10)

    def test_poll_runnable_count_reports_zero_on_source_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready scope")])
            config = load_config(repo)
            self.assertEqual(poll_runnable_count(config), 1)
            # A task-source failure must be reported as no runnable work, never
            # crash the poll, so the supervisor keeps waiting.
            (repo / "PLAN.md").unlink()
            self.assertEqual(poll_runnable_count(config), 0)

    def test_poll_runnable_count_survives_command_source_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready scope")],
                # A command-backed source shells out with check=True, so a
                # nonzero exit raises CalledProcessError (a SubprocessError, not
                # in the parser trio collect_task_queue_status catches). The poll
                # must still report zero rather than crashing the supervisor.
                extra_toml='[task_source]\nlist = "exit 3"\n',
            )
            config = load_config(repo)
            self.assertEqual(poll_runnable_count(config), 0)

    def test_poll_runnable_count_survives_command_source_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready scope")],
                extra_toml='[task_source]\ntype = "command"\nlist = "list-tasks"\n',
            )
            config = load_config(repo)

            def raise_timeout(
                *args: object, **kwargs: object
            ) -> subprocess.CompletedProcess:
                raise subprocess.TimeoutExpired(
                    cmd=args[0], timeout=kwargs.get("timeout")
                )

            # A hung task-source command expires as TimeoutExpired instead of
            # hanging forever; the poll must still report zero runnable so the
            # supervisor keeps waiting rather than crashing.
            with mock.patch("vibe_loop.tasks.subprocess.run", raise_timeout):
                self.assertEqual(poll_runnable_count(config), 0)

    def test_collect_task_queue_status_reports_source_error_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready scope")],
                extra_toml='[task_source]\ntype = "command"\nlist = "list-tasks"\n',
            )
            config = load_config(repo)

            def raise_timeout(
                *args: object, **kwargs: object
            ) -> subprocess.CompletedProcess:
                raise subprocess.TimeoutExpired(
                    cmd=args[0], timeout=kwargs.get("timeout")
                )

            with mock.patch("vibe_loop.tasks.subprocess.run", raise_timeout):
                status = collect_task_queue_status(config)

        # The cycle-start list path folds a timeout into source_error (a blocker)
        # rather than letting it propagate and crash the supervisor.
        self.assertTrue(status.source_error)
        self.assertEqual(status.runnable, 0)

    def test_cycle_should_recheck_only_for_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready scope")])
            project_status = collect_project_status(load_config(repo))

        def result(status: str) -> AutopilotCycleResult:
            return AutopilotCycleResult(
                cycle_id="c1",
                repo=Path("/tmp"),
                status=status,
                occurred_at="",
                project_status=project_status,
            )

        self.assertTrue(cycle_should_recheck(result("idle")))
        for status in (
            "completed",
            "restartable",
            "terminated",
            "observing",
            "blocked",
        ):
            self.assertFalse(cycle_should_recheck(result(status)))

    def test_idle_planning_cycle_rechecks_and_wakes_early(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-00", "Next", "MISSING", "blocked scope")],
                extra_toml=(
                    "[autopilot]\n"
                    "planning_recheck_seconds = 10.0\n"
                    "require_clean_repo = false\n"
                    'planning_command = "plan"\n'
                ),
            )
            config = load_config(repo)
            runner, kinds = self._planning_runner()
            launcher, launcher_calls = self._recording_launcher()
            sleeps: list[float] = []

            def sleeper(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 2:
                    # A detached planning agent lands a runnable task mid-recheck.
                    write_plan(
                        repo,
                        [
                            ("TASK-00", "Next", "MISSING", "blocked scope"),
                            ("TASK-01", "Next", "", "ready scope"),
                        ],
                    )

            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=100.0,
                launcher=launcher,
                maintenance_runner=runner,
                sleep=sleeper,
            )

        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertIn("ran_planning_command:exit=0", summary.cycles[0].actions)
        self.assertEqual(kinds, ["planning"])
        # Recheck slices, not one full interval; woke on the second poll.
        self.assertEqual(sleeps, [10.0, 10.0])
        # The next cycle picked up the freshly planned task and dispatched.
        self.assertEqual(len(launcher_calls), 1)
        self.assertEqual(summary.cycles[1].status, "completed")

    def test_idle_cycle_without_new_work_sleeps_full_interval_in_slices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-00", "Next", "MISSING", "blocked scope")],
                extra_toml="[autopilot]\nplanning_recheck_seconds = 10.0\n",
            )
            config = load_config(repo)
            launcher, launcher_calls = self._recording_launcher()
            sleeps: list[float] = []

            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=100.0,
                launcher=launcher,
                sleep=sleeps.append,
            )

        self.assertEqual(summary.cycles[0].status, "idle")
        # No runnable task ever appears, so the recheck loop sleeps the full
        # interval in recheck-sized slices, then the next cycle begins.
        self.assertEqual(sleeps, [10.0] * 10)
        self.assertEqual(len(launcher_calls), 0)

    def test_dispatched_cycle_sleeps_plain_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready scope")],
                extra_toml="[autopilot]\nplanning_recheck_seconds = 10.0\n",
            )
            config = load_config(repo)
            launcher, launcher_calls = self._recording_launcher()
            sleeps: list[float] = []

            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=100.0,
                launcher=launcher,
                sleep=sleeps.append,
            )

        self.assertEqual(summary.cycles[0].status, "completed")
        # Enough work remains after the fresh post-dispatch poll, so the cycle
        # keeps the single full-interval sleep.
        self.assertEqual(sleeps, [100.0])
        self.assertEqual(len(launcher_calls), 2)

    def test_drained_dispatched_cycle_reaches_planning_after_one_recheck(self) -> None:
        for exit_code, expected_status in (
            (0, "completed"),
            (1, "restartable"),
            (-15, "terminated"),
        ):
            with (
                self.subTest(exit_code=exit_code),
                tempfile.TemporaryDirectory() as directory,
            ):
                repo = Path(directory)
                configured_repo(
                    repo,
                    [("TASK-01", "Next", "", "ready scope")],
                    extra_toml=(
                        "[autopilot]\n"
                        "planning_recheck_seconds = 10.0\n"
                        "require_clean_repo = false\n"
                        'planning_command = "plan"\n'
                    ),
                )
                config = load_config(repo)
                runner, kinds = self._planning_runner()
                sleeps: list[float] = []
                launcher_calls = 0

                def draining_launcher(command, *, cwd, log_path, on_start=None):
                    nonlocal launcher_calls
                    launcher_calls += 1
                    if on_start is not None:
                        on_start(4242)
                    write_plan(repo, [("TASK-01", "Done", "", "finished scope")])
                    return exit_code

                summary = run_autopilot(
                    config,
                    max_cycles=2,
                    interval=100.0,
                    launcher=draining_launcher,
                    maintenance_runner=runner,
                    sleep=sleeps.append,
                )

            self.assertEqual(
                [cycle.status for cycle in summary.cycles], [expected_status, "idle"]
            )
            self.assertIn("post_cycle_runnable:0/1", summary.cycles[0].actions)
            self.assertEqual(sleeps, [10.0])
            self.assertEqual(kinds, ["planning"])
            self.assertEqual(launcher_calls, 1)

    def test_limit_wall_pause_takes_precedence_over_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-00", "Next", "MISSING", "blocked scope")],
                extra_toml=(
                    "[autopilot]\n"
                    "planning_recheck_seconds = 10.0\n"
                    "[supervision]\n"
                    "limit_wall_backoff_seconds = 42\n"
                ),
            )
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")
            run_store.append_result(self._future_limit_wall_result(repo))
            launcher, _launcher_calls = self._recording_launcher()
            sleeps: list[float] = []

            summary = run_autopilot(
                config,
                max_cycles=2,
                interval=100.0,
                launcher=launcher,
                sleep=sleeps.append,
            )

        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertEqual(summary.cycles[0].limit_wall_pause_seconds, 42.0)
        # Even though the cycle is idle, the limit-wall backoff replaces the
        # recheck loop entirely; the recheck slice size never appears.
        self.assertEqual(sleeps, [42.0])


class AutopilotMaintenanceTests(unittest.TestCase):
    def _stub_runner(self, exit_codes: dict[str, int | None]):
        calls: list[dict[str, object]] = []

        def runner(
            command, kind, cycle_id, *, cwd, env_extra, timeout, max_output_bytes
        ):
            calls.append(
                {"command": command, "kind": kind, "env_extra": dict(env_extra)}
            )
            return MaintenanceCommandResult(
                kind=kind,
                cycle_id=cycle_id,
                exit_code=exit_codes.get(kind, 0),
                duration_seconds=0.0,
                output=f"{kind}-output",
                output_truncated=False,
                timed_out=False,
            )

        return runner, calls

    def _command_records(self, config) -> list[dict[str, object]]:
        records = RunStore(config.state_path / "runs.jsonl").read_records()
        return [
            record
            for record in records
            if record["record_type"] == AUTOPILOT_COMMAND_RESULT_RECORD_TYPE
        ]

    def test_low_ready_runs_planning_command_and_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml='[autopilot]\nplanning_command = "plan"\n',
            )
            config = load_config(repo)
            runner, calls = self._stub_runner({})
            launcher_calls: list[object] = []

            def launcher(command, *, cwd, log_path, on_start=None):
                launcher_calls.append(command)
                return 0

            summary = run_autopilot(
                config,
                once=True,
                min_ready=2,
                launcher=launcher,
                maintenance_runner=runner,
            )
            command_records = self._command_records(config)

        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertEqual(len(launcher_calls), 0)
        self.assertIn("ran_planning_command:exit=0", summary.cycles[0].actions)
        self.assertEqual([call["kind"] for call in calls], ["planning"])
        self.assertEqual(
            calls[0]["env_extra"]["VIBE_LOOP_AUTOPILOT_COMMAND_KIND"], "planning"
        )
        self.assertEqual([record["kind"] for record in command_records], ["planning"])

    def test_low_ready_without_planning_reports_low_runnable_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)
            runner, calls = self._stub_runner({})

            summary = run_autopilot(
                config,
                once=True,
                min_ready=2,
                launcher=lambda *a, **k: 0,
                maintenance_runner=runner,
            )

        self.assertEqual(summary.cycles[0].status, "idle")
        self.assertIn("planning_unconfigured", summary.cycles[0].actions)
        self.assertIn("low_runnable_work:1/2", summary.cycles[0].actions)
        self.assertEqual(calls, [])

    def test_failed_health_command_blocks_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml='[autopilot]\nhealth_command = "health"\n',
            )
            config = load_config(repo)
            runner, calls = self._stub_runner({"health": 1})
            launched: list[object] = []

            summary = run_autopilot(
                config,
                once=True,
                launcher=lambda command, **k: launched.append(command) or 0,
                maintenance_runner=runner,
            )

        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "blocked")
        self.assertIn("autopilot_health_failed", cycle.blockers)
        self.assertEqual(len(launched), 0)
        self.assertEqual([call["kind"] for call in calls], ["health"])

    def test_summary_and_troubleshoot_fire_around_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml=(
                    "[autopilot]\n"
                    'summary_command = "summary"\n'
                    'troubleshoot_command = "troubleshoot"\n'
                ),
            )
            config = load_config(repo)
            runner, calls = self._stub_runner({})

            summary = run_autopilot(
                config,
                once=True,
                launcher=lambda *a, **k: 1,
                maintenance_runner=runner,
            )

        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "restartable")
        self.assertEqual([call["kind"] for call in calls], ["summary", "troubleshoot"])

    def test_summary_fires_but_troubleshoot_skipped_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml=(
                    "[autopilot]\n"
                    'summary_command = "summary"\n'
                    'troubleshoot_command = "troubleshoot"\n'
                ),
            )
            config = load_config(repo)
            runner, calls = self._stub_runner({})

            run_autopilot(
                config,
                once=True,
                launcher=lambda *a, **k: 0,
                maintenance_runner=runner,
            )

        self.assertEqual([call["kind"] for call in calls], ["summary"])

    def test_require_clean_repo_false_allows_launch_when_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(
                repo,
                [("TASK-01", "Next", "", "ready slice")],
                extra_toml="[autopilot]\nrequire_clean_repo = false\n",
            )
            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            run(repo, "git", "add", "tracked.txt")
            config = load_config(repo)
            launched: list[object] = []
            runner, _calls = self._stub_runner({})

            summary = run_autopilot(
                config,
                once=True,
                launcher=lambda command, **k: launched.append(command) or 0,
                maintenance_runner=runner,
            )

        cycle = summary.cycles[0]
        self.assertEqual(cycle.status, "completed")
        self.assertNotIn("repo_dirty", cycle.blockers)
        self.assertIn("repo_dirty_ignored", cycle.actions)
        self.assertEqual(len(launched), 1)

    def test_run_maintenance_command_bounds_output_and_timeout(self) -> None:
        # Commands run via shell=True on every platform, so use python -c
        # one-liners that behave identically under sh and cmd.exe.
        python = f'"{sys.executable}"'
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            # os.write + os._exit keeps the write-to-exit window tiny so the
            # harness observes a completed process with oversized output (the
            # post-completion truncation path) instead of killing a still
            # running flood (the size_exceeded path).
            ok = run_maintenance_command(
                f"{python} -c \"import os; os.write(1, b'abcdef'); os._exit(0)\"",
                "summary",
                "cycle-1",
                cwd=repo,
                env_extra={},
                timeout=10.0,
                max_output_bytes=3,
            )
            failed = run_maintenance_command(
                f'{python} -c "raise SystemExit(7)"',
                "health",
                "cycle-1",
                cwd=repo,
                env_extra={},
                timeout=10.0,
                max_output_bytes=1024,
            )
            timed = run_maintenance_command(
                f'{python} -c "import time; time.sleep(5)"',
                "troubleshoot",
                "cycle-1",
                cwd=repo,
                env_extra={},
                timeout=0.2,
                max_output_bytes=1024,
            )

        self.assertEqual(ok.exit_code, 0)
        self.assertEqual(ok.output, "abc")
        self.assertTrue(ok.output_truncated)
        self.assertEqual(failed.exit_code, 7)
        self.assertFalse(failed.succeeded)
        self.assertIsNone(timed.exit_code)
        self.assertTrue(timed.timed_out)


class AutopilotRegistryTests(unittest.TestCase):
    def test_register_list_find_remove_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "projects.json"

            empty = ProjectRegistry.load(registry_path)
            self.assertEqual(empty.entries, ())

            registry = empty.with_entry(
                ProjectEntry(name="alpha", repo=Path("/repos/alpha"))
            ).with_entry(ProjectEntry(name="beta", repo=Path("/repos/beta")))
            registry.save()

            reloaded = ProjectRegistry.load(registry_path)
            self.assertEqual(
                [entry.name for entry in reloaded.entries], ["alpha", "beta"]
            )
            self.assertEqual(reloaded.find("beta").repo, Path("/repos/beta"))
            self.assertEqual(reloaded.find(str(Path("/repos/alpha"))).name, "alpha")
            self.assertIsNone(reloaded.find("missing"))

            # Re-registering a name replaces the prior entry rather than duplicating.
            updated = reloaded.with_entry(
                ProjectEntry(name="alpha", repo=Path("/repos/alpha2"))
            )
            self.assertEqual(len(updated.entries), 2)
            self.assertEqual(updated.find("alpha").repo, Path("/repos/alpha2"))

            without_beta, removed = updated.without("beta")
            self.assertTrue(removed)
            self.assertEqual([entry.name for entry in without_beta.entries], ["alpha"])
            _, removed_missing = without_beta.without("missing")
            self.assertFalse(removed_missing)

    def test_registry_context_roundtrips_without_public_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "projects.json"
            secret_marker = "selector-value-not-for-status"
            registry = ProjectRegistry(
                path=registry_path,
                entries=(
                    ProjectEntry(
                        name="alpha",
                        repo=Path("/repos/alpha"),
                        runtime_context=(("LOOPYARD_PROJECT", secret_marker),),
                    ),
                ),
            )
            registry.save()

            persisted = registry_path.read_text(encoding="utf-8")
            reloaded = ProjectRegistry.load(registry_path)

        self.assertIn(secret_marker, persisted)
        self.assertEqual(
            dict(reloaded.entries[0].runtime_context),
            {"LOOPYARD_PROJECT": secret_marker},
        )
        self.assertNotIn(secret_marker, json.dumps(reloaded.entries[0].to_json()))

    def test_registry_context_rejects_duplicate_assignments_consistently(self) -> None:
        duplicates = (
            (("PROJECT_SELECTOR", "first"), ("PROJECT_SELECTOR", "second")),
            (("PROJECT_SELECTOR", "first"), ("project_selector", "second")),
        )
        for runtime_context in duplicates:
            with self.subTest(runtime_context=runtime_context):
                with self.assertRaises(ValueError) as caught:
                    ProjectEntry(
                        name="alpha",
                        repo=Path("/repos/alpha"),
                        runtime_context=runtime_context,
                    )
            self.assertNotIn("first", str(caught.exception))
            self.assertNotIn("second", str(caught.exception))

    def test_registry_rejects_malformed_or_prohibited_context(self) -> None:
        invalid_contexts = (
            None,
            ["LOOPYARD_PROJECT=vibe-loop"],
            {"API_TOKEN": "must-not-appear-in-error"},
            {"LD_PRELOAD": "/tmp/library.so"},
        )
        for context in invalid_contexts:
            with self.subTest(context_type=type(context).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    registry_path = Path(directory) / "projects.json"
                    registry_path.write_text(
                        json.dumps(
                            {
                                "projects": [
                                    {
                                        "name": "alpha",
                                        "repo": "/repos/alpha",
                                        "context": context,
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError) as caught:
                        ProjectRegistry.load(registry_path)

                self.assertNotIn("must-not-appear-in-error", str(caught.exception))

    def test_collect_registry_status_isolates_three_command_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            expected_queues = {}
            selectors = (
                ("alpha", "selector-one; printf not-shell"),
                ("beta", "selector-two"),
                ("gamma", "selector-three"),
            )
            for name, selector in selectors:
                repo = root / name
                repo.mkdir()
                init_repo(repo)
                queue_id = f"QUEUE-{name.upper()}"
                expected_queues[name] = queue_id
                (repo / "expected.json").write_text(
                    json.dumps({"selector": selector, "queue_id": queue_id}),
                    encoding="utf-8",
                )
                (repo / "adapter.py").write_text(
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "expected = json.loads(Path('expected.json').read_text())\n"
                    "if os.environ.get('PROJECT_SELECTOR') != expected['selector']:\n"
                    "    raise SystemExit(7)\n"
                    "if sys.argv[1] == 'tasks':\n"
                    "    print(json.dumps([{'id': expected['queue_id'], "
                    "'title': expected['selector'], 'status': 'ready', "
                    "'source': expected['selector']}]))\n"
                    "elif os.environ.get('VIBE_LOOP_LOCK_OPERATION') == 'list':\n"
                    "    print('[]')\n"
                    "else:\n"
                    "    print(json.dumps({'locked': False}))\n",
                    encoding="utf-8",
                )
                command = f"{sys.executable} adapter.py locks"
                (repo / ".vibe-loop.toml").write_text(
                    "[task_source]\n"
                    'type = "command"\n'
                    f"list = {json.dumps(f'{sys.executable} adapter.py tasks')}\n"
                    'runnable_statuses = ["ready"]\n'
                    "[locks]\n"
                    'type = "command"\n'
                    f"acquire_command = {json.dumps(command)}\n"
                    f"release_command = {json.dumps(command)}\n"
                    f"status_command = {json.dumps(command)}\n"
                    f"list_command = {json.dumps(command)}\n",
                    encoding="utf-8",
                )
                run(
                    repo,
                    "git",
                    "add",
                    "adapter.py",
                    "expected.json",
                    ".vibe-loop.toml",
                )
                run(repo, "git", "commit", "-m", "initial")
                entries.append(
                    ProjectEntry(
                        name=name,
                        repo=repo,
                        runtime_context=(("PROJECT_SELECTOR", selector),),
                    )
                )

            registry = ProjectRegistry(
                path=root / "projects.json",
                entries=tuple(entries),
            )
            with mock.patch.dict(os.environ, {"PROJECT_SELECTOR": "host-default"}):
                results = collect_registry_status(registry)
                inherited_after = os.environ["PROJECT_SELECTOR"]

        self.assertEqual(inherited_after, "host-default")
        self.assertEqual(
            [result.name for result in results], ["alpha", "beta", "gamma"]
        )
        for result, (_name, selector) in zip(results, selectors, strict=True):
            self.assertEqual(result.error, "")
            self.assertIsNotNone(result.status)
            self.assertEqual(
                result.status.queue.runnable_tasks[0]["id"],
                expected_queues[result.name],
            )
            self.assertNotIn(selector, json.dumps(result.status.to_json()))
        status_payload = json.dumps([result.to_json() for result in results])
        for _name, selector in selectors:
            self.assertNotIn(selector, status_payload)

    def test_registry_context_is_redacted_from_lock_adapter_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "project"
            repo.mkdir()
            init_repo(repo)
            write_plan(repo, [("TASK-01", "Next", "", "ready slice")])
            selector = "selector-value-must-not-leak"
            (repo / "adapter.py").write_text(
                "import os, sys\n"
                "sys.stderr.write(os.environ['PROJECT_SELECTOR'])\n"
                "raise SystemExit(3)\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} adapter.py"
            (repo / ".vibe-loop.toml").write_text(
                "[locks]\n"
                'type = "command"\n'
                f"acquire_command = {json.dumps(command)}\n"
                f"release_command = {json.dumps(command)}\n"
                f"status_command = {json.dumps(command)}\n"
                f"list_command = {json.dumps(command)}\n",
                encoding="utf-8",
            )
            run(repo, "git", "add", "PLAN.md", "adapter.py", ".vibe-loop.toml")
            run(repo, "git", "commit", "-m", "initial")
            registry = ProjectRegistry(
                path=root / "projects.json",
                entries=(
                    ProjectEntry(
                        name="project",
                        repo=repo,
                        runtime_context=(("PROJECT_SELECTOR", selector),),
                    ),
                ),
            )

            result = collect_registry_status(registry)[0]

        self.assertIsNone(result.status)
        self.assertIn("runtime-context-redacted", result.error)
        self.assertNotIn(selector, result.error)

    def test_collect_registry_status_aggregates_and_isolates_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good"
            good.mkdir()
            configured_repo(good, [("TASK-01", "Next", "", "ready slice")])
            broken = root / "broken"
            broken.mkdir()
            init_repo(broken)
            (broken / ".vibe-loop.toml").write_text(
                "[autopilot]\nunsupported = true\n", encoding="utf-8"
            )
            write_plan(broken, [("TASK-09", "Next", "", "ready slice")])
            run(broken, "git", "add", "PLAN.md", ".vibe-loop.toml")
            run(broken, "git", "commit", "-m", "initial")

            registry = ProjectRegistry(
                path=root / "projects.json",
                entries=(
                    ProjectEntry(name="good", repo=good),
                    ProjectEntry(name="broken", repo=broken),
                ),
            )
            results = collect_registry_status(registry)

        by_name = {result.name: result for result in results}
        self.assertEqual(len(results), 2)
        self.assertIsNotNone(by_name["good"].status)
        self.assertEqual(by_name["good"].error, "")
        self.assertEqual(by_name["good"].status.queue.runnable, 1)
        self.assertIsNone(by_name["broken"].status)
        self.assertIn("autopilot contains unsupported keys", by_name["broken"].error)


class AutopilotWaitTests(unittest.TestCase):
    def test_cycle_schedule_deadline_aligns_to_utc_buckets(self) -> None:
        iso, epoch = cycle_schedule_deadline(30.0, now=100.0)
        self.assertEqual(epoch, 120.0)
        self.assertTrue(iso.endswith("Z"))
        with self.assertRaises(ValueError):
            cycle_schedule_deadline(0.0, now=100.0)

    def test_parse_wait_deadline_handles_z_and_naive(self) -> None:
        self.assertEqual(
            parse_wait_deadline("2026-06-06T17:00:00Z"),
            parse_wait_deadline("2026-06-06T17:00:00+00:00"),
        )
        # Naive timestamps are treated as UTC.
        self.assertEqual(
            parse_wait_deadline("2026-06-06T17:00:00"),
            parse_wait_deadline("2026-06-06T17:00:00Z"),
        )

    def test_wakes_on_pid_exit_in_any_mode(self) -> None:
        sleeps: list[float] = []
        result = wait_for_processes(
            pids=[123],
            deadline_epoch=10_000.0,
            mode="any",
            process_exists=lambda pid: False,
            wallclock=lambda: 0.0,
            sleep=sleeps.append,
        )
        self.assertEqual(result.wake_reason, "pid")
        self.assertEqual(result.wake_summary, "pid_exit:123")
        self.assertEqual(sleeps, [])

    def test_wakes_on_deadline_when_process_stays_alive(self) -> None:
        clock = iter([0.0, 100.0])
        sleeps: list[float] = []
        result = wait_for_processes(
            pids=[123],
            deadline_epoch=50.0,
            deadline_text="2026-06-06T17:00:00Z",
            mode="any",
            interval=5.0,
            process_exists=lambda pid: True,
            wallclock=lambda: next(clock),
            sleep=sleeps.append,
        )
        self.assertEqual(result.wake_reason, "deadline")
        self.assertEqual(result.wake_summary, "deadline:2026-06-06T17:00:00Z")
        self.assertEqual(len(sleeps), 1)

    def test_all_mode_waits_for_every_pid(self) -> None:
        result = wait_for_processes(
            pids=[1, 2],
            deadline_epoch=10_000.0,
            mode="all",
            process_exists=lambda pid: False,
            wallclock=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.wake_reason, "all_complete")
        self.assertEqual(sorted(event["pid"] for event in result.events), [1, 2])

    def test_wakes_immediately_on_user_message(self) -> None:
        sleeps: list[float] = []
        result = wait_for_processes(
            pids=[123],
            deadline_epoch=10_000.0,
            process_exists=lambda _pid: True,
            wallclock=lambda: 0.0,
            sleep=sleeps.append,
            message_poller=lambda: {
                "kind": "user_message",
                "id": 7,
                "text": "change direction",
            },
            session_ref="run-7",
        )
        self.assertEqual(result.wake_reason, "message")
        self.assertEqual(result.wake_summary, "message:1")
        self.assertEqual(result.events[0]["text"], "change direction")
        self.assertEqual(result.session_ref, "run-7")
        self.assertEqual(sleeps, [])

    def test_empty_message_poll_sleeps_before_later_message(self) -> None:
        polls = iter(
            [
                None,
                {"kind": "user_message", "id": 8, "text": "continue"},
            ]
        )
        sleeps: list[float] = []
        result = wait_for_processes(
            pids=[123],
            deadline_epoch=10_000.0,
            interval=2.0,
            process_exists=lambda _pid: True,
            wallclock=lambda: 0.0,
            sleep=sleeps.append,
            message_poller=lambda: next(polls),
        )
        self.assertEqual(result.wake_reason, "message")
        self.assertEqual(sleeps, [2.0])

    def test_dead_pid_wins_without_polling_messages(self) -> None:
        polls: list[bool] = []
        result = wait_for_processes(
            pids=[123],
            deadline_epoch=10_000.0,
            process_exists=lambda _pid: False,
            wallclock=lambda: 0.0,
            sleep=lambda _seconds: None,
            message_poller=lambda: polls.append(True),
        )
        self.assertEqual(result.wake_reason, "pid")
        self.assertEqual(polls, [])

    def test_message_command_maps_valid_envelope_and_session_environment(self) -> None:
        completed = subprocess.CompletedProcess(
            args="adapter",
            returncode=0,
            stdout=(
                '{"received":true,"message":{"id":3,"content":"continue",'
                '"sender_name":"operator","created_at":"2026-07-18T00:00:00Z"}}'
            ),
            stderr="",
        )
        with mock.patch(
            "vibe_loop.autopilot.subprocess.run", return_value=completed
        ) as run:
            event = poll_wait_message_command(
                "adapter --json", session_ref="run;literal", timeout=3.0
            )
        self.assertEqual(
            event,
            {
                "kind": "user_message",
                "id": 3,
                "text": "continue",
                "at": "2026-07-18T00:00:00Z",
                "sender": "operator",
            },
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["VIBE_LOOP_WAIT_SESSION_REF"],
            "run;literal",
        )

    def test_message_command_rejects_invalid_schema(self) -> None:
        completed = subprocess.CompletedProcess(
            args="adapter",
            returncode=0,
            stdout='{"received":true,"message":{"id":3}}',
            stderr="",
        )
        with (
            mock.patch("vibe_loop.autopilot.subprocess.run", return_value=completed),
            self.assertRaises(WaitMessageAdapterError) as caught,
        ):
            poll_wait_message_command("adapter", session_ref="run-1", timeout=3.0)
        self.assertEqual(caught.exception.category, "invalid_schema")


class WorktreeDispositionCycleTests(unittest.TestCase):
    def _orphan_repo(
        self,
        directory: str,
        *,
        worktree_disposition: str | None = "reap",
    ) -> tuple[Path, Path]:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        configured_repo(
            repo,
            [("TASK-01", "Next", "", "ready slice")],
            extra_toml=(
                f'[autopilot]\nworktree_disposition = "{worktree_disposition}"\n'
                if worktree_disposition is not None
                else ""
            ),
        )
        worktree = root / "orphan"
        run(repo, "git", "worktree", "add", "-b", "orphan", str(worktree), "main")
        return repo, worktree

    def test_reaps_merged_clean_unclaimed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")
            removed: list[Path] = []
            deleted: list[str] = []
            prompts: list[str] = []

            def analysis_runner(prompt, output_path):
                prompts.append(prompt)
                return {
                    "decisions": [
                        {
                            "worktree": str(worktree),
                            "action": "reap",
                            "reason": "orphan",
                        }
                    ]
                }

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=analysis_runner,
                remove_worktree=lambda path: removed.append(path) or "",
                delete_branch=lambda branch: deleted.append(branch) or "",
            )

        self.assertTrue(result.agent_invoked)
        self.assertEqual(result.agent_error, "")
        self.assertEqual(result.reaped, 1)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.status, "ok")
        self.assertEqual([path.resolve() for path in removed], [worktree.resolve()])
        self.assertEqual(deleted, ["orphan"])
        self.assertEqual(len(prompts), 1)
        record = result.to_record(config.repo)
        self.assertEqual(record["record_type"], AUTOPILOT_WORKTREE_REAP_RECORD_TYPE)
        self.assertEqual(record["policy"], "reap")
        self.assertEqual(record["candidates"], 1)
        self.assertEqual(record["reaped"], 1)
        self.assertEqual(record["status"], "ok")

    def test_report_only_default_journals_candidate_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(
                directory,
                worktree_disposition=None,
            )
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=lambda prompt, output_path: (_ for _ in ()).throw(
                    AssertionError("report-only policy must not invoke the agent")
                ),
                remove_worktree=lambda path: (_ for _ in ()).throw(
                    AssertionError("report-only policy must not remove worktrees")
                ),
                delete_branch=lambda branch: (_ for _ in ()).throw(
                    AssertionError("report-only policy must not delete branches")
                ),
            )

        self.assertFalse(result.agent_invoked)
        self.assertEqual(result.policy, "report-only")
        self.assertEqual(result.candidates, 1)
        self.assertEqual(result.reaped, 0)
        candidate = next(
            outcome
            for outcome in result.outcomes
            if outcome.worktree.resolve() == worktree.resolve()
        )
        self.assertEqual(candidate.requested, "keep")
        self.assertEqual(candidate.applied, "kept")
        self.assertEqual(
            candidate.reason,
            "worktree disposition policy is report-only",
        )
        record = result.to_record(config.repo)
        self.assertEqual(record["policy"], "report-only")
        self.assertEqual(record["candidates"], 1)
        self.assertEqual(record["reaped"], 0)

    def test_failed_git_removal_reports_errors_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            def analysis_runner(prompt, output_path):
                return {
                    "decisions": [
                        {
                            "worktree": str(worktree),
                            "action": "reap",
                            "reason": "orphan",
                        }
                    ]
                }

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=analysis_runner,
                remove_worktree=lambda path: "git worktree remove failed",
                delete_branch=lambda branch: "",
            )

        self.assertTrue(result.agent_invoked)
        self.assertEqual(result.reaped, 0)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.status, "errors")

    def test_keeps_unmerged_worktree_without_invoking_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
            run(worktree, "git", "add", "wip.txt")
            run(worktree, "git", "commit", "-m", "wip")
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")
            calls: list[str] = []

            def analysis_runner(prompt, output_path):
                calls.append(prompt)
                return {"decisions": []}

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=analysis_runner,
                remove_worktree=lambda path: "",
                delete_branch=lambda branch: "",
            )

        self.assertFalse(result.agent_invoked)
        self.assertEqual(calls, [])
        self.assertEqual(result.reaped, 0)
        self.assertEqual(result.status, "ok")

    def test_keeps_dirty_worktree_without_invoking_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            def analysis_runner(prompt, output_path):
                raise AssertionError("agent must not run for guarded worktrees")

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=analysis_runner,
                remove_worktree=lambda path: "",
                delete_branch=lambda branch: "",
            )

        self.assertFalse(result.agent_invoked)
        self.assertEqual(result.reaped, 0)

    def test_unavailable_agent_keeps_reapable_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=lambda prompt, output_path: None,
                remove_worktree=lambda path: "",
                delete_branch=lambda branch: "",
            )

        self.assertTrue(result.agent_invoked)
        self.assertNotEqual(result.agent_error, "")
        self.assertEqual(result.reaped, 0)
        self.assertEqual(result.status, "agent_error")

    def test_reap_policy_rejects_agent_decision_without_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree = self._orphan_repo(directory)
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=lambda prompt, output_path: {
                    "decisions": [
                        {
                            "worktree": str(worktree),
                            "action": "reap",
                        }
                    ]
                },
                remove_worktree=lambda path: (_ for _ in ()).throw(
                    AssertionError("unreasoned decision must not remove worktrees")
                ),
                delete_branch=lambda branch: (_ for _ in ()).throw(
                    AssertionError("unreasoned decision must not delete branches")
                ),
            )

        self.assertTrue(result.agent_invoked)
        self.assertEqual(
            result.agent_error,
            "analysis agent returned an invalid or unreasoned decision",
        )
        self.assertEqual(result.reaped, 0)
        candidate = next(
            outcome
            for outcome in result.outcomes
            if outcome.worktree.resolve() == worktree.resolve()
        )
        self.assertEqual(candidate.requested, "keep")
        self.assertEqual(candidate.applied, "kept")
        self.assertEqual(
            candidate.reason,
            "analysis disposition response was rejected",
        )

    def test_reap_policy_rejects_partial_multi_candidate_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, first_worktree = self._orphan_repo(directory)
            second_worktree = Path(directory) / "orphan-2"
            run(
                repo,
                "git",
                "worktree",
                "add",
                "-b",
                "orphan-2",
                str(second_worktree),
                "main",
            )
            config = load_config(repo)
            run_store = RunStore(config.state_path / "runs.jsonl")

            result = run_worktree_disposition(
                config,
                cycle_id="c1",
                run_store=run_store,
                process_exists=lambda pid: False,
                analysis_runner=lambda prompt, output_path: {
                    "decisions": [
                        {
                            "worktree": str(first_worktree),
                            "action": "reap",
                            "reason": "orphan",
                        }
                    ]
                },
                remove_worktree=lambda path: (_ for _ in ()).throw(
                    AssertionError("partial response must not remove worktrees")
                ),
                delete_branch=lambda branch: (_ for _ in ()).throw(
                    AssertionError("partial response must not delete branches")
                ),
            )

        self.assertEqual(result.candidates, 2)
        self.assertEqual(
            result.agent_error,
            "analysis agent must return exactly one reasoned disposition decision "
            "per candidate",
        )
        self.assertEqual(result.reaped, 0)
        candidate_outcomes = [
            outcome
            for outcome in result.outcomes
            if outcome.worktree.resolve()
            in {first_worktree.resolve(), second_worktree.resolve()}
        ]
        self.assertEqual(len(candidate_outcomes), 2)
        self.assertTrue(
            all(outcome.requested == "keep" for outcome in candidate_outcomes)
        )
        self.assertTrue(
            all(
                outcome.reason == "analysis disposition response was rejected"
                for outcome in candidate_outcomes
            )
        )

    def test_cycle_records_disposition_and_appends_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            configured_repo(repo, [("TASK-01", "Next", "", "ready slice")])
            config = load_config(repo)

            summary = run_autopilot(
                config,
                once=True,
                launcher=lambda *a, **k: 0,
            )
            records = RunStore(config.state_path / "runs.jsonl").read_records()

        cycle = summary.cycles[0]
        self.assertIn("worktree_disposition_policy:report-only", cycle.actions)
        self.assertIn("worktree_disposition_candidates:0", cycle.actions)
        self.assertIn("reaped_worktrees:0", cycle.actions)
        self.assertNotIn("worktree_disposition_agent_error", cycle.actions)
        reap_records = [
            record
            for record in records
            if record["record_type"] == AUTOPILOT_WORKTREE_REAP_RECORD_TYPE
        ]
        self.assertEqual(len(reap_records), 1)
        self.assertEqual(reap_records[0]["reaped"], 0)
        self.assertEqual(reap_records[0]["policy"], "report-only")
        self.assertFalse(reap_records[0]["agent_invoked"])


def configured_repo(
    repo: Path,
    rows: list[tuple[str, str, str, str]],
    *,
    extra_toml: str = "",
) -> None:
    init_repo(repo)
    (repo / ".vibe-loop.toml").write_text(
        '[agent]\ncommand = "codex exec {prompt}"\n' + extra_toml,
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
