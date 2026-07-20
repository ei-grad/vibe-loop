from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_loop.autopilot import (
    AUTOPILOT_RUNTIME_CONTEXT_FD_ENV,
    AggregateProjectStatus,
    ProjectEntry,
    ProjectRegistry,
    collect_project_status,
    collect_registry_status,
    run_autopilot,
    runtime_context_subprocess_transport,
    start_detached_autopilot,
)
from vibe_loop.config import (
    ProjectBindingError,
    RUNTIME_CONTEXT_REDACTION,
    load_config,
    parse_project_binding,
    resolve_project_binding,
)
from vibe_loop import cli
from vibe_loop.locks import build_lock_manager
from vibe_loop.runner import VibeRunner
from vibe_loop.workers import ActiveRunState, WorkerView


SELECTOR = "DEMO_PROJECT"


TASK_ADAPTER = """\
import json
import os
import sys
from pathlib import Path

invocation_log = os.environ.get("DEMO_INVOCATION_LOG")
if invocation_log:
    with Path(invocation_log).open("a", encoding="utf-8") as stream:
        stream.write("task\\n")
selector = os.environ.get("DEMO_PROJECT")
if not selector:
    sys.stderr.write("task adapter refused: DEMO_PROJECT is unset\\n")
    raise SystemExit(3)
root = Path(os.environ["DEMO_STATE_ROOT"]) / selector
root.mkdir(parents=True, exist_ok=True)
tasks_path = root / "tasks.json"
tasks = (
    json.loads(tasks_path.read_text(encoding="utf-8"))
    if tasks_path.is_file()
    else []
)
operation = sys.argv[1] if len(sys.argv) > 1 else "list"
if operation == "list":
    print(json.dumps({"tasks": tasks}))
elif operation == "probe":
    match = next((item for item in tasks if item["id"] == sys.argv[2]), None)
    print(json.dumps(match))
else:
    raise SystemExit(f"unsupported task operation: {operation}")
"""


LOCK_ADAPTER = """\
import json
import os
import sys
from pathlib import Path

invocation_log = os.environ.get("DEMO_INVOCATION_LOG")
if invocation_log:
    with Path(invocation_log).open("a", encoding="utf-8") as stream:
        stream.write("lock\\n")
selector = os.environ.get("DEMO_PROJECT")
if not selector:
    sys.stderr.write("lock adapter refused: DEMO_PROJECT is unset\\n")
    raise SystemExit(3)
root = Path(os.environ["DEMO_STATE_ROOT"]) / selector
root.mkdir(parents=True, exist_ok=True)
state_path = root / "locks.json"
state = (
    json.loads(state_path.read_text(encoding="utf-8"))
    if state_path.is_file()
    else {}
)


def save():
    state_path.write_text(json.dumps(state, sort_keys=True) + "\\n", encoding="utf-8")


operation = os.environ["VIBE_LOOP_LOCK_OPERATION"]
task_id = os.environ.get("VIBE_LOOP_LOCK_TASK_ID", "")
run_id = os.environ.get("VIBE_LOOP_LOCK_RUN_ID", "")
metadata = json.loads(os.environ.get("VIBE_LOOP_LOCK_METADATA_JSON", "{}"))
current = state.get(task_id)
owned = isinstance(current, dict) and current.get("run_id") == run_id
if operation == "acquire":
    if current is not None and not owned:
        print(json.dumps({"acquired": False, "metadata": current}))
    else:
        state[task_id] = metadata
        save()
        print(json.dumps({"acquired": True, "metadata": metadata}))
elif operation == "update":
    if not owned:
        print(json.dumps({"updated": False, "metadata": current or {}}))
    else:
        state[task_id] = metadata
        save()
        print(json.dumps({"updated": True, "metadata": metadata}))
elif operation == "release":
    if not owned:
        print(json.dumps({"released": False}))
    else:
        state.pop(task_id, None)
        save()
        print(json.dumps({"released": True}))
elif operation == "status":
    if current is None:
        print(json.dumps({"locked": False}))
    else:
        print(json.dumps({"locked": True, "metadata": current}))
elif operation == "list":
    print(json.dumps({"locks": [{"metadata": item} for item in state.values()]}))
else:
    raise SystemExit(f"unsupported lock operation: {operation}")
"""


