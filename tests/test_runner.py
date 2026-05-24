from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import vibe_loop.runner as runner_module
from vibe_loop.config import (
    AgentConfig,
    CompletionConfig,
    SpecDiagnosticsConfig,
    SupervisionConfig,
    SUPERVISION_DEFAULT_MAX_RESTARTS,
    VibeConfig,
)
from vibe_loop.locks import LockBusy, LockManager, LockOwnerMismatch
from vibe_loop.runner import (
    SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS,
    SchedulerLockBusy,
    VibeRunner,
    build_batch_selection_prompt,
    build_selection_prompt,
    build_spec_worker_context,
    build_worker_prompt,
    deterministic_task_batch,
    parse_selected_task_id,
    parse_selected_task_ids,
    parse_worker_session_id,
    run_streaming_command,
    validate_selected_task_batch,
)
from vibe_loop.runs import WORKER_REPORT_STATUSES, RunResult, WorkerReport
from vibe_loop.spec_diagnostics import SpecExecutionGateError
from vibe_loop.tasks import Task
from vibe_loop.workers import ActiveRunState


class MutableTaskSource:
    def __init__(self, tasks: list[Task]):
        self._tasks = tasks
        self._done: set[str] = set()
        self._lock = threading.Lock()

    def list_tasks(self) -> list[Task]:
        with self._lock:
            return [
                dataclasses.replace(
                    task,
                    status="Done" if task.task_id in self._done else task.status,
                )
                for task in self._tasks
            ]

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id),
            None,
        )

    def mark_done(self, task_id: str) -> None:
        with self._lock:
            self._done.add(task_id)


