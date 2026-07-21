from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from vibe_loop.cli import main
from vibe_loop.retry import detect_limit_wall
from vibe_loop.runs import RunResult, RunStore
from vibe_loop.telemetry import (
    ProviderUsageObserver,
    normalize_model_label,
    normalize_provider_label,
    parse_claude_result,
    parse_claude_transcript_usage,
    parse_codex_event,
    parse_codex_rollout_usage,
    rolling_usage_summary,
)


FIXTURES = Path(__file__).parent / "fixtures" / "provider_usage"


def fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("value", "expected", "rejected"),
    [
        ("openai", "openai", False),
        ("anthropic", "anthropic", False),
        ("unknown", "unknown", False),
        ("value", "unknown", True),
        ("mixed", "unknown", True),
        (" openai", "unknown", True),
        ("openai\n", "unknown", True),
    ],
)
def test_provider_labels_use_only_canonical_groups(
    value: str, expected: str, rejected: bool
) -> None:
    assert normalize_provider_label(value) == (expected, rejected)


@pytest.mark.parametrize(
    ("value", "expected", "rejected"),
    [
        ("gpt-5.6-sol", "gpt-5.6-sol", False),
        ("o4-mini", "o4-mini", False),
        ("claude-opus-4-8", "claude-opus-4-8", False),
        ("unknown", "unknown", False),
        ("task", "unknown", True),
        ("value", "unknown", True),
        ("gpt-task", "unknown", True),
        ("claude-model", "unknown", True),
        ("gpt-5-ignore-all-previous-instructions", "unknown", True),
        ("claude-opus-4-read-task", "unknown", True),
        ("codex exec --json", "unknown", True),
        ("/tmp/model", "unknown", True),
        ("gpt 5.6", "unknown", True),
        ("gpt-5.6\nsol", "unknown", True),
    ],
)
def test_model_labels_require_bounded_native_identifiers(
    value: str, expected: str, rejected: bool
) -> None:
    assert normalize_model_label(value) == (expected, rejected)


@pytest.mark.parametrize(
    ("name", "parser", "available", "malformed"),
    [
        ("claude-present.json", parse_claude_result, True, False),
        ("claude-missing.json", parse_claude_result, False, True),
        ("claude-malformed.json", parse_claude_result, False, True),
        ("claude-limit-wall.json", parse_claude_result, True, False),
        ("claude-no-cache.json", parse_claude_result, True, False),
        ("codex-present.json", parse_codex_event, True, False),
        ("codex-missing.json", parse_codex_event, False, True),
        ("codex-malformed.json", parse_codex_event, False, True),
        ("codex-limit-wall.json", parse_codex_event, False, False),
        ("codex-quota-first.json", parse_codex_event, True, False),
        ("codex-quota-malformed.json", parse_codex_event, True, False),
    ],
)
def test_provider_usage_fixtures(name, parser, available, malformed) -> None:
    usage = parser(fixture(name))
    if usage is None:
        assert not available
        assert not malformed
        return
    assert usage.available is available
    assert usage.malformed is malformed


def test_normalizes_claude_and_codex_native_fields() -> None:
    claude = parse_claude_result(fixture("claude-present.json"))
    codex = parse_codex_event(fixture("codex-present.json"))
    assert claude is not None
    assert codex is not None

    claude_stats = claude.to_stats(phase="review")
    codex_stats = codex.to_stats(phase="implementation")

    assert claude_stats == {
        "schema_version": 2,
        "phase": "review",
        "usage_source": "native:claude:result",
        "usage_version": "claude-result-v1",
        "provider": "anthropic",
        "session_continuation": False,
        "flexible_provider": False,
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 500,
        "total_tokens": 1500,
        "turns": 3,
        "duration_seconds": 12.5,
        "cost_usd": 0.42,
        "provider_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 500,
            "num_turns": 3,
            "duration_ms": 12500,
            "duration_api_ms": 11750,
            "total_cost_usd": 0.42,
        },
        "quota_evidence_available": False,
        "quota_unavailable_reason": "quota_snapshot_not_reported",
    }
    assert codex_stats["input_tokens"] == 24763
    assert codex_stats["cached_input_tokens"] == 24448
    assert codex_stats["output_tokens"] == 122
    assert codex_stats["total_tokens"] == 24885
    assert codex_stats["provider_usage"] == {
        "input_tokens": 24763,
        "cached_input_tokens": 24448,
        "output_tokens": 122,
        "reasoning_output_tokens": 17,
        "turns": 1,
    }


