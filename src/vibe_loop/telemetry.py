from __future__ import annotations

import dataclasses
import json
import math
import re
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping


USAGE_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
PHASES = frozenset(
    {
        "planning",
        "implementation",
        "focused_validation",
        "full_validation",
        "review",
        "remediation",
        "integration",
    }
)
WORK_KINDS = frozenset({"discovery", "review"})
NORMALIZED_NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_tokens",
    "turns",
    "duration_seconds",
    "cost_usd",
)
PROVIDER_NUMERIC_FIELDS = frozenset(
    {
        *NORMALIZED_NUMERIC_FIELDS,
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_output_tokens",
        "duration_ms",
        "duration_api_ms",
        "num_turns",
        "total_cost_usd",
    }
)
SAFE_METADATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._+/-]{0,159}$")
USAGE_SOURCES = frozenset(
    {
        "unavailable",
        "native:provider",
        "native:claude:result",
        "native:claude:transcript",
        "native:codex:turn.completed",
        "native:combined",
    }
)
USAGE_VERSIONS = frozenset(
    {
        "1",
        "claude-result-v1",
        "claude-transcript-v1",
        "codex-jsonl-v1",
        "provider-usage-v1",
    }
)
USAGE_PROVIDERS = frozenset({"anthropic", "openai", "mixed", "unknown"})
USAGE_UNAVAILABLE_REASONS = frozenset(
    {
        "provider_usage_not_reported",
        "malformed_provider_usage",
        "provider_transcript_unavailable",
    }
)
SENSITIVE_METADATA_MARKERS = (
    "credential",
    "fencing",
    "password",
    "prompt",
    "secret",
    "token",
    "transcript",
)


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or int(number) != number:
        return None
    return int(number)


