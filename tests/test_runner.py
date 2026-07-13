from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import socket
import subprocess
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
    AgentResolutionError,
    CompletionConfig,
    SpecDiagnosticsConfig,
    SupervisionConfig,
    SUPERVISION_DEFAULT_MAX_RESTARTS,
    VibeConfig,
)
from vibe_loop.locks import LockBusy, LockManager, LockOwnerMismatch
from vibe_loop.runner import (
    SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS,
    AgentRuntimeContext,
    SchedulerLockBusy,
    VibeRunner,
    active_lock_conflict_domains,
    build_batch_selection_prompt,
    build_run_context_payload,
    build_selection_prompt,
    build_spec_worker_context,
    build_worker_prompt,
    claude_project_dir_name,
    command_specifies_resume,
    command_supports_session_capture,
    command_supports_session_resume,
    deterministic_task_batch,
    build_resume_continuation_prompt,
    inject_claude_resume,
    inject_claude_session_id,
    parse_agent_runtime_context_from_command,
    parse_selected_task_id,
    parse_selected_task_ids,
    parse_worker_session_id,
    RecoveryContext,
    resumable_prior_session_id,
    build_recovery_prompt_section,
    predicted_claude_transcript,
    resolve_claude_home,
    resolve_claude_transcript,
    run_streaming_command,
    terminate_worker_process_group,
    validate_analysis_prompt_delivery,
    validate_selected_task_batch,
    wait_with_reap_watchdog,
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


def file_fingerprint(path: Path, relative_path: str) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "path": relative_path,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


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
            fingerprint = file_fingerprint(repo / "docs" / "spec.md", "docs/spec.md")
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
                        "size": (repo / "docs" / "spec.md").stat().st_size,
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
                        "size": (repo / "docs" / "spec.md").stat().st_size,
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

    def test_classify_task_probe_statuses_are_case_insensitive(self) -> None:
        # Command task sources pass wire statuses through verbatim (e.g. a
        # loopyard adapter returns lowercase "done"), so the probe fallback
        # must not depend on canonical capitalization.
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))

            for raw_status, expected in (
                ("Done", "completed"),
                ("done", "completed"),
                ("DONE", "completed"),
                ("Gated", "blocked"),
                ("gated", "blocked"),
                ("GATED", "blocked"),
            ):
                with self.subTest(status=raw_status):
                    runner._source = MutableTaskSource(
                        [
                            Task(
                                task_id="TASK-01",
                                title="Task 1",
                                status=raw_status,
                                order=1,
                            )
                        ]
                    )
                    result = runner.classify("TASK-01", 0, "aaa", "aaa", "", None)
                    self.assertEqual(result.status, expected)
                    self.assertEqual(result.source, "task_probe")

    def test_classify_detects_limit_wall_before_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            result = runner.classify(
                "TASK-01",
                1,
                "aaa",
                "aaa",
                "",
                None,
                output_tail="You've hit your session limit · resets 1am (UTC)",
            )
        self.assertEqual(result.status, "limit_wall")
        self.assertEqual(result.source, "limit_wall")
        self.assertEqual(result.detail, "resets 1am (UTC)")

    def test_classify_worker_report_wins_over_limit_wall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            result = runner.classify(
                "TASK-01",
                1,
                "aaa",
                "aaa",
                "",
                WorkerReport(run_id="r", task_id="TASK-01", status="completed"),
                output_tail="You've reached your Fable 5 limit",
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source, "worker_report")

    def test_classify_limit_wall_disabled_falls_back_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(
                VibeConfig(
                    repo=Path(directory),
                    supervision=SupervisionConfig(limit_wall_detection=False),
                )
            )
            result = runner.classify(
                "TASK-01",
                1,
                "aaa",
                "aaa",
                "",
                None,
                output_tail="You've hit your session limit",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.source, "exit_code_or_completion_check")

    def test_classify_honors_custom_limit_wall_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(
                VibeConfig(
                    repo=Path(directory),
                    supervision=SupervisionConfig(
                        limit_wall_patterns=("provider wall reached",)
                    ),
                )
            )
            # The default phrase no longer matches under a custom override.
            default_phrase = runner.classify(
                "TASK-01",
                1,
                "a",
                "a",
                "",
                None,
                output_tail="You've hit your session limit",
            )
            custom_phrase = runner.classify(
                "TASK-01",
                1,
                "a",
                "a",
                "",
                None,
                output_tail="the provider wall reached",
            )
        self.assertEqual(default_phrase.status, "failed")
        self.assertEqual(custom_phrase.status, "limit_wall")

    def test_inject_claude_resume_inserts_flag_before_prompt(self) -> None:
        self.assertEqual(
            inject_claude_resume("claude -p {prompt}", "sid-123"),
            "claude -p --resume sid-123 {prompt}",
        )
        self.assertEqual(
            inject_claude_resume("claude -p", "sid-123"),
            "claude -p --resume sid-123",
        )

    def test_command_supports_session_resume_gating(self) -> None:
        self.assertTrue(command_supports_session_resume("claude -p {prompt}", "claude"))
        self.assertTrue(command_supports_session_resume("claude -p {prompt}", "auto"))
        # Non-claude agent kind / executable cannot resume a claude session.
        self.assertFalse(command_supports_session_resume("claude -p {prompt}", "codex"))
        self.assertFalse(
            command_supports_session_resume("codex exec {prompt}", "claude")
        )
        # Operator already pinned a session id or a resume/continue flag.
        self.assertFalse(
            command_supports_session_resume(
                "claude -p --session-id x {prompt}", "claude"
            )
        )
        self.assertFalse(
            command_supports_session_resume("claude -p --resume x {prompt}", "claude")
        )
        self.assertFalse(
            command_supports_session_resume("claude -p --continue {prompt}", "claude")
        )
        # Session persistence disabled: the prior session is not on disk to resume.
        self.assertFalse(
            command_supports_session_resume(
                "claude -p --no-session-persistence {prompt}", "claude"
            )
        )

    def test_command_specifies_resume_detects_flags(self) -> None:
        self.assertTrue(command_specifies_resume(["claude", "--resume", "x"]))
        self.assertTrue(command_specifies_resume(["claude", "-r", "x"]))
        self.assertTrue(command_specifies_resume(["claude", "--continue"]))
        self.assertTrue(command_specifies_resume(["claude", "--resume=x"]))
        self.assertFalse(command_specifies_resume(["claude", "-p", "{prompt}"]))

    def test_build_resume_continuation_prompt_is_a_short_finish_nudge(self) -> None:
        recovery = RecoveryContext(
            task_id="TASK-01",
            prior_run_id="run-1",
            prior_classification="unknown",
            branch="task-01",
            worktree="/tmp/wt/task-01",
            head_commit="abc",
            transcript_path="/t.jsonl",
            wrapper_log="/w.log",
            attempt=2,
            max_attempts=3,
            workspace_claimed=True,
            prior_session_id="sid-123",
        )
        prompt = build_resume_continuation_prompt(recovery)
        self.assertIn("resumed session", prompt)
        self.assertIn("TASK-01", prompt)
        self.assertIn("attempt 2 of 3", prompt)
        self.assertIn("/tmp/wt/task-01", prompt)
        self.assertIn("$VIBE_LOOP_RUN_ID", prompt)
        self.assertIn("background", prompt)
        # Must NOT be the from-scratch recovery brief.
        self.assertNotIn("Investigate what the previous session did", prompt)

    def test_resumable_prior_session_id_requires_observed_and_on_disk(self) -> None:
        base = dict(
            run_id="r",
            task_id="T",
            classification="unknown",
            exit_code=0,
            log_path=Path("/l.log"),
            start_main="a",
            end_main="a",
        )
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "sid.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            # observed session with a transcript on disk -> resumable.
            self.assertEqual(
                resumable_prior_session_id(
                    RunResult(
                        **base,
                        session_id="sid",
                        session_id_source="observed",
                        transcript_path=str(transcript),
                    )
                ),
                "sid",
            )
            # observed but transcript missing -> fail closed (fresh path).
            self.assertEqual(
                resumable_prior_session_id(
                    RunResult(
                        **base,
                        session_id="sid",
                        session_id_source="observed",
                        transcript_path=str(Path(directory) / "missing.jsonl"),
                    )
                ),
                "",
            )
            # non-observed (stream-derived / fallback) session -> not resumable.
            self.assertEqual(
                resumable_prior_session_id(
                    RunResult(
                        **base,
                        session_id="sid",
                        session_id_source="fallback:run_id",
                        transcript_path=str(transcript),
                    )
                ),
                "",
            )
            # observed but no transcript path recorded -> not resumable.
            self.assertEqual(
                resumable_prior_session_id(
                    RunResult(
                        **base,
                        session_id="sid",
                        session_id_source="observed",
                        transcript_path="",
                    )
                ),
                "",
            )

    def test_recovery_context_prior_session_id_defaults_empty(self) -> None:
        recovery = RecoveryContext(
            task_id="T",
            prior_run_id="r",
            prior_classification="unknown",
            branch="",
            worktree="",
            head_commit="",
            transcript_path="",
            wrapper_log="",
            attempt=1,
            max_attempts=3,
            workspace_claimed=False,
        )
        self.assertEqual(recovery.prior_session_id, "")

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
            fingerprint = file_fingerprint(spec_path, "docs/spec.md")
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

    def test_streaming_command_captures_startup_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'model': {'provider': 'openai', "
                "'id': 'gpt-5.5', 'reasoning_effort': 'high'}}))\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            observations = []
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                    on_observation=observations.append,
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.runtime_context.model_provider, "openai")
        self.assertEqual(result.runtime_context.model_id, "gpt-5.5")
        self.assertEqual(result.runtime_context.reasoning_effort, "high")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].runtime_context.model_id, "gpt-5.5")

    def test_streaming_command_ignores_unqualified_reasoning_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'model': {'id': 'gpt-5.5'}, "
                "'reasoning': 'private chain of thought'}))\n"
                "print('reasoning: secret-token-value')\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.runtime_context.model_id, "gpt-5.5")
        self.assertEqual(result.runtime_context.reasoning_effort, "")

    def test_command_context_omits_shell_variables_and_wrapper_inference(self) -> None:
        context = parse_agent_runtime_context_from_command(
            "python wrapper.py codex exec --model $MODEL --reasoning-effort verbose"
        )

        self.assertEqual(context.model_provider, "")
        self.assertEqual(context.model_id, "")
        self.assertEqual(context.reasoning_effort, "")

    def test_command_context_accepts_direct_executable_and_safe_effort(self) -> None:
        context = parse_agent_runtime_context_from_command(
            "OPENAI_API_KEY=redacted codex exec --model gpt-5.5 --reasoning-effort high"
        )

        self.assertEqual(context.model_provider, "openai")
        self.assertEqual(context.model_provider_source, "command_executable:codex")
        self.assertEqual(context.model_id, "gpt-5.5")
        self.assertEqual(context.reasoning_effort, "high")

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


