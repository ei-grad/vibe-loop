from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_loop.runs import (
    RUN_RECORD_TYPE,
    RUN_SCHEMA_VERSION,
    WORKER_REPORT_RECORD_TYPE,
    WORKER_REPORT_SCHEMA_VERSION,
    RunResult,
    RunStore,
    WorkerReport,
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
                    agent_kind="claude",
                    agent_prompt_dialect="claude",
                    agent_prompt_dialect_source="agent.kind:claude",
                    agent_skill_ref_prefix="/",
                    agent_skill_ref_prefix_source="agent.kind:claude",
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

        self.assertEqual([run.run_id for run in runs], ["run-2", "run-1"])
        self.assertEqual(runs[0].status, "blocked")
        self.assertEqual(runs[0].record_type, "worker_report")
        self.assertIsNone(runs[0].exit_code)
        self.assertEqual(runs[1].status, "completed")
        self.assertEqual(runs[1].record_type, "run_result")
        self.assertEqual(runs[1].exit_code, 0)
        self.assertEqual(runs[1].record_count, 2)
        self.assertEqual(runs[1].agent_kind, "claude")
        self.assertEqual(runs[1].agent_prompt_dialect, "claude")
        self.assertEqual(runs[1].agent_prompt_dialect_source, "agent.kind:claude")
        self.assertEqual(runs[1].agent_skill_ref_prefix, "/")
        self.assertEqual(runs[1].agent_skill_ref_prefix_source, "agent.kind:claude")
        self.assertEqual(runs[1].worker_report["commit"], "abc123")

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

    def test_recent_records_ignore_invalid_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            path.write_text(
                'not json\n{"task_id":"TASK-01","log":"/tmp/missing.log"}\n',
                encoding="utf-8",
            )

            records = RunStore(path).recent_records()

        self.assertEqual([record["task_id"] for record in records], ["TASK-01"])

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
