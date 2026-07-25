"""Durable, atomic per-scope usage-budget reservations.

This module gates model launches on a configurable usage budget. Before a launch
a conservative declared allowance is *atomically* reserved against every matching
cap; concurrent supervisors and runs therefore cannot oversubscribe a cap,
because each reservation is appended under the same append-only journal lock the
run store already uses (`runs.append_record_lock`). When a run reaches an
authoritative terminal provider-usage figure the reservation is reconciled
exactly once against that figure; usage a provider never reported is charged
through a configurable fail-safe policy and never as zero. Abandoned reservations
(owner crashed, never reconciled) are recovered without double-spend by the same
exactly-once guard.

The ledger records the full input/output/cache/gross/fresh/cost dimension
breakdown plus an authoritative/unknown marker for evidence, while cap arithmetic
is denominated in the single configured `metric`. Per-provider projections are
reported separately and never summed across providers, so Claude-versus-Codex
route balancing has evidence without any cross-provider token equivalence.

Admission bounds *subsequent launches*: it cannot cap token production inside an
already-running model process, because the provider CLIs enforce no hard
per-invocation cap. That boundary is documented, not silently assumed away.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import socket
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vibe_loop.config import BUDGET_METRICS, BudgetConfig
from vibe_loop.processes import process_birth_identity
from vibe_loop.runs import append_record_lock, string_value, utc_now_iso
from vibe_loop.telemetry import (
    fresh_input_tokens,
    non_cached_input_tokens,
    normalize_model_label,
    normalize_provider_label,
    parse_timestamp,
)

BUDGET_SCHEMA_VERSION = 1
BUDGET_RESERVED_RECORD_TYPE = "budget_reserved"
BUDGET_RECONCILED_RECORD_TYPE = "budget_reconciled"
BUDGET_RELEASED_RECORD_TYPE = "budget_released"
BUDGET_DECISION_RECORD_TYPE = "budget_decision"
BUDGET_RECORD_TYPES = frozenset(
    {
        BUDGET_RESERVED_RECORD_TYPE,
        BUDGET_RECONCILED_RECORD_TYPE,
        BUDGET_RELEASED_RECORD_TYPE,
        BUDGET_DECISION_RECORD_TYPE,
    }
)
# Terminal record types close a reservation. The exactly-once guard keys on the
# reservation id: once a reconciliation or release exists, no further terminal
# record is written, so recovery and normal settlement cannot double-spend.
BUDGET_TERMINAL_RECORD_TYPES = frozenset(
    {BUDGET_RECONCILED_RECORD_TYPE, BUDGET_RELEASED_RECORD_TYPE}
)
BUDGET_DECISIONS = ("admit", "defer", "block", "disabled")
BUDGET_JOURNAL_HEADER_RECORD_TYPE = "budget_journal_header"
BUDGET_CHECKPOINT_SCHEMA_VERSION = 1
# Compaction folds closed reservations into the checkpoint once the active
# journal exceeds this many records, keeping replay work and file growth bounded
# without discarding audit counters or live-reservation recovery.
BUDGET_COMPACTION_THRESHOLD = 2000
# Deterministic upper bound on the length of any list in a projection; the
# remainder is reported only as a truncated count, never as raw rows.
BUDGET_MAX_PROJECTION_ITEMS = 100
BUDGET_SCOPE_KEYS = ("project", "provider", "phase", "model", "effort")
BUDGET_RECONCILE_REASONS = frozenset(
    {"terminal_usage", "recovered_terminal_usage", "recovered_abandoned"}
)
# Corruption classes counted (never with raw payloads) when a terminal record
# fails the durable schema/identity contract and is refused.
BUDGET_INTEGRITY_CLASSES = (
    "malformed_json",
    "unknown_record_type",
    "orphan_terminal",
    "identity_mismatch",
    "generation_mismatch",
    "missing_fields",
    "invalid_charge",
    "duplicate_terminal",
)
# Terminal-record reasons are bounded enum-like labels, never free text; this
# keeps content-bearing/raw payloads out of the durable ledger.
_SAFE_REASON_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
# Dimensions preserved on every reconciliation for evidence. Cap arithmetic uses
# only the single configured metric, but the full breakdown is retained so an
# operator can see input/output/cache/gross/fresh/cost separately.
BUDGET_DIMENSIONS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "non_cached_input_tokens",
    "fresh_input_tokens",
    "cost_usd",
)

_APPEND_LOCK = threading.Lock()


class BudgetReservationDenied(RuntimeError):
    """A launch was refused because a matching cap had insufficient remaining."""

    def __init__(self, decision: "BudgetDecision") -> None:
        self.decision = decision
        binding = ", ".join(
            f"{item['selector'] or 'any'}:{item['remaining']:g}<{item['declared']:g}"
            for item in decision.binding
        )
        super().__init__(
            f"budget {decision.decision} for {decision.phase} launch "
            f"(provider={decision.provider} model={decision.model}): {binding}"
        )


class BudgetLedgerCorruption(RuntimeError):
    """A compacted ledger cannot be reconstructed from its checkpoint."""


@dataclasses.dataclass(frozen=True)
class BudgetRunOutcome:
    """A run's authoritative-or-unknown terminal usage, for recovery."""

    stats: Mapping[str, object]
    provider: str


@dataclasses.dataclass(frozen=True)
class BudgetDecision:
    admitted: bool
    decision: str
    project: str = ""
    provider: str = ""
    phase: str = ""
    model: str = ""
    effort: str = ""
    reservation_id: str = ""
    run_id: str = ""
    generation: int = 0
    declared: float = 0.0
    metric: str = ""
    binding: tuple[dict[str, object], ...] = ()
    warnings: tuple[dict[str, object], ...] = ()

    @property
    def deferred(self) -> bool:
        return self.decision == "defer"

    @property
    def blocked(self) -> bool:
        return not self.admitted and self.decision in {"block", "defer"}

    def scope(self) -> dict[str, str]:
        return {
            "project": self.project,
            "provider": self.provider,
            "phase": self.phase,
            "model": self.model,
            "effort": self.effort,
        }

    def to_json(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "decision": self.decision,
            "scope": self.scope(),
            "reservation_id": self.reservation_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "declared": self.declared,
            "metric": self.metric,
            "binding": [dict(item) for item in self.binding],
            "warnings": [dict(item) for item in self.warnings],
        }


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, number)


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def budget_dimensions(stats: object, provider: str) -> dict[str, float]:
    """Full usage-dimension breakdown for one run, in provider-agnostic keys."""

    mapping = stats if isinstance(stats, Mapping) else {}
    dims = {key: _number(mapping.get(key)) for key in BUDGET_DIMENSIONS}
    if (
        not _reported(mapping, "total_tokens")
        and _reported(mapping, "input_tokens")
        and _reported(mapping, "output_tokens")
    ):
        dims["total_tokens"] = dims["input_tokens"] + dims["output_tokens"]
    dims["non_cached_input_tokens"] = float(non_cached_input_tokens(mapping))
    dims["fresh_input_tokens"] = float(fresh_input_tokens(mapping, provider))
    return dims


