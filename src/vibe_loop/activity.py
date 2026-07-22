from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime


ACTIVITY_SCHEMA_VERSION = 1
AGENT_STARTED_RECORD_TYPE = "agent_started"
ACTIVITY_CHECKPOINT_RECORD_TYPE = "activity_checkpoint"
GATE_RESULT_RECORD_TYPE = "gate_result"
WORK_BLOCKED_RECORD_TYPE = "work_blocked"
AGENT_COMPLETED_RECORD_TYPE = "agent_completed"
ACTIVITY_RECORD_TYPES = frozenset(
    {
        AGENT_STARTED_RECORD_TYPE,
        ACTIVITY_CHECKPOINT_RECORD_TYPE,
        GATE_RESULT_RECORD_TYPE,
        WORK_BLOCKED_RECORD_TYPE,
        AGENT_COMPLETED_RECORD_TYPE,
    }
)
ACTIVITY_CLASSES = frozenset(
    {
        "agent",
        "reasoning",
        "tool_started",
        "tool_completed",
        "blocked",
        "completed",
    }
)
MAX_STREAM_ENVELOPE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_CONTAINER_ITEMS = 256
MAX_JSON_NODES = 1024
MAX_ACTIVITY_COUNTER = 1_000_000
CHECKPOINT_EVENT_INTERVAL = 32
CHECKPOINT_SECONDS = 5.0
PHASE_CLASSES = frozenset(
    {
        "activation",
        "workspace",
        "implementing",
        "candidate",
        "gates",
        "review",
        "remediation",
        "closure",
        "integration",
        "provenance",
        "classification",
        "finalization",
    }
)

_CLAUDE_TOOL_START_TYPES = frozenset({"tool_use", "server_tool_use"})
_CLAUDE_TOOL_RESULT_TYPES = frozenset({"tool_result", "web_search_tool_result"})
_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "local_shell_call",
        "custom_tool_call",
        "mcp_tool_call",
        "command_execution",
        "file_change",
        "patch_apply",
        "web_search",
    }
)
_CODEX_ITEM_START_TYPES = frozenset({"item.started", "response.output_item.added"})
_CODEX_ITEM_COMPLETE_TYPES = frozenset({"item.completed", "response.output_item.done"})
_CODEX_TOOL_START_TYPES = frozenset(
    {
        "function_call",
        "local_shell_call",
        "custom_tool_call",
        "mcp_tool_call",
        "exec_command_begin",
        "command_execution",
        "patch_apply_begin",
        "apply_patch",
        "web_search_call",
        "file_change",
    }
)
_CODEX_TOOL_COMPLETE_TYPES = frozenset({"exec_command_end", "patch_apply_end"})


@dataclasses.dataclass(frozen=True)
class NativeActivity:
    record_type: str
    activity_class: str
    event_id: str
    observed_at: str
    reason_class: str = ""


@dataclasses.dataclass(frozen=True)
class ActivityEmission:
    record_type: str
    activity_id: str
    observed_at: str
    activity_class: str
    activity_delta: Mapping[str, int]
    activity_counts: Mapping[str, int]
    coalesced_count: int
    activity_events: tuple[tuple[str, str], ...]
    reason_class: str = ""

    def to_payload(
        self, *, provider: str, usage: Mapping[str, object]
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "activity_schema_version": ACTIVITY_SCHEMA_VERSION,
            "activity_id": self.activity_id,
            "observed_at": self.observed_at,
            "activity_class": self.activity_class,
            "activity_delta": dict(self.activity_delta),
            "activity_counts": dict(self.activity_counts),
            "coalesced_count": self.coalesced_count,
            "activity_events": [
                {"id": event_id, "record_type": record_type}
                for event_id, record_type in self.activity_events
            ],
            "provider": provider if provider in {"anthropic", "openai"} else "unknown",
        }
        if self.reason_class:
            payload["reason_class"] = self.reason_class
        if usage:
            payload["usage"] = _bounded_usage(usage)
        return payload


@dataclasses.dataclass(frozen=True)
class ActivitySummary:
    last_activity_at: str = ""
    latest_activity_class: str = ""
    activity_classes: tuple[str, ...] = ()
    activity_counts: Mapping[str, int] = dataclasses.field(default_factory=dict)
    activity_record_count: int = 0
    usage: Mapping[str, object] = dataclasses.field(default_factory=dict)
    phase_durations: Mapping[str, float] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "last_activity_at": self.last_activity_at,
            "latest_activity_class": self.latest_activity_class,
            "activity_classes": list(self.activity_classes),
            "activity_counts": dict(self.activity_counts),
            "activity_record_count": self.activity_record_count,
            "usage": dict(self.usage),
            "phase_durations": dict(self.phase_durations),
        }


