from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vibe_loop.orchestration import RunStage, StageTransition
from vibe_loop.runs import (
    AGENT_CONTEXT_OBSERVED_RECORD_TYPE,
    ATTEMPT_CIRCUIT_RESET_RECORD_TYPE,
    AUTOPILOT_CYCLE_RECORD_TYPE,
    AUTOPILOT_IDLE_WAIT_RECORD_TYPE,
    AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
    AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
    AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
    AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
    AUTOPILOT_RECORD_TYPES,
    AUTOPILOT_TROUBLESHOOT_RECORD_TYPE,
    AUTOPILOT_WORKTREE_REAP_RECORD_TYPE,
    KNOWN_RECORD_TYPES,
    LIFECYCLE_EVENT_SCHEMA_VERSION,
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_EXPIRED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    LIFECYCLE_STATES,
    POST_REPORT_ACTIVITY_RECORD_TYPE,
    RUN_RECORD_TYPE,
    RUN_SCHEMA_VERSION,
    RUN_STARTED_RECORD_TYPE,
    RUN_STATE_TRANSITION_RECORD_TYPE,
    STAGE_TRANSITION_RECORD_TYPE,
    TASK_RECOVERY_RECORD_TYPE,
    TASK_RESTART_RECORD_TYPE,
    WORKSPACE_CLAIM_RECORD_TYPE,
    WORKSPACE_CLAIMED_EVENT_TYPE,
    WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE,
    WORKSPACE_PREFLIGHT_RECORD_TYPE,
    WORKER_REPORT_RECORD_TYPE,
    WORKER_REPORT_SCHEMA_VERSION,
    WORKER_PROCESS_STARTED_RECORD_TYPE,
    RunLifecycleEvent,
    AttemptCircuitInputs,
    RunResult,
    RunStore,
    WorkerReport,
    attempt_circuit_blocker_class,
    derive_run_lifecycle,
    record_status,
)


