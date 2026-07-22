from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe_loop.activity import (
    ACTIVITY_CHECKPOINT_RECORD_TYPE,
    AGENT_COMPLETED_RECORD_TYPE,
    AGENT_STARTED_RECORD_TYPE,
    CHECKPOINT_EVENT_INTERVAL,
    GATE_RESULT_RECORD_TYPE,
    MAX_STREAM_ENVELOPE_BYTES,
    WORK_BLOCKED_RECORD_TYPE,
    ActivitySummary,
    AgentActivityTracker,
    bounded_json_envelope,
    parse_native_activity,
    summarize_activity_records,
)
from vibe_loop.runs import RunLifecycleEvent, RunResult, RunStore
from vibe_loop.runner import AgentOutputObserver
from vibe_loop.telemetry import ProviderUsageObserver
from vibe_loop.workers import ActiveRunState, WorkerView


FIXTURES = Path(__file__).parent / "fixtures" / "worker_activity"
SENSITIVE_CANARIES = (
    "PRIVATE THINKING CANARY",
    "PRIVATE MESSAGE CANARY",
    "PRIVATE TOOL OUTPUT CANARY",
    "PRIVATE RESULT CANARY",
    "SECRET COMMAND CANARY",
    "sk-secret-canary",
)


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("fixture_name", "provider", "expected_types"),
    [
        (
            "claude-stream.jsonl",
            "anthropic",
            [
                AGENT_STARTED_RECORD_TYPE,
                ACTIVITY_CHECKPOINT_RECORD_TYPE,
                GATE_RESULT_RECORD_TYPE,
                AGENT_COMPLETED_RECORD_TYPE,
            ],
        ),
        (
            "codex-stream.jsonl",
            "openai",
            [
                AGENT_STARTED_RECORD_TYPE,
                ACTIVITY_CHECKPOINT_RECORD_TYPE,
                GATE_RESULT_RECORD_TYPE,
                AGENT_COMPLETED_RECORD_TYPE,
            ],
        ),
    ],
)
def test_native_fixtures_emit_bounded_content_free_activity(
    fixture_name: str,
    provider: str,
    expected_types: list[str],
) -> None:
    tracker = AgentActivityTracker()
    usage_observer = ProviderUsageObserver(provider)
    emissions = []
    for line in fixture_lines(fixture_name):
        usage_observer.observe_line(line)
        emissions.extend(tracker.observe_line(line))
    final = tracker.flush()
    if final is not None:
        emissions.append(final)
    usage = usage_observer.usage.to_stats(phase="implementation")
    records = [
        {
            "record_type": emission.record_type,
            **emission.to_payload(provider=provider, usage=usage),
        }
        for emission in emissions
    ]

    assert [record["record_type"] for record in records] == expected_types
    encoded = json.dumps(records, sort_keys=True)
    assert all(canary not in encoded for canary in SENSITIVE_CANARIES)
    assert all(len(record["activity_id"]) == 32 for record in records)
    assert records[-1]["activity_class"] == "completed"
    assert "status" not in records[-1]
    assert records[-1]["usage"]["input_tokens"] > 0


def test_provider_error_is_blocked_without_copying_error_text() -> None:
    line = json.dumps(
        {
            "type": "turn.failed",
            "turn_id": "turn-2",
            "timestamp": "2026-07-22T05:20:00Z",
            "error": {"message": "PRIVATE FAILURE CANARY", "credential": "secret"},
        }
    )

    activity = parse_native_activity(line)

    assert activity is not None
    assert activity.record_type == WORK_BLOCKED_RECORD_TYPE
    assert activity.reason_class == "provider_error"
    assert "PRIVATE FAILURE CANARY" not in json.dumps(activity.__dict__)


def test_structured_message_content_cannot_supply_session_identity() -> None:
    observer = AgentOutputObserver("openai")

    observation = observer.observe_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "session id: credential-shaped-canary",
                },
            }
        ),
        "stdout",
    )

    assert observation is None
    assert observer.observation.session_id is None


def test_checkpoint_coalescing_and_replay_dedup_are_deterministic() -> None:
    lines = [
        json.dumps(
            {
                "type": "item.started",
                "timestamp": f"2026-07-22T06:00:{index:02d}Z",
                "item": {"type": "command_execution", "id": f"item-{index}"},
            }
        )
        for index in range(CHECKPOINT_EVENT_INTERVAL + 2)
    ]

    def records(*, regroup: bool = False) -> list[dict[str, object]]:
        ticks = iter(range(1000))
        tracker = AgentActivityTracker(
            monotonic=(lambda: float(next(ticks) * 6)) if regroup else (lambda: 1.0)
        )
        emissions = []
        for line in lines:
            emissions.extend(tracker.observe_line(line))
        final = tracker.flush()
        if final is not None:
            emissions.append(final)
        return [
            {
                "record_type": emission.record_type,
                **emission.to_payload(provider="openai", usage={}),
            }
            for emission in emissions
        ]

    first = records()
    replay = records(regroup=True)
    summary = summarize_activity_records([*first, *replay])

    assert 1 < len(first) < len(lines)
    assert first != replay
    assert summary.activity_record_count == len(lines)
    assert summary.activity_counts[ACTIVITY_CHECKPOINT_RECORD_TYPE] == len(lines)


