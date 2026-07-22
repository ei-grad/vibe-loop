from __future__ import annotations

import json
import sys
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
from vibe_loop.runner import AgentOutputObserver, AgentRuntimeObservation
from vibe_loop.runner import (
    parse_agent_runtime_context_from_command,
    run_streaming_command,
)
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
SOURCE_GENERATION_A = "a" * 64
SOURCE_GENERATION_B = "b" * 64


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
    for source_position, line in enumerate(fixture_lines(fixture_name), start=1):
        usage_observer.observe_line(line)
        emissions.extend(
            tracker.observe_line(
                line,
                source_generation=SOURCE_GENERATION_A,
                source_stream="stdout",
                source_position=source_position,
            )
        )
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
    observer = AgentOutputObserver("openai", source_generation=SOURCE_GENERATION_A)

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
        source_position=1,
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
        for source_position, line in enumerate(lines, start=1):
            emissions.extend(
                tracker.observe_line(
                    line,
                    source_generation=SOURCE_GENERATION_A,
                    source_stream="stdout",
                    source_position=source_position,
                )
            )
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


def test_idless_codex_identity_survives_replay_restart_and_reconstruction() -> None:
    completed = (
        Path(__file__).parent / "fixtures" / "provider_usage" / "codex-present.json"
    ).read_text(encoding="utf-8")
    lines = [line for _ in range(3) for line in ('{"type":"turn.started"}', completed)]

    def records(
        positioned_lines: list[tuple[int, str]],
        *,
        source_generation: str,
        regroup: bool,
    ) -> list[dict[str, object]]:
        ticks = iter(range(1000))
        tracker = AgentActivityTracker(
            monotonic=(lambda: float(next(ticks) * 6)) if regroup else (lambda: 1.0),
            wallclock=lambda: 100.0,
        )
        emissions = []
        for source_position, line in positioned_lines:
            emissions.extend(
                tracker.observe_line(
                    line,
                    source_generation=source_generation,
                    source_stream="stdout",
                    source_position=source_position,
                )
            )
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

    full_prefix = list(enumerate(lines, start=1))
    original = records(
        full_prefix, source_generation=SOURCE_GENERATION_A, regroup=False
    )
    full_replay = records(
        full_prefix, source_generation=SOURCE_GENERATION_A, regroup=True
    )
    partial_replay = records(
        full_prefix[2:], source_generation=SOURCE_GENERATION_A, regroup=True
    )
    restarted_suffix = records(
        list(enumerate(lines[:2], start=1)),
        source_generation=SOURCE_GENERATION_B,
        regroup=False,
    )
    original_event_ids = {
        event["id"] for record in original for event in record["activity_events"]
    }
    full_replay_ids = {
        event["id"] for record in full_replay for event in record["activity_events"]
    }
    partial_replay_ids = {
        event["id"] for record in partial_replay for event in record["activity_events"]
    }
    restarted_ids = {
        event["id"]
        for record in restarted_suffix
        for event in record["activity_events"]
    }
    journal_records = json.loads(
        json.dumps([*original, *partial_replay, *full_replay, *restarted_suffix])
    )
    summary = summarize_activity_records(journal_records)

    assert len(original_event_ids) == 6
    assert full_replay_ids == original_event_ids
    assert partial_replay_ids < original_event_ids
    assert restarted_ids.isdisjoint(original_event_ids)
    assert {
        event["source_generation"]
        for record in journal_records
        for event in record["activity_events"]
    } == {SOURCE_GENERATION_A, SOURCE_GENERATION_B}
    assert summary.activity_counts == {
        ACTIVITY_CHECKPOINT_RECORD_TYPE: 4,
        AGENT_COMPLETED_RECORD_TYPE: 4,
    }
    assert summary.activity_record_count == 8


def test_projection_uses_latest_timestamp_not_reader_order() -> None:
    tracker = AgentActivityTracker()
    completed = tracker.observe_line(
        '{"type":"turn.completed","turn_id":"turn-new",'
        '"timestamp":"2026-07-22T10:00:10Z"}',
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=1,
    )[0]
    older_tool = tracker.observe_line(
        '{"type":"item.started","timestamp":"2026-07-22T10:00:05Z",'
        '"item":{"type":"command_execution","id":"tool-old"}}',
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=2,
    )[0]
    records = [
        {
            "record_type": completed.record_type,
            **completed.to_payload(
                provider="openai",
                usage={"provider": "openai", "input_tokens": 20},
            ),
        },
        {
            "record_type": older_tool.record_type,
            **older_tool.to_payload(
                provider="openai",
                usage={"provider": "openai", "input_tokens": 10},
            ),
        },
    ]

    summary = summarize_activity_records([*records, records[1]])

    assert summary.last_activity_at == "2026-07-22T10:00:10+00:00"
    assert summary.latest_activity_class == "completed"
    assert summary.usage["input_tokens"] == 20
    assert summary.activity_counts == {
        AGENT_COMPLETED_RECORD_TYPE: 1,
        ACTIVITY_CHECKPOINT_RECORD_TYPE: 1,
    }