class RunnerTests(unittest.TestCase):
    def test_selection_prompt_includes_recent_logs(self) -> None:
        task = Task(task_id="LIVE-04", title="Realtime reconcile", status="Next")

        prompt = build_selection_prompt([task], "recent log tail: timeout on WEB-01")

        self.assertIn("LIVE-04", prompt)
        self.assertIn("recent log tail", prompt)
        self.assertIn("blocked or just failed", prompt)

    def test_batch_selection_prompt_includes_context(self) -> None:
        task = Task(task_id="LIVE-04", title="Realtime reconcile", status="Next")

        prompt = build_batch_selection_prompt(
            [task],
            max_tasks=2,
            recent_log_context="recent log tail: timeout on WEB-01",
            active_worker_context="Active vibe-loop workers: []",
        )

        self.assertIn('"max_batch_size": 2', prompt)
        self.assertIn('"task_ids"', prompt)
        self.assertIn("LIVE-04", prompt)
        self.assertIn("recent log tail", prompt)
        self.assertIn("Active vibe-loop workers", prompt)

    def test_worker_prompt_includes_bounded_spec_context_and_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            spec_text = (
                "# Spec\n\n"
                "## PRD-SDE-005 Spec-Aware Worker Context\n\n"
                "Worker prompts include relevant requirement text.\n"
                + ("Bounded requirement detail.\n" * 600)
                + "\n## PRD-SDE-999 Unrelated Requirement\n\n"
                "This unrelated requirement should not be copied.\n"
            )
            design_text = (
                "# Design\n\n## ADR-1\n\nDesign reference body for the worker prompt.\n"
            )
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            (repo / "docs" / "design.md").write_text(design_text, encoding="utf-8")
            fingerprint = {
                "path": "docs/spec.md",
                "size": len(spec_text.encode("utf-8")),
                "sha256": hashlib.sha256(spec_text.encode("utf-8")).hexdigest(),
            }
            task = Task(
                task_id="TRACE-01",
                title="Trace task",
                status="Next",
                acceptance="Worker prompts include bounded spec-aware context.",
                evidence="CLI/runner tests with bounded prompt assertions.",
                requirement_ids=("PRD-SDE-005",),
                spec_paths=("docs/spec.md",),
                design_refs=("docs/design.md#ADR-1",),
                approval_state="approved",
                source_fingerprints=(fingerprint,),
            )
            config = VibeConfig(
                repo=repo,
                specs=SpecDiagnosticsConfig(
                    require_approved=True,
                    require_current_fingerprints=True,
                ),
                completion=CompletionConfig(
                    commands=("uv run python -m unittest discover -s tests",),
                ),
            )

            prompt = build_worker_prompt("$", task, config)

        self.assertIn("### Spec-Aware Worker Context", prompt)
        self.assertIn("Worker prompts include relevant requirement text.", prompt)
        self.assertIn("Design reference body for the worker prompt.", prompt)
        self.assertIn('"status": "current"', prompt)
        self.assertIn('"id": "spec.require_approved"', prompt)
        self.assertIn('"id": "spec.require_current_fingerprints"', prompt)
        self.assertIn('"id": "completion.command"', prompt)
        self.assertIn("...[truncated]", prompt)
        self.assertNotIn("This unrelated requirement should not be copied.", prompt)

    def test_worker_prompt_skips_secret_like_spec_context_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "secrets").mkdir()
            (repo / "secrets" / "spec.md").write_text(
                "TOKEN=secret\nREQ-SECRET must stay hidden.\n",
                encoding="utf-8",
            )
            task = Task(
                task_id="TRACE-02",
                title="Secret trace task",
                status="Next",
                requirement_ids=("REQ-SECRET",),
                spec_paths=("secrets/spec.md",),
                source_fingerprints=(
                    {
                        "path": "secrets/spec.md",
                        "size": 37,
                        "sha256": "0" * 64,
                    },
                ),
            )
            config = VibeConfig(repo=repo)

            prompt = build_worker_prompt("$", task, config)

        self.assertIn('"reason": "unsafe_path"', prompt)
        self.assertNotIn("secrets/spec.md", prompt)
        self.assertNotIn("TOKEN=secret", prompt)
        self.assertNotIn("REQ-SECRET must stay hidden.", prompt)

    def test_worker_prompt_skips_symlinked_spec_context_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "secrets").mkdir()
            (repo / "secrets" / "spec.md").write_text(
                "TOKEN=secret\nREQ-SYMLINK must stay hidden.\n",
                encoding="utf-8",
            )
            (repo / "docs" / "spec.md").symlink_to("../secrets/spec.md")
            task = Task(
                task_id="TRACE-04",
                title="Symlink trace task",
                status="Next",
                requirement_ids=("REQ-SYMLINK",),
                spec_paths=("docs/spec.md",),
            )
            config = VibeConfig(
                repo=repo,
                completion=CompletionConfig(
                    commands=tuple(
                        f"pytest {'x' * 1000} --case {index}" for index in range(30)
                    ),
                ),
            )

            prompt = build_worker_prompt("$", task, config)

        self.assertIn('"reason": "symlink"', prompt)
        self.assertNotIn("TOKEN=secret", prompt)
        self.assertNotIn("REQ-SYMLINK must stay hidden.", prompt)

    def test_worker_prompt_reports_stale_and_missing_spec_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            spec_text = "## REQ-1\n\nCurrent requirement text.\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            task = Task(
                task_id="TRACE-03",
                title="Stale trace task",
                status="Next",
                requirement_ids=("REQ-1",),
                spec_paths=("docs/spec.md",),
                source_fingerprints=(
                    {
                        "path": "docs/spec.md",
                        "size": len(spec_text.encode("utf-8")),
                        "sha256": "1" * 64,
                    },
                    {
                        "path": "docs/missing.md",
                        "size": 10,
                    },
                ),
            )
            config = VibeConfig(repo=repo)

            prompt = build_worker_prompt("$", task, config)

        self.assertIn("Current requirement text.", prompt)
        self.assertIn('"status": "stale"', prompt)
        self.assertIn('"mismatches": [', prompt)
        self.assertIn('"sha256"', prompt)
        self.assertIn('"path": "docs/missing.md"', prompt)
        self.assertIn('"reason": "missing"', prompt)

    def test_worker_prompt_redacts_secret_like_ref_and_fingerprint_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            spec_text = "## REQ-REF\n\nRequirement text.\n"
            design_text = "safe design body\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            (repo / "docs" / "design.md").write_text(design_text, encoding="utf-8")
            task = Task(
                task_id="TRACE-06",
                title="Ref metadata task",
                status="Next",
                requirement_ids=("REQ-REF",),
                spec_paths=("docs/spec.md",),
                design_refs=(
                    "docs/design.md#https://hooks.slack.com/services/T/B/C",
                    "docs/design.md#foo/secrets/token",
                ),
                source_fingerprints=(
                    {
                        "path": "docs/spec.md",
                        "size": len(spec_text.encode("utf-8")),
                        "sha256": "https://hooks.slack.com/services/T/B/C",
                        "webhook_url": "https://hooks.slack.com/services/T/B/C",
                        "api_token": "secret-token",
                    },
                ),
            )
            config = VibeConfig(repo=repo)

            prompt = build_worker_prompt("$", task, config)

        self.assertIn("docs/design.md#<redacted>", prompt)
        self.assertIn('"sha256": "<invalid>"', prompt)
        self.assertNotIn("hooks.slack.com", prompt)
        self.assertNotIn("foo/secrets/token", prompt)
        self.assertNotIn("secret-token", prompt)
        self.assertNotIn("webhook_url", prompt)
        self.assertNotIn("api_token", prompt)

    def test_spec_worker_context_respects_total_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            spec_text = "## REQ-LARGE\n\n" + ("requirement detail\n" * 1000)
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            task = Task(
                task_id="TRACE-05",
                title="Large trace task",
                status="Next",
                scope="scope " * 1000,
                acceptance="acceptance " * 1000,
                evidence="evidence " * 1000,
                requirement_ids=tuple(f"REQ-{index}" for index in range(50)),
                spec_paths=("docs/spec.md",),
                design_refs=tuple(
                    f"docs/design-{index}.md#ADR-{index}" for index in range(50)
                ),
                source_fingerprints=tuple(
                    {
                        "path": f"docs/spec-{index}.md",
                        "size": index,
                        "sha256": "a" * 64,
                    }
                    for index in range(50)
                ),
            )
            config = VibeConfig(repo=repo)

            context = build_spec_worker_context(config, task)
            context_json = json.dumps(context, indent=2, sort_keys=True)

        self.assertLessEqual(len(context_json), SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS)
        self.assertIn("...[truncated]", context_json)

    def test_spec_worker_context_bounds_required_scalar_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "spec.md").write_text(
                "## REQ-SCALAR\n\nRequirement text.\n",
                encoding="utf-8",
            )
            task = Task(
                task_id="TRACE-" + ("x" * 20000),
                title="Scalar trace task",
                status="Next" + ("y" * 20000),
                priority="P1" + ("z" * 20000),
                requirement_ids=("REQ-SCALAR",),
                spec_paths=("docs/spec.md",),
            )
            config = VibeConfig(repo=repo)

            context = build_spec_worker_context(config, task)
            context_json = json.dumps(context, indent=2, sort_keys=True)

        self.assertLessEqual(len(context_json), SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS)
        self.assertIn("...[truncated]", context_json)

    def test_parse_selected_task_id_from_json_only_or_wrapped_output(self) -> None:
        self.assertEqual(
            parse_selected_task_id('{"task_id":"LIVE-04","reason":"ready"}'),
            "LIVE-04",
        )
        self.assertEqual(
            parse_selected_task_id('text\n{"task_id":"WEB-01"}\nmore'),
            "WEB-01",
        )
        self.assertIsNone(parse_selected_task_id("not json"))

    def test_parse_selected_task_ids_from_batch_output(self) -> None:
        self.assertEqual(
            parse_selected_task_ids('{"task_ids":["LIVE-04","WEB-01"]}'),
            ["LIVE-04", "WEB-01"],
        )
        self.assertEqual(
            parse_selected_task_ids('text\n{"task_id":"WEB-01"}\nmore'),
            ["WEB-01"],
        )
        self.assertIsNone(parse_selected_task_ids('{"task_ids":["WEB-01", 2]}'))
        self.assertIsNone(parse_selected_task_ids('{"task_ids":[]}'))

    def test_validate_selected_task_batch_rejects_unsafe_ids(self) -> None:
        candidates = [
            Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
            Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
        ]

        valid = validate_selected_task_batch(
            ["TASK-02", "TASK-01"],
            candidates,
            limit=2,
            is_locked=lambda _task_id: False,
        )
        duplicate = validate_selected_task_batch(
            ["TASK-01", "TASK-01"],
            candidates,
            limit=2,
        )
        unknown = validate_selected_task_batch(["TASK-99"], candidates, limit=2)
        too_many = validate_selected_task_batch(
            ["TASK-01", "TASK-02"],
            candidates,
            limit=1,
        )
        locked = validate_selected_task_batch(
            ["TASK-02"],
            candidates,
            limit=2,
            is_locked=lambda task_id: task_id == "TASK-02",
        )

        self.assertTrue(valid.valid)
        self.assertEqual([task.task_id for task in valid.tasks], ["TASK-02", "TASK-01"])
        self.assertFalse(duplicate.valid)
        self.assertEqual(duplicate.error, "duplicate task_id: TASK-01")
        self.assertFalse(unknown.valid)
        self.assertEqual(unknown.error, "unknown task_id: TASK-99")
        self.assertFalse(too_many.valid)
        self.assertEqual(too_many.error, "too many task_ids")
        self.assertFalse(locked.valid)
        self.assertEqual(locked.error, "locked task_id: TASK-02")

    def test_validate_selected_task_batch_rejects_resource_conflicts(self) -> None:
        candidates = [
            Task(
                task_id="TASK-01",
                title="Task 1",
                status="Next",
                resources=("api",),
                conflict_domains_known=True,
                order=1,
            ),
            Task(
                task_id="TASK-02",
                title="Task 2",
                status="Next",
                resources=("api",),
                conflict_domains_known=True,
                order=2,
            ),
            Task(
                task_id="TASK-03",
                title="Task 3",
                status="Next",
                resources=("docs",),
                conflict_domains_known=True,
                order=3,
            ),
        ]

        conflicting = validate_selected_task_batch(
            ["TASK-01", "TASK-02"],
            candidates,
            limit=2,
        )
        disjoint = validate_selected_task_batch(
            ["TASK-01", "TASK-03"],
            candidates,
            limit=2,
        )

        self.assertFalse(conflicting.valid)
        self.assertEqual(conflicting.error, "conflicting task_ids: TASK-01, TASK-02")
        self.assertTrue(disjoint.valid)

    def test_validate_selected_task_batch_rejects_overlapping_paths(self) -> None:
        candidates = [
            Task(
                task_id="TASK-01",
                title="Task 1",
                status="Next",
                paths=("src/api",),
                conflict_domains_known=True,
            ),
            Task(
                task_id="TASK-02",
                title="Task 2",
                status="Next",
                paths=("src/api/models",),
                conflict_domains_known=True,
            ),
            Task(
                task_id="TASK-03",
                title="Task 3",
                status="Next",
                paths=("src/web",),
                conflict_domains_known=True,
            ),
            Task(
                task_id="TASK-04",
                title="Task 4",
                status="Next",
                paths=(".",),
                conflict_domains_known=True,
            ),
        ]

        conflicting = validate_selected_task_batch(
            ["TASK-01", "TASK-02"],
            candidates,
            limit=2,
        )
        disjoint = validate_selected_task_batch(
            ["TASK-01", "TASK-03"],
            candidates,
            limit=2,
        )
        root = validate_selected_task_batch(
            ["TASK-03", "TASK-04"],
            candidates,
            limit=2,
        )

        self.assertFalse(conflicting.valid)
        self.assertTrue(disjoint.valid)
        self.assertFalse(root.valid)

    def test_deterministic_task_batch_keeps_legacy_no_domain_behavior(self) -> None:
        candidates = [
            Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
            Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
        ]

        selected = deterministic_task_batch(candidates, 2)

        self.assertEqual(
            [task.task_id for task in selected],
            ["TASK-01", "TASK-02"],
        )

    def test_deterministic_task_batch_skips_conflicts_and_unknown_domains(
        self,
    ) -> None:
        candidates = [
            Task(
                task_id="TASK-01",
                title="Task 1",
                status="Next",
                resources=("api",),
                conflict_domains_known=True,
                order=1,
            ),
            Task(
                task_id="TASK-02",
                title="Task 2",
                status="Next",
                resources=("api",),
                conflict_domains_known=True,
                order=2,
            ),
            Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
            Task(
                task_id="TASK-04",
                title="Task 4",
                status="Next",
                resources=("docs",),
                conflict_domains_known=True,
                order=4,
            ),
        ]

        selected = deterministic_task_batch(candidates, 3)

        self.assertEqual(
            [task.task_id for task in selected],
            ["TASK-01", "TASK-04"],
        )

    def test_parse_worker_session_id_from_codex_style_output(self) -> None:
        self.assertEqual(parse_worker_session_id("session id: abc-123"), "abc-123")
        self.assertEqual(parse_worker_session_id("Session_ID = codex.456"), "codex.456")
        self.assertIsNone(parse_worker_session_id("session started"))

    def test_classify_uses_worker_report_statuses_before_task_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))

            for status in WORKER_REPORT_STATUSES:
                for exit_code, message in (
                    (0, ""),
                    (7, ""),
                    (0, "completion check failed"),
                ):
                    with self.subTest(
                        status=status,
                        exit_code=exit_code,
                        message=message,
                    ):
                        result = runner.classify(
                            "TASK-01",
                            exit_code,
                            "aaa",
                            "aaa",
                            message,
                            WorkerReport(
                                run_id=f"run-{status}",
                                task_id="TASK-01",
                                status=status,
                            ),
                        )

                        self.assertEqual(result.status, status)
                        self.assertEqual(result.source, "worker_report")

    def test_run_until_done_parallel_honors_jobs_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                    Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
                    Task(task_id="TASK-04", title="Task 4", status="Next", order=4),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2)

        self.assertEqual(max_active, 2)
        self.assertEqual(len(results), 4)
        self.assertLessEqual(max_active, 2)
        self.assertEqual(
            sorted(result.task_id for result in results),
            ["TASK-01", "TASK-02", "TASK-03", "TASK-04"],
        )

    def test_run_until_done_default_remains_serial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.01)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done()

        self.assertEqual(max_active, 1)
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-02"],
        )

    def test_run_until_done_serial_stops_after_max_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id=f"TASK-0{n}",
                        title=f"Task {n}",
                        status="Next",
                        order=n,
                    )
                    for n in range(1, 5)
                ]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(max_tasks=2)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.classification == "completed" for result in results))

    def test_run_until_done_serial_rotates_completed_still_ready_tasks(self) -> None:
        # A completed task that stays runnable (multi-slice work) must not
        # monopolize the chain: every other ready task gets a turn first.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id=f"TASK-0{n}",
                        title=f"Task {n}",
                        status="Next",
                        order=n,
                    )
                    for n in range(1, 4)
                ]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                # Deliberately do NOT mark the task done, so it stays ready and
                # would be re-selected forever without rotation.
                return RunResult(
                    run_id=f"run-{task.task_id}-{len(seen)}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            seen: list[str] = []
            original = run_task

            def tracking_run_task(task: Task) -> RunResult:
                seen.append(task.task_id)
                return original(task)

            runner.run_task = tracking_run_task

            results = runner.run_until_done(max_tasks=3)

        self.assertEqual(len(results), 3)
        # Breadth: three distinct tasks, not three slices of the first one.
        self.assertEqual(
            sorted(result.task_id for result in results),
            ["TASK-01", "TASK-02", "TASK-03"],
        )

    def test_run_until_done_parallel_stops_after_max_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id=f"TASK-0{n}",
                        title=f"Task {n}",
                        status="Next",
                        order=n,
                    )
                    for n in range(1, 7)
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.02)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_tasks=3)

        completed = [
            result for result in results if result.classification == "completed"
        ]
        self.assertEqual(len(completed), 3)
        self.assertLessEqual(max_active, 2)

    def test_run_until_done_parallel_max_tasks_counts_only_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id=f"TASK-0{n}",
                        title=f"Task {n}",
                        status="Next",
                        order=n,
                    )
                    for n in range(1, 7)
                ]
            )
            runner._source = source
            failing = {"TASK-02", "TASK-04"}

            def run_task(task: Task) -> RunResult:
                if task.task_id in failing:
                    classification = "failed"
                else:
                    source.mark_done(task.task_id)
                    classification = "completed"
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification=classification,
                    exit_code=0 if classification == "completed" else 1,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(
                jobs=2, max_tasks=3, continue_on_failure=True
            )

        completed = [
            result for result in results if result.classification == "completed"
        ]
        self.assertEqual(len(completed), 3)

    def _completing_runner(self, repo: Path, source: MutableTaskSource) -> VibeRunner:
        runner = VibeRunner(VibeConfig(repo=repo, agent=AgentConfig(command="worker")))
        runner._source = source

        def run_task(task: Task) -> RunResult:
            source.mark_done(task.task_id)
            return RunResult(
                run_id=f"run-{task.task_id}",
                task_id=task.task_id,
                classification="completed",
                exit_code=0,
                log_path=repo / f"{task.task_id}.log",
                start_main="aaa",
                end_main="aaa",
            )

        runner.run_task = run_task
        return runner

    @staticmethod
    def _ready_tasks(count: int) -> list[Task]:
        return [
            Task(task_id=f"TASK-{n:02d}", title=f"Task {n}", status="Next", order=n)
            for n in range(1, count + 1)
        ]

    def test_run_until_done_max_slices_wins_when_lower_than_max_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(self._ready_tasks(6))
            runner = self._completing_runner(repo, source)

            results = runner.run_until_done(max_slices=2, max_tasks=5)

        self.assertEqual(len(results), 2)

    def test_run_until_done_max_tasks_wins_when_lower_than_max_slices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(self._ready_tasks(6))
            runner = self._completing_runner(repo, source)

            results = runner.run_until_done(max_slices=10, max_tasks=2)

        self.assertEqual(len(results), 2)

    def test_run_until_done_parallel_first_limit_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(self._ready_tasks(6))
            runner = self._completing_runner(repo, source)

            results = runner.run_until_done(jobs=2, max_slices=2, max_tasks=5)

        self.assertEqual(len(results), 2)

    def test_run_until_done_max_tasks_above_available_runs_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(self._ready_tasks(3))
            runner = self._completing_runner(repo, source)

            results = runner.run_until_done(jobs=2, max_tasks=10)

        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.classification == "completed" for result in results))

    def test_parallel_batch_selection_falls_back_to_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker", selection_command="selector"),
                )
            )
            tasks = [
                Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
            ]
            runner.ask_agent_to_select_batch = lambda _candidates, _limit: None

            selected = runner.select_batch_from_candidates(
                tasks,
                limit=2,
                ask_agent=True,
            )

        self.assertEqual(
            [task.task_id for task in selected],
            ["TASK-01", "TASK-02"],
        )

    def test_parallel_undersized_agent_batch_waits_before_refill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker", selection_command="selector"),
                )
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                    Task(task_id="TASK-03", title="Task 3", status="Next", order=3),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()
            selected_batches: list[list[str]] = []

            def select_one_task(candidates: list[Task], _limit: int) -> list[Task]:
                selected_batches.append([task.task_id for task in candidates])
                return [candidates[0]]

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.ask_agent_to_select_batch = select_one_task
            runner.run_task = run_task

            results = runner.run_until_done(ask_agent=True, jobs=2, max_slices=2)

        self.assertEqual(max_active, 1)
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-02"],
        )
        self.assertEqual(
            selected_batches,
            [
                ["TASK-01", "TASK-02", "TASK-03"],
                ["TASK-02", "TASK-03"],
            ],
        )

    def test_parallel_refill_rechecks_spec_gate_before_agent_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            spec_path = repo / "docs" / "spec.md"
            spec_text = "current spec\n"
            spec_path.write_text(spec_text, encoding="utf-8")
            fingerprint = {
                "path": "docs/spec.md",
                "size": len(spec_text),
                "sha256": hashlib.sha256(spec_text.encode("utf-8")).hexdigest(),
            }
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker", selection_command="selector"),
                    specs=SpecDiagnosticsConfig(require_current_fingerprints=True),
                )
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-01",
                        title="Task 1",
                        status="Next",
                        requirement_ids=("REQ-1",),
                        approval_state="approved",
                        source_fingerprints=(fingerprint,),
                        order=1,
                    ),
                    Task(
                        task_id="TASK-02",
                        title="Task 2",
                        status="Next",
                        requirement_ids=("REQ-2",),
                        approval_state="approved",
                        source_fingerprints=(fingerprint,),
                        order=2,
                    ),
                    Task(
                        task_id="TASK-03",
                        title="Task 3",
                        status="Next",
                        requirement_ids=("REQ-3",),
                        approval_state="approved",
                        source_fingerprints=(fingerprint,),
                        order=3,
                    ),
                ]
            )
            runner._source = source
            selected_batches: list[list[str]] = []

            def select_one_task(candidates: list[Task], _limit: int) -> list[Task]:
                selected_batches.append([task.task_id for task in candidates])
                return [candidates[0]]

            def run_task(task: Task) -> RunResult:
                source.mark_done(task.task_id)
                spec_path.write_text("drifted spec\n", encoding="utf-8")
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.ask_agent_to_select_batch = select_one_task
            runner.run_task = run_task

            with self.assertRaises(SpecExecutionGateError):
                runner.run_until_done(ask_agent=True, jobs=2, max_slices=2)

        self.assertEqual(selected_batches, [["TASK-01", "TASK-02", "TASK-03"]])

    def test_run_until_done_parallel_excludes_task_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source
            held_lock = runner.lock_manager.acquire("TASK-01", "external-run")

            def run_task(task: Task) -> RunResult:
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            try:
                results = runner.run_until_done(jobs=2, max_slices=1)
            finally:
                runner.lock_manager.release(held_lock)

        self.assertEqual([result.task_id for result in results], ["TASK-02"])

    def test_run_until_done_parallel_excludes_resource_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-01",
                        title="Task 1",
                        status="Next",
                        resources=("api",),
                        conflict_domains_known=True,
                        order=1,
                    ),
                    Task(
                        task_id="TASK-02",
                        title="Task 2",
                        status="Next",
                        resources=("api",),
                        conflict_domains_known=True,
                        order=2,
                    ),
                    Task(
                        task_id="TASK-03",
                        title="Task 3",
                        status="Next",
                        resources=("docs",),
                        conflict_domains_known=True,
                        order=3,
                    ),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_slices=2)

        self.assertEqual(max_active, 2)
        self.assertEqual(
            sorted(result.task_id for result in results),
            ["TASK-01", "TASK-03"],
        )

    def test_parallel_refill_honors_scheduled_resource_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-01",
                        title="Task 1",
                        status="Next",
                        resources=("api",),
                        conflict_domains_known=True,
                        order=1,
                    ),
                    Task(
                        task_id="TASK-02",
                        title="Task 2",
                        status="Next",
                        resources=("api",),
                        conflict_domains_known=True,
                        order=2,
                    ),
                ]
            )
            runner._source = source
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                source.mark_done(task.task_id)
                with active_lock:
                    active -= 1
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_slices=2)

        self.assertEqual(max_active, 1)
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-02"],
        )

    def test_list_candidates_excludes_active_resource_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            runner._source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-01",
                        title="Task 1",
                        status="Next",
                        resources=("api",),
                        conflict_domains_known=True,
                        order=1,
                    ),
                    Task(
                        task_id="TASK-02",
                        title="Task 2",
                        status="Next",
                        resources=("docs",),
                        conflict_domains_known=True,
                        order=2,
                    ),
                ]
            )
            held_lock = runner.lock_manager.acquire(
                "EXTERNAL-01",
                "external-run",
                metadata={
                    "record_type": "active_run",
                    "resources": ["api"],
                    "paths": [],
                    "conflict_domains_known": True,
                },
            )
            try:
                candidates = runner.list_candidates()
            finally:
                runner.lock_manager.release(held_lock)

        self.assertEqual([task.task_id for task in candidates], ["TASK-02"])

    def test_task_lock_acquire_rechecks_active_resource_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            task = Task(
                task_id="TASK-01",
                title="Task 1",
                status="Next",
                resources=("api",),
                conflict_domains_known=True,
            )
            active_state = ActiveRunState.new(
                task_id=task.task_id,
                run_id="run-task",
                log_path=repo / "run.log",
                base_main="aaa",
                command="worker",
                resources=task.resources,
                paths=task.paths,
                conflict_domains_known=task.conflict_domains_known,
            )
            held_lock = runner.lock_manager.acquire(
                "EXTERNAL-01",
                "external-run",
                metadata={
                    "record_type": "active_run",
                    "resources": ["api"],
                    "paths": [],
                    "conflict_domains_known": True,
                },
            )
            try:
                with self.assertRaises(LockBusy) as busy:
                    runner.acquire_scheduled_task_lock(
                        task,
                        "run-task",
                        active_state,
                    )
            finally:
                runner.lock_manager.release(held_lock)

        self.assertEqual(busy.exception.metadata["reason"], "resource_conflict")
        self.assertFalse(runner.lock_manager.is_locked("TASK-01"))

    def test_scheduler_lock_does_not_reserve_matching_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            task = Task(
                task_id="resource-scheduler",
                title="Task with internal lock name",
                status="Next",
            )
            active_state = ActiveRunState.new(
                task_id=task.task_id,
                run_id="run-task",
                log_path=repo / "run.log",
                base_main="aaa",
                command="worker",
            )

            task_lock = runner.acquire_scheduled_task_lock(
                task,
                "run-task",
                active_state,
            )
            try:
                self.assertTrue(runner.lock_manager.is_locked("resource-scheduler"))
            finally:
                runner.lock_manager.release(task_lock)

    def test_leftover_scheduler_lock_file_does_not_block_task_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            internal_dir = repo / ".vibe-loop" / "internal-locks"
            internal_dir.mkdir(parents=True)
            (internal_dir / "resource-scheduler.lock").write_text(
                '{"pid": 1, "owner_task_id": "old"}\n',
                encoding="utf-8",
            )
            task = Task(
                task_id="TASK-01",
                title="Task 1",
                status="Next",
            )
            active_state = ActiveRunState.new(
                task_id=task.task_id,
                run_id="run-task",
                log_path=repo / "run.log",
                base_main="aaa",
                command="worker",
            )

            task_lock = runner.acquire_scheduled_task_lock(
                task,
                "run-task",
                active_state,
            )
            try:
                self.assertTrue(runner.lock_manager.is_locked("TASK-01"))
            finally:
                runner.lock_manager.release(task_lock)

    def test_run_until_done_parallel_skips_task_lock_races(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                if task.task_id == "TASK-01":
                    raise LockBusy(repo / ".vibe-loop" / "locks" / "TASK-01.lock", {})
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_slices=1)

        self.assertEqual([result.task_id for result in results], ["TASK-02"])

    def test_msvcrt_scheduler_lock_permission_error_reports_busy(self) -> None:
        class PermissionHandle:
            def seek(self, *args) -> int:
                raise PermissionError(13, "Permission denied")

        class FakeMsvcrt:
            LK_NBLCK = 1

            def __init__(self) -> None:
                self.calls = 0

            def locking(self, *args) -> None:
                self.calls += 1

        fake_msvcrt = FakeMsvcrt()
        original_fcntl = runner_module.fcntl
        original_msvcrt = runner_module.msvcrt
        try:
            runner_module.fcntl = None
            runner_module.msvcrt = fake_msvcrt

            locked = runner_module.try_lock_scheduler_file(PermissionHandle())
        finally:
            runner_module.fcntl = original_fcntl
            runner_module.msvcrt = original_msvcrt

        self.assertFalse(locked)
        self.assertEqual(fake_msvcrt.calls, 0)

    def test_acquire_scheduler_lock_closes_handle_on_lock_error(self) -> None:
        class FakeHandle:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            handle = FakeHandle()

            with (
                patch.object(Path, "open", return_value=handle),
                patch.object(
                    runner_module,
                    "try_lock_scheduler_file",
                    side_effect=PermissionError(13, "Permission denied"),
                ),
            ):
                with self.assertRaises(PermissionError):
                    runner.acquire_scheduler_lock("run-task", "TASK-01")

        self.assertTrue(handle.closed)

    def test_run_until_done_parallel_skips_scheduler_lock_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                if task.task_id == "TASK-01":
                    raise SchedulerLockBusy(
                        repo / ".vibe-loop" / "internal-locks" / "resource.lock"
                    )
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id}.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task

            results = runner.run_until_done(jobs=2, max_slices=1)

        self.assertEqual([result.task_id for result in results], ["TASK-02"])

    def test_lock_manager_rejects_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            task_lock = manager.acquire("LIVE-04", "run-1")
            try:
                self.assertTrue(manager.is_locked("LIVE-04"))
                with self.assertRaises(LockBusy):
                    manager.acquire("LIVE-04", "run-2")
            finally:
                manager.release(task_lock)
            self.assertFalse(manager.is_locked("LIVE-04"))

    def test_lock_manager_rejects_empty_existing_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_root = Path(directory) / "locks"
            (lock_root / "LIVE-04.lock").mkdir(parents=True)
            manager = LockManager(lock_root)

            with self.assertRaises(LockBusy):
                manager.acquire("LIVE-04", "run-2")

    def test_main_integration_lock_serializes_holder_and_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            holder = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
            )
            try:
                status = manager.main_integration_status(
                    process_exists=lambda pid: True,
                )

                self.assertTrue(status.locked)
                self.assertEqual(status.state, "held")
                self.assertEqual(status.process_state, "running")
                self.assertEqual(status.metadata["task_id"], "main-integration")
                self.assertEqual(status.metadata["owner_task_id"], "TASK-01")
                self.assertEqual(status.metadata["run_id"], "run-holder")
                with self.assertRaises(LockBusy) as busy:
                    manager.acquire_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
                self.assertEqual(busy.exception.metadata["owner_task_id"], "TASK-01")
                with self.assertRaises(LockOwnerMismatch):
                    manager.release_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
                self.assertTrue(
                    manager.release_main_integration(
                        task_id="TASK-01",
                        run_id="run-holder",
                    )
                )
                self.assertFalse(manager.main_integration_status().locked)
            finally:
                manager.release(holder)

    def test_main_integration_stale_lock_is_visible_but_not_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            held_lock = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
                metadata={"pid": 999999999, "host": "test-host"},
            )
            try:
                status = manager.main_integration_status(
                    current_host="test-host",
                    process_exists=lambda pid: False,
                )

                self.assertTrue(status.locked)
                self.assertEqual(status.state, "stale")
                self.assertEqual(status.process_state, "missing")
                self.assertEqual(status.stale_reason, "missing_process")
                with self.assertRaises(LockBusy):
                    manager.acquire_main_integration(
                        task_id="TASK-02",
                        run_id="run-waiter",
                    )
            finally:
                manager.release(held_lock)

    def test_main_integration_wait_retries_until_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            holder = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
            )
            sleeps: list[float] = []

            def release_holder(delay: float) -> None:
                sleeps.append(delay)
                manager.release(holder)

            result = manager.acquire_main_integration_with_wait(
                task_id="TASK-02",
                run_id="run-waiter",
                wait=True,
                timeout_seconds=10,
                poll_interval_seconds=0.1,
                sleep=release_holder,
            )

        self.assertTrue(result.acquired)
        self.assertFalse(result.timed_out)
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(result.status.metadata["owner_task_id"], "TASK-02")

    def test_main_integration_wait_times_out_without_stealing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
            )

            result = manager.acquire_main_integration_with_wait(
                task_id="TASK-02",
                run_id="run-waiter",
                wait=True,
                timeout_seconds=0,
            )
            status = manager.main_integration_status(process_exists=lambda pid: True)

        self.assertFalse(result.acquired)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.status.metadata["owner_task_id"], "TASK-01")
        self.assertEqual(status.metadata["owner_task_id"], "TASK-01")

    def test_main_integration_wait_retries_available_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "locks")
            holder = manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
            )
            original_acquire = manager.acquire_main_integration
            attempts = 0

            def acquire_with_race(*, task_id, run_id, metadata=None):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    manager.release(holder)
                    raise LockBusy(holder.path, holder.metadata)
                return original_acquire(
                    task_id=task_id,
                    run_id=run_id,
                    metadata=metadata,
                )

            with patch.object(
                manager,
                "acquire_main_integration",
                side_effect=acquire_with_race,
            ):
                result = manager.acquire_main_integration_with_wait(
                    task_id="TASK-02",
                    run_id="run-waiter",
                    wait=True,
                    timeout_seconds=10,
                )

        self.assertTrue(result.acquired)
        self.assertEqual(attempts, 2)
        self.assertEqual(result.status.metadata["owner_task_id"], "TASK-02")

    def test_streaming_command_forwards_stdout_and_logs_stderr_by_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\nprint('out')\nprint('err', file=sys.stderr)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            stdout = StringIO()
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(result.session_id)
            self.assertIsNone(result.session_id_source)
            self.assertEqual("", stdout.getvalue())
            self.assertIn("out", stderr.getvalue())
            self.assertNotIn("err", stderr.getvalue())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("out", log_text)
            self.assertIn("err", log_text)

    def test_streaming_command_can_forward_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\nprint('err', file=sys.stderr)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                        forward_stderr=True,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIn("err", stderr.getvalue())
            self.assertIn("err", log_path.read_text(encoding="utf-8"))

    def test_streaming_command_captures_stdout_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "print('session id: native-stdout-123')\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.session_id, "native-stdout-123")
            self.assertEqual(result.session_id_source, "native:stdout")
            self.assertIn("session id: native-stdout-123", stderr.getvalue())
            self.assertIn(
                "session id: native-stdout-123",
                log_path.read_text(encoding="utf-8"),
            )

    def test_streaming_command_reports_started_process_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            log_path = Path(directory) / "run.log"
            started_pids: list[int] = []
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                    on_start=started_pids.append,
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(started_pids), 1)
        self.assertGreater(started_pids[0], 0)

    def test_streaming_command_captures_stderr_session_id_without_forwarding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\nprint('session id: native-stderr-123', file=sys.stderr)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.session_id, "native-stderr-123")
            self.assertEqual(result.session_id_source, "native:stderr")
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn(
                "session id: native-stderr-123",
                log_path.read_text(encoding="utf-8"),
            )

    def test_streaming_command_replaces_undecodable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\n"
                "sys.stdout.buffer.write(b'ok\\xff\\n')\n"
                "sys.stderr.buffer.write(b'bad\\xfe\\n')\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                    )

            self.assertEqual(result.exit_code, 0)
            self.assertIn("ok", stderr.getvalue())
            self.assertIn("\ufffd", stderr.getvalue())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("ok", log_text)
            self.assertIn("bad", log_text)
            self.assertIn("\ufffd", log_text)