class RunStoreTests(unittest.TestCase):
    def test_malformed_review_output_counts_toward_attempt_circuit(self) -> None:
        self.assertEqual(
            attempt_circuit_blocker_class(
                {
                    "classification": "blocked",
                    "classification_source": "review_output_malformed",
                }
            ),
            "blocked:review_output_malformed",
        )
        self.assertEqual(
            attempt_circuit_blocker_class(
                {
                    "classification": "blocked",
                    "classification_source": "reviewer_verdict",
                }
            ),
            "blocked:reviewer_verdict",
        )

    def test_integration_provenance_status_exposes_outcome(self) -> None:
        record = RunLifecycleEvent.integration_provenance(
            run_id="run-1",
            task_id="TASK-01",
            outcome="refused-unprovable",
            candidate_commit="a" * 40,
            target_commit="b" * 40,
        ).to_record()

        self.assertEqual(record_status(record), "refused-unprovable")

    def test_cross_run_attempt_circuit_opens_and_records_avoided_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            inputs = AttemptCircuitInputs(
                task_id="TASK-01",
                task_revision="task-a",
                configuration_revision="config-a",
                base="base-a",
                candidate="candidate-a",
                route="codex:gpt-5",
            )
            for index in range(3):
                run_id = f"run-{index}"
                self.assertFalse(
                    store.reserve_attempt_circuit(
                        run_id=run_id, inputs=inputs, threshold=3
                    ).open
                )
                result = RunResult(
                    run_id=run_id,
                    task_id="TASK-01",
                    classification="failed",
                    classification_source="worker_exit",
                    exit_code=1,
                    log_path=Path("run.log"),
                    start_main="base-a",
                    end_main="base-a",
                )
                store.append_result(result)
                store.record_attempt_circuit_outcome(result, threshold=3)

            blocked = store.reserve_attempt_circuit(
                run_id="run-3", inputs=inputs, threshold=3
            )

            self.assertTrue(blocked.open)
            self.assertEqual(blocked.attempt_count, 3)
            self.assertEqual(blocked.avoided_launches, 1)
            self.assertEqual(blocked.blocker_class, "failed:worker_exit")
            self.assertEqual(len(store.attempt_circuit_states(threshold=3)), 1)

    def test_changed_candidate_and_operator_reset_start_new_attempt_epochs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            old = AttemptCircuitInputs("TASK-01", "task-a", "config-a", "base", "old")
            for index in range(3):
                run_id = f"old-{index}"
                store.reserve_attempt_circuit(run_id=run_id, inputs=old, threshold=3)
                result = RunResult(
                    run_id=run_id,
                    task_id="TASK-01",
                    classification="blocked",
                    classification_source="reviewer_verdict",
                    exit_code=1,
                    log_path=Path("run.log"),
                    start_main="base",
                    end_main="base",
                )
                store.append_result(result)
                store.record_attempt_circuit_outcome(result, threshold=3)
            self.assertTrue(
                store.reserve_attempt_circuit(
                    run_id="old-blocked", inputs=old, threshold=3
                ).open
            )
            changed = dataclasses.replace(old, candidate="new")
            self.assertFalse(
                store.reserve_attempt_circuit(
                    run_id="new-0", inputs=changed, threshold=3
                ).open
            )

            store.reset_attempt_circuit("TASK-01")

            self.assertEqual(store.attempt_circuit_states(threshold=3), [])
            self.assertIn(
                ATTEMPT_CIRCUIT_RESET_RECORD_TYPE,
                [record["record_type"] for record in store.read_records()],
            )

    def test_provider_walls_and_concurrent_observers_do_not_bypass_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            inputs = AttemptCircuitInputs(
                "TASK-01", "task", "config", "base", "candidate"
            )
            for index in range(3):
                run_id = f"wall-{index}"
                store.reserve_attempt_circuit(run_id=run_id, inputs=inputs, threshold=3)
                result = RunResult(
                    run_id=run_id,
                    task_id="TASK-01",
                    classification="provider_limit",
                    classification_source="provider_limit",
                    exit_code=1,
                    log_path=Path("run.log"),
                    start_main="base",
                    end_main="base",
                )
                store.append_result(result)
                store.record_attempt_circuit_outcome(result, threshold=3)
            self.assertFalse(
                store.reserve_attempt_circuit(
                    run_id="after-wall", inputs=inputs, threshold=3
                ).open
            )
            after_wall = RunResult(
                run_id="after-wall",
                task_id="TASK-01",
                classification="provider_limit",
                classification_source="provider_limit",
                exit_code=1,
                log_path=Path("run.log"),
                start_main="base",
                end_main="base",
            )
            store.append_result(after_wall)
            store.record_attempt_circuit_outcome(after_wall, threshold=3)

            for index in range(2):
                run_id = f"failure-{index}"
                store.reserve_attempt_circuit(run_id=run_id, inputs=inputs, threshold=3)
                result = RunResult(
                    run_id=run_id,
                    task_id="TASK-01",
                    classification="failed",
                    classification_source="worker_exit",
                    exit_code=1,
                    log_path=Path("run.log"),
                    start_main="base",
                    end_main="base",
                )
                store.append_result(result)
                store.record_attempt_circuit_outcome(result, threshold=3)

            with ThreadPoolExecutor(max_workers=2) as executor:
                states = list(
                    executor.map(
                        lambda run_id: store.reserve_attempt_circuit(
                            run_id=run_id, inputs=inputs, threshold=3
                        ),
                        ("concurrent-a", "concurrent-b"),
                    )
                )

            self.assertEqual(sum(not state.open for state in states), 1)
            self.assertEqual(sum(state.open for state in states), 1)

    def test_pending_provider_wall_releases_conservative_attempt_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            inputs = AttemptCircuitInputs(
                "TASK-01", "task", "config", "base", "candidate"
            )
            for index in range(2):
                run_id = f"failure-{index}"
                store.reserve_attempt_circuit(
                    run_id=run_id,
                    inputs=inputs,
                    threshold=3,
                )
                result = RunResult(
                    run_id=run_id,
                    task_id=inputs.task_id,
                    classification="blocked",
                    classification_source="worker_report",
                    exit_code=1,
                    log_path=Path("run.log"),
                    start_main=inputs.base,
                    end_main=inputs.base,
                )
                store.append_result(result)
                store.record_attempt_circuit_outcome(result, threshold=3)

            store.reserve_attempt_circuit(
                run_id="pending",
                inputs=inputs,
                threshold=3,
            )
            avoided = store.reserve_attempt_circuit(
                run_id="avoided",
                inputs=inputs,
                threshold=3,
            )
            self.assertTrue(avoided.open)
            self.assertTrue(avoided.launch_blocked)

            provider_wall = RunResult(
                run_id="pending",
                task_id=inputs.task_id,
                classification="provider_limit",
                classification_source="provider_wall",
                exit_code=1,
                log_path=Path("run.log"),
                start_main=inputs.base,
                end_main=inputs.base,
            )
            store.append_result(provider_wall)
            store.record_attempt_circuit_outcome(provider_wall, threshold=3)

            state = store.attempt_circuit_task_states(threshold=3)[0]
            self.assertEqual(state.attempt_count, 2)
            self.assertEqual(state.pending_count, 0)
            self.assertFalse(state.open)
            self.assertFalse(state.launch_blocked)
            self.assertEqual(store.attempt_circuit_states(threshold=3), [])
            next_attempt = store.reserve_attempt_circuit(
                run_id="after-wall",
                inputs=inputs,
                threshold=3,
            )
            self.assertFalse(next_attempt.open)
            self.assertEqual(next_attempt.pending_count, 1)

    def test_run_result_exposes_public_model_and_effort_aliases(self) -> None:
        result = RunResult(
            run_id="run-effort",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("run.log"),
            start_main="abc",
            end_main="def",
            model_id="gpt-5.4",
            model_id_source="command_arg:-m",
            reasoning_effort="high",
            reasoning_effort_source="command_config:model_reasoning_effort",
        )

        payload = result.to_json()

        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["effort"], "high")
        self.assertEqual(
            payload["effort_source"], "command_config:model_reasoning_effort"
        )

    def test_run_result_normalizes_invalid_attribution_at_ingestion(self) -> None:
        result = RunResult(
            run_id="run-invalid-attribution",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("run.log"),
            start_main="abc",
            end_main="def",
            model_provider="value",
            model_provider_source="native:stdout:json.model_provider",
            model_id="task",
            model_id_source="native:stdout:json.model",
        )

        payload = result.to_json()

        self.assertEqual(payload["model_provider"], "unknown")
        self.assertEqual(payload["model_id"], "unknown")
        self.assertEqual(payload["model"], "unknown")
        self.assertEqual(
            payload["attribution_diagnostics"],
            [
                {
                    "type": "invalid_attribution_label",
                    "field": "provider",
                    "normalized": "unknown",
                },
                {
                    "type": "invalid_attribution_label",
                    "field": "model",
                    "normalized": "unknown",
                },
            ],
        )

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

    def test_run_result_stats_round_trip_and_redact_sensitive_fields(self) -> None:
        result = RunResult(
            run_id="run-usage",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
            stats={
                "schema_version": 1,
                "phase": "implementation",
                "usage_source": "native:codex:turn.completed",
                "usage_version": "codex-jsonl-v1",
                "input_tokens": 12,
                "output_tokens": 3,
                "provider_usage": {
                    "input_tokens": 12,
                    "reasoning_output_tokens": 2,
                    "prompt": "PROMPT CANARY",
                },
                "quota_evidence_available": True,
                "quota_snapshots": [
                    {
                        "provider": "openai",
                        "scope": "codex",
                        "window": "primary",
                        "observed_at": "2026-05-09T00:00:00Z",
                        "used_percent": 25,
                        "window_minutes": 300,
                        "resets_at": 1778292000,
                        "command": "PRIVATE COMMAND",
                    }
                ],
                "prompt": "PROMPT CANARY",
                "credential": "sk-secret-canary",
                "token": "TOKEN CANARY",
                "fencing_token": "FENCING CANARY",
                "raw_transcript": "TRANSCRIPT CANARY",
                "candidate_fingerprint": "sk-secret-canary",
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            RunStore(path).append_result(result)
            payload = RunStore(path).read_records()[0]

        self.assertEqual(
            payload["stats"],
            {
                "schema_version": 1,
                "phase": "implementation",
                "usage_source": "native:codex:turn.completed",
                "usage_version": "codex-jsonl-v1",
                "input_tokens": 12,
                "output_tokens": 3,
                "provider_usage": {
                    "input_tokens": 12,
                    "reasoning_output_tokens": 2,
                },
                "quota_evidence_available": True,
                "quota_snapshots": [
                    {
                        "provider": "openai",
                        "scope": "codex",
                        "window": "primary",
                        "observed_at": "2026-05-09T00:00:00+00:00",
                        "used_percent": 25.0,
                        "window_minutes": 300,
                        "resets_at": 1778292000,
                    }
                ],
            },
        )
        encoded = json.dumps(payload)
        self.assertNotIn("PROMPT CANARY", encoded)
        self.assertNotIn("sk-secret", encoded)
        self.assertNotIn("TOKEN CANARY", encoded)
        self.assertNotIn("FENCING CANARY", encoded)
        self.assertNotIn("TRANSCRIPT CANARY", encoded)
        self.assertNotIn("PRIVATE COMMAND", encoded)

    def test_run_result_stats_reject_malformed_provenance_values(self) -> None:
        result = RunResult(
            run_id="run-malformed-usage",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=Path("/tmp/run.log"),
            start_main="aaa",
            end_main="bbb",
            stats={
                "phase": {"prompt": "PROMPT CANARY"},
                "work_kind": ["review", "TRANSCRIPT CANARY"],
                "provider": {"credential": "sk-secret-canary"},
                "usage_source": ["native:provider"],
                "candidate_fingerprint": ["FENCING CANARY"],
                "provider_usage": {"input_tokens": 7, "secret": "TOKEN CANARY"},
                "quota_evidence_available": True,
                "quota_unavailable_reason": "malformed_quota_snapshot",
                "quota_snapshots": [
                    {
                        "provider": "openai",
                        "scope": "sk-secret-canary",
                        "window": "primary",
                        "observed_at": "PROMPT CANARY",
                        "used_percent": 10,
                        "window_minutes": 300,
                        "resets_at": 1778292000,
                    }
                ],
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            RunStore(path).append_result(result)
            payload = RunStore(path).read_records()[0]

        self.assertEqual(
            payload["stats"],
            {
                "provider_usage": {"input_tokens": 7},
                "quota_evidence_available": False,
                "quota_unavailable_reason": "malformed_quota_snapshot",
            },
        )
        encoded = json.dumps(payload)
        for canary in (
            "PROMPT CANARY",
            "TRANSCRIPT CANARY",
            "sk-secret-canary",
            "FENCING CANARY",
            "TOKEN CANARY",
        ):
            self.assertNotIn(canary, encoded)

    def test_run_result_json_can_store_native_session_id(self) -> None:
        result = RunResult(
            run_id="run-1",
            session_id="native-session-1",
            session_id_source="native:stdout:startup_frame.session_id",
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
        self.assertEqual(
            payload["session_id_source"],
            "native:stdout:startup_frame.session_id",
        )
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

    def test_worker_report_redacts_active_token_from_unlabelled_fields(self) -> None:
        token = "report-generation-7"
        report = WorkerReport(
            run_id="run-1",
            task_id="TASK-01",
            status="blocked",
            message=f"backend returned {token}",
            metadata={
                "detail": f"VIBE_LOOP_FENCING_TOKEN={token}",
                "substring": "report-generation-",
                "unrelated": "report-generation-70",
            },
            fencing_token=token,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"

            RunStore(path).append_report(report)

            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertNotIn(f'"{token}"', raw)
        self.assertEqual(payload["message"], "backend returned <redacted>")
        self.assertEqual(
            payload["metadata"]["detail"],
            "VIBE_LOOP_FENCING_TOKEN=<redacted>",
        )
        self.assertEqual(payload["metadata"]["substring"], "report-generation-")
        self.assertEqual(payload["metadata"]["unrelated"], "report-generation-70")

    def test_worker_report_preserves_legacy_positional_reported_at(self) -> None:
        reported_at = "2026-05-09T00:00:30+00:00"

        report = WorkerReport(
            "run-1",
            "TASK-01",
            "blocked",
            "abc123",
            "waiting",
            {"reason": "external"},
            reported_at,
        )

        self.assertEqual(report.reported_at, reported_at)
        self.assertEqual(report.fencing_token, "")

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

    def test_worker_process_started_event_persists_exact_observed_identity(
        self,
    ) -> None:
        event = RunLifecycleEvent.worker_process_started(
            run_id="run-1",
            task_id="TASK-01",
            worker_pid=123,
            supervisor_pid=100,
            process_group_id=123,
            session_id=123,
            process_birth_id="boot-id:500",
            host="test-host",
            activity_source_generation="a" * 64,
        ).to_record()

        self.assertEqual(event["record_type"], WORKER_PROCESS_STARTED_RECORD_TYPE)
        self.assertEqual(event["worker_pid"], 123)
        self.assertEqual(event["supervisor_pid"], 100)
        self.assertEqual(event["worker_process_group_id"], 123)
        self.assertEqual(event["worker_session_id"], 123)
        self.assertEqual(event["worker_process_birth_id"], "boot-id:500")
        self.assertEqual(event["activity_source_generation"], "a" * 64)
        self.assertEqual(event["pid_source"], "popen")
        self.assertIn(WORKER_PROCESS_STARTED_RECORD_TYPE, KNOWN_RECORD_TYPES)

    def test_workspace_preflight_event_has_closed_retry_vocabulary(self) -> None:
        event = RunLifecycleEvent.workspace_preflight(
            run_id="run-1",
            task_id="TASK-01",
            decision="rejected",
            reason="workspace_stale_current_base",
            retry_disposition="defer_until_workspace_changes",
            worker_launch_allowed=False,
            branch="vibe/TASK-01",
            worktree=Path("/workspace/TASK-01"),
            selected_base="a" * 40,
            workspace_base="b" * 40,
            head_commit="c" * 40,
            workspace_state_fingerprint="d" * 64,
            refresh_refused="merge_failed",
        ).to_record()

        self.assertEqual(event["record_type"], WORKSPACE_PREFLIGHT_RECORD_TYPE)
        self.assertEqual(event["decision"], "rejected")
        self.assertEqual(event["retry_disposition"], "defer_until_workspace_changes")
        self.assertFalse(event["worker_launch_allowed"])
        self.assertEqual(event["workspace_state_fingerprint"], "d" * 64)
        self.assertEqual(event["refresh_refused"], "merge_failed")
        self.assertIn(WORKSPACE_PREFLIGHT_RECORD_TYPE, KNOWN_RECORD_TYPES)
        omitted = RunLifecycleEvent.workspace_preflight(
            run_id="run-1b",
            task_id="TASK-01",
            decision="rejected",
            reason="workspace_stale_current_base",
            retry_disposition="defer_until_workspace_changes",
            worker_launch_allowed=False,
        ).to_record()
        self.assertNotIn("refresh_refused", omitted)
        with self.assertRaisesRegex(ValueError, "retry disposition"):
            RunLifecycleEvent.workspace_preflight(
                run_id="run-2",
                task_id="TASK-01",
                decision="rejected",
                reason="workspace_stale_current_base",
                retry_disposition="retry_immediately",
                worker_launch_allowed=False,
            )
        with self.assertRaisesRegex(ValueError, "state fingerprint"):
            RunLifecycleEvent.workspace_preflight(
                run_id="run-3",
                task_id="TASK-01",
                decision="rejected",
                reason="workspace_stale_current_base",
                retry_disposition="defer_until_workspace_changes",
                worker_launch_allowed=False,
                workspace_state_fingerprint="not-a-fingerprint",
            )
        with self.assertRaisesRegex(ValueError, "refresh refusal"):
            RunLifecycleEvent.workspace_preflight(
                run_id="run-4",
                task_id="TASK-01",
                decision="rejected",
                reason="workspace_stale_current_base",
                retry_disposition="defer_until_workspace_changes",
                worker_launch_allowed=False,
                refresh_refused="because_i_said_so",
            )

    def test_post_report_activity_event_records_violation_and_teardown(
        self,
    ) -> None:
        escaped = {
            "pid": 4444,
            "ppid": 4321,
            "pgid": 4444,
            "sid": 4321,
            "comm": "python",
            "cmdline": "python worker.py",
            "state": "S",
            "process_birth_id": "boot-id:501",
        }
        event = RunLifecycleEvent.post_report_activity(
            run_id="run-1",
            task_id="TASK-01",
            activity_kind="tool_call",
            activity_count=3,
            post_report_seconds=42.5,
            worker_pid=4321,
            process_group_id=4321,
            identity_verified=True,
            terminated=True,
            report_status="completed",
            runtime_lifecycle_decision="continue",
            runtime_lifecycle_reason="verified_runtime_enforced_teardown",
            escaped_descendants=(escaped,),
        ).to_record()

        self.assertEqual(event["record_type"], POST_REPORT_ACTIVITY_RECORD_TYPE)
        self.assertEqual(event["policy"], "post_report_activity")
        self.assertEqual(event["activity_kind"], "tool_call")
        self.assertEqual(event["activity_count"], 3)
        self.assertEqual(event["post_report_seconds"], 42.5)
        self.assertEqual(event["worker_pid"], 4321)
        self.assertEqual(event["worker_process_group_id"], 4321)
        self.assertTrue(event["identity_verified"])
        self.assertTrue(event["terminated"])
        self.assertEqual(event["report_status"], "completed")
        self.assertEqual(event["runtime_lifecycle_decision"], "continue")
        self.assertEqual(
            event["runtime_lifecycle_reason"],
            "verified_runtime_enforced_teardown",
        )
        self.assertEqual(event["escaped_descendants"], [escaped])
        self.assertIn(POST_REPORT_ACTIVITY_RECORD_TYPE, KNOWN_RECORD_TYPES)

    def test_post_report_activity_event_round_trips_through_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = RunStore(path)
            store.append_lifecycle_event(
                RunLifecycleEvent.post_report_activity(
                    run_id="run-1",
                    task_id="TASK-01",
                    activity_kind="tool_call",
                    activity_count=1,
                    post_report_seconds=5.0,
                    worker_pid=None,
                    process_group_id=None,
                    identity_verified=False,
                    terminated=False,
                    report_status="completed",
                    runtime_lifecycle_decision="refuse",
                    runtime_lifecycle_reason="worker_identity_not_verified",
                )
            )
            records = store.read_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], POST_REPORT_ACTIVITY_RECORD_TYPE)
        self.assertIsNone(records[0]["worker_pid"])
        self.assertFalse(records[0]["terminated"])

    def test_post_report_closure_records_bounded_teardown_evidence(self) -> None:
        escaped = {
            "pid": 102,
            "ppid": 101,
            "pgid": 102,
            "sid": 101,
            "comm": "python",
            "cmdline": "python worker.py",
            "state": "S",
            "process_birth_id": "boot-id:501",
        }
        event = RunLifecycleEvent.post_report_closure(
            run_id="run-1",
            task_id="TASK-01",
            post_report_seconds=0.4,
            teardown_seconds=0.03,
            worker_pid=101,
            process_group_id=101,
            process_count=2,
            identity_verified=True,
            descendants_verified=True,
            terminated=True,
            report_status="completed",
            teardown_reason="accepted_report_runtime_closure",
            runtime_lifecycle_decision="continue",
            runtime_lifecycle_reason=("verified_accepted_report_runtime_closure"),
            escaped_descendants=(escaped,),
        ).to_record()

        self.assertEqual(event["record_type"], "post_report_closure")
        self.assertEqual(event["policy"], "accepted_report_runtime_closure")
        self.assertEqual(event["teardown_process_count"], 2)
        self.assertTrue(event["identity_verified"])
        self.assertTrue(event["descendants_verified"])
        self.assertTrue(event["terminated"])
        self.assertEqual(event["escaped_descendants"], [escaped])
        encoded = json.dumps(event)
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("tool_payload", encoded)

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
        self.assertNotIn("stage", payload)

    def test_derive_run_lifecycle_exposes_only_accepted_recorded_stage(self) -> None:
        records = [
            RunLifecycleEvent.stage_transition(
                run_id="run-1",
                task_id="TASK-01",
                transition=StageTransition(
                    from_stage=None,
                    to_stage=RunStage.ACTIVATION,
                    reason="contract_resolved",
                    ordinal=1,
                    accepted=True,
                ),
            ).to_record(),
            RunLifecycleEvent.stage_transition(
                run_id="run-1",
                task_id="TASK-01",
                transition=StageTransition(
                    from_stage=RunStage.ACTIVATION,
                    to_stage=RunStage.REVIEW,
                    reason="illegal_skip",
                    ordinal=1,
                    accepted=False,
                ),
            ).to_record(),
            RunLifecycleEvent.stage_transition(
                run_id="run-1",
                task_id="TASK-01",
                transition=StageTransition(
                    from_stage=RunStage.ACTIVATION,
                    to_stage=RunStage.WORKSPACE,
                    reason="activated",
                    ordinal=1,
                    accepted=True,
                ),
            ).to_record(),
        ]
        records[-1]["occurred_at"] = "2026-05-09T00:00:20+00:00"

        payload = derive_run_lifecycle(records).to_json()

        self.assertEqual(records[0]["record_type"], STAGE_TRANSITION_RECORD_TYPE)
        self.assertEqual(payload["stage"], "workspace")
        self.assertEqual(payload["stage_ordinal"], 1)
        self.assertEqual(payload["stage_started_at"], "2026-05-09T00:00:20+00:00")

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

    def test_pending_recovery_is_durable_and_visible_in_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            store.append_lifecycle_event(
                RunLifecycleEvent.task_recovery(
                    run_id="run-1",
                    task_id="TASK-01",
                    phase="pending",
                    prior_run_id="run-1",
                    attempt=2,
                    max_attempts=3,
                    payload={
                        "prior_classification": "unknown",
                        "workspace_claimed": True,
                        "dirty_snapshot": ["M staged.txt", "?? unstaged.txt"],
                    },
                )
            )

            pending = store.pending_recovery_records()
            view = store.inspect_run("run-1")

            self.assertEqual(len(pending), 1)
            self.assertIsNotNone(view)
            assert view is not None
            payload = view.to_json()
            self.assertTrue(payload["recovery_pending"])
            self.assertEqual(payload["recovery_attempt"], 2)
            self.assertEqual(payload["recovery_max_attempts"], 3)

            store.append_lifecycle_event(
                RunLifecycleEvent.task_recovery(
                    run_id="run-1",
                    task_id="TASK-01",
                    phase="launched",
                    prior_run_id="run-1",
                    attempt=2,
                    max_attempts=3,
                )
            )

            self.assertEqual(store.pending_recovery_records(), [])
            refreshed = store.inspect_run("run-1")
            assert refreshed is not None
            self.assertFalse(refreshed.to_json()["recovery_pending"])

    def test_worker_launch_atomically_charges_embedded_recovery_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(root / "runs.jsonl")
            first_intent = {
                "task_id": "TASK-01",
                "prior_run_id": "run-1",
                "prior_classification": "unknown",
                "attempt": 1,
                "max_attempts": 3,
                "workspace_claimed": False,
                "dirty_snapshot": [],
            }
            store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="unknown",
                    exit_code=0,
                    log_path=root / "run-1.log",
                    start_main="aaa",
                    end_main="aaa",
                    recovery_intent=first_intent,
                )
            )
            self.assertEqual(
                store.pending_recovery_records()[0]["attempt"],
                1,
            )

            store.append_lifecycle_event(
                RunLifecycleEvent.worker_process_started(
                    run_id="run-2",
                    task_id="TASK-01",
                    worker_pid=123,
                    supervisor_pid=12,
                    process_group_id=123,
                    session_id=123,
                    process_birth_id="birth",
                    host="host",
                    recovery_payload=first_intent,
                )
            )
            launched_pending = store.pending_recovery_records()
            self.assertEqual(len(launched_pending), 1)
            self.assertEqual(launched_pending[0]["prior_run_id"], "run-2")
            self.assertEqual(launched_pending[0]["attempt"], 2)
            self.assertTrue(launched_pending[0]["needs_identity_refresh"])

            second_intent = {
                **first_intent,
                "prior_run_id": "run-2",
                "attempt": 2,
            }
            store.append_result(
                RunResult(
                    run_id="run-2",
                    task_id="TASK-01",
                    classification="unknown",
                    exit_code=0,
                    log_path=root / "run-2.log",
                    start_main="aaa",
                    end_main="aaa",
                    recovery_intent=second_intent,
                )
            )

            pending = store.pending_recovery_records()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["prior_run_id"], "run-2")
            self.assertEqual(pending[0]["attempt"], 2)

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

    def test_old_provider_limit_record_is_read_without_rewriting_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            legacy = {
                "schema_version": RUN_SCHEMA_VERSION,
                "record_type": RUN_RECORD_TYPE,
                "run_id": "legacy-run",
                "task_id": "TASK-01",
                "classification": "limit_wall",
                "classification_source": "limit_wall",
                "exit_code": 1,
                "log": "legacy.log",
                "start_main": "aaa",
                "end_main": "aaa",
                "finished_at": "2026-07-24T00:00:00+00:00",
            }
            path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            store = RunStore(path)

            listed = store.list_runs()
            inspected = store.inspect_run("legacy-run")

            self.assertEqual(listed[0].status, "provider_limit")
            self.assertEqual(listed[0].classification_source, "provider_limit")
            self.assertIsNotNone(inspected)
            assert inspected is not None
            self.assertEqual(inspected.view.status, "provider_limit")
            self.assertEqual(
                inspected.records[0]["classification"],
                "limit_wall",
            )
            self.assertIn('"classification": "limit_wall"', path.read_text())

    def test_new_run_result_canonicalizes_legacy_provider_limit_token(self) -> None:
        result = RunResult(
            run_id="new-run",
            task_id="TASK-01",
            classification="limit_wall",
            classification_source="limit_wall",
            exit_code=1,
            log_path=Path("run.log"),
            start_main="aaa",
            end_main="aaa",
        )

        payload = result.to_json()

        self.assertEqual(payload["classification"], "provider_limit")
        self.assertEqual(payload["classification_source"], "provider_limit")

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
            AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
            AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
        ):
            with self.subTest(record_type=record_type):
                self.assertIn(record_type, AUTOPILOT_RECORD_TYPES)
                self.assertIn(record_type, KNOWN_RECORD_TYPES)

    def test_read_records_keeps_native_planning_records_out_of_run_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.jsonl")
            planning_records = [
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                    "repo": directory,
                    "cycle_id": "cycle-1",
                    "run_id": "run-1",
                    "stage": "read_only_detection",
                    "status": "planning_requested",
                    "should_plan": True,
                },
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
                    "occurred_at": "2026-05-09T00:00:01+00:00",
                    "repo": directory,
                    "cycle_id": "cycle-1",
                    "run_id": "run-1",
                    "stage": "read_write_authoring",
                    "phase": "terminal",
                    "status": "completed",
                },
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
                    "occurred_at": "2026-05-09T00:00:02+00:00",
                    "repo": directory,
                    "cycle_id": "cycle-1",
                    "run_id": "run-1",
                    "fingerprint": "1:0:abcdef0123456789",
                },
                {
                    "schema_version": 1,
                    "record_type": AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
                    "occurred_at": "2026-05-09T00:00:03+00:00",
                    "repo": directory,
                    "cycle_id": "cycle-1",
                    "run_id": "run-1",
                    "outcome": "tasks_created",
                    "fingerprint": "1:0:abcdef0123456789",
                    "provider_launched": True,
                },
            ]
            for record in planning_records:
                store.append_record(record)
            store.append_record(
                {
                    "schema_version": 1,
                    "record_type": "unknown_future_record",
                    "cycle_id": "cycle-1",
                    "run_id": "run-1",
                }
            )

            records = store.read_records()
            runs = store.list_runs()

        self.assertEqual(
            [record["record_type"] for record in records],
            [
                AUTOPILOT_PLANNING_DECISION_RECORD_TYPE,
                AUTOPILOT_PLANNING_WORKER_RECORD_TYPE,
                AUTOPILOT_PLANNING_LAUNCH_RECORD_TYPE,
                AUTOPILOT_PLANNING_OUTCOME_RECORD_TYPE,
            ],
        )
        self.assertEqual(records[2]["fingerprint"], "1:0:abcdef0123456789")
        self.assertEqual(records[3]["outcome"], "tasks_created")
        self.assertEqual(runs, [])

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
                            {
                                "record_type": AUTOPILOT_TROUBLESHOOT_RECORD_TYPE,
                                "cycle_id": "cycle-1",
                                "status": "observed",
                            }
                        ),
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
            [record.get("task_id") for record in records],
            ["TASK-01", None, "TASK-02"],
        )
        self.assertEqual(records[1]["record_type"], AUTOPILOT_TROUBLESHOOT_RECORD_TYPE)
        self.assertEqual(
            records[2]["record_type"], WORKSPACE_CLAIM_MISMATCH_RECORD_TYPE
        )

    def test_recent_matching_records_ignore_unrelated_tail_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            store = RunStore(path)
            for ordinal in range(3):
                store.append_result(
                    RunResult(
                        run_id=f"run-{ordinal}",
                        task_id="TASK-01",
                        classification="failed",
                        exit_code=1,
                        log_path=Path(directory) / f"run-{ordinal}.log",
                        start_main="base",
                        end_main="base",
                    )
                )
                for cycle in range(100):
                    store.append_record(
                        {
                            "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
                            "cycle_id": f"cycle-{ordinal}-{cycle}",
                            "padding": "x" * 70000 if cycle == 50 else "",
                        }
                    )

            records = store.recent_records_matching(
                record_types=frozenset({RUN_RECORD_TYPE}),
                max_runs=3,
            )

        self.assertEqual(
            [record["run_id"] for record in records],
            ["run-0", "run-1", "run-2"],
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