def metric_value(dimensions: Mapping[str, float], metric: str) -> float:
    return _number(dimensions.get(metric))


def _reported(mapping: Mapping[str, object], key: str) -> bool:
    value = mapping.get(key)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def reported_metrics(stats: object) -> frozenset[str]:
    """Selectable metrics a terminal usage record actually reported.

    Presence, not value: an explicit numeric zero is reported, an absent key is
    not. Derived metrics inherit presence from their source dimension so a run
    that reports raw token counts still counts as having reported the fresh and
    gross views of them.
    """

    mapping = stats if isinstance(stats, Mapping) else {}
    present: set[str] = set()
    for metric in BUDGET_METRICS:
        if metric == "non_cached_input_tokens":
            if _reported(mapping, "input_tokens"):
                present.add(metric)
        elif metric == "total_tokens":
            if (
                _reported(mapping, "total_tokens")
                or _reported(mapping, "input_tokens")
                and _reported(mapping, "output_tokens")
            ):
                present.add(metric)
        elif _reported(mapping, metric):
            present.add(metric)
    return frozenset(present)


def usage_is_authoritative(stats: object) -> bool:
    """True only when the run carries reported, non-unavailable provider usage.

    Unknown or unavailable usage returns False so the caller applies the
    fail-safe charge instead of a silent zero.
    """

    mapping = stats if isinstance(stats, Mapping) else {}
    if mapping.get("usage_unavailable_reason"):
        return False
    source = mapping.get("usage_source")
    if not isinstance(source, str) or source in {"", "unavailable"}:
        return False
    return True


def _within_window(occurred_at: object, now: datetime, window_hours: float) -> bool:
    if window_hours <= 0:
        return True
    timestamp = parse_timestamp(occurred_at)
    if timestamp is None:
        # A reconciled charge with an unparseable timestamp is retained rather
        # than dropped: forgetting spend would understate consumption.
        return True
    return timestamp >= now - timedelta(hours=window_hours)


def _scope_of(record: Mapping[str, object]) -> dict[str, str]:
    return {
        "project": string_value(record.get("project")),
        "provider": string_value(record.get("provider")),
        "phase": string_value(record.get("phase")),
        "model": string_value(record.get("model")),
        "effort": string_value(record.get("effort")),
    }


def _scope_signature(scope: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(string_value(scope.get(key)) for key in BUDGET_SCOPE_KEYS)


def _signature_scope(signature: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(BUDGET_SCOPE_KEYS, signature))


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _terminal_integrity(
    record: Mapping[str, object], reserved: Mapping[str, object]
) -> str:
    """Return the corruption class, or "" when the terminal record is valid.

    Enforces the durable identity/schema contract: schema version, exact run
    identity and reservation generation, a bounded enum reason, and numeric usage
    fields. Any failure keeps the reservation live rather than closing it at a
    fabricated zero.
    """

    schema = record.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        return "missing_fields"
    if schema < 1 or schema > BUDGET_SCHEMA_VERSION:
        return "missing_fields"
    record_run_id = string_value(record.get("run_id"))
    if not record_run_id:
        return "missing_fields"
    if record_run_id != string_value(reserved.get("run_id")):
        return "identity_mismatch"
    reason = record.get("reason")
    if not isinstance(reason, str) or not _SAFE_REASON_RE.match(reason):
        return "missing_fields"
    owner = record.get("owner_run_id")
    if owner is not None and string_value(owner) != string_value(
        reserved.get("owner_run_id")
    ):
        return "identity_mismatch"
    generation = record.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation != _int_or_zero(reserved.get("generation"))
    ):
        return "generation_mismatch"
    if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE:
        if reason not in BUDGET_RECONCILE_REASONS:
            return "missing_fields"
        if not _finite_number(record.get("charge")):
            return "invalid_charge"
        if not isinstance(record.get("metric"), str):
            return "missing_fields"
        if not isinstance(record.get("authoritative"), bool):
            return "missing_fields"
        dimensions = record.get("dimensions")
        if dimensions is not None:
            if not isinstance(dimensions, Mapping):
                return "invalid_charge"
            for key, value in dimensions.items():
                if key not in BUDGET_DIMENSIONS or not _finite_number(value):
                    return "invalid_charge"
    return ""


@dataclasses.dataclass
class _LedgerState:
    reserved: dict[str, dict[str, object]]
    terminal: dict[str, dict[str, object]]
    reservation_counts: dict[str, int]
    # Folded window-0 (cumulative) consumption per full scope signature, plus the
    # audit counters carried forward from the durable checkpoint.
    cumulative: dict[tuple[str, ...], float] = dataclasses.field(default_factory=dict)
    folded_decision_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    folded_reservation_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    folded_integrity: dict[str, int] = dataclasses.field(default_factory=dict)
    integrity: dict[str, int] = dataclasses.field(default_factory=dict)
    generation: int = 0

    def live_reservations(self) -> list[dict[str, object]]:
        return [
            record
            for reservation_id, record in self.reserved.items()
            if reservation_id not in self.terminal
        ]

    def note_integrity(self, integrity_class: str) -> None:
        self.integrity[integrity_class] = self.integrity.get(integrity_class, 0) + 1


