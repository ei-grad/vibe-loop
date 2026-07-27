from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import shlex
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

import vibe_loop.locks as locks_module
import vibe_loop.runner as runner_module
import vibe_loop.tasks as tasks_module
from vibe_loop.budget import BudgetDecision
from vibe_loop.config import (
    AgentConfig,
    AgentRoutingRule,
    AgentResolutionError,
    BudgetConfig,
    CompletionConfig,
    OrchestrationConfig,
    ProjectBindingConfig,
    SpecDiagnosticsConfig,
    SupervisionConfig,
    SUPERVISION_DEFAULT_MAX_RESTARTS,
    TaskSourceConfig,
    VibeConfig,
    resolve_task_agent,
    shell_quote,
)
from vibe_loop.generated_profiles import RuntimeTaskSourceResolution
from vibe_loop.locks import (
    LockBusy,
    LockManager,
    LockOwnerMismatch,
    SettledOutcomeNotPersisted,
    TaskLock,
)
from vibe_loop.processes import ProcessNode, process_birth_identity, read_process_node
from vibe_loop.orchestration import (
    CandidateRecord,
    CandidateReanchorRetryExhausted,
    GateResult,
    GateRunSummary,
    IntegrationResult,
    ProvisionedWorkspace,
    ReviewConcurrencyBudget,
    ReviewControlFenceError,
    ReviewOutputMalformed,
    ReviewRouter,
    RunLifecycleStateMachine,
    RunStage,
    TaskSourceCompletionError,
    WorkspaceProvisionError,
    WorkspaceProvisioner,
)
from vibe_loop.runner import (
    CLI_WORKER_ADDENDUM,
    RUNTIME_OWNED_WORKER_ADDENDUM,
    SPEC_WORKER_CONTEXT_MAX_TOTAL_CHARS,
    ActivityEvent,
    AgentProviderLimitError,
    AgentRuntimeContext,
    PostReportActivityMonitor,
    SchedulerLockBusy,
    ExplicitTaskDispatchError,
    TaskActivationError,
    VibeRunner,
    active_lock_conflict_domains,
    bind_worker_workspace_env,
    build_batch_selection_prompt,
    build_run_context_payload,
    build_run_worker_prompt,
    build_selection_prompt,
    build_spec_worker_context,
    build_worker_prompt,
    claude_project_dir_name,
    command_specifies_resume,
    command_supports_session_capture,
    command_supports_session_resume,
    deterministic_task_batch,
    format_agent_command,
    implementer_session_from_records,
    reviewer_session_from_records,
    build_resume_continuation_prompt,
    classify_post_report_activity,
    classify_post_report_event,
    inject_claude_resume,
    inject_claude_session_id,
    inject_claude_implementer_tool_denial,
    inject_structured_usage_output,
    parse_agent_runtime_context_from_command,
    parse_agent_runtime_context_from_line,
    parse_selected_task_id,
    parse_selected_task_ids,
    parse_worker_session_id,
    RecoveryContext,
    resumable_prior_session_id,
    build_recovery_prompt_section,
    predicted_claude_transcript,
    provider_selection_is_flexible,
    resolve_claude_home,
    resolve_claude_transcript,
    resolve_codex_home,
    resolve_codex_rollout,
    run_streaming_command,
    terminate_verified_worker_process_group,
    terminate_worker_process_group,
    validate_analysis_prompt_delivery,
    validate_selected_task_batch,
    wait_with_reap_watchdog,
    worker_command_env,
    worker_report_persistence_epoch,
    worker_usage_provenance,
)
from vibe_loop.runs import (
    AttemptCircuitState,
    LOCK_FINALIZATION_FAILED_RECORD_TYPE,
    SETTLED_RUN_OUTCOMES,
    WORKER_REPORT_STATUSES,
    RunLifecycleEvent,
    RunResult,
    WorkerReport,
    settled_run_outcome,
)
from vibe_loop.skill_deployment import SkillDeploymentError
from vibe_loop.spec_diagnostics import SpecExecutionGateError
from vibe_loop.tasks import Task, run_json_command
from vibe_loop.workers import (
    ActiveRunState,
    StaleLock,
    clean_stale_locks,
    collect_stale_locks,
    git_dirty_snapshot,
    WorkspaceClaim,
)