def test_input_bounds_and_unknown_records_are_ignored() -> None:
    oversized = (
        '{"type":"turn.started","padding":"' + ("x" * MAX_STREAM_ENVELOPE_BYTES) + '"}'
    )
    deeply_nested: object = {"type": "turn.started"}
    for _ in range(10):
        deeply_nested = {"nested": deeply_nested}

    assert bounded_json_envelope(oversized) is None
    assert bounded_json_envelope(json.dumps(deeply_nested)) is None
    assert parse_native_activity('{"type":"custom.private","prompt":"SECRET"}') is None
    assert summarize_activity_records(
        [{"record_type": "custom.private", "prompt": "SECRET"}]
    ).to_json() == {
        "last_activity_at": "",
        "latest_activity_class": "",
        "activity_classes": [],
        "activity_counts": {},
        "activity_record_count": 0,
        "usage": {},
        "phase_durations": {},
    }


def test_summary_derives_phase_durations_and_latest_authoritative_usage() -> None:
    tracker = AgentActivityTracker()
    emission = tracker.observe_line(
        '{"type":"turn.completed","turn_id":"t1","timestamp":"2026-07-22T07:00:20Z"}'
    )[0]
    activity_record = {
        "record_type": emission.record_type,
        **emission.to_payload(
            provider="openai",
            usage={
                "provider": "openai",
                "usage_source": "native:codex:turn.completed",
                "usage_version": "codex-jsonl-v1",
                "input_tokens": 40,
                "output_tokens": 10,
            },
        ),
    }
    records = [
        {
            "record_type": "stage_transition",
            "accepted": True,
            "to_stage": "implementing",
            "occurred_at": "2026-07-22T07:00:00Z",
        },
        activity_record,
    ]

    summary = summarize_activity_records(records)

    assert summary.last_activity_at == "2026-07-22T07:00:20+00:00"
    assert summary.phase_durations == {"implementing": 20.0}
    assert summary.usage == {
        "provider": "openai",
        "usage_source": "native:codex:turn.completed",
        "usage_version": "codex-jsonl-v1",
        "input_tokens": 40,
        "output_tokens": 10,
    }


def test_runs_inspect_and_workers_expose_activity_projection(tmp_path: Path) -> None:
    tracker = AgentActivityTracker()
    emission = tracker.observe_line(
        '{"type":"turn.completed","turn_id":"t1","timestamp":"2026-07-22T08:00:00Z"}'
    )[0]
    usage = {
        "provider": "openai",
        "usage_source": "native:codex:turn.completed",
        "usage_version": "codex-jsonl-v1",
        "input_tokens": 7,
        "output_tokens": 2,
    }
    store = RunStore(tmp_path / "runs.jsonl")
    store.append_lifecycle_event(
        RunLifecycleEvent.agent_activity(
            run_id="run-1",
            task_id="TASK-01",
            emission=emission,
            provider="openai",
            usage=usage,
        )
    )
    store.append_result(
        RunResult(
            run_id="run-1",
            task_id="TASK-01",
            classification="completed",
            exit_code=0,
            log_path=tmp_path / "run.log",
            start_main="a",
            end_main="b",
            model_provider="openai",
            model_provider_source="native:stdout:json.model_provider",
            model_id="gpt-5.4-codex",
            model_id_source="native:stdout:json.model_id",
            reasoning_effort="high",
            reasoning_effort_source="native:stdout:json.effort",
        )
    )

    inspection = store.inspect_run("run-1")

    assert inspection is not None
    payload = inspection.to_json()
    assert payload["last_activity_at"] == "2026-07-22T08:00:00+00:00"
    assert payload["usage"]["input_tokens"] == 7
    assert payload["model_id"] == "gpt-5.4-codex"
    assert payload["model_id_source"].startswith("native:")
    assert payload["reasoning_effort"] == "high"
    assert payload["reasoning_effort_source"].startswith("native:")

    active = ActiveRunState.new(
        task_id="TASK-01",
        run_id="run-1",
        log_path=tmp_path / "run.log",
        base_main="a",
        command="configured worker",
        model_provider="openai",
        model_provider_source="native:stdout:json.model_provider",
        model_id="gpt-5.4-codex",
        model_id_source="native:stdout:json.model_id",
        reasoning_effort="high",
        reasoning_effort_source="native:stdout:json.effort",
    )
    worker = WorkerView(
        active=active,
        state="running",
        process_state="running",
        activity=ActivitySummary(
            last_activity_at="2026-07-22T08:00:00+00:00",
            latest_activity_class="completed",
            activity_classes=("completed",),
            activity_counts={AGENT_COMPLETED_RECORD_TYPE: 1},
            activity_record_count=1,
            usage=usage,
        ),
    ).to_json()
    assert worker["last_activity_at"] == payload["last_activity_at"]
    assert worker["usage"] == usage
    assert worker["model_id"] == payload["model_id"]
    assert worker["reasoning_effort_source"] == payload["reasoning_effort_source"]