GENERIC_TASK_ADAPTER = """\
import json

print(json.dumps({"tasks": [{"id": "GENERIC-1", "title": "generic", "status": "ready"}]}))
"""


GENERIC_LOCK_ADAPTER = """\
import json
import os

operation = os.environ["VIBE_LOOP_LOCK_OPERATION"]
if operation == "list":
    print(json.dumps({"locks": []}))
elif operation == "status":
    print(json.dumps({"locked": False}))
else:
    raise SystemExit(f"unexpected mutation: {operation}")
"""


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "init")


class ProjectBindingParseTests(unittest.TestCase):
    def test_parses_require_and_pinned_context(self) -> None:
        binding = parse_project_binding(
            {"require": [SELECTOR], "context": {SELECTOR: "alpha"}}
        )

        self.assertTrue(binding.declared)
        self.assertEqual(binding.require, (SELECTOR,))
        self.assertEqual(binding.context, ((SELECTOR, "alpha"),))
        self.assertEqual(
            binding.to_json(),
            {
                "declared": True,
                "require": [SELECTOR],
                "context_names": [SELECTOR],
            },
        )

    def test_absent_table_is_undeclared(self) -> None:
        binding = parse_project_binding({})

        self.assertFalse(binding.declared)
        self.assertEqual(binding.require, ())

    def test_rejects_unsupported_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported keys: namespace"):
            parse_project_binding({"namespace": "alpha"})

    def test_rejects_non_selector_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a namespace selector"):
            parse_project_binding({"require": ["DEMO_ENDPOINT"]})

    def test_rejects_dangerous_and_secret_shaped_names(self) -> None:
        for name in ("PATH", "DEMO_API_TOKEN", "VIBE_LOOP_PROJECT"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    parse_project_binding({"require": [name]})

    def test_rejects_duplicate_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than once"):
            parse_project_binding({"require": [SELECTOR, SELECTOR]})

    def test_rejects_non_list_require(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a list"):
            parse_project_binding({"require": SELECTOR})

    def test_rejects_invalid_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_binding.context is invalid"):
            parse_project_binding({"context": {SELECTOR: 7}})


class ProjectBindingResolutionTests(unittest.TestCase):
    def build_config(self, table: str, *, runtime_context=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name)
        (repo / ".vibe-loop.toml").write_text(table, encoding="utf-8")
        return load_config(repo, runtime_context=runtime_context)

    def test_undeclared_binding_resolves_empty(self) -> None:
        config = self.build_config("")
        binding = resolve_project_binding(config, environ={})

        self.assertFalse(binding.declared)
        self.assertIsNone(binding.blocker)
        self.assertEqual(binding.entries, ())

    def test_config_pin_resolves_and_ignores_ambient(self) -> None:
        config = self.build_config(
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n'
            "[project_binding.context]\n"
            f'{SELECTOR} = "alpha"\n'
        )
        binding = resolve_project_binding(config, environ={SELECTOR: "intruder"})

        self.assertIsNone(binding.blocker)
        self.assertEqual(len(binding.entries), 1)
        entry = binding.entries[0]
        self.assertEqual(
            (entry.name, entry.value, entry.source), (SELECTOR, "alpha", "config")
        )
        self.assertEqual(config.runtime_environment, {SELECTOR: "alpha"})

    def test_registry_context_resolves(self) -> None:
        config = self.build_config(
            f'[project_binding]\nrequire = ["{SELECTOR}"]\n',
            runtime_context={SELECTOR: "beta"},
        )
        binding = resolve_project_binding(config, environ={})

        entry = binding.entries[0]
        self.assertEqual((entry.value, entry.source), ("beta", "runtime_context"))

    def test_registry_context_overrides_matching_pin_without_conflict(self) -> None:
        config = self.build_config(
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n'
            "[project_binding.context]\n"
            f'{SELECTOR} = "alpha"\n',
            runtime_context={SELECTOR: "alpha"},
        )
        binding = resolve_project_binding(config, environ={})

        self.assertIsNone(binding.blocker)
        self.assertEqual(binding.entries[0].source, "runtime_context")

    def test_conflicting_pin_and_registry_context_fails(self) -> None:
        config = self.build_config(
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n'
            "[project_binding.context]\n"
            f'{SELECTOR} = "alpha"\n',
            runtime_context={SELECTOR: "beta"},
        )
        binding = resolve_project_binding(config, environ={})

        self.assertEqual(binding.blocker, f"project_binding_conflict:{SELECTOR}")
        self.assertEqual(binding.entries, ())

    def test_ambient_only_value_is_refused(self) -> None:
        config = self.build_config(f'[project_binding]\nrequire = ["{SELECTOR}"]\n')
        binding = resolve_project_binding(config, environ={SELECTOR: "capos"})

        self.assertEqual(binding.blocker, f"project_binding_ambient_only:{SELECTOR}")

    def test_missing_value_is_refused(self) -> None:
        config = self.build_config(f'[project_binding]\nrequire = ["{SELECTOR}"]\n')
        binding = resolve_project_binding(config, environ={})

        self.assertEqual(binding.blocker, f"project_binding_unset:{SELECTOR}")

    def test_diagnostics_do_not_leak_ambient_values(self) -> None:
        config = self.build_config(f'[project_binding]\nrequire = ["{SELECTOR}"]\n')
        binding = resolve_project_binding(config, environ={SELECTOR: "capos"})

        self.assertNotIn("capos", json.dumps(binding.to_json()))

    def test_require_project_binding_raises_on_diagnostics(self) -> None:
        config = self.build_config(f'[project_binding]\nrequire = ["{SELECTOR}"]\n')

        with self.assertRaises(ProjectBindingError) as caught:
            VibeRunner(config).run_until_done()

        self.assertEqual(
            caught.exception.binding.blocker,
            f"project_binding_unset:{SELECTOR}",
        )


class ProjectBindingGateTests(unittest.TestCase):
    """The gate must refuse before any lock or child process is touched."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        (self.repo / ".vibe-loop.toml").write_text(
            "[locks]\n"
            'type = "command"\n'
            'acquire_command = "false"\n'
            'release_command = "false"\n'
            'status_command = "false"\n'
            'list_command = "false"\n'
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n',
            encoding="utf-8",
        )
        self.config = load_config(self.repo)

    def test_run_autopilot_blocks_before_lock_acquisition(self) -> None:
        with (
            mock.patch("vibe_loop.autopilot.build_lock_manager") as lock_manager,
            mock.patch.dict(os.environ, {SELECTOR: "capos"}),
        ):
            summary = run_autopilot(self.config, once=True)

        self.assertFalse(summary.started)
        self.assertEqual(summary.blocker, f"project_binding_ambient_only:{SELECTOR}")
        lock_manager.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "detached start is POSIX only")
    def test_detached_start_blocks_before_launch(self) -> None:
        with (
            mock.patch("vibe_loop.autopilot.build_lock_manager") as lock_manager,
            mock.patch("vibe_loop.autopilot.subprocess.Popen") as popen,
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop(SELECTOR, None)
            launch = start_detached_autopilot(self.config)

        self.assertFalse(launch.started)
        self.assertEqual(launch.blocker, f"project_binding_unset:{SELECTOR}")
        lock_manager.assert_not_called()
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "detached start is POSIX only")
    def test_detached_start_carries_registry_binding_and_drops_ambient(self) -> None:
        config = load_config(self.repo, runtime_context={SELECTOR: "beta"})
        captured: dict[str, object] = {}

        def fake_popen(command, **kwargs):
            captured["env"] = dict(kwargs["env"])
            captured["pass_fds"] = kwargs["pass_fds"]
            fd = int(captured["env"][AUTOPILOT_RUNTIME_CONTEXT_FD_ENV])
            captured["transported"] = json.loads(os.read(fd, 4096).decode("utf-8"))
            return mock.Mock(pid=4242)

        lock_status = mock.Mock(locked=False, state="", metadata={})
        with (
            mock.patch(
                "vibe_loop.autopilot.build_lock_manager",
                return_value=mock.Mock(
                    autopilot_status=mock.Mock(return_value=lock_status)
                ),
            ),
            mock.patch("vibe_loop.autopilot.subprocess.Popen", side_effect=fake_popen),
            mock.patch.dict(os.environ, {SELECTOR: "capos"}),
        ):
            # Verification is expected to time out against the mocked child;
            # this asserts what the launch handed to the child, not liveness.
            start_detached_autopilot(config, verification_timeout=0.0)

        self.assertEqual(captured["transported"], {SELECTOR: "beta"})
        self.assertNotIn(SELECTOR, captured["env"])
        self.assertTrue(captured["pass_fds"])

    def test_subprocess_transport_drops_ambient_bound_names(self) -> None:
        with mock.patch.dict(os.environ, {SELECTOR: "capos"}):
            environment, context_file = runtime_context_subprocess_transport(
                (),
                bound_names=(SELECTOR,),
            )

        self.assertIsNone(context_file)
        self.assertNotIn(SELECTOR, environment)


class CrossProjectIsolationTests(unittest.TestCase):
    """One command backend, two repositories, two namespaces, no crossing."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shared = self.root / "shared"
        shared.mkdir()
        self.state_root = self.root / "backend-state"
        self.state_root.mkdir()
        self.invocation_log = self.root / "adapter-invocations.log"
        self.task_adapter = shared / "task_adapter.py"
        self.task_adapter.write_text(TASK_ADAPTER, encoding="utf-8")
        self.lock_adapter = shared / "lock_adapter.py"
        self.lock_adapter.write_text(LOCK_ADAPTER, encoding="utf-8")
        # The ambient selector names a third project throughout: any command
        # that routes by inheritance instead of by binding lands here.
        ambient = mock.patch.dict(
            os.environ,
            {
                SELECTOR: "intruder",
                "DEMO_STATE_ROOT": str(self.state_root),
                "DEMO_INVOCATION_LOG": str(self.invocation_log),
            },
        )
        ambient.start()
        self.addCleanup(ambient.stop)

    def seed_tasks(self, namespace: str, task_ids: list[str]) -> None:
        namespace_root = self.state_root / namespace
        namespace_root.mkdir(parents=True, exist_ok=True)
        (namespace_root / "tasks.json").write_text(
            json.dumps(
                [
                    {"id": task_id, "title": task_id, "status": "ready"}
                    for task_id in task_ids
                ]
            ),
            encoding="utf-8",
        )

    def adapter_invocations(self) -> list[str]:
        if not self.invocation_log.is_file():
            return []
        return self.invocation_log.read_text(encoding="utf-8").splitlines()

    def guarded_cli_operations(self, repo: Path) -> list[tuple[str, list[str], int]]:
        repo_arg = str(repo)
        return [
            ("doctor", ["doctor", "--repo", repo_arg], 1),
            ("workers", ["workers", "--repo", repo_arg], 1),
            ("workers clean", ["workers", "clean", "--repo", repo_arg], 1),
            ("task listing", ["tasks", "--repo", repo_arg], 1),
            ("task selection", ["next", "--repo", repo_arg], 1),
            ("task run selection", ["run-next", "--repo", repo_arg], 1),
            (
                "autopilot stop",
                ["autopilot", "stop", "--repo", repo_arg],
                2,
            ),
            (
                "autopilot stale recovery",
                [
                    "autopilot",
                    "stop",
                    "--repo",
                    repo_arg,
                    "--recover-stale",
                    "--run-id",
                    "test-run",
                ],
                2,
            ),
            (
                "main-integration acquire",
                [
                    "main-integration",
                    "acquire",
                    "--repo",
                    repo_arg,
                    "--run-id",
                    "test-run",
                    "--task-id",
                    "test-task",
                ],
                1,
            ),
            (
                "main-integration status",
                ["main-integration", "status", "--repo", repo_arg],
                1,
            ),
            (
                "main-integration release",
                [
                    "main-integration",
                    "release",
                    "--repo",
                    repo_arg,
                    "--run-id",
                    "test-run",
                    "--task-id",
                    "test-task",
                ],
                1,
            ),
            (
                "report fencing validation",
                [
                    "report",
                    "--repo",
                    repo_arg,
                    "--run-id",
                    "test-run",
                    "--task-id",
                    "test-task",
                    "--status",
                    "failed",
                    "--fencing-token",
                    "test-token",
                ],
                1,
            ),
        ]

    def build_repo(self, name: str, namespace: str):
        repo = self.root / name
        init_repo(repo)
        quoted_task = f"{sys.executable} {self.task_adapter}"
        quoted_lock = f"{sys.executable} {self.lock_adapter}"
        (repo / ".vibe-loop.toml").write_text(
            "[task_source]\n"
            'type = "command"\n'
            f'list = "{quoted_task} list"\n'
            f'probe = "{quoted_task} probe {{task_id}}"\n'
            'runnable_statuses = ["ready"]\n'
            "[locks]\n"
            'type = "command"\n'
            f'acquire_command = "{quoted_lock}"\n'
            f'release_command = "{quoted_lock}"\n'
            f'status_command = "{quoted_lock}"\n'
            f'list_command = "{quoted_lock}"\n'
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n'
            "[project_binding.context]\n"
            f'{SELECTOR} = "{namespace}"\n',
            encoding="utf-8",
        )
        run_git(repo, "add", ".vibe-loop.toml")
        run_git(repo, "commit", "-m", "configure backend")
        return load_config(repo)

    def test_task_selection_and_locks_cannot_cross_projects(self) -> None:
        self.seed_tasks("alpha", ["ALPHA-1", "SHARED-1"])
        self.seed_tasks("beta", ["BETA-1", "SHARED-1"])
        alpha = self.build_repo("alpha-repo", "alpha")
        beta = self.build_repo("beta-repo", "beta")

        alpha_tasks = sorted(
            task.task_id for task in VibeRunner(alpha).source.list_tasks()
        )
        beta_tasks = sorted(
            task.task_id for task in VibeRunner(beta).source.list_tasks()
        )

        self.assertEqual(alpha_tasks, ["ALPHA-1", "SHARED-1"])
        self.assertEqual(beta_tasks, ["BETA-1", "SHARED-1"])

        alpha_locks = build_lock_manager(
            alpha.repo,
            alpha.state_path / "locks",
            alpha.locks,
            runtime_context=alpha.runtime_environment,
        )
        beta_locks = build_lock_manager(
            beta.repo,
            beta.state_path / "locks",
            beta.locks,
            runtime_context=beta.runtime_environment,
        )

        alpha_lock = alpha_locks.acquire("SHARED-1", run_id="alpha-run")
        # The same task id in the other project must not be considered held.
        self.assertIsNone(beta_locks.status("SHARED-1"))
        beta_lock = beta_locks.acquire("SHARED-1", run_id="beta-run")

        self.assertIsNotNone(alpha_locks.status("SHARED-1"))
        self.assertIsNotNone(beta_locks.status("SHARED-1"))

        alpha_locks.release(alpha_lock)
        self.assertIsNone(alpha_locks.status("SHARED-1"))
        self.assertIsNotNone(beta_locks.status("SHARED-1"))
        beta_locks.release(beta_lock)

        # Nothing was ever written under the ambient selector's namespace.
        self.assertFalse((self.state_root / "intruder").exists())
        self.assertEqual(
            sorted(path.name for path in self.state_root.iterdir()),
            ["alpha", "beta"],
        )

    def test_status_reports_resolved_namespace_per_repository(self) -> None:
        self.seed_tasks("alpha", ["ALPHA-1"])
        self.seed_tasks("beta", ["BETA-1"])
        alpha = self.build_repo("alpha-repo", "alpha")
        beta = self.build_repo("beta-repo", "beta")

        alpha_status = collect_project_status(alpha).to_json()
        beta_status = collect_project_status(beta).to_json()

        self.assertEqual(
            alpha_status["project_binding"],
            {
                "declared": True,
                "resolved": [{"name": SELECTOR, "source": "config", "value": "alpha"}],
                "diagnostics": [],
                "injected_names": [SELECTOR],
            },
        )
        self.assertEqual(
            beta_status["project_binding"]["resolved"][0]["value"],
            "beta",
        )
        self.assertNotIn(f"project_binding_unset:{SELECTOR}", alpha_status["blockers"])
        self.assertEqual(
            [task["id"] for task in alpha_status["queue"]["runnable_tasks"]],
            ["ALPHA-1"],
        )
        self.assertEqual(
            [task["id"] for task in beta_status["queue"]["runnable_tasks"]],
            ["BETA-1"],
        )

    def write_unbound_repo(
        self,
        *,
        name: str = "unbound-repo",
        pinned_context: str | None = None,
    ) -> Path:
        repo = self.root / name
        init_repo(repo)
        quoted_task = f"{sys.executable} {self.task_adapter}"
        quoted_lock = f"{sys.executable} {self.lock_adapter}"
        binding = f'[project_binding]\nrequire = ["{SELECTOR}"]\n'
        if pinned_context is not None:
            binding += f'[project_binding.context]\n{SELECTOR} = "{pinned_context}"\n'
        (repo / ".vibe-loop.toml").write_text(
            "[task_source]\n"
            'type = "command"\n'
            f'list = "{quoted_task} list"\n'
            "[locks]\n"
            'type = "command"\n'
            f'acquire_command = "{quoted_lock}"\n'
            f'release_command = "{quoted_lock}"\n'
            f'status_command = "{quoted_lock}"\n'
            f'list_command = "{quoted_lock}"\n' + binding,
            encoding="utf-8",
        )
        run_git(repo, "add", ".vibe-loop.toml")
        run_git(repo, "commit", "-m", "configure backend")
        return repo

    def build_unbound_repo(
        self,
        *,
        name: str = "unbound-repo",
        pinned_context: str | None = None,
    ):
        return load_config(
            self.write_unbound_repo(name=name, pinned_context=pinned_context)
        )

    def build_generic_command_repo(self):
        repo = self.root / "generic-repo"
        init_repo(repo)
        task_adapter = self.root / "shared" / "generic_task_adapter.py"
        task_adapter.write_text(GENERIC_TASK_ADAPTER, encoding="utf-8")
        lock_adapter = self.root / "shared" / "generic_lock_adapter.py"
        lock_adapter.write_text(GENERIC_LOCK_ADAPTER, encoding="utf-8")
        (repo / ".vibe-loop.toml").write_text(
            "[task_source]\n"
            'type = "command"\n'
            f'list = "{sys.executable} {task_adapter}"\n'
            'runnable_statuses = ["ready"]\n'
            "[locks]\n"
            'type = "command"\n'
            f'acquire_command = "{sys.executable} {lock_adapter}"\n'
            f'release_command = "{sys.executable} {lock_adapter}"\n'
            f'status_command = "{sys.executable} {lock_adapter}"\n'
            f'list_command = "{sys.executable} {lock_adapter}"\n',
            encoding="utf-8",
        )
        return load_config(repo)

    def test_status_reports_binding_diagnostic_when_only_ambient_routing_exists(
        self,
    ) -> None:
        self.seed_tasks("intruder", ["INTRUDER-1"])
        config = self.build_unbound_repo()

        status = collect_project_status(config).to_json()

        self.assertIn(f"project_binding_ambient_only:{SELECTOR}", status["blockers"])
        self.assertEqual(
            status["project_binding"]["diagnostics"],
            [
                {
                    "name": SELECTOR,
                    "reason": "ambient_only",
                    "code": f"project_binding_ambient_only:{SELECTOR}",
                }
            ],
        )
        # The ambient project's queue must never be reported as this repo's.
        self.assertEqual(status["queue"]["runnable_tasks"], [])
        self.assertEqual(
            status["queue"]["source_error"],
            f"project_binding_ambient_only:{SELECTOR}",
        )
        # Supervisor liveness is unobservable without the lock adapter, so it
        # must not be reported as a checked "idle".
        self.assertEqual(status["supervisor"]["state"], "unknown")
        self.assertEqual(
            status["supervisor"]["blocker"],
            f"project_binding_ambient_only:{SELECTOR}",
        )
        self.assertTrue(status["git"]["available"])

    def test_unbound_status_never_invokes_command_adapters(self) -> None:
        config = self.build_unbound_repo()
        invocations = self.adapter_invocations()

        collect_project_status(config)

        self.assertEqual(self.adapter_invocations(), invocations)

    def test_missing_binding_blocks_every_guarded_cli_operation_without_invocation(
        self,
    ) -> None:
        config = self.build_unbound_repo(name="missing-binding-repo")

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("vibe_loop.cli.inherited_runtime_context", return_value={}),
        ):
            os.environ.pop(SELECTOR, None)
            for operation, arguments, expected_exit in self.guarded_cli_operations(
                config.repo
            ):
                with self.subTest(operation=operation):
                    invocations = self.adapter_invocations()
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        exit_code = cli.main(arguments)

                    self.assertEqual(exit_code, expected_exit)
                    self.assertIn(
                        f"project_binding_unset:{SELECTOR}",
                        stdout.getvalue() + stderr.getvalue(),
                    )
                    self.assertEqual(self.adapter_invocations(), invocations)

    def test_conflicting_binding_blocks_every_guarded_cli_operation_without_invocation(
        self,
    ) -> None:
        config = self.build_unbound_repo(
            name="conflicting-binding-repo",
            pinned_context="alpha",
        )

        with mock.patch(
            "vibe_loop.cli.inherited_runtime_context",
            return_value={SELECTOR: "beta"},
        ):
            for operation, arguments, expected_exit in self.guarded_cli_operations(
                config.repo
            ):
                with self.subTest(operation=operation):
                    invocations = self.adapter_invocations()
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        exit_code = cli.main(arguments)

                    self.assertEqual(exit_code, expected_exit)
                    self.assertIn(
                        f"project_binding_conflict:{SELECTOR}",
                        stdout.getvalue() + stderr.getvalue(),
                    )
                    self.assertEqual(self.adapter_invocations(), invocations)

    def test_empty_pinned_binding_is_rejected_without_adapter_invocation(self) -> None:
        repo = self.write_unbound_repo(
            name="empty-pinned-binding-repo",
            pinned_context="",
        )
        invocations = self.adapter_invocations()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = cli.main(["doctor", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertIn("must not be empty", stderr.getvalue())
        self.assertEqual(self.adapter_invocations(), invocations)

    def test_blank_registry_binding_is_rejected_without_adapter_invocation(
        self,
    ) -> None:
        repo = self.write_unbound_repo(name="blank-registry-binding-repo")
        invocations = self.adapter_invocations()
        stderr = io.StringIO()

        with (
            mock.patch(
                "vibe_loop.cli.inherited_runtime_context",
                return_value={SELECTOR: "   "},
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli.main(["doctor", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertIn("must not be empty", stderr.getvalue())
        self.assertEqual(self.adapter_invocations(), invocations)

    def test_generic_command_backends_need_no_project_binding(self) -> None:
        config = self.build_generic_command_repo()
        runner = VibeRunner(config)

        self.assertFalse(config.project_binding.declared)
        self.assertEqual(
            [task.task_id for task in runner.source.list_tasks()],
            ["GENERIC-1"],
        )
        self.assertEqual(runner.lock_manager.list_locks(), [])

    def test_unbound_lock_manager_construction_is_gated(self) -> None:
        config = self.build_unbound_repo()

        with self.assertRaises(ProjectBindingError):
            VibeRunner(config).lock_manager

    def test_binding_failure_is_not_reframed_as_a_cache_problem(self) -> None:
        config = self.build_unbound_repo()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = cli.main(["tasks", "--repo", str(config.repo)])

        self.assertEqual(exit_code, 1)
        message = stderr.getvalue()
        self.assertIn(f"project_binding_ambient_only:{SELECTOR}", message)
        self.assertNotIn("generated task-source cache", message)

    def test_registry_status_reports_binding_without_redacting_it(self) -> None:
        self.seed_tasks("beta", ["BETA-1"])
        repo = self.build_repo("registry-repo", "beta").repo
        entry = ProjectEntry(
            name="registry-repo",
            repo=repo,
            runtime_context=(),
        )
        registry = ProjectRegistry(path=self.root / "registry.json", entries=(entry,))

        payload = collect_registry_status(registry)[0].to_json()

        self.assertEqual(
            payload["status"]["project_binding"]["resolved"],
            [{"name": SELECTOR, "source": "config", "value": "beta"}],
        )

    def test_registry_supplied_binding_survives_aggregate_redaction(self) -> None:
        self.seed_tasks("beta", ["BETA-1"])
        repo = self.root / "registry-only-repo"
        init_repo(repo)
        quoted_task = f"{sys.executable} {self.task_adapter}"
        (repo / ".vibe-loop.toml").write_text(
            "[task_source]\n"
            'type = "command"\n'
            f'list = "{quoted_task} list"\n'
            'runnable_statuses = ["ready"]\n'
            "[project_binding]\n"
            f'require = ["{SELECTOR}"]\n',
            encoding="utf-8",
        )
        run_git(repo, "add", ".vibe-loop.toml")
        run_git(repo, "commit", "-m", "configure backend")
        entry = ProjectEntry(
            name="registry-only-repo",
            repo=repo,
            runtime_context=((SELECTOR, "beta"),),
        )

        status = collect_project_status(
            load_config(repo, runtime_context=dict(entry.runtime_context))
        )
        worker = WorkerView(
            active=ActiveRunState.new(
                task_id="worker-1",
                run_id="run-1",
                log_path=self.root / "worker.log",
                base_main="base",
                command="worker",
                trailer_context={
                    "project_binding": {"adapter_value": "beta"},
                },
            ),
            state="active",
            process_state="live",
        )
        payload = AggregateProjectStatus(
            name=entry.name,
            repo=entry.repo,
            status=dataclasses.replace(status, workers=(worker,)),
            runtime_context=entry.runtime_context,
        ).to_json()

        # The routing fact must survive the aggregate redaction pass that
        # otherwise scrubs every registry context value from the payload.
        self.assertEqual(
            payload["status"]["project_binding"]["resolved"],
            [{"name": SELECTOR, "source": "runtime_context", "value": "beta"}],
        )
        self.assertEqual(
            [task["id"] for task in payload["status"]["queue"]["runnable_tasks"]],
            ["BETA-1"],
        )
        self.assertEqual(
            payload["status"]["workers"][0]["trailer_context"]["project_binding"][
                "adapter_value"
            ],
            RUNTIME_CONTEXT_REDACTION,
        )


class CaseSensitivityTests(unittest.TestCase):
    def test_require_treats_case_variants_as_distinct_selectors(self) -> None:
        binding = parse_project_binding({"require": [SELECTOR, SELECTOR.title()]})

        self.assertEqual(binding.require, (SELECTOR, SELECTOR.title()))

    def test_pin_does_not_satisfy_a_differently_cased_requirement(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name)
        (repo / ".vibe-loop.toml").write_text(
            "[project_binding]\n"
            f'require = ["{SELECTOR.title()}"]\n'
            "[project_binding.context]\n"
            f'{SELECTOR} = "alpha"\n',
            encoding="utf-8",
        )
        config = load_config(repo)

        binding = resolve_project_binding(config, environ={})

        self.assertEqual(
            binding.blocker,
            f"project_binding_unset:{SELECTOR.title()}",
        )
        self.assertEqual(binding.injected_names, (SELECTOR,))


if __name__ == "__main__":
    unittest.main()
