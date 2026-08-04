from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from vibe_loop.budget import (
    BUDGET_DECISION_RECORD_TYPE,
    BUDGET_JOURNAL_HEADER_RECORD_TYPE,
    BUDGET_RECONCILED_RECORD_TYPE,
    BUDGET_RESERVED_RECORD_TYPE,
    BUDGET_SCHEMA_VERSION,
    BudgetLedgerCorruption,
    BudgetReservationDenied,
    BudgetRunOutcome,
    BudgetStore,
    PhaseBudget,
    budget_dimensions,
    reported_metrics,
    resolve_budget_ledger_path,
    resolve_budget_project,
    usage_is_authoritative,
)
from vibe_loop.config import (
    BUDGET_MAX_LIMITS,
    BUDGET_METRICS,
    AgentConfig,
    BudgetConfig,
    BudgetLimit,
    OrchestrationConfig,
    VibeConfig,
    parse_budget,
)
from vibe_loop.orchestration import (
    CandidateRecord,
    GateResult,
    GateRunSummary,
    ProvisionedWorkspace,
    ReviewConcurrencyBudget,
    ReviewBudgetExhausted,
    ReviewExecutionError,
    ReviewFinding,
    ReviewRouter,
    ReviewStageResultError,
    ReviewWaitIncomplete,
)
from vibe_loop.runner import (
    StreamingCommandResult,
    VibeRunner,
    run_streaming_command,  # noqa: F401
)
from vibe_loop.runs import RunResult, RunStore
from vibe_loop.tasks import Task
from vibe_loop.telemetry import ProviderUsage, unavailable_usage


def _multiprocess_reserve(path: str, index: int, start, results) -> None:
    config = make_config(default_declared=100.0, limits=(BudgetLimit(limit=400.0),))
    store = BudgetStore(Path(path), config)
    start.wait()
    decision = store.reserve(
        reservation_id=f"process-{index}",
        run_id=f"process-{index}",
        project="proj",
        provider="anthropic",
        phase="implementation",
        model="claude-opus-4-8",
        effort="high",
    )
    results.put(decision.admitted)


def _reserve_then_crash(path: str, ready) -> None:
    store = BudgetStore(
        Path(path),
        make_config(default_declared=100.0, limits=(BudgetLimit(limit=400.0),)),
    )
    store.reserve(
        reservation_id="crashed-process",
        run_id="crashed-process",
        project="proj",
        provider="anthropic",
        phase="implementation",
        model="claude-opus-4-8",
        effort="high",
    )
    ready.set()
    os._exit(0)


def _worktree_reserve_process(repo: str, reservation_id: str, start, results) -> None:
    from vibe_loop.config import load_config

    config = load_config(Path(repo))
    store = BudgetStore(resolve_budget_ledger_path(config), config.budget)
    start.wait()
    decision = store.reserve(
        reservation_id=reservation_id,
        run_id=reservation_id,
        project=resolve_budget_project(config),
        provider="anthropic",
        phase="implementation",
        model="claude-opus-4-8",
        effort="high",
    )
    results.put(decision.admitted)


def make_config(**overrides: object) -> BudgetConfig:
    base = {
        "enabled": True,
        "metric": "total_tokens",
        "default_declared": 400.0,
    }
    base.update(overrides)
    return BudgetConfig(**base)


def authoritative_stats(**fields: object) -> dict[str, object]:
    return {"usage_source": "native:claude:result", **fields}


def anthropic_stats(total: int) -> dict[str, object]:
    return {
        "total_tokens": total,
        "input_tokens": total,
        "output_tokens": 0,
        "usage_source": "native:claude:result",
    }