def test_projection_timestamp_tie_uses_later_journal_record() -> None:
    timestamp = "2026-07-22T10:30:00Z"
    tracker = AgentActivityTracker()
    started = tracker.observe_line(
        json.dumps(
            {
                "type": "item.started",
                "timestamp": timestamp,
                "item": {"type": "command_execution", "id": "tool-tie"},
            }
        ),
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=1,
    )[0]
    completed = tracker.observe_line(
        json.dumps(
            {"type": "turn.completed", "turn_id": "tie", "timestamp": timestamp}
        ),
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=2,
    )[0]
    records = [
        {
            "record_type": started.record_type,
            **started.to_payload(
                provider="openai", usage={"provider": "openai", "input_tokens": 1}
            ),
        },
        {
            "record_type": completed.record_type,
            **completed.to_payload(
                provider="openai", usage={"provider": "openai", "input_tokens": 2}
            ),
        },
    ]

    summary = summarize_activity_records(records)

    assert summary.latest_activity_class == "completed"
    assert summary.usage["input_tokens"] == 2


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


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"turn.started","integer":' + ("9" * 5000) + "}",
        ("[" * 2000) + "0" + ("]" * 2000),
    ],
)
def test_json_resource_adversaries_are_ignored_by_stream_observer(line: str) -> None:
    observer = AgentOutputObserver("openai", source_generation=SOURCE_GENERATION_A)

    assert bounded_json_envelope(line) is None
    assert observer.observe_line(line, "stdout", source_position=1) is None
    assert observer.observation.empty


def test_stream_drain_survives_json_resource_adversaries(tmp_path: Path) -> None:
    script = tmp_path / "emit_adversarial_json.py"
    script.write_text(
        "print('{\"type\":\"turn.started\",\"integer\":' + '9' * 5000 + '}')\n"
        "print('[' * 2000 + '0' + ']' * 2000)\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = run_streaming_command(
            f"{sys.executable} {script.name}",
            tmp_path,
            log,
            env={"VIBE_LOOP_FENCING_TOKEN": "fencing-test-value"},
            provider="openai",
        )

    assert result.exit_code == 0
    assert result.runtime_context.empty
    assert result.session_id is None
    logged = log_path.read_text(encoding="utf-8")
    assert '"integer":' + ("9" * 5000) in logged
    assert ("[" * 2000) + "0" + ("]" * 2000) in logged


def test_stream_restart_assigns_a_new_process_generation(tmp_path: Path) -> None:
    script = tmp_path / "emit_idless_turn.py"
    script.write_text(
        'print(\'{"type":"turn.started"}\')\nprint(\'{"type":"turn.completed"}\')\n',
        encoding="utf-8",
    )
    process_events: list[list[dict[str, object]]] = []
    for process_index in range(2):
        events: list[dict[str, object]] = []

        def capture(observation: AgentRuntimeObservation) -> None:
            for emission in observation.activity_emissions:
                events.extend(
                    emission.to_payload(provider="openai", usage={})["activity_events"]
                )

        with (tmp_path / f"process-{process_index}.log").open(
            "w", encoding="utf-8"
        ) as log:
            result = run_streaming_command(
                f"{sys.executable} {script.name}",
                tmp_path,
                log,
                env={"VIBE_LOOP_RUN_ID": "run-restart"},
                provider="openai",
                on_observation=capture,
            )
        assert result.exit_code == 0
        process_events.append(events)

    first_generations = {event["source_generation"] for event in process_events[0]}
    second_generations = {event["source_generation"] for event in process_events[1]}
    assert len(first_generations) == 1
    assert len(second_generations) == 1
    assert first_generations.isdisjoint(second_generations)
    assert [event["source_position"] for event in process_events[0]] == [1, 2]
    assert {event["id"] for event in process_events[0]}.isdisjoint(
        event["id"] for event in process_events[1]
    )


def test_summary_derives_phase_durations_and_latest_authoritative_usage() -> None:
    tracker = AgentActivityTracker()
    emission = tracker.observe_line(
        '{"type":"turn.completed","turn_id":"t1","timestamp":"2026-07-22T07:00:20Z"}',
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=1,
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
        '{"type":"turn.completed","turn_id":"t1","timestamp":"2026-07-22T08:00:00Z"}',
        source_generation=SOURCE_GENERATION_A,
        source_stream="stdout",
        source_position=1,
    )[0]
    usage = {
        "provider": "openai",
        "usage_source": "native:codex:turn.completed",
        "usage_version": "codex-jsonl-v1",
        "input_tokens": 7,
        "output_tokens": 2,
    }
    command_context = parse_agent_runtime_context_from_command(
        "codex exec --model gpt-5.4-codex --reasoning-effort high"
    )
    observer = AgentOutputObserver("openai", source_generation=SOURCE_GENERATION_A)
    assert (
        observer.observe_line(
            "Final summary: model=gpt-9.9 effort=xhigh reasoning_effort=minimal",
            "stdout",
            source_position=1,
        )
        is None
    )
    effective_context = command_context.prefer(observer.observation.runtime_context)
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
            model_provider=effective_context.model_provider,
            model_provider_source=effective_context.model_provider_source,
            model_id=effective_context.model_id,
            model_id_source=effective_context.model_id_source,
            reasoning_effort=effective_context.reasoning_effort,
            reasoning_effort_source=effective_context.reasoning_effort_source,
        )
    )

    inspection = store.inspect_run("run-1")

    assert inspection is not None
    payload = inspection.to_json()
    assert payload["last_activity_at"] == "2026-07-22T08:00:00+00:00"
    assert payload["usage"]["input_tokens"] == 7
    assert payload["model_id"] == "gpt-5.4-codex"
    assert payload["model_id_source"] == "command_arg:--model"
    assert payload["reasoning_effort"] == "high"
    assert payload["reasoning_effort_source"] == "command_arg:--reasoning-effort"

    active = ActiveRunState.new(
        task_id="TASK-01",
        run_id="run-1",
        log_path=tmp_path / "run.log",
        base_main="a",
        command="configured worker",
        model_provider=effective_context.model_provider,
        model_provider_source=effective_context.model_provider_source,
        model_id=effective_context.model_id,
        model_id_source=effective_context.model_id_source,
        reasoning_effort=effective_context.reasoning_effort,
        reasoning_effort_source=effective_context.reasoning_effort_source,
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