def _ledger_state(
    records: list[dict[str, object]],
    *,
    checkpoint: Mapping[str, object] | None = None,
    generation: int = 0,
    pre_integrity: Mapping[str, int] | None = None,
) -> _LedgerState:
    cumulative: dict[tuple[str, ...], float] = {}
    folded_decisions: dict[str, int] = {}
    folded_reservations: dict[str, int] = {}
    folded_integrity: dict[str, int] = {}
    checkpoint_matches = (
        checkpoint is not None
        and generation > 0
        and _checkpoint_is_valid(checkpoint, generation)
    )
    if checkpoint_matches:
        assert checkpoint is not None
        cumulative = _checkpoint_cumulative(checkpoint)
        folded_decisions = _checkpoint_counts(checkpoint.get("decision_counts"))
        folded_reservations = _checkpoint_counts(checkpoint.get("reservation_counts"))
        folded_integrity = _checkpoint_counts(checkpoint.get("integrity"))
    state = _LedgerState(
        reserved={},
        terminal={},
        reservation_counts={},
        cumulative=cumulative,
        folded_decision_counts=folded_decisions,
        folded_reservation_counts=folded_reservations,
        folded_integrity=folded_integrity,
        generation=generation,
    )
    for integrity_class, count in (pre_integrity or {}).items():
        if count:
            state.integrity[integrity_class] = (
                state.integrity.get(integrity_class, 0) + count
            )
    pending_terminals: list[dict[str, object]] = []
    for record in records:
        reservation_id = string_value(record.get("reservation_id"))
        if not reservation_id:
            continue
        record_type = record.get("record_type")
        if record_type == BUDGET_RESERVED_RECORD_TYPE:
            state.reserved[reservation_id] = record
            state.reservation_counts[reservation_id] = (
                state.reservation_counts.get(reservation_id, 0) + 1
            )
        elif record_type in BUDGET_TERMINAL_RECORD_TYPES:
            pending_terminals.append(record)
    # Terminals are validated against their reservation after all reserved rows
    # are known, so an out-of-order or orphaned terminal is classified, not
    # silently applied.
    for record in pending_terminals:
        reservation_id = string_value(record.get("reservation_id"))
        reserved = state.reserved.get(reservation_id)
        if reserved is None:
            state.note_integrity("orphan_terminal")
            continue
        if reservation_id in state.terminal:
            state.note_integrity("duplicate_terminal")
            continue
        integrity_class = _terminal_integrity(record, reserved)
        if integrity_class:
            state.note_integrity(integrity_class)
            continue
        state.terminal[reservation_id] = record
    return state


def _checkpoint_cumulative(
    checkpoint: Mapping[str, object],
) -> dict[tuple[str, ...], float]:
    cumulative: dict[tuple[str, ...], float] = {}
    raw = checkpoint.get("cumulative")
    if not isinstance(raw, list):
        return cumulative
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        signature = _scope_signature(entry)
        cumulative[signature] = cumulative.get(signature, 0.0) + _number(
            entry.get("charge")
        )
    return cumulative


def _checkpoint_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(key, str) and isinstance(raw, int) and not isinstance(raw, bool):
            counts[key] = raw
    return counts


