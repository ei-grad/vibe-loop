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
import json
import os
import socket
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vibe_loop.config import BudgetConfig
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


def budget_dimensions(stats: object, provider: str) -> dict[str, float]:
    """Full usage-dimension breakdown for one run, in provider-agnostic keys."""

    mapping = stats if isinstance(stats, Mapping) else {}
    dims = {key: _number(mapping.get(key)) for key in BUDGET_DIMENSIONS}
    dims["non_cached_input_tokens"] = float(non_cached_input_tokens(mapping))
    dims["fresh_input_tokens"] = float(fresh_input_tokens(mapping, provider))
    return dims


def metric_value(dimensions: Mapping[str, float], metric: str) -> float:
    return _number(dimensions.get(metric))


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


@dataclasses.dataclass(frozen=True)
class _LedgerState:
    reserved: dict[str, dict[str, object]]
    terminal: dict[str, dict[str, object]]
    reservation_counts: dict[str, int]

    def live_reservations(self) -> list[dict[str, object]]:
        return [
            record
            for reservation_id, record in self.reserved.items()
            if reservation_id not in self.terminal
        ]


def _ledger_state(records: list[dict[str, object]]) -> _LedgerState:
    reserved: dict[str, dict[str, object]] = {}
    terminal: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}
    for record in records:
        reservation_id = string_value(record.get("reservation_id"))
        if not reservation_id:
            continue
        record_type = record.get("record_type")
        if record_type == BUDGET_RESERVED_RECORD_TYPE:
            reserved[reservation_id] = record
            counts[reservation_id] = counts.get(reservation_id, 0) + 1
        elif record_type in BUDGET_TERMINAL_RECORD_TYPES:
            terminal.setdefault(reservation_id, record)
    return _LedgerState(reserved=reserved, terminal=terminal, reservation_counts=counts)


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


def _consumption(records_state: _LedgerState, limit, *, now: datetime) -> float:
    total = 0.0
    for reservation_id, reserved in records_state.reserved.items():
        scope = _scope_of(reserved)
        if not limit.matches(**scope):
            continue
        charge = _charge_of(
            reservation_id,
            records_state,
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                records = self._read_records_unlocked()
                state = _ledger_state(records)
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
    ) -> bool:
        """Charge a reservation exactly once against its terminal usage."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                records = self._read_records_unlocked()
                state = _ledger_state(records)
                return self._reconcile_unlocked(
                    reservation_id,
                    run_id=run_id,
                    dimensions=dimensions,
                    authoritative=authoritative,
                    reason=reason,
                    state=state,
                )

    def reconcile_run(
        self,
        *,
        run_id: str,
        stats: Mapping[str, object],
        provider: str,
    ) -> int:
        """Reconcile every live reservation a finished run still owns."""

        if not self.config.enabled or not run_id:
            return 0
        provider = normalize_provider_label(provider)[0]
        dimensions = budget_dimensions(stats, provider)
        authoritative = usage_is_authoritative(stats)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reconciled = 0
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                records = self._read_records_unlocked()
                state = _ledger_state(records)
                for record in state.live_reservations():
                    if string_value(record.get("owner_run_id")) != run_id:
                        continue
                    if self._reconcile_unlocked(
                        string_value(record.get("reservation_id")),
                        run_id=run_id,
                        dimensions=dimensions,
                        authoritative=authoritative,
                        reason="terminal_usage",
                        state=state,
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                records = self._read_records_unlocked()
                state = _ledger_state(records)
                if reservation_id not in state.reserved:
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
                        "reason": reason,
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
                records = self._read_records_unlocked()
                state = _ledger_state(records)
                for record in state.live_reservations():
                    reservation_id = string_value(record.get("reservation_id"))
                    owner_run_id = string_value(record.get("owner_run_id"))
                    outcome = resolve(owner_run_id) if owner_run_id else None
                    if outcome is not None:
                        dimensions = budget_dimensions(outcome.stats, outcome.provider)
                        if self._reconcile_unlocked(
                            reservation_id,
                            run_id=owner_run_id,
                            dimensions=dimensions,
                            authoritative=usage_is_authoritative(outcome.stats),
                            reason="recovered_terminal_usage",
                            state=state,
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
    ) -> bool:
        if not reservation_id or reservation_id not in state.reserved:
            return False
        if reservation_id in state.terminal:
            return False
        reserved = state.reserved[reservation_id]
        metric = string_value(reserved.get("metric")) or self.config.metric
        declared = _number(reserved.get("declared"))
        if authoritative:
            charge = metric_value(dimensions, metric)
            fail_safe_applied = False
        else:
            charge = _fail_safe_charge(reserved, declared)
            fail_safe_applied = True
        record = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "record_type": BUDGET_RECONCILED_RECORD_TYPE,
            "occurred_at": utc_now_iso(),
            "reservation_id": reservation_id,
            "run_id": run_id,
            "metric": metric,
            "charge": round(charge, 6),
            "authoritative": authoritative,
            "fail_safe_applied": fail_safe_applied,
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
        records = self.read_records()
        state = _ledger_state(records)
        limits_report: list[dict[str, object]] = []
        for index, limit in enumerate(self.config.limits):
            reserved_total = 0.0
            consumed_total = 0.0
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
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "project": project,
            "enabled": self.config.enabled,
            "metric": self.config.metric,
            "fail_safe": self.config.fail_safe,
            "limits": limits_report,
            "routes": _route_evidence(state, current),
            "reservations": _reservation_counts(state),
            "decisions": _decision_counts(records),
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

    def _append_unlocked(self, record: Mapping[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()

    def read_records(self) -> list[dict[str, object]]:
        with _APPEND_LOCK:
            with append_record_lock(self.path):
                return self._read_records_unlocked()

    def _read_records_unlocked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        records: list[dict[str, object]] = []
        for line in self.path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("record_type") in BUDGET_RECORD_TYPES
            ):
                records.append(payload)
        return records


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
    return {
        "live": live,
        "reconciled": reconciled,
        "released": released,
        "fail_safe_applied": fail_safe,
    }


def _decision_counts(records: list[dict[str, object]]) -> dict[str, int]:
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
    return counts


def process_alive_locally(pid: int | None, host: str) -> bool:
    """Owner-liveness probe for the local host, defaulting to alive when unknown.

    Unknown liveness must not trigger recovery, so a missing pid returns True
    (conservative: never reclaim a reservation we cannot prove is dead).
    """

    if pid is None:
        return True
    from vibe_loop.locks import pid_exists

    return pid_exists(pid)