class MutableTaskSource:
    def __init__(
        self,
        tasks: list[Task],
        *,
        reset_hook: bool = False,
        reset_error: Exception | None = None,
    ):
        self._tasks = tasks
        self._done: set[str] = set()
        self._lock = threading.Lock()
        # reset_hook mirrors an operator-configured command-backed reset: when
        # true, reset() records the call and reports it as invoked; otherwise
        # it reports no hook (like a file-based source). reset_error simulates a
        # failing hook so the runner's non-fatal handling can be exercised.
        self._reset_hook = reset_hook
        self._reset_error = reset_error
        self.reset_calls: list[str] = []
        self.reset_contexts: list[dict[str, str]] = []

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

    def reset(
        self,
        task_id: str,
        *,
        runtime_context: dict[str, str] | None = None,
    ) -> bool:
        with self._lock:
            self.reset_calls.append(task_id)
            self.reset_contexts.append(dict(runtime_context or {}))
        if self._reset_error is not None:
            raise self._reset_error
        return self._reset_hook

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
    def explicit_runner(self, repo: Path, tasks: list[Task]) -> VibeRunner:
        task_source = TaskSourceConfig(
            type="markdown-plan",
            runnable_statuses=("Next",),
            explicit_keys=frozenset({"type"}),
        )
        runner = VibeRunner(
            VibeConfig(
                repo=repo,
                agent=AgentConfig(command="worker"),
                task_source=task_source,
            )
        )
        runner._source = MutableTaskSource(tasks)
        runner._source_resolution = RuntimeTaskSourceResolution(
            task_source=task_source,
            origin="test",
        )
        return runner

    def test_run_task_id_dispatches_only_the_named_task_through_supervision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = [
                Task(task_id="TASK-01", title="First", status="Next"),
                Task(task_id="TASK-02", title="Second", status="Next"),
            ]
            runner = self.explicit_runner(Path(directory), tasks)
            expected = RunResult(
                run_id="run-2",
                task_id="TASK-02",
                classification="completed",
                exit_code=0,
                log_path=Path(directory) / "run.log",
                start_main="aaa",
                end_main="bbb",
            )

            with (
                patch.object(runner, "ensure_spec_execution_gate"),
                patch.object(runner, "require_worker_launch_config"),
                patch.object(
                    runner,
                    "run_task_with_supervision",
                    return_value=expected,
                ) as run_task,
            ):
                result = runner.run_task_id("TASK-02")

        self.assertIs(result, expected)
        run_task.assert_called_once_with(tasks[1])

    def test_run_task_id_rejects_unknown_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [Task(task_id="TASK-01", title="First", status="Next")],
            )

            with self.assertRaisesRegex(
                ExplicitTaskDispatchError,
                "unknown task 'TASK-99'",
            ):
                runner.run_task_id("TASK-99")

    def test_run_task_id_rejects_dependency_blocked_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [
                    Task(task_id="BLOCKER", title="Blocker", status="Next"),
                    Task(
                        task_id="TASK-02",
                        title="Blocked",
                        status="Next",
                        dependencies=("BLOCKER",),
                    ),
                ],
            )

            with self.assertRaisesRegex(
                ExplicitTaskDispatchError,
                "task 'TASK-02' is dependency-blocked by: BLOCKER",
            ):
                runner.run_task_id("TASK-02")

    def test_run_task_id_rejects_on_hold_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [Task(task_id="TASK-01", title="Held", status="on-hold")],
            )

            with self.assertRaisesRegex(
                ExplicitTaskDispatchError,
                "task 'TASK-01' is not runnable with status 'on-hold'",
            ):
                runner.run_task_id("TASK-01")

    def test_run_task_id_rejects_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [Task(task_id="TASK-01", title="Complete", status="Done")],
            )

            with self.assertRaisesRegex(
                ExplicitTaskDispatchError,
                "task 'TASK-01' is already complete with status 'Done'",
            ):
                runner.run_task_id("TASK-01")

    def test_run_task_id_rejects_existing_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [Task(task_id="TASK-01", title="Locked", status="Next")],
            )
            held_lock = runner.lock_manager.acquire("TASK-01", "live-run")
            try:
                with self.assertRaisesRegex(
                    ExplicitTaskDispatchError,
                    "task 'TASK-01' has an existing task lock",
                ):
                    runner.run_task_id("TASK-01")
            finally:
                runner.lock_manager.release(held_lock)

    def test_run_task_id_reports_lock_race_without_selecting_another_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.explicit_runner(
                Path(directory),
                [
                    Task(task_id="TASK-01", title="Requested", status="Next"),
                    Task(task_id="TASK-02", title="Other", status="Next"),
                ],
            )

            with (
                patch.object(runner, "ensure_spec_execution_gate"),
                patch.object(runner, "require_worker_launch_config"),
                patch.object(
                    runner,
                    "run_task_with_supervision",
                    side_effect=LockBusy(
                        Path(directory) / "TASK-01.lock",
                        {"task_id": "TASK-01"},
                    ),
                ) as run_task,
                self.assertRaisesRegex(
                    ExplicitTaskDispatchError,
                    "task 'TASK-01' has an existing task lock",
                ),
            ):
                runner.run_task_id("TASK-01")

        run_task.assert_called_once()

    def test_run_task_id_rejects_live_conflict_domain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = self.explicit_runner(
                repo,
                [
                    Task(
                        task_id="TASK-01",
                        title="Conflicting",
                        status="Next",
                        resources=("database",),
                        conflict_domains_known=True,
                    )
                ],
            )
            active = _active_run_state(
                task_id="ACTIVE-01",
                run_id="live-run",
                worker_pid=os.getpid(),
                host=socket.gethostname(),
                repo=repo,
                resources=("database",),
            )
            held_lock = runner.lock_manager.acquire(
                "ACTIVE-01",
                "live-run",
                metadata=active.to_lock_metadata(),
            )
            try:
                with self.assertRaisesRegex(
                    ExplicitTaskDispatchError,
                    "task 'TASK-01' is excluded by a conflict domain",
                ):
                    runner.run_task_id("TASK-01")
            finally:
                runner.lock_manager.release(held_lock)

    def test_run_task_id_uses_full_runnable_scope_for_conflict_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            requested = Task(
                task_id="TASK-REQ",
                title="Undeclared requested task",
                status="Next",
            )
            runner = self.explicit_runner(
                repo,
                [
                    requested,
                    Task(
                        task_id="TASK-OTHER",
                        title="Declared candidate",
                        status="Next",
                        resources=("database",),
                        conflict_domains_known=True,
                    ),
                ],
            )
            active = dataclasses.replace(
                _active_run_state(
                    task_id="ACTIVE-01",
                    run_id="live-run",
                    worker_pid=os.getpid(),
                    host=socket.gethostname(),
                    repo=repo,
                ),
                conflict_domains_known=False,
            )
            held_lock = runner.lock_manager.acquire(
                "ACTIVE-01",
                "live-run",
                metadata=active.to_lock_metadata(),
            )
            try:
                with (
                    patch.object(runner, "run_task_with_supervision") as run_task,
                    self.assertRaisesRegex(
                        ExplicitTaskDispatchError,
                        "task 'TASK-REQ' is excluded by a conflict domain",
                    ),
                ):
                    runner.run_task_id("TASK-REQ")
            finally:
                runner.lock_manager.release(held_lock)

        run_task.assert_not_called()

    def test_run_task_id_carries_runnable_conflict_policy_to_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requested = Task(
                task_id="TASK-REQ",
                title="Undeclared requested task",
                status="Next",
            )
            runner = self.explicit_runner(
                Path(directory),
                [
                    requested,
                    Task(
                        task_id="TASK-OTHER",
                        title="Declared candidate",
                        status="Next",
                        resources=("database",),
                        conflict_domains_known=True,
                    ),
                ],
            )
            expected = RunResult(
                run_id="run-requested",
                task_id="TASK-REQ",
                classification="completed",
                exit_code=0,
                log_path=Path(directory) / "run.log",
                start_main="aaa",
                end_main="bbb",
            )

            with (
                patch.object(runner, "ensure_spec_execution_gate"),
                patch.object(runner, "require_worker_launch_config"),
                patch.object(
                    runner,
                    "run_task_with_supervision",
                    return_value=expected,
                ) as run_task,
            ):
                result = runner.run_task_id("TASK-REQ")

        self.assertIs(result, expected)
        run_task.assert_called_once_with(
            requested,
            enforce_resource_conflicts=True,
        )

    def test_worker_workspace_environment_uses_canonical_claimed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = root / "worktree"
            symlink = root / "worktree-link"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "worktree", "add", "-b", "task/test", str(worktree)],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            )
            symlink.symlink_to(worktree, target_is_directory=True)
            relative_symlink = Path(os.path.relpath(symlink, Path.cwd()))
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            workspace = ProvisionedWorkspace(
                mode="created",
                branch="task/test",
                worktree=relative_symlink,
                base_commit=head,
                head_commit=head,
            )
            claim = WorkspaceClaim(
                task_id="test",
                run_id="run",
                branch="task/test",
                worktree=symlink,
                base_commit=head,
                head_commit=head,
                current_branch="task/test",
                dirty=False,
                dirty_summary=(),
            )
            environment = {"VIBE_LOOP_PRIMARY_REPO": str(repo)}

            bind_worker_workspace_env(
                environment,
                workspace=workspace,
                claim=claim,
            )

            self.assertEqual(environment["VIBE_LOOP_REPO"], str(worktree.resolve()))
            self.assertEqual(environment["VIBE_LOOP_WORKTREE"], str(worktree.resolve()))
            self.assertEqual(environment["VIBE_LOOP_BRANCH"], "task/test")
            self.assertNotIn("VIBE_LOOP_PRIMARY_REPO", environment)
            primary_common = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            task_common = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(
                (repo / primary_common).resolve(),
                (worktree / task_common).resolve(),
            )

    def test_worker_workspace_environment_rejects_claim_mismatch(self) -> None:
        workspace = ProvisionedWorkspace(
            mode="created",
            branch="task/test",
            worktree=Path.cwd(),
            base_commit="base",
            head_commit="head",
        )
        claim = WorkspaceClaim(
            task_id="test",
            run_id="run",
            branch="task/test",
            worktree=Path.cwd().parent,
            base_commit="base",
            head_commit="head",
            current_branch="task/test",
            dirty=False,
            dirty_summary=(),
        )
        environment = {"VIBE_LOOP_PRIMARY_REPO": "/primary"}

        with self.assertRaisesRegex(
            WorkspaceProvisionError,
            "persisted workspace claim",
        ):
            bind_worker_workspace_env(
                environment,
                workspace=workspace,
                claim=claim,
            )

        self.assertNotIn("VIBE_LOOP_REPO", environment)
        self.assertEqual(environment["VIBE_LOOP_PRIMARY_REPO"], "/primary")

    def test_activate_task_before_launch_rejects_empty_status(self) -> None:
        class EmptyStatusSource:
            def activate(self, *args: object, **kwargs: object) -> Task:
                return Task(task_id="TASK-01", title="Task", status="")

        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            runner._source = EmptyStatusSource()

            with self.assertRaisesRegex(TaskActivationError, "empty status"):
                runner.activate_task_before_launch(
                    Task(task_id="TASK-01", title="Task", status="Next"),
                    "run-1",
                    {},
                    continuation=False,
                )

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

    def test_worker_prompt_appends_repo_extension_for_both_dialects(self) -> None:
        task = Task(task_id="POLICY-01", title="Respect repo policy", status="Next")
        extension = (
            "Never merge to main.\nLeave the reviewed branch for the orchestrator."
        )
        config = VibeConfig(
            repo=Path("."),
            worker_prompt_extra=extension,
            orchestration=OrchestrationConfig(mode="worker-owned"),
        )

        for skill_prefix in ("$", "/"):
            with self.subTest(skill_prefix=skill_prefix):
                prompt = build_worker_prompt(skill_prefix, task, config)

                self.assertTrue(prompt.startswith(f"{skill_prefix}vibe-loop POLICY-01"))
                self.assertIn("## Repository Worker Prompt Extension", prompt)
                self.assertIn(
                    "OVERRIDE the generic vibe-loop CLI coordination protocol",
                    prompt,
                )
                self.assertTrue(prompt.endswith(extension))
                self.assertGreater(
                    prompt.index("## Repository Worker Prompt Extension"),
                    prompt.index("### Integration Locking"),
                )

    def test_initial_worker_prompt_requires_async_work_to_settle(self) -> None:
        task = Task(task_id="ASYNC-01", title="Finish async work", status="Next")

        for skill_prefix in ("$", "/"):
            with self.subTest(skill_prefix=skill_prefix):
                token = "initial-generation-7"
                with patch.dict(os.environ, {"VIBE_LOOP_FENCING_TOKEN": token}):
                    prompt = build_worker_prompt(
                        skill_prefix,
                        task,
                        VibeConfig(
                            repo=Path("."),
                            orchestration=OrchestrationConfig(mode="worker-owned"),
                        ),
                    )

                self.assertTrue(prompt.startswith(f"{skill_prefix}vibe-loop ASYNC-01"))
                self.assertIn("### Headless Completion", prompt)
                self.assertIn("Agent/Task/Workflow", prompt)
                self.assertIn("await or collect every result", prompt)
                self.assertIn("returning a progress summary", prompt)
                self.assertIn("VIBE_LOOP_WORKTREE", prompt)
                self.assertIn("VIBE_LOOP_BRANCH", prompt)
                self.assertIn("runtime provisioned", prompt)
                self.assertNotIn("worker claim-workspace", prompt)
                self.assertNotIn(token, prompt)
                self.assertLess(
                    prompt.index("### Headless Completion"),
                    prompt.index("### Worker Reports"),
                )

    def test_worker_prompt_extension_applies_after_profile_routing(self) -> None:
        extension = "Repository integration policy wins."
        config = VibeConfig(
            repo=Path("."),
            agent=AgentConfig(skill_ref_prefix="$"),
            agent_profiles={
                "claude": AgentConfig(
                    agent_kind="claude",
                    prompt_dialect="claude",
                    skill_ref_prefix="/",
                )
            },
            agent_routing=(
                AgentRoutingRule(
                    profile="claude",
                    match_task_id_regex="^CLAUDE-",
                ),
            ),
            worker_prompt_extra=extension,
        )

        for task_id, expected_prefix, expected_profile in (
            ("CODEX-01", "$", ""),
            ("CLAUDE-01", "/", "claude"),
        ):
            with self.subTest(task_id=task_id):
                task = Task(task_id=task_id, title="Routed task", status="Next")
                selection = resolve_task_agent(config, task)
                prompt = build_worker_prompt(
                    selection.config.require_skill_ref_prefix(),
                    task,
                    config,
                )

                self.assertEqual(selection.profile, expected_profile)
                self.assertTrue(prompt.startswith(f"{expected_prefix}vibe-loop"))
                self.assertTrue(prompt.endswith(extension))

    def test_worker_prompt_extension_is_last_for_recovery_prompts(self) -> None:
        task = Task(task_id="POLICY-03", title="Recovery policy", status="Next")
        extension = "Never integrate this branch."
        config = VibeConfig(repo=Path("."), worker_prompt_extra=extension)
        recovery = RecoveryContext(
            task_id=task.task_id,
            prior_run_id="run-1",
            prior_classification="unknown",
            branch="policy-03",
            worktree="/tmp/policy-03",
            head_commit="abc123",
            transcript_path="/tmp/transcript.jsonl",
            wrapper_log="/tmp/run-1.log",
            attempt=1,
            max_attempts=3,
            workspace_claimed=True,
            prior_session_id="session-1",
        )

        for skill_prefix in ("$", "/"):
            for resuming, branch_marker in (
                (False, "## Unknown-Run Recovery"),
                (True, "## Continue this run (resumed session)"),
            ):
                with self.subTest(skill_prefix=skill_prefix, resuming=resuming):
                    token = "recovery-generation-11"
                    with patch.dict(os.environ, {"VIBE_LOOP_FENCING_TOKEN": token}):
                        prompt = build_run_worker_prompt(
                            skill_prefix,
                            task,
                            config,
                            recovery=recovery,
                            resuming=resuming,
                        )

                    self.assertIn(branch_marker, prompt)
                    if not resuming:
                        self.assertTrue(
                            prompt.startswith(f"{skill_prefix}vibe-loop {task.task_id}")
                        )
                    self.assertIn("VIBE_LOOP_FENCING_TOKEN is a secret", prompt)
                    self.assertIn("CURRENT active task lock", prompt)
                    self.assertIn("VIBE_LOOP_WORKTREE", prompt)
                    self.assertIn("VIBE_LOOP_BRANCH", prompt)
                    self.assertNotIn("worker claim-workspace", prompt)
                    self.assertNotIn(token, prompt)
                    self.assertGreater(
                        prompt.index("## Repository Worker Prompt Extension"),
                        prompt.index(branch_marker),
                    )
                    self.assertTrue(prompt.endswith(extension))

    def test_worker_prompt_omits_repo_extension_when_unset(self) -> None:
        task = Task(task_id="POLICY-02", title="Default policy", status="Next")
        self.assertEqual(
            build_worker_prompt("$", task, None),
            f"$vibe-loop {task.task_id}{CLI_WORKER_ADDENDUM}",
        )
        self.assertEqual(
            build_worker_prompt("$", task, VibeConfig(repo=Path("."))),
            f"$vibe-loop {task.task_id}{RUNTIME_OWNED_WORKER_ADDENDUM}",
        )

    def test_runtime_owned_worker_prompt_uses_slim_addendum(self) -> None:
        task = Task(task_id="ORC-10", title="Runtime lifecycle", status="Next")
        config = VibeConfig(
            repo=Path("."),
            orchestration=OrchestrationConfig(mode="runtime-owned"),
        )

        prompt = build_worker_prompt("$", task, config)

        self.assertEqual(
            prompt,
            f"$vibe-loop {task.task_id}{RUNTIME_OWNED_WORKER_ADDENDUM}",
        )
        self.assertNotIn("Integration Locking", prompt)
        self.assertNotIn("Task Source Context", prompt)

    def test_worker_prompt_contains_token_rule_without_environment_value(self) -> None:
        task = Task(task_id="POLICY-04", title="Protect lock token", status="Next")
        token = "prompt-generation-9"

        with patch.dict(os.environ, {"VIBE_LOOP_FENCING_TOKEN": token}):
            prompt = build_worker_prompt("$", task, VibeConfig(repo=Path(".")))

        self.assertIn("VIBE_LOOP_FENCING_TOKEN is a secret", prompt)
        self.assertIn("Never print or echo its value", prompt)
        self.assertIn("command argument, tool payload, log, or summary", prompt)
        self.assertNotIn(token, prompt)

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
                ("Blocked", "blocked"),
                ("blocked", "blocked"),
                ("BLOCKED", "blocked"),
                ("Gated", "blocked"),
                ("gated", "blocked"),
                ("GATED", "blocked"),
                ("Low", "blocked"),
                ("low", "blocked"),
                ("LOW", "blocked"),
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

    def test_classify_timed_out_wins_over_report_and_exit_code(self) -> None:
        # A wall-clock kill leaves inconclusive output and a possibly stale
        # report; the run must classify as timed_out regardless of exit_code or
        # any partial worker report so dispatch returns the task to runnable.
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))

            result = runner.classify(
                "TASK-01",
                0,
                "aaa",
                "aaa",
                "",
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                ),
                timed_out=True,
            )
            self.assertEqual(result.status, "timed_out")
            self.assertEqual(result.source, "worker_timeout")

    def test_classify_not_timed_out_keeps_normal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            result = runner.classify(
                "TASK-01", 7, "aaa", "aaa", "", None, timed_out=False
            )
            self.assertEqual(result.status, "failed")

    def test_enforced_post_report_teardown_keeps_accepted_report_status(
        self,
    ) -> None:
        # A worker whose process group was stopped for post-report activity
        # exits on a signal (nonzero exit code) but was not timed out. The
        # accepted terminal report must stay authoritative so the run finalizes
        # completed and is never turned into a retry by the teardown.
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            result = runner.classify(
                "TASK-01",
                -15,
                "aaa",
                "aaa",
                "",
                WorkerReport(
                    run_id="run-1",
                    task_id="TASK-01",
                    status="completed",
                ),
                timed_out=False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source, "worker_report")

    def test_classify_falls_through_to_unknown_on_probe_failure(self) -> None:
        # A command-backed probe can fail to shell out, exit nonzero, or hang
        # past its timeout. None confirm the run's outcome, so classification
        # must degrade to the safe "unknown" fallback (routed to unknown-run
        # recovery) rather than propagate — run_next only catches LockBusy.
        class TimingOutSource(MutableTaskSource):
            def probe(self, task_id: str) -> Task | None:
                raise subprocess.TimeoutExpired(cmd="probe", timeout=1.0)

        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            runner._source = TimingOutSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            result = runner.classify("TASK-01", 0, "aaa", "aaa", "", None)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.source, "task_probe_error")

    def test_classify_falls_through_to_unknown_on_malformed_probe_json(self) -> None:
        class MalformedJsonSource(MutableTaskSource):
            def probe(self, task_id: str) -> Task | None:
                raise ValueError("malformed task-source JSON")

        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            runner._source = MalformedJsonSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            result = runner.classify("TASK-01", 0, "aaa", "aaa", "", None)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.source, "task_probe_error")

    def test_clean_claude_exit_without_report_has_specific_failure_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            runner._source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Active", order=1)]
            )

            result = runner.classify(
                "TASK-01",
                0,
                "aaa",
                "aaa",
                "",
                None,
                agent_kind="claude",
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.source, "worker_report_missing")
        self.assertIn("terminal worker report", result.detail)

    def test_clean_codex_exit_without_report_keeps_legacy_unknown_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            runner._source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Active", order=1)]
            )

            result = runner.classify(
                "TASK-01",
                0,
                "aaa",
                "aaa",
                "",
                None,
                agent_kind="codex",
            )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.source, "fallback")

    def test_classify_detects_provider_limit_before_failed(self) -> None:
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
        self.assertEqual(result.status, "provider_limit")
        self.assertEqual(result.source, "provider_limit")
        self.assertEqual(result.detail, "resets 1am (UTC)")

    def test_classify_successful_run_ignores_quoted_limit_phrase(self) -> None:
        # A completed run (exit 0, no worker report) whose captured output merely
        # quotes a limit phrase must proceed to the normal completion path, not
        # be recorded as provider_limit and pause the whole supervisor.
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(VibeConfig(repo=Path(directory)))
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            source.mark_done("TASK-01")
            runner._source = source
            result = runner.classify(
                "TASK-01",
                0,
                "aaa",
                "aaa",
                "",
                None,
                output_tail="You've hit your session limit · resets 1am (UTC)",
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.source, "task_probe")

    def test_classify_worker_report_wins_over_provider_limit(self) -> None:
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

    def test_classify_provider_limit_disabled_falls_back_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(
                VibeConfig(
                    repo=Path(directory),
                    supervision=SupervisionConfig(provider_limit_detection=False),
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

    def test_classify_honors_custom_provider_limit_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = VibeRunner(
                VibeConfig(
                    repo=Path(directory),
                    supervision=SupervisionConfig(
                        provider_limit_patterns=("provider wall reached",)
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
        self.assertEqual(custom_phrase.status, "provider_limit")

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
        self.assertIn("Agent/Task/Workflow", prompt)
        self.assertIn("CURRENT active task lock", prompt)
        self.assertIn("VIBE_LOOP_WORKTREE", prompt)
        self.assertIn("VIBE_LOOP_BRANCH", prompt)
        self.assertNotIn("worker claim-workspace", prompt)
        # Must NOT be the from-scratch recovery brief.
        self.assertNotIn("Investigate what the previous session did", prompt)

    def test_resume_prompt_without_claim_describes_new_workspace(self) -> None:
        recovery = RecoveryContext(
            task_id="TASK-01",
            prior_run_id="run-1",
            prior_classification="unknown",
            branch="",
            worktree="",
            head_commit="",
            transcript_path="/t.jsonl",
            wrapper_log="/w.log",
            attempt=1,
            max_attempts=3,
            workspace_claimed=False,
            prior_session_id="sid-123",
        )

        prompt = build_resume_continuation_prompt(recovery)

        self.assertIn("created and claimed a new dedicated workspace", prompt)
        self.assertIn("prior uncommitted filesystem changes are not", prompt)
        self.assertNotIn("adopted the preserved workspace", prompt)

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

    def test_unknown_explicit_agent_fails_task_without_aborting_batch(self) -> None:
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    runner = VibeRunner(
                        VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
                    )
                    source = MutableTaskSource(
                        [
                            Task(
                                task_id="TASK-BAD",
                                title="Bad route",
                                status="Next",
                                agent="typo",
                                order=1,
                            ),
                            Task(
                                task_id="TASK-GOOD",
                                title="Good route",
                                status="Next",
                                order=2,
                            ),
                        ]
                    )
                    runner._source = source
                    original_run_task = runner.run_task

                    def run_task(task: Task) -> RunResult:
                        if task.task_id == "TASK-BAD":
                            return original_run_task(task)
                        source.mark_done(task.task_id)
                        return RunResult(
                            run_id="run-good",
                            task_id=task.task_id,
                            classification="completed",
                            exit_code=0,
                            log_path=repo / "good.log",
                            start_main="aaa",
                            end_main="aaa",
                        )

                    runner.run_task = run_task
                    with patch("vibe_loop.runner.git_rev_parse", return_value="aaa"):
                        results = runner.run_until_done(jobs=jobs)

                    by_task = {result.task_id: result for result in results}
                    failed = by_task["TASK-BAD"]
                    self.assertTrue(failed.log_path.is_file())
                    failed_log = failed.log_path.read_text(encoding="utf-8")

                self.assertEqual(set(by_task), {"TASK-BAD", "TASK-GOOD"})
                self.assertEqual(by_task["TASK-GOOD"].classification, "completed")
                self.assertEqual(failed.classification, "failed")
                self.assertEqual(failed.exit_code, 1)
                self.assertEqual(
                    failed.classification_source,
                    "task_agent_contract",
                )
                self.assertIn("agent profile 'typo'", failed.message)
                self.assertIn("agent resolution failed", failed_log)

    def test_contract_failure_records_result_and_opens_attempt_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent=AgentConfig(command="worker {prompt}"),
                    supervision=SupervisionConfig(
                        cross_run_attempt_threshold=2,
                    ),
                )
            )
            task = Task(task_id="TASK-CONTRACT", title="Contract", status="Next")
            runner._source = MutableTaskSource([task])

            with (
                patch(
                    "vibe_loop.runner.verify_worker_skill_deployments",
                    return_value=(),
                ),
                patch("vibe_loop.runner.git_rev_parse", return_value="aaa"),
            ):
                first = runner.run_task_with_supervision(task)
                second = runner.run_task_with_supervision(task)
                with self.assertRaises(runner_module.AttemptCircuitOpen):
                    runner.run_task_with_supervision(task)

            records = runner.run_store.read_records()
            results = [
                record
                for record in records
                if record.get("record_type") == "run_result"
            ]
            circuit = runner.run_store.attempt_circuit_states(threshold=2)
            failed_inputs = runner_module.attempt_circuit_inputs(
                task,
                runner.config,
                base="aaa",
                candidate="aaa",
                agent=runner.config.agent,
                profile="",
            )
            fixed_config = dataclasses.replace(
                runner.config,
                orchestration=OrchestrationConfig(
                    mode="worker-owned",
                    explicit_keys=frozenset({"mode"}),
                ),
            )
            fixed_inputs = runner_module.attempt_circuit_inputs(
                task,
                fixed_config,
                base="aaa",
                candidate="aaa",
                agent=fixed_config.agent,
                profile="",
            )

        self.assertEqual(
            [first.classification, second.classification],
            ["failed", "failed"],
        )
        self.assertEqual(
            first.classification_source,
            "config_contract_reviewer_profile_missing",
        )
        self.assertIn("requires an explicit fallback", first.message)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {record["run_id"] for record in results},
            {first.run_id, second.run_id},
        )
        self.assertEqual(len(circuit), 1)
        self.assertTrue(circuit[0].open)
        self.assertEqual(circuit[0].attempt_count, 2)
        self.assertEqual(
            circuit[0].blocker_class,
            "failed:config_contract_reviewer_profile_missing",
        )
        self.assertNotEqual(
            failed_inputs.configuration_revision,
            fixed_inputs.configuration_revision,
        )
        self.assertFalse(runner.lock_manager.is_locked(task.task_id))

    def test_attempt_circuit_ignores_source_status_and_reason_text(self) -> None:
        config = VibeConfig(
            repo=Path("/repo"),
            agent=AgentConfig(command="worker {prompt}"),
        )
        task = Task(
            task_id="TASK-RETRY",
            title="Retry",
            status="ready",
            status_reason="retry 1: worker timed out at 10:00",
        )
        inputs = runner_module.attempt_circuit_inputs(
            task,
            config,
            base="aaa",
            candidate="aaa",
            agent=config.agent,
            profile="",
        )
        updated_inputs = runner_module.attempt_circuit_inputs(
            dataclasses.replace(
                task,
                status="active",
                status_reason="retry 2: worker timed out at 11:00",
            ),
            config,
            base="aaa",
            candidate="aaa",
            agent=config.agent,
            profile="",
        )

        self.assertEqual(inputs.task_revision, updated_inputs.task_revision)

    def test_reviewer_agent_override_fails_task_without_aborting_batch(self) -> None:
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    worker = AgentConfig(
                        command="worker {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    reviewer = AgentConfig(
                        command="reviewer {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    runner = VibeRunner(
                        VibeConfig(
                            repo=repo,
                            agent=worker,
                            agent_profiles={
                                "worker": worker,
                                "review": reviewer,
                            },
                            task_source=TaskSourceConfig(
                                type="command",
                                list_command="list-tasks",
                                complete_command="complete-task",
                            ),
                            orchestration=OrchestrationConfig(
                                mode="runtime-owned",
                                reviewer_profile="review",
                                task_provenance_mode="adapter",
                                explicit_keys=frozenset(
                                    {
                                        "mode",
                                        "reviewer_profile",
                                        "task_provenance_mode",
                                    }
                                ),
                            ),
                        )
                    )
                    source = MutableTaskSource(
                        [
                            Task(
                                task_id="TASK-BAD",
                                title="Bad route",
                                status="Next",
                                agent="review",
                                order=1,
                            ),
                            Task(
                                task_id="TASK-SLOW",
                                title="Slow valid route",
                                status="Next",
                                agent="worker",
                                order=2,
                            ),
                            Task(
                                task_id="TASK-LATER",
                                title="Later valid route",
                                status="Next",
                                agent="worker",
                                order=3,
                            ),
                        ]
                    )
                    runner._source = source
                    original_run_task = runner.run_task
                    bad_finished = threading.Event()

                    def run_task(task: Task) -> RunResult:
                        if task.task_id == "TASK-BAD":
                            try:
                                return original_run_task(task)
                            finally:
                                bad_finished.set()
                        if task.task_id == "TASK-SLOW":
                            self.assertTrue(bad_finished.wait(timeout=1))
                            time.sleep(0.05)
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
                    with (
                        patch("vibe_loop.runner.git_rev_parse", return_value="aaa"),
                        patch(
                            "vibe_loop.runner.verify_worker_skill_deployments",
                            return_value=(),
                        ),
                    ):
                        results = runner.run_until_done(jobs=jobs)

                by_task = {result.task_id: result for result in results}
                self.assertEqual(
                    set(by_task),
                    {"TASK-BAD", "TASK-SLOW", "TASK-LATER"},
                )
                self.assertEqual(by_task["TASK-SLOW"].classification, "completed")
                self.assertEqual(by_task["TASK-LATER"].classification, "completed")
                self.assertEqual(by_task["TASK-BAD"].classification, "failed")
                self.assertEqual(
                    by_task["TASK-BAD"].classification_source,
                    "task_agent_contract",
                )
                self.assertIn(
                    "same profile for implementation and review",
                    by_task["TASK-BAD"].message,
                )

    def test_invalid_explicit_agent_route_does_not_abort_batch(self) -> None:
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    worker = AgentConfig(
                        command="worker {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    broken = AgentConfig(
                        command="worker --model {model} {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    runner = VibeRunner(
                        VibeConfig(
                            repo=repo,
                            agent=worker,
                            agent_profiles={"worker": worker, "broken": broken},
                        )
                    )
                    source = MutableTaskSource(
                        [
                            Task(
                                task_id="TASK-BAD",
                                title="Bad route",
                                status="Next",
                                agent="broken",
                                order=1,
                            ),
                            Task(
                                task_id="TASK-SLOW",
                                title="Slow valid route",
                                status="Next",
                                agent="worker",
                                order=2,
                            ),
                            Task(
                                task_id="TASK-LATER",
                                title="Later valid route",
                                status="Next",
                                agent="worker",
                                order=3,
                            ),
                        ]
                    )
                    runner._source = source
                    original_run_task = runner.run_task
                    bad_finished = threading.Event()

                    def run_task(task: Task) -> RunResult:
                        if task.task_id == "TASK-BAD":
                            try:
                                return original_run_task(task)
                            finally:
                                bad_finished.set()
                        if task.task_id == "TASK-SLOW":
                            self.assertTrue(bad_finished.wait(timeout=1))
                            time.sleep(0.05)
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
                    with (
                        patch("vibe_loop.runner.git_rev_parse", return_value="aaa"),
                        patch(
                            "vibe_loop.runner.verify_worker_skill_deployments",
                            return_value=(),
                        ),
                    ):
                        results = runner.run_until_done(jobs=jobs)

                by_task = {result.task_id: result for result in results}
                self.assertEqual(
                    set(by_task),
                    {"TASK-BAD", "TASK-SLOW", "TASK-LATER"},
                )
                self.assertEqual(
                    by_task["TASK-BAD"].classification_source,
                    "task_agent_contract",
                )
                self.assertIn(
                    "references {model}, but no model is resolved",
                    by_task["TASK-BAD"].message,
                )

    def test_task_agent_refusal_does_not_clear_parallel_provider_limit_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-WALL",
                        title="Wall",
                        status="Next",
                        order=1,
                    ),
                    Task(
                        task_id="TASK-BAD",
                        title="Bad route",
                        status="Next",
                        agent="typo",
                        order=2,
                    ),
                    Task(
                        task_id="TASK-LATER",
                        title="Later",
                        status="Next",
                        order=3,
                    ),
                ]
            )
            runner._source = source
            dispatched: list[str] = []

            def run_task(task: Task) -> RunResult:
                dispatched.append(task.task_id)
                if task.task_id == "TASK-BAD":
                    time.sleep(0.05)
                    return RunResult(
                        run_id="run-bad",
                        task_id=task.task_id,
                        classification="failed",
                        classification_source="task_agent_contract",
                        exit_code=1,
                        log_path=repo / "bad.log",
                        start_main="aaa",
                        end_main="aaa",
                    )
                return RunResult(
                    run_id="run-wall",
                    task_id=task.task_id,
                    classification="provider_limit",
                    exit_code=1,
                    log_path=repo / "wall.log",
                    start_main="aaa",
                    end_main="aaa",
                )

            runner.run_task = run_task
            results = runner.run_until_done(jobs=2, max_slices=3)

        self.assertEqual(dispatched, ["TASK-WALL", "TASK-BAD"])
        self.assertEqual(
            {result.task_id for result in results},
            {"TASK-WALL", "TASK-BAD"},
        )

    def test_skill_verification_blocks_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            task = Task(task_id="TASK-DRIFT", title="Drift", status="Next")

            def reject_task(_task: Task) -> RunResult:
                raise SkillDeploymentError(
                    "installed skills failed provenance verification",
                    diagnostics=(
                        "/tmp/skills/example/SKILL.md: runtime-edited: hash changed",
                    ),
                )

            runner.run_task = reject_task
            with patch("vibe_loop.runner.git_rev_parse", return_value="aaa"):
                result = runner.run_task_with_supervision(task)
            log_text = result.log_path.read_text(encoding="utf-8")

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.classification_source, "skill_verification")
        self.assertIn("runtime-edited", result.message)
        self.assertIn("worker skill preflight blocked", log_text)

    def test_skill_verification_blocks_unknown_run_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            task = Task(task_id="TASK-DRIFT", title="Drift", status="Next")
            runner._source = MutableTaskSource([task])
            recovery = RecoveryContext(
                task_id=task.task_id,
                prior_run_id="prior-run",
                prior_classification="unknown",
                branch="",
                worktree="",
                head_commit="",
                transcript_path="",
                wrapper_log="",
                attempt=1,
                max_attempts=2,
                workspace_claimed=False,
            )

            def reject_task(
                _task: Task,
                *,
                recovery: RecoveryContext | None = None,
            ) -> RunResult:
                self.assertIsNotNone(recovery)
                raise SkillDeploymentError(
                    "installed skills failed provenance verification",
                    diagnostics=("/tmp/skills/example/SKILL.md: branch-sourced",),
                )

            runner.run_task = reject_task
            with patch("vibe_loop.runner.git_rev_parse", return_value="aaa"):
                result = runner.resume_pending_recovery(recovery)

            self.assertIsNotNone(result)
            assert result is not None
            log_text = result.log_path.read_text(encoding="utf-8")

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.classification_source, "skill_verification")
        self.assertIn("branch-sourced", result.message)
        self.assertIn("worker skill preflight blocked", log_text)

    def test_task_agent_recovery_refusal_does_not_abort_batch(self) -> None:
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    worker = AgentConfig(
                        command="worker {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    broken = AgentConfig(
                        command="worker --model {model} {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                    runner = VibeRunner(
                        VibeConfig(
                            repo=repo,
                            agent=worker,
                            agent_profiles={"worker": worker, "broken": broken},
                        )
                    )
                    bad = Task(
                        task_id="TASK-BAD",
                        title="Bad recovery route",
                        status="Next",
                        agent="broken",
                        order=1,
                    )
                    good = Task(
                        task_id="TASK-GOOD",
                        title="Good route",
                        status="Next",
                        order=2,
                    )
                    source = MutableTaskSource([bad, good])
                    runner._source = source
                    recovery = RecoveryContext(
                        task_id=bad.task_id,
                        prior_run_id="prior-run",
                        prior_classification="unknown",
                        branch="",
                        worktree="",
                        head_commit="",
                        transcript_path="",
                        wrapper_log="",
                        attempt=1,
                        max_attempts=2,
                        workspace_claimed=False,
                    )
                    runner.pending_recovery_contexts = lambda: [recovery]
                    original_run_task = runner.run_task

                    def run_task(
                        task: Task,
                        *,
                        recovery: RecoveryContext | None = None,
                    ) -> RunResult:
                        if task.task_id == bad.task_id:
                            self.assertIsNotNone(recovery)
                            return original_run_task(task, recovery=recovery)
                        source.mark_done(task.task_id)
                        return RunResult(
                            run_id="run-good",
                            task_id=task.task_id,
                            classification="completed",
                            exit_code=0,
                            log_path=repo / "good.log",
                            start_main="aaa",
                            end_main="aaa",
                        )

                    runner.run_task = run_task
                    with (
                        patch("vibe_loop.runner.git_rev_parse", return_value="aaa"),
                        patch(
                            "vibe_loop.runner.verify_worker_skill_deployments",
                            return_value=(),
                        ),
                    ):
                        results = runner.run_until_done(jobs=jobs)

                by_task = {result.task_id: result for result in results}
                self.assertEqual(set(by_task), {"TASK-BAD", "TASK-GOOD"})
                self.assertEqual(
                    by_task["TASK-BAD"].classification_source,
                    "task_agent_contract",
                )
                self.assertIn(
                    "references {model}, but no model is resolved",
                    by_task["TASK-BAD"].message,
                )
                self.assertEqual(by_task["TASK-GOOD"].classification, "completed")

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

    def test_task_lock_acquire_preserves_snapshot_conflict_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            task = Task(
                task_id="TASK-01",
                title="Undeclared task",
                status="Next",
            )
            active_state = ActiveRunState.new(
                task_id=task.task_id,
                run_id="run-task",
                log_path=repo / "run.log",
                base_main="aaa",
                command="worker",
            )
            live = dataclasses.replace(
                _active_run_state(
                    task_id="ACTIVE-01",
                    run_id="external-run",
                    worker_pid=os.getpid(),
                    host=socket.gethostname(),
                    repo=repo,
                ),
                conflict_domains_known=False,
            )
            held_lock = runner.lock_manager.acquire(
                "ACTIVE-01",
                "external-run",
                metadata=live.to_lock_metadata(),
            )
            try:
                with self.assertRaises(LockBusy) as busy:
                    runner.acquire_scheduled_task_lock(
                        task,
                        "run-task",
                        active_state,
                        enforce_resource_conflicts=True,
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
            self.assertEqual(
                result.usage.unavailable_reason,
                "configured_command_cannot_report_usage",
            )
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

    def test_streaming_command_redacts_active_token_for_codex_and_claude(
        self,
    ) -> None:
        token = "stream-generation-3"
        substring = "stream-generation-"
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                with tempfile.TemporaryDirectory() as directory:
                    script = Path(directory) / "cmd.py"
                    script.write_text(
                        "import json\n"
                        "import os\n"
                        "import sys\n"
                        "token = os.environ['VIBE_LOOP_FENCING_TOKEN']\n"
                        "print(token)\n"
                        "print(json.dumps({'type': 'item.completed', "
                        "'item': {'output': token}, "
                        "'substring': token[:-1]}))\n"
                        "print(f'stderr token={token}', file=sys.stderr)\n",
                        encoding="utf-8",
                    )
                    log_path = Path(directory) / "run.log"
                    stderr = StringIO()
                    environment = os.environ.copy()
                    environment["VIBE_LOOP_FENCING_TOKEN"] = token
                    with log_path.open("w", encoding="utf-8") as log:
                        with redirect_stderr(stderr):
                            result = run_streaming_command(
                                f"{sys.executable} cmd.py",
                                Path(directory),
                                log,
                                env=environment,
                                forward_stderr=True,
                                provider=provider,
                            )

                    log_text = log_path.read_text(encoding="utf-8")
                    rendered = log_text + stderr.getvalue()
                    structured = next(
                        json.loads(line)
                        for line in log_text.splitlines()
                        if line.startswith("{")
                    )

                self.assertEqual(result.exit_code, 0)
                self.assertNotIn(token, rendered)
                self.assertIn("<redacted>", rendered)
                self.assertEqual(structured["item"]["output"], "<redacted>")
                self.assertEqual(structured["substring"], substring)

    def test_streaming_command_preserves_numeric_fields_for_short_token(self) -> None:
        token = "1"
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                with tempfile.TemporaryDirectory() as directory:
                    script = Path(directory) / "cmd.py"
                    script.write_text(
                        "import json\n"
                        "import os\n"
                        "import sys\n"
                        "token = os.environ['VIBE_LOOP_FENCING_TOKEN']\n"
                        "print(token)\n"
                        "print(token, file=sys.stderr)\n"
                        "print(json.dumps({'fencing_token': int(token), "
                        "'output': token, 'count': 1, 'task_id': 'TASK-1'}))\n",
                        encoding="utf-8",
                    )
                    log_path = Path(directory) / "run.log"
                    environment = os.environ.copy()
                    environment["VIBE_LOOP_FENCING_TOKEN"] = token
                    stderr = StringIO()
                    with log_path.open("w", encoding="utf-8") as log:
                        with redirect_stderr(stderr):
                            result = run_streaming_command(
                                f"{sys.executable} cmd.py",
                                Path(directory),
                                log,
                                env=environment,
                                forward_stderr=True,
                                provider=provider,
                            )
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                    payload = next(
                        json.loads(line) for line in lines if line.startswith("{")
                    )

                self.assertEqual(result.exit_code, 0)
                self.assertEqual(
                    [line for line in lines if not line.startswith("{")],
                    ["<redacted>", "<redacted>"],
                )
                self.assertNotIn("\n1\n", f"\n{stderr.getvalue()}")
                self.assertEqual(payload["fencing_token"], "<redacted>")
                self.assertEqual(payload["output"], "<redacted>")
                self.assertEqual(payload["count"], 1)
                self.assertEqual(payload["task_id"], "TASK-1")

    def test_streaming_command_captures_structured_stdout_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'type': 'thread.started', "
                "'thread_id': 'native-stdout-123'}))\n",
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
            self.assertEqual(
                result.session_id_source,
                "native:stdout:json.thread_id",
            )
            self.assertIn("native-stdout-123", stderr.getvalue())
            self.assertIn(
                "native-stdout-123",
                log_path.read_text(encoding="utf-8"),
            )

    def test_streaming_command_preserves_first_line_session_id_compatibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\n"
                "print('session id: first-line-session', file=sys.stderr)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertEqual(result.session_id, "first-line-session")
        self.assertEqual(result.session_id_source, "native:stderr")

    def test_first_line_session_compatibility_is_scoped_per_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import sys\n"
                "import time\n"
                "print('starting worker', flush=True)\n"
                "time.sleep(0.05)\n"
                "print('session id: stderr-startup-session', "
                "file=sys.stderr, flush=True)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertEqual(result.session_id, "stderr-startup-session")
        self.assertEqual(result.session_id_source, "native:stderr")

    def test_streaming_command_captures_ansi_codex_startup_frame_only(self) -> None:
        session_id = "019c0104-6f6b-7cd1-ae1f-a7bdc01f24f9"
        frame = [
            "\x1b[1mOpenAI Codex\x1b[0m",
            "\x1b[2m--------\x1b[0m",
            "\x1b[2mmodel:\x1b[0m gpt-5.6-sol",
            "\x1b[2mprovider:\x1b[0m openai",
            "\x1b[2mreasoning effort:\x1b[0m high",
            f"\x1b[2msession id:\x1b[0m {session_id}",
            "\x1b[2m--------\x1b[0m",
            "user",
            "str session_id abc-123 run_id optional_string",
            "model: gpt-9.9",
            "provider: value",
            "reasoning effort: low",
            "session id: later-session",
        ]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                f"lines = {frame!r}\nfor line in lines:\n    print(line)\n",
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
        self.assertEqual(result.session_id, session_id)
        self.assertEqual(
            result.session_id_source,
            "native:stdout:startup_frame.session_id",
        )
        self.assertEqual(result.runtime_context.model_provider, "openai")
        self.assertEqual(
            result.runtime_context.model_provider_source,
            "native:stdout:startup_frame.provider",
        )
        self.assertEqual(result.runtime_context.model_id, "gpt-5.6-sol")
        self.assertEqual(
            result.runtime_context.model_id_source,
            "native:stdout:startup_frame.model",
        )
        self.assertEqual(result.runtime_context.reasoning_effort, "high")
        self.assertEqual(
            result.runtime_context.reasoning_effort_source,
            "native:stdout:startup_frame.reasoning_effort",
        )

    def test_codex_name_in_prose_does_not_open_startup_frame(self) -> None:
        lines = [
            "Reading task: migrate the launcher to OpenAI Codex v2",
            "user",
            "model: attacker-model",
            "provider: anthropic",
            "reasoning effort: minimal",
            "session id: attacker-session",
        ]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                f"lines = {lines!r}\nfor line in lines:\n    print(line)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertIsNone(result.session_id)
        self.assertTrue(result.runtime_context.empty)

    def test_startup_session_closes_frame_without_trailing_separator(self) -> None:
        lines = [
            "OpenAI Codex v0.9",
            "model: gpt-5.6-sol",
            "provider: openai",
            "reasoning effort: high",
            "session id: real-session-1",
            "",
            "thinking about the plan",
            "model: gpt-3.5",
            "provider: anthropic",
        ]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                f"lines = {lines!r}\nfor line in lines:\n    print(line)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertEqual(result.session_id, "real-session-1")
        self.assertEqual(result.runtime_context.model_id, "gpt-5.6-sol")
        self.assertEqual(result.runtime_context.model_provider, "openai")
        self.assertEqual(result.runtime_context.reasoning_effort, "high")

    def test_boxed_startup_footer_closes_frame(self) -> None:
        lines = [
            "╭──────────────────────────╮",
            "│ OpenAI Codex v0.9        │",
            "│ model: gpt-5.6-sol       │",
            "│ provider: openai         │",
            "╰──────────────────────────╯",
            "model: gpt-3.5",
            "provider: anthropic",
        ]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                f"lines = {lines!r}\nfor line in lines:\n    print(line)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertEqual(result.runtime_context.model_id, "gpt-5.6-sol")
        self.assertEqual(result.runtime_context.model_provider, "openai")

    def test_streaming_command_does_not_capture_session_id_from_later_prose(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "print('worker output')\nprint('session id: abc-123')\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                )

        self.assertIsNone(result.session_id)
        self.assertIsNone(result.session_id_source)

    def test_streaming_command_keeps_rate_limit_event_before_immediate_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            event = {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed",
                    "rateLimitType": "five_hour",
                    "resetsAt": 1784642400,
                    "overageStatus": "rejected",
                    "overageDisabledReason": "org_level_disabled",
                },
            }
            script.write_text(
                f"print({json.dumps(json.dumps(event))})\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    Path(directory),
                    log,
                    provider="anthropic",
                )

        self.assertEqual(result.exit_code, 0)
        stats = result.usage.to_stats(phase="implementation")
        self.assertFalse(stats["quota_evidence_available"])
        self.assertEqual(
            stats["account_wall_observations"][0]["window"],
            "five_hour",
        )
        self.assertEqual(
            stats["account_wall_observations"][0]["status"],
            "allowed",
        )

    def test_streaming_command_captures_startup_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'type': 'session.created', "
                "'model': {'provider': 'openai', "
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
                "print(json.dumps({'type': 'session.created', "
                "'model': {'id': 'gpt-5.5'}, "
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

    def test_command_context_accepts_provider_neutral_effort_flag(self) -> None:
        context = parse_agent_runtime_context_from_command(
            "claude -p --model opus --effort medium"
        )

        self.assertEqual(context.model_provider, "anthropic")
        self.assertEqual(context.model_id, "")
        self.assertEqual(context.reasoning_effort, "medium")
        self.assertEqual(context.reasoning_effort_source, "command_arg:--effort")

    def test_text_line_cannot_establish_route_provenance(self) -> None:
        bare = parse_agent_runtime_context_from_line("effort: high", "stdout")
        legacy = parse_agent_runtime_context_from_line(
            "reasoning_effort: high", "stdout"
        )

        self.assertTrue(bare.empty)
        self.assertTrue(legacy.empty)

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

    @unittest.skipUnless(
        sys.platform == "linux", "launch publication barrier requires Linux"
    )
    def test_streaming_command_waits_for_pid_publication_before_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker_path = root / "worker-ran"
            script = root / "cmd.py"
            script.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker_path)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            log_path = root / "run.log"

            def publish_pid(worker_pid: int) -> None:
                self.assertGreater(worker_pid, 0)
                self.assertFalse(marker_path.exists())
                time.sleep(0.2)
                self.assertFalse(marker_path.exists())

            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} cmd.py",
                    root,
                    log,
                    on_start=publish_pid,
                )

            self.assertEqual(result.exit_code, 0)
            self.assertTrue(marker_path.exists())

    @unittest.skipUnless(
        sys.platform == "linux", "launch publication barrier requires Linux"
    )
    def test_worker_guard_blocks_shell_until_pid_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            marker_path = root / "worker-ran"
            worker_code = (
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(0.5)\n"
                f"Path({str(marker_path)!r}).write_text('ran', encoding='utf-8')\n"
            )
            parent_code = (
                "import os\n"
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"worker_code = {worker_code!r}\n"
                "gate_read, gate_write = os.pipe()\n"
                "child = subprocess.Popen([\n"
                "    sys.executable, '-m', 'vibe_loop.worker_guard',\n"
                "    str(os.getpid()), str(gate_read), 'shell', '--',\n"
                "    f'{sys.executable} -c {repr(worker_code)}',\n"
                "], pass_fds=(gate_read,))\n"
                "os.close(gate_read)\n"
                f"Path({str(child_pid_path)!r}).write_text("
                "str(child.pid), encoding='utf-8')\n"
            )

            subprocess.run(
                [sys.executable, "-c", parent_code],
                cwd=root,
                check=True,
            )
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                node = read_process_node(child_pid)
                if node is None or node.state == "Z":
                    break
                time.sleep(0.01)

            self.assertFalse(marker_path.exists())
            node = read_process_node(child_pid)
            self.assertTrue(node is None or node.state == "Z", node)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "detached process groups are POSIX-only"
    )
    def test_streaming_command_reaps_group_when_start_recording_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "hang.py"
            child_path = Path(directory) / "child.pid"
            child_ready_path = Path(directory) / "child.ready"
            child_code = (
                "import signal\n"
                "import time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"Path({str(child_ready_path)!r}).write_text("
                "'ready', encoding='utf-8')\n"
                "time.sleep(30)\n"
            )
            script.write_text(
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                f"child_code = {child_code!r}\n"
                "child = subprocess.Popen([sys.executable, '-c', child_code])\n"
                f"Path({str(child_path)!r}).write_text(str(child.pid), encoding='utf-8')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            log_path = Path(directory) / "run.log"
            started_pids: list[int] = []

            def fail_to_record(worker_pid: int) -> None:
                started_pids.append(worker_pid)
                self.assertFalse(child_path.is_file())
                self.assertFalse(child_ready_path.is_file())
                raise OSError("worker identity record store unavailable")

            try:
                with log_path.open("w", encoding="utf-8") as log:
                    with self.assertRaisesRegex(OSError, "record store unavailable"):
                        run_streaming_command(
                            f"{sys.executable} hang.py",
                            Path(directory),
                            log,
                            on_start=fail_to_record,
                        )
                process_pids = tuple(started_pids)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if all(read_process_node(pid) is None for pid in process_pids):
                        break
                    time.sleep(0.01)
                self.assertTrue(
                    all(read_process_node(pid) is None for pid in process_pids),
                    process_pids,
                )
                self.assertFalse(child_path.exists())
                self.assertFalse(child_ready_path.exists())
            finally:
                if started_pids:
                    try:
                        os.killpg(started_pids[0], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(
        hasattr(os, "killpg"), "detached process groups are POSIX-only"
    )
    def test_streaming_command_wall_clock_timeout_reaps_detached_worker(
        self,
    ) -> None:
        # End-to-end proof against a real detached child: a worker that never
        # exits on its own is reaped by the wall-clock timeout, flagged
        # timed_out, and its process is actually dead afterward. The child is
        # killed within the tiny timeout so the test does not block on the sleep.
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "hang.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            log_path = Path(directory) / "run.log"
            started_pids: list[int] = []
            with log_path.open("w", encoding="utf-8") as log:
                result = run_streaming_command(
                    f"{sys.executable} hang.py",
                    Path(directory),
                    log,
                    on_start=started_pids.append,
                    timeout_seconds=0.1,
                    reap_poll_seconds=0.02,
                )

        self.assertTrue(result.timed_out)
        # Killed by signal rather than a clean exit.
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(len(started_pids), 1)
        child_pid = started_pids[0]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_streaming_command_captures_stderr_session_id_without_forwarding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(
                "import json\n"
                "import sys\n"
                "print(json.dumps({'type': 'thread.started', "
                "'thread_id': 'native-stderr-123'}), file=sys.stderr)\n",
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
            self.assertEqual(
                result.session_id_source,
                "native:stderr:json.thread_id",
            )
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn(
                "native-stderr-123",
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


class ProviderLimitLoopTests(unittest.TestCase):
    def _provider_limit_runner(
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
                classification="provider_limit",
                exit_code=1,
                log_path=repo / f"{task.task_id}.log",
                start_main="aaa",
                end_main="aaa",
                message="resets 1am (UTC)",
            )

        runner.run_task = run_task
        return runner

    def test_serial_provider_limit_stops_without_consuming_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)
            restart_calls: list[object] = []
            runner.record_task_restart = (  # type: ignore[method-assign]
                lambda *args, **kwargs: restart_calls.append((args, kwargs))
            )

            results = runner.run_until_done()

        self.assertEqual(
            [result.classification for result in results], ["provider_limit"]
        )
        # Dispatch stops instead of tight-looping into the same wall.
        self.assertEqual(calls, ["TASK-01"])
        # No restart/recovery budget is consumed.
        self.assertEqual(restart_calls, [])
        # The task remains runnable for the supervisor's next cycle.
        self.assertNotIn("TASK-01", source._done)

    def test_parallel_provider_limit_stops_without_consuming_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ]
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)
            restart_calls: list[object] = []
            runner.record_task_restart = (  # type: ignore[method-assign]
                lambda *args, **kwargs: restart_calls.append((args, kwargs))
            )

            results = runner.run_until_done(jobs=2)

        self.assertTrue(results)
        self.assertTrue(
            all(result.classification == "provider_limit" for result in results)
        )
        self.assertEqual(restart_calls, [])
        self.assertEqual(source._done, set())

    def test_provider_limit_invokes_reset_hook_for_claimed_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)],
                reset_hook=True,
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)

            results = runner.run_until_done()

        self.assertEqual(
            [result.classification for result in results], ["provider_limit"]
        )
        # The claimed task is handed back to the backend for re-dispatch.
        self.assertEqual(source.reset_calls, ["TASK-01"])
        # It stays runnable (never marked done), so the next cycle can pick it.
        self.assertNotIn("TASK-01", source._done)

    def test_parallel_provider_limit_invokes_reset_hook_for_claimed_task(self) -> None:
        # The serial path is covered above; the parallel drain path reaches the
        # same _report_provider_limit_pause chokepoint, so a reset hook must fire
        # there too. Locks path-independence of the reset behavior.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [
                    Task(task_id="TASK-01", title="Task 1", status="Next", order=1),
                    Task(task_id="TASK-02", title="Task 2", status="Next", order=2),
                ],
                reset_hook=True,
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)

            results = runner.run_until_done(jobs=2)

        self.assertTrue(results)
        self.assertTrue(
            all(result.classification == "provider_limit" for result in results)
        )
        # The claimed task(s) are handed back to the backend for re-dispatch and
        # never marked done, so the next cycle can pick them up.
        self.assertTrue(source.reset_calls)
        self.assertTrue(set(source.reset_calls) <= {"TASK-01", "TASK-02"})
        self.assertEqual(source._done, set())

    def test_provider_limit_without_reset_hook_leaves_status_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)],
                reset_hook=False,
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)
            restart_calls: list[object] = []
            runner.record_task_restart = (  # type: ignore[method-assign]
                lambda *args, **kwargs: restart_calls.append((args, kwargs))
            )

            results = runner.run_until_done()

        # Absent hook: dispatch still pauses without consuming budget, and the
        # source reports no reset (unchanged behavior).
        self.assertEqual(
            [result.classification for result in results], ["provider_limit"]
        )
        self.assertEqual(restart_calls, [])
        self.assertEqual(source.reset_calls, ["TASK-01"])

    def test_provider_limit_reset_hook_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)],
                reset_hook=True,
                reset_error=subprocess.CalledProcessError(3, "reset-hook"),
            )
            calls: list[str] = []
            runner = self._provider_limit_runner(repo, source, calls)

            # A failing reset hook must not crash the dispatch loop.
            results = runner.run_until_done()

        self.assertEqual(
            [result.classification for result in results], ["provider_limit"]
        )
        self.assertEqual(source.reset_calls, ["TASK-01"])
        self.assertEqual(calls, ["TASK-01"])