class BudgetStoreCoreTests(unittest.TestCase):
    def _store(self, directory: str, config: BudgetConfig) -> BudgetStore:
        return BudgetStore(Path(directory) / "budget.jsonl", config)

    def _reserve(self, store: BudgetStore, reservation_id: str, **overrides):
        kwargs = {
            "reservation_id": reservation_id,
            "run_id": reservation_id,
            "project": "proj",
            "provider": "anthropic",
            "phase": "implementation",
            "model": "claude-opus-4-8",
            "effort": "high",
        }
        kwargs.update(overrides)
        return store.reserve(**kwargs)

    def test_unconfigured_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, BudgetConfig())
            decision = self._reserve(store, "r1")
            self.assertTrue(decision.admitted)
            self.assertEqual(decision.decision, "disabled")
            self.assertFalse((Path(directory) / "budget.jsonl").exists())

    def test_reservation_denied_when_insufficient(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=500.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            first = self._reserve(store, "r1")
            self.assertTrue(first.admitted)
            second = self._reserve(store, "r2")
            self.assertFalse(second.admitted)
            self.assertEqual(second.decision, "block")
            self.assertTrue(second.binding)
            records = store.read_records()
            reserved = [
                r
                for r in records
                if r.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            ]
            decisions = [
                r
                for r in records
                if r.get("record_type") == BUDGET_DECISION_RECORD_TYPE
            ]
            # Exactly one reservation persisted; the denied launch made none.
            self.assertEqual(len(reserved), 1)
            self.assertEqual(
                sorted(r["decision"] for r in decisions), ["admit", "block"]
            )

    def test_concurrent_reservations_cannot_oversubscribe(self) -> None:
        # Ten concurrent launches, a cap that fits exactly four declared
        # allowances. No thread may see stale remaining and oversubscribe.
        config = make_config(default_declared=100.0, limits=(BudgetLimit(limit=400.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            with ThreadPoolExecutor(max_workers=10) as pool:
                decisions = list(
                    pool.map(
                        lambda index: self._reserve(store, f"r{index}"),
                        range(10),
                    )
                )
            admitted = [d for d in decisions if d.admitted]
            self.assertEqual(len(admitted), 4)
            reserved = [
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            ]
            self.assertEqual(len(reserved), 4)

    def test_independent_processes_cannot_oversubscribe_shared_ledger(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "budget.jsonl")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_multiprocess_reserve,
                    args=(path, index, start, results),
                )
                for index in range(10)
            ]
            try:
                for process in processes:
                    process.start()
                start.set()
                for process in processes:
                    process.join(timeout=15)
                self.assertTrue(all(not process.is_alive() for process in processes))
                self.assertEqual([process.exitcode for process in processes], [0] * 10)
                admissions = [results.get(timeout=2) for _ in processes]
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=2)
                results.close()
            self.assertEqual(sum(admissions), 4)
            records = BudgetStore(
                Path(path),
                make_config(default_declared=100.0, limits=(BudgetLimit(limit=400.0),)),
            ).read_records()
            reservations = [
                record
                for record in records
                if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            ]
            self.assertEqual(len(reservations), 4)

    def test_defer_policy_reports_defer(self) -> None:
        config = make_config(
            on_insufficient="defer", limits=(BudgetLimit(limit=100.0),)
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            decision = self._reserve(store, "r1")
            self.assertFalse(decision.admitted)
            self.assertEqual(decision.decision, "defer")
            self.assertTrue(decision.deferred)

    def test_reconcile_is_exactly_once(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=100000.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            first = store.reconcile_run(
                run_id="r1", stats=anthropic_stats(250), provider="anthropic"
            )
            second = store.reconcile_run(
                run_id="r1", stats=anthropic_stats(999), provider="anthropic"
            )
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            reconciled = [
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            ]
            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0]["charge"], 250.0)
            self.assertTrue(reconciled[0]["authoritative"])
            self.assertFalse(reconciled[0]["fail_safe_applied"])

    def test_unknown_usage_is_charged_fail_safe_not_zero(self) -> None:
        config = make_config(default_declared=400.0, limits=(BudgetLimit(limit=1e9),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            store.reconcile_run(
                run_id="r1",
                stats={"usage_unavailable_reason": "provider_usage_not_reported"},
                provider="anthropic",
            )
            reconciled = next(
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )
            self.assertFalse(reconciled["authoritative"])
            self.assertTrue(reconciled["fail_safe_applied"])
            self.assertEqual(reconciled["charge"], 400.0)

    def test_fixed_fail_safe_amount_overrides_declared(self) -> None:
        config = make_config(
            fail_safe="fixed",
            fail_safe_amount=777.0,
            default_declared=400.0,
            limits=(BudgetLimit(limit=1e9),),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            store.reconcile_run(run_id="r1", stats={}, provider="anthropic")
            reconciled = next(
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )
            self.assertEqual(reconciled["charge"], 777.0)

    def test_release_returns_the_allowance(self) -> None:
        config = make_config(default_declared=100.0, limits=(BudgetLimit(limit=100.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self.assertTrue(self._reserve(store, "r1").admitted)
            # The cap is full, so a second launch is blocked...
            self.assertFalse(self._reserve(store, "r2").admitted)
            # ...until the first reservation is released without launching.
            self.assertTrue(store.release(reservation_id="r1", run_id="r1", reason="x"))
            self.assertTrue(self._reserve(store, "r3").admitted)
            # Release is idempotent against a closed reservation.
            self.assertFalse(
                store.release(reservation_id="r1", run_id="r1", reason="y")
            )

    def test_recovery_reconciles_from_terminal_result(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=1e9),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            outcomes = {"r1": BudgetRunOutcome(anthropic_stats(320), "anthropic")}
            recovered = store.recover_abandoned(
                resolve=outcomes.get,
                process_alive=lambda pid, host: False,
            )
            self.assertEqual(recovered, 1)
            reconciled = next(
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )
            self.assertEqual(reconciled["charge"], 320.0)
            self.assertTrue(reconciled["authoritative"])
            self.assertEqual(reconciled["reason"], "recovered_terminal_usage")
            # A second recovery pass is a no-op (no double-spend).
            self.assertEqual(
                store.recover_abandoned(
                    resolve=outcomes.get, process_alive=lambda pid, host: False
                ),
                0,
            )

    def test_recovery_of_dead_owner_charges_fail_safe(self) -> None:
        config = make_config(default_declared=400.0, limits=(BudgetLimit(limit=1e9),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            past = datetime.now(UTC) - timedelta(hours=2)
            with patch("vibe_loop.budget.utc_now_iso", return_value=past.isoformat()):
                self._reserve(store, "r1")
            recovered = store.recover_abandoned(
                resolve=lambda run_id: None,
                process_alive=lambda pid, host: False,
                grace_seconds=900.0,
            )
            self.assertEqual(recovered, 1)
            reconciled = next(
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )
            self.assertFalse(reconciled["authoritative"])
            self.assertEqual(reconciled["charge"], 400.0)
            self.assertEqual(reconciled["reason"], "recovered_abandoned")

    def test_recovery_leaves_live_owners_alone(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=1e9),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            recovered = store.recover_abandoned(
                resolve=lambda run_id: None,
                process_alive=lambda pid, host: True,
            )
            self.assertEqual(recovered, 0)

    def test_recovery_detects_reused_pid_by_process_birth_identity(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=1e9),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            recovered = store.recover_abandoned(
                resolve=lambda run_id: None,
                process_alive=lambda pid, host: True,
                process_birth=lambda pid: "different-boot:123",
                grace_seconds=0,
            )
            self.assertEqual(recovered, 1)
            self.assertEqual(
                store.recover_abandoned(
                    resolve=lambda run_id: None,
                    process_alive=lambda pid, host: True,
                    process_birth=lambda pid: "different-boot:123",
                    grace_seconds=0,
                ),
                0,
            )

    def test_crashed_process_reservation_recovers_exactly_once(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.jsonl"
            ready = context.Event()
            process = context.Process(
                target=_reserve_then_crash, args=(str(path), ready)
            )
            process.start()
            self.assertTrue(ready.wait(timeout=10))
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                self.fail("reservation owner did not exit")
            self.assertEqual(process.exitcode, 0)

            config = make_config(
                default_declared=100.0, limits=(BudgetLimit(limit=400.0),)
            )
            store = self._store(directory, config)
            reservation = next(
                record
                for record in store.read_records()
                if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            )
            self.assertEqual(reservation["pid"], process.pid)
            self.assertTrue(reservation["owner_process_birth_id"])

            self.assertEqual(
                store.recover_abandoned(
                    resolve=lambda run_id: None,
                    process_alive=lambda pid, host: False,
                    grace_seconds=0,
                ),
                1,
            )
            self.assertEqual(
                store.recover_abandoned(
                    resolve=lambda run_id: None,
                    process_alive=lambda pid, host: False,
                    grace_seconds=0,
                ),
                0,
            )
            reconciliations = [
                record
                for record in store.read_records()
                if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            ]
            self.assertEqual(len(reconciliations), 1)
            self.assertEqual(reconciliations[0]["charge"], 100.0)
            self.assertEqual(reconciliations[0]["reason"], "recovered_abandoned")

    def test_phase_and_provider_are_separately_attributable(self) -> None:
        # Two caps: one Anthropic implementation, one OpenAI review. Each launch
        # charges only its own scope; neither leaks into the other.
        config = make_config(
            limits=(
                BudgetLimit(limit=1000.0, provider="anthropic", phase="implementation"),
                BudgetLimit(limit=1000.0, provider="openai", phase="review"),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "impl", provider="anthropic", phase="implementation")
            review = store.reserve(
                reservation_id="rev",
                run_id="rev",
                project="proj",
                provider="openai",
                phase="review",
                model="gpt-5-codex",
                effort="",
            )
            self.assertTrue(review.admitted)
            projection = store.projection(project="proj")
            providers = {r["provider"]: r for r in projection["routes"]}
            self.assertIn("anthropic", providers)
            self.assertIn("openai", providers)
            self.assertEqual(set(providers["anthropic"]["phases"]), {"implementation"})
            self.assertEqual(set(providers["openai"]["phases"]), {"review"})

    def test_projection_is_content_free_and_typed(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=1000.0, warn_at=0.5),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            self._reserve(store, "r1")
            store.reconcile_run(
                run_id="r1", stats=anthropic_stats(600), provider="anthropic"
            )
            projection = store.projection(project="proj")
            limit = projection["limits"][0]
            self.assertTrue(limit["warning"])
            self.assertFalse(limit["exceeded"])
            self.assertEqual(limit["consumed"], 600.0)
            # Every value is a label or a number - no task text or commands.
            for route in projection["routes"]:
                for value in route.values():
                    self.assertIsInstance(value, (str, int, float, dict))

    def test_window_limit_expires_old_consumption(self) -> None:
        config = make_config(limits=(BudgetLimit(limit=500.0, window_hours=1.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, config)
            old = datetime.now(UTC) - timedelta(hours=3)
            # A reservation reconciled three hours ago fills the cap, but its
            # charge is outside the one-hour window, so it must not block now.
            with patch("vibe_loop.budget.utc_now_iso", return_value=old.isoformat()):
                self._reserve(store, "r1")
                store.reconcile(
                    reservation_id="r1",
                    run_id="r1",
                    dimensions=budget_dimensions(anthropic_stats(500), "anthropic"),
                    authoritative=True,
                )
            fresh = self._reserve(store, "r2")
            self.assertTrue(fresh.admitted)


class BudgetHelperTests(unittest.TestCase):
    def test_usage_is_authoritative_rejects_unavailable(self) -> None:
        self.assertFalse(usage_is_authoritative({"usage_source": "unavailable"}))
        self.assertFalse(
            usage_is_authoritative(
                {"usage_unavailable_reason": "provider_usage_not_reported"}
            )
        )
        self.assertTrue(
            usage_is_authoritative(
                {"usage_source": "native:claude:result", "total_tokens": 5}
            )
        )

    def test_dimensions_include_fresh_and_cost(self) -> None:
        stats = {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 10,
            "cost_usd": 1.5,
        }
        dims = budget_dimensions(stats, "openai")
        self.assertEqual(dims["non_cached_input_tokens"], 60.0)
        self.assertEqual(dims["fresh_input_tokens"], 60.0)
        self.assertEqual(dims["cost_usd"], 1.5)


class BudgetConfigParseTests(unittest.TestCase):
    def test_default_is_disabled_and_empty(self) -> None:
        config = parse_budget({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.limits, ())
        self.assertEqual(config.declared, ())

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_budget({"nope": 1})

    def test_enabled_requires_a_declared_allowance(self) -> None:
        with self.assertRaises(ValueError):
            parse_budget({"enabled": True})

    def test_fixed_fail_safe_requires_amount(self) -> None:
        with self.assertRaises(ValueError):
            parse_budget(
                {"enabled": True, "default_declared": 10, "fail_safe": "fixed"}
            )

    def test_invalid_metric_provider_and_phase_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_budget({"metric": "bananas"})
        with self.assertRaises(ValueError):
            parse_budget({"limits": [{"limit": 1, "provider": "gemini"}]})
        with self.assertRaises(ValueError):
            parse_budget({"limits": [{"limit": 1, "phase": "napping"}]})
        with self.assertRaises(ValueError):
            parse_budget({"declared": {"napping": 10}})

    def test_limit_requires_positive_cap_and_valid_warn(self) -> None:
        with self.assertRaises(ValueError):
            parse_budget({"limits": [{"provider": "anthropic"}]})
        with self.assertRaises(ValueError):
            parse_budget({"limits": [{"limit": 100, "warn_at": 1.5}]})

    def test_declared_and_limit_round_trip(self) -> None:
        config = parse_budget(
            {
                "enabled": True,
                "default_declared": 100,
                "declared": {"implementation": 200, "review": 150},
                "limits": [
                    {
                        "provider": "anthropic",
                        "phase": "implementation",
                        "limit": 5000,
                        "warn_at": 0.8,
                        "window_hours": 24,
                    }
                ],
            }
        )
        self.assertEqual(config.declared_for("implementation"), 200.0)
        self.assertEqual(config.declared_for("planning"), 100.0)
        self.assertEqual(len(config.limits), 1)
        self.assertEqual(config.limits[0].warn_at, 0.8)


class BudgetRunnerIntegrationTests(unittest.TestCase):
    def _git_repo(self, directory: str) -> Path:
        repo = Path(directory) / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tester@example.com"],
            cwd=repo,
            check=True,
        )
        (repo / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return repo

    def _runner(self, repo: Path, budget: BudgetConfig) -> VibeRunner:
        return VibeRunner(
            VibeConfig(
                repo=repo,
                agent=AgentConfig(
                    command="worker {prompt}",
                    prompt_dialect="codex",
                    skill_ref_prefix="$",
                ),
                orchestration=OrchestrationConfig(
                    mode="worker-owned",
                    explicit_keys=frozenset({"mode"}),
                ),
                budget=budget,
            )
        )

    def test_denied_reservation_launches_no_worker_process(self) -> None:
        budget = BudgetConfig(
            enabled=True,
            metric="total_tokens",
            default_declared=1000.0,
            limits=(BudgetLimit(limit=1.0),),
        )
        task = Task(task_id="TASK-01", title="Task", status="Next")
        launched: list[str] = []

        def sentinel(*args, **kwargs):
            launched.append("launched")
            raise AssertionError("worker process must not launch when denied")

        with tempfile.TemporaryDirectory() as directory:
            repo = self._git_repo(directory)
            runner = self._runner(repo, budget)
            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", sentinel):
                    with self.assertRaises(BudgetReservationDenied):
                        runner.run_task(task)
            self.assertEqual(launched, [])
            run_results = [
                r
                for r in runner.run_store.read_records()
                if r.get("record_type") in {None, "run_result"}
            ]
            self.assertEqual(run_results, [])
            decisions = [
                r
                for r in runner.budget_store.read_records()
                if r.get("record_type") == BUDGET_DECISION_RECORD_TYPE
                and r.get("decision") == "block"
            ]
            self.assertEqual(len(decisions), 1)
            # The task lock was released, so the task stays dispatchable.
            self.assertFalse(runner.lock_manager.is_locked("TASK-01"))

    def test_record_result_reconciles_live_reservation(self) -> None:
        budget = BudgetConfig(
            enabled=True,
            metric="total_tokens",
            default_declared=400.0,
            limits=(BudgetLimit(limit=1e9),),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = self._git_repo(directory)
            runner = self._runner(repo, budget)
            runner.budget_store.reserve(
                reservation_id="run-1",
                run_id="run-1",
                project=repo.name,
                provider="anthropic",
                phase="implementation",
                model="claude-opus-4-8",
                effort="high",
            )
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=repo / "run-1.log",
                start_main="a",
                end_main="b",
                model_provider="anthropic",
                stats=anthropic_stats(275),
            )
            runner.record_result(result)
            reconciled = [
                r
                for r in runner.budget_store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            ]
            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0]["charge"], 275.0)

    def test_recover_abandoned_budget_uses_run_store_terminal(self) -> None:
        budget = BudgetConfig(
            enabled=True,
            metric="total_tokens",
            default_declared=400.0,
            limits=(BudgetLimit(limit=1e9),),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = self._git_repo(directory)
            runner = self._runner(repo, budget)
            runner.budget_store.reserve(
                reservation_id="run-9",
                run_id="run-9",
                project=repo.name,
                provider="anthropic",
                phase="implementation",
                model="claude-opus-4-8",
                effort="high",
            )
            runner.run_store.append_result(
                RunResult(
                    run_id="run-9",
                    task_id="TASK-01",
                    classification="completed",
                    exit_code=0,
                    log_path=repo / "run-9.log",
                    start_main="a",
                    end_main="b",
                    model_provider="anthropic",
                    stats=anthropic_stats(180),
                )
            )
            recovered = runner.recover_abandoned_budget()
            self.assertEqual(recovered, 1)
            reconciled = next(
                r
                for r in runner.budget_store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )
            self.assertEqual(reconciled["charge"], 180.0)


METRIC_CASES = {
    "input_tokens": ({"input_tokens": 0}, {"input_tokens": 30}, 30.0),
    "output_tokens": ({"output_tokens": 0}, {"output_tokens": 25}, 25.0),
    "total_tokens": ({"total_tokens": 0}, {"total_tokens": 120}, 120.0),
    "non_cached_input_tokens": ({"input_tokens": 0}, {"input_tokens": 40}, 40.0),
    "cache_read_input_tokens": (
        {"cache_read_input_tokens": 0},
        {"cache_read_input_tokens": 15},
        15.0,
    ),
    "cache_creation_input_tokens": (
        {"cache_creation_input_tokens": 0},
        {"cache_creation_input_tokens": 12},
        12.0,
    ),
    "cost_usd": ({"cost_usd": 0}, {"cost_usd": 3.5}, 3.5),
}


class F1MissingMetricTests(unittest.TestCase):
    """A metric absent from authoritative usage charges fail-safe, not zero."""

    def _reconcile(self, metric: str, stats: dict) -> dict:
        config = make_config(
            metric=metric, default_declared=500.0, limits=(BudgetLimit(limit=1e12),)
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", config)
            store.reserve(
                reservation_id="r1",
                run_id="r1",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="claude-opus-4-8",
                effort="high",
            )
            store.reconcile_reservation(
                reservation_id="r1", run_id="r1", stats=stats, provider="anthropic"
            )
            return next(
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
            )

    def test_metric_coverage_is_exhaustive(self) -> None:
        self.assertEqual(set(METRIC_CASES), set(BUDGET_METRICS))

    def test_missing_metric_charges_fail_safe(self) -> None:
        for metric in BUDGET_METRICS:
            with self.subTest(metric=metric):
                record = self._reconcile(metric, authoritative_stats())
                self.assertTrue(record["fail_safe_applied"])
                self.assertEqual(record["fail_safe_reason"], "missing_selected_metric")
                self.assertTrue(record["authoritative"])
                self.assertEqual(record["charge"], 500.0)

    def test_explicit_zero_metric_is_a_real_zero(self) -> None:
        for metric, (zero_fields, _value_fields, _expected) in METRIC_CASES.items():
            with self.subTest(metric=metric):
                record = self._reconcile(metric, authoritative_stats(**zero_fields))
                self.assertFalse(record["fail_safe_applied"])
                self.assertEqual(record["charge"], 0.0)

    def test_positive_metric_charges_the_value(self) -> None:
        for metric, (_zero_fields, value_fields, expected) in METRIC_CASES.items():
            with self.subTest(metric=metric):
                record = self._reconcile(metric, authoritative_stats(**value_fields))
                self.assertFalse(record["fail_safe_applied"])
                self.assertEqual(record["charge"], expected)

    def test_reported_metrics_presence(self) -> None:
        self.assertNotIn("total_tokens", reported_metrics({"input_tokens": 5}))
        self.assertNotIn("total_tokens", reported_metrics({"output_tokens": 5}))
        self.assertIn(
            "total_tokens",
            reported_metrics({"input_tokens": 5, "output_tokens": 7}),
        )
        self.assertIn("non_cached_input_tokens", reported_metrics({"input_tokens": 0}))
        self.assertNotIn("cost_usd", reported_metrics({"input_tokens": 5}))

    def test_total_tokens_requires_complete_sources_or_explicit_total(self) -> None:
        cases = (
            ("input_only", {"input_tokens": 5}, 500.0, True),
            ("output_only", {"output_tokens": 7}, 500.0, True),
            ("both", {"input_tokens": 5, "output_tokens": 7}, 12.0, False),
            (
                "explicit_zero",
                {"total_tokens": 0, "input_tokens": 5, "output_tokens": 7},
                0.0,
                False,
            ),
            ("no_sources", {}, 500.0, True),
        )
        for name, fields, expected_charge, fail_safe in cases:
            with self.subTest(name=name):
                record = self._reconcile("total_tokens", authoritative_stats(**fields))
                self.assertEqual(record["charge"], expected_charge)
                self.assertEqual(record["fail_safe_applied"], fail_safe)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True
    )


class F2SharedLedgerTests(unittest.TestCase):
    def test_linked_worktrees_share_one_ledger_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            _init_repo(main)
            worktree = Path(directory) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "-b", "feature", str(worktree)],
                cwd=main,
                check=True,
                capture_output=True,
            )
            enabled = make_config(limits=(BudgetLimit(limit=1.0),))
            main_config = VibeConfig(repo=main, budget=enabled)
            wt_config = VibeConfig(repo=worktree, budget=enabled)
            self.assertEqual(
                resolve_budget_ledger_path(main_config),
                resolve_budget_ledger_path(wt_config),
            )
            self.assertEqual(
                resolve_budget_project(main_config),
                resolve_budget_project(wt_config),
            )
            # Identity is not the checkout basename.
            self.assertNotEqual(resolve_budget_project(wt_config), worktree.name)

    def test_unrelated_repos_stay_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo_a = Path(directory) / "a"
            repo_b = Path(directory) / "b"
            _init_repo(repo_a)
            _init_repo(repo_b)
            enabled = make_config(limits=(BudgetLimit(limit=1.0),))
            self.assertNotEqual(
                resolve_budget_ledger_path(VibeConfig(repo=repo_a, budget=enabled)),
                resolve_budget_ledger_path(VibeConfig(repo=repo_b, budget=enabled)),
            )

    def test_non_git_falls_back_to_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "plain"
            repo.mkdir()
            config = VibeConfig(
                repo=repo, budget=make_config(limits=(BudgetLimit(limit=1.0),))
            )
            self.assertEqual(
                resolve_budget_ledger_path(config),
                repo.resolve() / ".vibe-loop" / "budget.jsonl",
            )

    def test_disabled_budget_uses_plain_repo_path_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "main"
            _init_repo(repo)
            worktree = Path(directory) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "-b", "feature", str(worktree)],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            # A disabled budget preserves the per-worktree legacy path and does
            # not consult Git at all.
            config = VibeConfig(repo=worktree)
            with patch("vibe_loop.config.git_main_worktree_path") as git_call:
                path = resolve_budget_ledger_path(config)
                git_call.assert_not_called()
            self.assertEqual(path, worktree.resolve() / ".vibe-loop" / "budget.jsonl")

    def test_independent_processes_two_worktrees_one_cap(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            _init_repo(main)
            (main / ".vibe-loop.toml").write_text(
                "[budget]\nenabled = true\ndefault_declared = 100\n"
                "[[budget.limits]]\nlimit = 400\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "-A"], cwd=main, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "config"],
                cwd=main,
                check=True,
                capture_output=True,
            )
            worktree = Path(directory) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "-b", "feature", str(worktree)],
                cwd=main,
                check=True,
                capture_output=True,
            )
            start = context.Event()
            results: multiprocessing.Queue = context.Queue()
            procs = []
            for index in range(8):
                repo = main if index % 2 == 0 else worktree
                procs.append(
                    context.Process(
                        target=_worktree_reserve_process,
                        args=(str(repo), f"proc-{index}", start, results),
                    )
                )
            for proc in procs:
                proc.start()
            start.set()
            for proc in procs:
                proc.join(30)
            admitted = [results.get() for _ in range(8)]
            self.assertEqual(sum(1 for value in admitted if value), 4)
            ledger = resolve_budget_ledger_path(VibeConfig(repo=main))
            store = BudgetStore(ledger, VibeConfig(repo=main).budget)
            reserved = [
                r
                for r in store.read_records()
                if r.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            ]
            self.assertEqual(len(reserved), 4)


def _reviewer_agent(provider: str = "claude") -> AgentConfig:
    return AgentConfig(
        command=f"{provider} review --model {{model}} --effort {{effort}} {{prompt}}",
        command_source="explicit",
        model="review-model",
        model_source="explicit",
        effort="high",
        effort_source="explicit",
        agent_kind=provider,
        agent_kind_source="explicit",
        executable_kind=provider,
        profile_name="review",
    )


class F3ReviewLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.run_store = RunStore(self.repo / "runs.jsonl")
        candidate = CandidateRecord(
            branch="vibe-loop/TASK-01",
            worktree=self.repo,
            base_main="a" * 40,
            head_commit="b" * 40,
            changed_paths=("src/example.py",),
            source="derived",
        )
        self.gates = GateRunSummary(
            candidate=candidate,
            results=(
                GateResult(
                    config_key="completion.commands[0]",
                    exit_class="passed",
                    exit_code=0,
                    duration_seconds=0.5,
                    log_reference=str(self.repo / "gate.log"),
                    evidence_digest="sha256:" + "c" * 64,
                    candidate_fingerprint=candidate.fingerprint,
                ),
            ),
            candidate_recorded=True,
        )

    def _store(self, config: BudgetConfig) -> BudgetStore:
        return BudgetStore(self.repo / "budget.jsonl", config)

    def _router(self, store: BudgetStore, executor) -> ReviewRouter:
        return ReviewRouter(
            reviewer=_reviewer_agent("claude"),
            reviewer_profile="review",
            run_store=self.run_store,
            run_id="run-1",
            task_id="TASK-01",
            worktree=self.repo,
            policy_references=("REVIEW.md",),
            max_initial_passes=1,
            max_closure_passes=2,
            concurrency=ReviewConcurrencyBudget(1),
            executor=executor,
            session_id_factory=lambda: "session",
            budget=PhaseBudget(store, "proj"),
        )

    def _approve(self, usage: dict | None, *, closure: bool = False):
        def execute(command: str, **kwargs):
            verdict = {
                "verdict": "approve",
                "findings": (
                    [
                        {
                            "id": "F1",
                            "severity": "P1",
                            "summary": "Correct the candidate",
                            "evidence": "Focused reproduction",
                            "files": ["src/example.py"],
                            "state": "remediated",
                        }
                    ]
                    if closure
                    else []
                ),
                "session_id": "",
                "session_id_source": "",
                "continuation_ordinal": 0,
            }
            lines = []
            if usage is not None:
                lines.append(json.dumps({"type": "result", **usage}))
            lines.append(json.dumps(verdict))
            return subprocess.CompletedProcess(command, 0, stdout="\n".join(lines))

        return execute

    def _review_phase(self, router: ReviewRouter, phase: str):
        if phase == "initial_review":
            return router.review(self.gates)
        self.assertEqual(phase, "targeted_closure")
        return router.review(
            self.gates,
            pass_kind="closure:1",
            prior_findings=(
                ReviewFinding(
                    finding_id="F1",
                    severity="P1",
                    summary="Correct the candidate",
                    evidence="Focused reproduction",
                    files=("src/example.py",),
                ),
            ),
        )

    def test_initial_review_denial_launches_no_reviewer(self) -> None:
        store = self._store(
            make_config(default_declared=1000.0, limits=(BudgetLimit(limit=1.0),))
        )
        launched: list[str] = []

        def sentinel(command: str, **kwargs):
            launched.append(command)
            raise AssertionError("reviewer must not launch when denied")

        router = self._router(store, sentinel)
        with self.assertRaises(BudgetReservationDenied):
            router.review(self.gates)
        self.assertEqual(launched, [])
        blocks = [
            r
            for r in store.read_records()
            if r.get("record_type") == BUDGET_DECISION_RECORD_TYPE
            and r.get("decision") == "block"
        ]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["phase"], "initial_review")

    def test_review_success_reconciles_its_own_usage(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        router = self._router(
            store,
            self._approve({"usage": {"input_tokens": 80, "output_tokens": 20}}),
        )
        result = router.review(self.gates)
        self.assertTrue(result.approved)
        reconciled = [
            r
            for r in store.read_records()
            if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["charge"], 100.0)
        self.assertTrue(reconciled[0]["authoritative"])
        self.assertTrue(
            reconciled[0]["reservation_id"].startswith("run-1:initial_review:")
        )

    def test_structured_transient_retry_reserves_and_reconciles_each_launch(
        self,
    ) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        outputs = iter(
            (
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "result",
                                "usage": {
                                    "input_tokens": 80,
                                    "output_tokens": 20,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "verdict": "error",
                                "findings": [],
                                "session_id": "session",
                                "session_id_source": "provider",
                                "continuation_ordinal": 0,
                                "retry_classification": "transient",
                            }
                        ),
                    )
                ),
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "result",
                                "usage": {
                                    "input_tokens": 40,
                                    "output_tokens": 10,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "verdict": "approve",
                                "findings": [],
                                "session_id": "session",
                                "session_id_source": "provider",
                                "continuation_ordinal": 1,
                            }
                        ),
                    )
                ),
            )
        )

        def execute(command: str, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs))

        result = self._router(store, execute).review(self.gates)

        self.assertTrue(result.approved)
        budget_records = store.read_records()
        reserved = [
            record
            for record in budget_records
            if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
        ]
        reconciled = [
            record
            for record in budget_records
            if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reserved), 2)
        self.assertEqual(len(reconciled), 2)
        self.assertEqual(
            {record["reservation_id"] for record in reserved},
            {record["reservation_id"] for record in reconciled},
        )
        self.assertEqual(
            [record["phase"] for record in reserved],
            ["initial_review", "initial_review"],
        )
        self.assertEqual(
            [record["charge"] for record in reconciled],
            [100.0, 50.0],
        )
        verdicts = [
            record
            for record in self.run_store.read_records()
            if record["record_type"] == "review_verdict"
        ]
        self.assertEqual(
            [record["stats"]["phase"] for record in verdicts],
            ["initial_review", "initial_review"],
        )
        self.assertEqual(
            [record["stats"]["provider_usage"]["input_tokens"] for record in verdicts],
            [80, 40],
        )
        review_budgets = [
            record
            for record in self.run_store.read_records()
            if record["record_type"] == "review_budget"
        ]
        self.assertEqual(
            [record["action"] for record in review_budgets],
            ["initialized", "consumed"],
        )
        self.assertEqual(review_budgets[-1]["pass_ordinal"], 1)

    def test_review_timeout_charges_fail_safe(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )

        def execute(command: str, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        router = self._router(store, execute)
        with self.assertRaises(ReviewWaitIncomplete):
            router.review(self.gates)
        reconciled = [
            r
            for r in store.read_records()
            if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        self.assertTrue(reconciled[0]["fail_safe_applied"])
        self.assertEqual(reconciled[0]["charge"], 500.0)

    def test_no_double_accounting_across_phase_boundary(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        # Implementation reservation shares the run id with the review launch.
        store.reserve(
            reservation_id="run-1",
            run_id="run-1",
            project="proj",
            provider="anthropic",
            phase="implementation",
            model="claude-opus-4-8",
            effort="high",
        )
        router = self._router(
            store,
            self._approve({"usage": {"input_tokens": 30, "output_tokens": 10}}),
        )
        router.review(self.gates)
        # The run's aggregate usage reconciles only the implementation reservation.
        store.reconcile_run(
            run_id="run-1", stats=anthropic_stats(900), provider="anthropic"
        )
        reconciled = {
            r["reservation_id"]: r["charge"]
            for r in store.read_records()
            if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        }
        self.assertEqual(reconciled["run-1"], 900.0)
        review_ids = [rid for rid in reconciled if rid != "run-1"]
        self.assertEqual(len(review_ids), 1)
        self.assertEqual(reconciled[review_ids[0]], 40.0)

    def test_recovery_does_not_charge_review_with_run_usage(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        # A review reservation left live (its launch never reconciled).
        store.reserve(
            reservation_id="run-1:initial_review:orphan",
            run_id="run-1",
            project="proj",
            provider="anthropic",
            phase="initial_review",
            model="claude-opus-4-8",
            effort="high",
        )
        # Recovery resolves by reservation id, so the run's usage never applies.
        outcomes = {"run-1": BudgetRunOutcome(anthropic_stats(900), "anthropic")}
        with patch("vibe_loop.budget.utc_now_iso") as clock:
            clock.return_value = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            store.reserve(
                reservation_id="already-old",
                run_id="already-old",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="m",
                effort="",
            )
        recovered = store.recover_abandoned(
            resolve=outcomes.get,
            process_alive=lambda pid, host: False,
            grace_seconds=0.0,
        )
        # The review reservation is fail-safe charged, not charged 900.
        reconciled = {
            r["reservation_id"]: r
            for r in store.read_records()
            if r.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        }
        review = reconciled["run-1:initial_review:orphan"]
        self.assertTrue(review["fail_safe_applied"])
        self.assertEqual(review["charge"], 500.0)
        self.assertGreaterEqual(recovered, 1)

    def test_targeted_closure_defer_launches_no_reviewer(self) -> None:
        store = self._store(
            make_config(
                default_declared=1000.0,
                on_insufficient="defer",
                limits=(BudgetLimit(phase="targeted_closure", limit=1.0),),
            )
        )
        launched: list[str] = []

        def sentinel(command: str, **kwargs):
            launched.append(command)
            raise AssertionError("targeted closure must not launch when deferred")

        router = self._router(store, sentinel)
        with self.assertRaises(BudgetReservationDenied) as caught:
            self._review_phase(router, "targeted_closure")
        self.assertEqual(caught.exception.decision.decision, "defer")
        self.assertEqual(caught.exception.decision.phase, "targeted_closure")
        self.assertEqual(launched, [])

    def test_targeted_closure_reconciles_own_usage_once(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        router = self._router(
            store,
            self._approve(
                {"usage": {"input_tokens": 60, "output_tokens": 15}}, closure=True
            ),
        )
        self._review_phase(router, "targeted_closure")
        reconciled = [
            record
            for record in store.read_records()
            if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        reservation = next(
            record
            for record in store.read_records()
            if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            and record.get("reservation_id") == reconciled[0]["reservation_id"]
        )
        self.assertEqual(reservation["phase"], "targeted_closure")
        self.assertEqual(reconciled[0]["charge"], 75.0)
        self.assertEqual(
            store.recover_abandoned(
                resolve=lambda reservation_id: None,
                process_alive=lambda pid, host: False,
                grace_seconds=0,
            ),
            0,
        )
        self.assertEqual(
            sum(
                record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
                for record in store.read_records()
            ),
            1,
        )

    def test_targeted_closure_timeout_charges_fail_safe(self) -> None:
        store = self._store(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )

        def execute(command: str, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        with self.assertRaises(ReviewWaitIncomplete):
            self._review_phase(self._router(store, execute), "targeted_closure")
        reconciled = [
            record
            for record in store.read_records()
            if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        reservation = next(
            record
            for record in store.read_records()
            if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            and record.get("reservation_id") == reconciled[0]["reservation_id"]
        )
        self.assertEqual(reservation["phase"], "targeted_closure")
        self.assertTrue(reconciled[0]["fail_safe_applied"])
        self.assertEqual(reconciled[0]["charge"], 500.0)

    def test_review_launch_oserror_releases_both_review_phases(self) -> None:
        for phase in ("initial_review", "targeted_closure"):
            with self.subTest(phase=phase):
                store = self._store(
                    make_config(
                        default_declared=500.0,
                        limits=(BudgetLimit(phase=phase, limit=1e12),),
                    )
                )

                def execute(command: str, **kwargs):
                    raise OSError("launch failed")

                with self.assertRaises(ReviewExecutionError):
                    self._review_phase(self._router(store, execute), phase)
                records = store.read_records()
                reservation_phases = {
                    record["reservation_id"]: record["phase"]
                    for record in records
                    if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
                }
                released = [
                    record
                    for record in records
                    if record.get("record_type") == "budget_released"
                    and reservation_phases.get(record.get("reservation_id")) == phase
                ]
                self.assertEqual(len(released), 1)
                self.assertFalse(
                    any(
                        record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
                        and reservation_phases.get(record.get("reservation_id"))
                        == phase
                        for record in records
                    )
                )

    def test_review_claim_failures_release_without_launch_or_charge(self) -> None:
        failures = (
            ("exhausted", ReviewBudgetExhausted("initial", 1)),
            ("pending", ReviewWaitIncomplete("initial", 1, 1)),
            ("malformed", ReviewExecutionError("invalid claim status")),
            ("other", ValueError("claim failed")),
        )
        for phase in ("initial_review", "targeted_closure"):
            for name, failure in failures:
                with self.subTest(phase=phase, failure=name):
                    store = self._store(
                        make_config(
                            default_declared=500.0,
                            limits=(BudgetLimit(phase=phase, limit=1e12),),
                        )
                    )
                    launched: list[str] = []

                    def sentinel(command: str, **kwargs):
                        launched.append(command)
                        raise AssertionError("claim failure must prevent launch")

                    router = self._router(store, sentinel)
                    released_before = sum(
                        record.get("record_type") == "budget_released"
                        for record in store.read_records()
                    )
                    with patch.object(
                        router, "_claim_review_attempt", side_effect=failure
                    ):
                        with self.assertRaises(type(failure)):
                            self._review_phase(router, phase)
                    records = store.read_records()
                    self.assertEqual(launched, [])
                    self.assertEqual(
                        sum(
                            record.get("record_type") == "budget_released"
                            for record in records
                        ),
                        released_before + 1,
                    )
                    self.assertFalse(
                        any(
                            record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
                            for record in records
                        )
                    )
                    projection = store.projection(project="proj")
                    self.assertEqual(projection["reservations"]["live"], 0)
                    self.assertEqual(
                        sum(float(limit["consumed"]) for limit in projection["limits"]),
                        0.0,
                    )


class F3RuntimeRemediationLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        _init_repo(self.repo)
        self.task = Task(task_id="TASK-01", title="Task", status="Next")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.workspace = ProvisionedWorkspace(
            mode="existing",
            branch="main",
            worktree=self.repo,
            base_commit=head,
            head_commit=head,
            owner_run_id="run-1",
        )

    def _runner(self, budget: BudgetConfig) -> VibeRunner:
        runner = VibeRunner(
            VibeConfig(
                repo=self.repo,
                agent=_reviewer_agent("claude"),
                orchestration=OrchestrationConfig(
                    mode="worker-owned", explicit_keys=frozenset({"mode"})
                ),
                budget=budget,
            )
        )
        runner.runs_dir.mkdir(parents=True, exist_ok=True)
        return runner

    def _launch(self, runner: VibeRunner) -> None:
        runner._launch_runtime_remediation(
            task=self.task,
            run_id="run-1",
            workspace=self.workspace,
            agent=runner.config.agent,
            agent_profile="",
            command_env={},
            implementation_session_id="",
            implementation_session_id_source="",
            round_number=1,
        )

    def test_remediation_defer_launches_no_process_or_lifecycle(self) -> None:
        runner = self._runner(
            make_config(
                default_declared=1000.0,
                on_insufficient="defer",
                limits=(BudgetLimit(phase="remediation", limit=1.0),),
            )
        )
        launched: list[str] = []

        def sentinel(*args, **kwargs):
            launched.append("launched")
            raise AssertionError("remediation must not launch when deferred")

        with patch("vibe_loop.runner.run_streaming_command", sentinel):
            with self.assertRaises(BudgetReservationDenied) as caught:
                self._launch(runner)
        self.assertEqual(caught.exception.decision.decision, "defer")
        self.assertEqual(caught.exception.decision.phase, "remediation")
        self.assertEqual(launched, [])
        self.assertFalse(
            any(
                record.get("to_state") == "remediation_started"
                for record in runner.run_store.read_records()
            )
        )

    def test_remediation_reconciles_own_usage_once_and_recovery_is_noop(
        self,
    ) -> None:
        runner = self._runner(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        result = StreamingCommandResult(
            exit_code=0,
            usage=ProviderUsage(
                provider="anthropic",
                source="native:test",
                version="1",
                values={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            ),
        )
        with patch("vibe_loop.runner.run_streaming_command", return_value=result):
            self._launch(runner)
        reconciled = [
            record
            for record in runner.budget_store.read_records()
            if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        reservation = next(
            record
            for record in runner.budget_store.read_records()
            if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            and record.get("reservation_id") == reconciled[0]["reservation_id"]
        )
        self.assertEqual(reservation["phase"], "remediation")
        self.assertEqual(reconciled[0]["charge"], 100.0)
        self.assertEqual(runner.recover_abandoned_budget(), 0)
        self.assertEqual(
            sum(
                record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
                for record in runner.budget_store.read_records()
            ),
            1,
        )
        self.assertTrue(
            any(
                record.get("to_state") == "remediation_started"
                for record in runner.run_store.read_records()
            )
        )

    def test_remediation_timeout_unavailable_usage_charges_fail_safe(self) -> None:
        runner = self._runner(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        result = StreamingCommandResult(
            exit_code=124,
            timed_out=True,
            usage=unavailable_usage("anthropic", "provider_usage_not_reported"),
        )
        with patch("vibe_loop.runner.run_streaming_command", return_value=result):
            with self.assertRaises(ReviewStageResultError):
                self._launch(runner)
        reconciled = [
            record
            for record in runner.budget_store.read_records()
            if record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
        ]
        self.assertEqual(len(reconciled), 1)
        reservation = next(
            record
            for record in runner.budget_store.read_records()
            if record.get("record_type") == BUDGET_RESERVED_RECORD_TYPE
            and record.get("reservation_id") == reconciled[0]["reservation_id"]
        )
        self.assertEqual(reservation["phase"], "remediation")
        self.assertTrue(reconciled[0]["fail_safe_applied"])
        self.assertEqual(reconciled[0]["charge"], 500.0)

    def test_remediation_launch_oserror_releases_reservation(self) -> None:
        runner = self._runner(
            make_config(default_declared=500.0, limits=(BudgetLimit(limit=1e12),))
        )
        with patch(
            "vibe_loop.runner.run_streaming_command",
            side_effect=OSError("launch failed"),
        ):
            with self.assertRaises(OSError):
                self._launch(runner)
        records = runner.budget_store.read_records()
        self.assertEqual(
            sum(record.get("record_type") == "budget_released" for record in records),
            1,
        )
        self.assertFalse(
            any(
                record.get("record_type") == BUDGET_RECONCILED_RECORD_TYPE
                for record in records
            )
        )


class F4TerminalSchemaTests(unittest.TestCase):
    def _store_with_reservation(self, directory: str) -> BudgetStore:
        config = make_config(default_declared=100.0, limits=(BudgetLimit(limit=100.0),))
        store = BudgetStore(Path(directory) / "budget.jsonl", config)
        store.reserve(
            reservation_id="r1",
            run_id="run-1",
            project="proj",
            provider="anthropic",
            phase="implementation",
            model="claude-opus-4-8",
            effort="high",
        )
        return store

    def _append_raw(self, store: BudgetStore, text: str) -> None:
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _valid_terminal(self, **overrides) -> dict:
        record = {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "record_type": BUDGET_RECONCILED_RECORD_TYPE,
            "occurred_at": datetime.now(UTC).isoformat(),
            "reservation_id": "r1",
            "run_id": "run-1",
            "owner_run_id": "run-1",
            "generation": 1,
            "metric": "total_tokens",
            "charge": 10.0,
            "authoritative": True,
            "fail_safe_applied": False,
            "reason": "terminal_usage",
            "dimensions": {},
        }
        record.update(overrides)
        return record

    def _projection(self, store: BudgetStore) -> dict:
        return store.projection(project="proj")

    def _assert_live_and_flagged(
        self, store: BudgetStore, integrity_class: str
    ) -> None:
        projection = self._projection(store)
        self.assertEqual(projection["reservations"]["live"], 1)
        self.assertEqual(projection["reservations"]["reconciled"], 0)
        self.assertGreaterEqual(projection["integrity"].get(integrity_class, 0), 1)

    def test_malformed_json_row_does_not_close_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(store, '{"record_type": "budget_reconciled", "charg')
            self._assert_live_and_flagged(store, "malformed_json")

    def test_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(
                store, json.dumps(self._valid_terminal(owner_run_id="attacker"))
            )
            self._assert_live_and_flagged(store, "identity_mismatch")

    def test_terminal_for_other_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(
                store, json.dumps(self._valid_terminal(run_id="other-run"))
            )
            self._assert_live_and_flagged(store, "identity_mismatch")

    def test_generation_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(store, json.dumps(self._valid_terminal(generation=99)))
            self._assert_live_and_flagged(store, "generation_mismatch")

    def test_non_integer_generations_are_rejected(self) -> None:
        for generation in ("1", True):
            with self.subTest(generation=generation):
                with tempfile.TemporaryDirectory() as directory:
                    store = self._store_with_reservation(directory)
                    self._append_raw(
                        store,
                        json.dumps(self._valid_terminal(generation=generation)),
                    )
                    self._assert_live_and_flagged(store, "generation_mismatch")

    def test_missing_required_fields_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            record = self._valid_terminal()
            del record["charge"]
            self._append_raw(store, json.dumps(record))
            self._assert_live_and_flagged(store, "invalid_charge")

    def test_negative_or_raw_charge_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(
                store,
                json.dumps(self._valid_terminal(dimensions={"input_tokens": "raw"})),
            )
            self._assert_live_and_flagged(store, "invalid_charge")

    def test_orphan_terminal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            self._append_raw(
                store, json.dumps(self._valid_terminal(reservation_id="ghost"))
            )
            projection = self._projection(store)
            self.assertGreaterEqual(
                projection["integrity"].get("orphan_terminal", 0), 1
            )
            self.assertEqual(projection["reservations"]["live"], 1)

    def test_duplicate_terminal_keeps_first_charge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            store.reconcile_reservation(
                reservation_id="r1",
                run_id="run-1",
                stats=anthropic_stats(10),
                provider="anthropic",
            )
            self._append_raw(store, json.dumps(self._valid_terminal(charge=999.0)))
            projection = self._projection(store)
            self.assertGreaterEqual(
                projection["integrity"].get("duplicate_terminal", 0), 1
            )
            self.assertEqual(projection["reservations"]["reconciled"], 1)
            # The duplicate row is physically present but never applied: consumed
            # reflects the first valid charge (10), not the forged 999.
            consumed = sum(float(limit["consumed"]) for limit in projection["limits"])
            self.assertEqual(consumed, 10.0)

    def test_legacy_terminal_without_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            legacy = self._valid_terminal()
            del legacy["owner_run_id"]
            del legacy["generation"]
            self._append_raw(store, json.dumps(legacy))
            self._assert_live_and_flagged(store, "generation_mismatch")

    def test_legacy_terminal_for_other_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_reservation(directory)
            legacy = self._valid_terminal(run_id="other-run")
            del legacy["owner_run_id"]
            self._append_raw(store, json.dumps(legacy))
            self._assert_live_and_flagged(store, "identity_mismatch")


class F5BoundedStateTests(unittest.TestCase):
    def _old_config(self) -> BudgetConfig:
        return make_config(default_declared=100.0, limits=(BudgetLimit(limit=1e12),))

    def _compacted_cap_store(self, directory: str) -> BudgetStore:
        store = BudgetStore(
            Path(directory) / "budget.jsonl",
            make_config(
                default_declared=60.0,
                limits=(BudgetLimit(limit=100.0),),
            ),
        )
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        with patch("vibe_loop.budget.utc_now_iso", return_value=old):
            store.reserve(
                reservation_id="r1",
                run_id="r1",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="m",
                effort="",
            )
            store.reconcile_reservation(
                reservation_id="r1",
                run_id="r1",
                stats=anthropic_stats(60),
                provider="anthropic",
            )
        self.assertTrue(store.compact())
        return store

    def _reserve_second_allowance(self, store: BudgetStore):
        return store.reserve(
            reservation_id="r2",
            run_id="r2",
            project="proj",
            provider="anthropic",
            phase="implementation",
            model="m",
            effort="",
        )

    def test_config_rejects_excess_limits(self) -> None:
        limits = [{"limit": 10, "phase": "implementation"}] * (BUDGET_MAX_LIMITS + 1)
        with self.assertRaises(ValueError):
            parse_budget({"enabled": True, "default_declared": 10, "limits": limits})

    def test_compaction_folds_closed_reservations_and_preserves_accounting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", self._old_config())
            old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
            with patch("vibe_loop.budget.utc_now_iso", return_value=old):
                for index in range(20):
                    store.reserve(
                        reservation_id=f"r{index}",
                        run_id=f"r{index}",
                        project="proj",
                        provider="anthropic",
                        phase="implementation",
                        model="claude-opus-4-8",
                        effort="high",
                    )
                    store.reconcile_reservation(
                        reservation_id=f"r{index}",
                        run_id=f"r{index}",
                        stats=anthropic_stats(10),
                        provider="anthropic",
                    )
            before = self._projection_consumed(store)
            self.assertEqual(before, 200.0)
            active_before = len(store.read_records())
            self.assertTrue(store.compact())
            active_after = len(store.read_records())
            self.assertLess(active_after, active_before)
            self.assertTrue(store.checkpoint_path.exists())
            # Accounting is preserved across compaction and a fresh reader.
            self.assertEqual(self._projection_consumed(store), 200.0)
            reopened = BudgetStore(store.path, self._old_config())
            self.assertEqual(self._projection_consumed(reopened), 200.0)
            self.assertEqual(
                reopened.projection(project="proj")["reservations"]["reconciled"], 20
            )

    def test_torn_checkpoint_fails_safe_to_full_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", self._old_config())
            store.reserve(
                reservation_id="r1",
                run_id="r1",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="m",
                effort="",
            )
            store.reconcile_reservation(
                reservation_id="r1",
                run_id="r1",
                stats=anthropic_stats(50),
                provider="anthropic",
            )
            store.checkpoint_path.write_text("{ torn json", encoding="utf-8")
            # The torn checkpoint is ignored; consumption replays from the journal.
            self.assertEqual(self._projection_consumed(store), 50.0)

    def test_stale_generation_checkpoint_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", self._old_config())
            store.reserve(
                reservation_id="r1",
                run_id="r1",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="m",
                effort="",
            )
            store.reconcile_reservation(
                reservation_id="r1",
                run_id="r1",
                stats=anthropic_stats(50),
                provider="anthropic",
            )
            # A checkpoint whose generation does not match the journal header must
            # be ignored (never double-counted on top of the journal).
            store.checkpoint_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation": 99,
                        "cumulative": [
                            {
                                "project": "proj",
                                "provider": "anthropic",
                                "phase": "implementation",
                                "model": "m",
                                "effort": "",
                                "charge": 9999.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(self._projection_consumed(store), 50.0)

    def test_incomplete_and_forged_same_generation_checkpoints_fail_closed(
        self,
    ) -> None:
        config = make_config(
            default_declared=60.0,
            limits=(BudgetLimit(limit=100.0),),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", config)
            old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
            with patch("vibe_loop.budget.utc_now_iso", return_value=old):
                store.reserve(
                    reservation_id="r1",
                    run_id="r1",
                    project="proj",
                    provider="anthropic",
                    phase="implementation",
                    model="m",
                    effort="",
                )
                store.reconcile_reservation(
                    reservation_id="r1",
                    run_id="r1",
                    stats=anthropic_stats(60),
                    provider="anthropic",
                )
            self.assertTrue(store.compact())
            original = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
            cases = {
                "incomplete": {
                    key: value for key, value in original.items() if key != "integrity"
                },
                "forged_empty_spend": {
                    **original,
                    "cumulative": [],
                    "reservation_counts": {},
                },
            }
            for name, checkpoint in cases.items():
                with self.subTest(name=name):
                    store.checkpoint_path.write_text(
                        json.dumps(checkpoint), encoding="utf-8"
                    )
                    with self.assertRaises(BudgetLedgerCorruption):
                        store.reserve(
                            reservation_id=f"r2-{name}",
                            run_id=f"r2-{name}",
                            project="proj",
                            provider="anthropic",
                            phase="implementation",
                            model="m",
                            effort="",
                        )

    def test_appended_generation_zero_header_cannot_erase_compacted_spend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._compacted_cap_store(directory)
            forged = {
                "schema_version": BUDGET_SCHEMA_VERSION,
                "record_type": BUDGET_JOURNAL_HEADER_RECORD_TYPE,
                "generation": 0,
                "checkpoint_sha256": "0" * 64,
            }
            with store.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(forged) + "\n")
            with self.assertRaises(BudgetLedgerCorruption):
                self._reserve_second_allowance(store)
            self.assertNotIn(
                '"reservation_id":"r2"', store.path.read_text(encoding="utf-8")
            )

    def test_duplicate_valid_header_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._compacted_cap_store(directory)
            header = store.path.read_text(encoding="utf-8").splitlines()[0]
            with store.path.open("a", encoding="utf-8") as handle:
                handle.write(header + "\n")
            with self.assertRaises(BudgetLedgerCorruption):
                self._reserve_second_allowance(store)

    def test_malformed_canonical_header_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._compacted_cap_store(directory)
            store.path.write_text(
                '{"record_type":"budget_journal_header"\n', encoding="utf-8"
            )
            with self.assertRaises(BudgetLedgerCorruption):
                self._reserve_second_allowance(store)

    def test_invalid_canonical_header_matrix_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._compacted_cap_store(directory)
            lines = store.path.read_text(encoding="utf-8").splitlines()
            valid = json.loads(lines[0])
            cases = {
                "missing_digest": {
                    key: value
                    for key, value in valid.items()
                    if key != "checkpoint_sha256"
                },
                "extra_field": {**valid, "unexpected": 1},
                "schema_bool": {**valid, "schema_version": True},
                "schema_string": {**valid, "schema_version": "1"},
                "generation_zero": {**valid, "generation": 0},
                "generation_bool": {**valid, "generation": True},
                "generation_string": {**valid, "generation": "1"},
                "stale_generation": {**valid, "generation": valid["generation"] + 1},
                "digest_short": {**valid, "checkpoint_sha256": "0" * 63},
                "digest_wrong_value": {**valid, "checkpoint_sha256": "0" * 64},
                "digest_uppercase": {
                    **valid,
                    "checkpoint_sha256": valid["checkpoint_sha256"].upper(),
                },
            }
            remainder = "\n".join(lines[1:])
            for name, header in cases.items():
                with self.subTest(name=name):
                    journal = json.dumps(header, separators=(",", ":")) + "\n"
                    if remainder:
                        journal += remainder + "\n"
                    store.path.write_text(journal, encoding="utf-8")
                    with self.assertRaises(BudgetLedgerCorruption):
                        self._reserve_second_allowance(store)

    def test_no_header_legacy_journal_preserves_spend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(
                Path(directory) / "budget.jsonl",
                make_config(
                    default_declared=60.0,
                    limits=(BudgetLimit(limit=100.0),),
                ),
            )
            store.reserve(
                reservation_id="r1",
                run_id="r1",
                project="proj",
                provider="anthropic",
                phase="implementation",
                model="m",
                effort="",
            )
            store.reconcile_reservation(
                reservation_id="r1",
                run_id="r1",
                stats=anthropic_stats(60),
                provider="anthropic",
            )
            self.assertNotIn(
                BUDGET_JOURNAL_HEADER_RECORD_TYPE,
                store.path.read_text(encoding="utf-8").splitlines()[0],
            )
            self.assertFalse(self._reserve_second_allowance(store).admitted)

    def test_sustained_denial_load_stays_bounded_and_audited(self) -> None:
        config = make_config(default_declared=100.0, limits=(BudgetLimit(limit=1.0),))
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", config)
            old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
            with patch("vibe_loop.budget.utc_now_iso", return_value=old):
                for index in range(50):
                    decision = store.reserve(
                        reservation_id=f"d{index}",
                        run_id=f"d{index}",
                        project="proj",
                        provider="anthropic",
                        phase="implementation",
                        model="m",
                        effort="",
                    )
                    self.assertFalse(decision.admitted)
            store.compact()
            projection = store.projection(project="proj")
            # Every denial is preserved in the audit counter, but the active
            # journal no longer carries all 50 decision rows.
            self.assertGreaterEqual(projection["decisions"]["block"], 50)
            self.assertLess(projection["compaction"]["active_records"], 50)

    def test_projection_lists_are_bounded_with_counts(self) -> None:
        limits = tuple(
            BudgetLimit(limit=10.0, model=f"m{index}") for index in range(150)
        )
        config = make_config(default_declared=1.0, limits=limits)
        with tempfile.TemporaryDirectory() as directory:
            store = BudgetStore(Path(directory) / "budget.jsonl", config)
            projection = store.projection(project="proj")
            self.assertEqual(len(projection["limits"]), 100)
            self.assertEqual(projection["limits_truncated"], 50)

    def _projection_consumed(self, store: BudgetStore) -> float:
        projection = store.projection(project="proj")
        return sum(float(limit["consumed"]) for limit in projection["limits"])


if __name__ == "__main__":
    unittest.main()