class AgentActivityTracker:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic = monotonic
        self._wallclock = wallclock
        self._counts = {record_type: 0 for record_type in ACTIVITY_RECORD_TYPES}
        self._pending: list[NativeActivity] = []
        self._last_emit_monotonic: float | None = None

    def observe_line(self, line: str) -> tuple[ActivityEmission, ...]:
        activity = parse_native_activity(line, wallclock=self._wallclock)
        if activity is None:
            return ()
        self._counts[activity.record_type] = min(
            MAX_ACTIVITY_COUNTER,
            self._counts[activity.record_type] + 1,
        )
        if activity.record_type in {
            AGENT_STARTED_RECORD_TYPE,
            WORK_BLOCKED_RECORD_TYPE,
            AGENT_COMPLETED_RECORD_TYPE,
        }:
            emissions: list[ActivityEmission] = []
            pending = self.flush()
            if pending is not None:
                emissions.append(pending)
            emissions.append(self._emission((activity,)))
            return tuple(emissions)

        self._pending.append(activity)
        now = self._monotonic()
        if self._last_emit_monotonic is None:
            due = True
        else:
            due = (
                len(self._pending) >= CHECKPOINT_EVENT_INTERVAL
                or now - self._last_emit_monotonic >= CHECKPOINT_SECONDS
            )
        if not due:
            return ()
        emission = self.flush()
        return (emission,) if emission is not None else ()

    def flush(self) -> ActivityEmission | None:
        if not self._pending:
            return None
        pending = tuple(self._pending)
        self._pending.clear()
        self._last_emit_monotonic = self._monotonic()
        return self._emission(pending)

    def _emission(self, activities: Sequence[NativeActivity]) -> ActivityEmission:
        delta: dict[str, int] = {}
        for activity in activities:
            delta[activity.record_type] = min(
                MAX_ACTIVITY_COUNTER,
                delta.get(activity.record_type, 0) + 1,
            )
        latest = activities[-1]
        record_type = (
            GATE_RESULT_RECORD_TYPE
            if GATE_RESULT_RECORD_TYPE in delta
            else latest.record_type
        )
        digest_input = "\n".join(sorted(activity.event_id for activity in activities))
        activity_id = hashlib.sha256(
            f"{record_type}\n{digest_input}".encode("utf-8")
        ).hexdigest()[:32]
        return ActivityEmission(
            record_type=record_type,
            activity_id=activity_id,
            observed_at=latest.observed_at,
            activity_class=latest.activity_class,
            activity_delta=delta,
            activity_counts=dict(self._counts),
            coalesced_count=len(activities),
            activity_events=tuple(
                (activity.event_id, activity.record_type) for activity in activities
            ),
            reason_class=latest.reason_class,
        )