class LimitWallLoopTests(unittest.TestCase):
    def _limit_wall_runner(
        self,
        repo: Path,
        source: MutableTaskSource,
        calls: list[str],
    ) -> VibeRunner:
        runner = VibeRunner(VibeConfig(repo=repo, agent=AgentConfig(command="worker")))
        runner._source = source

        def run_task(task: Task) -> RunResult:
            calls.append(task.task_id)
            return RunResult(
                run_id=f"run-{task.task_id}-{len(calls)}",
                task_id=task.task_id,
                classification="limit_wall",
                exit_code=1,
                log_path=repo / f"{task.task_id}.log",
                start_main="aaa",
                end_main="aaa",
                message="resets 1am (UTC)",
            )

        runner.run_task = run_task
        return runner

    def test_serial_limit_wall_stops_without_consuming_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            calls: list[str] = []
            runner = self._limit_wall_runner(repo, source, calls)
            restart_calls: list[object] = []
            runner.record_task_restart = (  # type: ignore[method-assign]
                lambda *args, **kwargs: restart_calls.append((args, kwargs))
            )

            results = runner.run_until_done()

        self.assertEqual([result.classification for result in results], ["limit_wall"])
        # Dispatch stops instead of tight-looping into the same wall.
        self.assertEqual(calls, ["TASK-01"])
        # No restart/recovery budget is consumed.
        self.assertEqual(restart_calls, [])
        # The task remains runnable for the supervisor's next cycle.
        self.assertNotIn("TASK-01", source._done)

    def test_parallel_limit_wall_stops_without_consuming_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            calls: list[str] = []
            runner = self._limit_wall_runner(repo, source, calls)
            restart_calls: list[object] = []
            runner.record_task_restart = (  # type: ignore[method-assign]
                lambda *args, **kwargs: restart_calls.append((args, kwargs))
            )

            results = runner.run_until_done(jobs=2)

        self.assertTrue(results)
        self.assertTrue(
            all(result.classification == "limit_wall" for result in results)
        )
        self.assertEqual(restart_calls, [])
        self.assertEqual(source._done, set())


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

    def test_build_recovery_prompt_section_includes_claimed_workspace(self) -> None:
        recovery = RecoveryContext(
            task_id="TASK-01",
            prior_run_id="run-1",
            prior_classification="unknown",
            branch="auto-01-branch",
            worktree="/tmp/auto-01",
            head_commit="abc123",
            transcript_path="/tmp/transcript.jsonl",
            wrapper_log="/tmp/run-1.log",
            attempt=2,
            max_attempts=3,
            workspace_claimed=True,
        )

        section = build_recovery_prompt_section(recovery)

        self.assertIn("Unknown-Run Recovery", section)
        self.assertIn("TASK-01", section)
        self.assertIn("run-1", section)
        self.assertIn("auto-01-branch", section)
        self.assertIn("/tmp/auto-01", section)
        self.assertIn("/tmp/transcript.jsonl", section)
        self.assertIn("/tmp/run-1.log", section)
        self.assertIn("attempt 2 of 3", section)
        self.assertIn("do NOT park", section)

    def test_build_recovery_prompt_section_notes_missing_claim(self) -> None:
        recovery = RecoveryContext(
            task_id="TASK-01",
            prior_run_id="run-1",
            prior_classification="unknown",
            branch="",
            worktree="",
            head_commit="",
            transcript_path="",
            wrapper_log="/tmp/run-1.log",
            attempt=1,
            max_attempts=3,
            workspace_claimed=False,
        )

        section = build_recovery_prompt_section(recovery)

        self.assertIn("No `workspace_claim` record", section)
        self.assertIn("transcript: not captured", section)

    def test_serial_loop_recovers_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run.log"
            log_path.write_text("worker parked on external gate\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            calls: list[RecoveryContext | None] = []
            call_count = 0

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                nonlocal call_count
                call_count += 1
                calls.append(recovery)
                if recovery is None:
                    return RunResult(
                        run_id="run-1",
                        task_id=task.task_id,
                        classification="unknown",
                        exit_code=0,
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
            results = runner.run_until_done_serial()
            records = runner.run_store.read_records()

        self.assertEqual(call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1].classification, "completed")
        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])
        assert calls[1] is not None
        self.assertEqual(calls[1].prior_run_id, "run-1")
        self.assertEqual(calls[1].attempt, 1)
        recovery_records = [
            record for record in records if record.get("record_type") == "task_recovery"
        ]
        phases = [record["phase"] for record in recovery_records]
        self.assertEqual(phases, ["launched", "outcome"])
        self.assertEqual(recovery_records[1]["outcome"], "completed")
        restart_records = [
            record
            for record in records
            if record.get("record_type") == "task_restart"
            and record.get("reason") == "unknown_run_recovery"
        ]
        self.assertEqual(len(restart_records), 1)

    def test_serial_loop_recovery_budget_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(max_restarts=2, cooldown_seconds=0),
                )
            )
            log_path = repo / "run.log"
            log_path.write_text("still parked\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            recovery_calls = 0
            run_count = 0

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                nonlocal recovery_calls, run_count
                run_count += 1
                if recovery is not None:
                    recovery_calls += 1
                return RunResult(
                    run_id=f"run-{run_count}",
                    task_id=task.task_id,
                    classification="unknown",
                    exit_code=0,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            results = runner.run_until_done_serial(continue_on_failure=True)
            records = runner.run_store.read_records()

        self.assertEqual(recovery_calls, 2)
        self.assertEqual(results[-1].classification, "failed")
        self.assertEqual(
            results[-1].classification_source,
            "recovery_budget_exhausted",
        )
        launched = [
            record
            for record in records
            if record.get("record_type") == "task_recovery"
            and record.get("phase") == "launched"
        ]
        self.assertEqual(len(launched), 2)
        exhausted = [
            record
            for record in records
            if record.get("record_type") == "task_restart"
            and record.get("reason") == "recovery_budget_exhausted"
        ]
        self.assertEqual(len(exhausted), 1)
        self.assertTrue(exhausted[0]["exhausted"])

    def test_serial_loop_recovery_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(recover_unknown_runs=False),
                )
            )
            log_path = repo / "run.log"
            log_path.write_text("parked\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            call_count = 0

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                nonlocal call_count
                call_count += 1
                return RunResult(
                    run_id="run-1",
                    task_id=task.task_id,
                    classification="unknown",
                    exit_code=0,
                    log_path=log_path,
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            results = runner.run_until_done_serial(continue_on_failure=True)
            records = runner.run_store.read_records()

        self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].classification, "unknown")
        self.assertFalse(
            any(record.get("record_type") == "task_recovery" for record in records)
        )

    def test_recover_unknown_run_carries_workspace_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run-1.log"
            log_path.write_text("parked\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            runner.run_store.append_record(
                {
                    "record_type": "workspace_claim",
                    "event_type": "workspace_claimed",
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "auto-01-branch",
                    "worktree": "/tmp/auto-01",
                    "head_commit": "deadbeef",
                }
            )
            captured: list[RecoveryContext | None] = []

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                captured.append(recovery)
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
            prior = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="unknown",
                exit_code=0,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
                transcript_path="/tmp/transcript.jsonl",
            )
            result = runner.recover_unknown_run(prior, attempt=1, max_attempts=3)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.classification, "completed")
        self.assertEqual(len(captured), 1)
        recovery = captured[0]
        assert recovery is not None
        self.assertTrue(recovery.workspace_claimed)
        self.assertEqual(recovery.branch, "auto-01-branch")
        self.assertEqual(recovery.worktree, str(Path("/tmp/auto-01")))
        self.assertEqual(recovery.head_commit, "deadbeef")
        self.assertEqual(recovery.transcript_path, "/tmp/transcript.jsonl")
        self.assertEqual(recovery.wrapper_log, str(log_path))

    def test_recover_unknown_run_skips_when_task_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run-1.log"
            log_path.write_text("parked\n", encoding="utf-8")
            runner._source = MutableTaskSource([])

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                raise AssertionError("run_task should not be called")

            runner.run_task = run_task
            prior = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="unknown",
                exit_code=0,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
            )
            result = runner.recover_unknown_run(prior, attempt=1, max_attempts=3)

        self.assertIsNone(result)

    def test_recover_unknown_run_defers_on_lock_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run-1.log"
            log_path.write_text("parked\n", encoding="utf-8")
            runner._source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                raise LockBusy(repo / "lock", {"task_id": task.task_id})

            runner.run_task = run_task
            prior = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="unknown",
                exit_code=0,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
            )
            result = runner.recover_unknown_run(prior, attempt=1, max_attempts=3)
            records = runner.run_store.read_records()

        self.assertIsNone(result)
        launched = [
            record
            for record in records
            if record.get("record_type") == "task_recovery"
            and record.get("phase") == "launched"
        ]
        self.assertEqual(len(launched), 1)
        outcomes = [
            record
            for record in records
            if record.get("record_type") == "task_recovery"
            and record.get("phase") == "outcome"
        ]
        self.assertEqual(outcomes, [])

    def test_parallel_loop_recovers_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker"),
                    supervision=SupervisionConfig(cooldown_seconds=0),
                )
            )
            log_path = repo / "run.log"
            log_path.write_text("parked\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            call_lock = threading.Lock()
            recovery_calls = 0

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                nonlocal recovery_calls
                with call_lock:
                    if recovery is not None:
                        recovery_calls += 1
                        is_recovery = True
                    else:
                        is_recovery = False
                if not is_recovery:
                    return RunResult(
                        run_id="run-1",
                        task_id=task.task_id,
                        classification="unknown",
                        exit_code=0,
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

        self.assertEqual(recovery_calls, 1)
        self.assertEqual(results[-1].classification, "completed")

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
        self.assertEqual(results[0].task_id, "TASK-01")
        self.assertEqual(results[0].classification, "failed")
        self.assertCountEqual(
            [(result.task_id, result.classification) for result in results],
            [
                ("TASK-01", "failed"),
                ("TASK-01", "completed"),
                ("TASK-02", "completed"),
            ],
        )
        retry_result = next(
            result
            for result in results
            if result.task_id == "TASK-01" and result.classification == "completed"
        )
        self.assertEqual(retry_result.restart_count, 1)


def _active_run_state(
    *,
    task_id: str,
    run_id: str,
    worker_pid: int,
    host: str,
    repo: Path,
    paths: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
) -> ActiveRunState:
    return ActiveRunState(
        task_id=task_id,
        run_id=run_id,
        worker_pid=worker_pid,
        supervisor_pid=worker_pid,
        host=host,
        started_at="2026-05-09T00:00:00+00:00",
        log_path=repo / ".vibe-loop" / "runs" / f"{run_id}.log",
        base_main="abc123",
        command=f"agent {task_id}",
        paths=paths,
        resources=resources,
        conflict_domains_known=True,
    )


class ActiveLockConflictDomainLivenessTests(unittest.TestCase):
    """A lock only leases its conflict domains while its run is actually live.

    Regression guard for the run-until-done empty-selection bug: a lock left
    behind by a dead worker (matching host, dead pid) kept serializing its
    broad path/resource domains, which blocked every dep-free ready task that
    shared one of those domains and made the runnable set empty.
    """

    def test_stale_lock_does_not_hold_conflict_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            state = _active_run_state(
                task_id="DEAD-OWNER",
                run_id="run-dead",
                worker_pid=999999999,  # not a live pid on this host
                host=socket.gethostname(),
                repo=repo,
                paths=("kernel/src/cap", "Makefile"),
                resources=("resource:system-monitoring",),
            )
            manager.acquire("DEAD-OWNER", "run-dead", metadata=state.to_lock_metadata())

            domains = active_lock_conflict_domains(manager)

            self.assertEqual(
                domains,
                (),
                "a dead-owner lock must not keep leasing its conflict domains",
            )

    def test_live_lock_still_holds_conflict_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            state = _active_run_state(
                task_id="LIVE-OWNER",
                run_id="run-live",
                worker_pid=os.getpid(),  # this test process is alive
                host=socket.gethostname(),
                repo=repo,
                paths=("kernel/src/cap", "Makefile"),
                resources=("resource:system-monitoring",),
            )
            manager.acquire("LIVE-OWNER", "run-live", metadata=state.to_lock_metadata())

            domains = active_lock_conflict_domains(manager)

            self.assertEqual(
                len(domains),
                1,
                "a live lock must keep serializing its conflict domains",
            )
            self.assertIn("Makefile", domains[0].paths)
            self.assertIn("resource:system-monitoring", domains[0].resources)


class StaleLockSelectionDrainingTests(unittest.TestCase):
    """list_candidates drains dep-free ready tasks past a stale broad lock."""

    def _runner(self, repo: Path, tasks: list[Task]) -> VibeRunner:
        runner = VibeRunner(VibeConfig(repo=repo, agent=AgentConfig(command="worker")))
        runner._source = MutableTaskSource(tasks)
        return runner

    def test_dep_free_tasks_selectable_despite_stale_broad_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop").mkdir(parents=True, exist_ok=True)
            tasks = [
                Task(
                    task_id="dep-free-a",
                    title="dep-free a",
                    status="Next",
                    paths=("kernel/src/cap",),
                    resources=("resource:a",),
                    conflict_domains_known=True,
                ),
                Task(
                    task_id="dep-free-b",
                    title="dep-free b",
                    status="Next",
                    paths=("Makefile",),
                    resources=("resource:b",),
                    conflict_domains_known=True,
                ),
            ]
            runner = self._runner(repo, tasks)
            # Stale lock with broad paths overlapping both ready tasks.
            stale = _active_run_state(
                task_id="stale-owner",
                run_id="run-stale",
                worker_pid=999999999,
                host=socket.gethostname(),
                repo=repo,
                paths=("kernel/src/cap", "Makefile"),
                resources=("resource:stale",),
            )
            runner.lock_manager.acquire(
                "stale-owner", "run-stale", metadata=stale.to_lock_metadata()
            )

            candidate_ids = {task.task_id for task in runner.list_candidates()}

            self.assertEqual(candidate_ids, {"dep-free-a", "dep-free-b"})

    def test_live_broad_lock_still_serializes_overlapping_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop").mkdir(parents=True, exist_ok=True)
            tasks = [
                Task(
                    task_id="overlaps",
                    title="overlaps Makefile",
                    status="Next",
                    paths=("Makefile",),
                    resources=("resource:a",),
                    conflict_domains_known=True,
                ),
                Task(
                    task_id="disjoint",
                    title="disjoint domain",
                    status="Next",
                    paths=("demos/",),
                    resources=("resource:b",),
                    conflict_domains_known=True,
                ),
            ]
            runner = self._runner(repo, tasks)
            live = _active_run_state(
                task_id="live-owner",
                run_id="run-live",
                worker_pid=os.getpid(),
                host=socket.gethostname(),
                repo=repo,
                paths=("Makefile",),
                resources=("resource:live",),
            )
            runner.lock_manager.acquire(
                "live-owner", "run-live", metadata=live.to_lock_metadata()
            )

            candidate_ids = {task.task_id for task in runner.list_candidates()}

            self.assertEqual(candidate_ids, {"disjoint"})


class FakeWatchdogProcess:
    """Minimal Popen stand-in for watchdog tests.

    ``wait(timeout=...)`` raises ``TimeoutExpired`` until ``alive_polls`` is
    exhausted, then returns ``returncode``; ``wait()`` (no timeout) returns
    immediately so a forced kill resolves.
    """

    def __init__(self, *, alive_polls: int, pid: int = 4321, returncode: int = 0):
        self.pid = pid
        self.returncode = returncode
        self._remaining = alive_polls
        self.kill_calls = 0

    def wait(self, timeout=None):
        if timeout is None:
            return self.returncode
        if self._remaining > 0:
            self._remaining -= 1
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    def kill(self):
        self.kill_calls += 1


class WaitWithReapWatchdogTests(unittest.TestCase):
    def test_no_reap_check_is_a_plain_blocking_wait(self):
        proc = FakeWatchdogProcess(alive_polls=0, returncode=7)
        result = wait_with_reap_watchdog(
            proc, StringIO(), reap_check=None, grace_seconds=120.0, poll_seconds=0.01
        )
        self.assertEqual(result, 7)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_worker_exiting_within_grace_is_not_killed(self):
        proc = FakeWatchdogProcess(alive_polls=2)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=100.0,
                poll_seconds=0.001,
            )
        self.assertEqual(result, 0)
        self.assertEqual(killed, [])

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_worker_hung_after_terminal_report_is_reaped(self):
        proc = FakeWatchdogProcess(alive_polls=10_000)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=0.0,
                poll_seconds=0.001,
            )
        self.assertEqual(result, 0)
        self.assertTrue(killed)
        self.assertEqual(killed[0], (proc.pid, signal.SIGTERM))

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_not_eligible_keeps_waiting_without_killing(self):
        proc = FakeWatchdogProcess(alive_polls=3)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: False,
                grace_seconds=0.0,
                poll_seconds=0.001,
            )
        self.assertEqual(result, 0)
        self.assertEqual(killed, [])

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_reap_check_exception_does_not_abort_supervision(self):
        proc = FakeWatchdogProcess(alive_polls=2)

        def boom() -> bool:
            raise RuntimeError("flaky report read")

        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=boom,
                grace_seconds=0.0,
                poll_seconds=0.001,
            )
        self.assertEqual(result, 0)
        self.assertEqual(killed, [])

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_terminate_sigterm_then_sigkill_when_group_lingers(self):
        # SIGTERM is sent, the group does not die within the grace, so SIGKILL
        # follows. alive_polls=1 makes the post-SIGTERM wait(timeout=...) raise
        # once before the no-timeout wait resolves.
        proc = FakeWatchdogProcess(alive_polls=1)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            terminate_worker_process_group(
                proc, StringIO(), sigkill_after_seconds=0.001
            )
        self.assertEqual(
            killed, [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
        )


def write_analysis_stub(path: Path, *, stdout: str = "", exit_code: int = 0) -> None:
    payload = stdout.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write('{payload}')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class AnalysisAgentTests(unittest.TestCase):
    def test_validate_analysis_prompt_delivery_requires_prompt_field(self) -> None:
        validate_analysis_prompt_delivery("reviewer --read-only {prompt}")
        with self.assertRaisesRegex(AgentResolutionError, "must include .prompt."):
            validate_analysis_prompt_delivery("reviewer --read-only")

    def test_run_analysis_agent_parses_json_and_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stub = repo / "analysis-stub.py"
            write_analysis_stub(
                stub,
                stdout='thinking...\n{"decision": "keep", "reason": "active WIP"}\n',
            )
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="worker",
                        analysis_command=f"{stub} {{prompt}}",
                    ),
                )
            )
            output_path = repo / "decision.json"

            payload = runner.run_analysis_agent("inspect worktrees", output_path)

            self.assertEqual(payload, {"decision": "keep", "reason": "active WIP"})
            self.assertTrue(output_path.exists())
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                {"decision": "keep", "reason": "active WIP"},
            )

    def test_run_analysis_agent_returns_none_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stub = repo / "analysis-stub.py"
            write_analysis_stub(stub, stdout='{"decision": "reap"}', exit_code=2)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="worker",
                        analysis_command=f"{stub} {{prompt}}",
                    ),
                )
            )
            output_path = repo / "decision.json"

            payload = runner.run_analysis_agent("inspect", output_path)

            self.assertIsNone(payload)
            self.assertFalse(output_path.exists())

    def test_run_analysis_agent_returns_none_on_non_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stub = repo / "analysis-stub.py"
            write_analysis_stub(stub, stdout="no structured decision here\n")
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(
                        command="worker",
                        analysis_command=f"{stub} {{prompt}}",
                    ),
                )
            )
            output_path = repo / "decision.json"

            payload = runner.run_analysis_agent("inspect", output_path)

            self.assertIsNone(payload)
            self.assertFalse(output_path.exists())

    def test_run_analysis_agent_requires_resolved_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            with self.assertRaises(AgentResolutionError):
                runner.run_analysis_agent("inspect", repo / "decision.json")


