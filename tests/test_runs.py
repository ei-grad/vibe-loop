from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_loop.runs import (
    AGENT_CONTEXT_OBSERVED_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
    AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
    AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
    AUTOPILOT_RECORD_TYPES,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    KNOWN_RECORD_TYPES,
    LIFECYCLE_EVENT_SCHEMA_VERSION,
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_EXPIRED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    LIFECYCLE_STATES,
    RUN_RECORD_TYPE,
    RUN_SCHEMA_VERSION,
    RUN_STARTED_RECORD_TYPE,
    RUN_STATE_TRANSITION_RECORD_TYPE,
    TASK_RECOVERY_RECORD_TYPE,
    TASK_RESTART_RECORD_TYPE,
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
            started_at="2026-05-09T00:00:00+00:00",
        )

        first = result.to_json()
        second = result.to_json()

        self.assertEqual(first["session_id"], "run-1")
        self.assertEqual(first["session_id_source"], "fallback:run_id")
        self.assertEqual(first["started_at"], "2026-05-09T00:00:00+00:00")
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

    def test_run_result_json_records_observed_transcript_path(self) -> None:
        result = RunResult(
            run_id="run-1",
            session_id="session-uuid",
            session_id_source="observed",
            transcript_path="/work/u/.claude/projects/p/session-uuid.jsonl",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
        )

        payload = result.to_json()

        self.assertEqual(payload["session_id"], "session-uuid")
        self.assertEqual(payload["session_id_source"], "observed")
        self.assertEqual(
            payload["transcript_path"],
            "/work/u/.claude/projects/p/session-uuid.jsonl",
        )

    def test_run_result_json_omits_empty_transcript_path(self) -> None:
        result = RunResult(
            run_id="run-1",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
        )

        self.assertNotIn("transcript_path", result.to_json())

    def test_run_history_view_surfaces_transcript_path(self) -> None:
        from vibe_loop.runs import RunHistoryView

        records = [
            {
                "record_type": RUN_STARTED_RECORD_TYPE,
                "run_id": "run-1",
                "task_id": "TASK-01",
                "session_id": "session-uuid",
                "session_id_source": "observed",
                "transcript_path": "/work/u/.claude/projects/p/session-uuid.jsonl",
            },
            {
                "record_type": RUN_RECORD_TYPE,
                "run_id": "run-1",
                "task_id": "TASK-01",
                "status": "completed",
                "session_id": "session-uuid",
                "session_id_source": "observed",
                "transcript_path": "/work/u/.claude/projects/p/session-uuid.jsonl",
            },
        ]

        view = RunHistoryView.from_records("run-1", records)

        self.assertEqual(view.session_id_source, "observed")
        self.assertEqual(
            view.transcript_path,
            "/work/u/.claude/projects/p/session-uuid.jsonl",
        )
        self.assertEqual(
            view.to_json()["transcript_path"],
            "/work/u/.claude/projects/p/session-uuid.jsonl",
        )

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

    def test_append_record_redacts_nested_fencing_token_fields(self) -> None:
        expected_token = "persisted-expected-fencing-canary"
        actual_token = "persisted-actual-fencing-canary"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = RunStore(path)

            store.append_record(
                {
                    "schema_version": 1,
                    "record_type": WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "reason": "fencing_token_mismatch",
                    "details": {
                        "expected_token": expected_token,
                        "nested": {"actual_token": actual_token},
                        "lock_path": "/safe/lock/path",
                    },
                }
            )

            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertNotIn(expected_token, raw)
        self.assertNotIn(actual_token, raw)
        self.assertEqual(payload["details"]["expected_token"], "<redacted>")
        self.assertEqual(payload["details"]["nested"]["actual_token"], "<redacted>")
        self.assertEqual(payload["details"]["lock_path"], "/safe/lock/path")

    def test_run_started_event_writes_trailer_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = RunStore(path)

            store.append_lifecycle_event(
                RunLifecycleEvent.run_started(
                    run_id="run-1",
                    task_id="TASK-01",
                    payload={
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "session_id": "run-1",
                        "session_id_source": "fallback:run_id",
                        "agent_kind": "codex",
                        "model_provider": "openai",
                        "model_provider_source": "command_executable:codex",
                        "trailer_context": {
                            "plan_item_candidates": ["TASK-01"],
                            "run_id": "run-1",
                            "session_id": "run-1",
                        },
                        "trailer_context_sources": {
                            "plan_item_candidates": "task_id",
                            "session_id": "fallback:run_id",
                        },
                    },
                )
            )

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], LIFECYCLE_EVENT_SCHEMA_VERSION)
        self.assertEqual(payload["record_type"], RUN_STARTED_RECORD_TYPE)
        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(payload["model_provider"], "openai")
        self.assertEqual(
            payload["trailer_context"]["plan_item_candidates"], ["TASK-01"]
        )

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
                RunLifecycleEvent.run_started(
                    run_id="run-1",
                    task_id="TASK-01",
                    payload={"started_at": "2026-05-09T00:00:10+00:00"},
                ).to_record(),
                RunLifecycleEvent.agent_context_observed(
                    run_id="run-1",
                    task_id="TASK-01",
                    payload={
                        "started_at": "2026-05-09T00:00:10+00:00",
                        "model_id": "gpt-5.5",
                        "model_id_source": "native:stdout:json.model",
                    },
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

        self.assertEqual(records[2]["record_type"], AGENT_CONTEXT_OBSERVED_RECORD_TYPE)
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
        self.assertEqual(by_state["started"]["record_type"], RUN_STARTED_RECORD_TYPE)
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

    def test_latest_workspace_claim_record_matches_task_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_record(
                {
                    "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
                    "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "auto-01-old",
                    "worktree": "/tmp/old",
                }
            )
            store.append_record(
                {
                    "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
                    "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
                    "task_id": "TASK-02",
                    "run_id": "run-2",
                    "branch": "auto-02",
                    "worktree": "/tmp/other",
                }
            )
            store.append_record(
                {
                    "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
                    "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "auto-01-new",
                    "worktree": "/tmp/new",
                }
            )

            record = store.latest_workspace_claim_record("TASK-01", "run-1")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["branch"], "auto-01-new")
        self.assertEqual(record["worktree"], "/tmp/new")

    def test_latest_workspace_claim_record_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_record(
                {
                    "record_type": WORKSPACE_CLAIM_RECORD_TYPE,
                    "event_type": WORKSPACE_CLAIMED_EVENT_TYPE,
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "auto-01",
                    "worktree": "/tmp/wt",
                }
            )

            self.assertIsNone(store.latest_workspace_claim_record("TASK-01", "run-2"))

    def test_task_recovery_event_records_launch_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_lifecycle_event(
                RunLifecycleEvent.task_recovery(
                    run_id="run-1",
                    task_id="TASK-01",
                    phase="launched",
                    prior_run_id="run-1",
                    attempt=1,
                    max_attempts=3,
                    branch="auto-01",
                    worktree="/tmp/wt",
                    transcript_path="/tmp/transcript.jsonl",
                    wrapper_log="/tmp/run-1.log",
                )
            )
            store.append_lifecycle_event(
                RunLifecycleEvent.task_recovery(
                    run_id="run-2",
                    task_id="TASK-01",
                    phase="outcome",
                    prior_run_id="run-1",
                    attempt=1,
                    max_attempts=3,
                    outcome="completed",
                )
            )
            records = store.read_records()

        recovery_records = [
            record
            for record in records
            if record.get("record_type") == TASK_RECOVERY_RECORD_TYPE
        ]
        self.assertEqual(len(recovery_records), 2)
        launched, outcome = recovery_records
        self.assertEqual(launched["phase"], "launched")
        self.assertEqual(launched["prior_run_id"], "run-1")
        self.assertEqual(launched["attempt"], 1)
        self.assertEqual(launched["branch"], "auto-01")
        self.assertEqual(launched["transcript_path"], "/tmp/transcript.jsonl")
        self.assertEqual(launched["reason"], "unknown_run_recovery")
        self.assertEqual(outcome["phase"], "outcome")
        self.assertEqual(outcome["outcome"], "completed")
        self.assertEqual(outcome["run_id"], "run-2")

    def test_task_recovery_record_type_is_known_and_lifecycle(self) -> None:
        self.assertIn(TASK_RECOVERY_RECORD_TYPE, KNOWN_RECORD_TYPES)

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
            inspection = store.inspect_run("run-1")

        self.assertEqual([run.run_id for run in runs], ["run-2", "run-1"])
        self.assertEqual(runs[0].status, "blocked")
        self.assertEqual(runs[0].record_type, "worker_report")
        self.assertIsNone(runs[0].exit_code)
        self.assertEqual(runs[1].status, "completed")
        self.assertEqual(runs[1].record_type, "run_result")
        self.assertEqual(runs[1].exit_code, 0)
        self.assertEqual(runs[1].record_count, 3)
        self.assertEqual(runs[1].agent_kind, "claude")
        self.assertEqual(runs[1].agent_prompt_dialect, "claude")
        self.assertEqual(runs[1].agent_prompt_dialect_source, "agent.kind:claude")
        self.assertEqual(runs[1].agent_skill_ref_prefix, "/")
        self.assertEqual(runs[1].agent_skill_ref_prefix_source, "agent.kind:claude")
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

    def test_read_records_keeps_autopilot_records_out_of_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
                    "cycle_id": "cycle-1",
                    "repo": directory,
                    "status": "blocked",
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                    "blockers": ["repo_dirty"],
                }
            )
            store.append_record(
                {
                    "schema_version": 1,
                    "record_type": "unknown_future_record",
                    "run_id": "run-1",
                }
            )

            records = store.read_records()
            runs = store.list_runs()

        self.assertIn(AUTOPILOT_CYCLE_RECORD_TYPE, AUTOPILOT_RECORD_TYPES)
        self.assertEqual(
            [record["record_type"] for record in records], ["autopilot_cycle"]
        )
        self.assertEqual(runs, [])

    def test_worktree_reap_record_type_registered(self) -> None:
        self.assertEqual(AUTOPILOT_WORKTREE_REAP_RECORD_TYPE, "autopilot_worktree_reap")
        self.assertIn(AUTOPILOT_WORKTREE_REAP_RECORD_TYPE, AUTOPILOT_RECORD_TYPES)
        self.assertIn(AUTOPILOT_WORKTREE_REAP_RECORD_TYPE, KNOWN_RECORD_TYPES)

    def test_native_planning_record_types_registered(self) -> None:
        for record_type in (
            AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
            AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
        ):
            self.assertIn(record_type, AUTOPILOT_RECORD_TYPES)
            self.assertIn(record_type, KNOWN_RECORD_TYPES)

    def test_idle_wait_record_type_registered(self) -> None:
        self.assertEqual(AUTOPILOT_IDLE_WAIT_RECORD_TYPE, "autopilot_idle_wait")
        self.assertIn(AUTOPILOT_IDLE_WAIT_RECORD_TYPE, AUTOPILOT_RECORD_TYPES)
        self.assertIn(AUTOPILOT_IDLE_WAIT_RECORD_TYPE, KNOWN_RECORD_TYPES)

    def test_read_records_keeps_worktree_reap_record_and_out_of_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_record(
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
                    "cycle_id": "cycle-1",
                    "repo": directory,
                    "status": "ok",
                    "reaped": 1,
                    "kept": 2,
                    "refused": 0,
                    "errors": 0,
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                }
            )

            records = store.read_records()
            runs = store.list_runs()

        self.assertEqual(
            [record["record_type"] for record in records],
            [AUTOPILOT_WORKTREE_REAP_RECORD_TYPE],
        )
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

    def test_inspect_run_includes_restart_lifecycle_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            store = RunStore(repo / "runs.jsonl")
            store.append_lifecycle_event(
                RunLifecycleEvent.task_restart(
                    run_id="run-1",
                    task_id="RT-04",
                    restart_count=2,
                    max_restarts=3,
                    cooldown_seconds=0.5,
                    reason="transient_worker_failure",
                )
            )
            store.append_lifecycle_event(
                RunLifecycleEvent.task_restart(
                    run_id="run-1",
                    task_id="RT-04",
                    restart_count=3,
                    max_restarts=3,
                    cooldown_seconds=0.5,
                    reason="restart_budget_exhausted",
                    exhausted=True,
                    attempted_restart_count=4,
                )
            )

            inspection = store.inspect_run("run-1")

        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.view.record_type, TASK_RESTART_RECORD_TYPE)
        self.assertEqual(inspection.view.status, "restart_budget_exhausted")
        self.assertEqual(inspection.view.restart_count, 3)
        self.assertEqual(inspection.view.max_restarts, 3)
        self.assertTrue(inspection.view.restart_exhausted)
        self.assertEqual(
            inspection.view.restart_exhausted_reason,
            "restart_budget_exhausted",
        )
        self.assertEqual(inspection.records[-1]["attempted_restart_count"], 4)

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