def parse_native_activity(
    line: str,
    *,
    wallclock: Callable[[], float] = time.time,
) -> NativeActivity | None:
    payload = bounded_json_envelope(line)
    if payload is None:
        return None
    nested = payload.get("payload")
    event = nested if isinstance(nested, Mapping) else payload
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    observed_at = _observed_at(payload, event, wallclock=wallclock)

    if event_type == "system" and event.get("subtype") == "init":
        return _native_activity(
            AGENT_STARTED_RECORD_TYPE,
            "agent",
            event_type,
            event,
            observed_at,
        )
    if event_type == "assistant":
        block = _first_content_block(event, _CLAUDE_TOOL_START_TYPES)
        if block is not None:
            return _native_activity(
                ACTIVITY_CHECKPOINT_RECORD_TYPE,
                "tool_started",
                "claude.tool.started",
                block,
                observed_at,
            )
        return None
    if event_type == "user":
        block = _first_content_block(event, _CLAUDE_TOOL_RESULT_TYPES)
        if block is not None:
            return _native_activity(
                GATE_RESULT_RECORD_TYPE,
                "tool_completed",
                "claude.tool.completed",
                block,
                observed_at,
            )
        return None
    if event_type == "result":
        if event.get("is_error") is True or str(event.get("subtype", "")).startswith(
            "error"
        ):
            return _native_activity(
                WORK_BLOCKED_RECORD_TYPE,
                "blocked",
                "claude.result.blocked",
                event,
                observed_at,
                reason_class="provider_error",
            )
        return _native_activity(
            AGENT_COMPLETED_RECORD_TYPE,
            "completed",
            "claude.result.completed",
            event,
            observed_at,
        )

    if event_type == "thread.started":
        return _native_activity(
            AGENT_STARTED_RECORD_TYPE,
            "agent",
            event_type,
            event,
            observed_at,
        )
    if event_type == "turn.started":
        return _native_activity(
            ACTIVITY_CHECKPOINT_RECORD_TYPE,
            "reasoning",
            event_type,
            event,
            observed_at,
        )
    if event_type in _CODEX_ITEM_START_TYPES:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in _CODEX_TOOL_ITEM_TYPES:
            return _native_activity(
                ACTIVITY_CHECKPOINT_RECORD_TYPE,
                "tool_started",
                "codex.item.started",
                item,
                observed_at,
            )
        return None
    if event_type in _CODEX_ITEM_COMPLETE_TYPES:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in _CODEX_TOOL_ITEM_TYPES:
            return _native_activity(
                GATE_RESULT_RECORD_TYPE,
                "tool_completed",
                "codex.item.completed",
                item,
                observed_at,
            )
        return None
    if event_type in _CODEX_TOOL_START_TYPES:
        return _native_activity(
            ACTIVITY_CHECKPOINT_RECORD_TYPE,
            "tool_started",
            event_type,
            event,
            observed_at,
        )
    if event_type in _CODEX_TOOL_COMPLETE_TYPES:
        return _native_activity(
            GATE_RESULT_RECORD_TYPE,
            "tool_completed",
            event_type,
            event,
            observed_at,
        )
    if event_type == "turn.failed" or event_type == "error":
        return _native_activity(
            WORK_BLOCKED_RECORD_TYPE,
            "blocked",
            event_type,
            event,
            observed_at,
            reason_class="provider_error",
        )
    if event_type == "turn.completed":
        return _native_activity(
            AGENT_COMPLETED_RECORD_TYPE,
            "completed",
            event_type,
            event,
            observed_at,
        )
    return None


def bounded_json_envelope(line: str) -> dict[str, object] | None:
    if len(line.encode("utf-8", errors="replace")) > MAX_STREAM_ENVELOPE_BYTES:
        return None
    text = line.strip()
    if text.startswith("data:"):
        text = text.removeprefix("data:").strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not _json_shape_within_bounds(payload):
        return None
    return payload