def _numeric_mapping(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for key, candidate in value.items():
        if not isinstance(key, str) or key not in PROVIDER_NUMERIC_FIELDS:
            continue
        number = _number(candidate)
        if number is not None:
            result[key] = number
    return result


@dataclasses.dataclass(frozen=True)
class ProviderUsage:
    provider: str
    source: str
    version: str
    values: Mapping[str, int | float] = dataclasses.field(default_factory=dict)
    raw: Mapping[str, int | float] = dataclasses.field(default_factory=dict)
    unavailable_reason: str = ""
    malformed: bool = False

    @property
    def available(self) -> bool:
        return bool(self.values)

    def to_stats(
        self,
        *,
        phase: str,
        wall_time_seconds: float | None = None,
        candidate_fingerprint: str = "",
        continuation: bool = False,
        flexible_provider: bool = False,
        changed_lines: int | None = None,
        work_kind: str = "",
    ) -> dict[str, object]:
        stats: dict[str, object] = {
            "schema_version": USAGE_SCHEMA_VERSION,
            "phase": phase if phase in PHASES else "implementation",
            "usage_source": self.source,
            "usage_version": self.version,
            "provider": self.provider,
            "session_continuation": continuation,
            "flexible_provider": flexible_provider,
        }
        for key in NORMALIZED_NUMERIC_FIELDS:
            number = _number(self.values.get(key))
            if number is not None:
                stats[key] = number
        wall_time = _number(wall_time_seconds)
        if wall_time is not None:
            stats["wall_time_seconds"] = wall_time
        if self.raw:
            stats["provider_usage"] = _numeric_mapping(self.raw)
        reason = self.unavailable_reason
        if not self.available and not reason:
            reason = (
                "malformed_provider_usage"
                if self.malformed
                else "provider_usage_not_reported"
            )
        if reason:
            stats["usage_unavailable_reason"] = reason
        if candidate_fingerprint:
            stats["candidate_fingerprint"] = candidate_fingerprint[:160]
        if changed_lines is not None and changed_lines >= 0:
            stats["changed_lines"] = changed_lines
        if work_kind in WORK_KINDS:
            stats["work_kind"] = work_kind
        return stats


def unavailable_usage(provider: str, reason: str) -> ProviderUsage:
    return ProviderUsage(
        provider=provider or "unknown",
        source="unavailable",
        version="1",
        unavailable_reason=reason,
    )


def sanitize_run_stats(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (*NORMALIZED_NUMERIC_FIELDS, "wall_time_seconds", "changed_lines"):
        number = _number(value.get(key))
        if number is not None:
            result[key] = number
    schema_version = _integer(value.get("schema_version"))
    if schema_version is not None:
        result["schema_version"] = schema_version
    phase = value.get("phase")
    if isinstance(phase, str) and phase in PHASES:
        result["phase"] = phase
    work_kind = value.get("work_kind")
    if isinstance(work_kind, str) and work_kind in WORK_KINDS:
        result["work_kind"] = work_kind
    enumerated = {
        "usage_source": USAGE_SOURCES,
        "usage_version": USAGE_VERSIONS,
        "provider": USAGE_PROVIDERS,
        "usage_unavailable_reason": USAGE_UNAVAILABLE_REASONS,
    }
    for key, allowed in enumerated.items():
        metadata = value.get(key)
        if isinstance(metadata, str) and metadata in allowed:
            result[key] = metadata
    fingerprint = value.get("candidate_fingerprint")
    if (
        isinstance(fingerprint, str)
        and SAFE_METADATA_RE.fullmatch(fingerprint)
        and not any(
            marker in fingerprint.casefold() for marker in SENSITIVE_METADATA_MARKERS
        )
    ):
        result["candidate_fingerprint"] = fingerprint
    for key in ("session_continuation", "flexible_provider"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    provider_usage = _numeric_mapping(value.get("provider_usage"))
    if provider_usage:
        result["provider_usage"] = provider_usage
    return result


def merge_provider_usage(*items: ProviderUsage) -> ProviderUsage:
    available = [item for item in items if item.available]
    if not available:
        return (
            items[0]
            if items
            else unavailable_usage("unknown", "provider_usage_not_reported")
        )
    providers = {item.provider for item in available}
    provider = available[0].provider if len(providers) == 1 else "mixed"
    values: dict[str, int | float] = defaultdict(int)
    raw: dict[str, int | float] = defaultdict(int)
    for item in available:
        for key, value in item.values.items():
            values[key] += value
        for key, value in item.raw.items():
            raw[key] += value
    return ProviderUsage(
        provider=provider,
        source="native:combined",
        version="provider-usage-v1",
        values=dict(values),
        raw=dict(raw),
        malformed=any(item.malformed for item in items),
    )


def _normalized_usage(
    provider: str,
    source: str,
    version: str,
    raw_usage: object,
    *,
    extra: Mapping[str, object] | None = None,
) -> ProviderUsage:
    if not isinstance(raw_usage, Mapping):
        return ProviderUsage(provider, source, version, malformed=True)
    raw = _numeric_mapping(raw_usage)
    reported_usage = bool(raw)
    values: dict[str, int | float] = {}
    aliases = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cache_read_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens",
        "cache_read_tokens": "cache_read_input_tokens",
        "cache_creation_tokens": "cache_creation_input_tokens",
        "total_tokens": "total_tokens",
    }
    for raw_key, normalized_key in aliases.items():
        number = _number(raw_usage.get(raw_key))
        if number is not None and normalized_key not in values:
            values[normalized_key] = number
    if extra:
        raw.update(_numeric_mapping(extra))
        turns = _integer(extra.get("turns"))
        if turns is None:
            turns = _integer(extra.get("num_turns"))
        if turns is not None and reported_usage:
            values["turns"] = turns
        duration_ms = _number(extra.get("duration_ms"))
        if duration_ms is not None:
            values["duration_seconds"] = duration_ms / 1000
        cost = _number(extra.get("cost_usd"))
        if cost is None:
            cost = _number(extra.get("total_cost_usd"))
        if cost is not None:
            values["cost_usd"] = cost
    if "total_tokens" not in values:
        input_tokens = _number(values.get("input_tokens"))
        output_tokens = _number(values.get("output_tokens"))
        if input_tokens is not None or output_tokens is not None:
            values["total_tokens"] = (input_tokens or 0) + (output_tokens or 0)
    return ProviderUsage(
        provider,
        source,
        version,
        values,
        raw,
        malformed=bool(raw_usage) and not reported_usage,
    )


def parse_claude_result(payload: Mapping[str, object]) -> ProviderUsage | None:
    if payload.get("type") != "result":
        return None
    return _normalized_usage(
        "anthropic",
        "native:claude:result",
        "claude-result-v1",
        payload.get("usage"),
        extra={
            "num_turns": payload.get("num_turns"),
            "duration_ms": payload.get("duration_ms"),
            "duration_api_ms": payload.get("duration_api_ms"),
            "total_cost_usd": payload.get("total_cost_usd"),
        },
    )


def parse_codex_event(payload: Mapping[str, object]) -> ProviderUsage | None:
    event_type = payload.get("type")
    if event_type not in {"turn.completed", "turn_complete", "turn.completed.v1"}:
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        turn = payload.get("turn")
        usage = turn.get("usage") if isinstance(turn, Mapping) else usage
    return _normalized_usage(
        "openai",
        "native:codex:turn.completed",
        "codex-jsonl-v1",
        usage,
        extra={"turns": 1},
    )


class ProviderUsageObserver:
    def __init__(self, provider: str) -> None:
        self.provider = provider or "unknown"
        self._lock = threading.Lock()
        self._usage: ProviderUsage | None = None
        self._saw_malformed_usage = False

    @property
    def usage(self) -> ProviderUsage:
        with self._lock:
            if self._usage is not None:
                return self._usage
            if self._saw_malformed_usage:
                return ProviderUsage(
                    self.provider,
                    "native:provider",
                    "1",
                    malformed=True,
                )
            return unavailable_usage(self.provider, "provider_usage_not_reported")

    def observe_line(self, line: str) -> None:
        text = line.strip()
        if text.startswith("data:"):
            text = text.removeprefix("data:").strip()
        if not text.startswith("{"):
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        parsed = parse_claude_result(payload) or parse_codex_event(payload)
        if parsed is None:
            return
        with self._lock:
            self._saw_malformed_usage = self._saw_malformed_usage or parsed.malformed
            if parsed.available or self._usage is None:
                self._usage = parsed


def parse_claude_transcript_usage(
    path: Path,
    *,
    start_offset: int = 0,
) -> ProviderUsage:
    totals: dict[str, int | float] = defaultdict(int)
    raw_totals: dict[str, int | float] = defaultdict(int)
    message_ids: set[str] = set()
    malformed = False
    try:
        with path.open("rb") as transcript:
            if start_offset > 0:
                transcript.seek(start_offset)
            for raw_line in transcript:
                try:
                    payload = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed = True
                    continue
                if not isinstance(payload, dict) or payload.get("type") != "assistant":
                    continue
                message = payload.get("message")
                if not isinstance(message, Mapping):
                    continue
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    if message_id in message_ids:
                        continue
                    message_ids.add(message_id)
                usage = message.get("usage")
                parsed = _normalized_usage(
                    "anthropic",
                    "native:claude:transcript",
                    "claude-transcript-v1",
                    usage,
                )
                if parsed.malformed:
                    malformed = True
                    continue
                for key, value in parsed.values.items():
                    if key != "total_tokens":
                        totals[key] += value
                for key, value in parsed.raw.items():
                    raw_totals[key] += value
    except OSError:
        return unavailable_usage("anthropic", "provider_transcript_unavailable")
    if not raw_totals:
        return ProviderUsage(
            "anthropic",
            "native:claude:transcript",
            "claude-transcript-v1",
            unavailable_reason="provider_usage_not_reported",
            malformed=malformed,
        )
    totals["turns"] = len(message_ids) if message_ids else 1
    totals["total_tokens"] = totals.get("input_tokens", 0) + totals.get(
        "output_tokens", 0
    )
    return ProviderUsage(
        "anthropic",
        "native:claude:transcript",
        "claude-transcript-v1",
        dict(totals),
        dict(raw_totals),
        malformed=malformed,
    )


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def record_duration_seconds(record: Mapping[str, object]) -> float:
    stats = record.get("stats")
    if isinstance(stats, Mapping):
        duration = _number(stats.get("wall_time_seconds"))
        if duration is not None:
            return float(duration)
    started = parse_timestamp(record.get("started_at"))
    finished = parse_timestamp(record.get("finished_at"))
    if started is None or finished is None:
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def _metric(stats: object, key: str) -> float:
    if not isinstance(stats, Mapping):
        return 0.0
    value = _number(stats.get(key))
    return float(value) if value is not None else 0.0


def _group_identity(record: Mapping[str, object]) -> tuple[str, str, str]:
    stats = record.get("stats")
    stats_map = stats if isinstance(stats, Mapping) else {}
    provider = str(
        record.get("model_provider")
        or stats_map.get("provider")
        or {"codex": "openai", "claude": "anthropic"}.get(
            record.get("agent_kind"), "unknown"
        )
    )
    model = str(record.get("model_id") or "unknown")
    phase = str(stats_map.get("phase") or "implementation")
    if phase not in PHASES:
        phase = "implementation"
    return provider, model, phase


def rolling_usage_summary(
    records: Iterable[Mapping[str, object]],
    *,
    project: str,
    hours: float = 24.0,
    now: datetime | None = None,
    slice_token_threshold: int = 100_000,
) -> dict[str, object]:
    current = now or datetime.now(UTC)
    since = current - timedelta(hours=hours)
    recent = [
        record
        for record in records
        if (
            timestamp := parse_timestamp(
                record.get("finished_at") or record.get("occurred_at")
            )
        )
        is not None
        and since <= timestamp <= current
    ]
    groups: dict[tuple[str, str, str], dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []
    task_attempts: dict[str, list[tuple[datetime, Mapping[str, object]]]] = defaultdict(
        list
    )
    quick_failures: dict[str, list[tuple[datetime, Mapping[str, object]]]] = (
        defaultdict(list)
    )
    repeated: dict[tuple[str, str, str], list[Mapping[str, object]]] = defaultdict(list)
    flexible_starts: dict[str, int] = defaultdict(int)
    flexible_minutes: dict[str, float] = defaultdict(float)
    flexible_total_starts = 0
    flexible_total_minutes = 0.0
    landed_tasks: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for record in recent:
        if record.get("record_type") != "autopilot_planning_outcome":
            continue
        stats = record.get("stats")
        stats_map = stats if isinstance(stats, Mapping) else {}
        provider = str(
            record.get("model_provider") or stats_map.get("provider") or "unknown"
        )
        model = str(record.get("model_id") or "unknown")
        key = (provider, model, "planning")
        group = groups.setdefault(
            key,
            {
                "project": project,
                "provider": provider,
                "model": model,
                "phase": "planning",
                "launches": 0,
                "completed_runs": 0,
                "immediate_failures": 0,
                "restarts": 0,
                "worker_minutes": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": 0,
                "reported_cost_usd": 0.0,
                "tasks_created": 0,
                "tasks_landed": 0,
            },
        )
        if record.get("provider_launched") is not False:
            group["launches"] = int(group["launches"]) + 1
        if record.get("outcome") == "productive":
            group["completed_runs"] = int(group["completed_runs"]) + 1
        group["worker_minutes"] = float(group["worker_minutes"]) + (
            record_duration_seconds(record) / 60
        )
        group["tasks_created"] = int(group["tasks_created"]) + int(
            _integer(record.get("created_count")) or 0
        )
        for metric in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "total_tokens",
        ):
            group[metric] = int(group[metric]) + int(_metric(stats, metric))
        group["reported_cost_usd"] = float(group["reported_cost_usd"]) + _metric(
            stats, "cost_usd"
        )

    for record in recent:
        if record.get("record_type") not in {None, "run_result"}:
            continue
        provider, model, phase = _group_identity(record)
        key = (provider, model, phase)
        group = groups.setdefault(
            key,
            {
                "project": project,
                "provider": provider,
                "model": model,
                "phase": phase,
                "launches": 0,
                "completed_runs": 0,
                "immediate_failures": 0,
                "restarts": 0,
                "worker_minutes": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": 0,
                "reported_cost_usd": 0.0,
                "tasks_created": 0,
                "tasks_landed": 0,
            },
        )
        stats = record.get("stats")
        duration = record_duration_seconds(record)
        group["launches"] = int(group["launches"]) + 1
        group["worker_minutes"] = float(group["worker_minutes"]) + duration / 60
        completed = record.get("classification", record.get("status")) == "completed"
        if completed:
            group["completed_runs"] = int(group["completed_runs"]) + 1
            if task_id := str(record.get("task_id") or ""):
                landed_tasks[key].add(task_id)
        elif duration < 15:
            group["immediate_failures"] = int(group["immediate_failures"]) + 1
        if (_integer(record.get("restart_count")) or 0) > 0:
            group["restarts"] = int(group["restarts"]) + 1
        for metric in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "total_tokens",
        ):
            group[metric] = int(group[metric]) + int(_metric(stats, metric))
        group["reported_cost_usd"] = float(group["reported_cost_usd"]) + _metric(
            stats, "cost_usd"
        )
        timestamp = parse_timestamp(record.get("finished_at"))
        task_id = str(record.get("task_id") or "")
        if timestamp is not None and task_id:
            task_attempts[task_id].append((timestamp, record))
            if not completed and duration < 15:
                quick_failures[provider].append((timestamp, record))
        stats_map = stats if isinstance(stats, Mapping) else {}
        work_kind = stats_map.get("work_kind")
        if work_kind in WORK_KINDS:
            fingerprint = str(stats_map.get("candidate_fingerprint") or "")
            if fingerprint:
                repeated[(task_id, str(work_kind), fingerprint)].append(record)
        if stats_map.get("flexible_provider") is True and phase == "implementation":
            flexible_starts[provider] += 1
            flexible_minutes[provider] += duration / 60
            flexible_total_starts += 1
            flexible_total_minutes += duration / 60
        total_tokens = _metric(stats, "total_tokens")
        changed_lines = _integer(stats_map.get("changed_lines"))
        if (
            slice_token_threshold > 0
            and total_tokens >= slice_token_threshold
            and changed_lines is not None
            and changed_lines <= 10
        ):
            diagnostics.append(
                {
                    "type": "low_change_high_token",
                    "severity": "warning",
                    "run_id": record.get("run_id"),
                    "task_id": task_id,
                    "total_tokens": int(total_tokens),
                    "changed_lines": changed_lines,
                    "threshold": slice_token_threshold,
                }
            )
        if record.get("classification") == "limit_wall":
            diagnostics.append(
                {
                    "type": "limit_wall",
                    "severity": "warning",
                    "run_id": record.get("run_id"),
                    "task_id": task_id,
                    "provider": provider,
                }
            )

    for group in groups.values():
        key = (str(group["provider"]), str(group["model"]), str(group["phase"]))
        group["tasks_landed"] = len(landed_tasks[key])
        productive_tasks = int(group["tasks_landed"]) or int(group["tasks_created"])
        group["worker_minutes"] = round(float(group["worker_minutes"]), 3)
        group["reported_cost_usd"] = round(float(group["reported_cost_usd"]), 6)
        group["tokens_per_completed_task"] = (
            round(int(group["total_tokens"]) / productive_tasks, 3)
            if productive_tasks
            else None
        )
        group["cost_per_completed_task_usd"] = (
            round(float(group["reported_cost_usd"]) / productive_tasks, 6)
            if productive_tasks
            else None
        )

    for task_id, attempts in task_attempts.items():
        count = len(attempts)
        if count >= 3:
            diagnostics.append(
                {
                    "type": "task_attempts",
                    "severity": "critical" if count >= 4 else "warning",
                    "task_id": task_id,
                    "attempts": count,
                    "threshold": 4 if count >= 4 else 3,
                }
            )
    for provider, failures in quick_failures.items():
        failures.sort(key=lambda item: item[0])
        for index in range(1, len(failures)):
            if failures[index][0] - failures[index - 1][0] <= timedelta(minutes=15):
                diagnostics.append(
                    {
                        "type": "rapid_provider_failures",
                        "severity": "warning",
                        "provider": provider,
                        "count": 2,
                        "window_minutes": 15,
                    }
                )
                break
    for (task_id, work_kind, fingerprint), candidates in repeated.items():
        if len(candidates) < 2:
            continue
        sessions = {
            str(candidate.get("session_id"))
            for candidate in candidates
            if candidate.get("session_id")
        }
        independent_sessions = len(sessions) if sessions else len(candidates)
        diagnostic_type = (
            "same_session_continuation"
            if independent_sessions == 1
            else "repeated_candidate_work"
        )
        diagnostics.append(
            {
                "type": diagnostic_type,
                "severity": "info" if independent_sessions == 1 else "warning",
                "task_id": task_id,
                "work_kind": work_kind,
                "candidate_fingerprint": fingerprint,
                "launches": len(candidates),
                "independent_sessions": independent_sessions,
            }
        )
    planning_cost = sum(
        float(group["reported_cost_usd"])
        for group in groups.values()
        if group["phase"] == "planning"
    )
    if planning_cost > 10:
        diagnostics.append(
            {
                "type": "planning_spend",
                "severity": "warning",
                "reported_cost_usd": round(planning_cost, 6),
                "threshold_usd": 10,
            }
        )
    output_tokens = sum(int(group["output_tokens"]) for group in groups.values())
    if output_tokens > 50_000:
        diagnostics.append(
            {
                "type": "daily_output_tokens",
                "severity": "warning",
                "output_tokens": output_tokens,
                "threshold": 50_000,
            }
        )
    if flexible_total_starts:
        for provider, starts in sorted(flexible_starts.items()):
            share = starts / flexible_total_starts
            minute_share = (
                flexible_minutes[provider] / flexible_total_minutes
                if flexible_total_minutes
                else share
            )
            if share < 0.4 or share > 0.6 or minute_share < 0.4 or minute_share > 0.6:
                diagnostics.append(
                    {
                        "type": "flexible_provider_share",
                        "severity": "warning",
                        "provider": provider,
                        "implementation_start_share": round(share, 6),
                        "worker_minute_share": round(minute_share, 6),
                        "expected_range": [0.4, 0.6],
                    }
                )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "project": project,
        "window_hours": hours,
        "since": since.isoformat(),
        "until": current.isoformat(),
        "groups": [groups[key] for key in sorted(groups)],
        "diagnostics": diagnostics,
    }