class TransientWorkerFailureTests(unittest.TestCase):
    def test_normal_workspace_deferral_waits_across_cycles_for_state_change(
        self,
    ) -> None:
        for mode in ("serial", "parallel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / "repo"
                repo.mkdir()
                subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "test@example.com"],
                    cwd=repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Test"],
                    cwd=repo,
                    check=True,
                )
                (repo / "README.md").write_text("base\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "base"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                base = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                runner = VibeRunner(
                    VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
                )
                source = MutableTaskSource(
                    [Task(task_id="TASK-01", title="Task", status="Next", order=1)]
                )
                runner._source = source
                dispatch_calls = 0

                def reject_dispatch(task: Task, restart_count: int = 0) -> RunResult:
                    nonlocal dispatch_calls
                    dispatch_calls += 1
                    fingerprint = runner_module.workspace_state_fingerprint(
                        repo=repo,
                        main_branch="main",
                        branch="main",
                        worktree=repo,
                        expected_base=base,
                    )
                    runner.run_store.append_lifecycle_event(
                        RunLifecycleEvent.workspace_preflight(
                            run_id="run-1",
                            task_id=task.task_id,
                            decision="rejected",
                            reason="workspace_stale_current_base",
                            retry_disposition="defer_until_workspace_changes",
                            worker_launch_allowed=False,
                            branch="main",
                            worktree=repo,
                            selected_base=base,
                            workspace_state_fingerprint=fingerprint,
                        )
                    )
                    raise WorkspaceProvisionError(
                        "workspace_stale_current_base",
                        "workspace does not contain current base",
                    )

                runner.run_task_with_supervision = reject_dispatch

                def run_cycle() -> list[RunResult]:
                    if mode == "serial":
                        return runner.run_until_done_serial(max_slices=1)
                    return runner.run_until_done_parallel(
                        ask_agent=False,
                        max_slices=1,
                        continue_on_failure=False,
                        jobs=2,
                    )

                with patch.object(runner, "ensure_spec_execution_gate"):
                    self.assertEqual(run_cycle(), [])
                    records_after_rejection = runner.run_store.read_records()
                    budget_records_after_rejection = [
                        record
                        for record in records_after_rejection
                        if record.get("record_type")
                        in {
                            "attempt_circuit_attempt",
                            "task_recovery",
                            "task_restart",
                            "worker_process_started",
                        }
                    ]
                    self.assertEqual(run_cycle(), [])
                    self.assertEqual(run_cycle(), [])

                    self.assertEqual(dispatch_calls, 1)
                    self.assertEqual(
                        [
                            record
                            for record in runner.run_store.read_records()
                            if record.get("record_type")
                            in {
                                "attempt_circuit_attempt",
                                "task_recovery",
                                "task_restart",
                                "worker_process_started",
                            }
                        ],
                        budget_records_after_rejection,
                    )

                    (repo / "changed.txt").write_text("wake\n", encoding="utf-8")

                    def complete_dispatch(
                        task: Task,
                        restart_count: int = 0,
                    ) -> RunResult:
                        nonlocal dispatch_calls
                        dispatch_calls += 1
                        source.mark_done(task.task_id)
                        return RunResult(
                            run_id="run-2",
                            task_id=task.task_id,
                            classification="completed",
                            exit_code=0,
                            log_path=repo / "run-2.log",
                            start_main=base,
                            end_main=base,
                        )

                    runner.run_task_with_supervision = complete_dispatch
                    changed = run_cycle()

                self.assertEqual(
                    [result.classification for result in changed], ["completed"]
                )
                self.assertEqual(dispatch_calls, 2)

    def test_deferred_workspace_recovery_waits_for_state_change(self) -> None:
        for mode in ("serial", "parallel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / "repo"
                repo.mkdir()
                subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "test@example.com"],
                    cwd=repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Test"],
                    cwd=repo,
                    check=True,
                )
                (repo / "README.md").write_text("base\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "base"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                base = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                dirty, dirty_fingerprint = runner_module.git_dirty_snapshot(repo)
                recovery = RecoveryContext(
                    task_id="TASK-01",
                    prior_run_id="run-1",
                    prior_classification="unknown",
                    branch="main",
                    worktree=str(repo),
                    head_commit=base,
                    transcript_path="",
                    wrapper_log="",
                    attempt=1,
                    max_attempts=3,
                    workspace_claimed=True,
                    base_commit=base,
                    git_common_dir=str((repo / ".git").resolve()),
                    dirty_snapshot=tuple(dirty),
                    dirty_fingerprint=dirty_fingerprint,
                )
                runner = VibeRunner(
                    VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
                )
                source = MutableTaskSource(
                    [Task(task_id="TASK-01", title="Task", status="Next", order=1)]
                )
                runner._source = source
                fingerprint = runner_module.workspace_retry_state_fingerprint(
                    repo=repo,
                    main_branch="main",
                    recovery=recovery,
                )
                runner.record_recovery_phase(
                    recovery,
                    phase="deferred",
                    blocker="workspace_stale_current_base",
                    retry_disposition="defer_until_workspace_changes",
                    workspace_state_fingerprint=fingerprint,
                )
                launches: list[RecoveryContext] = []

                def complete_after_wake(
                    task: Task,
                    *,
                    recovery: RecoveryContext | None = None,
                ) -> RunResult:
                    assert recovery is not None
                    launches.append(recovery)
                    source.mark_done(task.task_id)
                    return RunResult(
                        run_id="run-2",
                        task_id=task.task_id,
                        classification="completed",
                        exit_code=0,
                        log_path=repo / "run-2.log",
                        start_main=base,
                        end_main=base,
                    )

                runner.run_task = complete_after_wake
                if mode == "serial":
                    unchanged = runner.run_until_done_serial(max_slices=1)
                else:
                    unchanged = runner.run_until_done_parallel(
                        ask_agent=False,
                        max_slices=1,
                        continue_on_failure=False,
                        jobs=2,
                    )

                self.assertEqual(unchanged, [])
                self.assertEqual(launches, [])
                records = runner.run_store.read_records()
                self.assertFalse(
                    any(
                        record.get("record_type") == "task_restart"
                        for record in records
                    )
                )
                deferred = runner.run_store.pending_recovery_records()[0]
                self.assertEqual(deferred["attempt"], 1)
                self.assertEqual(deferred["workspace_state_fingerprint"], fingerprint)

                (repo / "changed.txt").write_text("wake\n", encoding="utf-8")
                if mode == "serial":
                    changed = runner.run_until_done_serial(max_slices=1)
                else:
                    changed = runner.run_until_done_parallel(
                        ask_agent=False,
                        max_slices=1,
                        continue_on_failure=False,
                        jobs=2,
                    )

                self.assertEqual(
                    [result.classification for result in changed], ["completed"]
                )
                self.assertEqual(len(launches), 1)
                self.assertEqual(launches[0].attempt, 1)

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
        self.assertIn("CURRENT active task lock", section)
        self.assertIn("VIBE_LOOP_WORKTREE", section)
        self.assertIn("VIBE_LOOP_BRANCH", section)
        self.assertNotIn("worker claim-workspace", section)

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
        self.assertIn("VIBE_LOOP_WORKTREE", section)
        self.assertIn("created and claimed a new dedicated workspace", section)
        self.assertIn("uncommitted filesystem changes were not adopted", section)
        self.assertNotIn("safely adopted the preserved workspace", section)
        self.assertNotIn("worker claim-workspace", section)

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
        self.assertEqual(phases, ["pending", "launched", "outcome"])
        self.assertEqual(recovery_records[2]["outcome"], "completed")
        restart_records = [
            record
            for record in records
            if record.get("record_type") == "task_restart"
            and record.get("reason") == "unknown_run_recovery"
        ]
        self.assertEqual(len(restart_records), 1)

    def test_serial_loop_recovers_reportless_claude_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run.log"
            log_path.write_text("Claude exited before reporting\n", encoding="utf-8")
            source = MutableTaskSource(
                [Task(task_id="TASK-01", title="Task 1", status="Next", order=1)]
            )
            runner._source = source
            calls: list[RecoveryContext | None] = []

            def run_task(task: Task, *, recovery: RecoveryContext | None = None):
                calls.append(recovery)
                if recovery is None:
                    return RunResult(
                        run_id="run-1",
                        task_id=task.task_id,
                        classification="failed",
                        classification_source="worker_report_missing",
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

        self.assertEqual(len(calls), 2)
        self.assertEqual(results[-1].classification, "completed")
        self.assertIsNotNone(calls[1])
        assert calls[1] is not None
        self.assertEqual(calls[1].prior_classification, "failed")

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
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tester"], cwd=repo, check=True
            )
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
            worktree = Path(directory) / "auto-01"
            subprocess.run(
                ["git", "worktree", "add", "-b", "auto-01-branch", str(worktree)],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
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
                    "worktree": str(worktree),
                    "base_commit": base,
                    "head_commit": base,
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
        self.assertEqual(recovery.worktree, str(worktree))
        self.assertEqual(recovery.head_commit, base)
        self.assertEqual(recovery.base_commit, base)
        self.assertTrue(recovery.git_common_dir)
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

    def test_recover_unknown_run_skips_when_probe_times_out(self) -> None:
        # Classification falls through to "unknown" on a probe failure, which
        # routes here; a command-backed probe that keeps failing (timeout, spawn
        # error, nonzero exit) must skip recovery rather than propagate.
        class TimingOutSource(MutableTaskSource):
            def probe(self, task_id: str) -> Task | None:
                raise subprocess.TimeoutExpired(cmd="probe", timeout=1.0)

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            log_path = repo / "run-1.log"
            log_path.write_text("parked\n", encoding="utf-8")
            runner._source = TimingOutSource([])

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

    def test_recover_unknown_run_skips_when_probe_json_is_malformed(self) -> None:
        class MalformedJsonSource(MutableTaskSource):
            def probe(self, task_id: str) -> Task | None:
                raise ValueError("malformed task-source JSON")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            runner._source = MalformedJsonSource([])
            prior = RunResult(
                run_id="run-1",
                task_id="TASK-01",
                classification="unknown",
                exit_code=0,
                log_path=repo / "run-1.log",
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
        self.assertEqual(launched, [])
        deferred = [
            record
            for record in records
            if record.get("record_type") == "task_recovery"
            and record.get("phase") == "deferred"
        ]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["attempt"], 1)
        outcomes = [
            record
            for record in records
            if record.get("record_type") == "task_recovery"
            and record.get("phase") == "outcome"
        ]
        self.assertEqual(outcomes, [])

    def test_pending_recovery_survives_prelaunch_failure_and_supervisor_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            log_path = repo / "run-1.log"
            log_path.write_text("parked\n", encoding="utf-8")
            transcript_path = repo / "session.jsonl"
            transcript_path.write_text("{}\n", encoding="utf-8")
            task = Task(task_id="TASK-01", title="Task 1", status="Next", order=1)
            first = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            first._source = MutableTaskSource([task])

            def blocked_before_launch(
                task: Task,
                *,
                recovery: RecoveryContext | None = None,
            ) -> RunResult:
                raise WorkspaceProvisionError(
                    "dirty_primary_worktree",
                    "primary worktree is temporarily dirty",
                )

            first.run_task = blocked_before_launch
            prior = RunResult(
                run_id="run-1",
                task_id=task.task_id,
                classification="unknown",
                exit_code=0,
                log_path=log_path,
                start_main="aaa",
                end_main="aaa",
                session_id="session-1",
                session_id_source="observed",
                transcript_path=str(transcript_path),
            )

            self.assertIsNone(
                first.recover_unknown_run(prior, attempt=1, max_attempts=3)
            )
            records_after_block = first.run_store.read_records()
            self.assertEqual(len(first.run_store.pending_recovery_records()), 1)
            deferred = [
                record
                for record in records_after_block
                if record.get("record_type") == "task_recovery"
                and record.get("phase") == "deferred"
            ]
            self.assertEqual(deferred[0]["retry_disposition"], "retry_later")
            self.assertFalse(
                any(
                    record.get("record_type") == "task_restart"
                    for record in records_after_block
                )
            )

            second = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            source = MutableTaskSource([task])
            second._source = source
            captured: list[RecoveryContext] = []

            def complete_recovery(
                task: Task,
                *,
                recovery: RecoveryContext | None = None,
            ) -> RunResult:
                assert recovery is not None
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

            second.run_task = complete_recovery
            results = second.run_until_done_serial()

            self.assertEqual(
                [result.classification for result in results], ["completed"]
            )
            self.assertEqual(captured[0].attempt, 1)
            self.assertEqual(captured[0].prior_session_id, "session-1")
            self.assertEqual(second.run_store.pending_recovery_records(), [])
            restart_records = [
                record
                for record in second.run_store.read_records()
                if record.get("record_type") == "task_restart"
            ]
            self.assertEqual(len(restart_records), 1)
            self.assertEqual(restart_records[0]["restart_count"], 1)

    def test_crash_after_worker_launch_advances_and_refreshes_recovery_intent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tester"], cwd=repo, check=True
            )
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
            worktree = root / "task-worktree"
            subprocess.run(
                ["git", "worktree", "add", "-b", "task/TASK-01", str(worktree)],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (worktree / "preserved.txt").write_text("work\n", encoding="utf-8")
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            first_intent = {
                "task_id": "TASK-01",
                "prior_run_id": "run-1",
                "prior_classification": "unknown",
                "attempt": 1,
                "max_attempts": 3,
                "workspace_claimed": True,
                "dirty_snapshot": [],
            }
            runner.run_store.append_result(
                RunResult(
                    run_id="run-1",
                    task_id="TASK-01",
                    classification="unknown",
                    exit_code=0,
                    log_path=root / "run-1.log",
                    start_main=base,
                    end_main=base,
                    recovery_intent=first_intent,
                )
            )
            runner.run_store.append_record(
                {
                    "record_type": "workspace_claim",
                    "event_type": "workspace_claimed",
                    "task_id": "TASK-01",
                    "run_id": "run-2",
                    "branch": "task/TASK-01",
                    "worktree": str(worktree),
                    "base_commit": base,
                    "head_commit": base,
                    "dirty": True,
                    "dirty_summary": ["?? preserved.txt"],
                }
            )
            runner.run_store.append_lifecycle_event(
                runner_module.RunLifecycleEvent.worker_process_started(
                    run_id="run-2",
                    task_id="TASK-01",
                    worker_pid=123,
                    supervisor_pid=12,
                    process_group_id=123,
                    session_id=123,
                    process_birth_id="birth",
                    host="host",
                    recovery_payload=first_intent,
                )
            )

            contexts = runner.pending_recovery_contexts()

            self.assertEqual(len(contexts), 1)
            recovery = contexts[0]
            self.assertEqual(recovery.prior_run_id, "run-2")
            self.assertEqual(recovery.attempt, 2)
            self.assertTrue(recovery.workspace_claimed)
            self.assertIn("?? preserved.txt", recovery.dirty_snapshot)
            self.assertTrue(recovery.dirty_fingerprint)
            pending = runner.run_store.pending_recovery_records()
            self.assertEqual(len(pending), 1)
            self.assertNotIn("needs_identity_refresh", pending[0])

    def test_crash_after_final_recovery_launch_exhausts_durable_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(repo=repo, agent=AgentConfig(command="worker"))
            )
            runner.run_store.append_lifecycle_event(
                runner_module.RunLifecycleEvent.worker_process_started(
                    run_id="run-final",
                    task_id="TASK-01",
                    worker_pid=123,
                    supervisor_pid=12,
                    process_group_id=123,
                    session_id=123,
                    process_birth_id="birth",
                    host="host",
                    recovery_payload={
                        "task_id": "TASK-01",
                        "prior_run_id": "run-before-final",
                        "prior_classification": "unknown",
                        "attempt": 3,
                        "max_attempts": 3,
                        "workspace_claimed": False,
                        "dirty_snapshot": [],
                    },
                )
            )

            contexts = runner.pending_recovery_contexts()

            self.assertEqual(contexts, [])
            self.assertEqual(runner._durably_exhausted_recovery_tasks, {"TASK-01"})
            failed = [
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "run_result"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["classification"], "failed")
            self.assertEqual(
                failed[0]["classification_source"], "recovery_budget_exhausted"
            )

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

    def test_parallel_loop_rescans_ready_work_before_retry_cooldown_sleep(
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
                        cooldown_seconds=3600,
                    ),
                )
            )
            log_path = repo / "transient.log"
            log_path.write_text("overloaded, please wait\n", encoding="utf-8")
            source = MutableTaskSource(
                [
                    Task(
                        task_id="TASK-A",
                        title="Cooling task",
                        status="Next",
                        order=1,
                        resources=("provider",),
                        conflict_domains_known=True,
                    ),
                    Task(
                        task_id="TASK-B",
                        title="Conflict holder",
                        status="Next",
                        order=2,
                        resources=("shared",),
                        conflict_domains_known=True,
                    ),
                    Task(
                        task_id="TASK-C",
                        title="Newly eligible work",
                        status="Next",
                        order=3,
                        resources=("shared",),
                        conflict_domains_known=True,
                    ),
                ]
            )
            runner._source = source
            clock = [0.0]
            c_started_at: list[float] = []
            original_list_candidates = runner.list_candidates
            stale_post_completion_scan = False

            def run_task(task: Task) -> RunResult:
                restart_count = runner.current_restart_count(task.task_id)
                if task.task_id == "TASK-A" and restart_count == 0:
                    return RunResult(
                        run_id="run-a-0",
                        task_id=task.task_id,
                        classification="failed",
                        exit_code=1,
                        log_path=log_path,
                        start_main="aaa",
                        end_main="aaa",
                    )
                if task.task_id == "TASK-C":
                    c_started_at.append(clock[0])
                source.mark_done(task.task_id)
                return RunResult(
                    run_id=f"run-{task.task_id.lower()}-{restart_count}",
                    task_id=task.task_id,
                    classification="completed",
                    exit_code=0,
                    log_path=repo / f"{task.task_id.lower()}.log",
                    start_main="aaa",
                    end_main="bbb",
                )

            def list_candidates(exclude: set[str] | None = None) -> list[Task]:
                nonlocal stale_post_completion_scan
                candidates = original_list_candidates(exclude=exclude)
                excluded = exclude or set()
                if (
                    not stale_post_completion_scan
                    and "TASK-A" in excluded
                    and "TASK-B" not in excluded
                    and any(task.task_id == "TASK-C" for task in candidates)
                ):
                    stale_post_completion_scan = True
                    return []
                return candidates

            def advance_clock(delay: float) -> None:
                clock[0] += delay

            runner.run_task = run_task
            runner.list_candidates = list_candidates
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
                    continue_on_failure=False,
                    jobs=2,
                    max_tasks=3,
                )

        self.assertTrue(stale_post_completion_scan)
        self.assertEqual(c_started_at, [0.0])
        self.assertEqual(
            sum(result.classification == "completed" for result in results),
            3,
        )

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

    def test_live_supervisor_holds_domains_after_worker_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            state = dataclasses.replace(
                _active_run_state(
                    task_id="POST-WORKER",
                    run_id="run-post-worker",
                    worker_pid=999999999,
                    host=socket.gethostname(),
                    repo=repo,
                    resources=("db",),
                ),
                supervisor_pid=os.getpid(),
                supervisor_process_birth_id=process_birth_identity(os.getpid()),
            )
            manager.acquire(
                "POST-WORKER",
                "run-post-worker",
                metadata=state.to_lock_metadata(),
            )

            domains = active_lock_conflict_domains(manager)

        self.assertEqual(len(domains), 1)
        self.assertIn("db", domains[0].resources)

    def test_group_liveness_uses_one_process_snapshot_for_all_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            for worker_pid in (999999901, 999999902):
                state = dataclasses.replace(
                    _active_run_state(
                        task_id=f"LIVE-{worker_pid}",
                        run_id=f"run-{worker_pid}",
                        worker_pid=worker_pid,
                        host=socket.gethostname(),
                        repo=repo,
                        resources=(f"resource-{worker_pid}",),
                    ),
                    worker_process_group_id=worker_pid,
                    worker_session_id=worker_pid,
                    worker_process_birth_id=f"boot-id:{worker_pid}",
                )
                manager.acquire(
                    state.task_id,
                    state.run_id,
                    metadata=state.to_lock_metadata(),
                )
            process_table = {
                worker_pid + 10: ProcessNode(
                    pid=worker_pid + 10,
                    parent_pid=1,
                    process_group_id=worker_pid,
                    session_id=worker_pid,
                    process_birth_id=f"boot-id:{worker_pid + 10}",
                    state="S",
                )
                for worker_pid in (999999901, 999999902)
            }
            with patch(
                "vibe_loop.workers.read_process_table",
                return_value=process_table,
            ) as read_table:
                domains = active_lock_conflict_domains(manager)

        self.assertEqual(len(domains), 2)
        self.assertEqual(read_table.call_count, 1)


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
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)

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
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
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
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
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
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
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
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
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

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_accepted_report_teardown_refuses_descendant_outside_worker_group(self):
        proc = FakeWatchdogProcess(alive_polls=10_000)
        root = ProcessNode(
            pid=proc.pid,
            parent_pid=1,
            process_group_id=proc.pid,
            session_id=proc.pid,
            process_birth_id="boot:root",
        )
        escaped = ProcessNode(
            pid=proc.pid + 1,
            parent_pid=proc.pid,
            process_group_id=proc.pid + 1,
            session_id=proc.pid + 1,
            process_birth_id="boot:child",
        )
        nodes = {root.pid: root, escaped.pid: escaped}
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = terminate_verified_worker_process_group(
                proc,
                StringIO(),
                expected_birth_id=root.process_birth_id,
                process_table=lambda: nodes,
                process_node=nodes.get,
            )

        self.assertFalse(result.terminated)
        self.assertTrue(result.identity_verified)
        self.assertFalse(result.descendants_verified)
        self.assertEqual(result.reason, "descendant_outside_worker_process_group")
        self.assertEqual(killed, [])

    def test_accepted_report_teardown_owns_reparented_same_group_child(self):
        proc = FakeWatchdogProcess(alive_polls=10_000)
        root = ProcessNode(
            pid=proc.pid,
            parent_pid=1,
            process_group_id=proc.pid,
            session_id=proc.pid,
            process_birth_id="boot:root",
        )
        orphan = ProcessNode(
            pid=proc.pid + 1,
            parent_pid=1,
            process_group_id=proc.pid,
            session_id=proc.pid,
            process_birth_id="boot:orphan",
        )
        nodes = {root.pid: root, orphan.pid: orphan}

        def terminated_group(*args, **kwargs) -> None:
            nodes.clear()

        with patch.object(
            runner_module,
            "terminate_worker_process_group",
            side_effect=terminated_group,
        ):
            result = terminate_verified_worker_process_group(
                proc,
                StringIO(),
                expected_birth_id=root.process_birth_id,
                process_table=lambda: nodes.copy(),
                process_node=nodes.get,
            )

        self.assertTrue(result.terminated)
        self.assertTrue(result.identity_verified)
        self.assertTrue(result.descendants_verified)
        self.assertEqual(result.reason, "accepted_report_runtime_closure")
        self.assertEqual(result.process_count, 2)

    def test_accepted_completed_candidate_uses_immediate_closure_path(self):
        proc = FakeWatchdogProcess(alive_polls=10_000, returncode=-signal.SIGTERM)
        monitor = FakePostReportMonitor(violates=False)
        teardown_calls = 0

        def teardown() -> runner_module.VerifiedWorkerTeardown:
            nonlocal teardown_calls
            teardown_calls += 1
            return runner_module.VerifiedWorkerTeardown(
                True,
                True,
                True,
                "accepted_report_runtime_closure",
                2,
            )

        result = wait_with_reap_watchdog(
            proc,
            StringIO(),
            reap_check=lambda: True,
            grace_seconds=120.0,
            poll_seconds=0.001,
            post_report_monitor=monitor,
            post_report_closure_check=lambda: "accepted_completed_candidate",
            post_report_teardown=teardown,
        )

        self.assertEqual(teardown_calls, 1)
        self.assertTrue(result.post_report_enforced)
        self.assertFalse(result.timed_out)
        self.assertEqual(
            result.post_report_teardown_reason,
            "accepted_report_runtime_closure",
        )
        self.assertTrue(result.post_report_identity_verified)
        self.assertTrue(result.post_report_descendants_verified)
        self.assertEqual(result.post_report_teardown_process_count, 2)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_activity_enforcement_preserves_failed_closure_evidence(self):
        proc = FakeWatchdogProcess(
            alive_polls=10_000,
            returncode=-signal.SIGTERM,
        )
        monitor = FakePostReportMonitor(violates=True)
        failed = runner_module.VerifiedWorkerTeardown(
            False,
            True,
            False,
            "process_group_contains_unowned_member",
            3,
        )
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                post_report_monitor=monitor,
                identity_verified_ok=lambda: True,
                post_report_activity_grace_seconds=0.0,
                post_report_closure_check=lambda: "accepted_completed_candidate",
                post_report_teardown=lambda: failed,
            )

        self.assertTrue(result.post_report_enforced)
        self.assertEqual(
            result.post_report_teardown_reason,
            "process_group_contains_unowned_member",
        )
        self.assertTrue(result.post_report_identity_verified)
        self.assertFalse(result.post_report_descendants_verified)
        self.assertEqual(result.post_report_teardown_process_count, 3)
        self.assertTrue(killed)
        activity = runner_module.PostReportActivity(
            reported=True,
            seconds=0.1,
            activity_kind="tool_call",
            activity_count=1,
            enforced_stop=result.post_report_enforced,
            identity_verified=result.post_report_identity_verified,
            usage=runner_module.unavailable_usage("anthropic", "test_fixture"),
            teardown_reason=result.post_report_teardown_reason,
            descendants_verified=result.post_report_descendants_verified,
            teardown_process_count=result.post_report_teardown_process_count,
        )
        completed = WorkerReport(
            run_id="run-1",
            task_id="T-1",
            status="completed",
        )
        self.assertEqual(
            runner_module.post_report_runtime_lifecycle_decision(
                runtime_owned=True,
                exit_code=result.exit_code,
                timed_out=False,
                worker_report=completed,
                activity=activity,
            ),
            ("refuse", "process_group_contains_unowned_member"),
        )

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_timeout_preserves_failed_closure_evidence(self):
        proc = FakeWatchdogProcess(
            alive_polls=10_000,
            returncode=-signal.SIGKILL,
        )
        monitor = FakePostReportMonitor(violates=False)
        failed = runner_module.VerifiedWorkerTeardown(
            False,
            True,
            False,
            "process_group_contains_unowned_member",
            3,
        )
        clock = FakeMonotonicClock(([0.0] * 7) + [2.0])
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                monotonic=clock,
                post_report_monitor=monitor,
                identity_verified_ok=lambda: True,
                post_report_closure_check=lambda: "accepted_completed_candidate",
                post_report_teardown=lambda: failed,
            )

        self.assertTrue(result.timed_out)
        self.assertFalse(result.post_report_enforced)
        self.assertEqual(
            result.post_report_teardown_reason,
            "process_group_contains_unowned_member",
        )
        self.assertTrue(result.post_report_identity_verified)
        self.assertFalse(result.post_report_descendants_verified)
        self.assertEqual(result.post_report_teardown_process_count, 3)
        self.assertTrue(killed)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_wall_clock_timeout_kills_process_group_and_flags_timed_out(self):
        # A worker that never becomes reap-eligible but overruns the wall-clock
        # deadline must be force-killed via its process GROUP (it is launched
        # detached, so a plain terminate would miss grandchildren) and reported
        # as timed_out so the caller returns the task to runnable.
        proc = FakeWatchdogProcess(alive_polls=10_000, returncode=0)
        clock = FakeMonotonicClock([0.0, 100.0])
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: False,
                grace_seconds=120.0,
                poll_seconds=0.001,
                timeout_seconds=5.0,
                monotonic=clock,
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(killed)
        self.assertEqual(killed[0], (proc.pid, signal.SIGTERM))
        # The lingering fake group forces the SIGTERM -> SIGKILL escalation.
        self.assertIn((proc.pid, signal.SIGKILL), killed)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_wall_clock_timeout_fires_without_a_reap_check(self):
        # The deadline must bound the run even when no reap_check is supplied
        # (the historical no-reap path was a plain unbounded blocking wait).
        proc = FakeWatchdogProcess(alive_polls=10_000, returncode=0)
        clock = FakeMonotonicClock([0.0, 100.0])
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=None,
                grace_seconds=120.0,
                poll_seconds=0.001,
                timeout_seconds=5.0,
                monotonic=clock,
            )
        self.assertTrue(result.timed_out)
        self.assertEqual(killed[0], (proc.pid, signal.SIGTERM))

    def test_zero_timeout_is_unbounded_and_never_kills(self):
        # timeout_seconds=0 (or None) preserves today's unbounded behavior: the
        # no-reap path stays a plain blocking wait and nothing is killed.
        proc = FakeWatchdogProcess(alive_polls=0, returncode=3)

        def exploding_clock() -> float:
            raise AssertionError("monotonic must not be consulted when unbounded")

        result = wait_with_reap_watchdog(
            proc,
            StringIO(),
            reap_check=None,
            grace_seconds=120.0,
            poll_seconds=0.001,
            timeout_seconds=0.0,
            monotonic=exploding_clock,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.timed_out)
        self.assertEqual(proc.kill_calls, 0)