def summarize_activity_records(
    records: Sequence[Mapping[str, object]],
) -> ActivitySummary:
    seen_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    counts: dict[str, int] = {}
    classes: list[str] = []
    last_activity_at = ""
    latest_class = ""
    usage: dict[str, object] = {}
    activity_record_count = 0
    for record in records:
        if record.get("activity_schema_version") != ACTIVITY_SCHEMA_VERSION:
            continue
        record_type = record.get("record_type")
        activity_id = record.get("activity_id")
        if record_type not in ACTIVITY_RECORD_TYPES or not _is_hex_id(
            activity_id, length=32
        ):
            continue
        if activity_id in seen_ids:
            continue
        seen_ids.add(activity_id)
        activity_record_count += 1
        events = record.get("activity_events")
        has_event_list = isinstance(events, list)
        event_delta: dict[str, int] = {}
        if has_event_list:
            for event in events[:CHECKPOINT_EVENT_INTERVAL]:
                if not isinstance(event, Mapping):
                    continue
                event_id = event.get("id")
                event_record_type = event.get("record_type")
                if (
                    not _is_hex_id(event_id, length=64)
                    or event_record_type not in ACTIVITY_RECORD_TYPES
                    or event_id in seen_event_ids
                ):
                    continue
                seen_event_ids.add(event_id)
                event_delta[event_record_type] = (
                    event_delta.get(event_record_type, 0) + 1
                )
        if has_event_list and not event_delta:
            continue
        delta = event_delta if has_event_list else record.get("activity_delta")
        if isinstance(delta, Mapping):
            for key, value in delta.items():
                if (
                    key in ACTIVITY_RECORD_TYPES
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    counts[key] = min(
                        MAX_ACTIVITY_COUNTER,
                        counts.get(key, 0) + value,
                    )
        observed_at = record.get("observed_at")
        parsed_observed_at = _parse_timestamp(observed_at)
        if parsed_observed_at is not None:
            current_last = _parse_timestamp(last_activity_at)
            if current_last is None or parsed_observed_at > current_last:
                last_activity_at = parsed_observed_at.isoformat()
        activity_class = record.get("activity_class")
        if isinstance(activity_class, str) and activity_class in ACTIVITY_CLASSES:
            latest_class = activity_class
            if activity_class not in classes:
                classes.append(activity_class)
                classes = classes[-8:]
        candidate_usage = record.get("usage")
        if isinstance(candidate_usage, Mapping):
            usage = _bounded_usage(candidate_usage)
    return ActivitySummary(
        last_activity_at=last_activity_at,
        latest_activity_class=latest_class,
        activity_classes=tuple(classes),
        activity_counts=counts,
        activity_record_count=len(seen_event_ids) or activity_record_count,
        usage=usage,
        phase_durations=derive_phase_durations(records, last_activity_at),
    )


def derive_phase_durations(
    records: Sequence[Mapping[str, object]],
    last_activity_at: str,
) -> dict[str, float]:
    transitions: list[tuple[str, datetime]] = []
    for record in records:
        if record.get("record_type") != "stage_transition":
            continue
        if record.get("accepted") is not True:
            continue
        stage = record.get("to_stage")
        occurred_at = _parse_timestamp(record.get("occurred_at"))
        if stage not in PHASE_CLASSES or occurred_at is None:
            continue
        transitions.append((stage, occurred_at))
    if not transitions:
        return {}
    end = _parse_timestamp(last_activity_at)
    if end is None:
        for record in reversed(records):
            end = _parse_timestamp(record.get("finished_at")) or _parse_timestamp(
                record.get("occurred_at")
            )
            if end is not None:
                break
    if end is None:
        end = transitions[-1][1]
    durations: dict[str, float] = {}
    for index, (stage, started) in enumerate(transitions):
        stopped = transitions[index + 1][1] if index + 1 < len(transitions) else end
        seconds = max(0.0, (stopped - started).total_seconds())
        durations[stage] = round(
            min(MAX_ACTIVITY_COUNTER, durations.get(stage, 0.0) + seconds), 6
        )
    return durations


def _native_activity(
    record_type: str,
    activity_class: str,
    event_class: str,
    event: Mapping[str, object],
    observed_at: str,
    *,
    reason_class: str = "",
) -> NativeActivity:
    identifiers = tuple(
        str(event[key])
        for key in (
            "session_id",
            "thread_id",
            "turn_id",
            "call_id",
            "tool_use_id",
            "id",
        )
        if isinstance(event.get(key), str) and event.get(key)
    )
    identity = identifiers or (str(event.get("subtype", "")),)
    fingerprint = hashlib.sha256(
        json.dumps(
            [event_class, identity],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return NativeActivity(
        record_type=record_type,
        activity_class=activity_class,
        event_id=fingerprint,
        observed_at=observed_at,
        reason_class=reason_class,
    )


def _first_content_block(
    event: Mapping[str, object], kinds: frozenset[str]
) -> Mapping[str, object] | None:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return None
    for block in content[:MAX_JSON_CONTAINER_ITEMS]:
        if isinstance(block, Mapping) and block.get("type") in kinds:
            return block
    return None


def _observed_at(
    payload: Mapping[str, object],
    event: Mapping[str, object],
    *,
    wallclock: Callable[[], float],
) -> str:
    for value in (event.get("timestamp"), payload.get("timestamp")):
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed.isoformat()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                try:
                    return datetime.fromtimestamp(number, UTC).isoformat()
                except (OverflowError, OSError, ValueError):
                    pass
    return datetime.fromtimestamp(wallclock(), UTC).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _json_shape_within_bounds(value: object) -> bool:
    nodes = 0
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        candidate, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(candidate, Mapping):
            if len(candidate) > MAX_JSON_CONTAINER_ITEMS:
                return False
            pending.extend((item, depth + 1) for item in candidate.values())
        elif isinstance(candidate, list):
            if len(candidate) > MAX_JSON_CONTAINER_ITEMS:
                return False
            pending.extend((item, depth + 1) for item in candidate)
    return True


def _bounded_usage(value: Mapping[str, object]) -> dict[str, object]:
    allowed_text = {
        "usage_source",
        "usage_version",
        "provider",
        "usage_unavailable_reason",
    }
    allowed_numbers = {
        "input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "turns",
        "duration_seconds",
        "cost_usd",
    }
    result: dict[str, object] = {}
    for key in allowed_text:
        candidate = value.get(key)
        if isinstance(candidate, str) and len(candidate) <= 64:
            result[key] = candidate
    for key in allowed_numbers:
        candidate = value.get(key)
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and math.isfinite(float(candidate))
            and candidate >= 0
        ):
            result[key] = candidate
    return result


def _is_hex_id(value: object, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )
