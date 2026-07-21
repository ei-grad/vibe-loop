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


USAGE_SCHEMA_VERSION = 2
SUMMARY_SCHEMA_VERSION = 2
PHASES = frozenset(
    {
        "planning",
        "implementation",
        "initial_review",
        "focused_validation",
        "full_validation",
        "review",
        "remediation",
        "targeted_closure",
        "integration",
    }
)
WORK_KINDS = frozenset({"discovery", "review"})
NORMALIZED_NUMERIC_FIELDS = (
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
)
TOKEN_GROUP_METRICS = (
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_tokens",
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
CANONICAL_USAGE_PROVIDERS = frozenset({"anthropic", "openai", "unknown"})
NATIVE_MODEL_LABEL_RE = re.compile(
    r"(?:claude-(?:(?:haiku|opus|sonnet)(?:-[0-9]+){1,3}"
    r"|[0-9]+(?:-[0-9]+){0,2}-(?:haiku|opus|sonnet)(?:-[0-9]{8})?)"
    r"|gpt-[0-9]+(?:\.[0-9]+)?[a-z]?"
    r"(?:-(?:chat|codex|latest|max|mini|nano|pro|sol|terra|[0-9]{8})){0,4}"
    r"|o[1-9](?:-(?:max|mini|pro|[0-9]{8})){0,3})"
)
ATTRIBUTION_DIAGNOSTIC_LIMIT = 16
# Structured activity a worker may perform after its accepted terminal report.
# A bounded text-only summary is not activity and carries no kind.
POST_REPORT_ACTIVITY_KINDS = frozenset({"tool_call", "tool_result", "child_process"})
USAGE_SOURCES = frozenset(
    {
        "unavailable",
        "native:provider",
        "native:claude:result",
        "native:claude:transcript",
        "native:codex:turn.completed",
        "native:codex:token_count",
        "native:combined",
    }
)
USAGE_VERSIONS = frozenset(
    {
        "1",
        "claude-result-v1",
        "claude-transcript-v1",
        "codex-jsonl-v1",
        "codex-rollout-v1",
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
QUOTA_UNAVAILABLE_REASONS = frozenset(
    {
        "quota_snapshot_not_reported",
        "malformed_quota_snapshot",
    }
)
QUOTA_RESET_JITTER_TOLERANCE_SECONDS = 1
SENSITIVE_METADATA_MARKERS = (
    "credential",
    "fencing",
    "password",
    "prompt",
    "secret",
    "token",
    "transcript",
)


def normalize_provider_label(value: object) -> tuple[str, bool]:
    """Return a canonical usage-group provider and whether input was rejected."""
    if not isinstance(value, str):
        return "unknown", value is not None
    normalized = value.casefold()
    if value != value.strip() or normalized not in CANONICAL_USAGE_PROVIDERS:
        return "unknown", True
    return normalized, False


def normalize_model_label(value: object) -> tuple[str, bool]:
    """Return a bounded native model identifier or the safe fallback."""
    if not isinstance(value, str):
        return "unknown", value is not None
    normalized = value.casefold()
    if value != value.strip():
        return "unknown", True
    if normalized == "unknown":
        return normalized, False
    if NATIVE_MODEL_LABEL_RE.fullmatch(normalized):
        return normalized, False
    return "unknown", True


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


def _safe_quota_label(value: object, fallback: str) -> str:
    if (
        isinstance(value, str)
        and SAFE_METADATA_RE.fullmatch(value)
        and not any(marker in value.casefold() for marker in SENSITIVE_METADATA_MARKERS)
    ):
        return value
    return fallback


def _canonical_observed_at(value: object, fallback: datetime | None = None) -> str:
    observed = parse_timestamp(value)
    if observed is None:
        observed = fallback or datetime.now(UTC)
    return observed.isoformat()


@dataclasses.dataclass(frozen=True)
class QuotaSnapshot:
    provider: str
    scope: str
    window: str
    observed_at: str
    used_percent: float
    window_minutes: int
    resets_at: int

    def to_stats(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "scope": self.scope,
            "window": self.window,
            "observed_at": self.observed_at,
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
        }


def _sanitize_quota_snapshots(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    snapshots: list[dict[str, object]] = []
    for candidate in value[:8]:
        if not isinstance(candidate, Mapping):
            continue
        provider = candidate.get("provider")
        scope = candidate.get("scope")
        window = candidate.get("window")
        observed_at = parse_timestamp(candidate.get("observed_at"))
        used_percent = _number(candidate.get("used_percent"))
        window_minutes = _integer(candidate.get("window_minutes"))
        resets_at = _integer(candidate.get("resets_at"))
        if (
            not isinstance(provider, str)
            or provider not in CANONICAL_USAGE_PROVIDERS
            or not isinstance(scope, str)
            or _safe_quota_label(scope, "") != scope
            or not isinstance(window, str)
            or _safe_quota_label(window, "") != window
            or observed_at is None
            or used_percent is None
            or used_percent > 100
            or window_minutes is None
            or not 0 < window_minutes <= 10 * 366 * 24 * 60
            or resets_at is None
            or resets_at > 253402300799
        ):
            continue
        snapshots.append(
            {
                "provider": provider,
                "scope": scope,
                "window": window,
                "observed_at": observed_at.isoformat(),
                "used_percent": float(used_percent),
                "window_minutes": window_minutes,
                "resets_at": resets_at,
            }
        )
    return snapshots


def _quota_snapshots(
    provider: str,
    payload: Mapping[str, object],
    *,
    observed_at: datetime | None = None,
) -> tuple[tuple[QuotaSnapshot, ...], str]:
    event = payload
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        event = nested
    raw_limits = event.get("rate_limits")
    if raw_limits is None:
        raw_limits = event.get("quota")
    if raw_limits is None:
        return (), "quota_snapshot_not_reported"
    if not isinstance(raw_limits, Mapping):
        return (), "malformed_quota_snapshot"

    scope = _safe_quota_label(raw_limits.get("limit_id"), provider)
    observed_value = payload.get("timestamp") or event.get("observed_at")
    observation = _canonical_observed_at(observed_value, observed_at)
    snapshots: list[QuotaSnapshot] = []
    malformed = False
    windows = (
        ("primary", raw_limits.get("primary") or raw_limits.get("primary_window")),
        (
            "secondary",
            raw_limits.get("secondary") or raw_limits.get("secondary_window"),
        ),
    )
    for window_name, raw_window in windows:
        if raw_window is None:
            continue
        if not isinstance(raw_window, Mapping):
            malformed = True
            continue
        used_percent = _number(raw_window.get("used_percent"))
        window_minutes = _integer(raw_window.get("window_minutes"))
        if window_minutes is None:
            window_seconds = _integer(raw_window.get("limit_window_seconds"))
            if window_seconds is not None and window_seconds % 60 == 0:
                window_minutes = window_seconds // 60
        resets_at = _integer(raw_window.get("resets_at"))
        if (
            used_percent is None
            or used_percent > 100
            or window_minutes is None
            or not 0 < window_minutes <= 10 * 366 * 24 * 60
            or resets_at is None
            or resets_at > 253402300799
        ):
            malformed = True
            continue
        snapshots.append(
            QuotaSnapshot(
                provider=provider,
                scope=scope,
                window=window_name,
                observed_at=observation,
                used_percent=float(used_percent),
                window_minutes=window_minutes,
                resets_at=resets_at,
            )
        )
    if snapshots:
        return tuple(snapshots), ""
    return (
        (),
        "malformed_quota_snapshot"
        if malformed or raw_limits
        else "quota_snapshot_not_reported",
    )


@dataclasses.dataclass(frozen=True)
class ProviderUsage:
    provider: str
    source: str
    version: str
    values: Mapping[str, int | float] = dataclasses.field(default_factory=dict)
    raw: Mapping[str, int | float] = dataclasses.field(default_factory=dict)
    unavailable_reason: str = ""
    malformed: bool = False
    quota_snapshots: tuple[QuotaSnapshot, ...] = ()
    quota_unavailable_reason: str = "quota_snapshot_not_reported"

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
        post_report: Mapping[str, object] | None = None,
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
        stats["quota_evidence_available"] = bool(self.quota_snapshots)
        if self.quota_snapshots:
            stats["quota_snapshots"] = [
                snapshot.to_stats() for snapshot in self.quota_snapshots
            ]
        else:
            stats["quota_unavailable_reason"] = (
                self.quota_unavailable_reason or "quota_snapshot_not_reported"
            )
        if candidate_fingerprint:
            stats["candidate_fingerprint"] = candidate_fingerprint[:160]
        if changed_lines is not None and changed_lines >= 0:
            stats["changed_lines"] = changed_lines
        if work_kind in WORK_KINDS:
            stats["work_kind"] = work_kind
        post_report_stats = _sanitize_post_report_stats(post_report)
        if post_report_stats:
            stats["post_report"] = post_report_stats
        return stats


def _sanitize_post_report_stats(value: object) -> dict[str, object]:
    """Whitelist the post-report teardown breakdown attached to run stats.

    Kept separate from the primary usage so quota diagnostics can subtract the
    teardown burn a worker accrued after its accepted terminal report from the
    useful implementation/review spend.
    """
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    duration = _number(value.get("duration_seconds"))
    if duration is not None and duration >= 0:
        result["duration_seconds"] = duration
    if isinstance(value.get("enforced_stop"), bool):
        result["enforced_stop"] = value["enforced_stop"]
    activity_kind = value.get("activity_kind")
    if isinstance(activity_kind, str) and activity_kind in POST_REPORT_ACTIVITY_KINDS:
        result["activity_kind"] = activity_kind
    activity_count = _integer(value.get("activity_count"))
    if activity_count is not None and activity_count >= 0:
        result["activity_count"] = activity_count
    usage = _numeric_mapping(value.get("usage"))
    if usage:
        result["usage"] = usage
    return result


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
    post_report = _sanitize_post_report_stats(value.get("post_report"))
    if post_report:
        result["post_report"] = post_report
    quota_reason = value.get("quota_unavailable_reason")
    snapshots = _sanitize_quota_snapshots(value.get("quota_snapshots"))
    if snapshots:
        result["quota_evidence_available"] = True
        result["quota_snapshots"] = snapshots
    elif any(
        key in value
        for key in (
            "quota_evidence_available",
            "quota_unavailable_reason",
            "quota_snapshots",
        )
    ):
        result["quota_evidence_available"] = False
        result["quota_unavailable_reason"] = (
            quota_reason
            if isinstance(quota_reason, str)
            and quota_reason in QUOTA_UNAVAILABLE_REASONS
            else "malformed_quota_snapshot"
        )
    return result


def merge_provider_usage(*items: ProviderUsage) -> ProviderUsage:
    available = [item for item in items if item.available]
    quota_snapshots = _merge_quota_snapshots(*(item.quota_snapshots for item in items))
    if not available:
        if not items:
            return unavailable_usage("unknown", "provider_usage_not_reported")
        return dataclasses.replace(
            items[0],
            quota_snapshots=quota_snapshots,
            quota_unavailable_reason=(
                "" if quota_snapshots else items[0].quota_unavailable_reason
            ),
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
        quota_snapshots=quota_snapshots,
        quota_unavailable_reason=(
            "" if quota_snapshots else "quota_snapshot_not_reported"
        ),
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
        "reasoning_output_tokens": "reasoning_output_tokens",
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


def parse_claude_result(
    payload: Mapping[str, object], *, observed_at: datetime | None = None
) -> ProviderUsage | None:
    if payload.get("type") != "result":
        return None
    usage = _normalized_usage(
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
    snapshots, reason = _quota_snapshots("anthropic", payload, observed_at=observed_at)
    return dataclasses.replace(
        usage, quota_snapshots=snapshots, quota_unavailable_reason=reason
    )


def parse_codex_event(
    payload: Mapping[str, object], *, observed_at: datetime | None = None
) -> ProviderUsage | None:
    event = payload
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        event = nested
    event_type = event.get("type")
    snapshots, reason = _quota_snapshots("openai", payload, observed_at=observed_at)
    if event_type not in {
        "turn.completed",
        "turn_complete",
        "turn.completed.v1",
        "token_count",
    }:
        return None
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        turn = event.get("turn")
        usage = turn.get("usage") if isinstance(turn, Mapping) else usage
    if not isinstance(usage, Mapping) and event_type == "token_count":
        info = event.get("info")
        if isinstance(info, Mapping):
            usage = info.get("total_token_usage")
            if not isinstance(usage, Mapping):
                usage = info.get("last_token_usage")
    normalized = _normalized_usage(
        "openai",
        (
            "native:codex:token_count"
            if event_type == "token_count"
            else "native:codex:turn.completed"
        ),
        "codex-rollout-v1" if event_type == "token_count" else "codex-jsonl-v1",
        usage,
        extra={"turns": 1},
    )
    return dataclasses.replace(
        normalized, quota_snapshots=snapshots, quota_unavailable_reason=reason
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

    def observe_line(self, line: str) -> ProviderUsage | None:
        text = line.strip()
        if text.startswith("data:"):
            text = text.removeprefix("data:").strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        observed_at = datetime.now(UTC)
        parsed = parse_claude_result(
            payload, observed_at=observed_at
        ) or parse_codex_event(payload, observed_at=observed_at)
        if parsed is None:
            return None
        with self._lock:
            self._saw_malformed_usage = self._saw_malformed_usage or parsed.malformed
            if self._usage is None:
                self._usage = parsed
                return parsed
            current = self._usage
            usage = parsed if parsed.available else current
            snapshots = _merge_quota_snapshots(
                current.quota_snapshots, parsed.quota_snapshots
            )
            quota_reason = "" if snapshots else parsed.quota_unavailable_reason
            self._usage = dataclasses.replace(
                usage,
                quota_snapshots=snapshots,
                quota_unavailable_reason=quota_reason,
                malformed=current.malformed or parsed.malformed,
            )
        return parsed


def _merge_quota_snapshots(
    *items: Iterable[QuotaSnapshot],
) -> tuple[QuotaSnapshot, ...]:
    snapshots: dict[tuple[object, ...], QuotaSnapshot] = {}
    for item in items:
        for snapshot in item:
            key = (
                snapshot.provider,
                snapshot.scope,
                snapshot.window,
                snapshot.observed_at,
                snapshot.window_minutes,
                snapshot.resets_at,
            )
            snapshots[key] = snapshot
    return tuple(list(snapshots.values())[-8:])


def parse_codex_rollout_usage(path: Path) -> ProviderUsage:
    observer = ProviderUsageObserver("openai")
    try:
        with path.open(encoding="utf-8") as rollout:
            for line in rollout:
                observer.observe_line(line)
    except (OSError, UnicodeError):
        return unavailable_usage("openai", "provider_transcript_unavailable")
    return observer.usage


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


def _raw_provider_label(record: Mapping[str, object]) -> object:
    stats = record.get("stats")
    stats_map = stats if isinstance(stats, Mapping) else {}
    return (
        record.get("model_provider")
        or stats_map.get("provider")
        or {"codex": "openai", "claude": "anthropic"}.get(
            record.get("agent_kind"), "unknown"
        )
    )


def _group_identity(
    record: Mapping[str, object],
) -> tuple[str, str, str, frozenset[str]]:
    provider, provider_rejected = normalize_provider_label(_raw_provider_label(record))
    model, model_rejected = normalize_model_label(record.get("model_id") or "unknown")
    rejected = frozenset(
        field
        for field, invalid in (
            ("provider", provider_rejected),
            ("model", model_rejected),
        )
        if invalid
    )
    stats = record.get("stats")
    stats_map = stats if isinstance(stats, Mapping) else {}
    phase = str(stats_map.get("phase") or "implementation")
    if phase not in PHASES:
        phase = "implementation"
    return provider, model, phase, rejected


def _stored_attribution_rejections(record: Mapping[str, object]) -> frozenset[str]:
    value = record.get("attribution_diagnostics")
    if not isinstance(value, list | tuple):
        return frozenset()
    fields: set[str] = set()
    for diagnostic in value[:ATTRIBUTION_DIAGNOSTIC_LIMIT]:
        if not isinstance(diagnostic, Mapping):
            continue
        if diagnostic.get("type") != "invalid_attribution_label":
            continue
        field = diagnostic.get("field")
        if field in {"provider", "model"}:
            fields.add(str(field))
    return frozenset(fields)


def _new_usage_group(
    *, project: str, provider: str, model: str, phase: str
) -> dict[str, object]:
    group: dict[str, object] = {
        "project": project,
        "provider": provider,
        "model": model,
        "phase": phase,
        "launches": 0,
        "completed_runs": 0,
        "immediate_failures": 0,
        "restarts": 0,
        "worker_minutes": 0.0,
        "reported_cost_usd": 0.0,
        "tasks_created": 0,
        "tasks_landed": 0,
    }
    for metric in TOKEN_GROUP_METRICS:
        group[metric] = 0
    group["non_cached_input_tokens"] = 0
    return group


def non_cached_input_tokens(stats: object) -> int:
    """Fresh (non-cache-served) input tokens. Providers report cached input as a
    subset of input, so this is clamped at zero rather than allowed to go
    negative when a provider reports the two inconsistently."""
    return max(
        0,
        int(_metric(stats, "input_tokens"))
        - int(_metric(stats, "cached_input_tokens")),
    )


def fresh_input_tokens(stats: object, provider: str) -> int:
    if provider == "openai":
        return non_cached_input_tokens(stats)
    return int(_metric(stats, "input_tokens"))


def _quota_role(record: Mapping[str, object]) -> str:
    stats = record.get("stats")
    stats_map = stats if isinstance(stats, Mapping) else {}
    phase = str(stats_map.get("phase") or "implementation")
    if phase == "review":
        return (
            "resumed_review"
            if stats_map.get("session_continuation") is True
            else "review"
        )
    if phase in {"focused_validation", "full_validation"}:
        return "validation"
    if phase in {"planning", "remediation", "integration"}:
        return phase
    return "implementation"


def _quota_provider_group(provider: str) -> dict[str, object]:
    return {
        "provider": provider,
        "launches": 0,
        "attempts": 0,
        "productive_completions": 0,
        "landed_tasks": 0,
        "worker_minutes": 0.0,
        "fresh_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "gross_tokens": 0,
        "reported_cost_usd": 0.0,
        "gross_usage_per_landed_task": None,
        "fresh_input_per_landed_task": None,
        "activity": {},
        "quota_evidence_available": False,
        "quota_unavailable_reason": "quota_snapshot_not_reported",
        "account_wall_evidence_available": False,
        "account_wall_observations": 0,
        "account_wall_last_observed_at": None,
        "snapshots": [],
        "forecasts": [],
    }


def _quota_forecasts(snapshots: list[dict[str, object]]) -> list[dict[str, object]]:
    compatible: dict[tuple[str, str, str, int], list[dict[str, object]]] = defaultdict(
        list
    )
    for snapshot in snapshots:
        key = (
            str(snapshot["provider"]),
            str(snapshot["scope"]),
            str(snapshot["window"]),
            int(snapshot["window_minutes"]),
        )
        compatible[key].append(snapshot)
    forecasts: list[dict[str, object]] = []
    for key, compatible_candidates in sorted(compatible.items()):
        reset_clusters: list[tuple[int, list[dict[str, object]]]] = []
        for candidate in sorted(
            compatible_candidates,
            key=lambda item: (
                int(item["resets_at"]),
                str(item["observed_at"]),
                float(item["used_percent"]),
            ),
        ):
            reset_at = int(candidate["resets_at"])
            if (
                not reset_clusters
                or reset_at - reset_clusters[-1][0]
                > QUOTA_RESET_JITTER_TOLERANCE_SECONDS
            ):
                reset_clusters.append((reset_at, [candidate]))
            else:
                reset_clusters[-1][1].append(candidate)

        for normalized_reset_at, candidates in reset_clusters:
            candidates_by_time: dict[str, list[dict[str, object]]] = defaultdict(list)
            for candidate in candidates:
                candidates_by_time[str(candidate["observed_at"])].append(candidate)

            current_window: list[dict[str, object]] = []
            for observed_at in sorted(candidates_by_time):
                same_time = candidates_by_time[observed_at]
                usages = {float(item["used_percent"]) for item in same_time}
                # Unequal usage at one timestamp has no reliable order. Treat the
                # lowest value as the post-reset baseline instead of risking a bridge.
                if len(usages) > 1:
                    current_window = []
                candidate = min(
                    same_time,
                    key=lambda item: (
                        float(item["used_percent"]),
                        -int(item["resets_at"]),
                    ),
                )
                if current_window and float(candidate["used_percent"]) < float(
                    current_window[-1]["used_percent"]
                ):
                    current_window = []
                current_window.append(candidate)
            if len(current_window) < 2:
                continue
            after = current_window[-1]
            before = next(
                (
                    candidate
                    for candidate in reversed(current_window[:-1])
                    if float(candidate["used_percent"]) < float(after["used_percent"])
                ),
                None,
            )
            if before is None:
                continue
            before_at = parse_timestamp(before["observed_at"])
            after_at = parse_timestamp(after["observed_at"])
            if before_at is None or after_at is None or after_at <= before_at:
                continue
            delta = float(after["used_percent"]) - float(before["used_percent"])
            elapsed_hours = (after_at - before_at).total_seconds() / 3600
            burn_rate = delta / elapsed_hours
            remaining_percent = max(0.0, 100 - float(after["used_percent"]))
            exhaustion_at = after_at + timedelta(hours=remaining_percent / burn_rate)
            reset_at = datetime.fromtimestamp(int(after["resets_at"]), UTC)
            forecasts.append(
                {
                    "provider": key[0],
                    "scope": key[1],
                    "window": key[2],
                    "window_minutes": key[3],
                    "resets_at": int(after["resets_at"]),
                    "normalized_resets_at": normalized_reset_at,
                    "first_observed_at": before_at.isoformat(),
                    "last_observed_at": after_at.isoformat(),
                    "burn_rate_percent_per_hour": round(burn_rate, 6),
                    "exhaustion_at": exhaustion_at.isoformat(),
                    "exhaustion_before_reset": exhaustion_at < reset_at,
                }
            )
    return forecasts


def _quota_account_wall_summary(
    recent: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    providers: dict[str, dict[str, object]] = {}
    landed: dict[str, set[str]] = defaultdict(set)
    malformed_evidence: set[str] = set()
    for record in recent:
        record_type = record.get("record_type")
        if record_type not in {None, "run_result", "autopilot_planning_outcome"}:
            continue
        stats = record.get("stats")
        stats_map = stats if isinstance(stats, Mapping) else {}
        provider, _ = normalize_provider_label(_raw_provider_label(record))
        group = providers.setdefault(provider, _quota_provider_group(provider))
        launched = not (
            record_type == "autopilot_planning_outcome"
            and record.get("provider_launched") is False
        )
        if launched:
            group["launches"] = int(group["launches"]) + 1
            group["attempts"] = int(group["attempts"]) + 1
        productive = (
            record.get("outcome") == "productive"
            if record_type == "autopilot_planning_outcome"
            else record.get("classification", record.get("status")) == "completed"
        )
        if productive:
            group["productive_completions"] = int(group["productive_completions"]) + 1
            task_id = str(record.get("task_id") or "")
            if task_id:
                landed[provider].add(task_id)
        group["worker_minutes"] = float(group["worker_minutes"]) + (
            record_duration_seconds(record) / 60
        )
        fresh = fresh_input_tokens(stats, provider)
        cache_read = int(
            _metric(
                stats,
                "cached_input_tokens"
                if provider == "openai"
                else "cache_read_input_tokens",
            )
        )
        cache_create = int(_metric(stats, "cache_creation_input_tokens"))
        output = int(_metric(stats, "output_tokens"))
        reasoning = int(_metric(stats, "reasoning_output_tokens"))
        gross = fresh + cache_read + cache_create + output
        group["fresh_input_tokens"] = int(group["fresh_input_tokens"]) + fresh
        group["cache_read_tokens"] = int(group["cache_read_tokens"]) + cache_read
        group["cache_create_tokens"] = int(group["cache_create_tokens"]) + cache_create
        group["output_tokens"] = int(group["output_tokens"]) + output
        group["reasoning_output_tokens"] = (
            int(group["reasoning_output_tokens"]) + reasoning
        )
        group["gross_tokens"] = int(group["gross_tokens"]) + gross
        group["reported_cost_usd"] = float(group["reported_cost_usd"]) + _metric(
            stats, "cost_usd"
        )
        activity = group["activity"]
        assert isinstance(activity, dict)
        role = _quota_role(record)
        activity[role] = int(activity.get(role, 0)) + 1
        if not productive:
            activity["failed_attempt"] = int(activity.get("failed_attempt", 0)) + 1
        if (_integer(record.get("restart_count")) or 0) > 0:
            activity["restarted_attempt"] = (
                int(activity.get("restarted_attempt", 0)) + 1
            )
        snapshots = _sanitize_quota_snapshots(stats_map.get("quota_snapshots"))
        if snapshots:
            group["quota_evidence_available"] = True
            group["quota_unavailable_reason"] = ""
            stored = group["snapshots"]
            assert isinstance(stored, list)
            stored.extend(snapshots)
        elif stats_map.get("quota_unavailable_reason") == "malformed_quota_snapshot":
            malformed_evidence.add(provider)
        if record.get("classification") == "limit_wall":
            group["account_wall_evidence_available"] = True
            group["account_wall_observations"] = (
                int(group["account_wall_observations"]) + 1
            )
            observed_at = parse_timestamp(
                record.get("finished_at") or record.get("occurred_at")
            )
            if observed_at is not None:
                prior = parse_timestamp(group["account_wall_last_observed_at"])
                if prior is None or observed_at > prior:
                    group["account_wall_last_observed_at"] = observed_at.isoformat()

    for provider, group in providers.items():
        group["landed_tasks"] = len(landed[provider])
        tasks = int(group["landed_tasks"])
        group["worker_minutes"] = round(float(group["worker_minutes"]), 3)
        group["reported_cost_usd"] = round(float(group["reported_cost_usd"]), 6)
        if tasks:
            group["gross_usage_per_landed_task"] = round(
                int(group["gross_tokens"]) / tasks, 3
            )
            group["fresh_input_per_landed_task"] = round(
                int(group["fresh_input_tokens"]) / tasks, 3
            )
        if provider in malformed_evidence and not group["quota_evidence_available"]:
            group["quota_unavailable_reason"] = "malformed_quota_snapshot"
        stored = group["snapshots"]
        assert isinstance(stored, list)
        stored.sort(
            key=lambda item: (
                str(item["scope"]),
                str(item["window"]),
                str(item["observed_at"]),
            )
        )
        group["forecasts"] = _quota_forecasts(stored)
    return {
        "evidence_available": any(
            bool(group["quota_evidence_available"])
            or bool(group["account_wall_evidence_available"])
            for group in providers.values()
        ),
        "providers": [providers[key] for key in sorted(providers)],
    }


def attempt_circuit_breaker_summary(
    records: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Summarize durable breaker records without exposing task text or commands."""

    latest_attempt: dict[str, Mapping[str, object]] = {}
    latest_reset: dict[str, int] = {}
    opened: list[tuple[int, Mapping[str, object]]] = []
    avoided: dict[tuple[str, str], int] = defaultdict(int)
    materialized = list(records)
    for index, record in enumerate(materialized):
        task_id = str(record.get("task_id") or "")
        if not task_id:
            continue
        record_type = record.get("record_type")
        if record_type == "attempt_circuit_reset":
            latest_reset[task_id] = index
        elif record_type == "attempt_circuit_attempt":
            latest_attempt[task_id] = record
        elif record_type == "attempt_circuit_opened":
            opened.append((index, record))
    for index, record in enumerate(materialized):
        if record.get("record_type") != "attempt_circuit_avoided":
            continue
        task_id = str(record.get("task_id") or "")
        fingerprint = str(record.get("fingerprint") or "")
        if task_id and index > latest_reset.get(task_id, -1):
            avoided[(task_id, fingerprint)] += 1
    open_breakers: list[dict[str, object]] = []
    for index, record in opened:
        task_id = str(record.get("task_id") or "")
        fingerprint = str(record.get("fingerprint") or "")
        if not task_id or index <= latest_reset.get(task_id, -1):
            continue
        current = latest_attempt.get(task_id)
        if current is None or str(current.get("fingerprint") or "") != fingerprint:
            continue
        open_breakers.append(
            {
                "task_id": task_id,
                "fingerprint": fingerprint,
                "fingerprint_inputs": {
                    key: record.get(key, "")
                    for key in (
                        "task_revision",
                        "configuration_revision",
                        "base",
                        "candidate",
                        "route",
                    )
                },
                "attempt_count": int(record.get("attempt_count") or 0),
                "threshold": int(record.get("threshold") or 0),
                "blocker_class": str(record.get("blocker_class") or ""),
                "opening_reason": str(record.get("reason") or ""),
                "opened_at": str(record.get("occurred_at") or ""),
                "avoided_launches": avoided[(task_id, fingerprint)],
            }
        )
    return {
        "open": sorted(open_breakers, key=lambda item: str(item["task_id"])),
        "open_count": len(open_breakers),
        "avoided_launches": sum(item["avoided_launches"] for item in open_breakers),
    }


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
    all_records = list(records)
    recent = [
        record
        for record in all_records
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
    attribution_rejections: dict[str, int] = defaultdict(int)
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
        if record.get("record_type") not in {
            None,
            "run_result",
            "autopilot_planning_outcome",
        }:
            continue
        _, _, _, projected_rejections = _group_identity(record)
        for field in projected_rejections | _stored_attribution_rejections(record):
            attribution_rejections[field] += 1

    for record in recent:
        if record.get("record_type") != "autopilot_planning_outcome":
            continue
        stats = record.get("stats")
        stats_map = stats if isinstance(stats, Mapping) else {}
        provider, model, _, _ = _group_identity(record)
        key = (provider, model, "planning")
        group = groups.setdefault(
            key,
            _new_usage_group(
                project=project, provider=provider, model=model, phase="planning"
            ),
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
        for metric in TOKEN_GROUP_METRICS:
            group[metric] = int(group[metric]) + int(_metric(stats, metric))
        group["non_cached_input_tokens"] = int(
            group["non_cached_input_tokens"]
        ) + non_cached_input_tokens(stats)
        group["reported_cost_usd"] = float(group["reported_cost_usd"]) + _metric(
            stats, "cost_usd"
        )

    for record in recent:
        if record.get("record_type") not in {None, "run_result"}:
            continue
        provider, model, phase, _ = _group_identity(record)
        key = (provider, model, phase)
        group = groups.setdefault(
            key,
            _new_usage_group(
                project=project, provider=provider, model=model, phase=phase
            ),
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
        for metric in TOKEN_GROUP_METRICS:
            group[metric] = int(group[metric]) + int(_metric(stats, metric))
        group["non_cached_input_tokens"] = int(
            group["non_cached_input_tokens"]
        ) + non_cached_input_tokens(stats)
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
                    "input_tokens": int(_metric(stats, "input_tokens")),
                    "cached_input_tokens": int(_metric(stats, "cached_input_tokens")),
                    "non_cached_input_tokens": non_cached_input_tokens(stats),
                    "output_tokens": int(_metric(stats, "output_tokens")),
                    "changed_lines": changed_lines,
                    "threshold": slice_token_threshold,
                    "threshold_metric": "total_tokens",
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
        group["fresh_input_tokens_per_completed_task"] = (
            round(int(group["non_cached_input_tokens"]) / productive_tasks, 3)
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
        failed = [
            record
            for _, record in attempts
            if record.get("classification", record.get("status")) != "completed"
        ]
        if len(failed) >= 2:
            diagnostics.append(
                {
                    "type": "repeated_failed_attempts",
                    "severity": "warning",
                    "task_id": task_id,
                    "attempts": len(failed),
                    "avoidable_burn": True,
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
        if work_kind == "review":
            diagnostic_type = (
                "same_session_review_resume"
                if independent_sessions == 1
                else "new_session_rereview"
            )
        else:
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
                "avoidable_burn": independent_sessions > 1,
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
    for field in sorted(attribution_rejections):
        diagnostics.append(
            {
                "type": "invalid_attribution_label",
                "severity": "warning",
                "field": field,
                "count": attribution_rejections[field],
                "normalized": "unknown",
            }
        )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "project": project,
        "window_hours": hours,
        "since": since.isoformat(),
        "until": current.isoformat(),
        "groups": [groups[key] for key in sorted(groups)],
        "quota_account_wall": _quota_account_wall_summary(recent),
        "attempt_circuit_breakers": attempt_circuit_breaker_summary(all_records),
        "diagnostics": diagnostics,
    }