class FakeMonotonicClock:
    """Deterministic ``time.monotonic`` stand-in for deadline tests.

    Returns each queued value in turn, then repeats the last value so any
    surplus calls stay past the deadline instead of raising.
    """

    def __init__(self, values: list[float]):
        self._values = list(values)
        self._last = self._values[0] if self._values else 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class ClassifyPostReportActivityTests(unittest.TestCase):
    def test_runtime_lifecycle_decision_rejects_non_authoritative_exits(self) -> None:
        activity = runner_module.PostReportActivity(
            reported=True,
            seconds=0.5,
            activity_kind="tool_call",
            activity_count=1,
            enforced_stop=True,
            identity_verified=True,
            usage=runner_module.unavailable_usage("anthropic", "test_fixture"),
        )
        completed = WorkerReport(run_id="run-1", task_id="T-1", status="completed")
        blocked = dataclasses.replace(completed, status="blocked")

        self.assertEqual(
            runner_module.post_report_runtime_lifecycle_decision(
                runtime_owned=False,
                exit_code=-signal.SIGTERM,
                timed_out=False,
                worker_report=completed,
                activity=activity,
            ),
            ("refuse", "runtime_owned_orchestration_disabled"),
        )

        cases = (
            ({"timed_out": True, "worker_report": completed}, "worker_timed_out"),
            (
                {"timed_out": False, "worker_report": None},
                "accepted_report_missing",
            ),
            (
                {"timed_out": False, "worker_report": blocked},
                "accepted_report_not_completed",
            ),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                decision = runner_module.post_report_runtime_lifecycle_decision(
                    runtime_owned=True,
                    exit_code=-signal.SIGTERM,
                    activity=activity,
                    **overrides,
                )
                self.assertEqual(decision, ("refuse", reason))

    def test_claude_tool_use_block_is_tool_call(self) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "summary"},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                    ]
                },
            }
        )
        self.assertEqual(classify_post_report_activity(line), "tool_call")

    def test_claude_text_only_assistant_is_benign(self) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "final summary"}]},
            }
        )
        self.assertEqual(classify_post_report_activity(line), "")

    def test_claude_result_event_is_benign(self) -> None:
        line = json.dumps({"type": "result", "subtype": "success", "usage": {}})
        self.assertEqual(classify_post_report_activity(line), "")

    def test_claude_tool_result_user_turn_is_tool_result(self) -> None:
        line = json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
            }
        )
        self.assertEqual(classify_post_report_activity(line), "tool_result")

    def test_codex_function_call_event_is_tool_call(self) -> None:
        line = json.dumps({"type": "function_call", "name": "shell"})
        self.assertEqual(classify_post_report_activity(line), "tool_call")

    def test_codex_item_completed_command_is_tool_call(self) -> None:
        line = json.dumps(
            {"type": "item.completed", "item": {"type": "command_execution"}}
        )
        self.assertEqual(classify_post_report_activity(line), "tool_call")

    def test_codex_agent_message_and_token_count_are_benign(self) -> None:
        for payload in (
            {"type": "item.completed", "item": {"type": "agent_message"}},
            {"type": "token_count", "info": {}},
            {"type": "turn.completed", "usage": {}},
        ):
            self.assertEqual(classify_post_report_activity(json.dumps(payload)), "")

    def test_non_json_and_prose_are_benign(self) -> None:
        self.assertEqual(classify_post_report_activity("just some prose\n"), "")
        self.assertEqual(classify_post_report_activity("{not json"), "")

    def test_event_carries_claude_tool_use_id_as_a_start(self) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash"}]
                },
            }
        )
        event = classify_post_report_event(line)
        self.assertEqual(event, ActivityEvent("tool_call", "toolu_1", False))

    def test_event_carries_claude_tool_result_id_as_a_completion(self) -> None:
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]
                },
            }
        )
        event = classify_post_report_event(line)
        self.assertEqual(event, ActivityEvent("tool_result", "toolu_1", True))

    def test_event_codex_exec_begin_and_end_correlate_by_call_id(self) -> None:
        begin = classify_post_report_event(
            json.dumps({"type": "exec_command_begin", "call_id": "c1"})
        )
        end = classify_post_report_event(
            json.dumps({"type": "exec_command_end", "call_id": "c1"})
        )
        self.assertEqual(begin, ActivityEvent("tool_call", "c1", False))
        self.assertEqual(end, ActivityEvent("tool_call", "c1", True))

    def test_event_codex_item_started_and_completed_correlate_by_id(self) -> None:
        started = classify_post_report_event(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "id": "i1"},
                }
            )
        )
        completed = classify_post_report_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "id": "i1"},
                }
            )
        )
        self.assertEqual(started, ActivityEvent("tool_call", "i1", False))
        self.assertEqual(completed, ActivityEvent("tool_call", "i1", True))


class PostReportActivityMonitorTests(unittest.TestCase):
    def _tool_line(self) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            }
        )

    def test_monitor_is_inert_before_report(self) -> None:
        monitor = PostReportActivityMonitor("anthropic")
        monitor.observe_line(self._tool_line())
        self.assertFalse(monitor.reported)
        self.assertFalse(monitor.violation)
        snapshot = monitor.snapshot()
        self.assertFalse(snapshot.reported)

    def test_text_summary_after_report_is_not_a_violation(self) -> None:
        clock = FakeMonotonicClock([10.0, 12.5])
        monitor = PostReportActivityMonitor("anthropic", monotonic=clock)
        monitor.mark_report_observed()
        monitor.observe_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "bye"}]},
                }
            )
        )
        self.assertTrue(monitor.reported)
        self.assertFalse(monitor.violation)
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.activity_kind, "")
        self.assertEqual(snapshot.seconds, 2.5)

    def test_tool_activity_after_report_is_a_violation(self) -> None:
        monitor = PostReportActivityMonitor("anthropic")
        monitor.mark_report_observed()
        monitor.observe_line(self._tool_line())
        monitor.observe_line(self._tool_line())
        self.assertTrue(monitor.violation)
        snapshot = monitor.snapshot(enforced_stop=True, identity_verified=True)
        self.assertEqual(snapshot.activity_kind, "tool_call")
        self.assertEqual(snapshot.activity_count, 2)
        self.assertTrue(snapshot.enforced_stop)
        self.assertTrue(snapshot.identity_verified)

    def test_post_report_usage_reports_only_teardown_delta(self) -> None:
        # Provider result/turn events carry cumulative totals. The useful
        # implementation spend before the report must not be attributed to the
        # post-report teardown; only the delta from the boundary snapshot is.
        monitor = PostReportActivityMonitor("anthropic")
        pre = json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 100000, "output_tokens": 800},
            }
        )
        monitor.observe_line(pre)
        self.assertFalse(monitor.snapshot().usage.available)
        monitor.mark_report_observed()
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100100, "output_tokens": 900},
                }
            )
        )
        usage = monitor.snapshot().usage
        self.assertTrue(usage.available)
        # Only the 100 input / 100 output tokens spent after the boundary.
        self.assertEqual(usage.values["input_tokens"], 100)
        self.assertEqual(usage.values["output_tokens"], 100)

    def test_post_report_usage_empty_when_no_additional_spend(self) -> None:
        # A cumulative event repeated after the report with no new spend must
        # not re-attribute the whole run as teardown burn.
        monitor = PostReportActivityMonitor("anthropic")
        event = json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 100000, "output_tokens": 800},
            }
        )
        monitor.observe_line(event)
        monitor.mark_report_observed()
        monitor.observe_line(event)
        usage = monitor.snapshot().usage
        self.assertFalse(usage.available)

    def _tool_use(self, tool_id: str) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "id": tool_id, "name": "Bash"}]
                },
            }
        )

    def _tool_result(self, tool_id: str) -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": tool_id}]
                },
            }
        )

    def test_activity_between_persistence_and_mark_is_reconciled(self) -> None:
        # A tool call emitted after the report persisted (wall 120) but before
        # the watchdog marked the boundary is buffered and, once the boundary is
        # marked at the persistence instant (100), counted rather than lost (F1).
        # A tool call from before persistence (wall 80) stays benign.
        wall = FakeMonotonicClock([80.0, 120.0])
        monitor = PostReportActivityMonitor("anthropic", wallclock=wall)
        monitor.observe_line(self._tool_use("pre"))
        monitor.observe_line(self._tool_use("post"))
        self.assertFalse(monitor.violation)
        monitor.mark_report_observed(boundary_wall=100.0)
        self.assertTrue(monitor.violation)
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.activity_kind, "tool_call")
        self.assertEqual(snapshot.activity_count, 1)

    def test_pre_boundary_tool_completing_after_report_is_ignored(self) -> None:
        # A tool that started before the report (wall 80) and only completes
        # after it (wall 130) -- e.g. the worker's own vibe-loop report call and
        # its result -- is correlated by id and not counted as fresh activity.
        wall = FakeMonotonicClock([80.0, 130.0])
        monitor = PostReportActivityMonitor("anthropic", wallclock=wall)
        monitor.observe_line(self._tool_use("t0"))
        monitor.observe_line(self._tool_result("t0"))
        monitor.mark_report_observed(boundary_wall=100.0)
        self.assertFalse(monitor.violation)
        self.assertEqual(monitor.snapshot().activity_count, 0)

    def test_delayed_claude_report_tool_start_uses_provider_timestamp(self) -> None:
        # The report command started before persistence but the reader did not
        # consume its Claude stream-json line until after the watchdog boundary.
        # Its provider timestamp, rather than reader delay, keeps it benign.
        monitor = PostReportActivityMonitor("anthropic", wallclock=lambda: 150.0)
        monitor.observe_line(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": 90.0,
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "report", "name": "Bash"}
                        ]
                    },
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        self.assertFalse(monitor.violation)

    def test_delayed_codex_report_item_uses_provider_timestamp(self) -> None:
        monitor = PostReportActivityMonitor("openai", wallclock=lambda: 150.0)
        monitor.observe_line(
            json.dumps(
                {
                    "timestamp": "1970-01-01T00:01:30+00:00",
                    "type": "item.started",
                    "item": {"type": "command_execution", "id": "report"},
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        self.assertFalse(monitor.violation)

    def test_malformed_provider_timestamp_falls_back_to_reader_order(self) -> None:
        monitor = PostReportActivityMonitor("anthropic", wallclock=lambda: 150.0)
        monitor.observe_line(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "not-a-timestamp",
                    "message": {
                        "content": [{"type": "tool_use", "id": "fresh", "name": "Bash"}]
                    },
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        self.assertTrue(monitor.violation)

    def test_fresh_post_boundary_tool_start_is_a_violation(self) -> None:
        wall = FakeMonotonicClock([150.0])
        monitor = PostReportActivityMonitor("anthropic", wallclock=wall)
        monitor.mark_report_observed(boundary_wall=100.0)
        monitor.observe_line(self._tool_use("fresh"))
        self.assertTrue(monitor.violation)
        self.assertEqual(monitor.snapshot().activity_count, 1)

    def test_end_only_cumulative_usage_is_not_labeled_post_report(self) -> None:
        # The only usage signal is a single cumulative total emitted after the
        # report. Attributing the whole run as teardown burn would be wrong, so
        # post-report usage is left unavailable (F5).
        monitor = PostReportActivityMonitor("anthropic")
        monitor.mark_report_observed()
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100000, "output_tokens": 800},
                }
            )
        )
        usage = monitor.snapshot().usage
        self.assertFalse(usage.available)
        self.assertEqual(
            usage.unavailable_reason, "post_report_usage_end_only_cumulative"
        )

    def test_delayed_claude_usage_uses_pre_boundary_cumulative_baseline(self) -> None:
        # Both lines reach the reader after persistence. The later cumulative
        # total must subtract the timestamped pre-boundary total, not itself.
        monitor = PostReportActivityMonitor("anthropic")
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "timestamp": 90.0,
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            )
        )
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "timestamp": 110.0,
                    "usage": {"input_tokens": 140, "output_tokens": 15},
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        usage = monitor.snapshot().usage
        self.assertTrue(usage.available)
        self.assertEqual(usage.values["input_tokens"], 40)
        self.assertEqual(usage.values["output_tokens"], 5)

    def test_delayed_codex_usage_uses_pre_boundary_cumulative_baseline(self) -> None:
        monitor = PostReportActivityMonitor("openai")
        monitor.observe_line(
            json.dumps(
                {
                    "timestamp": "1970-01-01T00:01:30+00:00",
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            )
        )
        monitor.observe_line(
            json.dumps(
                {
                    "timestamp": "1970-01-01T00:01:50+00:00",
                    "type": "turn.completed",
                    "usage": {"input_tokens": 130, "output_tokens": 20},
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        usage = monitor.snapshot().usage
        self.assertTrue(usage.available)
        self.assertEqual(usage.values["input_tokens"], 30)
        self.assertEqual(usage.values["output_tokens"], 10)

    def test_usage_order_uses_provider_timestamp_not_reader_consumption(self) -> None:
        # A delayed pre-report line can arrive after a post-report cumulative
        # total. The logical final total is still the later provider timestamp.
        monitor = PostReportActivityMonitor("anthropic")
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "timestamp": 110.0,
                    "usage": {"input_tokens": 140, "output_tokens": 15},
                }
            )
        )
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "timestamp": 90.0,
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        usage = monitor.snapshot().usage
        self.assertTrue(usage.available)
        self.assertEqual(usage.values["input_tokens"], 40)
        self.assertEqual(usage.values["output_tokens"], 5)

    def test_incompatible_usage_history_is_not_subtracted(self) -> None:
        monitor = PostReportActivityMonitor("anthropic")
        monitor.observe_line(
            json.dumps(
                {
                    "type": "result",
                    "timestamp": 90.0,
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            )
        )
        monitor.observe_line(
            json.dumps(
                {
                    "timestamp": 110.0,
                    "type": "turn.completed",
                    "usage": {"input_tokens": 130, "output_tokens": 20},
                }
            )
        )
        monitor.mark_report_observed(boundary_wall=100.0)
        usage = monitor.snapshot().usage
        self.assertFalse(usage.available)
        self.assertEqual(
            usage.unavailable_reason, "post_report_usage_incompatible_cumulative"
        )

    def test_report_persistence_epoch_parses_reported_at(self) -> None:
        report = WorkerReport(
            run_id="run-1",
            task_id="TASK-01",
            status="completed",
            reported_at="2026-07-21T12:00:00+00:00",
        )
        epoch = worker_report_persistence_epoch(report)
        self.assertIsNotNone(epoch)
        self.assertEqual(
            epoch,
            datetime.datetime(2026, 7, 21, 12, 0, 0, tzinfo=datetime.UTC).timestamp(),
        )

    def test_report_persistence_epoch_is_none_for_unparseable(self) -> None:
        self.assertIsNone(worker_report_persistence_epoch(None))
        report = WorkerReport(
            run_id="run-1",
            task_id="TASK-01",
            status="completed",
            reported_at="not-a-timestamp",
        )
        self.assertIsNone(worker_report_persistence_epoch(report))


class FakePostReportMonitor:
    """Watchdog-facing stand-in whose violation state is deterministic."""

    def __init__(self, *, violates: bool):
        self._violates = violates
        self.mark_calls: list[float | None] = []

    def mark_report_observed(
        self, at: float | None = None, *, boundary_wall: float | None = None
    ) -> None:
        self.mark_calls.append(at)

    @property
    def violation(self) -> bool:
        # Only meaningful once the boundary is marked, mirroring the real
        # monitor which is inert until then.
        return bool(self.mark_calls) and self._violates


class PostReportWatchdogTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_post_report_activity_stops_verified_group_without_timeout(self):
        proc = FakeWatchdogProcess(alive_polls=10_000)
        monitor = FakePostReportMonitor(violates=True)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                post_report_monitor=monitor,
                identity_ok=lambda: True,
                post_report_activity_grace_seconds=0.0,
            )
        self.assertTrue(result.post_report_enforced)
        self.assertFalse(result.timed_out)
        self.assertTrue(killed)
        self.assertEqual(killed[0], (proc.pid, signal.SIGTERM))
        self.assertTrue(monitor.mark_calls)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_post_report_activity_with_identity_mismatch_never_signals(self):
        proc = FakeWatchdogProcess(alive_polls=10_000)
        monitor = FakePostReportMonitor(violates=True)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                post_report_monitor=monitor,
                identity_ok=lambda: False,
                post_report_activity_grace_seconds=0.0,
            )
        self.assertEqual(killed, [])
        self.assertFalse(result.post_report_enforced)
        self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_post_report_enforcement_is_fail_closed_without_verified_identity(self):
        # Post-report teardown must NOT signal when birth identity cannot be
        # positively verified, even though the lenient hang/timeout guard would
        # (identity_ok True). This is the fail-closed guarantee: an unverifiable
        # PID may name a recycled, unrelated group.
        proc = FakeWatchdogProcess(alive_polls=10_000)
        monitor = FakePostReportMonitor(violates=True)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                post_report_monitor=monitor,
                identity_ok=lambda: True,
                identity_verified_ok=lambda: False,
                post_report_activity_grace_seconds=0.0,
            )
        self.assertEqual(killed, [])
        self.assertFalse(result.post_report_enforced)
        self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_text_only_summary_within_grace_is_not_killed(self):
        proc = FakeWatchdogProcess(alive_polls=2)
        monitor = FakePostReportMonitor(violates=False)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=lambda: True,
                grace_seconds=120.0,
                poll_seconds=0.001,
                post_report_monitor=monitor,
                identity_ok=lambda: True,
                post_report_activity_grace_seconds=0.0,
            )
        self.assertEqual(killed, [])
        self.assertFalse(result.post_report_enforced)
        self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_hang_reap_respects_identity_guard(self):
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
                identity_ok=lambda: False,
            )
        self.assertEqual(killed, [])
        self.assertFalse(result.post_report_enforced)
        self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_hang_reap_requires_positively_verified_identity(self):
        # F3: report-hang termination is fail-closed on identity too. A lenient
        # identity_ok=True must not reap when the strict verified gate says the
        # birth identity could not be positively confirmed.
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
                identity_ok=lambda: True,
                identity_verified_ok=lambda: False,
            )
        self.assertEqual(killed, [])
        self.assertFalse(result.post_report_enforced)
        self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        hasattr(os, "killpg"), "patches os.killpg; POSIX process groups only"
    )
    def test_timeout_reap_requires_positively_verified_identity(self):
        # F3: wall-clock timeout termination is fail-closed on identity too.
        proc = FakeWatchdogProcess(alive_polls=10_000)
        killed: list[tuple[int, int]] = []
        with patch.object(
            runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
        ):
            result = wait_with_reap_watchdog(
                proc,
                StringIO(),
                reap_check=None,
                grace_seconds=120.0,
                poll_seconds=0.001,
                timeout_seconds=0.01,
                monotonic=FakeMonotonicClock([0.0, 0.02, 0.03]),
                identity_ok=lambda: True,
                identity_verified_ok=lambda: False,
            )
        self.assertEqual(killed, [])
        self.assertTrue(result.timed_out)


class RunStreamingPostReportTests(unittest.TestCase):
    def _run(self, body: str, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "cmd.py"
            script.write_text(body, encoding="utf-8")
            log_path = Path(directory) / "run.log"
            stderr = StringIO()
            with log_path.open("w", encoding="utf-8") as log:
                with redirect_stderr(stderr):
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        Path(directory),
                        log,
                        **kwargs,
                    )
        return result

    def test_worker_that_exits_immediately_after_report_is_unchanged(self):
        result = self._run(
            "import json\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success'}))\n",
            reap_check=lambda: True,
            reap_grace_seconds=60.0,
            reap_poll_seconds=0.02,
            provider="anthropic",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.post_report is None or not result.post_report.violation)

    def test_text_only_summary_after_report_finalizes_without_violation(self):
        result = self._run(
            "import json, time\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': "
            "[{'type': 'text', 'text': 'done'}]}}), flush=True)\n"
            "time.sleep(0.2)\n",
            reap_check=lambda: True,
            reap_grace_seconds=60.0,
            reap_poll_seconds=0.02,
            provider="anthropic",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.post_report)
        self.assertTrue(result.post_report.reported)
        self.assertFalse(result.post_report.violation)
        self.assertFalse(result.post_report.enforced_stop)

    def test_post_report_tool_activity_stops_the_worker(self):
        pids: list[int] = []
        result = self._run(
            "import json, sys, time\n"
            "line = json.dumps({'type': 'assistant', 'message': {'content': "
            "[{'type': 'tool_use', 'name': 'Bash'}]}})\n"
            "for _ in range(100000):\n"
            "    sys.stdout.write(line + '\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.02)\n",
            on_start=pids.append,
            reap_check=lambda: True,
            reap_grace_seconds=60.0,
            reap_poll_seconds=0.02,
            post_report_activity_grace_seconds=0.0,
            provider="anthropic",
        )
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.post_report)
        self.assertTrue(result.post_report.violation)
        self.assertEqual(result.post_report.activity_kind, "tool_call")
        self.assertTrue(result.post_report.enforced_stop)
        self.assertNotEqual(result.exit_code, 0)
        # The call returns only after the worker is reaped, so no next-task
        # dispatch can overlap an unfinalized process.
        self.assertTrue(pids)
        self.assertIsNone(read_process_node(pids[0]))

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "killpg"),
        "verified process-tree teardown requires Linux",
    )
    def test_accepted_report_immediately_drains_lingering_process_tree(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sentinel_node = read_process_node(sentinel.pid)
        self.assertIsNotNone(sentinel_node)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                child_pid_path = root / "child.pid"
                orphan_ready_path = root / "orphan.ready"
                report_path = root / "reported"
                spawner = root / "spawn_child.py"
                spawner.write_text(
                    "import pathlib, subprocess, sys\n"
                    "child = subprocess.Popen([sys.executable, '-c', "
                    '"import os, pathlib, time; '
                    "deadline = time.monotonic() + 10; "
                    "\\nwhile os.getppid() != 1 and time.monotonic() < deadline: "
                    "time.sleep(0.01); "
                    "\\npathlib.Path('orphan.ready').write_text('ready'); "
                    'time.sleep(60)"])\n'
                    "pathlib.Path('child.pid').write_text(str(child.pid))\n",
                    encoding="utf-8",
                )
                script = root / "cmd.py"
                script.write_text(
                    "import pathlib, subprocess, sys, time\n"
                    "intermediate = subprocess.Popen("
                    "[sys.executable, 'spawn_child.py'])\n"
                    "intermediate.wait()\n"
                    "deadline = time.monotonic() + 10\n"
                    "while not pathlib.Path('orphan.ready').exists():\n"
                    "    if time.monotonic() >= deadline:\n"
                    "        raise RuntimeError('child was not reparented')\n"
                    "    time.sleep(0.01)\n"
                    "pathlib.Path('reported').write_text('completed')\n"
                    "time.sleep(60)\n",
                    encoding="utf-8",
                )
                log_path = root / "run.log"
                started = time.monotonic()
                with log_path.open("w", encoding="utf-8") as log:
                    result = run_streaming_command(
                        f"{sys.executable} cmd.py",
                        root,
                        log,
                        reap_check=report_path.exists,
                        reap_grace_seconds=120.0,
                        reap_poll_seconds=0.02,
                        post_report_closure_check=(
                            lambda: "accepted_completed_candidate"
                        ),
                        provider="anthropic",
                    )
                elapsed = time.monotonic() - started
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                self.assertTrue(orphan_ready_path.exists())

                self.assertLess(elapsed, 5.0)
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                self.assertIsNotNone(result.post_report)
                self.assertTrue(result.post_report.enforced_stop)
                self.assertTrue(result.post_report.identity_verified)
                self.assertTrue(result.post_report.descendants_verified)
                self.assertEqual(
                    result.post_report.teardown_reason,
                    "accepted_report_runtime_closure",
                )
                self.assertGreaterEqual(result.post_report.teardown_process_count, 2)
                child = read_process_node(child_pid)
                self.assertTrue(
                    child is None or child.state in {"Z", "X", "x"},
                    "lingering worker child survived verified teardown",
                )
                surviving_sentinel = read_process_node(sentinel.pid)
                self.assertIsNotNone(surviving_sentinel)
                self.assertEqual(
                    surviving_sentinel.process_birth_id,
                    sentinel_node.process_birth_id,
                )
        finally:
            current = read_process_node(sentinel.pid)
            if (
                current is not None
                and current.process_birth_id == sentinel_node.process_birth_id
            ):
                sentinel.terminate()
            try:
                sentinel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                current = read_process_node(sentinel.pid)
                if (
                    current is not None
                    and current.process_birth_id == sentinel_node.process_birth_id
                ):
                    sentinel.kill()
                sentinel.wait(timeout=5)

    def test_unavailable_birth_identity_keeps_clean_exit_fallback(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                closure_checks = 0

                def accepted_closure() -> str:
                    nonlocal closure_checks
                    closure_checks += 1
                    return "accepted_completed_candidate"

                with (
                    patch.object(runner_module.sys, "platform", platform),
                    patch.object(
                        runner_module,
                        "read_process_node",
                        return_value=None,
                    ),
                ):
                    result = self._run(
                        "import time\ntime.sleep(0.05)\n",
                        reap_check=lambda: True,
                        reap_grace_seconds=60.0,
                        reap_poll_seconds=0.01,
                        post_report_closure_check=accepted_closure,
                        provider="anthropic",
                    )

                self.assertEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                self.assertEqual(closure_checks, 0)
                self.assertIsNotNone(result.post_report)
                self.assertEqual(result.post_report.teardown_reason, "")
                self.assertFalse(result.post_report.enforced_stop)

    def test_tool_activity_before_a_poll_is_still_recorded(self):
        # F1: a worker that reports, invokes a tool, and exits within a single
        # poll window is gone before the watchdog ever marks the boundary. The
        # exit-time reconciliation still marks the boundary from the persisted
        # report so the post-report tool call is attributed, even though there
        # is no live process left to stop.
        boundary = time.time()
        result = self._run(
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'type': 'assistant', 'message': "
            "{'content': [{'type': 'tool_use', 'id': 'x', 'name': 'Bash'}]}}) "
            "+ '\\n')\n"
            "sys.stdout.flush()\n",
            reap_check=lambda: True,
            report_persistence_epoch=lambda: boundary,
            reap_grace_seconds=60.0,
            reap_poll_seconds=5.0,
            provider="anthropic",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.post_report)
        self.assertTrue(result.post_report.reported)
        self.assertTrue(result.post_report.violation)
        self.assertEqual(result.post_report.activity_kind, "tool_call")
        self.assertFalse(result.post_report.enforced_stop)


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
                        analysis_command=f"{stub} --model analysis-model {{prompt}}",
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
            self.assertEqual(runner.last_analysis_runtime_context.model_id, "")
            self.assertEqual(runner.last_analysis_runtime_context.model_id_source, "")
            self.assertEqual(
                runner.last_analysis_runtime_context.attribution_diagnostics,
                ("model",),
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

    def test_run_analysis_agent_raises_on_a_real_provider_limit_subprocess(
        self,
    ) -> None:
        # End-to-end over a real subprocess: the observed Codex wall text must
        # travel from the agent's exit through the retry layer and surface as a
        # typed error carrying the reset, without spending any retry attempt.
        # The reset is expressed relative to the real clock the production
        # parser reads. Pinning the observed Jul 25 2026 instant instead would
        # make this test pass only until that date, then report the wall as
        # elapsed and fall back to the default backoff.
        reset = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=5
        )
        reset_month = reset.strftime("%b")
        reset_phrase = (
            f"{reset_month} {reset.day}, {reset.year} "
            f"{reset.hour % 12 or 12}:{reset.minute:02d} "
            f"{'AM' if reset.hour < 12 else 'PM'}"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stub = repo / "analysis-stub.py"
            attempts = repo / "attempts.log"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"open({str(attempts)!r}, 'a').write('x')\n"
                'sys.stderr.write("You\'ve hit your usage limit. Your limit "\n'
                f'    "will reset and you can try again at {reset_phrase}.")\n'
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
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

            with self.assertRaises(AgentProviderLimitError) as caught:
                runner.run_analysis_agent("inspect", output_path)

            error = caught.exception
            self.assertIn(f"{reset_month} {reset.day}", error.signal.reset_text)
            assert error.signal.reset_delay is not None
            # Days out, so the pause is the parsed reset rather than the
            # configured default backoff.
            self.assertGreater(error.pause_seconds, 4 * 24 * 3600)
            self.assertEqual(error.pause_seconds, error.signal.reset_delay)
            # Invoked exactly once: no jittered retries were burned.
            self.assertEqual(attempts.read_text(), "x")
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


class AgentCommandModelTests(unittest.TestCase):
    def test_built_command_uses_resolved_profile_and_task_model(self) -> None:
        config = VibeConfig(
            repo=Path("."),
            agent_profiles={
                "opus": AgentConfig(
                    command="worker --model {model} {prompt}",
                    model="opus",
                )
            },
        )

        for task_model, expected_model in (("", "opus"), ("sonnet", "sonnet")):
            with self.subTest(task_model=task_model):
                task = Task(
                    task_id="TASK-01",
                    title="Task",
                    status="Next",
                    agent="opus",
                    model=task_model,
                )
                selection = resolve_task_agent(config, task)
                command = format_agent_command(
                    selection.config.require_command(),
                    prompt="inspect repo",
                    model=selection.config.model,
                    task=task,
                    profile=selection.profile,
                )

                self.assertEqual(
                    command,
                    f"worker --model {expected_model} {shell_quote('inspect repo')}",
                )

    def test_format_agent_command_substitutes_shell_quoted_model(self) -> None:
        command = format_agent_command(
            "worker --model {model} {prompt}",
            prompt="inspect repo",
            model="model with spaces",
            task_id="TASK-01",
            profile="opus",
        )

        self.assertEqual(
            command,
            f"worker --model {shell_quote('model with spaces')} "
            f"{shell_quote('inspect repo')}",
        )

    def test_format_agent_command_substitutes_shell_quoted_effort(self) -> None:
        command = format_agent_command(
            "worker --effort {effort} {prompt}",
            prompt="inspect repo",
            model=None,
            effort="high",
            task_id="TASK-01",
            profile="opus",
        )

        self.assertEqual(
            command,
            f"worker --effort high {shell_quote('inspect repo')}",
        )

    def test_format_agent_command_shell_quotes_task_and_run_ids(self) -> None:
        task_id = "TASK; $(command) 'quoted'\nspace &|<>()^%!"
        run_id = 'run "quoted"; command'

        command = format_agent_command(
            "worker --task {task_id} --run {run_id} {prompt}",
            prompt="inspect repo",
            model=None,
            task_id=task_id,
            run_id=run_id,
        )

        self.assertEqual(
            command,
            f"worker --task {shell_quote(task_id)} --run {shell_quote(run_id)} "
            f"{shell_quote('inspect repo')}",
        )

    def test_format_agent_command_preserves_windows_trailing_backslash(self) -> None:
        with patch("vibe_loop.config.sys.platform", "win32"):
            command = format_agent_command(
                "worker --task {task_id} --run {run_id} {prompt}",
                prompt="inspect repo",
                model=None,
                task_id="TASK-1\\",
                run_id="run-1",
            )

        self.assertEqual(
            command,
            'worker --task "TASK-1\\\\" --run "run-1" "inspect repo"',
        )

    def test_format_agent_command_rejects_unsafe_windows_nonprompt_value(
        self,
    ) -> None:
        with patch("vibe_loop.config.sys.platform", "win32"):
            with self.assertRaisesRegex(ValueError, "cmd.exe"):
                format_agent_command(
                    "worker --task {task_id} --run {run_id} {prompt}",
                    prompt='prompt may contain "quotes"',
                    model=None,
                    task_id='TASK" & calc & "X',
                    run_id="run-1",
                )

    def test_format_agent_command_rejects_unsafe_template_fields(self) -> None:
        templates = (
            "worker {unsupported}",
            "worker {task_id!r}",
            "worker {task_id:>10}",
            "worker {task_id",
        )

        for template in templates:
            with self.subTest(template=template):
                with self.assertRaises(ValueError):
                    format_agent_command(
                        template,
                        prompt="inspect repo",
                        model=None,
                        task_id="TASK-01",
                        run_id="run-1",
                    )

    def test_format_agent_command_without_model_field_is_unchanged(self) -> None:
        expected = f"worker {shell_quote('inspect repo')}"
        for model in (None, "opus"):
            with self.subTest(model=model):
                self.assertEqual(
                    format_agent_command(
                        "worker {prompt}",
                        prompt="inspect repo",
                        model=model,
                        task_id="TASK-01",
                        profile="opus",
                    ),
                    expected,
                )

    def test_run_task_model_field_without_resolved_model_does_not_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runner = VibeRunner(
                VibeConfig(
                    repo=repo,
                    agent_profiles={
                        "opus": AgentConfig(
                            command="worker --model {model} {prompt}",
                            prompt_dialect="codex",
                            skill_ref_prefix="$",
                        )
                    },
                )
            )
            task = Task(
                task_id="TASK-01",
                title="Task",
                status="Next",
                agent="opus",
            )

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.git_rev_parse", return_value="abc"):
                    with patch("vibe_loop.runner.run_streaming_command") as launch:
                        with self.assertRaisesRegex(
                            AgentResolutionError,
                            "task 'TASK-01'.*profile 'opus'.*no model",
                        ):
                            runner.run_task(task)

            launch.assert_not_called()


class SessionIdInjectionTests(unittest.TestCase):
    def test_codex_structured_output_injection_allows_global_options(self) -> None:
        self.assertEqual(
            inject_structured_usage_output(
                "codex --profile reviewer exec {prompt}", "codex"
            ),
            "codex --profile reviewer exec --json {prompt}",
        )
        self.assertEqual(
            inject_structured_usage_output(
                "MODE=review /usr/bin/codex --model gpt-5 exec {prompt}", "auto"
            ),
            "MODE=review /usr/bin/codex --model gpt-5 exec --json {prompt}",
        )

    def test_claude_structured_output_injection_completes_stream_contract(
        self,
    ) -> None:
        self.assertEqual(
            inject_structured_usage_output(
                "claude -p --output-format stream-json {prompt}",
                "claude",
            ),
            "claude --verbose -p --output-format stream-json {prompt}",
        )
        self.assertEqual(
            inject_structured_usage_output(
                "CLAUDE_HOME=.claude claude -p --output-format stream-json {prompt}",
                "claude",
            ),
            "CLAUDE_HOME=.claude claude --verbose -p "
            "--output-format stream-json {prompt}",
        )
        self.assertEqual(
            inject_structured_usage_output(
                "CLAUDE_HOME=.claude claude -p {prompt}",
                "claude",
            ),
            "CLAUDE_HOME=.claude claude --output-format stream-json "
            "--verbose -p {prompt}",
        )
        self.assertEqual(
            inject_structured_usage_output(
                "/usr/bin/claude -p {prompt}",
                "claude",
            ),
            "/usr/bin/claude --output-format stream-json --verbose -p {prompt}",
        )

    def test_claude_implementer_denies_nested_agent_and_task_tools(self) -> None:
        prepared = inject_claude_implementer_tool_denial(
            "CLAUDE_HOME=.claude claude -p {prompt}", "claude"
        )

        argv = shlex.split(prepared)
        denied = argv.index("--disallowedTools")
        self.assertEqual(argv[denied + 1], "Agent,Task")
        self.assertIn("{prompt}", argv)

    def test_claude_implementer_preserves_existing_tool_denials(self) -> None:
        prepared = inject_claude_implementer_tool_denial(
            "claude -p {prompt} --disallowedTools Edit Write", "auto"
        )

        argv = shlex.split(prepared)
        self.assertEqual(argv.count("--disallowedTools"), 1)
        denied = argv.index("--disallowedTools")
        self.assertEqual(argv[denied + 1 :], ["Agent,Task", "Edit", "Write"])

    def test_claude_implementer_preserves_shell_template_syntax(self) -> None:
        command = "claude -p {prompt} >> $VIBE_LOOP_LOG 2>&1"

        prepared = inject_claude_implementer_tool_denial(command, "claude")

        self.assertEqual(
            prepared,
            "claude --disallowedTools Agent,Task -p {prompt} >> $VIBE_LOOP_LOG 2>&1",
        )
        self.assertEqual(
            inject_claude_implementer_tool_denial(
                "claude -p {prompt} --add-dir ~/shared",
                "claude",
            ),
            "claude --disallowedTools Agent,Task -p {prompt} --add-dir ~/shared",
        )

    def test_claude_implementer_preserves_embedded_prompt_after_denials(self) -> None:
        command = 'claude -p --disallowedTools Edit "Task: {prompt}"'

        prepared = inject_claude_implementer_tool_denial(command, "claude")

        self.assertEqual(
            prepared,
            'claude -p --disallowedTools Agent,Task Edit "Task: {prompt}"',
        )

    def test_claude_launch_injections_keep_quoted_prompt_as_one_argument(self) -> None:
        command = inject_claude_session_id(
            'claude -p "Task: {prompt}"',
            "12345678-1234-1234-1234-123456789abc",
        )
        command = inject_claude_implementer_tool_denial(command, "claude")
        command = inject_structured_usage_output(command, "claude")
        command = format_agent_command(command, prompt="hello world", model=None)

        argv = shlex.split(command)

        prompt_arguments = [value for value in argv if "hello" in value]
        self.assertEqual(len(prompt_arguments), 1)
        self.assertTrue(prompt_arguments[0].startswith("Task: "))

    def test_claude_implementer_policy_does_not_change_codex_path(self) -> None:
        command = "codex exec {prompt}"

        self.assertEqual(
            inject_claude_implementer_tool_denial(command, "codex"),
            command,
        )

    def test_claude_implementer_disables_background_tasks_in_environment(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"},
        ):
            claude_env = worker_command_env(
                run_id="run-1",
                task_id="TASK-01",
                log_path=Path("/tmp/run.log"),
                agent_kind="claude",
                agent_profile="claude-opus",
                disable_background_tasks=True,
            )
            codex_env = worker_command_env(
                run_id="run-2",
                task_id="TASK-02",
                log_path=Path("/tmp/run.log"),
                agent_kind="codex",
                agent_profile="codex",
            )

        self.assertEqual(claude_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual(codex_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "0")

    def test_worker_owned_claude_keeps_background_tasks_available(self) -> None:
        with patch.dict(
            os.environ,
            {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"},
        ):
            environment = worker_command_env(
                run_id="run-1",
                task_id="TASK-01",
                log_path=Path("/tmp/run.log"),
                agent_kind="claude",
                agent_profile="claude-opus",
            )

        self.assertEqual(environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "0")

    def test_worker_process_does_not_observe_bound_selector_names(self) -> None:
        selector_name = "LOOPYARD_PROJECT"
        inherited_name = "WORKER_ENV_UNRELATED"
        with patch.dict(
            os.environ,
            {
                selector_name: "ambient-project",
                inherited_name: "preserved",
            },
        ):
            environment = worker_command_env(
                run_id="run-1",
                task_id="TASK-01",
                log_path=Path("/tmp/run.log"),
                agent_kind="codex",
                agent_profile="codex",
                bound_names=(selector_name,),
            )

        observed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({"
                "'selector': os.environ.get('LOOPYARD_PROJECT', 'absent'), "
                "'unrelated': os.environ.get('WORKER_ENV_UNRELATED')}))",
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            json.loads(observed.stdout),
            {"selector": "absent", "unrelated": "preserved"},
        )

    def test_worker_usage_provenance_is_allowlisted(self) -> None:
        report = WorkerReport(
            run_id="run-1",
            task_id="TASK-01",
            status="completed",
            metadata={"phase": "review", "work_kind": "review"},
        )
        malformed = dataclasses.replace(
            report,
            metadata={"phase": ["review"], "work_kind": "raw transcript"},
        )

        self.assertEqual(worker_usage_provenance(report), ("review", "review"))
        self.assertEqual(worker_usage_provenance(malformed), ("implementation", ""))

    def test_flexible_provider_selection_excludes_pinned_dispatches(self) -> None:
        task = Task(task_id="TASK-01", title="Telemetry", status="Ready")

        self.assertTrue(provider_selection_is_flexible(AgentConfig(), task))
        self.assertFalse(
            provider_selection_is_flexible(
                dataclasses.replace(AgentConfig(), agent_kind="codex"), task
            )
        )
        self.assertFalse(
            provider_selection_is_flexible(
                AgentConfig(), dataclasses.replace(task, model="gpt-pinned")
            )
        )

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

    def test_resolve_codex_home_prefers_inline_then_env_then_default(self) -> None:
        cwd = Path("/repo")
        self.assertEqual(
            resolve_codex_home("CODEX_HOME=/abs codex exec {prompt}", {}, cwd),
            Path("/abs"),
        )
        self.assertEqual(
            resolve_codex_home("CODEX_HOME=rel codex exec {prompt}", {}, cwd),
            Path("/repo/rel"),
        )
        self.assertEqual(
            resolve_codex_home("codex exec {prompt}", {"CODEX_HOME": "/env"}, cwd),
            Path("/env"),
        )
        self.assertEqual(
            resolve_codex_home("codex exec {prompt}", {}, cwd),
            Path.home() / ".codex",
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

    def test_resolve_codex_rollout_by_native_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            day = home / "sessions" / "2026" / "07" / "21"
            day.mkdir(parents=True)
            rollout = day / "rollout-2026-07-21T10-00-00-abc-123.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")

            self.assertEqual(resolve_codex_rollout("abc-123", home), rollout)
            self.assertIsNone(resolve_codex_rollout("missing", home))
            self.assertIsNone(resolve_codex_rollout("../abc-123", home))

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

    def test_run_context_payload_exposes_public_model_and_effort_aliases(self) -> None:
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
            runtime_context=AgentRuntimeContext(
                model_id="gpt-5.4",
                model_id_source="command_arg:-m",
                reasoning_effort="high",
                reasoning_effort_source="command_config:model_reasoning_effort",
            ),
        )

        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["effort"], "high")
        self.assertEqual(payload["trailer_context"]["effort"], "high")


class RecordingLockManager(LockManager):
    """Captures the lock metadata visible to the backend at each transition.

    A command lock backend finalizes external run provenance from the lock row
    it holds at release time, so these snapshots are what such a backend would
    actually observe.
    """

    def __init__(self, lock_root: Path) -> None:
        super().__init__(lock_root)
        self.events: list[tuple[str, dict[str, object]]] = []

    def update(self, task_lock, metadata):
        self.events.append(("update", dict(metadata)))
        return super().update(task_lock, metadata)

    def release(self, task_lock) -> None:
        task_id = str(task_lock.metadata.get("task_id") or "")
        # What a backend finalizes on is the stored lock row, not anything the
        # release call carries.
        self.events.append(("release", dict(self.status(task_id) or {})))
        super().release(task_lock)

    def outcome_at_release(self, task_id: str) -> str:
        for kind, metadata in self.events:
            if kind == "release" and metadata.get("task_id") == task_id:
                return str(metadata.get("outcome") or "")
        return ""


class SynchronizedHeartbeatLockManager(LockManager):
    """Runs a real ``LockManager.heartbeat`` across the settling update.

    The heartbeat is the production one: it reads the lock row, then writes
    that snapshot back with a fresh ``heartbeat_at``. Here its caller-level read
    is held before the settling update and its write released after, so it
    carries the genuinely stale pre-settlement pair - a stored
    ``outcome=unknown`` / ``classification=unknown`` written by an earlier
    unsettled publication - into a row the backend has already finalized as
    completed. The write path still merges against a re-read of the row, so
    stored precedence is what has to keep the outcome terminal.
    """

    HEARTBEAT_WAIT_SECONDS = 10.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._reads = 0
        self.read_done = threading.Event()
        self.settled = threading.Event()
        self.heartbeat_snapshot: dict[str, object] | None = None
        self.heartbeat_metadata: dict[str, object] | None = None
        self.heartbeat_error: BaseException | None = None
        self.injected = False
        self._heartbeat = threading.local()

    def _on_heartbeat_thread(self) -> bool:
        return getattr(self._heartbeat, "active", False)

    def _replay_heartbeat(self, task_lock) -> None:
        self._heartbeat.active = True
        try:
            refreshed = self.heartbeat(
                task_id=task_lock.task_id,
                run_id=str(task_lock.metadata.get("run_id") or ""),
                fencing_token=str(task_lock.metadata.get("fencing_token") or "")
                or None,
                heartbeat_at="2026-07-20T00:00:00Z",
            )
            self.heartbeat_metadata = dict(refreshed.metadata)
        except BaseException as exc:  # surfaced by the test, never swallowed
            self.heartbeat_error = exc

    def current_lock(self, task_id):
        current = super().current_lock(task_id)
        if self._on_heartbeat_thread():
            self._reads += 1
            if self._reads == 1:
                # Hold this view of the row until the settling update stored a
                # terminal outcome, so the write back is unambiguously stale.
                self.heartbeat_snapshot = dict(current.metadata)
                self.read_done.set()
                self.settled.wait(timeout=self.HEARTBEAT_WAIT_SECONDS)
        return current

    def update(self, task_lock, metadata):
        if self._on_heartbeat_thread() or self.injected:
            return super().update(task_lock, metadata)
        if (
            str(metadata.get("outcome") or "")
            not in locks_module.TERMINAL_LOCK_OUTCOMES
        ):
            return super().update(task_lock, metadata)
        self.injected = True
        # Model the row an earlier unsettled publication left behind. It is
        # written straight through the backend: the manager already knows the
        # outcome it is about to settle and would restore it.
        stored = self.current_lock(task_lock.task_id)
        unsettled = dict(stored.metadata)
        unsettled["outcome"] = "unknown"
        unsettled["classification"] = "unknown"
        self.backend.update(stored, unsettled)
        thread = threading.Thread(target=self._replay_heartbeat, args=(task_lock,))
        thread.start()
        try:
            self.read_done.wait(timeout=self.HEARTBEAT_WAIT_SECONDS)
            settled = super().update(task_lock, metadata)
        finally:
            self.settled.set()
            thread.join(timeout=self.HEARTBEAT_WAIT_SECONDS)
        return settled


class ParkedWriteBackend:
    """Lock backend that parks inside ``update`` once the caller has merged.

    Modelling the racing writer at the backend write - not at its read - is the
    point: by then it has already decided the exact row it intends to store, so
    nothing downstream of the merge can repair a stale outcome.
    """

    WAIT_SECONDS = 10.0

    def __init__(self, inner) -> None:
        self.inner = inner
        self.at_write = threading.Event()
        self.proceed = threading.Event()

    def acquire(self, task_id, run_id, metadata=None):
        return self.inner.acquire(task_id, run_id, metadata=metadata)

    def update(self, task_lock, metadata):
        self.at_write.set()
        self.proceed.wait(timeout=self.WAIT_SECONDS)
        return self.inner.update(task_lock, metadata)

    def release(self, task_lock) -> None:
        self.inner.release(task_lock)

    def status(self, task_id):
        return self.inner.status(task_id)

    def list_locks(self):
        return self.inner.list_locks()

    def path_for(self, task_id):
        return self.inner.path_for(task_id)


MUTEX_PROBE_SOURCE = """
import fcntl
import sys

with open(sys.argv[1], "a+b") as handle:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(3)
sys.exit(0)
"""


def settlement_mutex_is_free(mutex_path: Path) -> bool:
    """Report whether another process could take the settlement mutex now.

    The probe is a real separate process taking the real advisory lock, so it
    answers the only question that matters about the boundary - is it held
    while the racing writer is mid-update - without depending on elapsed time.
    """

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            MUTEX_PROBE_SOURCE,
            str(locks_module.metadata_lock_file_path(mutex_path)),
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode not in (0, 3):
        raise AssertionError(
            f"settlement mutex probe failed: rc={probe.returncode} {probe.stderr}"
        )
    return probe.returncode == 0


class ForeignHeartbeatLockManager(LockManager):
    """Settles while a heartbeat from another process is parked mid-update.

    The racing writer is a separate ``LockManager`` over its own backend
    instance, the way ``vibe-loop worker heartbeat`` runs it: the settling
    process shares no memory with it and cannot know what it merged. It is
    parked after reading the pre-settlement row and merging its stale
    ``unknown`` pair, immediately before the backend write - the window a
    backend without compare-and-swap cannot close by row precedence alone.
    """

    WAIT_SECONDS = 10.0

    def __init__(self, *args, writer_backend: ParkedWriteBackend, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.writer_backend = writer_backend
        self.writer_metadata: dict[str, object] | None = None
        self.writer_error: BaseException | None = None
        self.mutex_free_while_parked: bool | None = None
        self.mutex_free_after_write: bool | None = None
        self.injected = False

    def _run_writer(self, task_lock) -> None:
        writer = LockManager(self.lock_root, backend=self.writer_backend)
        try:
            refreshed = writer.heartbeat(
                task_id=task_lock.task_id,
                run_id=str(task_lock.metadata.get("run_id") or ""),
                fencing_token=str(task_lock.metadata.get("fencing_token") or "")
                or None,
                heartbeat_at="2026-07-20T00:00:00Z",
            )
            self.writer_metadata = dict(refreshed.metadata)
        except BaseException as exc:  # surfaced by the test, never swallowed
            self.writer_error = exc

    def update(self, task_lock, metadata):
        if (
            self.injected
            or str(metadata.get("outcome") or "")
            not in locks_module.TERMINAL_LOCK_OUTCOMES
        ):
            return super().update(task_lock, metadata)
        self.injected = True
        # The row an earlier unsettled publication left behind, written straight
        # through the backend so the settling manager's own gate does not see it.
        stored = self.current_lock(task_lock.task_id)
        unsettled = dict(stored.metadata)
        unsettled["outcome"] = "unknown"
        unsettled["classification"] = "unknown"
        self.backend.update(stored, unsettled)
        mutex_path = self.settlement_mutex_path(task_lock.task_id)
        writer = threading.Thread(target=self._run_writer, args=(task_lock,))
        writer.start()
        try:
            if not self.writer_backend.at_write.wait(timeout=self.WAIT_SECONDS):
                raise AssertionError(
                    "the foreign writer never reached its backend write"
                )
            # The writer has merged its stale pair and is one instruction from
            # storing it. Whether settlement can interleave is decided entirely
            # by whether the boundary is held right now, which another process
            # can answer outright.
            self.mutex_free_while_parked = settlement_mutex_is_free(mutex_path)
        finally:
            self.writer_backend.proceed.set()
            writer.join(timeout=self.WAIT_SECONDS)
        if writer.is_alive():
            raise AssertionError("the foreign writer never finished its update")
        self.mutex_free_after_write = settlement_mutex_is_free(mutex_path)
        # Settlement runs only after the stale write landed, which is the order
        # the boundary forces on the real supervisor.
        return super().update(task_lock, metadata)


class StubTaskSource:
    def __init__(self, tasks: list[Task], probe_results: dict[str, Task | None]):
        self._tasks = tasks
        self._probe_results = probe_results
        self._dispatched: set[str] = set()
        self.probe_calls: list[str] = []

    def list_tasks(self) -> list[Task]:
        # A dispatched task leaves the runnable set the way a real source does
        # once the worker moves it out of a runnable status.
        return [task for task in self._tasks if task.task_id not in self._dispatched]

    def probe(self, task_id: str) -> Task | None:
        self.probe_calls.append(task_id)
        return self._probe_results.get(task_id)

    def mark_dispatched(self, task_id: str) -> None:
        self._dispatched.add(task_id)


class RuntimeOwnedTaskSource(StubTaskSource):
    def __init__(self, task: Task) -> None:
        super().__init__([task], {task.task_id: task})
        self.status = "ready"
        self.activate_context: dict[str, str] = {}
        self.complete_context: dict[str, str] = {}
        self.settlement_context: dict[str, str] = {}

    def probe(self, task_id: str) -> Task:
        self.probe_calls.append(task_id)
        return Task(task_id=task_id, title="Task", status=self.status, agent="worker")

    def activate(
        self,
        task_id: str,
        run_id: str,
        *,
        continuation: bool = False,
        runtime_context: dict[str, str] | None = None,
    ) -> Task:
        self.activate_context = dict(runtime_context or {})
        self.status = "active"
        self.mark_dispatched(task_id)
        return self.probe(task_id)

    def complete(
        self,
        task_id: str,
        run_id: str,
        *,
        runtime_context: dict[str, str] | None = None,
    ) -> Task:
        self.complete_context = dict(runtime_context or {})
        self.status = "done"
        return self.probe(task_id)

    def reset(
        self,
        task_id: str,
        *,
        runtime_context: dict[str, str] | None = None,
    ) -> bool:
        self.settlement_context = dict(runtime_context or {})
        self.status = "ready"
        return True

    def park(
        self,
        task_id: str,
        run_id: str,
        *,
        runtime_context: dict[str, str] | None = None,
    ) -> Task:
        self.settlement_context = dict(runtime_context or {})
        self.status = "on-hold"
        return self.probe(task_id)


class SettledOutcomeFinalizationTests(unittest.TestCase):
    """Regression cover for completed runs finalizing as ``unknown``.

    A worker that files a completed report, has its task marked done and its
    lock released must settle as ``completed`` in the run record *and* in the
    lock state the backend sees at release, regardless of whether the enclosing
    run-until-done process immediately dispatches another task or goes idle.
    """

    def _build_runner(
        self,
        directory: str,
        tasks: list[Task],
        probe_results: dict[str, Task | None],
        supervision: SupervisionConfig | None = None,
    ) -> tuple[VibeRunner, RecordingLockManager, StubTaskSource]:
        repo = Path(directory)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Tester"],
            cwd=repo,
            check=True,
        )
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
        (repo / ".git" / "info" / "exclude").write_text(
            "/adapter.py\n/provenance_adapter.py\n/state/\n/wire.jsonl\n/wire.state\n",
            encoding="utf-8",
        )
        runner = VibeRunner(
            VibeConfig(
                repo=repo,
                agent=AgentConfig(
                    command="worker {prompt}",
                    prompt_dialect="codex",
                    skill_ref_prefix="$",
                ),
                agent_profiles={
                    "worker": AgentConfig(
                        command="worker {prompt}",
                        prompt_dialect="codex",
                        skill_ref_prefix="$",
                    )
                },
                orchestration=OrchestrationConfig(
                    mode="worker-owned",
                    explicit_keys=frozenset({"mode"}),
                ),
                supervision=supervision or SupervisionConfig(),
            )
        )
        lock_manager = RecordingLockManager(runner.config.state_path / "locks")
        runner._lock_manager = lock_manager
        source = StubTaskSource(tasks, probe_results)
        runner._source = source
        return runner, lock_manager, source

    def _reporting_worker(
        self,
        runner: VibeRunner,
        status: str,
        *,
        exit_code: int = 0,
        report: bool = True,
    ):
        def fake_run(command, cwd, log, **kwargs):
            env = kwargs.get("env") or {}
            if report:
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status=status,
                        message=f"{status} via worker report",
                    )
                )
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(os.getpid())
            return runner_module.StreamingCommandResult(exit_code=exit_code)

        return fake_run

    def _enable_runtime_owned_task_source(
        self,
        runner: VibeRunner,
        task: Task,
    ) -> RuntimeOwnedTaskSource:
        review_agent = AgentConfig(
            command="reviewer {prompt}",
            prompt_dialect="codex",
            skill_ref_prefix="$",
        )
        runner.config = dataclasses.replace(
            runner.config,
            agent_profiles={
                **runner.config.agent_profiles,
                "review": review_agent,
            },
            task_source=TaskSourceConfig(
                type="command",
                list_command="list",
                probe_command="probe {task_id}",
                activate_command="activate {task_id} {run_id}",
                complete_command="complete {task_id} {run_id}",
                reset_command="reset {task_id}",
                park_command="park {task_id} {run_id}",
                runnable_statuses=("ready",),
            ),
            orchestration=OrchestrationConfig(
                mode="runtime-owned",
                reviewer_profile="review",
                task_provenance_mode="adapter",
                explicit_keys=frozenset(
                    {"mode", "reviewer_profile", "task_provenance_mode"}
                ),
            ),
        )
        runner._source_resolution = None
        source = RuntimeOwnedTaskSource(task)
        runner._source = source
        return source

    def _record_runtime_integration(
        self,
        runner: VibeRunner,
        run_id: str,
        task_id: str,
    ) -> None:
        records = [
            record
            for record in runner.run_store.read_records()
            if record.get("run_id") == run_id and record.get("task_id") == task_id
        ]
        machine = RunLifecycleStateMachine.from_records(
            records,
            lambda transition: runner.run_store.append_lifecycle_event(
                RunLifecycleEvent.stage_transition(
                    run_id=run_id,
                    task_id=task_id,
                    transition=transition,
                )
            ),
        )
        for stage in (
            RunStage.CANDIDATE,
            RunStage.GATES,
            RunStage.REVIEW,
            RunStage.INTEGRATION,
        ):
            machine.transition(stage, reason="runtime_test_stage")
        runner.run_store.append_lifecycle_event(
            RunLifecycleEvent.integration_result(
                run_id=run_id,
                task_id=task_id,
                payload=IntegrationResult(
                    outcome="merged",
                    status="completed",
                    reason="",
                    branch=f"vibe-loop/{task_id}",
                    candidate_head="b" * 40,
                    refreshed_head="b" * 40,
                    main_before="a" * 40,
                    main_after="b" * 40,
                ).to_payload(),
            )
        )

    def _run_task(self, runner: VibeRunner, task: Task, fake_run) -> RunResult:
        with patch.object(runner, "ensure_spec_execution_gate"):
            with patch("vibe_loop.runner.run_streaming_command", fake_run):
                return runner.run_task(task)

    def test_completed_report_settles_before_lock_release(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, source = self._build_runner(
                directory, [task], {"T-1": done}
            )

            result = self._run_task(
                runner, task, self._reporting_worker(runner, "completed")
            )

            self.assertEqual(result.classification, "completed")
            # The backend that finalizes external run provenance at release
            # must already see the settled outcome, not infer one afterwards.
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "completed")

    def test_worker_owned_launch_uses_explicit_binding_without_overstripping(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        selector_name = "LOOPYARD_PROJECT"
        inherited_name = "WORKER_ENV_UNRELATED"
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            runner.config = dataclasses.replace(
                runner.config,
                project_binding=ProjectBindingConfig(
                    require=(selector_name,),
                    context=((selector_name, "configured-project"),),
                ),
            )
            reporting_worker = self._reporting_worker(runner, "completed")

            def observing_worker(command, cwd, log, **kwargs):
                observed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import json, os; print(json.dumps({"
                        "'selector': os.environ.get('LOOPYARD_PROJECT'), "
                        "'unrelated': os.environ.get('WORKER_ENV_UNRELATED')}))",
                    ],
                    env=kwargs["env"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    json.loads(observed.stdout),
                    {
                        "selector": "configured-project",
                        "unrelated": "preserved",
                    },
                )
                return reporting_worker(command, cwd, log, **kwargs)

            with patch.dict(
                os.environ,
                {
                    selector_name: "",
                    inherited_name: "preserved",
                },
            ):
                result = self._run_task(runner, task, observing_worker)

        self.assertEqual(result.classification, "completed")

    def test_completed_report_is_reclassified_when_main_is_not_upstream(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _source = self._build_runner(
                directory, [task], {"T-1": done}
            )
            remote = runner.config.repo / ".remote.git"
            with (runner.config.repo / ".git" / "info" / "exclude").open(
                "a",
                encoding="utf-8",
            ) as exclude:
                exclude.write("/.remote.git/\n")
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)],
                cwd=runner.config.repo,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            (runner.config.repo / "candidate.txt").write_text(
                "candidate\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "candidate.txt"],
                cwd=runner.config.repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "candidate"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            runner.config = dataclasses.replace(
                runner.config,
                autopilot=dataclasses.replace(
                    runner.config.autopilot,
                    require_upstream_sync=True,
                ),
            )

            result = self._run_task(
                runner, task, self._reporting_worker(runner, "completed")
            )

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.classification_source, "upstream_ahead")
        self.assertIn('"relation": "ahead"', result.message)
        self.assertEqual(lock_manager.outcome_at_release("T-1"), "blocked")

    def test_claude_worker_clean_exit_without_report_is_recorded_failed(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        active = dataclasses.replace(task, status="Active")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _source = self._build_runner(
                directory, [task], {"T-1": active}
            )
            claude = AgentConfig(
                agent_kind="auto",
                command="claude -p {prompt}",
                prompt_dialect="claude",
                skill_ref_prefix="/",
            )
            runner.config = dataclasses.replace(
                runner.config,
                agent=claude,
                agent_profiles={"worker": claude},
            )

            worker = self._reporting_worker(runner, "", report=False)

            def reportless_worker(command, cwd, log, **kwargs):
                self.assertNotIn("--disallowedTools", command)
                self.assertEqual(
                    kwargs["env"].get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
                    os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
                )
                return worker(command, cwd, log, **kwargs)

            result = self._run_task(
                runner,
                task,
                reportless_worker,
            )

            self.assertEqual(result.classification, "failed")
            self.assertEqual(result.classification_source, "worker_report_missing")
            self.assertIn("terminal worker report", result.message)
            self.assertIsNone(result.worker_report)
            self.assertIsNotNone(result.recovery_intent)
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "failed")

    def test_runtime_owned_claude_exit_without_report_continues_to_candidate(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _source = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)
            claude = AgentConfig(
                agent_kind="auto",
                command="claude -p {prompt}",
                prompt_dialect="claude",
                skill_ref_prefix="/",
            )
            runner.config = dataclasses.replace(
                runner.config,
                agent=claude,
                agent_profiles={
                    **runner.config.agent_profiles,
                    "worker": claude,
                },
                project_binding=ProjectBindingConfig(
                    require=("LOOPYARD_PROJECT",),
                    context=(("LOOPYARD_PROJECT", "configured-project"),),
                ),
            )

            def complete_runtime_lifecycle(**kwargs):
                self._record_runtime_integration(
                    runner,
                    kwargs["run_id"],
                    kwargs["task"].task_id,
                )
                return runner_module.ClassificationResult(
                    "completed",
                    "runtime_lifecycle",
                )

            worker = self._reporting_worker(runner, "", report=False)

            def reportless_worker(command, cwd, log, **kwargs):
                self.assertIn("--disallowedTools Agent,Task", command)
                self.assertEqual(
                    kwargs["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"],
                    "1",
                )
                self.assertNotIn("LOOPYARD_PROJECT", kwargs["env"])
                return worker(command, cwd, log, **kwargs)

            with patch.dict(os.environ, {"LOOPYARD_PROJECT": ""}):
                with patch.object(
                    runner,
                    "execute_runtime_owned_lifecycle",
                    side_effect=complete_runtime_lifecycle,
                ) as lifecycle:
                    result = self._run_task(
                        runner,
                        task,
                        reportless_worker,
                    )

            lifecycle.assert_called_once()
            self.assertEqual(result.classification, "completed")
            self.assertIsNone(result.worker_report)
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "completed")

    def test_explicit_worker_owned_mode_does_not_run_runtime_lifecycle(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            runner.config = dataclasses.replace(
                runner.config,
                completion=CompletionConfig(commands=("false",)),
                agent_profiles={
                    **runner.config.agent_profiles,
                    "review": AgentConfig(command="reviewer {prompt}"),
                },
                orchestration=OrchestrationConfig(
                    mode="worker-owned",
                    reviewer_profile="review",
                    gates=("completion.commands[0]",),
                    verify_on_main=("completion.commands[0]",),
                    explicit_keys=frozenset(
                        {"mode", "reviewer_profile", "gates", "verify_on_main"}
                    ),
                ),
            )

            result = self._run_task(
                runner,
                task,
                self._reporting_worker(runner, "completed"),
            )
            records = runner.run_store.read_records()

        self.assertEqual(result.classification, "completed")
        contract = next(
            record
            for record in records
            if record.get("record_type") == "run_contract_resolved"
        )
        self.assertEqual(contract["mode"], "worker-owned")
        self.assertTrue(
            {
                "candidate_recorded",
                "gate_result",
                "review_started",
                "review_verdict",
                "integration_result",
                "task_provenance_committed",
            }.isdisjoint(record.get("record_type") for record in records)
        )

    def test_legacy_journal_without_contract_or_stages_remains_worker_owned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [], {})
            runner.config = dataclasses.replace(
                runner.config,
                orchestration=OrchestrationConfig(),
            )
            runner.run_store.append_record(
                {
                    "record_type": "run_started",
                    "run_id": "legacy-run",
                    "task_id": "T-legacy",
                    "started_at": "2026-07-01T00:00:00+00:00",
                }
            )

            runtime_owned = runner._run_uses_runtime_owned_orchestration("legacy-run")
            records = runner.run_store.read_records()

        self.assertFalse(runtime_owned)
        self.assertNotIn(
            "stage_transition",
            {record.get("record_type") for record in records},
        )

    def test_runtime_owned_completion_commits_provenance_before_result(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def fake_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                run_id = env["VIBE_LOOP_RUN_ID"]
                task_id = env["VIBE_LOOP_TASK_ID"]
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                self._record_runtime_integration(runner, run_id, task_id)
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="completed",
                    )
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            result = self._run_task(runner, task, fake_run)

            records = runner.run_store.read_records()
            record_types = [record.get("record_type") for record in records]
            self.assertEqual(result.classification, "completed")
            self.assertLess(
                record_types.index("integration_result"),
                record_types.index("task_provenance_committed"),
            )
            self.assertLess(
                record_types.index("task_provenance_committed"),
                record_types.index("run_result"),
            )
            self.assertLess(
                record_types.index("run_result"),
                record_types.index("lock_released"),
            )
            self.assertEqual(source.complete_context["VIBE_LOOP_RUN_ID"], result.run_id)
            self.assertEqual(source.complete_context["VIBE_LOOP_TASK_ID"], "T-1")
            self.assertEqual(
                source.activate_context["VIBE_LOOP_PRIMARY_REPO"],
                str(runner.config.repo),
            )
            self.assertNotIn("VIBE_LOOP_REPO", source.activate_context)
            self.assertEqual(
                source.complete_context["VIBE_LOOP_PRIMARY_REPO"],
                str(runner.config.repo),
            )
            self.assertNotIn("VIBE_LOOP_REPO", source.complete_context)
            self.assertNotIn("VIBE_LOOP_WORKTREE", source.complete_context)
            release_metadata = next(
                metadata for kind, metadata in lock_manager.events if kind == "release"
            )
            self.assertEqual(
                source.complete_context["VIBE_LOOP_FENCING_TOKEN"],
                release_metadata["fencing_token"],
            )

    def test_runtime_owned_completion_failure_records_adapter_stderr(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)
            secret = "adapter-password"
            diagnostic = "completion refused because evidence is missing"
            noisy_line = "x" * (tasks_module.TASK_SOURCE_ERROR_RAW_WINDOW_LIMIT * 16)

            def refusing_complete(*args: object, **kwargs: object) -> Task:
                raise subprocess.CalledProcessError(
                    3,
                    "complete",
                    stderr=(
                        f"{noisy_line}\n{diagnostic}: "
                        f"postgres://user:{secret}@database/app\n"
                    ),
                )

            source.complete = refusing_complete  # type: ignore[method-assign]

            def fake_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                run_id = env["VIBE_LOOP_RUN_ID"]
                task_id = env["VIBE_LOOP_TASK_ID"]
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                self._record_runtime_integration(runner, run_id, task_id)
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="completed",
                    )
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            result = self._run_task(runner, task, fake_run)
            record = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "run_result"
            )

        message = str(record["message"])
        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.classification_source, "completion_adapter_failed")
        self.assertIn("CalledProcessError", message)
        self.assertIn(diagnostic, message)
        self.assertIn("postgres://user:<redacted>@database/app", message)
        self.assertNotIn(secret, message)
        self.assertNotIn(
            "x" * (tasks_module.TASK_SOURCE_ERROR_LINE_LIMIT + 1),
            message,
        )
        self.assertEqual(message.count(diagnostic), 1)

    def test_runtime_owned_result_append_failure_retains_fenced_lock(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)

            def fake_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                run_id = env["VIBE_LOOP_RUN_ID"]
                task_id = env["VIBE_LOOP_TASK_ID"]
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                self._record_runtime_integration(runner, run_id, task_id)
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="completed",
                    )
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            with patch.object(
                runner,
                "record_result",
                side_effect=OSError("run journal unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "run journal unavailable"):
                    self._run_task(runner, task, fake_run)

            record_types = [
                record.get("record_type") for record in runner.run_store.read_records()
            ]
            self.assertIn("task_provenance_committed", record_types)
            self.assertNotIn("run_result", record_types)
            self.assertNotIn("lock_released", record_types)
            self.assertTrue(lock_manager.is_locked("T-1"))

    def test_runtime_owned_failure_settles_before_result_and_release(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            result = self._run_task(
                runner,
                task,
                self._reporting_worker(runner, "blocked"),
            )

            records = runner.run_store.read_records()
            record_types = [record.get("record_type") for record in records]
            self.assertEqual(result.classification, "blocked")
            self.assertLess(
                record_types.index("task_source_settled"),
                record_types.index("run_result"),
            )
            self.assertLess(
                record_types.index("run_result"),
                record_types.index("lock_released"),
            )
            self.assertEqual(source.status, "on-hold")
            self.assertEqual(
                source.settlement_context["VIBE_LOOP_RUN_ID"], result.run_id
            )
            self.assertEqual(
                source.settlement_context["VIBE_LOOP_PRIMARY_REPO"],
                str(runner.config.repo),
            )
            self.assertNotIn("VIBE_LOOP_REPO", source.settlement_context)
            self.assertNotIn("VIBE_LOOP_WORKTREE", source.settlement_context)
            release_metadata = next(
                metadata for kind, metadata in lock_manager.events if kind == "release"
            )
            self.assertEqual(
                source.settlement_context["VIBE_LOOP_FENCING_TOKEN"],
                release_metadata["fencing_token"],
            )

    def test_malformed_review_exhaustion_parks_preserved_candidate(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def fail_after_gates(**kwargs):
                run_id = kwargs["run_id"]
                task_id = kwargs["task"].task_id
                runner.run_store.append_lifecycle_event(
                    RunLifecycleEvent.candidate_recorded(
                        run_id=run_id,
                        task_id=task_id,
                        payload={
                            "branch": "vibe-loop/T-1",
                            "worktree": str(kwargs["provisioned_workspace"].worktree),
                            "base_main": "a" * 40,
                            "head_commit": "b" * 40,
                            "changed_paths": ["candidate.txt"],
                            "fingerprint": "sha256:candidate",
                            "source": "derived",
                        },
                    )
                )
                runner.run_store.append_lifecycle_event(
                    RunLifecycleEvent.gate_result(
                        run_id=run_id,
                        task_id=task_id,
                        payload={
                            "config_key": "completion.commands[0]",
                            "exit_class": "passed",
                            "candidate_fingerprint": "sha256:candidate",
                        },
                    )
                )
                raise ReviewOutputMalformed(
                    "malformed review output: invalid verdict schema",
                    2,
                )

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=fail_after_gates,
            ):
                result = self._run_task(
                    runner,
                    task,
                    self._reporting_worker(runner, "completed"),
                )

            records = runner.run_store.read_records()
            candidate = next(
                record
                for record in records
                if record.get("record_type") == "candidate_recorded"
            )
            gate = next(
                record
                for record in records
                if record.get("record_type") == "gate_result"
            )

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.classification_source, "review_output_malformed")
        self.assertIn("candidate and passed gates preserved", result.message)
        self.assertEqual(source.status, "on-hold")
        self.assertEqual(lock_manager.outcome_at_release("T-1"), "blocked")
        self.assertEqual(candidate["head_commit"], "b" * 40)
        self.assertEqual(gate["exit_class"], "passed")

    def test_review_control_fence_parks_with_stable_classification(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=ReviewControlFenceError("review_verdict_findings"),
            ):
                result = self._run_task(
                    runner,
                    task,
                    self._reporting_worker(runner, "completed"),
                )

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(
            result.classification_source,
            "review_verdict_findings",
        )
        self.assertEqual(source.status, "on-hold")
        self.assertEqual(lock_manager.outcome_at_release("T-1"), "blocked")

    def test_runtime_owned_completion_without_integration_parks_as_blocked(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def completed_without_integration(**kwargs):
                stage_machine = kwargs["stage_machine"]
                for stage in (
                    RunStage.CANDIDATE,
                    RunStage.GATES,
                    RunStage.REVIEW,
                    RunStage.INTEGRATION,
                ):
                    stage_machine.transition(
                        stage,
                        reason="test_missing_integration_result",
                    )
                return runner_module.ClassificationResult(
                    "completed",
                    "runtime_lifecycle",
                )

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=completed_without_integration,
            ):
                result = self._run_task(
                    runner,
                    task,
                    self._reporting_worker(runner, "completed"),
                )

            record_types = [
                record.get("record_type") for record in runner.run_store.read_records()
            ]
            self.assertEqual(result.classification, "blocked")
            self.assertIn("durable completed integration_result", result.message)
            self.assertNotIn("task_provenance_committed", record_types)
            self.assertLess(
                record_types.index("task_source_settled"),
                record_types.index("run_result"),
            )
            self.assertEqual(source.status, "on-hold")

    def test_runtime_integration_recovery_rejects_identityless_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [], {})
            runner.run_store.append_lifecycle_event(
                RunLifecycleEvent.integration_result(
                    run_id="run-1",
                    task_id="T-1",
                    payload=IntegrationResult(
                        outcome="branch_already_merged",
                        status="completed",
                        reason="legacy",
                        branch="worker/T-1",
                        candidate_head="",
                        refreshed_head="",
                        main_before="",
                        main_after="",
                    ).to_payload(),
                )
            )

            result = runner._runtime_integration_result(
                run_id="run-1",
                task_id="T-1",
            )

        self.assertIsNone(result)

    def test_candidate_reanchor_retry_bound_parks_as_blocked(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=CandidateReanchorRetryExhausted(
                    attempts=2,
                    candidate_base="a" * 40,
                    observed_base="b" * 40,
                ),
            ):
                result = self._run_task(
                    runner,
                    task,
                    self._reporting_worker(runner, "completed"),
                )

        self.assertEqual(result.classification, "blocked")
        self.assertEqual(
            result.classification_source,
            "candidate_reanchor_retry_bound",
        )
        self.assertEqual(source.status, "on-hold")

    def test_adopted_workspace_older_than_main_reanchors_before_gates(self) -> None:
        """An adopted workspace base is older than `main` by design.

        The provisioner only requires the workspace base to be an ancestor of
        the selected base, so the candidate must reach the re-anchorer instead
        of being rejected during collection for not descending from run-start
        `main`.
        """

        class GatesReached(RuntimeError):
            pass

        def git_at(cwd: Path, *args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        observed_candidates: list[CandidateRecord] = []
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)
            source.status = "active"
            repo = runner.config.repo
            workspace_base = git_at(repo, "rev-parse", "HEAD")
            with tempfile.TemporaryDirectory() as workspace_directory:
                worktree = Path(workspace_directory) / "T-1"
                git_at(
                    repo,
                    "worktree",
                    "add",
                    "-b",
                    "vibe-loop/T-1",
                    str(worktree),
                    workspace_base,
                )
                (worktree / "candidate.txt").write_text(
                    "candidate\n",
                    encoding="utf-8",
                )
                git_at(worktree, "add", "candidate.txt")
                git_at(worktree, "commit", "-m", "candidate")
                candidate_head = git_at(worktree, "rev-parse", "HEAD")
                (repo / "main.txt").write_text("advanced\n", encoding="utf-8")
                git_at(repo, "add", "main.txt")
                git_at(repo, "commit", "-m", "advance main")
                advanced_main = git_at(repo, "rev-parse", "HEAD")

                workspace = ProvisionedWorkspace(
                    mode="adopted",
                    branch="vibe-loop/T-1",
                    worktree=worktree,
                    base_commit=workspace_base,
                    head_commit=candidate_head,
                    owner_run_id="run-0",
                )
                stage_machine = RunLifecycleStateMachine(lambda transition: None)
                stage_machine.transition(RunStage.ACTIVATION, reason="test")
                stage_machine.transition(RunStage.WORKSPACE, reason="test")
                stage_machine.transition(RunStage.IMPLEMENTING, reason="test")
                log_path = repo / "worker.log"
                log_path.write_text("", encoding="utf-8")

                class StubGateController:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                    def run(
                        self,
                        candidate: CandidateRecord | None = None,
                    ) -> None:
                        observed_candidates.append(candidate)
                        raise GatesReached("gates reached")

                with patch.object(
                    runner_module,
                    "RuntimeGateController",
                    StubGateController,
                ):
                    with self.assertRaises(GatesReached):
                        runner.execute_runtime_owned_lifecycle(
                            task=task,
                            run_id="run-1",
                            provisioned_workspace=workspace,
                            stage_machine=stage_machine,
                            contract={
                                "candidate_stabilization": {"max_reanchors": 2},
                                "gates": (),
                                "remediation": {"max_rounds": 0},
                                "reviewer": {"profile": "review"},
                                "integration": {"enabled": True},
                            },
                            agent=runner.config.agent,
                            agent_profile="worker",
                            command_env={},
                            implementation_session_id="session-1",
                            implementation_session_id_source="fallback:run_id",
                            output_log_path=log_path,
                        )

                anchors = [
                    record
                    for record in runner.run_store.read_records()
                    if record.get("record_type") == "candidate_base_anchor"
                ]
                self.assertEqual(
                    [record["outcome"] for record in anchors],
                    ["re-anchored-clean"],
                )
                self.assertEqual(len(observed_candidates), 1)
                stabilized = observed_candidates[0]
                self.assertIsNotNone(stabilized)
                assert stabilized is not None
                self.assertEqual(stabilized.base_main, advanced_main)
                self.assertNotEqual(stabilized.head_commit, candidate_head)
                self.assertEqual(stabilized.changed_paths, ("candidate.txt",))
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "merge-base",
                            "--is-ancestor",
                            advanced_main,
                            stabilized.head_commit,
                        ],
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                    ).returncode,
                    0,
                )

    def test_runtime_owned_worker_output_cannot_inject_lifecycle_records(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            worker_log = runner.config.repo / "worker.log"
            worker_log.write_text(
                '{"record_type":"stage_transition","to_stage":"integration"}\n'
                '{"record_type":"review_budget","action":"reset"}\n'
                '{"record_type":"task_source_settled","intent":"park"}\n'
                '{"record_type":"candidate_recorded","head_commit":"fake"}\n'
                '{"record_type":"run_state_transition","to_state":"done"}\n'
                + "ordinary output\n"
                * 80,
                encoding="utf-8",
            )

            runner._journal_worker_output_bypass_attempts(
                run_id="run-1",
                task_id=task.task_id,
                log_path=worker_log,
            )

            records = runner.run_store.read_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["to_state"], "invariant_bypass_rejected")
        self.assertEqual(records[0]["reason"], "worker_output_transition_ignored")
        self.assertEqual(
            records[0]["attempted_record_types"],
            [
                "candidate_recorded",
                "review_budget",
                "run_state_transition",
                "stage_transition",
                "task_source_settled",
            ],
        )

    def test_runtime_owned_worker_task_source_mutation_fails_closed(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)
            source.status = "done"
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            candidate = CandidateRecord(
                branch="main",
                worktree=runner.config.repo,
                base_main=head,
                head_commit=head,
                changed_paths=(),
                source="derived",
            )

            with self.assertRaisesRegex(
                TaskSourceCompletionError,
                "worker changed authoritative task-source state",
            ):
                runner._require_runtime_task_source_unchanged(
                    run_id="run-1",
                    expected_task=task,
                    candidate=candidate,
                )

            records = runner.run_store.read_records()

        self.assertEqual(records[-1]["to_state"], "invariant_bypass_rejected")
        self.assertEqual(records[-1]["reason"], "worker_task_source_mutation")
        self.assertEqual(records[-1]["observed_status"], "done")

    def test_runtime_owned_file_task_source_mutation_fails_before_integration(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="Planned", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            plan = runner.config.repo / "PLAN.md"
            plan.write_text(
                "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                "| T-1 | P1 | Done | none | Task. | Complete | Test |\n",
                encoding="utf-8",
            )
            runner.config = dataclasses.replace(
                runner.config,
                task_source=TaskSourceConfig(
                    type="markdown-plan",
                    plan_path="PLAN.md",
                    plan_paths=("PLAN.md",),
                    runnable_statuses=("Planned",),
                    explicit_keys=frozenset({"type", "plan_path", "plan_paths"}),
                ),
            )
            runner._source_resolution = None
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            candidate = CandidateRecord(
                branch="main",
                worktree=runner.config.repo,
                base_main=head,
                head_commit=head,
                changed_paths=("PLAN.md",),
                source="derived",
            )

            with self.assertRaisesRegex(
                TaskSourceCompletionError,
                "worker changed authoritative task-source state",
            ):
                runner._require_runtime_task_source_unchanged(
                    run_id="run-1",
                    expected_task=task,
                    candidate=candidate,
                )

            records = runner.run_store.read_records()

        self.assertEqual(records[-1]["reason"], "worker_task_source_mutation")
        self.assertEqual(records[-1]["observed_status"], "Done")

    def test_runtime_owned_run_executes_candidate_gates_review_and_integration(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            reviewer = runner.config.repo / "reviewer.py"
            reviewer.write_text(
                "import json\n"
                "import sys\n"
                'if \'"exit_class": "passed"\' not in sys.argv[-1]:\n'
                "    raise SystemExit('review launched before gate terminal evidence')\n"
                "print(json.dumps({\n"
                "    'verdict': 'approve', 'findings': [],\n"
                "    'session_id': '', 'session_id_source': '',\n"
                "}))\n",
                encoding="utf-8",
            )
            gate = runner.config.repo / "gate.py"
            gate.write_text(
                "import time\ntime.sleep(0.15)\nprint('slow gate passed')\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "reviewer.py", "gate.py"],
                cwd=runner.config.repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add reviewer fixture"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            source = self._enable_runtime_owned_task_source(runner, task)
            review_agent = AgentConfig(
                command=f"{sys.executable} {reviewer} {{prompt}}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner.config = dataclasses.replace(
                runner.config,
                completion=CompletionConfig(commands=(f"{sys.executable} {gate}",)),
                agent_profiles={
                    **runner.config.agent_profiles,
                    "review": review_agent,
                },
                orchestration=OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    gates=("completion.commands[0]",),
                    verify_on_main=("completion.commands[0]",),
                    task_provenance_mode="adapter",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "gates",
                            "verify_on_main",
                            "task_provenance_mode",
                        }
                    ),
                ),
            )

            worker_calls = 0

            def implementing_worker(command, cwd, log, **kwargs):
                nonlocal worker_calls
                worker_calls += 1
                kwargs["on_start"](os.getpid())
                (cwd / "candidate.txt").write_text("candidate\n", encoding="utf-8")
                subprocess.run(["git", "add", "candidate.txt"], cwd=cwd, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "implement candidate"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            holder = runner.lock_manager.acquire_main_integration(
                task_id="T-2",
                run_id="run-holder",
                metadata={"pid": os.getpid()},
            )
            original_wait = runner.lock_manager.acquire_main_integration_with_wait
            integration_lock_attempts = 0

            def release_after_timeout(**kwargs):
                nonlocal integration_lock_attempts
                integration_lock_attempts += 1
                if integration_lock_attempts == 1:
                    kwargs["timeout_seconds"] = 0
                    lock_result = original_wait(**kwargs)
                    runner.lock_manager.release(holder)
                    return lock_result
                return original_wait(**kwargs)

            with patch.object(
                runner.lock_manager,
                "acquire_main_integration_with_wait",
                side_effect=release_after_timeout,
            ):
                result = self._run_task(runner, task, implementing_worker)
            records = runner.run_store.read_records()
            record_types = [record.get("record_type") for record in records]
            candidate_text = (runner.config.repo / "candidate.txt").read_text(
                encoding="utf-8"
            )
            task_recovery_records = [
                record
                for record in records
                if record.get("record_type") == "task_recovery"
            ]
            provenance_records = [
                record
                for record in records
                if record.get("record_type") == "task_provenance_committed"
            ]

        self.assertEqual(result.classification, "completed")
        self.assertEqual(source.status, "done")
        self.assertEqual(candidate_text, "candidate\n")
        self.assertEqual(task_recovery_records, [])
        self.assertEqual(len(provenance_records), 1)
        self.assertEqual(worker_calls, 1)
        self.assertEqual(integration_lock_attempts, 2)
        self.assertEqual(
            sum(record.get("record_type") == "review_started" for record in records),
            1,
        )
        timeout_record = next(
            record
            for record in records
            if record.get("record_type") == "integration_result"
            and record.get("reason") == "lock_timeout"
        )
        self.assertEqual(
            timeout_record["diagnostics"]["holder_task_id"],
            "T-2",
        )
        self.assertEqual(
            timeout_record["diagnostics"]["holder_run_id"],
            "run-holder",
        )
        self.assertEqual(provenance_records[0]["mode"], "adapter")
        self.assertEqual(provenance_records[0]["confirmed_status"], "done")
        for required in (
            "candidate_recorded",
            "gate_result",
            "review_started",
            "review_verdict",
            "integration_result",
            "task_provenance_committed",
        ):
            self.assertIn(required, record_types)
        self.assertLess(
            record_types.index("gate_result"),
            record_types.index("review_started"),
        )
        self.assertLess(
            record_types.index("review_verdict"),
            record_types.index("integration_result"),
        )
        self.assertLess(
            record_types.index("integration_result"),
            record_types.index("task_provenance_committed"),
        )

    def test_runtime_owned_conflict_returns_to_implementer_in_same_run(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            reviewer = runner.config.repo / "reviewer.py"
            reviewer.write_text(
                "import json\n"
                "import subprocess\n"
                "from pathlib import Path\n"
                f"repo = Path({str(runner.config.repo)!r})\n"
                "(repo / 'README.md').write_text('main advance\\n', "
                "encoding='utf-8')\n"
                "subprocess.run(['git', 'add', 'README.md'], cwd=repo, check=True)\n"
                "subprocess.run(['git', 'commit', '-m', 'advance main during "
                "review'], cwd=repo, check=True, capture_output=True, text=True)\n"
                "print(json.dumps({\n"
                "    'verdict': 'approve', 'findings': [],\n"
                "    'session_id': '', 'session_id_source': '',\n"
                "}))\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "reviewer.py"],
                cwd=runner.config.repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add conflict reviewer fixture"],
                cwd=runner.config.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            source = self._enable_runtime_owned_task_source(runner, task)
            review_agent = AgentConfig(
                command=f"{sys.executable} {reviewer} {{prompt}}",
                agent_kind="custom",
                prompt_dialect="codex",
                skill_ref_prefix="$",
            )
            runner.config = dataclasses.replace(
                runner.config,
                completion=CompletionConfig(commands=("true",)),
                agent_profiles={
                    **runner.config.agent_profiles,
                    "review": review_agent,
                },
                orchestration=OrchestrationConfig(
                    mode="runtime-owned",
                    reviewer_profile="review",
                    gates=("completion.commands[0]",),
                    verify_on_main=("completion.commands[0]",),
                    task_provenance_mode="adapter",
                    explicit_keys=frozenset(
                        {
                            "mode",
                            "reviewer_profile",
                            "gates",
                            "verify_on_main",
                            "task_provenance_mode",
                        }
                    ),
                ),
            )
            worker_calls = 0

            def implementing_worker(command, cwd, log, **kwargs):
                nonlocal worker_calls
                worker_calls += 1
                if "on_start" in kwargs:
                    kwargs["on_start"](os.getpid())
                    content = "approved candidate\n"
                    message = "implement approved candidate"
                else:
                    self.assertIn("integration_conflict_resolution", command)
                    self.assertIn("README.md", command)
                    content = "resolved approved candidate and main\n"
                    message = "resolve integration conflict"
                (cwd / "README.md").write_text(content, encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], cwd=cwd, check=True)
                subprocess.run(
                    ["git", "commit", "-m", message],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            result = self._run_task(runner, task, implementing_worker)
            records = runner.run_store.read_records()
            integration = next(
                record
                for record in records
                if record.get("record_type") == "integration_result"
                and record.get("status") == "completed"
            )
            main_text = (runner.config.repo / "README.md").read_text(encoding="utf-8")

        self.assertEqual(result.classification, "completed")
        self.assertEqual(source.status, "done")
        self.assertEqual(worker_calls, 2)
        self.assertEqual(main_text, "resolved approved candidate and main\n")
        self.assertTrue(integration["diagnostics"]["approved_candidate"])
        self.assertTrue(integration["diagnostics"]["resolution_commit_valid"])
        self.assertEqual(
            sum(record.get("record_type") == "review_started" for record in records),
            1,
        )

    def test_runtime_owned_lifecycle_continues_after_verified_post_report_teardown(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)

            def enforced_worker(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="completed",
                    )
                )
                kwargs["on_start"](os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGTERM,
                    post_report=runner_module.PostReportActivity(
                        reported=True,
                        seconds=0.5,
                        activity_kind="tool_call",
                        activity_count=1,
                        enforced_stop=True,
                        identity_verified=True,
                        usage=runner_module.unavailable_usage(
                            "anthropic", "test_fixture"
                        ),
                    ),
                )

            def complete_lifecycle(**kwargs):
                self._record_runtime_integration(
                    runner,
                    kwargs["run_id"],
                    kwargs["task"].task_id,
                )
                return runner_module.ClassificationResult(
                    "completed", "runtime_lifecycle"
                )

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=complete_lifecycle,
            ) as lifecycle:
                result = self._run_task(runner, task, enforced_worker)
            activity = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "post_report_activity"
            )

        self.assertEqual(result.classification, "completed")
        self.assertEqual(result.exit_code, -signal.SIGTERM)
        lifecycle.assert_called_once()
        self.assertEqual(activity["runtime_lifecycle_decision"], "continue")
        self.assertEqual(
            activity["runtime_lifecycle_reason"],
            "verified_runtime_enforced_teardown",
        )

    def test_runtime_owned_accepts_reported_candidate_for_immediate_closure(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)

            def closed_worker(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                candidate = CandidateRecord(
                    branch=branch,
                    worktree=cwd,
                    base_main=head,
                    head_commit=head,
                    changed_paths=(),
                    source="worker_command",
                )
                runner.run_store.append_lifecycle_event(
                    RunLifecycleEvent.candidate_recorded(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        payload=candidate.to_payload(),
                    )
                )
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="completed",
                        commit=head,
                    )
                )
                self.assertTrue(kwargs["reap_check"]())
                self.assertEqual(
                    kwargs["post_report_closure_check"](),
                    "accepted_completed_candidate",
                )
                kwargs["on_start"](os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGTERM,
                    post_report=runner_module.PostReportActivity(
                        reported=True,
                        seconds=0.1,
                        activity_kind="",
                        activity_count=0,
                        enforced_stop=True,
                        identity_verified=True,
                        usage=runner_module.unavailable_usage(
                            "anthropic", "test_fixture"
                        ),
                        teardown_reason="accepted_report_runtime_closure",
                        descendants_verified=True,
                        teardown_process_count=2,
                        teardown_seconds=0.02,
                    ),
                )

            def complete_lifecycle(**kwargs):
                self._record_runtime_integration(
                    runner,
                    kwargs["run_id"],
                    kwargs["task"].task_id,
                )
                return runner_module.ClassificationResult(
                    "completed", "runtime_lifecycle"
                )

            with patch.object(
                runner,
                "execute_runtime_owned_lifecycle",
                side_effect=complete_lifecycle,
            ) as lifecycle:
                result = self._run_task(runner, task, closed_worker)
            closure = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "post_report_closure"
            )

        self.assertEqual(result.classification, "completed")
        lifecycle.assert_called_once()
        self.assertTrue(closure["terminated"])
        self.assertTrue(closure["identity_verified"])
        self.assertTrue(closure["descendants_verified"])
        self.assertEqual(closure["teardown_process_count"], 2)
        self.assertEqual(
            closure["runtime_lifecycle_reason"],
            "verified_accepted_report_runtime_closure",
        )

    def test_runtime_owned_lifecycle_refuses_unverified_post_report_exit(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)

            def externally_stopped_worker(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="completed",
                    )
                )
                kwargs["on_start"](os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGTERM,
                    post_report=runner_module.PostReportActivity(
                        reported=True,
                        seconds=0.5,
                        activity_kind="tool_call",
                        activity_count=1,
                        enforced_stop=False,
                        identity_verified=False,
                        usage=runner_module.unavailable_usage(
                            "anthropic", "test_fixture"
                        ),
                    ),
                )

            with patch.object(runner, "execute_runtime_owned_lifecycle") as lifecycle:
                result = self._run_task(runner, task, externally_stopped_worker)
            activity = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "post_report_activity"
            )

        lifecycle.assert_not_called()
        self.assertEqual(result.classification, "blocked")
        self.assertEqual(activity["runtime_lifecycle_decision"], "refuse")
        self.assertEqual(
            activity["runtime_lifecycle_reason"],
            "teardown_not_runtime_enforced",
        )

    def test_post_report_candidate_is_revalidated_before_runtime_gates(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {})
            self._enable_runtime_owned_task_source(runner, task)

            def invalidating_worker(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                candidate = CandidateRecord(
                    branch=branch,
                    worktree=cwd,
                    base_main=head,
                    head_commit=head,
                    changed_paths=(),
                    source="derived",
                )
                runner.run_store.append_lifecycle_event(
                    RunLifecycleEvent.candidate_recorded(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        payload=candidate.to_payload(),
                    )
                )
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="completed",
                    )
                )
                self.assertTrue(kwargs["reap_check"]())
                kwargs["on_start"](os.getpid())
                (cwd / "README.md").write_text(
                    "post-report mutation\n", encoding="utf-8"
                )
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGTERM,
                    post_report=runner_module.PostReportActivity(
                        reported=True,
                        seconds=0.5,
                        activity_kind="tool_call",
                        activity_count=1,
                        enforced_stop=True,
                        identity_verified=True,
                        usage=runner_module.unavailable_usage(
                            "anthropic", "test_fixture"
                        ),
                    ),
                )

            result = self._run_task(runner, task, invalidating_worker)
            records = runner.run_store.read_records()
            record_types = {record.get("record_type") for record in records}
            candidate_records = [
                record
                for record in records
                if record.get("record_type") == "candidate_recorded"
            ]

        self.assertEqual(result.classification, "failed")
        self.assertEqual(result.classification_source, "runtime_stage_failed")
        self.assertIn("candidate no longer matches", result.message)
        self.assertEqual(len(candidate_records), 1)
        self.assertNotIn("gate_result", record_types)
        self.assertNotIn("review_started", record_types)

    def test_runtime_owned_activation_crash_releases_lock_before_first_attempt(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def crashing_activate(*args, **kwargs):
                source.status = "active"
                raise OSError("activation response lost")

            source.activate = crashing_activate  # type: ignore[method-assign]

            with self.assertRaises(TaskActivationError):
                self._run_task(
                    runner,
                    task,
                    self._reporting_worker(runner, "completed"),
                )

            records = runner.run_store.read_records()
            self.assertFalse(lock_manager.is_locked("T-1"))
            self.assertEqual(source.status, "ready")
            record_types = [record.get("record_type") for record in records]
            self.assertLess(
                record_types.index("lock_released"),
                record_types.index("task_source_settled"),
            )
            stale = collect_stale_locks(
                lock_manager,
                runner.run_store,
                current_host=socket.gethostname(),
                process_exists=lambda pid: False,
            )
            clean_result = clean_stale_locks(stale, lock_manager)
            self.assertEqual(stale, [])
            self.assertEqual(clean_result.cleaned, [])
            self.assertFalse(lock_manager.is_locked("T-1"))

    def test_recovery_prelaunch_exits_leave_no_task_lock(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")

        def recovery_context(repo: Path) -> RecoveryContext:
            return RecoveryContext(
                task_id=task.task_id,
                prior_run_id="prior-run",
                prior_classification="unknown",
                branch="",
                worktree="",
                head_commit="",
                transcript_path="",
                wrapper_log=str(repo / "prior-run.log"),
                attempt=1,
                max_attempts=3,
                workspace_claimed=False,
            )

        for exit_path in (
            "probe_failure",
            "task_absent",
            "attempt_circuit_open",
            "budget_denial",
        ):
            with (
                self.subTest(exit_path=exit_path),
                tempfile.TemporaryDirectory() as directory,
            ):
                runner, lock_manager, _ = self._build_runner(directory, [task], {})
                source = self._enable_runtime_owned_task_source(runner, task)
                recovery = recovery_context(Path(directory))

                if exit_path == "probe_failure":

                    def failing_probe(task_id: str) -> Task:
                        raise OSError("task source unavailable")

                    source.probe = failing_probe  # type: ignore[method-assign]
                    result = runner.resume_pending_recovery(recovery)
                elif exit_path == "task_absent":
                    runner._source = StubTaskSource([], {})
                    result = runner.resume_pending_recovery(recovery)
                elif exit_path == "attempt_circuit_open":

                    def open_circuit(*, inputs, threshold, **kwargs):
                        return AttemptCircuitState(
                            task_id=inputs.task_id,
                            inputs=inputs,
                            threshold=threshold,
                            attempt_count=threshold,
                        )

                    with patch.object(
                        runner.run_store,
                        "reserve_attempt_circuit",
                        side_effect=open_circuit,
                    ):
                        result = runner.resume_pending_recovery(recovery)
                else:
                    runner.config = dataclasses.replace(
                        runner.config,
                        budget=BudgetConfig(enabled=True),
                    )
                    denial = BudgetDecision(
                        admitted=False,
                        decision="block",
                        phase="implementation",
                        binding=(
                            {
                                "selector": {},
                                "remaining": 0.0,
                                "declared": 1.0,
                            },
                        ),
                    )
                    with patch.object(
                        runner.budget_store,
                        "reserve",
                        return_value=denial,
                    ):
                        result = runner.resume_pending_recovery(recovery)

                self.assertIsNone(result)
                self.assertFalse(lock_manager.is_locked(task.task_id))

    def test_runtime_owned_recovery_stale_workspace_releases_task_lock(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)
            repo = runner.config.repo
            old_base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            prior_lock = lock_manager.acquire(
                task.task_id,
                "prior-run",
                metadata={
                    "task_id": task.task_id,
                    "run_id": "prior-run",
                    "base_main": old_base,
                    "started_at": "2026-07-26T00:00:00+00:00",
                },
            )
            workspace = WorkspaceProvisioner(
                repo=repo,
                main_branch="main",
                lock_manager=lock_manager,
                run_store=runner.run_store,
            ).provision(
                task_id=task.task_id,
                run_id="prior-run",
                base_commit=old_base,
                fencing_token=str(prior_lock.metadata["fencing_token"]),
            )
            (workspace.worktree / "candidate.txt").write_text(
                "interrupted candidate\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "candidate.txt"],
                cwd=workspace.worktree,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "interrupted candidate"],
                cwd=workspace.worktree,
                check=True,
                capture_output=True,
                text=True,
            )
            stale_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace.worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            lock_manager.release(lock_manager.current_lock(task.task_id))
            (repo / "current-base.txt").write_text("new base\n", encoding="utf-8")
            subprocess.run(["git", "add", "current-base.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            dirty_snapshot, dirty_fingerprint = git_dirty_snapshot(workspace.worktree)
            git_common_dir = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=workspace.worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            recovery = RecoveryContext(
                task_id=task.task_id,
                prior_run_id="prior-run",
                prior_classification="unknown",
                branch=workspace.branch,
                worktree=str(workspace.worktree),
                head_commit=stale_head,
                transcript_path="",
                wrapper_log=str(repo / "prior-run.log"),
                attempt=1,
                max_attempts=3,
                workspace_claimed=True,
                base_commit=old_base,
                git_common_dir=str((workspace.worktree / git_common_dir).resolve()),
                dirty_snapshot=tuple(dirty_snapshot),
                dirty_fingerprint=dirty_fingerprint,
            )

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command") as launch:
                    result = runner.resume_pending_recovery(recovery)

            self.assertIsNone(result)
            launch.assert_not_called()
            records = runner.run_store.read_records()
            record_types = [record.get("record_type") for record in records]
            settlement = next(
                record
                for record in records
                if record.get("record_type") == "task_source_settled"
            )
            self.assertEqual(source.status, "ready")
            self.assertFalse(lock_manager.is_locked(task.task_id))
            self.assertEqual(settlement["intent"], "requeue")
            self.assertEqual(settlement["confirmed_status"], "ready")
            self.assertTrue(settlement["recovered"])
            self.assertLess(
                record_types.index("lock_released"),
                record_types.index("task_source_settled"),
            )

    def test_prelaunch_requeue_failure_still_releases_task_lock(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def failing_reset(*args, **kwargs) -> bool:
                raise OSError("task source reset unavailable")

            source.reset = failing_reset  # type: ignore[method-assign]
            recovery = RecoveryContext(
                task_id=task.task_id,
                prior_run_id="prior-run",
                prior_classification="unknown",
                branch="",
                worktree="",
                head_commit="",
                transcript_path="",
                wrapper_log=str(runner.config.repo / "prior-run.log"),
                attempt=1,
                max_attempts=3,
                workspace_claimed=False,
            )

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch.object(
                    WorkspaceProvisioner,
                    "provision",
                    side_effect=WorkspaceProvisionError(
                        "workspace_stale_current_base",
                        "existing workspace does not contain the selected current base",
                    ),
                ):
                    result = runner.resume_pending_recovery(recovery)

            attempted = next(
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "task_source_settlement_attempted"
            )
            self.assertIsNone(result)
            self.assertEqual(source.status, "active")
            self.assertFalse(lock_manager.is_locked(task.task_id))
            self.assertEqual(attempted["intent"], "requeue")
            self.assertEqual(attempted["phase"], "post_release")
            self.assertTrue(attempted["settlement_pending"])
            self.assertTrue(attempted["recovery_command"])

    def test_runtime_owned_requeue_reset_receives_live_fencing_context(self) -> None:
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)

            def timed_out_worker(command, cwd, log, **kwargs):
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGKILL,
                    timed_out=True,
                )

            result = self._run_task(runner, task, timed_out_worker)

            release_metadata = next(
                metadata for kind, metadata in lock_manager.events if kind == "release"
            )
            self.assertEqual(result.classification, "timed_out")
            self.assertEqual(source.status, "ready")
            self.assertEqual(
                source.settlement_context["VIBE_LOOP_RUN_ID"], result.run_id
            )
            self.assertEqual(source.settlement_context["VIBE_LOOP_TASK_ID"], "T-1")
            self.assertEqual(
                source.settlement_context["VIBE_LOOP_FENCING_TOKEN"],
                release_metadata["fencing_token"],
            )

    def test_post_result_settlement_refusal_releases_then_settles_unfenced(
        self,
    ) -> None:
        # Live deadlock (2026-07-24): a run that failed after its result was
        # recorded settled through an adapter that refuses every call made
        # while the lock is held. The lock then refused to release before
        # settlement and the source refused to settle before release, which
        # stranded the task and, through it, the whole repository's dispatch.
        task = Task(task_id="T-1", title="Task", status="ready", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(directory, [task], {})
            source = self._enable_runtime_owned_task_source(runner, task)
            fenced_attempts: list[dict[str, str]] = []

            def refuse_while_locked(
                task_id: str,
                *args: object,
                runtime_context: dict[str, str] | None = None,
                **kwargs: object,
            ) -> bool:
                if lock_manager.is_locked(task_id):
                    fenced_attempts.append(dict(runtime_context or {}))
                    raise subprocess.CalledProcessError(
                        3,
                        f"task-source-reset {task_id}",
                        stderr=(
                            f'task-source: task "{task_id}" has an unreleased '
                            "lock; refusing unfenced reset\n"
                        ),
                    )
                source.settlement_context = dict(runtime_context or {})
                source.status = "ready"
                return True

            source.reset = refuse_while_locked  # type: ignore[method-assign]
            source.park = refuse_while_locked  # type: ignore[method-assign]

            def timed_out_worker(command, cwd, log, **kwargs):
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=-signal.SIGKILL,
                    timed_out=True,
                )

            result = self._run_task(runner, task, timed_out_worker)
            records = runner.run_store.read_records()

        self.assertEqual(result.classification, "timed_out")
        # The fenced path was tried first and its refusal is diagnosable.
        attempted = [
            record
            for record in records
            if record.get("record_type") == "task_source_settlement_attempted"
        ]
        self.assertTrue(attempted)
        self.assertEqual(attempted[0]["error_class"], "CalledProcessError")
        self.assertEqual(attempted[0]["exit_code"], 3)
        self.assertIn("refusing unfenced reset", attempted[0]["stderr"])
        self.assertEqual(attempted[0]["confirmed_status"], "active")
        self.assertTrue(fenced_attempts[0]["VIBE_LOOP_FENCING_TOKEN"])
        # Recovery converged: the lock released and the source settled after.
        self.assertFalse(lock_manager.is_locked("T-1"))
        self.assertEqual(source.status, "ready")
        settled = [
            record
            for record in records
            if record.get("record_type") == "task_source_settled"
        ]
        self.assertEqual(len(settled), 1)
        self.assertTrue(settled[0]["settled"])
        self.assertTrue(settled[0]["recovered"])
        self.assertEqual(settled[0]["confirmed_status"], "ready")
        stale = collect_stale_locks(
            lock_manager,
            runner.run_store,
            current_host=socket.gethostname(),
            process_exists=lambda pid: False,
        )
        self.assertEqual(stale, [])

    def test_first_accepted_report_survives_a_later_differing_report(self) -> None:
        # A worker that files ``completed``, has that report accepted (observed
        # by the watchdog), then files a second ``failed`` report before teardown
        # must still finalize from the first accepted report.
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, source = self._build_runner(
                directory, [task], {"T-1": done}
            )

            def fake_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                run_id = env["VIBE_LOOP_RUN_ID"]
                task_id = env["VIBE_LOOP_TASK_ID"]
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="completed",
                        message="completed via worker report",
                    )
                )
                # The supervisor accepts the first report here.
                reap_check = kwargs.get("reap_check")
                if reap_check is not None:
                    self.assertTrue(reap_check())
                # A misbehaving worker then files a contradicting report.
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=run_id,
                        task_id=task_id,
                        status="failed",
                        message="spurious later report",
                    )
                )
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                return runner_module.StreamingCommandResult(exit_code=0)

            result = self._run_task(runner, task, fake_run)

            self.assertEqual(result.classification, "completed")
            self.assertEqual(
                runner.run_store.latest_worker_report(result.run_id).status,
                "failed",
            )

    def test_settled_outcome_published_before_next_dispatch(self) -> None:
        first = Task(task_id="T-1", title="First", status="Next", agent="worker")
        second = Task(task_id="T-2", title="Second", status="Next", agent="worker")
        done = Task(task_id="T-1", title="First", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, source = self._build_runner(
                directory,
                [first, second],
                {
                    "T-1": done,
                    "T-2": Task(
                        task_id="T-2", title="Second", status="Done", agent="worker"
                    ),
                },
            )
            dispatched: list[str] = []
            fake_run = self._reporting_worker(runner, "completed")

            def tracking_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                task_id = env["VIBE_LOOP_TASK_ID"]
                dispatched.append(task_id)
                source.mark_dispatched(task_id)
                if task_id == "T-2":
                    # The second dispatch must not be able to rewrite or defer
                    # the first run's already-settled outcome.
                    self.assertEqual(
                        lock_manager.outcome_at_release("T-1"), "completed"
                    )
                return fake_run(command, cwd, log, **kwargs)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", tracking_run):
                    results = runner.run_until_done(continue_on_failure=True)

            self.assertEqual(dispatched[:2], ["T-1", "T-2"])
            self.assertEqual(results[0].classification, "completed")
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "completed")

    def test_settled_outcome_survives_idle_after_completion(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, source = self._build_runner(
                directory, [task], {"T-1": done}
            )
            reporting = self._reporting_worker(runner, "completed")

            def fake_run(command, cwd, log, **kwargs):
                source.mark_dispatched((kwargs.get("env") or {})["VIBE_LOOP_TASK_ID"])
                return reporting(command, cwd, log, **kwargs)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", fake_run):
                    results = runner.run_until_done()

            # The queue drains to idle right after the completed task; the
            # settled outcome must not be reopened by the cycle ending.
            self.assertEqual([item.classification for item in results], ["completed"])
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "completed")

    def test_terminal_report_statuses_map_to_settled_outcomes(self) -> None:
        for status, expected in (
            ("completed", "completed"),
            ("failed", "failed"),
            ("blocked", "blocked"),
            ("unknown", "unknown"),
        ):
            with self.subTest(status=status):
                task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
                with tempfile.TemporaryDirectory() as directory:
                    runner, lock_manager, _ = self._build_runner(
                        directory, [task], {"T-1": None}
                    )

                    result = self._run_task(
                        runner, task, self._reporting_worker(runner, status)
                    )

                    self.assertEqual(result.classification, status)
                    self.assertEqual(lock_manager.outcome_at_release("T-1"), expected)

    def test_missing_report_with_indeterminate_probe_stays_unknown(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(
                directory, [task], {"T-1": None}
            )

            result = self._run_task(
                runner,
                task,
                self._reporting_worker(runner, "completed", report=False),
            )

            self.assertEqual(result.classification, "unknown")
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "unknown")

    def test_interrupted_run_settles_unknown_not_completed(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(
                directory, [task], {"T-1": done}
            )

            def interrupting_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=env["VIBE_LOOP_TASK_ID"],
                        status="completed",
                        message="reported then interrupted",
                    )
                )
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                self._run_task(runner, task, interrupting_run)

            # A report on disk is not a settled run: the supervisor never
            # classified this one, so its outcome is genuinely unknown even
            # though the task source would now probe as done.
            self.assertEqual(lock_manager.outcome_at_release("T-1"), "unknown")

    def test_publish_failure_surfaces_without_losing_the_recorded_result(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, _ = self._build_runner(
                directory, [task], {"T-1": done}
            )
            original_update = lock_manager.update

            def failing_update(task_lock, metadata):
                if "outcome" in metadata:
                    raise LockBusy(task_lock.path, {"reason": "backend unavailable"})
                return original_update(task_lock, metadata)

            lock_manager.update = failing_update

            with self.assertRaises(SettledOutcomeNotPersisted):
                self._run_task(
                    runner, task, self._reporting_worker(runner, "completed")
                )

            # The classification is durable before finalization is attempted, so
            # the failure reports an unfinalized lock rather than losing the run.
            self.assertEqual(
                [
                    record.get("classification")
                    for record in runner.run_store.read_records()
                    if record.get("record_type") == "run_result"
                ],
                ["completed"],
            )
            self.assertIsNotNone(lock_manager.status("T-1"))

    def test_lock_released_event_carries_the_settled_outcome(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})

            self._run_task(runner, task, self._reporting_worker(runner, "completed"))

            released = [
                record
                for record in runner.run_store.read_records()
                if record.get("record_type") == "lock_released"
            ]
            self.assertEqual(len(released), 1)
            # vibe-loop's own provenance must settle on the same outcome it
            # published to the backend, so both views agree after the fact.
            self.assertEqual(released[0].get("outcome"), "completed")
            self.assertEqual(released[0].get("classification"), "completed")

    def test_parallel_jobs_settle_each_run_independently(self) -> None:
        tasks = [
            Task(task_id=f"T-{index}", title=f"Task {index}", status="Next")
            for index in range(1, 5)
        ]
        statuses = {"T-1": "completed", "T-2": "blocked", "T-3": "failed"}
        with tempfile.TemporaryDirectory() as directory:
            runner, lock_manager, source = self._build_runner(
                directory,
                tasks,
                {
                    task.task_id: Task(
                        task_id=task.task_id, title=task.title, status="Done"
                    )
                    for task in tasks
                },
            )

            def fake_run(command, cwd, log, **kwargs):
                env = kwargs.get("env") or {}
                task_id = env["VIBE_LOOP_TASK_ID"]
                source.mark_dispatched(task_id)
                runner.run_store.append_report(
                    WorkerReport(
                        run_id=env["VIBE_LOOP_RUN_ID"],
                        task_id=task_id,
                        status=statuses.get(task_id, "unknown"),
                        message="parallel worker report",
                    )
                )
                return runner_module.StreamingCommandResult(exit_code=0)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", fake_run):
                    runner.run_until_done(jobs=2, continue_on_failure=True)

            # Concurrent slots share the supervisor but not their settled
            # outcomes: no run may inherit or overwrite a sibling's.
            for task_id, expected in {
                "T-1": "completed",
                "T-2": "blocked",
                "T-3": "failed",
                "T-4": "unknown",
            }.items():
                with self.subTest(task_id=task_id):
                    self.assertEqual(lock_manager.outcome_at_release(task_id), expected)

    def test_command_lock_backend_receives_outcome_on_the_wire(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            wire_log = repo / "wire.jsonl"
            adapter = repo / "adapter.py"
            adapter.write_text(
                "import json, os, pathlib, sys\n"
                "operation = os.environ['VIBE_LOOP_LOCK_OPERATION']\n"
                "metadata = json.loads(os.environ['VIBE_LOOP_LOCK_METADATA_JSON'])\n"
                "log = pathlib.Path(sys.argv[1])\n"
                "store = log.with_suffix('.state')\n"
                "log.open('a').write(\n"
                "    json.dumps({'operation': operation, 'metadata': metadata}) + '\\n'\n"
                ")\n"
                "held = json.loads(store.read_text()) if store.exists() else None\n"
                "if operation == 'list':\n"
                "    print(json.dumps({'locks': [held] if held else []}))\n"
                "elif operation == 'status':\n"
                "    print(json.dumps({'locked': bool(held), 'metadata': held or {}}))\n"
                "elif operation == 'release':\n"
                "    store.unlink(missing_ok=True)\n"
                "    print(json.dumps({'released': True}))\n"
                "else:\n"
                "    store.write_text(json.dumps(metadata))\n"
                "    print(json.dumps({'acquired': True, 'metadata': metadata}))\n"
            )
            command = f"{sys.executable} {adapter} {wire_log}"
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            runner._lock_manager = LockManager(
                runner.config.state_path / "locks",
                backend=locks_module.CommandLockBackend(
                    repo=repo,
                    lock_root=runner.config.state_path / "locks",
                    acquire_command=command,
                    release_command=command,
                    status_command=command,
                    list_command=command,
                ),
            )

            self._run_task(runner, task, self._reporting_worker(runner, "completed"))

            calls = [
                json.loads(line)
                for line in wire_log.read_text().splitlines()
                if line.strip()
            ]
            # The defect lived on this wire: the backend that finalizes run
            # provenance must be handed the outcome by an update it persists,
            # before the release it finalizes on.
            operations = [call["operation"] for call in calls]
            last_update = max(
                index
                for index, operation in enumerate(operations)
                if operation == "update"
            )
            release_index = operations.index("release")
            self.assertLess(last_update, release_index)
            self.assertEqual(calls[last_update]["metadata"].get("outcome"), "completed")

    def _provenance_adapter(self, root: Path) -> str:
        """A command lock adapter that mirrors run provenance, as Loopyard does.

        It opens an external run at acquire and finalizes that run at release
        from the lock row it has already stored. Release-time metadata is
        deliberately discarded, matching the verified backend constraint that
        ``lock_wire_release`` persists nothing: only a prior update can settle
        the run, and an outcome that never reached the stored row stays
        ``unknown`` - which is exactly the reproduced defect.

        Touching ``fail_update`` under the state directory makes every outcome
        update fail, standing in for a backend that rejects the settling write.
        """

        adapter = root / "provenance_adapter.py"
        adapter.write_text(
            "import json, os, pathlib, sys\n"
            "operation = os.environ['VIBE_LOOP_LOCK_OPERATION']\n"
            "task_id = os.environ['VIBE_LOOP_LOCK_TASK_ID']\n"
            "run_id = os.environ['VIBE_LOOP_LOCK_RUN_ID']\n"
            "metadata = json.loads(os.environ['VIBE_LOOP_LOCK_METADATA_JSON'])\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "root.mkdir(parents=True, exist_ok=True)\n"
            "held_path = root / (task_id + '.held.json')\n"
            "runs_path = root / 'runs.json'\n"
            "runs = json.loads(runs_path.read_text()) if runs_path.exists() else {}\n"
            "held = json.loads(held_path.read_text()) if held_path.exists() else None\n"
            "if operation == 'list':\n"
            "    locks = [json.loads(p.read_text())\n"
            "             for p in sorted(root.glob('*.held.json'))]\n"
            "    print(json.dumps({'locks': locks}))\n"
            "elif operation == 'status':\n"
            "    print(json.dumps({'locked': bool(held), 'metadata': held or {}}))\n"
            "elif operation == 'release':\n"
            "    outcome = (held or {}).get('outcome') or 'unknown'\n"
            "    record = runs.get(run_id) or {'task_id': task_id}\n"
            "    record['outcome'] = outcome\n"
            "    runs[run_id] = record\n"
            "    runs_path.write_text(json.dumps(runs))\n"
            "    held_path.unlink(missing_ok=True)\n"
            "    print(json.dumps({'released': True}))\n"
            "elif (operation == 'update' and metadata.get('outcome')\n"
            "      and (root / 'fail_update').exists()):\n"
            "    sys.stderr.write('outcome update rejected')\n"
            "    print(json.dumps({'updated': False}))\n"
            "else:\n"
            "    runs.setdefault(run_id, {'task_id': task_id, 'outcome': 'unknown'})\n"
            "    runs_path.write_text(json.dumps(runs))\n"
            "    held_path.write_text(json.dumps(metadata))\n"
            "    print(json.dumps({'acquired': True, 'metadata': metadata}))\n"
        )
        return f"{sys.executable} {adapter} {root / 'state'}"

    @staticmethod
    def _external_outcomes(root: Path) -> dict[str, str]:
        runs_path = root / "state" / "runs.json"
        if not runs_path.exists():
            return {}
        records = json.loads(runs_path.read_text())
        return {
            str(record["task_id"]): str(record["outcome"])
            for record in records.values()
        }

    def _attach_provenance_backend(self, runner: VibeRunner, root: Path) -> None:
        command = self._provenance_adapter(root)
        runner._lock_manager = LockManager(
            runner.config.state_path / "locks",
            backend=locks_module.CommandLockBackend(
                repo=root,
                lock_root=runner.config.state_path / "locks",
                acquire_command=command,
                release_command=command,
                status_command=command,
                list_command=command,
            ),
        )

    def test_command_backend_finalizes_completed_across_next_dispatch(self) -> None:
        first = Task(task_id="T-1", title="First", status="Next", agent="worker")
        second = Task(task_id="T-2", title="Second", status="Next", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, source = self._build_runner(
                directory,
                [first, second],
                {
                    task_id: Task(task_id=task_id, title=task_id, status="Done")
                    for task_id in ("T-1", "T-2")
                },
            )
            self._attach_provenance_backend(runner, root)
            reporting = self._reporting_worker(runner, "completed")

            def fake_run(command, cwd, log, **kwargs):
                source.mark_dispatched((kwargs.get("env") or {})["VIBE_LOOP_TASK_ID"])
                return reporting(command, cwd, log, **kwargs)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", fake_run):
                    results = runner.run_until_done(continue_on_failure=True)

            self.assertEqual(
                [result.classification for result in results[:2]],
                ["completed", "completed"],
            )
            # The external provenance store - not the command text - is the
            # evidence: dispatching the next task must not leave the finished
            # run finalized as unknown.
            self.assertEqual(
                self._external_outcomes(root),
                {"T-1": "completed", "T-2": "completed"},
            )

    def test_command_backend_finalizes_completed_when_cycle_goes_idle(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, source = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            reporting = self._reporting_worker(runner, "completed")

            def fake_run(command, cwd, log, **kwargs):
                source.mark_dispatched((kwargs.get("env") or {})["VIBE_LOOP_TASK_ID"])
                return reporting(command, cwd, log, **kwargs)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", fake_run):
                    results = runner.run_until_done(continue_on_failure=True)

            self.assertEqual(results[0].classification, "completed")
            self.assertEqual(self._external_outcomes(root), {"T-1": "completed"})

    def test_command_backend_update_failure_blocks_unknown_finalizing_release(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            (root / "state").mkdir(parents=True, exist_ok=True)
            (root / "state" / "fail_update").write_text("")

            with self.assertRaises(SettledOutcomeNotPersisted) as raised:
                self._run_task(
                    runner, task, self._reporting_worker(runner, "completed")
                )

            self.assertEqual(raised.exception.outcome, "completed")
            # Releasing here is what produced completed-locally /
            # unknown-externally: the backend discards release metadata, so an
            # unsettled row can only finalize as unknown. No release may happen.
            self.assertEqual(self._external_outcomes(root), {"T-1": "unknown"})
            held = runner.lock_manager.status("T-1")
            self.assertIsNotNone(held)
            self.assertNotEqual(held.get("outcome"), "completed")
            records = runner.run_store.read_records()
            events = [
                record
                for record in records
                if record.get("record_type")
                in {"lock_released", "lock_finalization_failed"}
            ]
            self.assertEqual(
                [event["record_type"] for event in events],
                ["lock_finalization_failed"],
            )
            self.assertIs(events[0]["released"], False)
            self.assertEqual(events[0]["outcome"], "completed")
            # The run itself still classified correctly; only finalization failed.
            classifications = [
                record["classification"]
                for record in records
                if record.get("record_type") == "run_result"
            ]
            self.assertEqual(classifications, ["completed"])

    def test_stale_cleanup_cannot_finalize_a_retained_lock_as_unknown(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            (root / "state").mkdir(parents=True, exist_ok=True)
            (root / "state" / "fail_update").write_text("")

            with self.assertRaises(SettledOutcomeNotPersisted):
                self._run_task(
                    runner, task, self._reporting_worker(runner, "completed")
                )

            def collect() -> list[StaleLock]:
                return collect_stale_locks(
                    runner.lock_manager,
                    runner.run_store,
                    process_exists=lambda pid: False,
                )

            stale = collect()
            self.assertEqual([lock.task_id for lock in stale], ["T-1"])
            # The run is durably completed locally, so the operator recovery path
            # must carry that verdict rather than releasing the row it collected.
            self.assertEqual(stale[0].settled_outcome, "completed")

            blocked = clean_stale_locks(stale, runner.lock_manager)
            self.assertEqual(blocked.cleaned, [])
            self.assertEqual([lock.task_id for lock, _ in blocked.errors], ["T-1"])
            # Republication still fails, so refusing the release is the only way
            # to keep provenance from finalizing this run against its own result.
            self.assertEqual(self._external_outcomes(root), {"T-1": "unknown"})
            self.assertIsNotNone(runner.lock_manager.status("T-1"))

            (root / "state" / "fail_update").unlink()
            recovered = clean_stale_locks(collect(), runner.lock_manager)
            self.assertEqual(recovered.errors, [])
            self.assertEqual([lock.task_id for lock in recovered.cleaned], ["T-1"])
            self.assertEqual(self._external_outcomes(root), {"T-1": "completed"})
            self.assertIsNone(runner.lock_manager.status("T-1"))

    def test_stale_cleanup_falls_back_when_failure_event_append_fails(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            (root / "state").mkdir(parents=True, exist_ok=True)
            (root / "state" / "fail_update").write_text("")
            append_event = runner.run_store.append_lifecycle_event

            def append_except_finalization_failure(event) -> None:
                if event.record_type == LOCK_FINALIZATION_FAILED_RECORD_TYPE:
                    raise OSError("injected lifecycle append failure")
                append_event(event)

            with patch.object(
                runner.run_store,
                "append_lifecycle_event",
                side_effect=append_except_finalization_failure,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected lifecycle append failure"
                ):
                    self._run_task(
                        runner, task, self._reporting_worker(runner, "completed")
                    )

            records = runner.run_store.read_records()
            self.assertEqual(
                [
                    record["classification"]
                    for record in records
                    if record.get("record_type") == "run_result"
                ],
                ["completed"],
            )
            self.assertNotIn(
                LOCK_FINALIZATION_FAILED_RECORD_TYPE,
                [record.get("record_type") for record in records],
            )

            def collect() -> list[StaleLock]:
                return collect_stale_locks(
                    runner.lock_manager,
                    runner.run_store,
                    process_exists=lambda pid: False,
                )

            stale = collect()
            self.assertEqual([lock.settled_outcome for lock in stale], ["completed"])

            blocked = clean_stale_locks(stale, runner.lock_manager)
            self.assertEqual(blocked.cleaned, [])
            self.assertEqual([lock.task_id for lock, _ in blocked.errors], ["T-1"])
            self.assertEqual(self._external_outcomes(root), {"T-1": "unknown"})
            self.assertIsNotNone(runner.lock_manager.status("T-1"))

            (root / "state" / "fail_update").unlink()
            recovered = clean_stale_locks(collect(), runner.lock_manager)
            self.assertEqual(recovered.errors, [])
            self.assertEqual([lock.task_id for lock in recovered.cleaned], ["T-1"])
            self.assertEqual(self._external_outcomes(root), {"T-1": "completed"})
            self.assertIsNone(runner.lock_manager.status("T-1"))

    @staticmethod
    def _external_run_outcomes(root: Path) -> dict[str, str]:
        runs_path = root / "state" / "runs.json"
        if not runs_path.exists():
            return {}
        records = json.loads(runs_path.read_text())
        return {run_id: str(record["outcome"]) for run_id, record in records.items()}

    def test_command_backend_survives_a_stale_heartbeat_before_release(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            command_backend = runner.lock_manager.backend
            manager = SynchronizedHeartbeatLockManager(
                runner.config.state_path / "locks",
                backend=command_backend,
            )
            runner._lock_manager = manager

            self._run_task(runner, task, self._reporting_worker(runner, "completed"))

            self.assertTrue(manager.injected)
            self.assertIsNone(manager.heartbeat_error)
            snapshot = manager.heartbeat_snapshot or {}
            self.assertEqual(snapshot.get("outcome"), "unknown")
            self.assertEqual(snapshot.get("classification"), "unknown")
            # The heartbeat really did carry the stale unsettled pair into the
            # row after settlement; precedence, not absence, is what keeps the
            # stored outcome terminal for the backend that finalizes on release.
            self.assertEqual(
                (manager.heartbeat_metadata or {}).get("outcome"), "completed"
            )
            self.assertEqual(
                (manager.heartbeat_metadata or {}).get("classification"), "completed"
            )
            self.assertEqual(self._external_outcomes(root), {"T-1": "completed"})

    @unittest.skipIf(
        locks_module.fcntl is None,
        "the out-of-process mutex probe is written against flock",
    )
    def test_command_backend_orders_settlement_after_a_parked_foreign_write(
        self,
    ) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)
            command_backend = runner.lock_manager.backend
            writer_backend = ParkedWriteBackend(
                locks_module.CommandLockBackend(
                    repo=root,
                    lock_root=runner.config.state_path / "locks",
                    acquire_command=self._provenance_adapter(root),
                    release_command=self._provenance_adapter(root),
                    status_command=self._provenance_adapter(root),
                    list_command=self._provenance_adapter(root),
                )
            )
            manager = ForeignHeartbeatLockManager(
                runner.config.state_path / "locks",
                backend=command_backend,
                writer_backend=writer_backend,
            )
            runner._lock_manager = manager

            self._run_task(runner, task, self._reporting_worker(runner, "completed"))

            self.assertTrue(manager.injected)
            self.assertIsNone(manager.writer_error)
            # A separate process could not take the boundary while the foreign
            # writer was parked with its stale row merged, and could once that
            # write had landed. No settlement can interleave with that window.
            self.assertIs(manager.mutex_free_while_parked, False)
            self.assertIs(manager.mutex_free_after_write, True)
            # The foreign heartbeat did store its stale pair - it was parked
            # holding it, so nothing could rewrite it - which is why ordering,
            # not row precedence, has to be what keeps the run settled.
            self.assertEqual((manager.writer_metadata or {}).get("outcome"), "unknown")
            self.assertEqual(self._external_outcomes(root), {"T-1": "completed"})

    def test_command_backend_publishes_nothing_without_a_durable_result(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        done = Task(task_id="T-1", title="Task", status="Done", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": done})
            self._attach_provenance_backend(runner, root)

            with patch.object(
                runner, "record_result", side_effect=OSError("run store full")
            ):
                with self.assertRaises(OSError):
                    self._run_task(
                        runner, task, self._reporting_worker(runner, "completed")
                    )

            # External provenance may never claim a completion vibe-loop itself
            # failed to record: with no durable RunResult there is nothing for
            # the two stores to agree on, so the run stays honestly unknown.
            self.assertEqual(self._external_outcomes(root), {"T-1": "unknown"})
            classifications = [
                record["classification"]
                for record in runner.run_store.read_records()
                if record.get("record_type") == "run_result"
            ]
            self.assertEqual(classifications, [])

    def test_command_backend_settles_exhausted_recovery_as_failed(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The task stays runnable through every attempt, so each run
            # classifies unknown and recovery burns its single attempt.
            runner, _, _ = self._build_runner(
                directory,
                [task],
                {"T-1": task},
                supervision=SupervisionConfig(max_restarts=1, cooldown_seconds=0),
            )
            self._attach_provenance_backend(runner, root)
            worker = self._reporting_worker(runner, "completed", report=False)

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", worker):
                    first = runner.run_task(task)
                    results: list[RunResult] = []
                    terminal = runner.drive_unknown_recovery(
                        first, attempts={}, results=results
                    )

            self.assertEqual(first.classification, "unknown")
            self.assertEqual(terminal.classification, "failed")
            self.assertEqual(
                terminal.classification_source, "recovery_budget_exhausted"
            )
            self.assertNotEqual(terminal.run_id, first.run_id)
            external = self._external_run_outcomes(root)
            # Task runs and worklog must settle together: the supervisor calls
            # the final run failed, so external provenance may not keep it
            # unknown just because the exhaustion verdict lands after release.
            self.assertEqual(external.get(terminal.run_id), "failed")
            self.assertEqual(external.get(first.run_id), "unknown")
            recorded = [
                record["classification"]
                for record in runner.run_store.read_records()
                if record.get("record_type") == "run_result"
                and record.get("run_id") == terminal.run_id
            ]
            # The exhausting run recorded the verdict before releasing its lock
            # and the recovery driver reused it: one durable terminal result.
            self.assertEqual(recorded, ["unknown", "failed"])

    def test_command_backend_keeps_a_timed_out_final_attempt_unknown(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(
                directory,
                [task],
                {"T-1": task},
                supervision=SupervisionConfig(max_restarts=1, cooldown_seconds=0),
            )
            self._attach_provenance_backend(runner, root)
            recovery = runner_module.RecoveryContext(
                task_id="T-1",
                prior_run_id="prior",
                prior_classification="unknown",
                branch="",
                worktree="",
                head_commit="",
                transcript_path="",
                wrapper_log="",
                attempt=1,
                max_attempts=1,
                workspace_claimed=False,
            )

            def timing_out_worker(command, cwd, log, **kwargs):
                environment = kwargs["env"]
                self.assertEqual(environment["VIBE_LOOP_REPO"], str(cwd.resolve()))
                self.assertEqual(environment["VIBE_LOOP_WORKTREE"], str(cwd.resolve()))
                self.assertNotIn("VIBE_LOOP_PRIMARY_REPO", environment)
                on_start = kwargs.get("on_start")
                if on_start is not None:
                    on_start(os.getpid())
                return runner_module.StreamingCommandResult(
                    exit_code=143, timed_out=True
                )

            with patch.object(runner, "ensure_spec_execution_gate"):
                with patch("vibe_loop.runner.run_streaming_command", timing_out_worker):
                    result = runner.run_task(task, recovery=recovery)

            # A timed_out run never re-enters recovery, so no exhaustion verdict
            # is ever recorded for it. Publishing failed here would leave the
            # external run terminal while the run store still says timed_out.
            self.assertEqual(result.classification, "timed_out")
            self.assertEqual(
                self._external_run_outcomes(root).get(result.run_id), "unknown"
            )

    def test_command_backend_keeps_unsettled_runs_unknown(self) -> None:
        task = Task(task_id="T-1", title="Task", status="Next", agent="worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _, _ = self._build_runner(directory, [task], {"T-1": None})
            self._attach_provenance_backend(runner, root)

            result = self._run_task(
                runner,
                task,
                self._reporting_worker(runner, "completed", report=False),
            )

            self.assertNotEqual(result.classification, "completed")
            self.assertEqual(self._external_outcomes(root), {"T-1": "unknown"})


class SettledRunOutcomeTests(unittest.TestCase):
    def test_settling_classifications_map_through(self) -> None:
        for classification in ("completed", "failed", "blocked"):
            with self.subTest(classification=classification):
                self.assertEqual(settled_run_outcome(classification), classification)

    def test_non_settling_classifications_are_unknown(self) -> None:
        for classification in ("unknown", "timed_out", "provider_limit", "", "weird"):
            with self.subTest(classification=classification):
                self.assertEqual(settled_run_outcome(classification), "unknown")

    def test_every_run_classification_settles_within_the_outcome_family(self) -> None:
        # A backend that stores one terminal outcome per run rejects anything
        # outside this family, so no classification may escape the mapping.
        for classification in (
            "completed",
            "failed",
            "blocked",
            "unknown",
            "timed_out",
            "provider_limit",
        ):
            with self.subTest(classification=classification):
                self.assertIn(settled_run_outcome(classification), SETTLED_RUN_OUTCOMES)


class AgentRuntimeContextPrecedenceTests(unittest.TestCase):
    """Regression cover for run
    20260720T214201Z-hyphen-adjacent-generation-redaction-3d23bf62, where a
    generic JSON `model` value was recorded as the run's model identity."""

    def test_generic_json_model_value_is_not_a_model_identity(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps({"model": "task", "status": "queued"}),
            "stdout",
        )

        self.assertEqual(context.model_id, "")
        self.assertEqual(context.model_id_source, "")

    def test_nested_generic_model_key_is_not_scraped_from_json_text(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps({"type": "item.completed", "item": {"model": "task"}}),
            "stdout",
        )

        self.assertEqual(context.model_id, "")

    def test_codex_session_event_still_supplies_model_identity(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps({"type": "session.created", "model": "gpt-5.6-sol"}),
            "stdout",
        )

        self.assertEqual(context.model_id, "gpt-5.6-sol")
        self.assertEqual(context.model_id_source, "native:stdout:json.model")

    def test_generic_structured_session_id_is_not_runtime_identity(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "session_id": "abc-123",
                "item": {"type": "agent_message", "text": "done"},
            }
        )

        self.assertIsNone(runner_module.observe_worker_session(line, "stdout"))

    def test_typeless_structured_attribution_is_not_runtime_identity(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "model_provider": "openai",
                    "model_id": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                }
            ),
            "stdout",
        )

        self.assertTrue(context.empty)

    def test_claude_init_event_still_supplies_model_identity(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-4-8"}
            ),
            "stdout",
        )

        self.assertEqual(context.model_id, "claude-opus-4-8")

    def test_structured_model_mapping_retains_existing_precedence(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "type": "session.created",
                    "model": {"provider": "openai", "id": "gpt-5.5"},
                }
            ),
            "stdout",
        )

        self.assertEqual(context.model_provider, "openai")
        self.assertEqual(context.model_id, "gpt-5.5")

    def test_explicit_model_id_field_retains_existing_precedence(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps({"type": "session.created", "model_id": "gpt-5.5"}),
            "stdout",
        )

        self.assertEqual(context.model_id, "gpt-5.5")

    def test_structured_placeholder_attribution_is_rejected_with_diagnostics(
        self,
    ) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "type": "session.created",
                    "model": {"provider": "value", "id": "task"},
                }
            ),
            "stdout",
        )

        self.assertEqual(context.model_provider, "")
        self.assertEqual(context.model_id, "")
        self.assertEqual(
            context.attribution_diagnostics,
            ("provider", "model"),
        )

    def test_rejected_aliases_do_not_hide_later_native_identity(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "type": "session.created",
                    "model_provider": "value",
                    "model_id": "task",
                    "model": {"provider": "openai", "id": "gpt-5.6-sol"},
                }
            ),
            "stdout",
        )

        self.assertEqual(context.model_provider, "openai")
        self.assertEqual(context.model_id, "gpt-5.6-sol")
        self.assertEqual(
            context.attribution_diagnostics,
            ("provider", "model"),
        )

    def test_command_fragments_and_paths_are_not_model_attribution(self) -> None:
        for model in ("task", "codex exec --json", "/tmp/gpt-5.6-sol"):
            with self.subTest(model=model):
                context = parse_agent_runtime_context_from_command(
                    f"codex exec --model {shell_quote(model)}"
                )
                self.assertEqual(context.model_provider, "openai")
                self.assertEqual(context.model_id, "")
                self.assertEqual(context.attribution_diagnostics, ("model",))

    def test_noncanonical_explicit_provider_falls_back_to_executable_identity(
        self,
    ) -> None:
        context = parse_agent_runtime_context_from_command(
            "codex exec --provider value --model gpt-5.6-sol"
        )

        self.assertEqual(context.model_provider, "openai")
        self.assertEqual(context.model_id, "gpt-5.6-sol")
        self.assertEqual(context.attribution_diagnostics, ("provider",))

    def test_malformed_model_value_fails_closed_to_unknown(self) -> None:
        context = parse_agent_runtime_context_from_line(
            json.dumps({"type": "session.created", "model": "gpt 5.6\tsol"}),
            "stdout",
        )

        self.assertEqual(context.model_id, "")
        self.assertEqual(context.model_id_source, "")

    def test_generic_text_cannot_establish_codex_or_claude_identity(self) -> None:
        observed = parse_agent_runtime_context_from_line(
            "worker note: provider=value model=task",
            "stdout",
        )

        self.assertTrue(observed.empty)
        for command, expected_provider in (
            ("codex exec --json", "openai"),
            ("claude --output-format stream-json", "anthropic"),
        ):
            with self.subTest(command=command):
                effective = parse_agent_runtime_context_from_command(command).prefer(
                    observed
                )
                self.assertEqual(effective.model_provider, expected_provider)
                self.assertEqual(effective.model_id, "")

    def test_claude_init_resolves_command_model_alias(self) -> None:
        command_context = parse_agent_runtime_context_from_command(
            "claude --model opus"
        )
        init_context = parse_agent_runtime_context_from_line(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-4-8"}
            ),
            "stdout",
        )
        assistant_context = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": "provider=value model=task"},
                }
            ),
            "stdout",
        )

        effective = command_context.prefer(init_context).prefer(assistant_context)

        self.assertEqual(effective.model_provider, "anthropic")
        self.assertEqual(effective.model_id, "claude-opus-4-8")
        self.assertEqual(effective.model_id_source, "native:stdout:json.model")

    def test_structured_native_provider_refines_executable_inference(self) -> None:
        command_context = parse_agent_runtime_context_from_command("codex exec")
        observed = parse_agent_runtime_context_from_line(
            json.dumps(
                {
                    "type": "session.created",
                    "model": {"provider": "openai", "id": "gpt-5.6-sol"},
                }
            ),
            "stdout",
        )

        effective = command_context.prefer(observed)

        self.assertEqual(effective.model_id, "gpt-5.6-sol")
        self.assertEqual(effective.model_id_source, "native:stdout:json.model")
        self.assertEqual(effective.model_provider, "openai")
        self.assertEqual(
            effective.model_provider_source, "native:stdout:json.model_provider"
        )

    def test_startup_frame_provider_refines_executable_inference(self) -> None:
        command_context = parse_agent_runtime_context_from_command("codex exec")
        observed = AgentRuntimeContext(
            model_provider="oss",
            model_provider_source="native:stdout:startup_frame.provider",
            model_id="qwen3-coder",
            model_id_source="native:stdout:startup_frame.model",
        )

        effective = command_context.prefer(observed)

        self.assertEqual(effective.model_provider, "oss")
        self.assertEqual(
            effective.model_provider_source,
            "native:stdout:startup_frame.provider",
        )
        self.assertEqual(effective.model_id, "qwen3-coder")

    def test_same_value_structured_event_upgrades_weak_source(self) -> None:
        weak = AgentRuntimeContext(
            model_id="gpt-5.6-sol",
            model_id_source="native:stdout:model",
        )
        strong = parse_agent_runtime_context_from_line(
            json.dumps({"type": "session.created", "model": "gpt-5.6-sol"}),
            "stdout",
        )

        delta = weak.missing_delta(strong)

        merged = weak.overlay(delta)
        self.assertEqual(delta.model_id, "gpt-5.6-sol")
        self.assertEqual(delta.model_id_source, "native:stdout:json.model")
        self.assertEqual(merged.model_id, "gpt-5.6-sol")
        self.assertEqual(merged.model_id_source, "native:stdout:json.model")