@pytest.mark.parametrize("name", ["claude-limit-wall.json", "codex-limit-wall.json"])
def test_provider_limit_wall_fixtures_remain_typed(name: str) -> None:
    signal = detect_limit_wall(json.dumps(fixture(name)))
    assert signal is not None
    assert "usage limit" in signal.marker


def test_usage_stats_never_persist_sensitive_payload_text() -> None:
    observer = ProviderUsageObserver("openai")
    canaries = {
        "prompt": "PRIVATE PROMPT CANARY",
        "credential": "sk-secret-canary",
        "fencing_token": "FENCING CANARY",
        "raw_transcript": "TRANSCRIPT CANARY",
    }
    observer.observe_line(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    **canaries,
                },
                **canaries,
            }
        )
    )

    encoded = json.dumps(observer.usage.to_stats(phase="implementation"))
    assert "PRIVATE PROMPT" not in encoded
    assert "sk-secret" not in encoded
    assert "FENCING CANARY" not in encoded
    assert "TRANSCRIPT CANARY" not in encoded
    assert "prompt" not in encoded
    assert "credential" not in encoded
    assert "fencing_token" not in encoded
    assert "raw_transcript" not in encoded


def test_codex_quota_snapshot_keeps_only_bounded_window_evidence() -> None:
    usage = parse_codex_event(fixture("codex-quota-first.json"))
    assert usage is not None

    stats = usage.to_stats(phase="implementation")

    assert stats["input_tokens"] == 1500
    assert stats["cached_input_tokens"] == 1200
    assert stats["output_tokens"] == 80
    assert stats["quota_evidence_available"] is True
    assert stats["quota_snapshots"] == [
        {
            "provider": "openai",
            "scope": "codex",
            "window": "primary",
            "observed_at": "2026-07-21T10:00:00+00:00",
            "used_percent": 20.0,
            "window_minutes": 300,
            "resets_at": 1784642400,
        },
        {
            "provider": "openai",
            "scope": "codex",
            "window": "secondary",
            "observed_at": "2026-07-21T10:00:00+00:00",
            "used_percent": 40.0,
            "window_minutes": 10080,
            "resets_at": 1785247200,
        },
    ]
    encoded = json.dumps(stats)
    assert "plan_type" not in encoded
    assert "credits" not in encoded
    assert "SECRET CREDIT" not in encoded


def test_malformed_quota_snapshot_is_unavailable_and_redacted() -> None:
    usage = parse_codex_event(fixture("codex-quota-malformed.json"))
    assert usage is not None

    encoded = json.dumps(usage.to_stats(phase="review"))

    assert '"quota_evidence_available": false' in encoded
    assert "malformed_quota_snapshot" in encoded
    for canary in (
        "PROMPT CANARY",
        "sk-secret-canary",
        "FENCING CANARY",
        "PRIVATE COMMAND",
        "RAW TRANSCRIPT",
    ):
        assert canary not in encoded


def test_provider_usage_without_cache_fields_keeps_fresh_input() -> None:
    usage = parse_claude_result(fixture("claude-no-cache.json"))
    assert usage is not None

    stats = usage.to_stats(phase="implementation")

    assert stats["input_tokens"] == 125
    assert "cache_read_input_tokens" not in stats
    assert "cache_creation_input_tokens" not in stats


def test_codex_rollout_retains_same_run_snapshots_and_cumulative_usage(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(fixture(name))
            for name in ("codex-quota-first.json", "codex-quota-second.json")
        )
        + "\n",
        encoding="utf-8",
    )

    usage = parse_codex_rollout_usage(rollout)
    stats = usage.to_stats(phase="implementation")

    assert stats["input_tokens"] == 1700
    assert stats["cached_input_tokens"] == 1400
    assert stats["output_tokens"] == 100
    assert len(stats["quota_snapshots"]) == 4
    record = run_record(
        "rollout-run",
        "rollout-task",
        datetime(2026, 7, 21, 11, 0, tzinfo=UTC),
    )
    record["stats"] = stats
    summary = rolling_usage_summary(
        [record],
        project="demo",
        now=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    )
    forecasts = summary["quota_account_wall"]["providers"][0]["forecasts"]
    assert {item["window"] for item in forecasts} == {"primary", "secondary"}


def test_claude_resume_counts_only_records_appended_after_exact_offset(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    first = {
        "type": "assistant",
        "message": {
            "id": "first",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    }
    transcript.write_text(json.dumps(first) + "\n", encoding="utf-8")
    offset = transcript.stat().st_size
    second = {
        "type": "assistant",
        "message": {
            "id": "second",
            "usage": {"input_tokens": 20, "output_tokens": 5},
        },
    }
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second) + "\n")

    usage = parse_claude_transcript_usage(transcript, start_offset=offset)

    assert usage.values["input_tokens"] == 20
    assert usage.values["output_tokens"] == 5
    assert usage.values["total_tokens"] == 25
    assert usage.values["turns"] == 1