class SessionIdInjectionTests(unittest.TestCase):
    def test_supports_capture_for_default_claude_command(self) -> None:
        self.assertTrue(
            command_supports_session_capture("claude -p {prompt}", "claude")
        )
        self.assertTrue(command_supports_session_capture("claude -p {prompt}", "auto"))

    def test_supports_capture_skips_env_prefixed_claude(self) -> None:
        self.assertTrue(
            command_supports_session_capture(
                "CLAUDE_HOME=.claude claude -p {prompt}", "auto"
            )
        )

    def test_does_not_capture_codex_or_explicit_session_id(self) -> None:
        self.assertFalse(
            command_supports_session_capture("codex exec {prompt}", "auto")
        )
        self.assertFalse(
            command_supports_session_capture("codex exec {prompt}", "codex")
        )
        self.assertFalse(
            command_supports_session_capture(
                "claude -p --session-id fixed {prompt}", "claude"
            )
        )
        # An explicit codex kind must not get a Claude flag even if mislabeled.
        self.assertFalse(
            command_supports_session_capture("claude -p {prompt}", "codex")
        )

    def test_inject_inserts_flag_before_prompt(self) -> None:
        injected = inject_claude_session_id("claude -p {prompt}", "sid-123")
        self.assertEqual(injected, "claude -p --session-id sid-123 {prompt}")
        # The {prompt} placeholder survives for the later .format() call.
        self.assertEqual(
            injected.format(prompt="'hello world'"),
            "claude -p --session-id sid-123 'hello world'",
        )

    def test_inject_appends_when_no_prompt_placeholder(self) -> None:
        self.assertEqual(
            inject_claude_session_id("claude -p", "sid-9"),
            "claude -p --session-id sid-9",
        )

    def test_project_dir_name_replaces_non_alphanumeric(self) -> None:
        self.assertEqual(
            claude_project_dir_name(Path("/work/u/vibe-loop")),
            "-work-u-vibe-loop",
        )
        self.assertEqual(
            claude_project_dir_name(Path("/a/b.c_d")),
            "-a-b-c-d",
        )

    def test_resolve_claude_home_prefers_inline_then_env_then_default(self) -> None:
        cwd = Path("/repo")
        self.assertEqual(
            resolve_claude_home("CLAUDE_HOME=/abs claude -p {prompt}", {}, cwd),
            Path("/abs"),
        )
        self.assertEqual(
            resolve_claude_home("CLAUDE_HOME=rel claude -p {prompt}", {}, cwd),
            Path("/repo/rel"),
        )
        self.assertEqual(
            resolve_claude_home("claude -p {prompt}", {"CLAUDE_HOME": "/env"}, cwd),
            Path("/env"),
        )
        self.assertEqual(
            resolve_claude_home("claude -p {prompt}", {}, cwd),
            Path.home() / ".claude",
        )

    def test_resolve_transcript_globs_by_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "projects" / "-some-encoded-cwd"
            project.mkdir(parents=True)
            transcript = project / "abc-123.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")

            self.assertEqual(
                resolve_claude_transcript("abc-123", home),
                transcript,
            )
            self.assertIsNone(resolve_claude_transcript("missing", home))

    def test_predicted_transcript_uses_encoded_cwd(self) -> None:
        predicted = predicted_claude_transcript(
            "abc-123",
            Path("/work/u/repo"),
            Path("/claude"),
        )
        self.assertEqual(
            predicted,
            Path("/claude/projects/-work-u-repo/abc-123.jsonl"),
        )

    def test_run_context_payload_includes_transcript_path_when_present(self) -> None:
        payload = build_run_context_payload(
            task_id="T-1",
            run_id="r-1",
            started_at="2026-01-01T00:00:00Z",
            session_id="sid-1",
            session_id_source="observed",
            agent_kind="claude",
            agent_kind_source="explicit",
            agent_prompt_dialect="claude",
            agent_prompt_dialect_source="explicit",
            agent_skill_ref_prefix="/",
            agent_skill_ref_prefix_source="explicit",
            runtime_context=AgentRuntimeContext(),
            transcript_path="/work/u/.claude/projects/p/sid-1.jsonl",
        )
        self.assertEqual(
            payload["transcript_path"],
            "/work/u/.claude/projects/p/sid-1.jsonl",
        )

    def test_run_context_payload_omits_empty_transcript_path(self) -> None:
        payload = build_run_context_payload(
            task_id="T-1",
            run_id="r-1",
            started_at="2026-01-01T00:00:00Z",
            session_id="r-1",
            session_id_source="fallback:run_id",
            agent_kind="codex",
            agent_kind_source="explicit",
            agent_prompt_dialect="codex",
            agent_prompt_dialect_source="explicit",
            agent_skill_ref_prefix="$",
            agent_skill_ref_prefix_source="explicit",
            runtime_context=AgentRuntimeContext(),
        )
        self.assertNotIn("transcript_path", payload)


if __name__ == "__main__":
    unittest.main()