class TransientWorkerFailureTests(unittest.TestCase):
    def test_is_transient_worker_failure_detects_quota_in_log(self) -> None:
        from vibe_loop.runner import is_transient_worker_failure

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            log_path.write_text(
                "starting task\nworking...\n"
                "Error: 429 Too Many Requests\n"
                "API quota exceeded\n",
                encoding="utf-8",
            )
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="failed",
                exit_code=1,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
            )
            self.assertTrue(is_transient_worker_failure(result))

    def test_is_transient_worker_failure_ignores_non_transient_log(self) -> None:
        from vibe_loop.runner import is_transient_worker_failure

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            log_path.write_text(
                "starting task\nsyntax error at line 5\n",
                encoding="utf-8",
            )
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="failed",
                exit_code=1,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
            )
            self.assertFalse(is_transient_worker_failure(result))

    def test_is_transient_worker_failure_ignores_completed(self) -> None:
        from vibe_loop.runner import is_transient_worker_failure

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            log_path.write_text("rate limit\n", encoding="utf-8")
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="completed",
                exit_code=0,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
            )
            self.assertFalse(is_transient_worker_failure(result))

    def test_is_transient_worker_failure_ignores_blocked_worker_report(self) -> None:
        from vibe_loop.runner import is_transient_worker_failure

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            log_path.write_text("rate limit\n", encoding="utf-8")
            result = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="blocked",
                exit_code=1,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
                worker_report={"status": "blocked", "message": "needs approval"},
            )
            self.assertFalse(is_transient_worker_failure(result))

    def test_serial_loop_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "transient.log"
            log_path.write_text("Error: 429 rate limit\n", encoding="utf-8")

            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            call_count = 0

            def run_task(task: Task) -> RunResult:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    return RunResult(
                        run_id=f"run-{call_count}",
                        task_id=task.task_id,
                        classification="failed",
                        exit_code=1,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="aaa",
                    )
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{call_count}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="bbb",
                )

            runner.run_task = run_task
            with patch("vibe_loop.runner.time.sleep"):
                results = runner.run_until_done_serial()

        self.assertEqual(call_count, 3)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[-1].classification, "completed")

    def test_serial_loop_gives_up_after_max_transient_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "transient.log"
            log_path.write_text("Error: 503 Service Unavailable\n", encoding="utf-8")

            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                return RunResult(
                    run_id="run-1",
                    task_id=task.task_id,
                    classification="failed",
                    exit_code=1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            with patch("vibe_loop.runner.time.sleep"):
                results = runner.run_until_done_serial(continue_on_failure=True)

        self.assertEqual(len(results), SUPERVISION_DEFAULT_MAX_RESTARTS + 1)
        self.assertTrue(all(r.classification == "failed" for r in results))
        self.assertEqual(
            results[-1].classification_source,
            "restart_budget_exhausted",
        )

    def test_serial_loop_honors_configured_restart_budget_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(
                        max_restarts=1,
                        cooldown_seconds=0.25,
                    ),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("Error: 429 rate limit\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source

            def run_task(task: Task) -> RunResult:
                restart_count = runner.current_restart_count(task.task_id)
                return RunResult(
                    run_id=f"run-{restart_count}",
                    task_id=task.task_id,
                    classification="failed",
                    exit_code=1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                    restart_count=restart_count,
                    max_restarts=runner.config.supervision.max_restarts,
                )

            runner.run_task = run_task
            with patch("vibe_loop.runner.time.sleep") as sleep:
                results = runner.run_until_done_serial(continue_on_failure=True)

            records = runner.run_store.read_records()

        self.assertEqual([result.restart_count for result in results], [0, 1])
        self.assertEqual(results[-1].classification_source, "restart_budget_exhausted")
        sleep.assert_called_once_with(0.25)
        restart_records = [
            record for record in records if record.get("record_type") == "task_restart"
        ]
        self.assertEqual(len(restart_records), 2)
        self.assertEqual(restart_records[0]["restart_count"], 1)
        self.assertFalse(restart_records[0]["exhausted"])
        self.assertEqual(restart_records[1]["restart_count"], 1)
        self.assertEqual(restart_records[1]["attempted_restart_count"], 2)
        self.assertTrue(restart_records[1]["exhausted"])
        self.assertEqual(restart_records[1]["reason"], "restart_budget_exhausted")

    def test_restart_counts_do_not_accumulate_across_supervisor_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(max_restarts=1, cooldown_seconds=0),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("Error: 503 Service Unavailable\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            run_sequence = 0

            def run_task(task: Task) -> RunResult:
                nonlocal run_sequence
                run_sequence += 1
                restart_count = runner.current_restart_count(task.task_id)
                return RunResult(
                    run_id=f"run-{run_sequence}",
                    task_id=task.task_id,
                    classification="failed",
                    exit_code=1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                    restart_count=restart_count,
                    max_restarts=runner.config.supervision.max_restarts,
                )

            runner.run_task = run_task
            with patch("vibe_loop.runner.time.sleep"):
                first = runner.run_until_done_serial(continue_on_failure=True)
                second = runner.run_until_done_serial(continue_on_failure=True)

        self.assertEqual([result.restart_count for result in first], [0, 1])
        self.assertEqual([result.restart_count for result in second], [0, 1])

    def test_parallel_loop_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(cooldown_seconds=0),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("overloaded, please wait\n", encoding="utf-8")

            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            call_count = 0
            call_lock = threading.Lock()

            def run_task(task: Task) -> RunResult:
                nonlocal call_count
                with call_lock:
                    call_count += 1
                    current = call_count
                if current == 1:
                    return RunResult(
                        run_id="run-1",
                        task_id=task.task_id,
                        classification="failed",
                        exit_code=1,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="aaa",
                    )
                source.mark_done(task.task_id)
                return RunResult(
                    run_id="run-2",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="bbb",
                )

            runner.run_task = run_task

            results = runner.run_until_done_parallel(
                ask_agent=False,
                max_slices=0,
                continue_on_failure=False,
                jobs=1,
            )

        self.assertEqual(call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1].classification, "completed")

    def test_parallel_loop_honors_configured_restart_budget_and_cooldown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(
                        max_restarts=1,
                        cooldown_seconds=0.25,
                    ),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("overloaded, please wait\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            clock = [0.0]
            sleeps: list[float] = []

            def run_task(task: Task) -> RunResult:
                restart_count = runner.current_restart_count(task.task_id)
                return RunResult(
                    run_id=f"run-{restart_count}",
                    task_id=task.task_id,
                    classification="failed",
                    exit_code=1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                    restart_count=restart_count,
                    max_restarts=runner.config.supervision.max_restarts,
                )

            def advance_clock(delay: float) -> None:
                sleeps.append(delay)
                clock[0] += delay

            runner.run_task = run_task
            with (
                patch(
                    "vibe_loop.runner.time.monotonic",
                    side_effect=lambda: clock[0],
                ),
                patch("vibe_loop.runner.time.sleep", side_effect=advance_clock),
            ):
                results = runner.run_until_done_parallel(
                    ask_agent=False,
                    max_slices=0,
                    continue_on_failure=True,
                    jobs=1,
                )
            records = runner.run_store.read_records()

        self.assertEqual([result.restart_count for result in results], [0, 1])
        self.assertEqual(results[-1].classification, "failed")
        self.assertEqual(
            results[-1].classification_source,
            "restart_budget_exhausted",
        )
        self.assertEqual(sleeps, [0.25])
        restart_records = [
            record for record in records if record.get("record_type") == "task_restart"
        ]
        self.assertEqual(len(restart_records), 2)
        self.assertFalse(restart_records[0]["exhausted"])
        self.assertTrue(restart_records[1]["exhausted"])

    def test_parallel_loop_rebuilds_candidates_when_cooldown_expires_during_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(
                        max_restarts=1,
                        cooldown_seconds=0.25,
                    ),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("overloaded, please wait\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            clock = [0.0]
            original_list_candidates = runner.list_candidates
            discovery_advanced = False

            def run_task(task: Task) -> RunResult:
                restart_count = runner.current_restart_count(task.task_id)
                if restart_count:
                    source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{restart_count}",
                    task_id=task.task_id,
                    classification="completed" if restart_count else "failed",
                    exit_code=0 if restart_count else 1,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="bbb" if restart_count else "aaa",
                    restart_count=restart_count,
                    max_restarts=runner.config.supervision.max_restarts,
                )

            def list_candidates(exclude: set[str] | None = None) -> list[Task]:
                nonlocal discovery_advanced
                candidates = original_list_candidates(exclude=exclude)
                if (
                    exclude is not None
                    and "TASK-01" in exclude
                    and not discovery_advanced
                ):
                    discovery_advanced = True
                    clock[0] = 0.25
                return candidates

            runner.run_task = run_task
            runner.list_candidates = list_candidates
            with (
                patch(
                    "vibe_loop.runner.time.monotonic",
                    side_effect=lambda: clock[0],
                ),
                patch("vibe_loop.runner.time.sleep") as sleep,
            ):
                results = runner.run_until_done_parallel(
                    ask_agent=False,
                    max_slices=0,
                    continue_on_failure=False,
                    jobs=1,
                )

        self.assertTrue(discovery_advanced)
        sleep.assert_not_called()
        self.assertEqual([result.restart_count for result in results], [0, 1])
        self.assertEqual(results[-1].classification, "completed")

    def test_parallel_loop_requeues_ready_retry_while_other_task_is_running(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(
                        max_restarts=1,
                        cooldown_seconds=0.01,
                    ),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("overloaded, please wait\n", encoding="utf-8")
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            runner._source = source
            retry_started = threading.Event()
            b_finished = threading.Event()

            def run_task(task: Task) -> RunResult:
                restart_count = runner.current_restart_count(task.task_id)
                if task.task_id == "TASK-01" and restart_count == 0:
                    return RunResult(
                        run_id="run-a-0",
                        task_id=task.task_id,
                        classification="failed",
                        exit_code=1,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="aaa",
                        restart_count=restart_count,
                        max_restarts=runner.config.supervision.max_restarts,
                    )
                if task.task_id == "TASK-01":
                    retry_started.set()
                    source.mark_done(task.task_id)
                    return RunResult(
                        run_id="run-a-1",
                        task_id=task.task_id,
                        classification="completed",
                        exit_code=0,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="bbb",
                        restart_count=restart_count,
                        max_restarts=runner.config.supervision.max_restarts,
                    )
                retry_started.wait(timeout=1.0)
                b_finished.set()
                source.mark_done(task.task_id)
                return RunResult(
                    run_id="run-b",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / "task-b.log",
                    start_main="aaa",
                    end_main="bbb",
                )

            runner.run_task = run_task

            results = runner.run_until_done_parallel(
                ask_agent=False,
                max_slices=0,
                continue_on_failure=False,
                jobs=2,
                max_tasks=2,
            )

        self.assertTrue(retry_started.is_set())
        self.assertTrue(b_finished.is_set())
        self.assertEqual(
            [result.task_id for result in results],
            ["TASK-01", "TASK-01", "TASK-02"],
        )
        self.assertEqual(results[1].restart_count, 1)
        self.assertEqual(results[1].classification, "completed")


if __name__ == "__main__":
    unittest.main()
