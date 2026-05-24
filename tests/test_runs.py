from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_loop.runs import (
    LIFECYCLE_EVENT_SCHEMA_VERSION,
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_EXPIRED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    LIFECYCLE_STATES,
    RUN_RECORD_TYPE,
    RUN_SCHEMA_VERSION,
    RUN_STATE_TRANSITION_RECORD_TYPE,
    WORKSPACE_CLAIM_RECORD_TYPE,
    WORKSPACE_CLAIMED_EVENT_TYPE,
    WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
    WORKER_REPORT_RECORD_TYPE,
    WORKER_REPORT_SCHEMA_VERSION,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    WorkerReport,
    derive_run_lifecycle,
)


class RunStoreTests(unittest.TestCase):
    def test_run_result_json_uses_stable_finished_at(self) -> None:
        result = RunResult(
            run_id="run-1",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
        )

        first = result.to_json()
        second = result.to_json()

        self.assertEqual(first["session_id"], "run-1")
        self.assertEqual(first["session_id_source"], "fallback:run_id")
        self.assertEqual(first["finished_at"], second["finished_at"])

    def test_run_result_json_can_store_native_session_id(self) -> None:
        result = RunResult(
            run_id="run-1",
            session_id="native-session-1",
            session_id_source="native:stdout",
            agent_command_source="auto:codex",
            agent_selection_command_source="auto:codex",
            agent_default_policy_source="codex-first",
            agent_default_policy="Codex first.",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
        )

        payload = result.to_json()

        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["session_id"], "native-session-1")
        self.assertEqual(payload["session_id_source"], "native:stdout")
        self.assertEqual(payload["agent_command_source"], "auto:codex")
        self.assertEqual(payload["agent_selection_command_source"], "auto:codex")
        self.assertEqual(payload["agent_default_policy_source"], "codex-first")
        self.assertEqual(payload["agent_default_policy"], "Codex first.")

    def test_append_result_writes_versioned_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=Path(directory) / "run.log",
                start_main="aaa",
                end_main="bbb",
            )
            store = RunStore(path)

            store.append_result(result)

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(payload["record_type"], RUN_RECORD_TYPE)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["session_id"], "run-1")
        self.assertEqual(payload["session_id_source"], "fallback:run_id")
        self.assertEqual(payload["task_id"], "TASK-01")

    def test_append_result_uses_sidecar_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=Path(directory) / "run.log",
                start_main="aaa",
                end_main="bbb",
            )
            store = RunStore(path)

            store.append_result(result)

            lock_exists = path.with_name("runs.jsonl.lock").is_file()

        self.assertTrue(lock_exists)

    def test_append_report_writes_versioned_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            report = WorkerReport(
                run_id="run-1",
                task_id="TASK-01",
                status="blocked",
                commit="abc123",
                message="waiting on review",
                metadata={"reason": "external"},
            )
            store = RunStore(path)

            store.append_report(report)

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], WORKER_REPORT_SCHEMA_VERSION)
        self.assertEqual(payload["record_type"], WORKER_REPORT_RECORD_TYPE)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["commit"], "abc123")
        self.assertEqual(payload["metadata"], {"reason": "external"})

    def test_append_lifecycle_event_writes_versioned_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = RunStore(path)

            store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_ACQUIRED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="TASK-01",
                    lock_kind="task",
                    lock_path=Path(directory) / "TASK-01.lock",
                    payload={"resources": ["db"]},
                )
            )

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], LIFECYCLE_EVENT_SCHEMA_VERSION)
        self.assertEqual(payload["record_type"], LOCK_ACQUIRED_RECORD_TYPE)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["lock_kind"], "task")
        self.assertEqual(payload["resources"], ["db"])
        self.assertTrue(payload["occurred_at"])

    def test_lifecycle_event_rejects_unknown_type(self) -> None:
        with self.assertRaises(ValueError):
            RunLifecycleEvent(record_type="surprise", run_id="run-1")

    def test_lifecycle_event_rejects_payload_core_key_override(self) -> None:
        with self.assertRaises(ValueError):
            RunLifecycleEvent(
                record_type=LOCK_RELEASED_RECORD_TYPE,
                run_id="run-1",
                payload={"run_id": "other"},
            )

    def test_derive_run_lifecycle_uses_recorded_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            records = [
                RunLifecycleEvent.lock_event(
                    LOCK_ACQUIRED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="TASK-01",
                    lock_kind="task",
                    lock_path=repo / "TASK-01.lock",
                ).to_record(),
                RunLifecycleEvent.run_state_transition(
                    run_id="run-1",
                    task_id="TASK-01",
                    to_state="started",
                    reason="task_lock_acquired",
                ).to_record(),
                RunLifecycleEvent.run_state_transition(
                    run_id="run-1",
                    task_id="TASK-01",
                    from_state="started",
                    to_state="session_observed",
                    reason="native:stdout",
                    payload={"session_id": "native-1"},
                ).to_record(),
                {
                    "schema_version": 1,
                    "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
                    "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "occurred_at": "2026-05-09T00:00:20+00:00",
                    "branch": "worker/TASK-01",
                    "worktree": str(repo),
                },
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                    reported_at="2026-05-09T00:00:30+00:00",
                ).to_record(),
                RunLifecycleEvent.run_state_transition(
                    run_id="run-1",
                    task_id="TASK-01",
                    from_state="session_observed",
                    to_state="classified",
                    reason="worker_report",
                ).to_record(),
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / "run-1.log",
                    start_main="aaa",
                    end_main="bbb",
                    finished_at="2026-05-09T00:01:00+00:00",
                ).to_record(),
            ]

            progress = derive_run_lifecycle(records)
            payload = progress.to_json()

        self.assertEqual(progress.state, "finalized")
        self.assertEqual(
            [transition["state"] for transition in payload["lifecycle_transitions"]],
            list(LIFECYCLE_STATES),
        )
        self.assertEqual(payload["missing_lifecycle_transitions"], [])
        self.assertTrue(
            all(
                transition["observed"]
                for transition in payload["lifecycle_transitions"]
            )
        )
        by_state = {
            transition["state"]: transition
            for transition in payload["lifecycle_transitions"]
        }
        self.assertEqual(
            by_state["scheduled"]["record_type"], LOCK_ACQUIRED_RECORD_TYPE
        )
        self.assertEqual(by_state["reported"]["record_type"], WORKER_REPORT_RECORD_TYPE)
        self.assertEqual(by_state["finalized"]["record_type"], RUN_RECORD_TYPE)

    def test_derive_run_lifecycle_keeps_missing_transitions_visible(self) -> None:
        progress = derive_run_lifecycle(
            [
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="blocked",
                    reported_at="2026-05-09T00:00:30+00:00",
                ).to_record()
            ]
        )
        payload = progress.to_json()
        by_state = {
            transition["state"]: transition
            for transition in payload["lifecycle_transitions"]
        }

        self.assertEqual(progress.state, "reported")
        self.assertTrue(by_state["reported"]["observed"])
        self.assertFalse(by_state["scheduled"]["observed"])
        self.assertFalse(by_state["finalized"]["observed"])
        self.assertIn("scheduled", payload["missing_lifecycle_transitions"])
        self.assertIn("finalized", payload["missing_lifecycle_transitions"])

    def test_latest_worker_report_uses_latest_matching_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="blocked",
                    message="first",
                )
            )
            store.append_record(
                {
                    "record_type": WORKER_REPORT_RECORD_TYPE,
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "status": "not-a-status",
                }
            )
            store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                    message="second",
                )
            )

            report = store.latest_worker_report("run-1", "TASK-01")

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.message, "second")

    def test_list_runs_groups_records_by_run_and_uses_latest_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_ACQUIRED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="TASK-01",
                    lock_kind="task",
                    lock_path=repo / "TASK-01.lock",
                )
            )
            store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                    commit="abc123",
                )
            )
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                    start_main="aaa",
                    end_main="bbb",
                    worker_report={
                        "run_id": "run-1",
                        "task_id": "TASK-01",
                        "status": "completed",
                        "commit": "abc123",
                        "message": "",
                        "metadata": {},
                        "reported_at": "2026-05-09T00:00:00+00:00",
                    },
                )
            )
            store.append_report(
                WorkerReport(
                    run_id="run-2",
                    task_id="TASK-02",
                    status="blocked",
                    message="waiting on dependency",
                )
            )

            runs = store.list_runs()
            inspection = store.inspect_run("run-1")

        self.assertEqual([run.run_id for run in runs], ["run-2", "run-1"])
        self.assertEqual(runs[0].status, "blocked")
        self.assertEqual(runs[0].record_type, "worker_report")
        self.assertIsNone(runs[0].exit_code)
        self.assertEqual(runs[1].status, "completed")
        self.assertEqual(runs[1].record_type, "run_result")
        self.assertEqual(runs[1].exit_code, 0)
        self.assertEqual(runs[1].record_count, 3)
        self.assertEqual(runs[1].worker_report["commit"], "abc123")
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(
            [record["record_type"] for record in inspection.records],
            ["lock_acquired", "worker_report", "run_result"],
        )

    def test_list_runs_limit_zero_returns_no_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                )
            )

            runs = store.list_runs(limit=0)

        self.assertEqual(runs, [])

    def test_list_runs_orders_by_displayed_status_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / "run-1.log",
                    start_main="aaa",
                    end_main="bbb",
                )
            )
            store.append_report(
                WorkerReport(
                    run_id="run-2",
                    task_id="TASK-02",
                    status="blocked",
                    message="waiting",
                )
            )
            store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_RELEASED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="TASK-01",
                    lock_kind="task",
                    lock_path=repo / "TASK-01.lock",
                )
            )

            runs = store.list_runs()
            inspection = store.inspect_run("run-1")

        self.assertEqual([run.run_id for run in runs], ["run-2", "run-1"])
        self.assertEqual(runs[1].record_type, RUN_RECORD_TYPE)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.view.record_count, 2)

    def test_list_runs_ignores_invalid_worker_reports_for_latest_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                    start_main="aaa",
                    end_main="bbb",
                )
            )
            store.append_record(
                {
                    "record_type": WORKER_REPORT_RECORD_TYPE,
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "status": "not-valid",
                    "reported_at": "2026-05-09T00:02:00+00:00",
                }
            )

            runs = store.list_runs()
            inspection = store.inspect_run("run-1")

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "completed")
        self.assertEqual(runs[0].record_type, "run_result")
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.view.status, "completed")
        self.assertEqual(inspection.view.record_type, "run_result")
        self.assertEqual(inspection.view.record_count, 2)

    def test_inspect_run_returns_records_for_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_report(
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                )
            )
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / ".vibe-loop" / "runs" / "run-1.log",
                    start_main="aaa",
                    end_main="bbb",
                )
            )
            store.append_report(
                WorkerReport(
                    run_id="run-2",
                    task_id="TASK-02",
                    status="blocked",
                )
            )

            inspection = store.inspect_run("run-1")

        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.view.run_id, "run-1")
        self.assertEqual(inspection.view.record_count, 2)
        self.assertEqual(
            [record["record_type"] for record in inspection.records],
            ["worker_report", "run_result"],
        )
        self.assertIsNone(store.inspect_run("missing-run"))

    def test_recent_log_context_reads_records_and_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            log_path = repo / "run.log"
            log_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
            store = RunStore(repo / "runs.jsonl")
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="failed",
                    exit_code=1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                )
            )

            context = store.recent_log_context(max_runs=1, tail_lines=2)

        self.assertIn("TASK-01", context)
        self.assertNotIn("first", context)
        self.assertIn("second", context)
        self.assertIn("third", context)

    def test_recent_log_context_counts_run_results_not_report_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            for index in range(1, 4):
                task_id = f"TASK-0{index}"
                run_id = f"run-{index}"
                log_path = repo / f"{run_id}.log"
                log_path.write_text(f"log {index}\n", encoding="utf-8")
                store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="completed",
                    )
                )
                store.append_result(
                    RunResult(
                        run_id=run_id,
                        task_id=task_id,
                        classification="completed",
                        exit_code=0,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="bbb",
                    )
                )

            context = store.recent_log_context(max_runs=2, tail_lines=1)

        self.assertNotIn("TASK-01", context)
        self.assertIn("TASK-02", context)
        self.assertIn("TASK-03", context)
        self.assertIn("log 2", context)
        self.assertIn("log 3", context)

    def test_read_records_ignore_invalid_json_lines_and_unknown_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            path.write_text(
                "\n".join(
                    [
                        "not json",
                        '{"record_type":"future_record","task_id":"SKIP"}',
                        '{"task_id":"TASK-01","log":"/tmp/missing.log"}',
                        json.dumps(
                            RunLifecycleEvent.workspace_claim_mismatch(
                                run_id="run-1",
                                task_id="TASK-02",
                                reason="branch_worktree_mismatch",
                                message="workspace claim refused",
                            ).to_record()
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = RunStore(path).recent_records()

        self.assertEqual(
            [record["task_id"] for record in records], ["TASK-01", "TASK-02"]
        )
        self.assertEqual(
            records[1]["record_type"], WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE
        )

    def test_inspect_run_can_show_lifecycle_only_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_EXPIRED_RECORD_TYPE,
                    run_id="run-1",
                    task_id="TASK-01",
                    lock_kind="task",
                    lock_path=repo / "TASK-01.lock",
                    payload={"stale_reason": "missing_process"},
                )
            )
            store.append_lifecycle_event(
                RunLifecycleEvent.run_state_transition(
                    run_id="run-1",
                    task_id="TASK-01",
                    to_state="classified",
                    reason="worker_report",
                )
            )

            inspection = store.inspect_run("run-1")

        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.view.record_type, RUN_STATE_TRANSITION_RECORD_TYPE)
        self.assertEqual(inspection.view.status, "classified")
        self.assertEqual(inspection.view.record_count, 2)

    def test_recent_log_context_ignores_records_without_file_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            path.write_text(
                "\n".join(
                    [
                        '{"record_type":"active","task_id":"TASK-01"}',
                        '{"record_type":"run_result","task_id":"TASK-02","log":""}',
                        json.dumps({"task_id": "TASK-03", "log": directory}),
                    ]
                ),
                encoding="utf-8",
            )

            context = RunStore(path).recent_log_context()

        self.assertNotIn("TASK-01", context)
        self.assertIn("TASK-02", context)
        self.assertIn("TASK-03", context)
        self.assertNotIn("Log tail for", context)


if __name__ == "__main__":
    unittest.main()