def _checkpoint_is_valid(checkpoint: Mapping[str, object], generation: int) -> bool:
    expected_keys = {
        "schema_version",
        "generation",
        "updated_at",
        "cumulative",
        "decision_counts",
        "reservation_counts",
        "integrity",
    }
    if set(checkpoint) != expected_keys:
        return False
    if checkpoint.get("schema_version") != BUDGET_CHECKPOINT_SCHEMA_VERSION:
        return False
    checkpoint_generation = checkpoint.get("generation")
    if (
        isinstance(checkpoint_generation, bool)
        or not isinstance(checkpoint_generation, int)
        or checkpoint_generation != generation
        or checkpoint_generation <= 0
    ):
        return False
    updated_at = checkpoint.get("updated_at")
    if not isinstance(updated_at, str) or parse_timestamp(updated_at) is None:
        return False
    cumulative = checkpoint.get("cumulative")
    if not isinstance(cumulative, list):
        return False
    signatures: set[tuple[str, ...]] = set()
    for entry in cumulative:
        if not isinstance(entry, Mapping):
            return False
        if set(entry) != {*BUDGET_SCOPE_KEYS, "charge"}:
            return False
        if any(not isinstance(entry.get(key), str) for key in BUDGET_SCOPE_KEYS):
            return False
        if not _finite_number(entry.get("charge")):
            return False
        signature = _scope_signature(entry)
        if signature in signatures:
            return False
        signatures.add(signature)
    allowed_counts = (
        set(BUDGET_DECISIONS) | {"warnings"},
        {"reconciled", "released", "fail_safe_applied"},
        set(BUDGET_INTEGRITY_CLASSES),
    )
    for field, allowed in zip(
        ("decision_counts", "reservation_counts", "integrity"),
        allowed_counts,
    ):
        counts = checkpoint.get(field)
        if not isinstance(counts, Mapping):
            return False
        for key, value in counts.items():
            if (
                key not in allowed
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                return False
    return True


def _checkpoint_digest(checkpoint: Mapping[str, object]) -> str:
    """Unkeyed digest binding a journal header to its checkpoint file.

    This detects a torn, stale, or rolled-back checkpoint, not tampering: the
    header and the checkpoint sit in the same state directory under the same
    permissions, so any writer able to replace one can write a matching pair.
    """

    payload = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _charge_of(
    reservation_id: str,
    state: _LedgerState,
    *,
    now: datetime,
    window_hours: float,
) -> float | None:
    """Metric-unit charge a reservation contributes to an in-window cap.

    Returns None when the reservation's charge falls outside the window and so
    must not count. Live reservations always count (their conservative declared
    allowance); released reservations contribute zero.
    """

    terminal = state.terminal.get(reservation_id)
    reserved = state.reserved[reservation_id]
    if terminal is None:
        return _number(reserved.get("declared"))
    if terminal.get("record_type") == BUDGET_RELEASED_RECORD_TYPE:
        return 0.0
    if not _within_window(terminal.get("occurred_at"), now, window_hours):
        return None
    return _number(terminal.get("charge"))


def _consumption(state: _LedgerState, limit, *, now: datetime) -> float:
    total = 0.0
    if limit.window_hours <= 0:
        # Folded charges only ever count for cumulative (window-0) caps; they are
        # older than every window and so are excluded from windowed caps.
        for signature, charge in state.cumulative.items():
            if limit.matches(**_signature_scope(signature)):
                total += charge
    for reservation_id, reserved in state.reserved.items():
        scope = _scope_of(reserved)
        if not limit.matches(**scope):
            continue
        charge = _charge_of(
            reservation_id,
            state,
            now=now,
            window_hours=limit.window_hours,
        )
        if charge is not None:
            total += charge
    return total


class BudgetStore:
    """Append-only durable ledger of budget reservations and reconciliations."""

    def __init__(self, path: Path, config: BudgetConfig):
        self.path = path
        self.config = config

    # -- reservation / admission -------------------------------------------

    def reserve(
        self,
        *,
        reservation_id: str,
        run_id: str,
        project: str,
        provider: str,
        phase: str,
        model: str,
        effort: str,
        now: datetime | None = None,
    ) -> BudgetDecision:
        """Atomically admit or refuse a launch against every matching cap."""

        provider = normalize_provider_label(provider)[0]
        model = normalize_model_label(model)[0]
        declared = self.config.declared_for(phase)
        base = BudgetDecision(
            admitted=True,
            decision="admit",
            project=project,
            provider=provider,
            phase=phase,
            model=model,
            effort=effort,
            reservation_id=reservation_id,
            run_id=run_id,
            declared=declared,
            metric=self.config.metric,
        )
        if not self.config.enabled:
            return dataclasses.replace(base, decision="disabled")
        if not reservation_id:
            raise ValueError("budget reservation requires a reservation id")
        current = now or datetime.now(UTC)
        scope = base.scope()
        self._ensure_parent()
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                if (
                    reservation_id in state.reserved
                    and reservation_id not in state.terminal
                ):
                    # Reservation ids are single-use. Replaying one while it is
                    # live would overwrite its reserved row (last write wins),
                    # after which the earlier generation's terminal validates
                    # against the newer row, is classified generation_mismatch,
                    # and its charge disappears from consumption.
                    raise ValueError(
                        f"budget reservation id is already live: {reservation_id}"
                    )
                binding: list[dict[str, object]] = []
                warnings: list[dict[str, object]] = []
                for index, limit in enumerate(self.config.limits):
                    if not limit.matches(**scope):
                        continue
                    consumed = _consumption(state, limit, now=current)
                    remaining = limit.limit - consumed
                    item = {
                        "limit_index": index,
                        "selector": limit.selector(),
                        "limit": limit.limit,
                        "consumed": round(consumed, 6),
                        "remaining": round(remaining, 6),
                        "declared": declared,
                        "metric": self.config.metric,
                    }
                    if declared > remaining:
                        binding.append(item)
                    elif (
                        limit.warn_at is not None
                        and consumed + declared >= limit.warn_at * limit.limit
                    ):
                        warnings.append({**item, "warn_at": limit.warn_at})
                if binding:
                    decision = BudgetDecision(
                        admitted=False,
                        decision=self.config.on_insufficient,
                        project=project,
                        provider=provider,
                        phase=phase,
                        model=model,
                        effort=effort,
                        reservation_id=reservation_id,
                        run_id=run_id,
                        declared=declared,
                        metric=self.config.metric,
                        binding=tuple(binding),
                        warnings=tuple(warnings),
                    )
                    self._append_unlocked(self._decision_record(decision))
                    self._maybe_compact_unlocked(current)
                    return decision
                generation = state.reservation_counts.get(reservation_id, 0) + 1
                self._append_unlocked(
                    {
                        "schema_version": BUDGET_SCHEMA_VERSION,
                        "record_type": BUDGET_RESERVED_RECORD_TYPE,
                        "occurred_at": utc_now_iso(),
                        "reservation_id": reservation_id,
                        "run_id": run_id,
                        "owner_run_id": run_id,
                        "generation": generation,
                        "host": socket.gethostname(),
                        "pid": os.getpid(),
                        "owner_process_birth_id": process_birth_identity(os.getpid()),
                        "project": project,
                        "provider": provider,
                        "phase": phase,
                        "model": model,
                        "effort": effort,
                        "metric": self.config.metric,
                        "declared": declared,
                        "fail_safe": self.config.fail_safe,
                        "fail_safe_amount": self.config.fail_safe_amount,
                    }
                )
                admitted = dataclasses.replace(
                    base, generation=generation, warnings=tuple(warnings)
                )
                self._append_unlocked(self._decision_record(admitted))
                self._maybe_compact_unlocked(current)
                return admitted

    # -- reconciliation -----------------------------------------------------

    def reconcile(
        self,
        *,
        reservation_id: str,
        run_id: str,
        dimensions: Mapping[str, float],
        authoritative: bool,
        reason: str = "terminal_usage",
        present_metrics: frozenset[str] | None = None,
    ) -> bool:
        """Charge a reservation exactly once against its terminal usage."""

        self._ensure_parent()
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                return self._reconcile_unlocked(
                    reservation_id,
                    run_id=run_id,
                    dimensions=dimensions,
                    authoritative=authoritative,
                    reason=reason,
                    state=state,
                    present_metrics=present_metrics,
                )

    def reconcile_reservation(
        self,
        *,
        reservation_id: str,
        run_id: str,
        stats: Mapping[str, object],
        provider: str,
        reason: str = "terminal_usage",
    ) -> bool:
        """Reconcile one specific reservation from a phase launch's usage.

        Used for review/remediation/closure launches whose usage belongs to a
        single reservation id rather than to the whole run, so phase accounting
        stays separate.
        """

        if not self.config.enabled or not reservation_id:
            return False
        provider = normalize_provider_label(provider)[0]
        dimensions = budget_dimensions(stats, provider)
        authoritative = usage_is_authoritative(stats)
        present = reported_metrics(stats)
        self._ensure_parent()
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                return self._reconcile_unlocked(
                    reservation_id,
                    run_id=run_id,
                    dimensions=dimensions,
                    authoritative=authoritative,
                    reason=reason,
                    state=state,
                    present_metrics=present,
                )

    def reconcile_run(
        self,
        *,
        run_id: str,
        stats: Mapping[str, object],
        provider: str,
    ) -> int:
        """Reconcile the primary (implementation) reservation of a finished run.

        Only the reservation whose id equals the run id is charged here: a run's
        aggregate terminal usage is the implementation launch's usage, so review
        and remediation reservations are reconciled by their own launches and
        must not be charged the implementation figure.
        """

        if not self.config.enabled or not run_id:
            return 0
        provider = normalize_provider_label(provider)[0]
        dimensions = budget_dimensions(stats, provider)
        authoritative = usage_is_authoritative(stats)
        present = reported_metrics(stats)
        self._ensure_parent()
        reconciled = 0
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                if self._reconcile_unlocked(
                    run_id,
                    run_id=run_id,
                    dimensions=dimensions,
                    authoritative=authoritative,
                    reason="terminal_usage",
                    state=state,
                    present_metrics=present,
                ):
                    reconciled += 1
        return reconciled

    def release(
        self,
        *,
        reservation_id: str,
        run_id: str,
        reason: str,
    ) -> bool:
        """Release a reservation whose launch never happened (charge zero)."""

        if not self.config.enabled or not reservation_id:
            return False
        safe_reason = reason if _SAFE_REASON_RE.match(reason or "") else "released"
        self._ensure_parent()
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                reserved = state.reserved.get(reservation_id)
                if reserved is None:
                    return False
                if reservation_id in state.terminal:
                    return False
                self._append_unlocked(
                    {
                        "schema_version": BUDGET_SCHEMA_VERSION,
                        "record_type": BUDGET_RELEASED_RECORD_TYPE,
                        "occurred_at": utc_now_iso(),
                        "reservation_id": reservation_id,
                        "run_id": run_id,
                        "owner_run_id": string_value(reserved.get("owner_run_id")),
                        "generation": _int_or_zero(reserved.get("generation")),
                        "reason": safe_reason,
                    }
                )
                return True

    def recover_abandoned(
        self,
        *,
        resolve: Callable[[str], BudgetRunOutcome | None],
        process_alive: Callable[[int | None, str], bool],
        process_birth: Callable[[int], str] = process_birth_identity,
        now: datetime | None = None,
        grace_seconds: float = 900.0,
    ) -> int:
        """Reconcile reservations whose owner finished or provably died.

        A finished owner reconciles from its authoritative-or-unknown terminal
        usage. An owner that is neither resolvable nor alive, past the grace
        period, is charged through the fail-safe policy — never released for
        free — so a crashed launch cannot silently vanish from the ledger.
        """

        if not self.config.enabled:
            return 0
        current = now or datetime.now(UTC)
        recovered = 0
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                state = self._load_state_unlocked()
                for record in state.live_reservations():
                    reservation_id = string_value(record.get("reservation_id"))
                    owner_run_id = string_value(record.get("owner_run_id"))
                    # Resolve by reservation id, not run id: a run's terminal
                    # usage belongs only to its primary reservation (id == run
                    # id). Review/remediation reservations resolve to None here
                    # and fall through to the fail-safe path, so a run's usage is
                    # never double-charged across phase boundaries.
                    outcome = resolve(reservation_id) if reservation_id else None
                    if outcome is not None:
                        dimensions = budget_dimensions(outcome.stats, outcome.provider)
                        if self._reconcile_unlocked(
                            reservation_id,
                            run_id=owner_run_id,
                            dimensions=dimensions,
                            authoritative=usage_is_authoritative(outcome.stats),
                            reason="recovered_terminal_usage",
                            state=state,
                            present_metrics=reported_metrics(outcome.stats),
                        ):
                            recovered += 1
                        continue
                    pid = record.get("pid")
                    host = string_value(record.get("host"))
                    if host and host != socket.gethostname():
                        # Another host owns this reservation; only its own
                        # supervisor can judge the owner's liveness.
                        continue
                    owner_pid = pid if isinstance(pid, int) else None
                    if process_alive(owner_pid, host):
                        expected_birth = string_value(
                            record.get("owner_process_birth_id")
                        )
                        if owner_pid is None or not expected_birth:
                            # Legacy or unverifiable identities stay reserved:
                            # liveness without birth identity cannot rule out PID reuse.
                            continue
                        actual_birth = process_birth(owner_pid)
                        if not actual_birth or actual_birth == expected_birth:
                            continue
                    age = current - (
                        parse_timestamp(record.get("occurred_at")) or current
                    )
                    if age < timedelta(seconds=grace_seconds):
                        continue
                    if self._reconcile_unlocked(
                        reservation_id,
                        run_id=owner_run_id,
                        dimensions={},
                        authoritative=False,
                        reason="recovered_abandoned",
                        state=state,
                    ):
                        recovered += 1
        return recovered

    def _reconcile_unlocked(
        self,
        reservation_id: str,
        *,
        run_id: str,
        dimensions: Mapping[str, float],
        authoritative: bool,
        reason: str,
        state: _LedgerState,
        present_metrics: frozenset[str] | None = None,
    ) -> bool:
        if not reservation_id or reservation_id not in state.reserved:
            return False
        if reservation_id in state.terminal:
            return False
        reserved = state.reserved[reservation_id]
        metric = string_value(reserved.get("metric")) or self.config.metric
        declared = _number(reserved.get("declared"))
        fail_safe_reason = ""
        if authoritative and (present_metrics is None or metric in present_metrics):
            charge = metric_value(dimensions, metric)
            fail_safe_applied = False
        elif authoritative:
            # Usage was authoritative overall but omitted the configured metric.
            # An explicit numeric zero counts as a real zero (metric is present);
            # a missing metric must not close the reservation at zero.
            charge = _fail_safe_charge(reserved, declared)
            fail_safe_applied = True
            fail_safe_reason = "missing_selected_metric"
        else:
            charge = _fail_safe_charge(reserved, declared)
            fail_safe_applied = True
            fail_safe_reason = "unknown_usage"
        record = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "record_type": BUDGET_RECONCILED_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "reservation_id": reservation_id,
            "run_id": run_id,
            "owner_run_id": string_value(reserved.get("owner_run_id")),
            "generation": _int_or_zero(reserved.get("generation")),
            "metric": metric,
            "charge": round(charge, 6),
            "authoritative": authoritative,
            "fail_safe_applied": fail_safe_applied,
            "fail_safe_reason": fail_safe_reason,
            "reason": reason,
            "dimensions": {
                key: round(_number(dimensions.get(key)), 6) for key in BUDGET_DIMENSIONS
            },
        }
        self._append_unlocked(record)
        state.terminal[reservation_id] = record
        return True

    # -- projection ---------------------------------------------------------

    def projection(self, *, project: str = "", now: datetime | None = None) -> dict:
        current = now or datetime.now(UTC)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                _generation, records, _pre, _digest = self._read_journal_unlocked()
                state = self._load_state_unlocked()
        limits_report: list[dict[str, object]] = []
        for index, limit in enumerate(self.config.limits):
            reserved_total = 0.0
            consumed_total = 0.0
            if limit.window_hours <= 0:
                for signature, charge in state.cumulative.items():
                    if limit.matches(**_signature_scope(signature)):
                        consumed_total += charge
            for reservation_id, record in state.reserved.items():
                if not limit.matches(**_scope_of(record)):
                    continue
                terminal = state.terminal.get(reservation_id)
                if terminal is None:
                    reserved_total += _number(record.get("declared"))
                elif terminal.get(
                    "record_type"
                ) == BUDGET_RECONCILED_RECORD_TYPE and _within_window(
                    terminal.get("occurred_at"), current, limit.window_hours
                ):
                    consumed_total += _number(terminal.get("charge"))
            committed = consumed_total + reserved_total
            limits_report.append(
                {
                    "limit_index": index,
                    "selector": limit.selector(),
                    "limit": limit.limit,
                    "window_hours": limit.window_hours,
                    "consumed": round(consumed_total, 6),
                    "reserved": round(reserved_total, 6),
                    "committed": round(committed, 6),
                    "remaining": round(limit.limit - committed, 6),
                    "utilization": round(committed / limit.limit, 6)
                    if limit.limit
                    else None,
                    "warn_at": limit.warn_at,
                    "warning": (
                        limit.warn_at is not None
                        and committed >= limit.warn_at * limit.limit
                    ),
                    "exceeded": committed > limit.limit,
                }
            )
        limits_out, limits_truncated = _bounded(limits_report)
        routes_out, routes_truncated = _bounded(_route_evidence(state, current))
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "project": project,
            "enabled": self.config.enabled,
            "metric": self.config.metric,
            "fail_safe": self.config.fail_safe,
            "limits": limits_out,
            "limits_truncated": limits_truncated,
            "routes": routes_out,
            "routes_truncated": routes_truncated,
            "reservations": _reservation_counts(state),
            "decisions": _decision_counts(records, state),
            "integrity": _merge_counts(state.folded_integrity, state.integrity),
            "compaction": {
                "generation": state.generation,
                "active_records": len(records),
                "folded_scopes": len(state.cumulative),
            },
        }

    # -- storage primitives -------------------------------------------------

    def _decision_record(self, decision: BudgetDecision) -> dict[str, object]:
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "record_type": BUDGET_DECISION_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "reservation_id": decision.reservation_id,
            "run_id": decision.run_id,
            "decision": decision.decision,
            "project": decision.project,
            "provider": decision.provider,
            "phase": decision.phase,
            "model": decision.model,
            "effort": decision.effort,
            "declared": decision.declared,
            "metric": decision.metric,
            "binding": [dict(item) for item in decision.binding],
            "warning_count": len(decision.warnings),
        }

    @property
    def checkpoint_path(self) -> Path:
        return self.path.with_name(self.path.name + ".checkpoint")

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_state_unlocked(self) -> _LedgerState:
        generation, records, pre_integrity, expected_digest = (
            self._read_journal_unlocked()
        )
        checkpoint = self._read_checkpoint_unlocked()
        checkpoint_generation = (
            checkpoint.get("generation") if checkpoint is not None else None
        )
        if (
            generation == 0
            and checkpoint is not None
            and isinstance(checkpoint_generation, int)
            and not isinstance(checkpoint_generation, bool)
            and _checkpoint_is_valid(checkpoint, checkpoint_generation)
        ):
            raise BudgetLedgerCorruption(
                "budget checkpoint exists without its canonical journal header"
            )
        if generation > 0 and (
            checkpoint is None
            or not _checkpoint_is_valid(checkpoint, generation)
            or not expected_digest
            or _checkpoint_digest(checkpoint) != expected_digest
        ):
            raise BudgetLedgerCorruption(
                "compacted budget ledger has no valid matching checkpoint"
            )
        return _ledger_state(
            records,
            checkpoint=checkpoint,
            generation=generation,
            pre_integrity=pre_integrity,
        )

    def _append_unlocked(self, record: Mapping[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()

    def read_records(self) -> list[dict[str, object]]:
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                return self._read_records_unlocked()

    def _read_records_unlocked(self) -> list[dict[str, object]]:
        return self._read_journal_unlocked()[1]

    def _read_journal_unlocked(
        self,
    ) -> tuple[int, list[dict[str, object]], dict[str, int], str]:
        generation = 0
        checkpoint_digest = ""
        records: list[dict[str, object]] = []
        pre_integrity: dict[str, int] = {}
        if not self.path.exists():
            return generation, records, pre_integrity, checkpoint_digest
        text = self.path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # A torn / partially written row must never close a reservation;
                # it is counted and skipped.
                pre_integrity["malformed_json"] = (
                    pre_integrity.get("malformed_json", 0) + 1
                )
                continue
            if not isinstance(payload, dict):
                pre_integrity["malformed_json"] = (
                    pre_integrity.get("malformed_json", 0) + 1
                )
                continue
            record_type = payload.get("record_type")
            if record_type == BUDGET_JOURNAL_HEADER_RECORD_TYPE:
                expected_keys = {
                    "schema_version",
                    "record_type",
                    "generation",
                    "checkpoint_sha256",
                }
                schema_version = payload.get("schema_version")
                header_generation = payload.get("generation")
                digest = payload.get("checkpoint_sha256")
                if (
                    line_number != 0
                    or set(payload) != expected_keys
                    or isinstance(schema_version, bool)
                    or not isinstance(schema_version, int)
                    or schema_version != BUDGET_SCHEMA_VERSION
                    or isinstance(header_generation, bool)
                    or not isinstance(header_generation, int)
                    or header_generation <= 0
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise BudgetLedgerCorruption(
                        "budget journal has an invalid or misplaced header"
                    )
                generation = header_generation
                checkpoint_digest = digest
                continue
            if record_type in BUDGET_RECORD_TYPES:
                records.append(payload)
            else:
                pre_integrity["unknown_record_type"] = (
                    pre_integrity.get("unknown_record_type", 0) + 1
                )
        return generation, records, pre_integrity, checkpoint_digest

    def _read_checkpoint_unlocked(self) -> dict[str, object] | None:
        path = self.checkpoint_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Generation zero still has the complete legacy journal to replay.
            # A compacted journal rejects this missing checkpoint in _load_state.
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != BUDGET_CHECKPOINT_SCHEMA_VERSION:
            return None
        return payload

    def _record_count_unlocked(self) -> int:
        return len(self._read_journal_unlocked()[1])

    def _maybe_compact_unlocked(self, now: datetime) -> None:
        if self._record_count_unlocked() < BUDGET_COMPACTION_THRESHOLD:
            return
        self._compact_unlocked(now)

    def compact(self, *, now: datetime | None = None) -> bool:
        """Fold closed, out-of-window reservations into the durable checkpoint."""

        if not self.config.enabled:
            return False
        current = now or datetime.now(UTC)
        self._ensure_parent()
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                return self._compact_unlocked(current)

    def _compact_unlocked(self, now: datetime) -> bool:
        state = self._load_state_unlocked()
        max_window = max(
            (limit.window_hours for limit in self.config.limits),
            default=0.0,
        )
        fold_before = now - timedelta(hours=max_window) if max_window > 0 else now
        generation, records, _pre, _digest = self._read_journal_unlocked()
        # Fold closed reservations whose terminal charge is older than every
        # window, so it can only ever contribute to cumulative (window-0) caps.
        fold_reservations: set[str] = set()
        cumulative = dict(state.cumulative)
        folded_reservations = dict(state.folded_reservation_counts)
        for reservation_id, reserved in state.reserved.items():
            terminal = state.terminal.get(reservation_id)
            if terminal is None:
                continue
            occurred = parse_timestamp(terminal.get("occurred_at"))
            if occurred is None or occurred >= fold_before:
                continue
            fold_reservations.add(reservation_id)
            if terminal.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE:
                signature = _scope_signature(_scope_of(reserved))
                cumulative[signature] = cumulative.get(signature, 0.0) + _number(
                    terminal.get("charge")
                )
                folded_reservations["reconciled"] = (
                    folded_reservations.get("reconciled", 0) + 1
                )
                if terminal.get("fail_safe_applied") is True:
                    folded_reservations["fail_safe_applied"] = (
                        folded_reservations.get("fail_safe_applied", 0) + 1
                    )
            else:
                folded_reservations["released"] = (
                    folded_reservations.get("released", 0) + 1
                )
        # Fold aged-out decision rows (including sustained-denial blocks that have
        # no reservation to close) into durable audit counters.
        folded_decisions = dict(state.folded_decision_counts)
        fold_decision_indices: set[int] = set()
        for index, record in enumerate(records):
            if record.get("record_type") != BUDGET_DECISION_RECORD_TYPE:
                continue
            occurred = parse_timestamp(record.get("occurred_at"))
            if occurred is None or occurred >= fold_before:
                continue
            fold_decision_indices.add(index)
            decision = string_value(record.get("decision"))
            if decision:
                folded_decisions[decision] = folded_decisions.get(decision, 0) + 1
            folded_decisions["warnings"] = folded_decisions.get("warnings", 0) + int(
                record.get("warning_count") or 0
            )
        if not fold_reservations and not fold_decision_indices:
            return False
        new_generation = generation + 1
        checkpoint = {
            "schema_version": BUDGET_CHECKPOINT_SCHEMA_VERSION,
            "generation": new_generation,
            "updated_at": utc_now_iso(),
            "cumulative": [
                {**_signature_scope(signature), "charge": round(charge, 6)}
                for signature, charge in sorted(cumulative.items())
            ],
            "decision_counts": folded_decisions,
            "reservation_counts": folded_reservations,
            # Torn/unknown rows are dropped by the rewrite, so their audit counts
            # are carried into the checkpoint to preserve the corruption record.
            "integrity": _merge_counts(state.folded_integrity, state.integrity),
        }
        remaining: list[dict[str, object]] = []
        for index, record in enumerate(records):
            reservation_id = string_value(record.get("reservation_id"))
            record_type = record.get("record_type")
            if (
                record_type
                in (BUDGET_RESERVED_RECORD_TYPE, *BUDGET_TERMINAL_RECORD_TYPES)
                and reservation_id in fold_reservations
            ):
                continue
            if (
                record_type == BUDGET_DECISION_RECORD_TYPE
                and index in fold_decision_indices
            ):
                continue
            remaining.append(record)
        checkpoint_digest = _checkpoint_digest(checkpoint)
        # Crash-safe ordering: publish the checkpoint (tagged new_generation)
        # BEFORE truncating the journal to the new generation. A crash between
        # leaves checkpoint.generation ahead of the first-generation journal,
        # whose still-complete rows remain replayable. Once any compaction has
        # landed, a generation or digest mismatch fails closed because prior
        # folded rows no longer exist in the journal.
        self._write_checkpoint_atomic(checkpoint)
        self._rewrite_journal_atomic(new_generation, remaining, checkpoint_digest)
        return True

    def _write_checkpoint_atomic(self, checkpoint: Mapping[str, object]) -> None:
        path = self.checkpoint_path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(checkpoint, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _rewrite_journal_atomic(
        self,
        generation: int,
        records: list[dict[str, object]],
        checkpoint_digest: str,
    ) -> None:
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        header = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "record_type": BUDGET_JOURNAL_HEADER_RECORD_TYPE,
            "generation": generation,
            "checkpoint_sha256": checkpoint_digest,
        }
        lines = [json.dumps(header, separators=(",", ":"))]
        lines.extend(json.dumps(record, separators=(",", ":")) for record in records)
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


def _fail_safe_charge(reserved: Mapping[str, object], declared: float) -> float:
    policy = string_value(reserved.get("fail_safe")) or "reserved"
    if policy == "fixed":
        amount = reserved.get("fail_safe_amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return _number(amount)
    return declared


def _route_evidence(state: _LedgerState, now: datetime) -> list[dict[str, object]]:
    """Per-provider (and per-provider-phase) consumed/reserved, never summed."""

    providers: dict[str, dict[str, object]] = {}
    for reservation_id, record in state.reserved.items():
        provider = string_value(record.get("provider")) or "unknown"
        phase = string_value(record.get("phase")) or "implementation"
        group = providers.setdefault(
            provider,
            {
                "provider": provider,
                "reserved": 0.0,
                "consumed": 0.0,
                "authoritative_consumed": 0.0,
                "fail_safe_consumed": 0.0,
                "phases": {},
            },
        )
        phases = group["phases"]
        assert isinstance(phases, dict)
        phase_group = phases.setdefault(phase, {"reserved": 0.0, "consumed": 0.0})
        terminal = state.terminal.get(reservation_id)
        if terminal is None:
            declared = _number(record.get("declared"))
            group["reserved"] = float(group["reserved"]) + declared
            phase_group["reserved"] += declared
        elif terminal.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE:
            charge = _number(terminal.get("charge"))
            group["consumed"] = float(group["consumed"]) + charge
            phase_group["consumed"] += charge
            if terminal.get("fail_safe_applied") is True:
                group["fail_safe_consumed"] = (
                    float(group["fail_safe_consumed"]) + charge
                )
            else:
                group["authoritative_consumed"] = (
                    float(group["authoritative_consumed"]) + charge
                )
    for group in providers.values():
        for key in (
            "reserved",
            "consumed",
            "authoritative_consumed",
            "fail_safe_consumed",
        ):
            group[key] = round(float(group[key]), 6)
        phases = group["phases"]
        assert isinstance(phases, dict)
        group["phases"] = {
            name: {
                "reserved": round(values["reserved"], 6),
                "consumed": round(values["consumed"], 6),
            }
            for name, values in sorted(phases.items())
        }
    return [providers[name] for name in sorted(providers)]


def _merge_counts(*sources: Mapping[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in sources:
        for key, value in source.items():
            if isinstance(value, int) and not isinstance(value, bool):
                merged[key] = merged.get(key, 0) + value
    return merged


def _bounded(items: list) -> tuple[list, int]:
    """Deterministically cap a projection list, reporting the dropped count."""

    if len(items) <= BUDGET_MAX_PROJECTION_ITEMS:
        return items, 0
    return items[:BUDGET_MAX_PROJECTION_ITEMS], len(items) - BUDGET_MAX_PROJECTION_ITEMS


def _reservation_counts(state: _LedgerState) -> dict[str, int]:
    live = 0
    reconciled = 0
    released = 0
    fail_safe = 0
    for reservation_id in state.reserved:
        terminal = state.terminal.get(reservation_id)
        if terminal is None:
            live += 1
        elif terminal.get("record_type") == BUDGET_RELEASED_RECORD_TYPE:
            released += 1
        else:
            reconciled += 1
            if terminal.get("fail_safe_applied") is True:
                fail_safe += 1
    folded = state.folded_reservation_counts
    return {
        "live": live,
        "reconciled": reconciled + folded.get("reconciled", 0),
        "released": released + folded.get("released", 0),
        "fail_safe_applied": fail_safe + folded.get("fail_safe_applied", 0),
    }


def _decision_counts(
    records: list[dict[str, object]], state: _LedgerState
) -> dict[str, int]:
    counts = {decision: 0 for decision in BUDGET_DECISIONS}
    warnings = 0
    for record in records:
        if record.get("record_type") != BUDGET_DECISION_RECORD_TYPE:
            continue
        decision = string_value(record.get("decision"))
        if decision in counts:
            counts[decision] += 1
        warnings += int(record.get("warning_count") or 0)
    counts["warnings"] = warnings
    return _merge_counts(counts, state.folded_decision_counts)


@dataclasses.dataclass(frozen=True)
class PhaseBudget:
    """Admission/reconciliation hook for a single non-implementation phase.

    Injected into the review router and used by the remediation launcher so
    every runtime-owned model launch reserves before starting and reconciles its
    own usage against its own reservation, keeping phase accounting separate.
    """

    store: BudgetStore
    project: str

    @property
    def enabled(self) -> bool:
        return self.store.config.enabled

    def admit(
        self,
        *,
        reservation_id: str,
        run_id: str,
        provider: str,
        phase: str,
        model: str,
        effort: str,
    ) -> str:
        if not self.enabled:
            return ""
        decision = self.store.reserve(
            reservation_id=reservation_id,
            run_id=run_id,
            project=self.project,
            provider=provider,
            phase=phase,
            model=model,
            effort=effort,
        )
        if not decision.admitted:
            raise BudgetReservationDenied(decision)
        return reservation_id

    def reconcile(
        self,
        *,
        reservation_id: str,
        run_id: str,
        stats: Mapping[str, object],
        provider: str,
    ) -> None:
        if reservation_id:
            self.store.reconcile_reservation(
                reservation_id=reservation_id,
                run_id=run_id,
                stats=stats,
                provider=provider,
            )

    def release(self, *, reservation_id: str, run_id: str, reason: str) -> None:
        if reservation_id:
            self.store.release(
                reservation_id=reservation_id, run_id=run_id, reason=reason
            )


def canonical_repo_root(config: object) -> Path:
    """Repository-common root shared by every linked Git worktree.

    All linked worktrees of one repository report the same main worktree, so
    keying budget authority on it (rather than the checkout basename or the
    per-worktree state_dir) gives one ledger per repository while unrelated
    repositories, which have distinct roots, stay isolated. Non-Git or
    unresolvable repos fall back to their own path, preserving prior behavior.
    """

    from vibe_loop.config import git_main_worktree_path

    repo = Path(getattr(config, "repo"))
    main = git_main_worktree_path(repo)
    if main is not None:
        return main.resolve()
    return repo.resolve()


def _budget_enabled(config: object) -> bool:
    return bool(getattr(getattr(config, "budget", None), "enabled", False))


def resolve_budget_ledger_path(config: object) -> Path:
    """Single shared ledger file for the whole repository (all worktrees).

    Only enabled budgets resolve the repository-common root; a disabled/absent
    budget keeps the plain per-repo path and does no Git work, so unconfigured
    repositories behave exactly as before (nothing is ever written there).
    """

    state_dir = str(getattr(config, "state_dir", ".vibe-loop"))
    if not _budget_enabled(config):
        return Path(getattr(config, "repo")).resolve() / state_dir / "budget.jsonl"
    return canonical_repo_root(config) / state_dir / "budget.jsonl"


def resolve_budget_project(config: object) -> str:
    """Repository identity a ``budget.limits[].project`` selector matches.

    This is the basename of the repository-common root, so every linked
    worktree of one checkout shares a cap. It is a different axis from
    ``config.resolve_project_binding``, which resolves the task-backend
    namespace: spend is scoped to a repository, dispatch to a project, and the
    two are not required to agree.
    """

    if not _budget_enabled(config):
        return Path(getattr(config, "repo")).name
    return canonical_repo_root(config).name


def process_alive_locally(pid: int | None, host: str) -> bool:
    """Owner-liveness probe for the local host, defaulting to alive when unknown.

    Unknown liveness must not trigger recovery, so a missing pid returns True
    (conservative: never reclaim a reservation we cannot prove is dead).
    """

    if pid is None:
        return True
    from vibe_loop.locks import pid_exists

    return pid_exists(pid)
