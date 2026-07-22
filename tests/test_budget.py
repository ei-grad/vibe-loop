from __future__ import annotations

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
    BUDGET_RECONCILED_RECORD_TYPE,
    BUDGET_RESERVED_RECORD_TYPE,
    BudgetReservationDenied,
    BudgetRunOutcome,
    BudgetStore,
    budget_dimensions,
    usage_is_authoritative,
)
from vibe_loop.config import (
    AgentConfig,
    BudgetConfig,
    BudgetLimit,
    OrchestrationConfig,
    VibeConfig,
    parse_budget,
)
from vibe_loop.runner import VibeRunner, run_streaming_command  # noqa: F401
from vibe_loop.runs import RunResult
from vibe_loop.tasks import Task


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


def make_config(**overrides: object) -> BudgetConfig:
    base = {
        "enabled": True,
        "metric": "total_tokens",
        "default_declared": 400.0,
    }
    base.update(overrides)
    return BudgetConfig(**base)


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


if __name__ == "__main__":
    unittest.main()