def test_claude_structured_and_transcript_totals_use_same_semantics(
    tmp_path: Path,
) -> None:
    native = fixture("claude-present.json")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"id": "message-1", "usage": native["usage"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    structured = parse_claude_result(native)
    fallback = parse_claude_transcript_usage(transcript)

    assert structured is not None
    assert structured.values["total_tokens"] == 1500
    assert fallback.values["total_tokens"] == 1500
    assert fallback.values["cache_read_input_tokens"] == 4000
    assert fallback.values["cache_creation_input_tokens"] == 500


def run_record(
    run_id: str,
    task_id: str,
    finished: datetime,
    *,
    provider: str = "openai",
    phase: str = "implementation",
    classification: str = "completed",
    duration: float = 60,
    total_tokens: int = 100,
    output_tokens: int = 10,
    cost: float = 1,
    session_id: str = "session",
    fingerprint: str = "candidate",
    changed_lines: int = 20,
    restart_count: int = 0,
    work_kind: str = "",
) -> dict[str, object]:
    stats: dict[str, object] = {
        "phase": phase,
        "total_tokens": total_tokens,
        "input_tokens": total_tokens - output_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "candidate_fingerprint": fingerprint,
        "changed_lines": changed_lines,
        "flexible_provider": True,
    }
    if work_kind or phase == "review":
        stats["work_kind"] = work_kind or "review"
    return {
        "record_type": "run_result",
        "run_id": run_id,
        "task_id": task_id,
        "classification": classification,
        "exit_code": 0 if classification == "completed" else 1,
        "started_at": (finished - timedelta(seconds=duration)).isoformat(),
        "finished_at": finished.isoformat(),
        "session_id": session_id,
        "model_provider": provider,
        "model_id": "model-1",
        "restart_count": restart_count,
        "stats": stats,
    }


def test_rolling_summary_groups_productivity_and_budget_diagnostics() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    records = [
        run_record(
            "r1",
            "task-a",
            now - timedelta(minutes=10),
            total_tokens=157974,
            output_tokens=51000,
            changed_lines=2,
            cost=11,
            phase="planning",
        ),
        run_record(
            "r2",
            "task-b",
            now - timedelta(minutes=5),
            classification="limit_wall",
            duration=5,
            session_id="review-session",
            phase="review",
        ),
        run_record(
            "r3",
            "task-b",
            now - timedelta(minutes=4),
            classification="failed",
            duration=4,
            session_id="review-session",
            phase="review",
        ),
        run_record(
            "r4",
            "task-b",
            now - timedelta(minutes=3),
            classification="failed",
            duration=4,
            provider="anthropic",
        ),
        run_record(
            "r5",
            "task-b",
            now - timedelta(minutes=2),
            classification="failed",
            duration=4,
            provider="anthropic",
        ),
    ]

    summary = rolling_usage_summary(
        records,
        project="demo",
        now=now,
        slice_token_threshold=100000,
    )

    assert {group["phase"] for group in summary["groups"]} == {
        "planning",
        "review",
        "implementation",
    }
    planning = next(
        group for group in summary["groups"] if group["phase"] == "planning"
    )
    assert planning["tasks_landed"] == 1
    assert planning["tokens_per_completed_task"] == 157974
    diagnostic_types = {item["type"] for item in summary["diagnostics"]}
    assert {
        "limit_wall",
        "rapid_provider_failures",
        "task_attempts",
        "planning_spend",
        "daily_output_tokens",
        "low_change_high_token",
        "same_session_review_resume",
        "repeated_failed_attempts",
        "flexible_provider_share",
    } <= diagnostic_types
    quota_providers = {
        item["provider"]: item for item in summary["quota_account_wall"]["providers"]
    }
    assert quota_providers["openai"]["account_wall_evidence_available"] is True
    assert (
        quota_providers["openai"]["account_wall_last_observed_at"]
        == (now - timedelta(minutes=5)).isoformat()
    )


def test_summary_counts_restart_events_not_cumulative_ordinals() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    records = [
        run_record("r0", "task-a", now - timedelta(minutes=3), restart_count=0),
        run_record("r1", "task-a", now - timedelta(minutes=2), restart_count=1),
        run_record("r2", "task-a", now - timedelta(minutes=1), restart_count=2),
    ]

    summary = rolling_usage_summary(records, project="demo", now=now)

    assert summary["groups"][0]["restarts"] == 2


def test_repeated_discovery_distinguishes_independent_sessions() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    records = [
        run_record(
            "r1",
            "task-a",
            now - timedelta(minutes=2),
            session_id="session-1",
            work_kind="discovery",
        ),
        run_record(
            "r2",
            "task-a",
            now - timedelta(minutes=1),
            session_id="session-2",
            work_kind="discovery",
        ),
    ]

    summary = rolling_usage_summary(records, project="demo", now=now)
    repeated = next(
        item
        for item in summary["diagnostics"]
        if item["type"] == "repeated_candidate_work"
    )

    assert repeated["work_kind"] == "discovery"
    assert repeated["independent_sessions"] == 2


def test_persisted_phase_and_work_kind_drive_summary(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.jsonl")
    now = datetime.now(UTC)
    for phase, work_kind in (("review", "review"), ("full_validation", "")):
        store.append_result(
            RunResult(
                run_id=f"run-{phase}",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=tmp_path / f"{phase}.log",
                start_main="aaa",
                end_main="bbb",
                started_at=(now - timedelta(seconds=60)).isoformat(),
                finished_at=now.isoformat(),
                model_provider="openai",
                model_id="gpt-5-codex",
                stats={
                    "schema_version": 1,
                    "phase": phase,
                    "work_kind": work_kind,
                    "usage_source": "native:provider",
                    "usage_version": "provider-usage-v1",
                    "provider": "openai",
                    "total_tokens": 10,
                    "candidate_fingerprint": "bbb",
                },
            )
        )

    records = RunStore(store.path).read_records()
    summary = rolling_usage_summary(records, project="demo", now=now)

    assert {group["phase"] for group in summary["groups"]} == {
        "review",
        "full_validation",
    }
    repeated = [
        item
        for item in summary["diagnostics"]
        if item["type"] in {"same_session_continuation", "repeated_candidate_work"}
    ]
    assert repeated == []


def test_planning_summary_preserves_model_and_worker_minutes() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    record = {
        "record_type": "autopilot_planning_outcome",
        "occurred_at": (now - timedelta(minutes=1)).isoformat(),
        "outcome": "productive",
        "provider_launched": True,
        "created_count": 2,
        "model_provider": "anthropic",
        "model_id": "claude-opus-4-8",
        "stats": {
            "phase": "planning",
            "wall_time_seconds": 120,
            "total_tokens": 50,
            "cost_usd": 0.5,
        },
    }

    summary = rolling_usage_summary([record], project="demo", now=now)
    group = summary["groups"][0]

    assert group["provider"] == "anthropic"
    assert group["model"] == "claude-opus-4-8"
    assert group["worker_minutes"] == 2


def test_legacy_malformed_attribution_projects_safely_without_rewrite() -> None:
    record = fixture("legacy-malformed-attribution.json")
    original = copy.deepcopy(record)
    now = datetime(2026, 7, 21, 12, 5, tzinfo=UTC)

    summary = rolling_usage_summary([record], project="vibe-loop", now=now)

    assert record == original
    assert len(summary["groups"]) == 1
    group = summary["groups"][0]
    assert (group["provider"], group["model"]) == ("unknown", "unknown")
    assert group["input_tokens"] == 1200
    assert group["cache_read_input_tokens"] == 4000
    assert group["total_tokens"] == 1500
    quota = summary["quota_account_wall"]
    assert [item["provider"] for item in quota["providers"]] == ["unknown"]
    assert quota["providers"][0]["gross_tokens"] == 6000
    attribution = {
        item["field"]: item
        for item in summary["diagnostics"]
        if item["type"] == "invalid_attribution_label"
    }
    assert attribution == {
        "model": {
            "type": "invalid_attribution_label",
            "severity": "warning",
            "field": "model",
            "count": 1,
            "normalized": "unknown",
        },
        "provider": {
            "type": "invalid_attribution_label",
            "severity": "warning",
            "field": "provider",
            "count": 1,
            "normalized": "unknown",
        },
    }
    assert "value" not in json.dumps(attribution)
    assert "task" not in json.dumps(attribution)


def test_invalid_providers_cannot_create_usage_or_quota_groups() -> None:
    now = datetime(2026, 7, 21, 12, 5, tzinfo=UTC)
    records = []
    for index, provider in enumerate(("value", "mixed", "/tmp/provider")):
        record = fixture("legacy-malformed-attribution.json")
        record["run_id"] = f"invalid-{index}"
        record["model_provider"] = provider
        record["model_id"] = "gpt-5.6-sol"
        record["finished_at"] = (now - timedelta(minutes=index + 1)).isoformat()
        if index == 0:
            stats = record["stats"]
            assert isinstance(stats, dict)
            stats["quota_snapshots"] = [
                {
                    "provider": "mixed",
                    "scope": "account",
                    "window": "primary",
                    "observed_at": record["finished_at"],
                    "used_percent": 50,
                    "window_minutes": 300,
                    "resets_at": 1784642400,
                }
            ]
        records.append(record)

    summary = rolling_usage_summary(records, project="vibe-loop", now=now)

    assert {group["provider"] for group in summary["groups"]} == {"unknown"}
    assert {
        provider["provider"] for provider in summary["quota_account_wall"]["providers"]
    } == {"unknown"}
    assert summary["quota_account_wall"]["providers"][0]["snapshots"] == []
    diagnostic = next(
        item
        for item in summary["diagnostics"]
        if item.get("type") == "invalid_attribution_label"
        and item.get("field") == "provider"
    )
    assert diagnostic["count"] == 3


def test_quota_summary_forecasts_only_comparable_provider_windows() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    records: list[dict[str, object]] = []
    for index, name in enumerate(
        (
            "codex-quota-first.json",
            "codex-quota-second.json",
            "codex-quota-reset-change.json",
        )
    ):
        usage = parse_codex_event(fixture(name))
        assert usage is not None
        finished = datetime.fromisoformat(
            str(fixture(name)["timestamp"]).replace("Z", "+00:00")
        )
        record = run_record(
            f"openai-{index}",
            "task-openai",
            finished,
            classification="completed" if index < 2 else "failed",
        )
        record["stats"] = usage.to_stats(phase="implementation")
        records.append(record)

    claude = parse_claude_result(fixture("claude-present.json"))
    assert claude is not None
    anthropic = run_record(
        "anthropic-1",
        "task-anthropic",
        now - timedelta(minutes=5),
        provider="anthropic",
    )
    anthropic["stats"] = claude.to_stats(phase="planning")
    records.append(anthropic)

    summary = rolling_usage_summary(records, project="demo", now=now)
    quota = summary["quota_account_wall"]
    assert quota["evidence_available"] is True
    providers = {item["provider"]: item for item in quota["providers"]}

    openai = providers["openai"]
    assert openai["fresh_input_tokens"] == 700
    assert openai["cache_read_tokens"] == 3200
    assert openai["cache_create_tokens"] == 0
    assert openai["gross_tokens"] == 4110
    assert openai["landed_tasks"] == 1
    assert openai["gross_usage_per_landed_task"] == 4110
    assert openai["fresh_input_per_landed_task"] == 700
    assert openai["activity"] == {"implementation": 3, "failed_attempt": 1}
    assert {
        (forecast["window"], forecast["resets_at"]) for forecast in openai["forecasts"]
    } == {("primary", 1784642400), ("secondary", 1785247200)}
    primary = next(item for item in openai["forecasts"] if item["window"] == "primary")
    assert primary["burn_rate_percent_per_hour"] == 10
    assert primary["exhaustion_before_reset"] is False

    anthropic_group = providers["anthropic"]
    assert anthropic_group["fresh_input_tokens"] == 1200
    assert anthropic_group["cache_read_tokens"] == 4000
    assert anthropic_group["cache_create_tokens"] == 500
    assert anthropic_group["gross_tokens"] == 6000
    assert anthropic_group["quota_evidence_available"] is False
    assert anthropic_group["quota_unavailable_reason"] == (
        "quota_snapshot_not_reported"
    )
    assert anthropic_group["forecasts"] == []


def test_review_diagnostics_separate_resume_from_new_session_rereview() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    resume_first = run_record(
        "resume-1",
        "task-resume",
        now - timedelta(minutes=4),
        phase="review",
        session_id="review-session",
        fingerprint="candidate-resume",
    )
    resume_second = run_record(
        "resume-2",
        "task-resume",
        now - timedelta(minutes=3),
        phase="review",
        session_id="review-session",
        fingerprint="candidate-resume",
    )
    resume_stats = resume_second["stats"]
    assert isinstance(resume_stats, dict)
    resume_stats["session_continuation"] = True
    rereview_first = run_record(
        "rereview-1",
        "task-rereview",
        now - timedelta(minutes=2),
        phase="review",
        session_id="review-session-a",
        fingerprint="candidate-rereview",
    )
    rereview_second = run_record(
        "rereview-2",
        "task-rereview",
        now - timedelta(minutes=1),
        phase="review",
        session_id="review-session-b",
        fingerprint="candidate-rereview",
    )

    summary = rolling_usage_summary(
        [resume_first, resume_second, rereview_first, rereview_second],
        project="demo",
        now=now,
    )

    diagnostics = {item["type"]: item for item in summary["diagnostics"]}
    assert diagnostics["same_session_review_resume"]["avoidable_burn"] is False
    assert diagnostics["new_session_rereview"]["avoidable_burn"] is True
    provider = summary["quota_account_wall"]["providers"][0]
    assert provider["activity"]["review"] == 3
    assert provider["activity"]["resumed_review"] == 1


def test_runs_summary_cli_exposes_json(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "demo"
    runs_path = repo / ".vibe-loop" / "runs.jsonl"
    runs_path.parent.mkdir(parents=True)
    record = run_record(
        "run-1",
        "task-1",
        datetime.now(UTC) - timedelta(minutes=1),
        total_tokens=120,
    )
    runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    exit_code = main(["runs", "summary", "--repo", str(repo), "--json"])
    output = capsys.readouterr()

    assert exit_code == 0
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["project"] == "demo"
    assert payload["groups"][0]["total_tokens"] == 120
    assert (
        payload["quota_account_wall"]["providers"][0]["quota_evidence_available"]
        is False
    )


def test_runs_summary_text_separates_quota_account_wall(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "demo"
    runs_path = repo / ".vibe-loop" / "runs.jsonl"
    runs_path.parent.mkdir(parents=True)
    record = run_record(
        "run-1",
        "task-1",
        datetime.now(UTC) - timedelta(minutes=1),
    )
    runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    exit_code = main(["runs", "summary", "--repo", str(repo)])
    output = capsys.readouterr()

    assert exit_code == 0
    assert output.err == ""
    assert "quota/account-wall: evidence_available=false" in output.out
    assert "quota_unavailable_reason=quota_snapshot_not_reported" in output.out


class TelemetryUnittestCoverage(unittest.TestCase):
    def test_provider_usage_fixture_matrix(self) -> None:
        cases = (
            ("claude-present.json", parse_claude_result, True, False),
            ("claude-missing.json", parse_claude_result, False, True),
            ("claude-malformed.json", parse_claude_result, False, True),
            ("claude-limit-wall.json", parse_claude_result, True, False),
            ("claude-no-cache.json", parse_claude_result, True, False),
            ("codex-present.json", parse_codex_event, True, False),
            ("codex-missing.json", parse_codex_event, False, True),
            ("codex-malformed.json", parse_codex_event, False, True),
            ("codex-limit-wall.json", parse_codex_event, False, False),
            ("codex-quota-first.json", parse_codex_event, True, False),
            ("codex-quota-malformed.json", parse_codex_event, True, False),
        )
        for case in cases:
            with self.subTest(fixture=case[0]):
                test_provider_usage_fixtures(*case)

    def test_native_normalization_and_limit_walls(self) -> None:
        test_normalizes_claude_and_codex_native_fields()
        for name in ("claude-limit-wall.json", "codex-limit-wall.json"):
            with self.subTest(fixture=name):
                test_provider_limit_wall_fixtures_remain_typed(name)

    def test_usage_stats_redact_sensitive_payloads(self) -> None:
        test_usage_stats_never_persist_sensitive_payload_text()
        test_codex_quota_snapshot_keeps_only_bounded_window_evidence()
        test_malformed_quota_snapshot_is_unavailable_and_redacted()
        test_provider_usage_without_cache_fields_keeps_fresh_input()
        with tempfile.TemporaryDirectory() as directory:
            test_codex_rollout_retains_same_run_snapshots_and_cumulative_usage(
                Path(directory)
            )

    def test_resume_usage_uses_appended_records_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_claude_resume_counts_only_records_appended_after_exact_offset(
                Path(directory)
            )

    def test_claude_total_semantics_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_claude_structured_and_transcript_totals_use_same_semantics(
                Path(directory)
            )

    def test_rolling_summary_diagnostics(self) -> None:
        test_rolling_summary_groups_productivity_and_budget_diagnostics()
        test_summary_counts_restart_events_not_cumulative_ordinals()
        test_repeated_discovery_distinguishes_independent_sessions()
        test_planning_summary_preserves_model_and_worker_minutes()
        test_quota_summary_forecasts_only_comparable_provider_windows()
        test_review_diagnostics_separate_resume_from_new_session_rereview()

    def test_persisted_phase_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_persisted_phase_and_work_kind_drive_summary(Path(directory))

    def test_runs_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            runs_path = repo / ".vibe-loop" / "runs.jsonl"
            runs_path.parent.mkdir(parents=True)
            record = run_record(
                "run-1",
                "task-1",
                datetime.now(UTC) - timedelta(minutes=1),
                total_tokens=120,
            )
            runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["runs", "summary", "--repo", str(repo), "--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["project"], "demo")
        self.assertEqual(payload["groups"][0]["total_tokens"], 120)

    def test_cached_and_fresh_input_summary(self) -> None:
        test_rolling_summary_separates_cached_from_fresh_input_tokens()

    def test_high_token_diagnostic_evidence(self) -> None:
        test_low_change_high_token_reports_raw_and_non_cached_evidence()

    def test_non_cached_input_clamp(self) -> None:
        test_non_cached_input_clamps_inconsistent_provider_reports()


def codex_amplification_record() -> dict[str, object]:
    """Shaped from run 20260720T214201Z-hyphen-adjacent-generation-redaction-3d23bf62:
    a Codex run that burned 1M input tokens, almost entirely cache reads, while
    changing no mainline lines."""
    finished = datetime(2026, 7, 20, 21, 46, tzinfo=UTC)
    return {
        "record_type": "run_result",
        "run_id": "20260720T214201Z-hyphen-adjacent-generation-redaction-3d23bf62",
        "task_id": "hyphen-adjacent-generation-redaction",
        "classification": "blocked",
        "started_at": (finished - timedelta(seconds=234.6)).isoformat(),
        "finished_at": finished.isoformat(),
        "model_provider": "openai",
        "model_id": "gpt-5.6-sol",
        "stats": {
            "phase": "implementation",
            "input_tokens": 1_033_913,
            "cached_input_tokens": 977_152,
            "output_tokens": 6_608,
            "reasoning_output_tokens": 4_096,
            "total_tokens": 1_040_521,
            "changed_lines": 0,
        },
    }


def test_rolling_summary_separates_cached_from_fresh_input_tokens() -> None:
    now = datetime(2026, 7, 20, 22, 0, tzinfo=UTC)

    summary = rolling_usage_summary(
        [codex_amplification_record()], project="vibe-loop", hours=2, now=now
    )

    group = summary["groups"][0]
    assert group["provider"] == "openai"
    assert group["model"] == "gpt-5.6-sol"
    assert group["input_tokens"] == 1_033_913
    assert group["cached_input_tokens"] == 977_152
    assert group["non_cached_input_tokens"] == 56_761
    assert group["output_tokens"] == 6_608
    assert group["reasoning_output_tokens"] == 4_096


def test_low_change_high_token_reports_raw_and_non_cached_evidence() -> None:
    now = datetime(2026, 7, 20, 22, 0, tzinfo=UTC)

    summary = rolling_usage_summary(
        [codex_amplification_record()], project="vibe-loop", hours=2, now=now
    )

    diagnostic = next(
        item
        for item in summary["diagnostics"]
        if item["type"] == "low_change_high_token"
    )
    assert diagnostic["total_tokens"] == 1_040_521
    assert diagnostic["input_tokens"] == 1_033_913
    assert diagnostic["cached_input_tokens"] == 977_152
    assert diagnostic["non_cached_input_tokens"] == 56_761
    assert diagnostic["output_tokens"] == 6_608
    # The trigger still fires on raw totals; non-cached evidence is additive.
    assert diagnostic["threshold"] == 100_000
    assert diagnostic["threshold_metric"] == "total_tokens"


def test_non_cached_input_clamps_inconsistent_provider_reports() -> None:
    now = datetime(2026, 7, 20, 22, 0, tzinfo=UTC)
    record = codex_amplification_record()
    stats = record["stats"]
    assert isinstance(stats, dict)
    stats["cached_input_tokens"] = stats["input_tokens"] + 5_000

    summary = rolling_usage_summary([record], project="vibe-loop", hours=2, now=now)

    assert summary["groups"][0]["non_cached_input_tokens"] == 0