class TaskSourceSessionExportTests(unittest.TestCase):
    """The task-source environment must carry who implemented and who reviewed.

    A backend attributes a status transition to those sessions, and refuses to
    close a task whose reviewer is absent, so the runtime exports a value only
    when the run actually recorded a usable session for that role. How strongly
    the id is attested depends on the provider -- see
    `runner.RECOGNIZED_SESSION_ID_SOURCES`; these tests cover what is exported,
    not how much it is worth.
    """

    RUN_ID = "run-session-export"
    TASK_ID = "TASK-SESSION"

    def _runner(self, directory: str) -> VibeRunner:
        return VibeRunner(VibeConfig(repo=Path(directory)))

    def _lock(self, directory: str, **metadata: object) -> TaskLock:
        return TaskLock(
            task_id=self.TASK_ID,
            path=Path(directory) / "task.lock",
            metadata=dict(metadata),
        )

    def _record_session_observed(
        self,
        runner: VibeRunner,
        *,
        session_id: str,
        session_id_source: str,
        run_id: str = RUN_ID,
        task_id: str = TASK_ID,
    ) -> None:
        runner.run_store.append_lifecycle_event(
            RunLifecycleEvent.run_state_transition(
                run_id=run_id,
                task_id=task_id,
                from_state="started",
                to_state="session_observed",
                reason=session_id_source,
                payload={
                    "session_id": session_id,
                    "session_id_source": session_id_source,
                },
            )
        )

    def _record_review_verdict(
        self,
        runner: VibeRunner,
        *,
        verdict: str,
        session_id: str,
        session_id_source: str = "runtime_injected",
        pass_kind: str = "initial",
        run_id: str = RUN_ID,
        task_id: str = TASK_ID,
    ) -> None:
        runner.run_store.append_lifecycle_event(
            RunLifecycleEvent.review_verdict(
                run_id=run_id,
                task_id=task_id,
                payload={
                    "pass_kind": pass_kind,
                    "verdict": verdict,
                    "session_id": session_id,
                    "session_id_source": session_id_source,
                },
            )
        )

    def _context(
        self,
        runner: VibeRunner,
        task_lock: TaskLock,
        runtime_context: dict[str, str] | None = None,
        *,
        include_reviewer_session: bool = True,
    ) -> dict[str, str]:
        # Most cases exercise the completion path, the only transition that
        # carries the reviewer.
        return runner.task_source_runtime_context(
            task_id=self.TASK_ID,
            run_id=self.RUN_ID,
            task_lock=task_lock,
            runtime_context=runtime_context,
            include_reviewer_session=include_reviewer_session,
        )

    def test_reviewed_run_exports_both_recorded_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
            )

            context = self._context(runner, self._lock(directory))

        self.assertEqual(
            context["VIBE_LOOP_IMPLEMENTER_SESSION"],
            "worker-session-a",
        )
        self.assertEqual(
            context["VIBE_LOOP_REVIEWER_SESSION"],
            "reviewer-session-a",
        )

    def test_last_approving_pass_supplies_the_reviewer_session(self) -> None:
        # Guard, not a reachable production state: the review output parser
        # refuses an approve that carries findings or leaves a prior finding
        # open, so the approve that exits the loop is the only one a run
        # records today. This pins "last, never first" so a future change that
        # makes a second approving pass reachable cannot silently attribute the
        # merge to a superseded reviewer.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-initial",
            )
            self._record_review_verdict(
                runner,
                verdict="findings",
                session_id="reviewer-session-closure-1",
                pass_kind="closure:1",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-closure-2",
                session_id_source="runtime_resumed",
                pass_kind="closure:2",
            )

            context = self._context(runner, self._lock(directory))

        self.assertEqual(
            context["VIBE_LOOP_REVIEWER_SESSION"],
            "reviewer-session-closure-2",
        )

    def test_reviewer_session_absent_when_the_approval_is_unattributable(
        self,
    ) -> None:
        # An earlier attributable approval must not stand in for the pass that
        # actually approved the integrated candidate.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-initial",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-closure-1",
                session_id_source="unavailable",
                pass_kind="closure:1",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)
        self.assertEqual(
            context["VIBE_LOOP_IMPLEMENTER_SESSION"],
            "worker-session-a",
        )

    def test_a_source_outside_the_runtime_vocabulary_is_not_exported(self) -> None:
        # Only the label is checked here. On the `runtime_launch` path the agent
        # supplies both fields, so a recognized label does not make the id
        # runtime-bound -- it only keeps the vocabulary closed.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
                session_id_source="x",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_the_run_id_is_never_exported_as_a_session(self) -> None:
        # The value the design most wants withheld, whatever source is claimed.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id=self.RUN_ID,
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id=self.RUN_ID,
                session_id_source="runtime_launch",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_settlement_and_reset_transitions_omit_the_reviewer(self) -> None:
        # A settled run merged nothing, so the approver of an unmerged
        # candidate must not be attributed to its failure or requeue.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
            )

            context = self._context(
                runner,
                self._lock(directory),
                include_reviewer_session=False,
            )

        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)
        self.assertEqual(
            context["VIBE_LOOP_IMPLEMENTER_SESSION"],
            "worker-session-a",
        )

    def test_approval_without_a_recorded_session_id_is_unattributable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_reviewer_session_absent_when_review_never_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="findings",
                session_id="reviewer-session-a",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_implementer_session_absent_when_never_observed(self) -> None:
        # The runtime falls back to the run id when it never saw a session;
        # that names the run, not a session, so it attributes nothing.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id=self.RUN_ID,
                session_id_source="fallback:run_id",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertEqual(
            context["VIBE_LOOP_REVIEWER_SESSION"],
            "reviewer-session-a",
        )

    def test_no_records_export_neither_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_inherited_session_values_are_dropped_not_passed_through(self) -> None:
        # An ambient value would attribute the transition to a session that did
        # no work here, and the consumer cannot tell it from a derived one.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)

            context = self._context(
                runner,
                self._lock(directory),
                {
                    "VIBE_LOOP_IMPLEMENTER_SESSION": "ambient-implementer",
                    "VIBE_LOOP_REVIEWER_SESSION": "ambient-reviewer",
                },
            )

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_another_run_and_task_never_supply_this_run_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="other-run-worker",
                session_id_source="observed",
                run_id="run-other",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="other-task-reviewer",
                task_id="TASK-OTHER",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_session_ids_outside_the_identifier_alphabet_are_omitted(self) -> None:
        # Reviewer-reported ids are agent-influenced text; a value the runtime
        # cannot attest as an identifier is omitted rather than exported for
        # the consumer to reject as malformed.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker session with spaces",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer\nsession",
            )

            context = self._context(runner, self._lock(directory))

        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", context)
        self.assertNotIn("VIBE_LOOP_REVIEWER_SESSION", context)

    def test_session_export_keeps_the_existing_context_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
            )

            context = self._context(
                runner,
                self._lock(directory, fencing_token="generation-9"),
                {
                    "VIBE_LOOP_REPO": "/claimed/worktree",
                    "VIBE_LOOP_WORKTREE": "/claimed/worktree",
                    "VIBE_LOOP_BRANCH": "vibe/claimed",
                    "PROJECT_SELECTOR": "configured",
                },
            )

        self.assertNotIn("VIBE_LOOP_REPO", context)
        self.assertNotIn("VIBE_LOOP_WORKTREE", context)
        self.assertNotIn("VIBE_LOOP_BRANCH", context)
        self.assertEqual(context["VIBE_LOOP_FENCING_TOKEN"], "generation-9")
        self.assertEqual(context["PROJECT_SELECTOR"], "configured")
        self.assertEqual(context["VIBE_LOOP_TASK_ID"], self.TASK_ID)
        self.assertEqual(context["VIBE_LOOP_RUN_ID"], self.RUN_ID)
        self.assertEqual(context["VIBE_LOOP_PRIMARY_REPO"], str(runner.config.repo))

    def test_unfenced_lock_still_omits_the_fencing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)

            context = self._context(
                runner,
                self._lock(directory),
                {"VIBE_LOOP_FENCING_TOKEN": "stale-generation"},
            )

        self.assertNotIn("VIBE_LOOP_FENCING_TOKEN", context)

    def test_derivation_reads_records_rather_than_reconstructing(self) -> None:
        records = [
            {
                "record_type": "run_state_transition",
                "run_id": self.RUN_ID,
                "task_id": self.TASK_ID,
                "to_state": "session_observed",
                "session_id": "worker-session-a",
                "session_id_source": "native:stdout",
            },
            {
                "record_type": "review_verdict",
                "run_id": self.RUN_ID,
                "task_id": self.TASK_ID,
                "verdict": "approve",
                "session_id": "reviewer-session-a",
                "session_id_source": "runtime_launch",
            },
        ]

        self.assertEqual(
            implementer_session_from_records(
                records,
                run_id=self.RUN_ID,
                task_id=self.TASK_ID,
            ),
            "worker-session-a",
        )
        self.assertEqual(
            reviewer_session_from_records(
                records,
                run_id=self.RUN_ID,
                task_id=self.TASK_ID,
            ),
            "reviewer-session-a",
        )

    def test_a_non_session_state_transition_is_not_an_observation(self) -> None:
        records = [
            {
                "record_type": "run_state_transition",
                "run_id": self.RUN_ID,
                "task_id": self.TASK_ID,
                "to_state": "classified",
                "session_id": "worker-session-a",
                "session_id_source": "observed",
            }
        ]

        self.assertEqual(
            implementer_session_from_records(
                records,
                run_id=self.RUN_ID,
                task_id=self.TASK_ID,
            ),
            "",
        )

    def _adapter_report_command(self, directory: str) -> str:
        # Reports, as JSON on stdout, which of the two names the adapter
        # process actually observes in its own environment.
        script = Path(directory) / "report_env.py"
        script.write_text(
            "import json\n"
            "import os\n"
            "print(\n"
            "    json.dumps(\n"
            "        {\n"
            '            "implementer": os.environ.get(\n'
            '                "VIBE_LOOP_IMPLEMENTER_SESSION", "<absent>"\n'
            "            ),\n"
            '            "reviewer": os.environ.get(\n'
            '                "VIBE_LOOP_REVIEWER_SESSION", "<absent>"\n'
            "            ),\n"
            "        }\n"
            "    )\n"
            ")\n",
            encoding="utf-8",
        )
        return f"{shell_quote(sys.executable)} report_env.py"

    def test_an_unattributed_run_leaves_the_adapter_process_without_the_names(
        self,
    ) -> None:
        # The contract is absence in the adapter's *environment*, not in a
        # Python dict. `os.environ.copy()` plus `update` cannot express a
        # removal, so the ambient value must be withheld at the boundary.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            command = self._adapter_report_command(directory)

            with patch.dict(
                os.environ,
                {
                    "VIBE_LOOP_IMPLEMENTER_SESSION": "ambient-stale-implementer",
                    "VIBE_LOOP_REVIEWER_SESSION": "ambient-stale-reviewer",
                },
            ):
                context = self._context(runner, self._lock(directory))
                payload = run_json_command(
                    Path(directory),
                    command,
                    runtime_context=context,
                )

        self.assertEqual(
            payload,
            {"implementer": "<absent>", "reviewer": "<absent>"},
        )

    def test_derived_sessions_reach_the_adapter_process(self) -> None:
        # The same crossing in the positive direction: a derived value must
        # win over the ambient one rather than merely not being deleted.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            self._record_session_observed(
                runner,
                session_id="worker-session-a",
                session_id_source="observed",
            )
            self._record_review_verdict(
                runner,
                verdict="approve",
                session_id="reviewer-session-a",
            )
            command = self._adapter_report_command(directory)

            with patch.dict(
                os.environ,
                {
                    "VIBE_LOOP_IMPLEMENTER_SESSION": "ambient-stale-implementer",
                    "VIBE_LOOP_REVIEWER_SESSION": "ambient-stale-reviewer",
                },
            ):
                context = self._context(runner, self._lock(directory))
                payload = run_json_command(
                    Path(directory),
                    command,
                    runtime_context=context,
                )

        self.assertEqual(
            payload,
            {
                "implementer": "worker-session-a",
                "reviewer": "reviewer-session-a",
            },
        )

    def test_reader_reads_the_real_session_observation_producer(self) -> None:
        # Couples the reader's key expectations to `build_run_context_payload`.
        # A producer that renames or nests `session_id` must fail here rather
        # than silently emptying the export forever.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            payload = build_run_context_payload(
                task_id=self.TASK_ID,
                run_id=self.RUN_ID,
                started_at="2026-01-01T00:00:00Z",
                session_id="worker-session-a",
                session_id_source="observed",
                agent_kind="claude",
                agent_kind_source="explicit",
                agent_prompt_dialect="claude",
                agent_prompt_dialect_source="explicit",
                agent_skill_ref_prefix="/",
                agent_skill_ref_prefix_source="explicit",
                runtime_context=AgentRuntimeContext(),
            )
            runner.run_store.append_lifecycle_event(
                RunLifecycleEvent.run_state_transition(
                    run_id=self.RUN_ID,
                    task_id=self.TASK_ID,
                    from_state="started",
                    to_state="session_observed",
                    reason="observed",
                    payload=payload,
                )
            )

            context = self._context(runner, self._lock(directory))

        self.assertEqual(
            context["VIBE_LOOP_IMPLEMENTER_SESSION"],
            "worker-session-a",
        )

    def test_reader_reads_the_real_review_verdict_producer(self) -> None:
        # Couples the reader to `ReviewRouter`: the router writes the verdict
        # record itself, and the exported reviewer must equal the identity the
        # router reports for that pass.
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            candidate = CandidateRecord(
                branch=f"vibe-loop/{self.TASK_ID}",
                worktree=Path(directory),
                base_main="a" * 40,
                head_commit="b" * 40,
                changed_paths=("src/example.py",),
                source="derived",
            )
            gates = GateRunSummary(
                candidate=candidate,
                results=(
                    GateResult(
                        config_key="completion.commands[0]",
                        exit_class="passed",
                        exit_code=0,
                        duration_seconds=0.5,
                        log_reference=str(Path(directory) / "gate.log"),
                        evidence_digest="sha256:" + "c" * 64,
                        candidate_fingerprint=candidate.fingerprint,
                    ),
                ),
                candidate_recorded=True,
            )

            def execute(command: str, **kwargs: object):
                verdict = {
                    "verdict": "approve",
                    "findings": [],
                    "session_id": "reviewer-reported-session",
                    "session_id_source": "runtime_launch",
                    "continuation_ordinal": 0,
                }
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(verdict),
                )

            router = ReviewRouter(
                reviewer=AgentConfig(
                    command=("codex review --model {model} --effort {effort} {prompt}"),
                    command_source="explicit",
                    model="review-model",
                    model_source="explicit",
                    effort="high",
                    effort_source="explicit",
                    agent_kind="codex",
                    agent_kind_source="explicit",
                    executable_kind="codex",
                    profile_name="review",
                ),
                reviewer_profile="review",
                run_store=runner.run_store,
                run_id=self.RUN_ID,
                task_id=self.TASK_ID,
                worktree=Path(directory),
                policy_references=("REVIEW.md",),
                max_initial_passes=1,
                max_closure_passes=2,
                concurrency=ReviewConcurrencyBudget(1),
                executor=execute,
                continuation_availability=lambda *_args: "",
                session_id_factory=lambda: "runtime-placeholder",
            )

            result = router.review(gates)
            context = self._context(runner, self._lock(directory))

        self.assertTrue(result.approved)
        self.assertEqual(
            context["VIBE_LOOP_REVIEWER_SESSION"],
            result.session_id,
        )


if __name__ == "__main__":
    unittest.main()
