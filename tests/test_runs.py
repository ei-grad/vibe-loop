from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_loop.runs import RUN_RECORD_TYPE, RUN_SCHEMA_VERSION, RunResult, RunStore


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
                        f'{{"task_id":"TASK-03","log":"{directory}"}}',
                    ]
                ),
                encoding="utf-8",
            )

            context = RunStore(path).recent_log_context()

        self.assertIn("TASK-01", context)
        self.assertIn("TASK-02", context)
        self.assertIn("TASK-03", context)
        self.assertNotIn("Log tail for", context)


if __name__ == "__main__":
    unittest.main()
