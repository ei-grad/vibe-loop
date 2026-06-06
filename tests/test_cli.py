from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import warnings
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import vibe_loop.cli as cli_module
from vibe_loop.cli import main
from vibe_loop.locks import LockManager


@contextmanager
def temporary_directory_with_cleanup_retry():
    directory = Path(tempfile.mkdtemp())
    try:
        yield str(directory)
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            remove_tree_with_windows_retries(directory)
        except PermissionError:
            if not active_exception:
                raise
            warnings.warn(
                f"skipped temporary directory cleanup after test failure: {directory}",
                ResourceWarning,
                stacklevel=2,
            )


def remove_tree_with_windows_retries(path: Path) -> None:
    attempts = 100 if sys.platform == "win32" else 1
    delay = 0.05
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
        time.sleep(delay)


def toml_string(value: str) -> str:
    return json.dumps(value)


def file_fingerprint(path: Path, relative_path: str) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "path": relative_path,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def parse_run_result(
    test: unittest.TestCase,
    stdout: StringIO,
    stderr: StringIO,
    exit_code: int,
) -> dict[str, object]:
    raw = stdout.getvalue()
    if not raw.strip():
        test.fail(
            f"run produced no stdout (exit_code={exit_code})\nstderr:\n"
            + stderr.getvalue()
        )
    return json.loads(raw)


PLAN = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Test task. | Run agent. | Not run. |
"""


TWO_TASK_PLAN = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | First test task. | Run agent. | Not run. |
| TASK-02 | P0 | Next | none | Second test task. | Run agent. | Not run. |
"""

THREE_TASK_PLAN = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | First test task. | Run agent. | Not run. |
| TASK-02 | P0 | Next | none | Second test task. | Run agent. | Not run. |
| TASK-03 | P0 | Next | none | Third test task. | Run agent. | Not run. |
"""


WORK_TABLE = """# Work

| Key | State | Summary |
| --- | --- | --- |
| DISC-X | Todo | Configure generated discovery. |
"""


def spec_profile_config(*, specs_section: str = "") -> str:
    return (
        '[task_source]\ntype = "markdown-profile"\n'
        "[task_source.profile]\n"
        'kind = "markdown_table"\n'
        'source_paths = ["WORK.md"]\n'
        "stable_ids = true\n"
        '[task_source.profile.fields.id]\ncolumn = "ID"\n'
        '[task_source.profile.fields.status]\ncolumn = "Status"\n'
        '[task_source.profile.fields.title]\ncolumn = "Title"\n'
        "[task_source.profile.fields.requirement_ids]\n"
        'column = "Requirements"\nnone_values = ["none"]\n'
        "[task_source.profile.fields.spec_paths]\n"
        'column = "Spec Paths"\nnone_values = ["none"]\n'
        '[task_source.profile.fields.approval_state]\ncolumn = "Approval"\n'
        "[task_source.profile.fields.source_fingerprints]\n"
        'column = "Fingerprints"\nnone_values = ["none"]\n'
        '[task_source.profile.fields.evidence]\ncolumn = "Evidence"\n'
        "[task_source.profile.status_map]\n"
        'done = ["Done"]\n'
        'runnable = ["Planned"]\n'
        'blocked = ["Blocked"]\n'
        f"{specs_section}"
    )


def spec_task_table(*, fingerprint: dict[str, object]) -> str:
    fingerprint_json = json.dumps([fingerprint], separators=(",", ":"))
    return (
        "# Work\n\n"
        "| ID | Status | Title | Requirements | Spec Paths | Approval | Fingerprints | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        f"| TASK-01 | Planned | Stale draft spec. | REQ-1 | docs/spec.md | draft | {fingerprint_json} | Not run. |\n"
        "| TASK-02 | Planned | Missing requirement. | none | docs/spec.md | approved | none | Not run. |\n"
        "| TASK-03 | Done | Missing evidence. | REQ-3 | docs/spec.md | approved | none | Not run. |\n"
    )


class DirectUrlDistribution:
    def __init__(self, payload: dict[str, object] | None) -> None:
        self.payload = payload

    def read_text(self, name: str) -> str | None:
        if name != "direct_url.json" or self.payload is None:
            return None
        return json.dumps(self.payload)


class CliTests(unittest.TestCase):
    def test_version_flag_prints_package_version_without_loading_config(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch("vibe_loop.cli.package_version", return_value="9.8.7"),
            patch("vibe_loop.cli.load_config", side_effect=AssertionError),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["--version"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "vibe-loop 9.8.7\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_package_version_includes_git_direct_url_commit_for_branch_install(
        self,
    ) -> None:
        distribution = DirectUrlDistribution(
            {
                "url": "git+ssh://git@github.com/ei-grad/vibe-loop.git",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "main",
                    "commit_id": "abcdef1234567890abcdef1234567890abcdef12",
                },
            }
        )

        with (
            patch("vibe_loop.cli.metadata_version", return_value="1.2.3"),
            patch("vibe_loop.cli.metadata_distribution", return_value=distribution),
            patch("vibe_loop.cli.source_tree_git_commit_sha", return_value=""),
        ):
            version = cli_module.package_version()

        self.assertEqual(version, "1.2.3 (git abcdef123456)")

    def test_package_version_omits_git_commit_for_matching_tag_install(self) -> None:
        distribution = DirectUrlDistribution(
            {
                "url": "https://github.com/ei-grad/vibe-loop.git",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "v1.2.3",
                    "commit_id": "abcdef1234567890abcdef1234567890abcdef12",
                },
            }
        )

        with (
            patch("vibe_loop.cli.metadata_version", return_value="1.2.3"),
            patch("vibe_loop.cli.metadata_distribution", return_value=distribution),
            patch("vibe_loop.cli.source_tree_git_commit_sha", return_value=""),
        ):
            version = cli_module.package_version()

        self.assertEqual(version, "1.2.3")

    def test_package_version_includes_source_tree_commit_for_editable_install(
        self,
    ) -> None:
        distribution = DirectUrlDistribution(
            {
                "url": "file:///workspace/vibe-loop",
                "dir_info": {"editable": True},
            }
        )

        with (
            patch("vibe_loop.cli.metadata_version", return_value="1.2.3"),
            patch("vibe_loop.cli.metadata_distribution", return_value=distribution),
            patch(
                "vibe_loop.cli.source_tree_git_commit_sha",
                return_value="123456789abc",
            ),
        ):
            version = cli_module.package_version()

        self.assertEqual(version, "1.2.3 (git 123456789abc)")

    def test_package_version_omits_source_tree_commit_for_regular_install(
        self,
    ) -> None:
        distribution = DirectUrlDistribution(None)

        with (
            patch("vibe_loop.cli.metadata_version", return_value="1.2.3"),
            patch("vibe_loop.cli.metadata_distribution", return_value=distribution),
            patch(
                "vibe_loop.cli.source_tree_git_commit_sha",
                return_value="123456789abc",
            ),
        ):
            version = cli_module.package_version()

        self.assertEqual(version, "1.2.3")

    def test_run_next_uses_codex_default_when_only_codex_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            write_fake_git(bin_dir)
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "Path('agent-args.txt').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "print(json.dumps({'model': {'provider': 'openai', "
                "'id': 'gpt-5.5', 'reasoning_effort': 'high'}}))\n"
                "print('session id: codex-native-123')\n"
                "print(f'codex out: {sys.argv[2]}')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            agent_args = (repo / "agent-args.txt").read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            run_result = next(
                record
                for record in run_records
                if record.get("record_type") == "run_result"
            )
            run_started = next(
                record
                for record in run_records
                if record.get("record_type") == "run_started"
            )
            context_observed = next(
                record
                for record in run_records
                if record.get("record_type") == "agent_context_observed"
            )
            session_observed = next(
                record
                for record in run_records
                if record.get("record_type") == "run_state_transition"
                and record.get("to_state") == "session_observed"
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertNotEqual(payload["session_id"], payload["run_id"])
        self.assertEqual(payload["session_id"], "codex-native-123")
        self.assertEqual(payload["session_id_source"], "native:stdout")
        self.assertEqual(payload["agent_command_source"], "auto:codex")
        self.assertEqual(payload["agent_selection_command_source"], "auto:codex")
        self.assertEqual(payload["agent_kind"], "auto")
        self.assertEqual(payload["agent_prompt_dialect"], "codex")
        self.assertEqual(payload["agent_prompt_dialect_source"], "auto:codex")
        self.assertEqual(payload["agent_skill_ref_prefix"], "$")
        self.assertEqual(run_result["session_id"], "codex-native-123")
        self.assertEqual(run_result["session_id_source"], "native:stdout")
        self.assertEqual(run_result["agent_prompt_dialect"], "codex")
        self.assertEqual(run_result["started_at"], run_started["started_at"])
        self.assertEqual(run_result["model_provider"], "openai")
        self.assertEqual(run_result["model_id"], "gpt-5.5")
        self.assertEqual(run_result["reasoning_effort"], "high")
        self.assertEqual(
            run_result["trailer_context"]["plan_item_candidates"],
            ["TASK-01"],
        )
        self.assertEqual(
            run_result["trailer_context"]["session_id"], "codex-native-123"
        )
        self.assertEqual(run_started["session_id"], run_started["run_id"])
        self.assertEqual(run_started["session_id_source"], "fallback:run_id")
        self.assertEqual(run_started["model_provider"], "openai")
        self.assertEqual(
            run_started["model_provider_source"], "command_executable:codex"
        )
        self.assertNotIn("model_id", run_started)
        self.assertEqual(context_observed["model_id"], "gpt-5.5")
        self.assertEqual(
            context_observed["model_id_source"], "native:stdout:json.model"
        )
        self.assertEqual(context_observed["started_at"], run_started["started_at"])
        self.assertEqual(session_observed["session_id"], "codex-native-123")
        self.assertEqual(session_observed["model_id"], "gpt-5.5")
        for record in run_records:
            if record.get("record_type") in {
                "lock_acquired",
                "run_started",
                "agent_context_observed",
                "run_state_transition",
                "run_result",
                "lock_released",
            }:
                self.assertEqual(record.get("started_at"), run_started["started_at"])
        self.assertIn(
            "lock_acquired",
            {record.get("record_type") for record in run_records},
        )
        self.assertIn(
            "run_started",
            {record.get("record_type") for record in run_records},
        )
        self.assertIn(
            "run_state_transition",
            {record.get("record_type") for record in run_records},
        )
        agent_lines = agent_args.split("\n")
        self.assertEqual(agent_lines[0], "exec")
        self.assertIn("$vibe-loop TASK-01", agent_lines[1])
        self.assertIn("vibe-loop CLI Coordination", agent_args)
        self.assertIn("agent command source: auto:codex", stderr.getvalue())
        self.assertIn("agent prompt dialect source: auto:codex", stderr.getvalue())
        self.assertIn("agent_command_source=auto:codex", log_text)
        self.assertIn("agent_prompt_dialect_source=auto:codex", log_text)
        self.assertIn("session_id_source=native:stdout", log_text)

    def test_auto_codex_worker_can_report_with_run_id_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = base / "repo"
            bin_dir = base / "bin"
            source_path = Path(__file__).resolve().parents[1] / "src"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            write_fake_git(bin_dir)
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                f"sys.path.insert(0, {str(source_path)!r})\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "env_payload = {\n"
                "    'run_id': os.environ['VIBE_LOOP_RUN_ID'],\n"
                "    'task_id': os.environ['VIBE_LOOP_TASK_ID'],\n"
                "    'repo': os.environ['VIBE_LOOP_REPO'],\n"
                "    'log': os.environ['VIBE_LOOP_LOG'],\n"
                "}\n"
                "Path('agent-env.json').write_text(\n"
                "    json.dumps(env_payload),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main([\n"
                "        'report',\n"
                "        '--repo', '.',\n"
                "        '--run-id', env_payload['run_id'],\n"
                "        '--task-id', env_payload['task_id'],\n"
                "        '--status', 'completed',\n"
                "        '--metadata-json', '{\"via\":\"env\"}',\n"
                "    ])\n"
                ")\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)
            env_payload = json.loads(
                (repo / "agent-env.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertEqual(payload["classification_source"], "worker_report")
        self.assertEqual(payload["worker_report"]["metadata"], {"via": "env"})
        self.assertEqual(env_payload["run_id"], payload["run_id"])
        self.assertEqual(env_payload["task_id"], "TASK-01")
        self.assertEqual(env_payload["repo"], str(repo))
        self.assertEqual(env_payload["log"], payload["log"])
        self.assertIn("agent command source: auto:codex", stderr.getvalue())

    def test_install_skills_are_cli_agnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["install-skills", "--codex", "--home", str(home)])

            installed_paths = stdout.getvalue().splitlines()
            finite = home / ".codex" / "skills" / "vibe-loop" / "SKILL.md"
            infinite = home / ".codex" / "skills" / "infinite-vibe-loop" / "SKILL.md"
            orchestrated = (
                home / ".codex" / "skills" / "orchestrated-vibe-loop" / "SKILL.md"
            )
            finite_text = finite.read_text(encoding="utf-8")
            infinite_text = infinite.read_text(encoding="utf-8")
            orchestrated_text = orchestrated.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            installed_paths,
            [
                str(home / ".codex" / "skills" / "vibe-loop"),
                str(home / ".codex" / "skills" / "infinite-vibe-loop"),
                str(home / ".codex" / "skills" / "orchestrated-vibe-loop"),
            ],
        )
        for text in (finite_text, infinite_text, orchestrated_text):
            self.assertNotRegex(text, r"\bVIBE_LOOP_[A-Z0-9_]+\b")
            self.assertNotRegex(
                text,
                r"\bvibe-loop\s+"
                r"(?:report|worker|main-integration|tasks|next|run-next|"
                r"run-until-done|workers|runs|planning|doctor|install-skills|"
                r"specs|eval)\b",
            )

    def test_cli_worker_addendum_contains_coordination(self) -> None:
        from vibe_loop.runner import CLI_WORKER_ADDENDUM

        workspace_claim = CLI_WORKER_ADDENDUM.index("### Workspace Claim")
        worker_reports = CLI_WORKER_ADDENDUM.index("### Worker Reports")
        integration_locking = CLI_WORKER_ADDENDUM.index("### Integration Locking")
        self.assertLess(workspace_claim, worker_reports)
        self.assertLess(worker_reports, integration_locking)

        self.assertIn(
            "After creating or choosing your task branch/worktree, and before "
            "implementation\nedits",
            CLI_WORKER_ADDENDUM,
        )
        self.assertIn("vibe-loop worker claim-workspace", CLI_WORKER_ADDENDUM)
        self.assertIn("--branch <branch-name>", CLI_WORKER_ADDENDUM)
        self.assertIn("--worktree <absolute-worktree-path>", CLI_WORKER_ADDENDUM)
        self.assertRegex(
            CLI_WORKER_ADDENDUM,
            r"claim fails with an owner mismatch[\s\S]*report the run as blocked",
        )
        self.assertIn("advisory visibility metadata only", CLI_WORKER_ADDENDUM)
        self.assertIn(
            "do not permit deleting,\nresetting, cleaning, merging, or stealing",
            CLI_WORKER_ADDENDUM,
        )
        self.assertIn('vibe-loop report --repo "$VIBE_LOOP_REPO"', CLI_WORKER_ADDENDUM)
        self.assertIn("vibe-loop main-integration acquire", CLI_WORKER_ADDENDUM)
        self.assertIn("--wait --timeout 300", CLI_WORKER_ADDENDUM)
        self.assertRegex(
            CLI_WORKER_ADDENDUM,
            r"live holder timeout[\s\S]*park the slice as blocked",
        )
        self.assertRegex(
            CLI_WORKER_ADDENDUM,
            r"workspace preflight[\s\S]*report the run as blocked",
        )
        self.assertIn("vibe-loop main-integration release", CLI_WORKER_ADDENDUM)
        self.assertIn("VIBE_LOOP_RUN_ID", CLI_WORKER_ADDENDUM)
        self.assertIn("VIBE_LOOP_TASK_ID", CLI_WORKER_ADDENDUM)

    def test_run_next_uses_claude_default_when_only_claude_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            write_fake_git(bin_dir)
            write_python_executable(
                bin_dir / "claude",
                "from pathlib import Path\n"
                "import sys\n"
                "if sys.argv[1] != '-p':\n"
                "    raise SystemExit(64)\n"
                "Path('agent-args.txt').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "print(f'claude out: {sys.argv[2]}')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            agent_args = (repo / "agent-args.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        agent_lines = agent_args.split("\n")
        self.assertEqual(agent_lines[0], "-p")
        self.assertIn("/vibe-loop TASK-01", agent_lines[1])
        self.assertIn("vibe-loop CLI Coordination", agent_args)
        self.assertIn("agent command source: auto:claude", stderr.getvalue())
        self.assertIn("agent_command_source=auto:claude", log_text)

    def test_next_uses_codex_default_selection_when_only_codex_is_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "Path('selection-prompt.txt').write_text(sys.argv[2], encoding='utf-8')\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["next", "--repo", str(repo), "--ask-agent"])

            prompt = (repo / "selection-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue().strip(), "TASK-02")
        self.assertIn("TASK-01", prompt)
        self.assertIn("agent selection command source: auto:codex", stderr.getvalue())

    def test_next_uses_codex_first_selection_when_both_agents_are_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "Path('selection-prompt.txt').write_text(sys.argv[2], encoding='utf-8')\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
            )
            write_python_executable(bin_dir / "claude", "raise SystemExit(99)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["next", "--repo", str(repo), "--ask-agent", "--json"]
                    )

            prompt = (repo / "selection-prompt.txt").read_text(encoding="utf-8")
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["id"], "TASK-02")
        self.assertEqual(
            payload["agent_selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload["agent_default_policy_source"], "codex-first")
        self.assertIn("Codex", payload["agent_default_policy"])
        self.assertIn("TASK-01", prompt)
        self.assertIn(
            "agent selection command source: auto:codex:codex-first",
            stderr.getvalue(),
        )
        self.assertIn("agent default policy source: codex-first", stderr.getvalue())
        self.assertIn("agent default policy:", stderr.getvalue())

    def test_tasks_next_json_reports_selection_policy_with_both_agents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
            )
            write_python_executable(bin_dir / "claude", "raise SystemExit(99)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "tasks",
                            "next",
                            "--repo",
                            str(repo),
                            "--ask-agent",
                            "--json",
                        ]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["id"], "TASK-02")
        self.assertEqual(
            payload["agent_selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload["agent_default_policy_source"], "codex-first")

    def test_run_next_uses_codex_first_default_when_both_agents_are_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            write_fake_git(bin_dir)
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "import sys\n"
                "if sys.argv[1] != 'exec':\n"
                "    raise SystemExit(64)\n"
                "Path('agent-args.txt').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
            )
            write_python_executable(bin_dir / "claude", "raise SystemExit(99)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            agent_args = (repo / "agent-args.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertEqual(payload["agent_command_source"], "auto:codex:codex-first")
        self.assertEqual(
            payload["agent_selection_command_source"], "auto:codex:codex-first"
        )
        self.assertEqual(payload["agent_default_policy_source"], "codex-first")
        self.assertIn("Codex", payload["agent_default_policy"])
        self.assertEqual(payload["agent_prompt_dialect"], "codex")
        self.assertEqual(
            payload["agent_prompt_dialect_source"], "auto:codex:codex-first"
        )
        agent_lines = agent_args.split("\n")
        self.assertEqual(agent_lines[0], "exec")
        self.assertIn("$vibe-loop TASK-01", agent_lines[1])
        self.assertIn("vibe-loop CLI Coordination", agent_args)
        self.assertIn("agent command source: auto:codex:codex-first", stderr.getvalue())
        self.assertIn(
            "agent selection command source: auto:codex:codex-first",
            stderr.getvalue(),
        )
        self.assertIn("agent default policy source: codex-first", stderr.getvalue())
        self.assertIn("agent default policy:", stderr.getvalue())
        self.assertIn(
            "agent prompt dialect source: auto:codex:codex-first",
            stderr.getvalue(),
        )
        self.assertIn("agent_command_source=auto:codex:codex-first", log_text)
        self.assertIn(
            "agent_selection_command_source=auto:codex:codex-first",
            log_text,
        )
        self.assertIn("agent_default_policy_source=codex-first", log_text)
        self.assertIn("agent_default_policy=", log_text)
        self.assertIn("agent_prompt_dialect_source=auto:codex:codex-first", log_text)

    def test_run_until_done_uses_codex_first_default_when_both_agents_are_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            write_fake_git(bin_dir)
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
            )
            write_python_executable(bin_dir / "claude", "raise SystemExit(99)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-until-done", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["classification"], "completed")
        self.assertEqual(payload[0]["agent_command_source"], "auto:codex:codex-first")
        self.assertEqual(
            payload[0]["agent_selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload[0]["agent_default_policy_source"], "codex-first")
        self.assertIn("agent command source: auto:codex:codex-first", stderr.getvalue())

    def test_run_until_done_jobs_runs_independent_tasks_concurrently(self) -> None:
        with temporary_directory_with_cleanup_retry() as directory:
            repo = Path(directory) / "repo"
            source_path = Path(__file__).resolve().parents[1] / "src"
            repo.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "import time\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "task_id = os.environ['VIBE_LOOP_TASK_ID']\n"
                "run_id = os.environ['VIBE_LOOP_RUN_ID']\n"
                "log_path = os.environ['VIBE_LOOP_LOG']\n"
                "started = Path('started')\n"
                "started.mkdir(exist_ok=True)\n"
                "(started / task_id).write_text(run_id, encoding='utf-8')\n"
                "deadline = time.monotonic() + 15\n"
                "while len(list(started.iterdir())) < 2:\n"
                "    if time.monotonic() > deadline:\n"
                "        raise SystemExit('parallel barrier timed out')\n"
                "    time.sleep(0.02)\n"
                "lock_paths = sorted(str(path) for path in Path('.vibe-loop/locks').glob('*.lock'))\n"
                "lock_task_ids = []\n"
                "for lock_path in Path('.vibe-loop/locks').glob('*.lock'):\n"
                "    metadata_path = lock_path / 'lock.json'\n"
                "    if metadata_path.exists():\n"
                "        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))\n"
                "        lock_task_ids.append(metadata.get('task_id'))\n"
                "observed = Path('observed')\n"
                "observed.mkdir(exist_ok=True)\n"
                "(observed / f'{task_id}.json').write_text(\n"
                "    json.dumps(\n"
                "        {\n"
                "            'task_id': task_id,\n"
                "            'run_id': run_id,\n"
                "            'log': log_path,\n"
                "            'locks': lock_paths,\n"
                "            'lock_task_ids': sorted(lock_task_ids),\n"
                "        },\n"
                "        sort_keys=True,\n"
                "    ),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "second_deadline = time.monotonic() + 15\n"
                "while len(list(observed.glob('*.json'))) < 2:\n"
                "    if time.monotonic() > second_deadline:\n"
                "        raise SystemExit('observation barrier timed out')\n"
                "    time.sleep(0.02)\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', run_id,\n"
                "            '--task-id', task_id,\n"
                "            '--status', 'completed',\n"
                "            '--metadata-json', json.dumps({'lock_count': len(lock_paths)}),\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--jobs",
                        "2",
                        "--max-slices",
                        "2",
                    ]
                )

            payload = parse_run_result(self, stdout, stderr, exit_code)
            observations = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((repo / "observed").glob("*.json"))
            ]
            log_texts = {
                str(result["task_id"]): Path(str(result["log"])).read_text(
                    encoding="utf-8"
                )
                for result in payload
                if Path(str(result["log"])).is_file()
            }

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload), 2)
        self.assertEqual(
            sorted(result["task_id"] for result in payload),
            ["TASK-01", "TASK-02"],
        )
        self.assertTrue(
            all(result["classification"] == "completed" for result in payload)
        )
        self.assertEqual(len({result["log"] for result in payload}), 2)
        self.assertEqual(len(observations), 2)
        self.assertTrue(
            all(len(observation["locks"]) == 2 for observation in observations)
        )
        self.assertTrue(
            all(
                observation["lock_task_ids"] == ["TASK-01", "TASK-02"]
                for observation in observations
            )
        )
        self.assertIn("[vibe-loop] parallel supervisor jobs=2", stderr.getvalue())
        for result in payload:
            log_text = log_texts.get(str(result["task_id"]), "")
            self.assertTrue(log_text)
            self.assertIn(f"[vibe-loop] task_id={result['task_id']}", log_text)
            self.assertIn("worker report status=completed", log_text)
            self.assertIn(f"[vibe-loop] log: {result['log']}", stderr.getvalue())

    def test_run_until_done_max_tasks_stops_after_completed_budget(self) -> None:
        with temporary_directory_with_cleanup_retry() as directory:
            repo = Path(directory) / "repo"
            source_path = Path(__file__).resolve().parents[1] / "src"
            repo.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(THREE_TASK_PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "import os\n"
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', os.environ['VIBE_LOOP_RUN_ID'],\n"
                "            '--task-id', os.environ['VIBE_LOOP_TASK_ID'],\n"
                "            '--status', 'completed',\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--jobs",
                        "2",
                        "--max-tasks",
                        "2",
                    ]
                )

            payload = parse_run_result(self, stdout, stderr, exit_code)

        self.assertEqual(exit_code, 0)
        # Three tasks are runnable but the completed budget is two: the loop
        # must stop after two completed slices and never dispatch the third.
        self.assertEqual(len(payload), 2)
        self.assertTrue(
            all(result["classification"] == "completed" for result in payload)
        )
        self.assertEqual(
            sorted(result["task_id"] for result in payload),
            ["TASK-01", "TASK-02"],
        )
        self.assertNotIn("TASK-03", {result["task_id"] for result in payload})
        self.assertIn("[vibe-loop] parallel supervisor jobs=2", stderr.getvalue())

    def test_run_until_done_jobs_uses_agent_batch_selection(self) -> None:
        with temporary_directory_with_cleanup_retry() as directory:
            repo = Path(directory) / "repo"
            source_path = Path(__file__).resolve().parents[1] / "src"
            repo.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(THREE_TASK_PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "task_id = os.environ['VIBE_LOOP_TASK_ID']\n"
                "started = Path('started')\n"
                "started.mkdir(exist_ok=True)\n"
                "(started / task_id).write_text('ran', encoding='utf-8')\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', os.environ['VIBE_LOOP_RUN_ID'],\n"
                "            '--task-id', task_id,\n"
                "            '--status', 'completed',\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            (repo / "selector.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "Path('batch-prompt.txt').write_text(sys.argv[1], encoding='utf-8')\n"
                "print(json.dumps({'task_ids': ['TASK-03', 'TASK-01']}))\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            selector = f"{sys.executable} selector.py {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                "command = "
                + json.dumps(command)
                + "\nselection_command = "
                + json.dumps(selector)
                + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--ask-agent",
                        "--jobs",
                        "2",
                        "--max-slices",
                        "2",
                    ]
                )

            payload = parse_run_result(self, stdout, stderr, exit_code)
            prompt = (repo / "batch-prompt.txt").read_text(encoding="utf-8")
            started = sorted(path.name for path in (repo / "started").iterdir())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sorted(result["task_id"] for result in payload),
            ["TASK-01", "TASK-03"],
        )
        self.assertEqual(started, ["TASK-01", "TASK-03"])
        self.assertIn('"max_batch_size": 2', prompt)
        self.assertIn("No active vibe-loop workers recorded.", prompt)
        self.assertIn("agent selected batch: TASK-03, TASK-01", stderr.getvalue())

    def test_run_until_done_jobs_rejects_invalid_agent_batch_before_spawning(
        self,
    ) -> None:
        with temporary_directory_with_cleanup_retry() as directory:
            repo = Path(directory) / "repo"
            source_path = Path(__file__).resolve().parents[1] / "src"
            repo.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(THREE_TASK_PLAN, encoding="utf-8")
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-02.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-02",
                        "run_id": "external-run",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "pid_source": "popen",
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "task_id = os.environ['VIBE_LOOP_TASK_ID']\n"
                "started = Path('started')\n"
                "started.mkdir(exist_ok=True)\n"
                "(started / task_id).write_text('ran', encoding='utf-8')\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', os.environ['VIBE_LOOP_RUN_ID'],\n"
                "            '--task-id', task_id,\n"
                "            '--status', 'completed',\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            (repo / "selector.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "Path('batch-prompt.txt').write_text(sys.argv[1], encoding='utf-8')\n"
                "print(json.dumps({'task_ids': ['TASK-02', 'TASK-02']}))\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            selector = f"{sys.executable} selector.py {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                "command = "
                + json.dumps(command)
                + "\nselection_command = "
                + json.dumps(selector)
                + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--ask-agent",
                        "--jobs",
                        "2",
                        "--max-slices",
                        "2",
                    ]
                )

            payload = parse_run_result(self, stdout, stderr, exit_code)
            prompt = (repo / "batch-prompt.txt").read_text(encoding="utf-8")
            started = sorted(path.name for path in (repo / "started").iterdir())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sorted(result["task_id"] for result in payload),
            ["TASK-01", "TASK-03"],
        )
        self.assertEqual(started, ["TASK-01", "TASK-03"])
        self.assertIn("TASK-02", prompt)
        self.assertIn(
            "agent batch selection rejected: unknown task_id: TASK-02",
            stderr.getvalue(),
        )
        self.assertIn(
            "agent batch selection unavailable or invalid; "
            "using deterministic ready order",
            stderr.getvalue(),
        )

    def test_run_until_done_jobs_executes_spec_derived_dependency_waves(
        self,
    ) -> None:
        with temporary_directory_with_cleanup_retry() as directory:
            repo = Path(directory) / "repo"
            source_path = Path(__file__).resolve().parents[1] / "src"
            tasks_path = repo / "specs" / "001-checkout" / "tasks.md"
            tasks_path.parent.mkdir(parents=True)
            tasks_path.write_text(
                "# Tasks: Checkout Flow\n\n"
                "- [ ] T001 Add checkout API\n"
                "  - Conflict Resources: api\n"
                "  - Conflict Paths: src/api\n"
                "  - Evidence: pytest tests/test_api.py\n"
                "- [ ] T004 Add checkout migration (depends on T001, T002)\n"
                "  - Conflict Resources: db\n"
                "  - Conflict Paths: db/migrations\n"
                "  - Evidence: pytest tests/test_migrations.py\n"
                "- [ ] T003 Add checkout API client\n"
                "  - Conflict Resources: api\n"
                "  - Conflict Paths: src/api/client.py\n"
                "  - Evidence: pytest tests/test_client.py\n"
                "- [ ] T002 Update checkout docs\n"
                "  - Conflict Resources: docs\n"
                "  - Conflict Paths: docs/checkout.md\n"
                "  - Evidence: markdownlint docs/checkout.md\n",
                encoding="utf-8",
            )
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "import time\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from vibe_loop.cli import main\n"
                "task_id = os.environ['VIBE_LOOP_TASK_ID']\n"
                "run_id = os.environ['VIBE_LOOP_RUN_ID']\n"
                "local_id = task_id.split(':', 1)[-1]\n"
                "safe_id = task_id.replace(':', '__')\n"
                "done = Path('done')\n"
                "done.mkdir(exist_ok=True)\n"
                "done_at_start = sorted(path.name for path in done.iterdir())\n"
                "wave = 'wave1' if local_id in {'T001', 'T002'} else 'wave2'\n"
                "started_root = Path('started')\n"
                "started_all = started_root / 'all'\n"
                "started_all.mkdir(parents=True, exist_ok=True)\n"
                "(started_all / safe_id).write_text(run_id, encoding='utf-8')\n"
                "started = started_root / wave\n"
                "started.mkdir(parents=True, exist_ok=True)\n"
                "(started / safe_id).write_text(run_id, encoding='utf-8')\n"
                "if wave == 'wave1':\n"
                "    expected_wave1 = {'001-checkout__T001', '001-checkout__T002'}\n"
                "    deadline = time.monotonic() + 15\n"
                "    while len(list(started.iterdir())) < 2:\n"
                "        unexpected = sorted(\n"
                "            path.name\n"
                "            for path in started_all.iterdir()\n"
                "            if path.name not in expected_wave1\n"
                "        )\n"
                "        if unexpected:\n"
                "            raise SystemExit(f'unexpected first wave: {unexpected}')\n"
                "        if time.monotonic() > deadline:\n"
                "            raise SystemExit(f'{wave} barrier timed out for {task_id}')\n"
                "        time.sleep(0.02)\n"
                "lock_task_ids = []\n"
                "for lock_path in Path('.vibe-loop/locks').glob('*.lock'):\n"
                "    metadata_path = lock_path / 'lock.json'\n"
                "    if metadata_path.exists():\n"
                "        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))\n"
                "        lock_task_ids.append(metadata.get('task_id'))\n"
                "observed = Path('observed')\n"
                "observed.mkdir(exist_ok=True)\n"
                "(observed / f'{safe_id}.json').write_text(\n"
                "    json.dumps(\n"
                "        {\n"
                "            'task_id': task_id,\n"
                "            'wave': wave,\n"
                "            'done_at_start': done_at_start,\n"
                "            'lock_task_ids': sorted(lock_task_ids),\n"
                "        },\n"
                "        sort_keys=True,\n"
                "    ),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "if wave == 'wave1':\n"
                "    scanned = Path('scanned') / wave\n"
                "    scanned.mkdir(parents=True, exist_ok=True)\n"
                "    (scanned / safe_id).write_text(run_id, encoding='utf-8')\n"
                "    deadline = time.monotonic() + 15\n"
                "    while len(list(scanned.iterdir())) < 2:\n"
                "        if time.monotonic() > deadline:\n"
                "            raise SystemExit(f'{wave} scan barrier timed out for {task_id}')\n"
                "        time.sleep(0.02)\n"
                "lock = Path('.task-update.lock')\n"
                "deadline = time.monotonic() + 15\n"
                "while True:\n"
                "    try:\n"
                "        lock.mkdir()\n"
                "        break\n"
                "    except FileExistsError:\n"
                "        if time.monotonic() > deadline:\n"
                "            raise SystemExit('task update lock timed out')\n"
                "        time.sleep(0.02)\n"
                "try:\n"
                "    tasks = Path('specs/001-checkout/tasks.md')\n"
                "    text = tasks.read_text(encoding='utf-8')\n"
                "    text = text.replace(f'- [ ] {local_id}', f'- [x] {local_id}', 1)\n"
                "    tasks.write_text(text, encoding='utf-8')\n"
                "finally:\n"
                "    lock.rmdir()\n"
                "(done / safe_id).write_text(local_id, encoding='utf-8')\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', run_id,\n"
                "            '--task-id', task_id,\n"
                "            '--status', 'completed',\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                'type = "spec-kit"\n\n'
                "[agent]\n"
                "command = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--jobs",
                        "2",
                        "--max-tasks",
                        "4",
                    ]
                )

            payload = parse_run_result(self, stdout, stderr, exit_code)
            observed = {
                path.stem.replace("__", ":"): json.loads(
                    path.read_text(encoding="utf-8")
                )
                for path in (repo / "observed").glob("*.json")
            }
            wave1_started = {
                path.name.replace("__", ":")
                for path in (repo / "started" / "wave1").iterdir()
            }
            final_tasks = tasks_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload), 4)
        self.assertTrue(
            all(result["classification"] == "completed" for result in payload)
        )
        self.assertEqual(
            wave1_started,
            {"001-checkout:T001", "001-checkout:T002"},
        )
        self.assertEqual(
            sorted(observed["001-checkout:T001"]["lock_task_ids"]),
            ["001-checkout:T001", "001-checkout:T002"],
        )
        self.assertEqual(observed["001-checkout:T004"]["wave"], "wave2")
        self.assertIn(
            "001-checkout__T001", observed["001-checkout:T004"]["done_at_start"]
        )
        self.assertIn(
            "001-checkout__T002", observed["001-checkout:T004"]["done_at_start"]
        )
        self.assertIn("- [x] T004 Add checkout migration", final_tasks)
        self.assertIn("[vibe-loop] parallel supervisor jobs=2", stderr.getvalue())

    def test_run_until_done_continue_on_failure_exits_nonzero_for_any_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source_path = Path(__file__).resolve().parents[1] / "src"
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "import os\n"
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from vibe_loop.cli import main\n"
                "task_id = os.environ['VIBE_LOOP_TASK_ID']\n"
                "status = 'failed' if task_id == 'TASK-01' else 'completed'\n"
                "raise SystemExit(\n"
                "    main(\n"
                "        [\n"
                "            'report',\n"
                "            '--repo', '.',\n"
                "            '--run-id', os.environ['VIBE_LOOP_RUN_ID'],\n"
                "            '--task-id', task_id,\n"
                "            '--status', status,\n"
                "        ]\n"
                "    )\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {source_path}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "run-until-done",
                        "--repo",
                        str(repo),
                        "--continue-on-failure",
                        "--max-slices",
                        "2",
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            [result["classification"] for result in payload],
            ["failed", "completed"],
        )
        self.assertEqual(stderr.getvalue().count("[vibe-loop] running TASK-"), 2)

    def test_run_until_done_requires_agent_when_no_supported_cli_is_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-until-done", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("no supported agent CLI was found", stderr.getvalue())
        self.assertIn("agent.command", stderr.getvalue())

    def test_run_next_validates_worker_before_running_explicit_selector(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nselection_command = "selector {prompt}"\n',
                encoding="utf-8",
            )
            write_python_executable(
                bin_dir / "selector",
                "from pathlib import Path\n"
                "Path('selector-ran').write_text('ran', encoding='utf-8')\n"
                'print(\'{"task_id":"TASK-02"}\')\n',
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo), "--ask-agent"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("agent.command", stderr.getvalue())
        self.assertFalse((repo / "selector-ran").exists())

    def test_run_next_validates_custom_prompt_syntax_before_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "custom"\n'
                'command = "worker {prompt}"\n'
                'selection_command = "selector {prompt}"\n',
                encoding="utf-8",
            )
            write_python_executable(
                bin_dir / "selector",
                "from pathlib import Path\n"
                "Path('selector-ran').write_text('ran', encoding='utf-8')\n"
                'print(\'{"task_id":"TASK-02"}\')\n',
            )
            write_python_executable(
                bin_dir / "worker",
                "from pathlib import Path\n"
                "Path('worker-ran').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo), "--ask-agent"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("agent.kind is custom", stderr.getvalue())
        self.assertFalse((repo / "selector-ran").exists())
        self.assertFalse((repo / "worker-ran").exists())

    def test_parallel_run_validates_custom_prompt_syntax_before_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "custom"\n'
                'command = "worker {prompt}"\n'
                'selection_command = "selector {prompt}"\n',
                encoding="utf-8",
            )
            write_python_executable(
                bin_dir / "selector",
                "from pathlib import Path\n"
                "Path('selector-ran').write_text('ran', encoding='utf-8')\n"
                'print(\'{"task_ids":["TASK-01","TASK-02"]}\')\n',
            )
            write_python_executable(
                bin_dir / "worker",
                "from pathlib import Path\n"
                "Path('worker-ran').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "run-until-done",
                            "--repo",
                            str(repo),
                            "--jobs",
                            "2",
                            "--ask-agent",
                        ]
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("agent.kind is custom", stderr.getvalue())
        self.assertFalse((repo / "selector-ran").exists())
        self.assertFalse((repo / "worker-ran").exists())

    def test_doctor_reports_agent_detection_and_command_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_python_executable(bin_dir / "codex", "raise SystemExit(0)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("command", payload["agent"])
        self.assertEqual(payload["agent"]["command_source"], "auto:codex")
        self.assertEqual(payload["agent"]["selection_command_source"], "auto:codex")
        self.assertEqual(payload["agent"]["agent_kind"], "auto")
        self.assertEqual(payload["agent"]["prompt_dialect"], "codex")
        self.assertEqual(payload["agent"]["prompt_dialect_source"], "auto:codex")
        self.assertEqual(payload["agent"]["skill_ref_prefix"], "$")
        self.assertEqual(payload["agent"]["detected"]["available"], ["codex"])

    def test_doctor_reports_codex_first_policy_with_both_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_python_executable(bin_dir / "codex", "raise SystemExit(0)\n")
            write_python_executable(bin_dir / "claude", "raise SystemExit(0)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex:codex-first")
        self.assertEqual(
            payload["agent"]["selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload["agent"]["detected"]["available"], ["codex", "claude"])
        self.assertEqual(payload["agent"]["default_policy_source"], "codex-first")
        self.assertIn("Codex", payload["agent"]["default_policy"])
        self.assertEqual(payload["agent"]["prompt_dialect"], "codex")
        self.assertEqual(
            payload["agent"]["prompt_dialect_source"],
            "auto:codex:codex-first",
        )

    def test_doctor_reports_custom_missing_prompt_dialect_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "custom"\n'
                'command = "PRIVATE_PATH=/tmp/private custom-worker {prompt}"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("command", payload["agent"])
        self.assertNotIn("custom-worker", stdout.getvalue())
        self.assertNotIn("/tmp/private", stdout.getvalue())
        self.assertEqual(payload["agent"]["agent_kind"], "custom")
        self.assertIsNone(payload["agent"]["prompt_dialect"])
        self.assertIsNone(payload["agent"]["skill_ref_prefix"])
        self.assertIn(
            "agent.kind is custom",
            "\n".join(payload["agent"]["diagnostics"]),
        )

    def test_doctor_redacts_user_authored_command_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            lock_acquire_command = shell_command(
                sys.executable,
                "-c",
                "import json; print(json.dumps({'acquired': False, 'metadata': {}}))",
                "PRIVATE_VALUE",
            )
            lock_release_command = shell_command(
                sys.executable,
                "-c",
                "import json; print(json.dumps({'released': True}))",
                "PRIVATE_VALUE",
            )
            lock_status_command = shell_command(
                sys.executable,
                "-c",
                "import json; print(json.dumps({'locked': False}))",
                "PRIVATE_VALUE",
            )
            lock_list_command = shell_command(
                sys.executable,
                "-c",
                "import json; print(json.dumps([]))",
                "PRIVATE_VALUE",
            )
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                'list = "PRIVATE_VALUE tracker list"\n'
                'next = "PRIVATE_VALUE tracker next"\n'
                'probe = "PRIVATE_VALUE tracker probe"\n'
                "[locks]\n"
                'type = "command"\n'
                f"acquire_command = {json.dumps(lock_acquire_command)}\n"
                f"release_command = {json.dumps(lock_release_command)}\n"
                f"status_command = {json.dumps(lock_status_command)}\n"
                f"list_command = {json.dumps(lock_list_command)}\n"
                "[completion]\n"
                'commands = ["PRIVATE_VALUE completion"]\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("PRIVATE_VALUE", stdout.getvalue())
        self.assertNotIn("tracker list", stdout.getvalue())
        self.assertNotIn("locks acquire", stdout.getvalue())
        self.assertNotIn("PRIVATE_VALUE completion", stdout.getvalue())
        self.assertNotIn("list_command", payload["task_source"])
        self.assertTrue(payload["task_source"]["list_command_configured"])
        self.assertTrue(payload["task_source"]["list_command_redacted"])
        runtime_source = payload["task_source_runtime"]["task_source"]
        self.assertNotIn("list_command", runtime_source)
        self.assertTrue(runtime_source["list_command_configured"])
        self.assertNotIn("acquire_command", payload["locks"])
        self.assertTrue(payload["locks"]["acquire_command_configured"])
        self.assertEqual(payload["completion"]["commands_configured"], 1)
        self.assertTrue(payload["completion"]["commands_redacted"])

    def test_doctor_reports_planning_analytics_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(payload["task_source_runtime"]["usable"])
        self.assertEqual(payload["planning_analytics"]["status"], "ready")
        self.assertEqual(
            payload["planning_analytics"]["schedule_policy"],
            "current-runner-parity",
        )
        self.assertFalse(payload["planning_analytics"]["repo_artifact_outputs_enabled"])
        self.assertEqual(
            payload["planning_analytics"]["outputs"]["timeline_json"]["source"],
            "default_state_dir",
        )
        self.assertEqual(payload["specs"]["status"], "not_configured")
        self.assertEqual(payload["specs"]["diagnostics"], [])

    def test_doctor_reports_spec_diagnostics_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            spec_text = "current spec\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            stale_fingerprint = {
                "path": "docs/spec.md",
                "size": 1,
                "sha256": "0" * 64,
            }
            (repo / "WORK.md").write_text(
                spec_task_table(fingerprint=stale_fingerprint),
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                spec_profile_config(
                    specs_section=(
                        "[specs]\n"
                        "require_approved = true\n"
                        "require_current_fingerprints = true\n"
                        "require_requirement_coverage = true\n"
                        "require_completion_evidence = true\n"
                        'override_commands = ["make specs-override"]\n'
                    )
                ),
                encoding="utf-8",
            )
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('agent-ran').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            agent_ran = (repo / "agent-ran").exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(agent_ran)
        self.assertEqual(payload["specs"]["status"], "blocked")
        self.assertTrue(payload["specs"]["enforced"])
        self.assertEqual(payload["specs"]["override_commands"], ["make specs-override"])
        diagnostic_codes = {
            diagnostic["code"] for diagnostic in payload["specs"]["diagnostics"]
        }
        self.assertIn("unapproved_spec", diagnostic_codes)
        self.assertIn("stale_source_fingerprint", diagnostic_codes)
        self.assertIn("missing_requirement_coverage", diagnostic_codes)
        self.assertIn("completed_task_missing_evidence", diagnostic_codes)

    def test_doctor_and_specs_skip_command_task_source_without_running_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "task_source.py").write_text(
                "from pathlib import Path\n"
                "Path('task-source-ran').write_text('ran', encoding='utf-8')\n"
                "print('{\"tasks\": []}')\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                'type = "command"\n'
                f"list = {toml_string(f'{sys.executable} task_source.py')}\n"
                "[specs]\n"
                "require_approved = true\n",
                encoding="utf-8",
            )
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()
            specs_stdout = StringIO()
            specs_stderr = StringIO()

            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo)])
            with redirect_stdout(specs_stdout), redirect_stderr(specs_stderr):
                specs_exit = main(["specs", "check", "--repo", str(repo), "--json"])
            command_ran = (repo / "task-source-ran").exists()
            doctor_payload = json.loads(doctor_stdout.getvalue())
            specs_payload = json.loads(specs_stdout.getvalue())

        self.assertEqual(doctor_exit, 0)
        self.assertEqual(specs_exit, 1)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(specs_stderr.getvalue(), "")
        self.assertFalse(command_ran)
        self.assertEqual(
            doctor_payload["specs"]["diagnostics"][0]["code"],
            "command_task_source_unchecked",
        )
        self.assertEqual(
            specs_payload["diagnostics"][0]["code"],
            "command_task_source_unchecked",
        )

    def test_doctor_command_task_source_without_specs_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "task_source.py").write_text(
                "from pathlib import Path\n"
                "Path('task-source-ran').write_text('ran', encoding='utf-8')\n"
                "print('{\"tasks\": []}')\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                'type = "command"\n'
                f"list = {toml_string(f'{sys.executable} task_source.py')}\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse((repo / "task-source-ran").exists())
        self.assertEqual(payload["specs"]["status"], "not_configured")
        self.assertEqual(payload["specs"]["diagnostics"], [])
        self.assertEqual(payload["task_source_runtime"]["origin"], "command_output")

    def test_specs_check_advisory_diagnostics_do_not_fail_without_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "docs").mkdir()
            spec_text = "current spec\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            stale_fingerprint = {
                "path": "docs/spec.md",
                "size": len(spec_text),
                "sha256": "0" * 64,
            }
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| ID | Status | Title | Requirements | Spec Paths | Approval | Fingerprints | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | Planned | Stale draft spec. | REQ-1 | docs/spec.md | draft | "
                f"{json.dumps([stale_fingerprint], separators=(',', ':'))} | Not run. |\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                spec_profile_config(),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["specs", "check", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "issues")
        self.assertEqual(payload["blocking_count"], 0)
        self.assertEqual(
            {diagnostic["severity"] for diagnostic in payload["diagnostics"]},
            {"warning"},
        )

    def test_specs_check_returns_nonzero_for_enforced_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "docs").mkdir()
            spec_text = "current spec\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            stale_fingerprint = {
                "path": "docs/spec.md",
                "size": len(spec_text),
                "sha256": "0" * 64,
            }
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| ID | Status | Title | Requirements | Spec Paths | Approval | Fingerprints | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | Planned | Stale spec. | REQ-1 | docs/spec.md | approved | "
                f"{json.dumps([stale_fingerprint], separators=(',', ':'))} | Not run. |\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                spec_profile_config(
                    specs_section="[specs]\nrequire_current_fingerprints = true\n"
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["specs", "check", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["diagnostics"][0]["code"], "stale_source_fingerprint")

    def test_specs_check_enforces_approval_and_fingerprints_without_traceability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                "[specs]\n"
                "require_approved = true\n"
                "require_current_fingerprints = true\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["specs", "check", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        diagnostic_codes = {diagnostic["code"] for diagnostic in payload["diagnostics"]}
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("missing_spec_approval", diagnostic_codes)
        self.assertIn("missing_source_fingerprint", diagnostic_codes)

    def test_specs_check_includes_task_source_failure_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "PLAN.md").write_text("# No tasks\n", encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                "[specs]\nrequire_current_fingerprints = true\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["specs", "check", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["diagnostics"][0]["code"], "task_source_unusable")
        self.assertIn("task source is not usable", payload["diagnostics"][0]["message"])

    def test_specs_check_rejects_fingerprint_symlink_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "docs").mkdir()
            outside = root / "outside-spec.md"
            outside.write_text("outside spec\n", encoding="utf-8")
            try:
                os.symlink(outside, repo / "docs" / "spec.md")
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            fingerprint = {
                "path": "docs/spec.md",
                "size": outside.stat().st_size,
                "sha256": "0" * 64,
            }
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| ID | Status | Title | Requirements | Spec Paths | Approval | Fingerprints | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | Planned | Outside symlink. | REQ-1 | docs/spec.md | approved | "
                f"{json.dumps([fingerprint], separators=(',', ':'))} | Not run. |\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                spec_profile_config(
                    specs_section="[specs]\nrequire_current_fingerprints = true\n"
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["specs", "check", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(
            payload["diagnostics"][0]["code"],
            "unsafe_source_fingerprint_path",
        )
        self.assertIn("outside repository", payload["diagnostics"][0]["message"])

    def test_run_next_spec_gate_blocks_before_agent_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "docs").mkdir()
            spec_text = "current spec\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            stale_fingerprint = {
                "path": "docs/spec.md",
                "size": len(spec_text),
                "sha256": "0" * 64,
            }
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| ID | Status | Title | Requirements | Spec Paths | Approval | Fingerprints | Evidence |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| TASK-01 | Planned | Stale spec. | REQ-1 | docs/spec.md | approved | "
                f"{json.dumps([stale_fingerprint], separators=(',', ':'))} | Not run. |\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                spec_profile_config(
                    specs_section="[specs]\nrequire_current_fingerprints = true\n"
                ),
                encoding="utf-8",
            )
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('agent-ran').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])
            agent_ran = (repo / "agent-ran").exists()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("spec execution gate blocked", stderr.getvalue())
        self.assertFalse(agent_ran)

    def test_planning_timeline_json_command_emits_versioned_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    ["planning", "timeline", "--repo", str(repo), "--json"]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["generated_by"], "vibe-loop planning timeline")
        self.assertEqual(payload["tasks"][0]["id"], "TASK-01")
        self.assertEqual(payload["tasks"][0]["projected"]["estimate"]["minutes"], 60)
        self.assertEqual(
            payload["source_provenance"]["projection"]["anchor_source"],
            "default_epoch_no_actual_or_git_evidence",
        )

    def test_planning_artifacts_generate_writes_default_state_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            init_planning_repo(repo, PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["planning", "artifacts", "--repo", str(repo)])
            runnable_stdout = StringIO()
            runnable_stderr = StringIO()
            with redirect_stdout(runnable_stdout), redirect_stderr(runnable_stderr):
                runnable_exit = main(
                    ["tasks", "runnable", "--repo", str(repo), "--json"]
                )

            timeline_path = repo / ".vibe-loop" / "planning-analytics" / "timeline.json"
            html_path = repo / ".vibe-loop" / "planning-analytics" / "gantt.html"
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            html = html_path.read_text(encoding="utf-8")
            runnable = json.loads(runnable_stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(runnable_exit, 0)
        self.assertIn(f"timeline JSON: {timeline_path}", stdout.getvalue())
        self.assertIn(f"gantt HTML: {html_path}", stdout.getvalue())
        self.assertEqual(timeline["generated_by"], "vibe-loop planning timeline")
        self.assertEqual(timeline["tasks"][0]["id"], "TASK-01")
        self.assertIn("vibe-loop-planning-gantt", html)
        self.assertIn("TASK-01", html)
        self.assertEqual(runnable[0]["id"], "TASK-01")
        self.assertIn(
            "task discovery source=default_markdown_discovery",
            runnable_stderr.getvalue(),
        )

    def test_planning_artifacts_respects_cli_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            init_planning_repo(repo, PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "planning",
                        "artifacts",
                        "--repo",
                        str(repo),
                        "--output",
                        "docs/planning/timeline.json",
                        "--html-output",
                        "docs/planning/gantt.html",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            timeline_path = repo / "docs" / "planning" / "timeline.json"
            html_path = repo / "docs" / "planning" / "gantt.html"
            timeline_exists = timeline_path.is_file()
            html_exists = html_path.is_file()
            inspect_stdout = StringIO()
            inspect_stderr = StringIO()
            with redirect_stdout(inspect_stdout), redirect_stderr(inspect_stderr):
                inspect_exit = main(
                    [
                        "planning",
                        "artifacts",
                        "--repo",
                        str(repo),
                        "--output",
                        "docs/planning/timeline.json",
                        "--html-output",
                        "docs/planning/gantt.html",
                        "--inspect",
                        "--json",
                    ]
                )
            inspected = json.loads(inspect_stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspect_stderr.getvalue(), "")
        self.assertTrue(timeline_exists)
        self.assertTrue(html_exists)
        self.assertEqual(
            payload["paths"]["timeline_json"],
            {"path": str(timeline_path), "source": "cli"},
        )
        self.assertEqual(
            payload["paths"]["gantt_html"],
            {"path": str(html_path), "source": "cli"},
        )
        self.assertIn(
            "--output docs/planning/timeline.json",
            inspected["next_repair_commands"][0],
        )
        self.assertIn(
            "--html-output docs/planning/gantt.html",
            inspected["next_repair_commands"][0],
        )

    def test_planning_artifacts_check_detects_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            timeline_path = repo / ".vibe-loop" / "planning-analytics" / "timeline.json"
            timeline_path.write_text('{"stale": true}\n', encoding="utf-8")
            stale_stdout = StringIO()
            stale_stderr = StringIO()
            with redirect_stdout(stale_stdout), redirect_stderr(stale_stderr):
                stale_exit = main(
                    ["planning", "artifacts", "--repo", str(repo), "--check"]
                )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                refresh_exit = main(["planning", "artifacts", "--repo", str(repo)])
            fresh_stdout = StringIO()
            fresh_stderr = StringIO()
            with redirect_stdout(fresh_stdout), redirect_stderr(fresh_stderr):
                fresh_exit = main(
                    ["planning", "artifacts", "--repo", str(repo), "--check"]
                )

        self.assertEqual(generate_exit, 0)
        self.assertEqual(stale_exit, 1)
        self.assertEqual(stale_stdout.getvalue(), "")
        self.assertIn("timeline JSON artifact is stale", stale_stderr.getvalue())
        self.assertEqual(refresh_exit, 0)
        self.assertEqual(fresh_exit, 0)
        self.assertEqual(fresh_stderr.getvalue(), "")
        self.assertIn("planning artifacts are up to date", fresh_stdout.getvalue())

    def test_planning_artifacts_check_json_reports_unreadable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            timeline_path = repo / ".vibe-loop" / "planning-analytics" / "timeline.json"
            timeline_path.write_bytes(b"\xff\xfe\x00")
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                check_exit = main(
                    [
                        "planning",
                        "artifacts",
                        "--repo",
                        str(repo),
                        "--check",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(generate_exit, 0)
        self.assertEqual(check_exit, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["ok"])
        self.assertIn("cannot be read", payload["errors"][0])

    def test_planning_artifacts_check_accepts_committed_explicit_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            artifact_args = [
                "planning",
                "artifacts",
                "--repo",
                str(repo),
                "--output",
                "docs/planning/timeline.json",
                "--html-output",
                "docs/planning/gantt.html",
            ]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(artifact_args)
            subprocess.run(
                ["git", "add", "docs/planning"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "commit generated planning artifacts"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            check_stdout = StringIO()
            check_stderr = StringIO()
            with redirect_stdout(check_stdout), redirect_stderr(check_stderr):
                check_exit = main([*artifact_args, "--check", "--json"])

            payload = json.loads(check_stdout.getvalue())

        self.assertEqual(generate_exit, 0)
        self.assertEqual(check_exit, 0)
        self.assertEqual(check_stderr.getvalue(), "")
        self.assertTrue(payload["ok"])

    def test_planning_artifacts_render_and_inspect_warnings(self) -> None:
        warning_plan = """# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Planned | MISSING | Blocked task. | Works. | Not run. |
"""
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, warning_plan)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            inspect_stdout = StringIO()
            inspect_stderr = StringIO()
            with redirect_stdout(inspect_stdout), redirect_stderr(inspect_stderr):
                inspect_exit = main(
                    ["planning", "artifacts", "--repo", str(repo), "--inspect"]
                )

            html = (
                repo / ".vibe-loop" / "planning-analytics" / "gantt.html"
            ).read_text(encoding="utf-8")
            timeline = json.loads(
                (
                    repo / ".vibe-loop" / "planning-analytics" / "timeline.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(generate_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspect_stderr.getvalue(), "")
        self.assertIn("unknown_dependency", html)
        self.assertIn("MISSING", html)
        self.assertIn("schema=current_schema", inspect_stdout.getvalue())
        self.assertIn("unknown_dependency task=TASK-01", inspect_stdout.getvalue())
        self.assertIn(
            "unknown_dependency",
            {warning["code"] for warning in timeline["warnings"]},
        )

    def test_doctor_reports_planning_artifact_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            missing_stdout = StringIO()
            missing_stderr = StringIO()
            with redirect_stdout(missing_stdout), redirect_stderr(missing_stderr):
                missing_exit = main(["doctor", "--repo", str(repo)])
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            present_stdout = StringIO()
            present_stderr = StringIO()
            with redirect_stdout(present_stdout), redirect_stderr(present_stderr):
                present_exit = main(["doctor", "--repo", str(repo)])

            missing = json.loads(missing_stdout.getvalue())
            present = json.loads(present_stdout.getvalue())

        self.assertEqual(missing_exit, 0)
        self.assertEqual(generate_exit, 0)
        self.assertEqual(present_exit, 0)
        self.assertEqual(missing_stderr.getvalue(), "")
        self.assertEqual(present_stderr.getvalue(), "")
        missing_timeline = missing["planning_analytics"]["artifacts"]["timeline_json"]
        present_timeline = present["planning_analytics"]["artifacts"]["timeline_json"]
        self.assertEqual(missing_timeline["freshness"], "missing")
        self.assertEqual(present_timeline["freshness"], "not_checked")
        self.assertEqual(present_timeline["schema_status"], "current_schema")
        self.assertIsInstance(present_timeline["warning_count"], int)
        self.assertIn(
            "vibe-loop planning artifacts --repo",
            present["planning_analytics"]["artifacts"]["next_repair_commands"][0],
        )

    def test_planning_artifact_inspect_does_not_trust_gantt_marker_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            init_planning_repo(repo, PLAN)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            html_path = repo / ".vibe-loop" / "planning-analytics" / "gantt.html"
            lines = html_path.read_text(encoding="utf-8").splitlines()
            forged = {
                "schema_version": 1,
                "source": "forged",
                "path": "/tmp/forged.html",
                "exists": False,
                "freshness": "forged",
                "warning_count": 7,
            }
            lines[0] = f"<!-- vibe-loop-planning-gantt {json.dumps(forged)} -->"
            html_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            inspect_stdout = StringIO()
            inspect_stderr = StringIO()
            with redirect_stdout(inspect_stdout), redirect_stderr(inspect_stderr):
                inspect_exit = main(
                    [
                        "planning",
                        "artifacts",
                        "--repo",
                        str(repo),
                        "--inspect",
                        "--json",
                    ]
                )

            payload = json.loads(inspect_stdout.getvalue())
            gantt = payload["gantt_html"]

        self.assertEqual(generate_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspect_stderr.getvalue(), "")
        self.assertEqual(gantt["path"], str(html_path))
        self.assertEqual(gantt["source"], "default_state_dir")
        self.assertTrue(gantt["exists"])
        self.assertEqual(gantt["freshness"], "not_checked")
        self.assertEqual(gantt["schema_status"], "current_schema")
        self.assertEqual(gantt["warning_count"], 7)

    def test_planning_artifact_inspect_reports_old_timeline_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            init_planning_repo(repo, PLAN)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                generate_exit = main(["planning", "artifacts", "--repo", str(repo)])
            timeline_path = repo / ".vibe-loop" / "planning-analytics" / "timeline.json"
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            timeline["schema_version"] = 1
            timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
            inspect_stdout = StringIO()
            inspect_stderr = StringIO()
            with redirect_stdout(inspect_stdout), redirect_stderr(inspect_stderr):
                inspect_exit = main(
                    [
                        "planning",
                        "artifacts",
                        "--repo",
                        str(repo),
                        "--inspect",
                        "--json",
                    ]
                )

            payload = json.loads(inspect_stdout.getvalue())

        self.assertEqual(generate_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspect_stderr.getvalue(), "")
        self.assertEqual(payload["timeline_json"]["schema_status"], "unknown_schema")

    def test_tasks_configure_writes_validated_profile_cache_with_stub_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )
            prompt = json.loads(
                (repo / "configure-prompt.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "profile")
        self.assertEqual(payload["cache"]["status"], "profile")
        self.assertEqual(cache["status"], "profile")
        self.assertEqual(cache["profile"]["source_paths"], ["WORK.md"])
        self.assertEqual(cache["source_fingerprints"][0]["path"], "WORK.md")
        self.assertEqual(cache["agent"]["name"], "codex")
        self.assertEqual(cache["agent"]["kind"], "auto")
        self.assertEqual(cache["agent"]["prompt_dialect"], "codex")
        self.assertEqual(cache["agent"]["prompt_dialect_source"], "auto:codex")
        self.assertEqual(cache["agent"]["selection_command_source"], "auto:codex")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex")
        self.assertNotIn("command", cache["agent"])
        self.assertEqual(prompt["evidence"]["files"][0]["path"], "WORK.md")

    def test_generated_profile_preserves_traceability_fields(self) -> None:
        fingerprint = {
            "path": "docs/spec.md",
            "size": 10,
            "sha256": "c" * 64,
            "redacted": False,
        }
        trace_work_table = (
            "# Work\n\n"
            "| Key | State | Summary | Requirements | Spec Paths | Design Refs | Approval | Fingerprints |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| DISC-X | Todo | Configure generated discovery. | PRD-SDE-003, REQ-2 | docs/spec.md | ADR-1, docs/design.md#trace | approved | "
            f"{json.dumps([fingerprint], separators=(',', ':'))} |\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(trace_work_table, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={
                        "requirement_ids": {"column": "Requirements"},
                        "spec_paths": {"column": "Spec Paths"},
                        "design_refs": {"column": "Design Refs"},
                        "approval_state": {"column": "Approval"},
                        "source_fingerprints": {"column": "Fingerprints"},
                    },
                ),
            )
            configure_stdout = StringIO()
            configure_stderr = StringIO()
            list_stdout = StringIO()
            list_stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with (
                    redirect_stdout(configure_stdout),
                    redirect_stderr(configure_stderr),
                ):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            with redirect_stdout(list_stdout), redirect_stderr(list_stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo), "--json"])

            configure_payload = json.loads(configure_stdout.getvalue())
            tasks_payload = json.loads(list_stdout.getvalue())

        self.assertEqual(configure_exit, 0)
        self.assertEqual(list_exit, 0)
        self.assertEqual(configure_stderr.getvalue(), "")
        self.assertIn("task discovery source=generated_cache", list_stderr.getvalue())
        self.assertIn(
            "[task_source.profile.fields.requirement_ids]",
            configure_payload["promotion_toml"],
        )
        self.assertEqual(tasks_payload[0]["requirement_ids"], ["PRD-SDE-003", "REQ-2"])
        self.assertEqual(tasks_payload[0]["spec_paths"], ["docs/spec.md"])
        self.assertEqual(
            tasks_payload[0]["design_refs"],
            ["ADR-1", "docs/design.md#trace"],
        )
        self.assertEqual(tasks_payload[0]["approval_state"], "approved")
        self.assertEqual(tasks_payload[0]["source_fingerprints"], [fingerprint])

    def test_tasks_configure_dry_run_reports_profile_without_writing_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--dry-run",
                            "--json",
                        ]
                    )

            payload = json.loads(stdout.getvalue())
            cache_path = repo / ".vibe-loop" / "generated-task-source.json"
            cache_exists = cache_path.exists()
            prompt = json.loads(
                (repo / "configure-prompt.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "profile")
        self.assertEqual(payload["cache_action"], "dry_run")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["wrote_cache"])
        self.assertFalse(cache_exists)
        self.assertEqual(payload["cache"]["profile"]["source_paths"], ["WORK.md"])
        self.assertIn("[task_source.profile]", payload["promotion_toml"])
        self.assertEqual(prompt["evidence"]["files"][0]["path"], "WORK.md")

    def test_tasks_configure_json_handles_promotable_none_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={"id": {"none_values": ["none"]}},
                ),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--dry-run",
                            "--json",
                        ]
                    )

            payload = json.loads(stdout.getvalue())
            promoted = tomllib.loads(payload["promotion_toml"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            promoted["task_source"]["profile"]["fields"]["id"]["none_values"],
            ["none"],
        )

    def test_tasks_configure_reuses_fresh_cache_until_force_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            work_path = repo / "WORK.md"
            work_path.write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    initial_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            reuse_stdout = StringIO()
            reuse_stderr = StringIO()
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(reuse_stdout), redirect_stderr(reuse_stderr):
                    reuse_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload(
                    "WORK.md",
                    status_map_extra={"runnable": ["Todo", "Queued"]},
                ),
                marker="force-ran",
            )
            fresh_force_stdout = StringIO()
            fresh_force_stderr = StringIO()
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with (
                    redirect_stdout(fresh_force_stdout),
                    redirect_stderr(fresh_force_stderr),
                ):
                    fresh_force_exit = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--force-refresh",
                            "--json",
                        ]
                    )
            fresh_force_cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )

            roadmap_path = repo / "ROADMAP.md"
            work_path.rename(roadmap_path)
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("ROADMAP.md"),
            )
            refresh_stdout = StringIO()
            refresh_stderr = StringIO()
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(refresh_stdout), redirect_stderr(refresh_stderr):
                    refresh_exit = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--force-refresh",
                            "--json",
                        ]
                    )
                list_stdout = StringIO()
                list_stderr = StringIO()
                with redirect_stdout(list_stdout), redirect_stderr(list_stderr):
                    list_exit = main(["tasks", "list", "--repo", str(repo)])

            reuse_payload = json.loads(reuse_stdout.getvalue())
            fresh_force_payload = json.loads(fresh_force_stdout.getvalue())
            refresh_payload = json.loads(refresh_stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )
            fresh_force_ran = (repo / "force-ran").exists()

        self.assertEqual(initial_exit, 0)
        self.assertEqual(reuse_exit, 0)
        self.assertEqual(reuse_stderr.getvalue(), "")
        self.assertEqual(reuse_payload["cache_action"], "reused")
        self.assertFalse(reuse_payload["wrote_cache"])
        self.assertFalse((repo / "should-not-run").exists())
        self.assertEqual(fresh_force_exit, 0)
        self.assertEqual(fresh_force_stderr.getvalue(), "")
        self.assertEqual(fresh_force_payload["cache_action"], "wrote")
        self.assertTrue(fresh_force_ran)
        self.assertEqual(
            fresh_force_cache["profile"]["status_map"]["runnable"],
            ["Todo", "Queued"],
        )
        self.assertEqual(refresh_exit, 0)
        self.assertEqual(refresh_stderr.getvalue(), "")
        self.assertEqual(refresh_payload["cache_action"], "wrote")
        self.assertTrue(refresh_payload["wrote_cache"])
        self.assertEqual(cache["profile"]["source_paths"], ["ROADMAP.md"])
        self.assertEqual(list_exit, 0)
        self.assertIn("DISC-X", list_stdout.getvalue())
        self.assertIn("task discovery source=generated_cache", list_stderr.getvalue())

    def test_tasks_configure_promotion_toml_preserves_status_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nrunnable_statuses = ["Ready"]\n',
                encoding="utf-8",
            )
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--dry-run",
                            "--promotion-toml",
                        ]
                    )

            promoted = tomllib.loads(stdout.getvalue())
            cache_path = repo / ".vibe-loop" / "generated-task-source.json"
            cache_exists = cache_path.exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(cache_exists)
        self.assertEqual(promoted["task_source"]["type"], "markdown-profile")
        self.assertEqual(promoted["task_source"]["runnable_statuses"], ["Ready"])
        self.assertEqual(
            promoted["task_source"]["profile"]["source_paths"], ["WORK.md"]
        )
        self.assertEqual(
            promoted["task_source"]["profile"]["status_map"]["runnable"], ["Todo"]
        )

    def test_tasks_configure_promotion_toml_rejects_invalid_without_writing_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={"id": {"pattern": "["}},
                ),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "tasks",
                            "configure",
                            "--repo",
                            str(repo),
                            "--promotion-toml",
                        ]
                    )
            cache_exists = (repo / ".vibe-loop" / "generated-task-source.json").exists()

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("generated profile cannot parse task source", stderr.getvalue())
        self.assertIn("pattern is invalid", stderr.getvalue())
        self.assertFalse(cache_exists)

    def test_promoted_toml_matches_generated_cache_runtime_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nrunnable_statuses = ["Ready"]\n',
                encoding="utf-8",
            )
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
                generated_list_stdout = StringIO()
                generated_list_stderr = StringIO()
                with (
                    redirect_stdout(generated_list_stdout),
                    redirect_stderr(generated_list_stderr),
                ):
                    generated_list_exit = main(
                        ["tasks", "list", "--repo", str(repo), "--json"]
                    )
                generated_runnable_stdout = StringIO()
                generated_runnable_stderr = StringIO()
                with (
                    redirect_stdout(generated_runnable_stdout),
                    redirect_stderr(generated_runnable_stderr),
                ):
                    generated_runnable_exit = main(
                        ["tasks", "runnable", "--repo", str(repo), "--json"]
                    )
                promotion_stdout = StringIO()
                promotion_stderr = StringIO()
                with (
                    redirect_stdout(promotion_stdout),
                    redirect_stderr(promotion_stderr),
                ):
                    promotion_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--promotion-toml"]
                    )

            (repo / ".vibe-loop.toml").write_text(
                promotion_stdout.getvalue(),
                encoding="utf-8",
            )
            promoted_list_stdout = StringIO()
            promoted_list_stderr = StringIO()
            with (
                redirect_stdout(promoted_list_stdout),
                redirect_stderr(promoted_list_stderr),
            ):
                promoted_list_exit = main(
                    ["tasks", "list", "--repo", str(repo), "--json"]
                )
            promoted_runnable_stdout = StringIO()
            promoted_runnable_stderr = StringIO()
            with (
                redirect_stdout(promoted_runnable_stdout),
                redirect_stderr(promoted_runnable_stderr),
            ):
                promoted_runnable_exit = main(
                    ["tasks", "runnable", "--repo", str(repo), "--json"]
                )

            generated_list = json.loads(generated_list_stdout.getvalue())
            generated_runnable = json.loads(generated_runnable_stdout.getvalue())
            promoted_list = json.loads(promoted_list_stdout.getvalue())
            promoted_runnable = json.loads(promoted_runnable_stdout.getvalue())

        self.assertEqual(configure_exit, 0)
        self.assertEqual(generated_list_exit, 0)
        self.assertIn(
            "task discovery source=generated_cache",
            generated_list_stderr.getvalue(),
        )
        self.assertEqual(generated_runnable_exit, 0)
        self.assertIn(
            "task discovery source=generated_cache",
            generated_runnable_stderr.getvalue(),
        )
        self.assertEqual(promotion_exit, 0)
        self.assertEqual(promotion_stderr.getvalue(), "")
        self.assertEqual(promoted_list_exit, 0)
        self.assertIn(
            "task discovery source=explicit_config",
            promoted_list_stderr.getvalue(),
        )
        self.assertEqual(promoted_runnable_exit, 0)
        self.assertIn(
            "task discovery source=explicit_config",
            promoted_runnable_stderr.getvalue(),
        )
        self.assertEqual(generated_list, promoted_list)
        self.assertEqual(generated_runnable, promoted_runnable)

    def test_tasks_configure_uses_codex_first_policy_with_both_agents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            write_python_executable(
                bin_dir / "claude",
                "from pathlib import Path\n"
                "Path('claude-should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "profile")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex:codex-first")
        self.assertEqual(
            payload["agent"]["selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload["agent"]["default_policy_source"], "codex-first")
        self.assertIn("Codex", payload["agent"]["default_policy"])
        self.assertEqual(payload["agent"]["prompt_dialect"], "codex")
        self.assertEqual(
            payload["agent"]["prompt_dialect_source"],
            "auto:codex:codex-first",
        )
        self.assertFalse((repo / "claude-should-not-run").exists())

    def test_tasks_configure_text_reports_detected_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "claude",
                {
                    "status": "unavailable",
                    "confidence": None,
                    "degradation": {
                        "reason": "no_tasks",
                        "message": "no task source found",
                        "next_action": "add a task source",
                    },
                },
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["tasks", "configure", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("tasks configure: cache status=unavailable", stdout.getvalue())
        self.assertIn("detected agents: claude=", stdout.getvalue())
        self.assertIn("agent default policy source: codex-first", stdout.getvalue())
        self.assertIn("agent default policy:", stdout.getvalue())
        self.assertIn("agent.kind: auto", stdout.getvalue())
        self.assertIn("agent.command source: auto:claude", stdout.getvalue())
        self.assertIn("agent.prompt_dialect source: auto:claude", stdout.getvalue())
        self.assertIn("no_tasks: no task source found", stdout.getvalue())

    def test_tasks_configure_degrades_malformed_agent_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(bin_dir / "codex", "not-json")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(cache["status"], "rejected")
        self.assertEqual(cache["degradation"]["reason"], "malformed_json")

    def test_tasks_configure_rejects_non_finite_json_constants(self) -> None:
        constants = [
            (
                "confidence",
                (
                    '{"status":"profile","confidence":NaN,'
                    '"profile":{"kind":"markdown_table","source_paths":["WORK.md"],'
                    '"stable_ids":true,"fields":{"id":{"column":"Key"},'
                    '"title":{"column":"Summary"},"status":{"column":"State"}},'
                    '"status_map":{"done":["Done"],"runnable":["Todo"]}}}'
                ),
            ),
            (
                "nested_profile",
                (
                    '{"status":"profile","confidence":0.9,'
                    '"profile":{"kind":"markdown_table","source_paths":["WORK.md"],'
                    '"stable_ids":true,"fields":{"id":{"column":NaN},'
                    '"title":{"column":"Summary"},"status":{"column":"State"}},'
                    '"status_map":{"done":["Done"],"runnable":["Todo"]}}}'
                ),
            ),
        ]
        for name, raw_payload in constants:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory) / "repo"
                    bin_dir = Path(directory) / "bin"
                    repo.mkdir()
                    bin_dir.mkdir()
                    (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
                    write_configure_agent(bin_dir / "codex", raw_payload)
                    stdout = StringIO()
                    stderr = StringIO()

                    with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = main(
                                [
                                    "tasks",
                                    "configure",
                                    "--repo",
                                    str(repo),
                                    "--json",
                                ]
                            )

                    payload = json.loads(stdout.getvalue())

                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(
                    payload["cache"]["degradation"]["reason"], "malformed_json"
                )
                self.assertIn("non-finite", payload["cache"]["degradation"]["message"])

    def test_tasks_configure_rejects_executable_profile_directives(self) -> None:
        cases = [
            (
                "unsupported_profile_key",
                generated_profile_payload(
                    "WORK.md",
                    profile_extra={"extractor": "bash -c 'echo task'"},
                ),
            ),
            (
                "unsupported_field_mapping_key",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={"id": {"run": "python collect_tasks.py"}},
                ),
            ),
            (
                "invalid_field_mapping_value",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={"id": {"strategy": "python collect_tasks.py"}},
                ),
            ),
        ]
        for expected_reason, agent_payload in cases:
            with self.subTest(expected_reason=expected_reason):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory) / "repo"
                    bin_dir = Path(directory) / "bin"
                    repo.mkdir()
                    bin_dir.mkdir()
                    (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
                    write_configure_agent(bin_dir / "codex", agent_payload)
                    stdout = StringIO()
                    stderr = StringIO()

                    with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = main(
                                [
                                    "tasks",
                                    "configure",
                                    "--repo",
                                    str(repo),
                                    "--json",
                                ]
                            )

                    payload = json.loads(stdout.getvalue())

                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(
                    payload["cache"]["degradation"]["reason"], expected_reason
                )

    def test_doctor_reports_generated_cache_disabled_by_explicit_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_path = "PLAN.md"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            payload["generated_task_profile"]["status"],
            "disabled_by_explicit_task_source",
        )
        self.assertEqual(
            payload["generated_task_profile"]["explicit_source_keys"], ["plan_path"]
        )
        self.assertIn(
            "fix the explicit task_source",
            payload["generated_task_profile"]["next_action"],
        )

    def test_read_only_source_errors_report_generated_cache_disabled_by_explicit_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_path = "MISSING.md"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("disabled_by_explicit_task_source", stderr.getvalue())
        self.assertIn("fix the explicit task_source", stderr.getvalue())

    def test_tasks_configure_degrades_low_confidence_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md", confidence=0.2),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "planning_only")
        self.assertEqual(payload["cache"]["degradation"]["reason"], "low_confidence")
        self.assertEqual(payload["cache"]["profile"]["source_paths"], ["WORK.md"])

    def test_tasks_configure_writes_planning_cache_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            (repo / "script.py").write_text("print('skip me')\n", encoding="utf-8")
            agent_payload = generated_profile_payload("WORK.md", confidence=0.44)
            agent_payload["status"] = "planning_only"
            agent_payload["profile"].pop("status_map")
            agent_payload["degradation"] = {
                "reason": "ambiguous_format",
                "message": "work items exist but status policy is unclear",
                "next_action": "choose a status policy",
                "missing_inputs": ["status mapping", "stable done states"],
                "proposed_config": {
                    "task_source": {
                        "type": "markdown-profile",
                        "profile": agent_payload["profile"],
                    }
                },
                "candidate_sources": [{"path": "WORK.md", "format": "table"}],
                "questions": ["Which states are runnable?"],
            }
            original_work = (repo / "WORK.md").read_text(encoding="utf-8")
            write_configure_agent(bin_dir / "codex", agent_payload)
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
                doctor_stdout = StringIO()
                doctor_stderr = StringIO()
                with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                    doctor_exit = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            doctor_payload = json.loads(doctor_stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )
            final_work = (repo / "WORK.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(payload["status"], "planning_only")
        self.assertEqual(cache["status"], "planning_only")
        self.assertEqual(cache["profile"]["source_paths"], ["WORK.md"])
        self.assertEqual(cache["source_fingerprints"][0]["path"], "WORK.md")
        self.assertEqual(cache["degradation"]["reason"], "ambiguous_format")
        self.assertEqual(
            cache["degradation"]["missing_inputs"],
            ["status mapping", "stable done states"],
        )
        self.assertEqual(
            cache["degradation"]["proposed_config"]["task_source"]["type"],
            "markdown-profile",
        )
        self.assertEqual(
            cache["degradation"]["candidate_sources"],
            [{"path": "WORK.md", "format": "table"}],
        )
        self.assertEqual(
            cache["degradation"]["questions"], ["Which states are runnable?"]
        )
        self.assertIn(
            ("script.py", "unsupported_file_type"),
            {
                (item["path"], item["reason"])
                for item in cache["provenance"]["skipped_evidence"]
            },
        )
        self.assertEqual(final_work, original_work)
        report = doctor_payload["generated_task_profile"]
        self.assertEqual(report["origin"], "planning_only_cache")
        self.assertTrue(report["fresh"])
        self.assertEqual(
            report["missing_inputs"], ["status mapping", "stable done states"]
        )
        self.assertEqual(
            report["proposed_config"]["task_source"]["type"],
            "markdown-profile",
        )
        self.assertIn(
            {"path": "script.py", "reason": "unsupported_file_type"},
            report["skipped_evidence"],
        )

    def test_tasks_configure_without_agent_writes_unavailable_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(cache["degradation"]["reason"], "agent_unavailable")
        self.assertEqual(
            cache["degradation"]["missing_inputs"], ["agent.selection_command"]
        )
        self.assertEqual(cache["source_fingerprints"][0]["path"], "WORK.md")
        self.assertIn(
            (".env", "secret_path"),
            {
                (item["path"], item["reason"])
                for item in cache["provenance"]["skipped_evidence"]
            },
        )

    def test_tasks_configure_without_agent_reports_no_collected_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "TODO.md").write_text(
                "x" * (2 * 1024 * 1024 + 1),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())
            cache = json.loads(
                (repo / ".vibe-loop" / "generated-task-source.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(cache["source_fingerprints"], [])
        self.assertEqual(
            cache["degradation"]["missing_inputs"],
            ["agent.selection_command", "repo-local task source evidence"],
        )
        self.assertIn(
            ("TODO.md", "file_too_large"),
            {
                (item["path"], item["reason"])
                for item in cache["provenance"]["skipped_evidence"]
            },
        )

    def test_read_only_reuses_fresh_unavailable_cache_without_running_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(configure_exit, 2)
        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "generated task-source cache status=unavailable", stderr.getvalue()
        )
        self.assertIn("agent_unavailable", stderr.getvalue())
        self.assertFalse((repo / "should-not-run").exists())

    def test_read_only_no_cache_reports_configure_diagnostic_without_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    list_exit = main(["tasks", "list", "--repo", str(repo)])
                doctor_stdout = StringIO()
                doctor_stderr = StringIO()
                with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                    doctor_exit = main(["doctor", "--repo", str(repo)])

            doctor_payload = json.loads(doctor_stdout.getvalue())

        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("generated task-source cache status=missing", stderr.getvalue())
        self.assertIn("origin=no_usable_source", stderr.getvalue())
        self.assertIn("tasks configure", stderr.getvalue())
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertFalse(doctor_payload["task_source_runtime"]["usable"])
        self.assertFalse((repo / "should-not-run").exists())

    def test_doctor_marks_unavailable_cache_stale_when_new_evidence_appears(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            (repo / "TODO.md").write_text("new task evidence\n", encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    doctor_exit = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(configure_exit, 2)
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        report = payload["generated_task_profile"]
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["origin"], "stale_generated_cache")
        self.assertFalse(report["fresh"])
        self.assertIn("TODO.md is new bounded evidence", report["stale_reasons"])
        self.assertFalse((repo / "should-not-run").exists())

    def test_tasks_configure_profiles_missing_status_map_are_planning_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            agent_payload = generated_profile_payload("WORK.md")
            agent_payload["profile"].pop("status_map")
            write_configure_agent(bin_dir / "codex", agent_payload)
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "planning_only")
        self.assertEqual(payload["cache"]["degradation"]["reason"], "unmapped_statuses")
        self.assertEqual(
            payload["cache"]["degradation"]["missing_inputs"],
            ["status mapping for runnable and done tasks"],
        )

    def test_tasks_configure_empty_required_status_maps_are_planning_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            agent_payload = generated_profile_payload(
                "WORK.md",
                status_map_extra={"done": [], "runnable": []},
            )
            write_configure_agent(bin_dir / "codex", agent_payload)
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "planning_only")
        self.assertEqual(payload["cache"]["degradation"]["reason"], "unmapped_statuses")
        self.assertEqual(payload["cache"]["profile"]["source_paths"], ["WORK.md"])

    def test_tasks_configure_unstable_ids_are_planning_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload(
                    "WORK.md",
                    profile_extra={"stable_ids": False},
                ),
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "planning_only")
        self.assertEqual(payload["cache"]["degradation"]["reason"], "unstable_ids")
        self.assertEqual(
            payload["cache"]["degradation"]["missing_inputs"],
            ["stable task identifiers"],
        )

    def test_tasks_configure_missing_degradation_object_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(bin_dir / "codex", {"status": "needs_input"})
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "needs_input")
        self.assertEqual(payload["cache"]["degradation"]["reason"], "needs_input")
        self.assertEqual(
            payload["cache"]["degradation"]["message"],
            "agent returned needs_input without a degradation object",
        )

    def test_doctor_reports_stale_generated_cache_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            work_path = repo / "WORK.md"
            work_path.write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            work_path.write_text(WORK_TABLE + "\nchanged\n", encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    doctor_exit = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(configure_exit, 0)
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        report = payload["generated_task_profile"]
        self.assertEqual(report["status"], "profile")
        self.assertEqual(report["origin"], "stale_generated_cache")
        self.assertFalse(report["fresh"])
        self.assertIn("WORK.md changed", report["stale_reasons"][0])
        self.assertIn("generated cache is stale", report["diagnostics"][0])
        self.assertIn("tasks configure", report["next_action"])
        self.assertFalse((repo / "should-not-run").exists())

    def test_tasks_configure_rejects_unsupported_and_incomplete_profiles(
        self,
    ) -> None:
        cases = [
            (
                "unsupported_profile_kind",
                generated_profile_payload("WORK.md", kind="json_query"),
            ),
            (
                "incomplete_fields",
                generated_profile_payload("WORK.md", include_title=False),
            ),
            (
                "invalid_field_mapping_value",
                generated_profile_payload("WORK.md", empty_title_column=True),
            ),
            (
                "invalid_field_mapping_value",
                {
                    "status": "profile",
                    "confidence": 0.86,
                    "profile": {
                        "kind": "markdown_headings",
                        "source_paths": ["WORK.md"],
                        "stable_ids": True,
                        "fields": {
                            "id": {"strategy": "label_value"},
                            "title": {"strategy": "heading_text"},
                            "status": {"label": "State"},
                        },
                        "status_map": {
                            "done": ["Done"],
                            "runnable": ["Todo"],
                        },
                    },
                },
            ),
            (
                "unknown_field_column",
                generated_profile_payload("WORK.md", title_column="Missing"),
            ),
            (
                "unknown_field_column",
                generated_profile_payload("WORK.md", title_column="Todo"),
            ),
            (
                "invalid_field_mapping",
                generated_profile_payload(
                    "WORK.md",
                    field_extra={"priority": "Priority"},
                ),
            ),
            (
                "incomplete_status_map",
                generated_profile_payload(
                    "WORK.md", status_map_extra={"blocked": "Blocked"}
                ),
            ),
        ]
        for expected_reason, agent_payload in cases:
            with self.subTest(expected_reason=expected_reason):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory) / "repo"
                    bin_dir = Path(directory) / "bin"
                    repo.mkdir()
                    bin_dir.mkdir()
                    (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
                    write_configure_agent(bin_dir / "codex", agent_payload)
                    stdout = StringIO()
                    stderr = StringIO()

                    with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = main(
                                [
                                    "tasks",
                                    "configure",
                                    "--repo",
                                    str(repo),
                                    "--json",
                                ]
                            )

                    payload = json.loads(stdout.getvalue())

                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(
                    payload["cache"]["degradation"]["reason"], expected_reason
                )

    def test_read_only_task_commands_report_cache_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )

            results: dict[str, tuple[int, str, str]] = {}
            commands = {
                "tasks-list": ["tasks", "list", "--repo", str(repo)],
                "tasks-runnable": ["tasks", "runnable", "--repo", str(repo)],
                "next": ["next", "--repo", str(repo)],
                "doctor": ["doctor", "--repo", str(repo)],
            }
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                for name, command in commands.items():
                    stdout = StringIO()
                    stderr = StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(command)
                    results[name] = (
                        exit_code,
                        stdout.getvalue(),
                        stderr.getvalue(),
                    )

            doctor_payload = json.loads(results["doctor"][1])

        self.assertEqual(configure_exit, 0)
        self.assertEqual(results["tasks-list"][0], 0)
        self.assertIn("DISC-X", results["tasks-list"][1])
        self.assertIn("task discovery source=generated_cache", results["tasks-list"][2])
        self.assertEqual(results["tasks-runnable"][0], 0)
        self.assertIn("DISC-X", results["tasks-runnable"][1])
        self.assertIn(
            "task discovery source=generated_cache",
            results["tasks-runnable"][2],
        )
        self.assertEqual(results["next"][0], 0)
        self.assertEqual(results["next"][1].strip(), "DISC-X")
        self.assertIn("task discovery source=generated_cache", results["next"][2])
        self.assertEqual(results["doctor"][0], 0)
        self.assertEqual(results["doctor"][2], "")
        self.assertEqual(doctor_payload["generated_task_profile"]["status"], "profile")
        self.assertEqual(
            doctor_payload["task_source_runtime"]["origin"], "generated_cache"
        )
        self.assertTrue(doctor_payload["task_source_runtime"]["usable"])
        self.assertEqual(
            doctor_payload["generated_task_profile"]["next_action"],
            "generated cache is active for runtime task discovery",
        )
        self.assertFalse((repo / "should-not-run").exists())

    def test_read_only_task_success_reports_degraded_cache_without_running_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            agent_payload = generated_profile_payload("WORK.md", confidence=0.44)
            agent_payload["status"] = "planning_only"
            agent_payload["degradation"] = {
                "reason": "low_confidence",
                "message": "agent was unsure",
                "next_action": "rerun tasks configure",
            }
            write_configure_agent(
                bin_dir / "codex",
                agent_payload,
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )

            results: dict[str, tuple[int, str, str]] = {}
            commands = {
                "tasks-list": ["tasks", "list", "--repo", str(repo)],
                "tasks-runnable": ["tasks", "runnable", "--repo", str(repo)],
                "next": ["next", "--repo", str(repo)],
            }
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                for name, command in commands.items():
                    stdout = StringIO()
                    stderr = StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(command)
                    results[name] = (
                        exit_code,
                        stdout.getvalue(),
                        stderr.getvalue(),
                    )

        self.assertEqual(configure_exit, 2)
        self.assertEqual(results["tasks-list"][0], 0)
        self.assertIn("TASK-01", results["tasks-list"][1])
        self.assertIn(
            "generated task-source cache status=planning_only",
            results["tasks-list"][2],
        )
        self.assertIn(
            "task discovery source=default_markdown_discovery",
            results["tasks-list"][2],
        )
        self.assertEqual(results["tasks-runnable"][0], 0)
        self.assertIn(
            "generated task-source cache status=planning_only",
            results["tasks-runnable"][2],
        )
        self.assertEqual(results["next"][0], 0)
        self.assertEqual(results["next"][1].strip(), "TASK-01")
        self.assertIn("low_confidence: agent was unsure", results["next"][2])
        self.assertFalse((repo / "should-not-run").exists())

    def test_stale_generated_cache_blocks_default_plan_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            work_path = repo / "WORK.md"
            work_path.write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            work_path.write_text(WORK_TABLE + "\nchanged\n", encoding="utf-8")
            write_python_executable(
                bin_dir / "codex",
                "from pathlib import Path\n"
                "Path('should-not-run').write_text('ran', encoding='utf-8')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(configure_exit, 0)
        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("stale_generated_cache", stderr.getvalue())
        self.assertIn("generated cache is stale", stderr.getvalue())
        self.assertIn("tasks configure", stderr.getvalue())
        self.assertFalse((repo / "should-not-run").exists())

    def test_invalid_generated_cache_blocks_default_plan_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            state_dir = repo / ".vibe-loop"
            repo.mkdir()
            state_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            cache = generated_profile_cache("WORK.md")
            cache["status"] = "unsupported"
            (state_dir / "generated-task-source.json").write_text(
                json.dumps(cache),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unsupported generated cache status", stderr.getvalue())
        self.assertIn("origin=invalid_generated_cache", stderr.getvalue())

    def test_generated_cache_with_invalid_parser_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            agent_payload = generated_profile_payload(
                "WORK.md",
                field_extra={"id": {"pattern": "["}},
            )
            write_configure_agent(bin_dir / "codex", agent_payload)
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(configure_exit, 0)
        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("generated profile cannot parse task source", stderr.getvalue())
        self.assertIn("origin=invalid_generated_cache", stderr.getvalue())
        self.assertIn("tasks configure", stderr.getvalue())
        self.assertNotIn(
            "generated cache is active for runtime task discovery",
            stderr.getvalue(),
        )

    def test_generated_cache_rejects_invalid_source_path_before_parser(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            state_dir = repo / ".vibe-loop"
            outside_dir = Path(directory) / "outside"
            repo.mkdir()
            bin_dir.mkdir()
            outside_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            write_configure_agent(
                bin_dir / "codex",
                generated_profile_payload("WORK.md"),
            )
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    configure_exit = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )
            cache_path = state_dir / "generated-task-source.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            profile = cache["profile"]
            assert isinstance(profile, dict)
            profile["source_paths"] = [str(outside_dir)]
            cache_path.write_text(
                json.dumps(cache),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(configure_exit, 0)
        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid_source_path", stderr.getvalue())
        self.assertNotIn(
            "generated profile cannot parse task source", stderr.getvalue()
        )

    def test_empty_generated_source_fingerprints_are_stale_for_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            state_dir = repo / ".vibe-loop"
            repo.mkdir()
            state_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "WORK.md").write_text(WORK_TABLE, encoding="utf-8")
            cache = generated_profile_cache("WORK.md")
            cache["source_fingerprints"] = []
            (state_dir / "generated-task-source.json").write_text(
                json.dumps(cache),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo)])

        self.assertEqual(list_exit, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "source_fingerprints is empty for generated profile source paths",
            stderr.getvalue(),
        )

    def test_explicit_task_source_config_ignores_generated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            state_dir = repo / ".vibe-loop"
            repo.mkdir()
            state_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_path = "PLAN.md"\n',
                encoding="utf-8",
            )
            stale_cache = generated_profile_cache("WORK.md")
            (state_dir / "generated-task-source.json").write_text(
                json.dumps(stale_cache),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo)])
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()
            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo)])

            doctor_payload = json.loads(doctor_stdout.getvalue())

        self.assertEqual(list_exit, 0)
        self.assertIn("TASK-01", stdout.getvalue())
        self.assertIn("task discovery source=explicit_config", stderr.getvalue())
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(
            doctor_payload["task_source_runtime"]["origin"], "explicit_config"
        )
        self.assertTrue(doctor_payload["task_source_runtime"]["usable"])

    def test_command_task_source_reports_command_output_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'CMD-01','title':'Command task',"
                "'status':'Next','dependencies':[]}]))\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nlist = "python list_tasks.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                list_exit = main(["tasks", "list", "--repo", str(repo)])
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()
            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo)])

            doctor_payload = json.loads(doctor_stdout.getvalue())

        self.assertEqual(list_exit, 0)
        self.assertIn("CMD-01", stdout.getvalue())
        self.assertIn("task discovery source=command_output", stderr.getvalue())
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(
            doctor_payload["task_source_runtime"]["origin"], "command_output"
        )
        self.assertTrue(doctor_payload["task_source_runtime"]["usable"])

    def test_tasks_list_warns_when_using_main_worktree_config_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_repo = root / "main"
            linked_repo = root / "linked"
            main_repo.mkdir()
            linked_repo.mkdir()
            main_config = main_repo / ".vibe-loop.toml"
            main_config.write_text(
                '[task_source]\nlist = "python list_tasks.py"\n',
                encoding="utf-8",
            )
            (linked_repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'CMD-01','title':'Command task',"
                "'status':'Next','dependencies':[]}]))\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()

            with patch(
                "vibe_loop.config.main_worktree_config_path",
                return_value=main_config,
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    list_exit = main(["tasks", "list", "--repo", str(linked_repo)])
                with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                    doctor_exit = main(["doctor", "--repo", str(linked_repo)])

            doctor_payload = json.loads(doctor_stdout.getvalue())

        self.assertEqual(list_exit, 0)
        self.assertIn("CMD-01", stdout.getvalue())
        self.assertIn(
            "warning: using config_source=main_worktree",
            stderr.getvalue(),
        )
        self.assertIn(f"config_path={main_config.resolve()}", stderr.getvalue())
        self.assertIn(f"tasks_repo={linked_repo.resolve()}", stderr.getvalue())
        self.assertIn("task discovery source=command_output", stderr.getvalue())
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(doctor_payload["config"]["source"], "main_worktree")
        self.assertEqual(doctor_payload["config"]["path"], str(main_config.resolve()))
        self.assertEqual(
            doctor_payload["task_source_runtime"]["origin"], "command_output"
        )

    def test_doctor_reports_command_adapter_missing_list_as_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nnext = "python next_task.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                doctor_exit = main(["doctor", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(doctor_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["task_source_runtime"]["origin"], "command_output")
        self.assertFalse(payload["task_source_runtime"]["usable"])
        self.assertIn(
            "command task source requires task_source.list",
            payload["task_source_runtime"]["diagnostics"][0],
        )

    def test_report_writes_worker_report_without_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "report",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--status",
                        "blocked",
                        "--commit",
                        "abc123",
                        "--message",
                        "waiting on dependency",
                        "--metadata-json",
                        '{"reason":"external"}',
                    ]
                )

            payload = json.loads(stdout.getvalue())
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["commit"], "abc123")
        self.assertEqual(payload["metadata"], {"reason": "external"})
        self.assertEqual(records[0]["record_type"], "worker_report")
        self.assertEqual(records[0]["status"], "blocked")

    def test_report_rejects_fencing_token_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            task_lock = manager.acquire(
                "TASK-01",
                "run-1",
                metadata={
                    "record_type": "active_run",
                    "schema_version": 1,
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "pid": os.getpid(),
                    "worker_pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started_at": "2026-05-09T00:00:00+00:00",
                },
            )
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "report",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--status",
                        "blocked",
                        "--fencing-token",
                        "stale-token",
                    ]
                )
            good_stdout = StringIO()
            good_stderr = StringIO()
            with redirect_stdout(good_stdout), redirect_stderr(good_stderr):
                good_exit = main(
                    [
                        "report",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--status",
                        "blocked",
                        "--fencing-token",
                        str(task_lock.metadata["fencing_token"]),
                    ]
                )
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("fencing_token_mismatch", stderr.getvalue())
        self.assertEqual(good_exit, 0)
        self.assertEqual(good_stderr.getvalue(), "")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "worker_report")

    def test_report_refuses_explicit_fencing_token_without_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "report",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--status",
                        "blocked",
                        "--fencing-token",
                        "stale-token",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("active lock not found", stderr.getvalue())

    def test_worker_claim_workspace_updates_lock_and_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            subprocess.run(
                ["git", "checkout", "-b", "worker/TASK-01"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (repo / "notes.txt").write_text("dirty at claim\n", encoding="utf-8")
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-1",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "base-main",
                        "command": "agent TASK-01",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "worker",
                        "claim-workspace",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--branch",
                        "worker/TASK-01",
                        "--worktree",
                        str(repo),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            metadata = json.loads((active_lock / "lock.json").read_text("utf-8"))
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            workers_stdout = StringIO()
            workers_stderr = StringIO()
            with redirect_stdout(workers_stdout), redirect_stderr(workers_stderr):
                workers_exit = main(["workers", "--repo", str(repo), "--json"])
            workers_payload = json.loads(workers_stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(payload["claimed"])
        workspace = payload["workspace"]
        self.assertEqual(workspace["task_id"], "TASK-01")
        self.assertEqual(workspace["run_id"], "run-1")
        self.assertEqual(workspace["branch"], "worker/TASK-01")
        self.assertEqual(workspace["current_branch"], "worker/TASK-01")
        self.assertEqual(workspace["worktree"], str(repo.resolve()))
        self.assertEqual(workspace["base_commit"], "base-main")
        self.assertEqual(workspace["event_type"], "workspace_claimed")
        self.assertEqual(workspace["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(workspace["occurred_at"], workspace["claimed_at"])
        self.assertTrue(workspace["head_commit"])
        self.assertTrue(workspace["dirty"])
        self.assertTrue(any("notes.txt" in line for line in workspace["dirty_summary"]))
        self.assertEqual(metadata["workspace"], workspace)
        self.assertEqual(records[0]["record_type"], "workspace_claim")
        self.assertEqual(records[0]["event_type"], "workspace_claimed")
        self.assertEqual(records[0]["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(records[0]["occurred_at"], records[0]["claimed_at"])
        self.assertEqual(records[0]["branch"], "worker/TASK-01")
        self.assertEqual(workers_exit, 0)
        self.assertEqual(workers_stderr.getvalue(), "")
        self.assertEqual(workers_payload[0]["workspace"], workspace)

    def test_worker_claim_workspace_rejects_owner_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-other",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "worker",
                        "claim-workspace",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--branch",
                        "worker/TASK-01",
                        "--worktree",
                        str(repo),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["claimed"])
        self.assertEqual(payload["error"], "owner_mismatch")
        self.assertEqual(payload["details"]["active_run_ids"], ["run-other"])
        self.assertEqual(records[0]["record_type"], "workspace_claim_mismatch")
        self.assertEqual(records[0]["run_id"], "run-1")
        self.assertEqual(records[0]["task_id"], "TASK-01")
        self.assertEqual(records[0]["reason"], "owner_mismatch")

    def test_worker_claim_workspace_requires_active_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "worker",
                        "claim-workspace",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--branch",
                        "worker/TASK-01",
                        "--worktree",
                        str(repo),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["claimed"])
        self.assertEqual(payload["error"], "missing_active_task_lock")

    def test_worker_claim_workspace_rejects_branch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            subprocess.run(
                ["git", "checkout", "-b", "worker/TASK-01"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-1",
                        "started_at": "2026-05-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "worker",
                        "claim-workspace",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--branch",
                        "worker/OTHER",
                        "--worktree",
                        str(repo),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["claimed"])
        self.assertEqual(payload["error"], "branch_worktree_mismatch")
        self.assertEqual(payload["details"]["current_branch"], "worker/TASK-01")
        self.assertEqual(records[0]["started_at"], "2026-05-09T00:00:00+00:00")

    def test_workers_and_doctor_json_report_workspace_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            missing_worktree = repo.parent / "missing-worktree"
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-1",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "base-main",
                        "command": "agent TASK-01",
                        "workspace": {
                            "record_type": "workspace_claim",
                            "schema_version": 1,
                            "task_id": "TASK-01",
                            "run_id": "run-1",
                            "branch": "worker/TASK-01",
                            "worktree": str(missing_worktree),
                            "base_commit": "base-main",
                            "head_commit": "",
                            "current_branch": "worker/TASK-01",
                            "dirty": False,
                            "dirty_summary": [],
                            "claimed_at": "2026-05-09T00:01:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            workers_stdout = StringIO()
            workers_stderr = StringIO()
            with redirect_stdout(workers_stdout), redirect_stderr(workers_stderr):
                workers_exit = main(["workers", "--repo", str(repo), "--json"])
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()
            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo), "--json"])

            workers_payload = json.loads(workers_stdout.getvalue())
            doctor_payload = json.loads(doctor_stdout.getvalue())

        worker_codes = {
            diagnostic["code"]
            for diagnostic in workers_payload[0]["workspace_diagnostics"]
        }
        doctor_codes = {
            diagnostic["code"]
            for diagnostic in doctor_payload["workspace_diagnostics"]["diagnostics"]
        }
        self.assertEqual(workers_exit, 0)
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(workers_stderr.getvalue(), "")
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertIn("missing_claimed_worktree", worker_codes)
        self.assertIn("missing_claimed_worktree", doctor_codes)
        self.assertGreaterEqual(
            doctor_payload["workspace_diagnostics"]["count"],
            1,
        )

    def test_run_next_keeps_json_stdout_when_agent_streams_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                'import sys\nprint("agent out")\nprint("agent err", file=sys.stderr)\n',
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\ncommand = "python agent.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["session_id"], payload["run_id"])
        self.assertEqual(payload["session_id_source"], "fallback:run_id")
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["classification"], "unknown")
        self.assertIn("agent out", stderr.getvalue())
        self.assertNotIn("agent err", stderr.getvalue())
        self.assertIn("[vibe-loop] running TASK-01", stderr.getvalue())
        self.assertIn(
            f"[vibe-loop] session_id={payload['session_id']}", stderr.getvalue()
        )
        self.assertIn(
            f"[vibe-loop] worker process started task=TASK-01 "
            f"run_id={payload['run_id']} pid=",
            stderr.getvalue(),
        )
        self.assertIn(f"[vibe-loop] session_id={payload['session_id']}", log_text)
        self.assertIn("[vibe-loop] session_id_source=fallback:run_id", log_text)
        self.assertIn(
            f"[vibe-loop] worker process started task=TASK-01 "
            f"run_id={payload['run_id']} pid=",
            log_text,
        )
        self.assertIn("agent out", log_text)
        self.assertIn("agent err", log_text)

    def test_run_next_prefers_worker_report_over_task_probe_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source_path = Path(__file__).resolve().parents[1] / "src"
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "import sys\n"
                "sys.path.insert(0, sys.argv[3])\n"
                "from vibe_loop.cli import main\n"
                "report_exit = main([\n"
                "        'report',\n"
                "        '--repo', '.',\n"
                "        '--run-id', sys.argv[1],\n"
                "        '--task-id', sys.argv[2],\n"
                "        '--status', 'completed',\n"
                "        '--commit', 'reported-commit',\n"
                "        '--message', 'explicit worker result',\n"
                "        '--metadata-json', '{\"source\":\"agent\"}',\n"
                "])\n"
                "raise SystemExit(7 if report_exit == 0 else report_exit)\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {{run_id}} {{task_id}} {source_path}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            worker_reports = [
                record
                for record in records
                if record.get("record_type") == "worker_report"
            ]
            run_results = [
                record
                for record in records
                if record.get("record_type") == "run_result"
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["classification_source"], "worker_report")
        self.assertEqual(payload["worker_report"]["status"], "completed")
        self.assertEqual(payload["worker_report"]["commit"], "reported-commit")
        self.assertEqual(payload["worker_report"]["metadata"], {"source": "agent"})
        self.assertEqual(len(worker_reports), 1)
        self.assertEqual(len(run_results), 1)
        self.assertEqual(run_results[0]["classification_source"], "worker_report")
        self.assertIn(
            "lock_released",
            {record.get("record_type") for record in records},
        )
        self.assertIn("worker report status=completed", log_text)
        self.assertIn("reported-commit", log_text)

    def test_run_next_skips_completion_checks_after_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source_path = Path(__file__).resolve().parents[1] / "src"
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "import sys\n"
                "sys.path.insert(0, sys.argv[3])\n"
                "from vibe_loop.cli import main\n"
                "raise SystemExit(\n"
                "    main([\n"
                "        'report',\n"
                "        '--repo', '.',\n"
                "        '--run-id', sys.argv[1],\n"
                "        '--task-id', sys.argv[2],\n"
                "        '--status', 'blocked',\n"
                "        '--message', 'blocked by dependency',\n"
                "    ])\n"
                ")\n",
                encoding="utf-8",
            )
            (repo / "completion.py").write_text(
                "from pathlib import Path\n"
                "Path('completion-ran').write_text('ran', encoding='utf-8')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {{run_id}} {{task_id}} {source_path}"
            completion_command = f"{sys.executable} completion.py"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = "
                + json.dumps(command)
                + "\n[completion]\ncommands = ["
                + json.dumps(completion_command)
                + "]\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["classification"], "blocked")
        self.assertEqual(payload["classification_source"], "worker_report")
        self.assertEqual(payload["message"], "")
        self.assertFalse((repo / "completion-ran").exists())

    def test_run_next_records_active_worker_metadata_in_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import time\n"
                "metadata = {}\n"
                "for _ in range(100):\n"
                "    locks = list(Path('.vibe-loop/locks').glob('*.lock/lock.json'))\n"
                "    if locks:\n"
                "        metadata = json.loads(locks[0].read_text(encoding='utf-8'))\n"
                "        if metadata.get('worker_pid'):\n"
                "            break\n"
                "    time.sleep(0.01)\n"
                "Path('active-worker.json').write_text(\n"
                "    json.dumps(metadata),\n"
                "    encoding='utf-8',\n"
                ")\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\ncommand = "python agent.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            metadata = json.loads(
                (repo / "active-worker.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertEqual(metadata["record_type"], "active_run")
        self.assertEqual(metadata["task_id"], "TASK-01")
        self.assertEqual(metadata["run_id"], payload["run_id"])
        self.assertEqual(metadata["log"], payload["log"])
        self.assertEqual(metadata["command"], "python agent.py")
        self.assertIsInstance(metadata["worker_pid"], int)
        self.assertEqual(metadata["pid"], metadata["worker_pid"])
        self.assertEqual(metadata["pid_source"], "popen")
        self.assertEqual(metadata["pid_scope"], "configured_command_process")
        self.assertIsInstance(metadata["supervisor_pid"], int)
        self.assertIn("base_main", metadata)
        self.assertEqual(metadata["agent_kind"], "auto")
        self.assertEqual(metadata["agent_prompt_dialect"], "codex")
        self.assertEqual(
            metadata["agent_prompt_dialect_source"], "legacy-default:codex"
        )
        self.assertEqual(metadata["agent_skill_ref_prefix"], "$")
        self.assertEqual(
            metadata["agent_skill_ref_prefix_source"],
            "legacy-default:codex",
        )

    def test_run_next_worker_prompt_includes_traceability_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source_path = Path(__file__).resolve().parents[1] / "src"
            (repo / "docs").mkdir()
            spec_text = (
                "# Spec\n\n"
                "## PRD-SDE-003 Traceability\n\n"
                "Requirement text for launched workers.\n"
            )
            design_text = "# Design\n\n## ADR-1\n\nDesign context for workers.\n"
            (repo / "docs" / "spec.md").write_text(spec_text, encoding="utf-8")
            (repo / "docs" / "design.md").write_text(design_text, encoding="utf-8")
            fingerprint = file_fingerprint(repo / "docs" / "spec.md", "docs/spec.md")
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'TRACE-01','title':'Trace task',"
                "'status':'Next','dependencies':[],"
                "'acceptance':'Prompt includes bounded spec context.',"
                "'evidence':'CLI prompt assertion.',"
                "'requirement_ids':['PRD-SDE-003'],"
                "'spec_paths':['docs/spec.md'],"
                "'design_refs':['docs/design.md#ADR-1'],"
                "'approval_state':'approved',"
                "'source_fingerprints':[{'path':'docs/spec.md','size':"
                + str(fingerprint["size"])
                + ",'sha256':'"
                + str(fingerprint["sha256"])
                + "','redacted':False}]}]))\n",
                encoding="utf-8",
            )
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.path.insert(0, sys.argv[3])\n"
                "from vibe_loop.cli import main\n"
                "Path('worker-prompt.txt').write_text(sys.argv[4], encoding='utf-8')\n"
                "raise SystemExit(main([\n"
                "    'report', '--repo', '.', '--run-id', sys.argv[1],\n"
                "    '--task-id', sys.argv[2], '--status', 'completed',\n"
                "    '--commit', 'trace-commit', '--message', 'trace complete',\n"
                "]))\n",
                encoding="utf-8",
            )
            command = (
                f"{sys.executable} agent.py {{run_id}} {{task_id}} "
                f"{source_path} {{prompt}}"
            )
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                f"list = {toml_string(f'{sys.executable} list_tasks.py')}\n\n"
                "[agent]\n"
                'kind = "claude"\n'
                "command = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            prompt = (repo / "worker-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertIn("/vibe-loop TRACE-01", prompt)
        self.assertNotIn("$vibe-loop TRACE-01", prompt)
        self.assertEqual(payload["agent_prompt_dialect"], "claude")
        self.assertIn("### Normalized Task Traceability", prompt)
        self.assertIn('"requirement_ids": [', prompt)
        self.assertIn('"PRD-SDE-003"', prompt)
        self.assertIn('"spec_paths": [', prompt)
        self.assertIn('"docs/spec.md"', prompt)
        self.assertIn('"approval_state": "approved"', prompt)
        self.assertIn("### Spec-Aware Worker Context", prompt)
        self.assertIn("Requirement text for launched workers.", prompt)
        self.assertIn("Design context for workers.", prompt)
        self.assertIn('"id": "task.acceptance"', prompt)
        self.assertIn('"status": "current"', prompt)

    def test_run_next_refuses_traceable_task_when_command_omits_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
            (repo / "list_tasks.py").write_text(
                "import json\n"
                "print(json.dumps([{'id':'TRACE-01','title':'Trace task',"
                "'status':'Next','dependencies':[],"
                "'requirement_ids':['PRD-SDE-003'],"
                "'spec_paths':['docs/spec.md']}]))\n",
                encoding="utf-8",
            )
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "Path('agent-ran').write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {{task_id}}"
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                f"list = {toml_string(f'{sys.executable} list_tasks.py')}\n\n"
                "[agent]\n"
                "command = " + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse((repo / "agent-ran").exists())
        self.assertIn("agent.command must include {prompt}", stderr.getvalue())
        self.assertIn("spec context cannot be delivered", stderr.getvalue())

    def test_run_next_captures_explicit_worker_session_id_from_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "print('session id: explicit-native-456', file=sys.stderr)\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\ncommand = "python agent.py"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            run_result = next(
                record
                for record in run_records
                if record.get("record_type") == "run_result"
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["session_id"], "explicit-native-456")
        self.assertEqual(payload["session_id_source"], "native:stderr")
        self.assertEqual(payload["agent_command_source"], "explicit")
        self.assertEqual(payload["classification"], "completed")
        self.assertNotEqual(payload["session_id"], payload["run_id"])
        self.assertNotIn("session id: explicit-native-456", stderr.getvalue())
        self.assertIn("[vibe-loop] session_id=explicit-native-456", stderr.getvalue())
        self.assertIn("session id: explicit-native-456", log_text)
        self.assertIn("session_id_source=native:stderr", log_text)
        self.assertEqual(run_result["session_id"], "explicit-native-456")
        self.assertEqual(run_result["session_id_source"], "native:stderr")

    def test_run_next_supports_configured_claude_prompt_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "worker.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "prompt = sys.argv[1]\n"
                "Path('worker-prompt.txt').write_text(prompt, encoding='utf-8')\n"
                "print(f'claude out: {prompt.splitlines()[0]}')\n"
                "print(f'claude err: {prompt.splitlines()[0]}', file=sys.stderr)\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{shell_command(sys.executable, 'worker.py')} {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "claude"\ncommand = ' + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            prompt = (repo / "worker-prompt.txt").read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            run_result = next(
                record
                for record in run_records
                if record.get("record_type") == "run_result"
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["classification"], "completed")
        self.assertIn("/vibe-loop TASK-01", prompt)
        self.assertNotIn("$vibe-loop TASK-01", prompt)
        self.assertEqual(payload["agent_prompt_dialect"], "claude")
        self.assertEqual(payload["agent_prompt_dialect_source"], "agent.kind:claude")
        self.assertIn("claude out: /vibe-loop TASK-01", stderr.getvalue())
        self.assertNotIn("claude err", stderr.getvalue())
        self.assertIn("claude out: /vibe-loop TASK-01", log_text)
        self.assertIn("claude err: /vibe-loop TASK-01", log_text)
        self.assertEqual(run_result["task_id"], "TASK-01")
        self.assertEqual(run_result["status"], "completed")
        self.assertEqual(run_result["agent_prompt_dialect"], "claude")

    def test_run_next_refuses_custom_agent_without_prompt_dialect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "Path('agent-ran').write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "custom"\ncommand = ' + json.dumps(command) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse((repo / "agent-ran").exists())
        self.assertIn("agent.kind is custom", stderr.getvalue())

    def test_run_next_custom_agent_uses_explicit_skill_ref_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "agent.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path('worker-prompt.txt').write_text(sys.argv[1], encoding='utf-8')\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} agent.py {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "custom"\n'
                "command = " + json.dumps(command) + "\n"
                'skill_ref_prefix = "/"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            prompt = (repo / "worker-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertIn("/vibe-loop TASK-01", prompt)
        self.assertEqual(payload["agent_prompt_dialect"], "claude")
        self.assertEqual(
            payload["agent_prompt_dialect_source"],
            "explicit:agent.skill_ref_prefix",
        )

    def test_next_supports_configured_claude_prompt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            (repo / "selector.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "Path('selection-prompt.txt').write_text(sys.argv[1], encoding='utf-8')\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
                encoding="utf-8",
            )
            selector_cmd = f"{sys.executable} selector.py {{prompt}}"
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\nselection_command = " + json.dumps(selector_cmd) + "\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["next", "--repo", str(repo), "--ask-agent"])

            prompt = (repo / "selection-prompt.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue().strip(), "TASK-02")
        self.assertIn("TASK-01", prompt)
        self.assertIn("TASK-02", prompt)
        self.assertIn("Return JSON only", prompt)
        self.assertIn("agent selected TASK-02", stderr.getvalue())

    def test_run_next_empty_queue_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(
                PLAN.replace("| TASK-01 | P0 | Next |", "| TASK-01 | P0 | Done |"),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["run-next", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("no runnable tasks", stderr.getvalue())

    def test_main_integration_cli_allows_one_holder_and_blocks_waiter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-holder",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "pid_source": "popen",
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            holder_stdout = StringIO()
            holder_stderr = StringIO()
            waiter_stdout = StringIO()
            waiter_stderr = StringIO()
            status_stdout = StringIO()
            status_stderr = StringIO()
            release_stdout = StringIO()
            release_stderr = StringIO()

            with redirect_stdout(holder_stdout), redirect_stderr(holder_stderr):
                holder_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                        "--json",
                    ]
                )
            with redirect_stdout(waiter_stdout), redirect_stderr(waiter_stderr):
                waiter_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-waiter",
                        "--task-id",
                        "TASK-02",
                        "--json",
                    ]
                )
            with redirect_stdout(status_stdout), redirect_stderr(status_stderr):
                status_exit = main(
                    ["main-integration", "status", "--repo", str(repo), "--json"]
                )
            with patch.dict("os.environ", {"VIBE_LOOP_FENCING_TOKEN": "task-token"}):
                with redirect_stdout(release_stdout), redirect_stderr(release_stderr):
                    release_exit = main(
                        [
                            "main-integration",
                            "release",
                            "--repo",
                            str(repo),
                            "--run-id",
                            "run-holder",
                            "--task-id",
                            "TASK-01",
                            "--json",
                        ]
                    )

            holder = json.loads(holder_stdout.getvalue())
            waiter = json.loads(waiter_stdout.getvalue())
            status = json.loads(status_stdout.getvalue())
            release = json.loads(release_stdout.getvalue())
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(holder_exit, 0)
        self.assertEqual(holder_stderr.getvalue(), "")
        self.assertTrue(holder["acquired"])
        self.assertEqual(holder["status"]["state"], "held")
        self.assertEqual(holder["status"]["owner_task_id"], "TASK-01")
        self.assertEqual(holder["status"]["run_id"], "run-holder")
        self.assertEqual(holder["status"]["pid"], os.getpid())
        self.assertEqual(holder["status"]["pid_source"], "active_task_lock:worker_pid")
        self.assertEqual(
            holder["status"]["metadata"]["owner_started_at"],
            "2026-05-09T00:00:00+00:00",
        )
        self.assertEqual(waiter_exit, 1)
        self.assertEqual(waiter_stderr.getvalue(), "")
        self.assertFalse(waiter["acquired"])
        self.assertEqual(waiter["status"]["state"], "held")
        self.assertEqual(waiter["status"]["owner_task_id"], "TASK-01")
        self.assertEqual(waiter["status"]["run_id"], "run-holder")
        self.assertEqual(status_exit, 0)
        self.assertEqual(status_stderr.getvalue(), "")
        self.assertTrue(status["locked"])
        self.assertEqual(status["owner_task_id"], "TASK-01")
        self.assertEqual(release_exit, 0)
        self.assertEqual(release_stderr.getvalue(), "")
        self.assertTrue(release["released"])
        self.assertFalse(release["status"]["locked"])
        self.assertEqual(
            [record["record_type"] for record in records],
            ["lock_acquired", "lock_released"],
        )
        self.assertEqual(records[0]["lock_kind"], "integration")
        self.assertEqual(records[0]["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(records[1]["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(records[1]["owner_task_id"], "TASK-01")

    def test_main_integration_release_rejects_fencing_token_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-holder")
            acquire_stdout = StringIO()
            acquire_stderr = StringIO()
            release_stdout = StringIO()
            release_stderr = StringIO()

            with redirect_stdout(acquire_stdout), redirect_stderr(acquire_stderr):
                acquire_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                        "--json",
                    ]
                )
            with redirect_stdout(release_stdout), redirect_stderr(release_stderr):
                release_exit = main(
                    [
                        "main-integration",
                        "release",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                        "--fencing-token",
                        "stale-token",
                        "--json",
                    ]
                )

            acquired = json.loads(acquire_stdout.getvalue())
            released = json.loads(release_stdout.getvalue())

        self.assertEqual(acquire_exit, 0)
        self.assertEqual(acquire_stderr.getvalue(), "")
        self.assertTrue(acquired["status"]["fencing_token"])
        self.assertEqual(release_exit, 1)
        self.assertEqual(release_stderr.getvalue(), "")
        self.assertFalse(released["released"])
        self.assertEqual(released["error"], "fencing_token_mismatch")
        self.assertTrue(released["status"]["locked"])

    def test_main_integration_acquire_waits_until_holder_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-holder")
            write_active_run_lock(repo, "TASK-02", "run-waiter")
            holder_stdout = StringIO()
            holder_stderr = StringIO()
            waiter_stdout = StringIO()
            waiter_stderr = StringIO()
            sleep_calls: list[float] = []

            with redirect_stdout(holder_stdout), redirect_stderr(holder_stderr):
                holder_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                        "--json",
                    ]
                )

            def release_holder(delay: float) -> None:
                sleep_calls.append(delay)
                shutil.rmtree(repo / ".vibe-loop" / "locks" / "main-integration.lock")

            with patch("vibe_loop.cli.time.sleep", side_effect=release_holder):
                with redirect_stdout(waiter_stdout), redirect_stderr(waiter_stderr):
                    waiter_exit = main(
                        [
                            "main-integration",
                            "acquire",
                            "--repo",
                            str(repo),
                            "--run-id",
                            "run-waiter",
                            "--task-id",
                            "TASK-02",
                            "--wait",
                            "--timeout",
                            "5",
                            "--poll-interval",
                            "0.1",
                            "--json",
                        ]
                    )

            holder = json.loads(holder_stdout.getvalue())
            waiter = json.loads(waiter_stdout.getvalue())

        self.assertEqual(holder_exit, 0)
        self.assertEqual(holder_stderr.getvalue(), "")
        self.assertTrue(holder["acquired"])
        self.assertEqual(waiter_exit, 0)
        self.assertEqual(waiter_stderr.getvalue(), "")
        self.assertEqual(sleep_calls, [0.1])
        self.assertTrue(waiter["acquired"])
        self.assertFalse(waiter["timed_out"])
        self.assertEqual(waiter["status"]["owner_task_id"], "TASK-02")
        self.assertEqual(waiter["status"]["run_id"], "run-waiter")

    def test_main_integration_acquire_wait_timeout_respects_live_holder(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-holder")
            write_active_run_lock(repo, "TASK-02", "run-waiter")
            holder_stdout = StringIO()
            holder_stderr = StringIO()
            waiter_stdout = StringIO()
            waiter_stderr = StringIO()
            status_stdout = StringIO()
            status_stderr = StringIO()

            with redirect_stdout(holder_stdout), redirect_stderr(holder_stderr):
                holder_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                    ]
                )
            with patch("vibe_loop.cli.time.sleep") as sleep:
                with redirect_stdout(waiter_stdout), redirect_stderr(waiter_stderr):
                    waiter_exit = main(
                        [
                            "main-integration",
                            "acquire",
                            "--repo",
                            str(repo),
                            "--run-id",
                            "run-waiter",
                            "--task-id",
                            "TASK-02",
                            "--wait",
                            "--timeout",
                            "0",
                            "--json",
                        ]
                    )
                sleep.assert_not_called()
            with redirect_stdout(status_stdout), redirect_stderr(status_stderr):
                status_exit = main(
                    ["main-integration", "status", "--repo", str(repo), "--json"]
                )

            waiter = json.loads(waiter_stdout.getvalue())
            status = json.loads(status_stdout.getvalue())

        self.assertEqual(holder_exit, 0)
        self.assertEqual(holder_stderr.getvalue(), "")
        self.assertEqual(waiter_exit, 1)
        self.assertEqual(waiter_stderr.getvalue(), "")
        self.assertFalse(waiter["acquired"])
        self.assertTrue(waiter["timed_out"])
        self.assertEqual(waiter["status"]["state"], "held")
        self.assertEqual(waiter["status"]["owner_task_id"], "TASK-01")
        self.assertEqual(status_exit, 0)
        self.assertEqual(status_stderr.getvalue(), "")
        self.assertEqual(status["owner_task_id"], "TASK-01")
        self.assertEqual(status["run_id"], "run-holder")

    def test_main_integration_acquire_wait_reports_stale_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-02", "run-waiter")
            manager = LockManager(repo / ".vibe-loop" / "locks")
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-holder",
                metadata={"pid": 999999999, "host": socket.gethostname()},
            )
            waiter_stdout = StringIO()
            waiter_stderr = StringIO()

            with patch("vibe_loop.cli.time.sleep") as sleep:
                with redirect_stdout(waiter_stdout), redirect_stderr(waiter_stderr):
                    waiter_exit = main(
                        [
                            "main-integration",
                            "acquire",
                            "--repo",
                            str(repo),
                            "--run-id",
                            "run-waiter",
                            "--task-id",
                            "TASK-02",
                            "--wait",
                            "--timeout",
                            "10",
                            "--json",
                        ]
                    )
                sleep.assert_not_called()

            waiter = json.loads(waiter_stdout.getvalue())

        self.assertEqual(waiter_exit, 1)
        self.assertEqual(waiter_stderr.getvalue(), "")
        self.assertFalse(waiter["acquired"])
        self.assertFalse(waiter["timed_out"])
        self.assertEqual(waiter["status"]["state"], "stale")
        self.assertEqual(waiter["status"]["stale_reason"], "missing_process")
        self.assertEqual(waiter["status"]["owner_task_id"], "TASK-01")

    def test_main_integration_acquire_rechecks_workspace_after_wait(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            ),
            tempfile.TemporaryDirectory() as directory,
        ):
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            base_commit = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
            write_active_run_lock(repo, "TASK-01", "run-holder")
            write_active_run_lock(
                repo,
                "TASK-02",
                "run-waiter",
                workspace={
                    "schema_version": 1,
                    "record_type": "workspace_claim",
                    "task_id": "TASK-02",
                    "run_id": "run-waiter",
                    "branch": "main",
                    "worktree": str(repo),
                    "base_commit": base_commit,
                    "head_commit": base_commit,
                    "current_branch": "main",
                    "dirty": False,
                    "dirty_summary": [],
                    "claimed_at": "2026-05-09T00:01:00+00:00",
                },
            )
            holder_stdout = StringIO()
            holder_stderr = StringIO()
            waiter_stdout = StringIO()
            waiter_stderr = StringIO()

            with redirect_stdout(holder_stdout), redirect_stderr(holder_stderr):
                holder_exit = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-holder",
                        "--task-id",
                        "TASK-01",
                    ]
                )

            def dirty_workspace_and_release(delay: float) -> None:
                self.assertEqual(delay, 0.1)
                (repo / "notes.txt").write_text("not committed\n", encoding="utf-8")
                LockManager(repo / ".vibe-loop" / "locks").release_main_integration(
                    task_id="TASK-01",
                    run_id="run-holder",
                )

            with patch(
                "vibe_loop.cli.time.sleep",
                side_effect=dirty_workspace_and_release,
            ):
                with redirect_stdout(waiter_stdout), redirect_stderr(waiter_stderr):
                    waiter_exit = main(
                        [
                            "main-integration",
                            "acquire",
                            "--repo",
                            str(repo),
                            "--run-id",
                            "run-waiter",
                            "--task-id",
                            "TASK-02",
                            "--wait",
                            "--timeout",
                            "5",
                            "--poll-interval",
                            "0.1",
                            "--json",
                        ]
                    )

            payload = json.loads(waiter_stdout.getvalue())
            codes = {
                diagnostic["code"] for diagnostic in payload["workspace_diagnostics"]
            }
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()

        self.assertEqual(holder_exit, 0)
        self.assertEqual(holder_stderr.getvalue(), "")
        self.assertEqual(waiter_exit, 1)
        self.assertEqual(waiter_stderr.getvalue(), "")
        self.assertFalse(payload["acquired"])
        self.assertEqual(payload["error"], "workspace_preflight_failed")
        self.assertIn("foreign_dirty_claimed_worktree", codes)
        self.assertFalse(lock_exists)

    def test_main_integration_acquire_rejects_task_lock_owner_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-other")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["acquired"])
        self.assertEqual(payload["error"], "owner_mismatch")
        self.assertEqual(payload["active_run_ids"], ["run-other"])
        self.assertFalse(lock_exists)

    def test_main_integration_acquire_rejects_pid_owner_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-other")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--pid",
                        "123",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["acquired"])
        self.assertEqual(payload["error"], "owner_mismatch")
        self.assertEqual(payload["active_run_ids"], ["run-other"])
        self.assertFalse(lock_exists)

    def test_main_integration_acquire_rejects_workspace_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            missing_worktree = repo.parent / "missing-worker"
            write_active_run_lock(
                repo,
                "TASK-01",
                "run-1",
                workspace={
                    "schema_version": 1,
                    "record_type": "workspace_claim",
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "worker/TASK-01",
                    "worktree": str(missing_worktree),
                    "base_commit": "base",
                    "head_commit": "",
                    "current_branch": "worker/TASK-01",
                    "dirty": False,
                    "dirty_summary": [],
                    "claimed_at": "2026-05-09T00:01:00+00:00",
                },
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            codes = {
                diagnostic["code"] for diagnostic in payload["workspace_diagnostics"]
            }
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["acquired"])
        self.assertEqual(payload["error"], "workspace_preflight_failed")
        self.assertEqual(payload["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertIn("missing_claimed_worktree", codes)
        self.assertFalse(lock_exists)
        self.assertEqual(records[0]["record_type"], "workspace_claim_mismatch")
        self.assertEqual(records[0]["reason"], "workspace_preflight_failed")
        self.assertEqual(records[0]["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(
            records[0]["diagnostic_count"],
            len(records[0]["details"]["workspace_diagnostics"]),
        )
        self.assertEqual(
            {
                diagnostic["code"]
                for diagnostic in records[0]["details"]["workspace_diagnostics"]
            },
            codes,
        )

    def test_main_integration_acquire_rejects_pid_workspace_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            init_planning_repo(repo, PLAN)
            missing_worktree = repo.parent / "missing-worker"
            write_active_run_lock(
                repo,
                "TASK-01",
                "run-1",
                workspace={
                    "schema_version": 1,
                    "record_type": "workspace_claim",
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "branch": "worker/TASK-01",
                    "worktree": str(missing_worktree),
                    "base_commit": "base",
                    "head_commit": "",
                    "current_branch": "worker/TASK-01",
                    "dirty": False,
                    "dirty_summary": [],
                    "claimed_at": "2026-05-09T00:01:00+00:00",
                },
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                        "--pid",
                        "123",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            codes = {
                diagnostic["code"] for diagnostic in payload["workspace_diagnostics"]
            }
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["acquired"])
        self.assertEqual(payload["error"], "workspace_preflight_failed")
        self.assertEqual(payload["started_at"], "2026-05-09T00:00:00+00:00")
        self.assertIn("missing_claimed_worktree", codes)
        self.assertFalse(lock_exists)

    def test_main_integration_acquire_requires_pid_or_active_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "main-integration",
                        "acquire",
                        "--repo",
                        str(repo),
                        "--run-id",
                        "run-1",
                        "--task-id",
                        "TASK-01",
                    ]
                )
            lock_exists = (
                repo / ".vibe-loop" / "locks" / "main-integration.lock"
            ).exists()

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("active task lock", stderr.getvalue())
        self.assertFalse(lock_exists)

    def test_main_integration_parent_json_flag_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    ["main-integration", "--json", "status", "--repo", str(repo)]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(payload["locked"])
        self.assertEqual(payload["state"], "available")

    def test_main_integration_subprocess_acquire_uses_active_worker_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            active_lock = repo / ".vibe-loop" / "locks" / "TASK-01.lock"
            active_lock.mkdir(parents=True)
            (active_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-holder",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "pid_source": "popen",
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            source_path = Path(__file__).resolve().parents[1] / "src"
            cli_script = (
                "import sys; "
                f"sys.path.insert(0, {str(source_path)!r}); "
                "from vibe_loop.cli import main; "
                "raise SystemExit(main(sys.argv[1:]))"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    cli_script,
                    "main-integration",
                    "acquire",
                    "--repo",
                    str(repo),
                    "--run-id",
                    "run-holder",
                    "--task-id",
                    "TASK-01",
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            status_stdout = StringIO()
            status_stderr = StringIO()
            with redirect_stdout(status_stdout), redirect_stderr(status_stderr):
                status_exit = main(
                    ["main-integration", "status", "--repo", str(repo), "--json"]
                )

            acquired = json.loads(result.stdout)
            status = json.loads(status_stdout.getvalue())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(status_exit, 0)
        self.assertEqual(status_stderr.getvalue(), "")
        self.assertEqual(acquired["status"]["pid"], os.getpid())
        self.assertEqual(
            acquired["status"]["pid_source"],
            "active_task_lock:worker_pid",
        )
        self.assertEqual(status["state"], "held")
        self.assertEqual(status["process_state"], "running")
        self.assertIsNone(status["stale_reason"])

    def test_tasks_locks_does_not_require_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["tasks", "locks", "--repo", str(repo)])

        self.assertEqual(exit_code, 0)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_workers_reports_running_and_missing_processes_without_plan_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            running_lock = lock_root / "TASK-01.lock"
            missing_lock = lock_root / "TASK-02.lock"
            running_lock.mkdir(parents=True)
            missing_lock.mkdir()
            (running_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-1",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "pid_source": "popen",
                        "pid_scope": "configured_command_process",
                        "supervisor_pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "abc123",
                        "command": "python worker.py",
                    }
                ),
                encoding="utf-8",
            )
            (missing_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-02",
                        "run_id": "run-2",
                        "pid": 999999999,
                        "worker_pid": 999999999,
                        "supervisor_pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:01:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-2.log"),
                        "base_main": "abc123",
                        "command": "python worker.py",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            runs_path = repo / ".vibe-loop" / "runs.jsonl"
            runs_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "worker_report",
                        "run_id": "run-1",
                        "task_id": "TASK-01",
                        "status": "blocked",
                        "commit": "",
                        "message": "waiting on dependency",
                        "metadata": {},
                        "reported_at": "2026-05-09T00:00:30+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["workers", "--repo", str(repo), "--json"])

            text_stdout = StringIO()
            text_stderr = StringIO()
            with redirect_stdout(text_stdout), redirect_stderr(text_stderr):
                text_exit = main(["workers", "--repo", str(repo)])

            payload = parse_run_result(self, stdout, stderr, exit_code)

        self.assertEqual(exit_code, 0)
        self.assertEqual(text_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(text_stderr.getvalue(), "")
        self.assertEqual(payload[0]["task_id"], "TASK-01")
        self.assertEqual(payload[0]["state"], "running")
        self.assertEqual(payload[0]["process_state"], "running")
        self.assertEqual(payload[0]["command"], "python worker.py")
        self.assertEqual(payload[0]["pid_source"], "popen")
        self.assertEqual(payload[0]["pid_scope"], "configured_command_process")
        self.assertEqual(payload[0]["lifecycle_state"], "reported")
        self.assertEqual(payload[0]["result_status"], "blocked")
        self.assertEqual(payload[1]["task_id"], "TASK-02")
        self.assertEqual(payload[1]["state"], "stale")
        self.assertEqual(payload[1]["process_state"], "missing")
        self.assertEqual(payload[1]["stale_reason"], "missing_process")
        self.assertEqual(payload[1]["lifecycle_state"], "")
        text_output = text_stdout.getvalue()
        self.assertIn(
            "TASK-01\trun-1\trunning\tprocess=running"
            f"\tpid={os.getpid()}"
            "\tstarted=2026-05-09T00:00:00+00:00"
            f"\tlog={repo / '.vibe-loop' / 'runs' / 'run-1.log'}"
            "\tcommand=python worker.py\tlifecycle=reported\tresult=blocked\n",
            text_output,
        )
        self.assertIn(
            "TASK-02\trun-2\tstale\tprocess=missing"
            "\tpid=999999999"
            "\tstarted=2026-05-09T00:01:00+00:00"
            f"\tlog={repo / '.vibe-loop' / 'runs' / 'run-2.log'}"
            "\tcommand=python worker.py\tlifecycle=-\tmissing_process\n",
            text_output,
        )
        self.assertIn("1 stale lock(s) found.", text_output)
        self.assertIn("vibe-loop workers clean", text_output)

    def test_doctor_json_reports_concurrency_diagnostics_from_worker_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            running_lock = lock_root / "TASK-01.lock"
            stale_lock = lock_root / "TASK-02.lock"
            running_lock.mkdir(parents=True)
            stale_lock.mkdir()
            (running_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-01",
                        "run_id": "run-1",
                        "pid": os.getpid(),
                        "worker_pid": os.getpid(),
                        "pid_source": "popen",
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (stale_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "TASK-02",
                        "run_id": "run-2",
                        "pid": 999999999,
                        "worker_pid": 999999999,
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:01:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            workers_stdout = StringIO()
            workers_stderr = StringIO()
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()

            with redirect_stdout(workers_stdout), redirect_stderr(workers_stderr):
                workers_exit = main(["workers", "--repo", str(repo), "--json"])
            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo), "--json"])

            workers_payload = json.loads(workers_stdout.getvalue())
            doctor_payload = json.loads(doctor_stdout.getvalue())

        blocked_workers = [
            worker
            for worker in workers_payload
            if worker["state"] != "running"
            or worker["result_status"] in {"blocked", "failed", "unknown"}
        ]
        diagnostics = doctor_payload["concurrency_diagnostics"]
        self.assertEqual(workers_exit, 0)
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(workers_stderr.getvalue(), "")
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(diagnostics["active_lock_count"], len(workers_payload))
        self.assertEqual(
            diagnostics["wip_count"],
            sum(1 for worker in workers_payload if worker["state"] == "running"),
        )
        self.assertEqual(
            diagnostics["blocked_ratio"],
            len(blocked_workers) / len(workers_payload),
        )
        self.assertEqual(
            len(diagnostics["lock_contention_events"]),
            len(blocked_workers),
        )
        self.assertEqual(
            diagnostics["lock_contention_events"][0]["task_id"],
            "TASK-02",
        )
        self.assertEqual(
            diagnostics["lock_contention_events"][0]["reason"],
            "missing_process",
        )
        self.assertEqual(
            diagnostics["lock_contention_events"][0]["stale_reason"],
            workers_payload[1]["stale_reason"],
        )
        self.assertIsNone(diagnostics["lock_contention_events"][0]["result_status"])

    def test_doctor_concurrency_diagnostics_include_blocked_worker_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_active_run_lock(repo, "TASK-01", "run-1")
            runs_path = repo / ".vibe-loop" / "runs.jsonl"
            original_runs = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "worker_report",
                        "run_id": "run-1",
                        "task_id": "TASK-01",
                        "status": "blocked",
                        "commit": "",
                        "message": "waiting on dependency",
                        "metadata": {},
                        "reported_at": "2026-05-09T00:00:30+00:00",
                    }
                )
                + "\n"
            )
            runs_path.parent.mkdir(parents=True, exist_ok=True)
            runs_path.write_text(original_runs, encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["doctor", "--repo", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())
            after_runs = runs_path.read_text(encoding="utf-8")

        diagnostics = payload["concurrency_diagnostics"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(diagnostics["active_lock_count"], 1)
        self.assertEqual(diagnostics["wip_count"], 1)
        self.assertEqual(diagnostics["blocked_ratio"], 1.0)
        self.assertEqual(
            diagnostics["lock_contention_events"][0]["result_status"],
            "blocked",
        )
        self.assertIsNone(diagnostics["lock_contention_events"][0]["stale_reason"])
        self.assertEqual(
            diagnostics["lock_contention_events"][0]["reason"],
            "result_blocked",
        )
        self.assertEqual(after_runs, original_runs)

    def test_workers_and_doctor_report_expired_lease_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            manager.acquire(
                "TASK-01",
                "run-1",
                metadata={
                    "record_type": "active_run",
                    "schema_version": 1,
                    "task_id": "TASK-01",
                    "run_id": "run-1",
                    "pid": os.getpid(),
                    "worker_pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started_at": "2026-05-09T00:00:00+00:00",
                    "heartbeat_at": "2000-01-01T00:00:00+00:00",
                    "lease_seconds": 1,
                    "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                    "base_main": "abc123",
                    "command": "python worker.py",
                },
            )
            workers_stdout = StringIO()
            workers_stderr = StringIO()
            doctor_stdout = StringIO()
            doctor_stderr = StringIO()

            with redirect_stdout(workers_stdout), redirect_stderr(workers_stderr):
                workers_exit = main(["workers", "--repo", str(repo), "--json"])
            with redirect_stdout(doctor_stdout), redirect_stderr(doctor_stderr):
                doctor_exit = main(["doctor", "--repo", str(repo), "--json"])

            workers_payload = json.loads(workers_stdout.getvalue())
            doctor_payload = json.loads(doctor_stdout.getvalue())

        self.assertEqual(workers_exit, 0)
        self.assertEqual(doctor_exit, 0)
        self.assertEqual(workers_stderr.getvalue(), "")
        self.assertEqual(doctor_stderr.getvalue(), "")
        self.assertEqual(workers_payload[0]["state"], "stale")
        self.assertEqual(workers_payload[0]["process_state"], "running")
        self.assertEqual(workers_payload[0]["stale_reason"], "lease_expired")
        self.assertEqual(doctor_payload["stale_locks"]["count"], 1)
        self.assertEqual(
            doctor_payload["stale_locks"]["locks"][0]["stale_reason"],
            "lease_expired",
        )

    def test_workers_text_does_not_require_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["workers", "--repo", str(repo)])

        self.assertEqual(exit_code, 0)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_workers_clean_dry_run_lists_stale_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            stale_lock = lock_root / "STALE-01.lock"
            stale_lock.mkdir(parents=True)
            (stale_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "STALE-01",
                        "run_id": "run-1",
                        "pid": 999999999,
                        "worker_pid": 999999999,
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "abc123",
                        "command": "agent STALE-01",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["workers", "--repo", str(repo), "clean"])
            lock_still_exists = stale_lock.exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(lock_still_exists)
        self.assertIn("1 stale lock(s) found (dry-run", stdout.getvalue())
        self.assertIn("STALE-01", stdout.getvalue())

    def test_workers_clean_force_removes_stale_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            stale_lock = lock_root / "STALE-01.lock"
            stale_lock.mkdir(parents=True)
            (stale_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "STALE-01",
                        "run_id": "run-1",
                        "pid": 999999999,
                        "worker_pid": 999999999,
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "abc123",
                        "command": "agent STALE-01",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["workers", "--repo", str(repo), "clean", "--force"])
            lock_still_exists = stale_lock.exists()
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertFalse(lock_still_exists)
        self.assertIn("Removed 1 stale lock(s)", stdout.getvalue())
        self.assertEqual(records[0]["record_type"], "lock_expired")
        self.assertEqual(records[0]["run_id"], "run-1")
        self.assertEqual(records[0]["task_id"], "STALE-01")
        self.assertEqual(records[0]["stale_reason"], "missing_process")

    def test_workers_clean_force_records_integration_lock_owner_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manager = LockManager(repo / ".vibe-loop" / "locks")
            manager.acquire_main_integration(
                task_id="TASK-01",
                run_id="run-1",
                metadata={
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "owner_started_at": "2026-05-09T00:00:00+00:00",
                },
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["workers", "--repo", str(repo), "clean", "--force"])
            records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertIn("Removed 1 stale lock(s)", stdout.getvalue())
        self.assertEqual(records[0]["record_type"], "lock_expired")
        self.assertEqual(records[0]["run_id"], "run-1")
        self.assertEqual(records[0]["task_id"], "TASK-01")
        self.assertEqual(records[0]["lock_kind"], "integration")
        self.assertEqual(records[0]["started_at"], "2026-05-09T00:00:00+00:00")

    def test_workers_clean_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lock_root = repo / ".vibe-loop" / "locks"
            stale_lock = lock_root / "STALE-01.lock"
            stale_lock.mkdir(parents=True)
            (stale_lock / "lock.json").write_text(
                json.dumps(
                    {
                        "record_type": "active_run",
                        "schema_version": 1,
                        "task_id": "STALE-01",
                        "run_id": "run-1",
                        "pid": 999999999,
                        "worker_pid": 999999999,
                        "host": socket.gethostname(),
                        "started_at": "2026-05-09T00:00:00+00:00",
                        "log": str(repo / ".vibe-loop" / "runs" / "run-1.log"),
                        "base_main": "abc123",
                        "command": "agent STALE-01",
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["workers", "--repo", str(repo), "clean", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload["stale_locks"]), 1)
        self.assertEqual(payload["stale_locks"][0]["task_id"], "STALE-01")
        self.assertEqual(payload["cleaned"], [])

    def test_workers_clean_no_stale_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["workers", "--repo", str(repo), "clean"])

        self.assertEqual(exit_code, 0)
        self.assertIn("No stale locks found", stdout.getvalue())

    def test_runs_list_and_inspect_do_not_require_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runs_dir = repo / ".vibe-loop" / "runs"
            runs_dir.mkdir(parents=True)
            (runs_dir / "run-1.log").write_text("first log\n", encoding="utf-8")
            runs_path = repo / ".vibe-loop" / "runs.jsonl"
            records = [
                {
                    "schema_version": 1,
                    "record_type": "lock_acquired",
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "occurred_at": "2026-05-09T00:00:00+00:00",
                    "lock_kind": "task",
                    "lock_path": str(repo / ".vibe-loop" / "locks" / "TASK-01.lock"),
                },
                {
                    "schema_version": 1,
                    "record_type": "worker_report",
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "status": "completed",
                    "commit": "abc123",
                    "message": "",
                    "metadata": {"source": "worker"},
                    "reported_at": "2026-05-09T00:00:00+00:00",
                },
                {
                    "schema_version": 1,
                    "record_type": "run_state_transition",
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "occurred_at": "2026-05-09T00:00:30+00:00",
                    "from_state": "started",
                    "to_state": "classified",
                    "reason": "worker_report",
                },
                {
                    "schema_version": 3,
                    "record_type": "run_result",
                    "run_id": "run-1",
                    "session_id": "native-1",
                    "session_id_source": "native:stdout",
                    "task_id": "TASK-01",
                    "classification": "completed",
                    "status": "completed",
                    "exit_code": 0,
                    "log": str(runs_dir / "run-1.log"),
                    "start_main": "aaa",
                    "end_main": "bbb",
                    "message": "",
                    "classification_source": "worker_report",
                    "worker_report": {
                        "run_id": "run-1",
                        "task_id": "TASK-01",
                        "status": "completed",
                        "commit": "abc123",
                        "message": "",
                        "metadata": {"source": "worker"},
                        "reported_at": "2026-05-09T00:00:00+00:00",
                    },
                    "finished_at": "2026-05-09T00:01:00+00:00",
                },
                {
                    "schema_version": 1,
                    "record_type": "worker_report",
                    "run_id": "run-2",
                    "task_id": "TASK-02",
                    "status": "blocked",
                    "commit": "",
                    "message": "waiting on dependency",
                    "metadata": {},
                    "reported_at": "2026-05-09T00:02:00+00:00",
                },
                {
                    "schema_version": 1,
                    "record_type": "future_record",
                    "run_id": "run-1",
                    "task_id": "TASK-01",
                    "occurred_at": "2026-05-09T00:03:00+00:00",
                },
            ]
            runs_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            list_stdout = StringIO()
            list_stderr = StringIO()
            list_text_stdout = StringIO()
            list_text_stderr = StringIO()
            inspect_stdout = StringIO()
            inspect_stderr = StringIO()
            inspect_json_stdout = StringIO()
            inspect_json_stderr = StringIO()

            with redirect_stdout(list_stdout), redirect_stderr(list_stderr):
                list_exit = main(["runs", "list", "--repo", str(repo), "--json"])
            with redirect_stdout(list_text_stdout), redirect_stderr(list_text_stderr):
                list_text_exit = main(
                    ["runs", "list", "--repo", str(repo), "--limit", "2"]
                )
            with redirect_stdout(inspect_stdout), redirect_stderr(inspect_stderr):
                inspect_exit = main(["runs", "inspect", "run-1", "--repo", str(repo)])
            with (
                redirect_stdout(inspect_json_stdout),
                redirect_stderr(inspect_json_stderr),
            ):
                inspect_json_exit = main(
                    ["runs", "inspect", "run-1", "--repo", str(repo), "--json"]
                )

            list_payload = json.loads(list_stdout.getvalue())
            inspect_payload = json.loads(inspect_json_stdout.getvalue())

        self.assertEqual(list_exit, 0)
        self.assertEqual(list_text_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspect_json_exit, 0)
        self.assertEqual(list_stderr.getvalue(), "")
        self.assertEqual(list_text_stderr.getvalue(), "")
        self.assertEqual(inspect_stderr.getvalue(), "")
        self.assertEqual(inspect_json_stderr.getvalue(), "")
        self.assertEqual([run["run_id"] for run in list_payload], ["run-2", "run-1"])
        self.assertEqual(list_payload[0]["status"], "blocked")
        self.assertEqual(list_payload[0]["record_type"], "worker_report")
        self.assertEqual(list_payload[0]["lifecycle_state"], "reported")
        self.assertEqual(list_payload[1]["status"], "completed")
        self.assertEqual(list_payload[1]["record_type"], "run_result")
        self.assertEqual(list_payload[1]["lifecycle_state"], "finalized")
        self.assertEqual(list_payload[1]["log"], str(runs_dir / "run-1.log"))
        self.assertEqual(inspect_payload["lifecycle_state"], "finalized")
        self.assertIn(
            "started",
            inspect_payload["missing_lifecycle_transitions"],
        )
        self.assertIn(
            "session_observed",
            inspect_payload["missing_lifecycle_transitions"],
        )
        self.assertEqual(
            [
                transition["state"]
                for transition in inspect_payload["lifecycle_transitions"]
                if transition["observed"]
            ],
            ["scheduled", "reported", "classified", "finalized"],
        )
        self.assertEqual(
            list_text_stdout.getvalue(),
            "run-2\tTASK-02\tblocked\trecord=worker_report"
            "\tupdated=2026-05-09T00:02:00+00:00\texit=-\tlog=\n"
            "run-1\tTASK-01\tcompleted\trecord=run_result"
            "\tupdated=2026-05-09T00:01:00+00:00"
            f"\texit=0\tlog={runs_dir / 'run-1.log'}\n",
        )
        self.assertEqual(
            inspect_stdout.getvalue(),
            "run: run-1\n"
            "task: TASK-01\n"
            "status: completed\n"
            "record: run_result\n"
            "updated: 2026-05-09T00:01:00+00:00\n"
            "exit: 0\n"
            "session: native-1 (native:stdout)\n"
            f"log: {runs_dir / 'run-1.log'}\n"
            "message: -\n"
            "lifecycle: finalized\n"
            "missing_lifecycle: started, session_observed, workspace_claimed\n"
            "records: 4\n"
            'worker_report: {"commit": "abc123", "message": "", '
            '"metadata": {"source": "worker"}, '
            '"reported_at": "2026-05-09T00:00:00+00:00", '
            '"run_id": "run-1", "status": "completed", '
            '"task_id": "TASK-01"}\n'
            "record_history:\n"
            "- lock_acquired\tstatus=acquired"
            "\tupdated=2026-05-09T00:00:00+00:00\n"
            "- worker_report\tstatus=completed"
            "\tupdated=2026-05-09T00:00:00+00:00\n"
            "- run_state_transition\tstatus=classified"
            "\tupdated=2026-05-09T00:00:30+00:00\n"
            "- run_result\tstatus=completed"
            "\tupdated=2026-05-09T00:01:00+00:00\n",
        )

    def test_runs_inspect_returns_not_found_without_plan_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["runs", "inspect", "missing", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("run not found: missing", stderr.getvalue())


class AutopilotCliTests(unittest.TestCase):
    def test_status_text_reports_repo_queue_and_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "project"
            init_planning_repo(repo, THREE_TASK_PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["autopilot", "status", "--repo", str(repo)])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn(f"repo: {repo.name} ({repo})", output)
        self.assertIn("queue: 3 runnable / 3 total", output)
        self.assertIn("supervisor: idle", output)

    def test_status_json_emits_stable_project_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "project"
            init_planning_repo(repo, THREE_TASK_PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["autopilot", "status", "--repo", str(repo), "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        for key in (
            "repo",
            "display_name",
            "state_dir",
            "main_branch",
            "git",
            "queue",
            "supervisor",
            "blockers",
            "observations",
            "last_cycle",
            "next_wake",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["queue"]["total"], 3)
        self.assertEqual(payload["queue"]["runnable"], 3)
        self.assertEqual(payload["supervisor"]["state"], "idle")
        self.assertIsNone(payload["last_cycle"])

    def test_status_does_not_start_worker_or_record_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "project"
            init_planning_repo(repo, THREE_TASK_PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["autopilot", "status", "--repo", str(repo)])

            runs = repo / ".vibe-loop" / "runs.jsonl"
            locks = repo / ".vibe-loop" / "locks"

        self.assertEqual(exit_code, 0)
        self.assertFalse(runs.exists())
        self.assertFalse(any(locks.glob("*.lock")) if locks.exists() else False)

    def test_run_is_wired_but_does_not_launch_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "project"
            init_planning_repo(repo, THREE_TASK_PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["autopilot", "run", "--repo", str(repo)])

            runs = repo / ".vibe-loop" / "runs.jsonl"

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("AUTO-03", stderr.getvalue())
        self.assertIn("autopilot status", stderr.getvalue())
        self.assertFalse(runs.exists())

    def test_bare_autopilot_routes_to_run_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "project"
            init_planning_repo(repo, THREE_TASK_PLAN)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["autopilot", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("autopilot run is wired", stderr.getvalue())


def write_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    if sys.platform == "win32":
        cmd = path.with_name(path.name + ".cmd")
        cmd.write_text(
            f'@"{sys.executable}" "%~dp0{path.name}" %*\r\n', encoding="utf-8"
        )


def shell_command(*args: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(list(args))
    return shlex.join(args)


def init_planning_repo(repo: Path, plan_text: str) -> None:
    repo.mkdir()
    (repo / "PLAN.md").write_text(plan_text, encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch", "main"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    subprocess.run(["git", "add", "PLAN.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_active_run_lock(
    repo: Path,
    task_id: str,
    run_id: str,
    *,
    workspace: dict[str, object] | None = None,
) -> None:
    active_lock = repo / ".vibe-loop" / "locks" / f"{task_id}.lock"
    active_lock.mkdir(parents=True)
    metadata: dict[str, object] = {
        "record_type": "active_run",
        "schema_version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "pid": os.getpid(),
        "worker_pid": os.getpid(),
        "pid_source": "popen",
        "host": socket.gethostname(),
        "started_at": "2026-05-09T00:00:00+00:00",
    }
    if workspace is not None:
        metadata["workspace"] = workspace
    (active_lock / "lock.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )


def write_configure_agent(
    path: Path,
    payload: object,
    *,
    marker: str | None = None,
) -> None:
    if isinstance(payload, str):
        emit = f"print({payload!r})\n"
    else:
        emit = f"print(json.dumps({payload!r}))\n"
    marker_write = ""
    if marker is not None:
        marker_write = f"Path({marker!r}).write_text('ran', encoding='utf-8')\n"
    write_python_executable(
        path,
        "from pathlib import Path\n"
        "import json\n"
        "import sys\n"
        "if sys.argv[1] not in {'exec', '-p'}:\n"
        "    raise SystemExit(64)\n"
        "Path('configure-prompt.json').write_text(sys.argv[2], encoding='utf-8')\n"
        f"{marker_write}"
        f"{emit}",
    )


def generated_profile_payload(
    source_path: str,
    *,
    confidence: float = 0.86,
    kind: str = "markdown_table",
    include_title: bool = True,
    title_column: str = "Summary",
    empty_title_column: bool = False,
    profile_extra: dict[str, object] | None = None,
    field_extra: dict[str, object] | None = None,
    status_map_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": {"column": "Key"},
        "status": {"column": "State"},
    }
    if include_title:
        fields["title"] = {"column": "" if empty_title_column else title_column}
    for field_name, extra in (field_extra or {}).items():
        if not isinstance(extra, dict):
            fields[field_name] = extra
            continue
        mapping = fields.setdefault(field_name, {})
        assert isinstance(mapping, dict)
        mapping.update(extra)
    status_map: dict[str, object] = {
        "done": ["Done"],
        "runnable": ["Todo"],
        "blocked": ["Blocked"],
    }
    status_map.update(status_map_extra or {})
    profile = {
        "kind": kind,
        "source_paths": [source_path],
        "stable_ids": True,
        "fields": fields,
        "status_map": status_map,
    }
    profile.update(profile_extra or {})
    return {
        "status": "profile",
        "confidence": confidence,
        "profile": profile,
    }


def generated_profile_cache(source_path: str) -> dict[str, object]:
    payload = generated_profile_payload(source_path)
    return {
        "schema_version": 1,
        "prompt_version": 1,
        "status": "profile",
        "generated_at": "2026-05-08T00:00:00Z",
        "agent": {
            "name": "codex",
            "selection_command_source": "auto:codex",
        },
        "confidence": payload["confidence"],
        "provenance": {
            "repo": ".",
            "evidence_limit": {
                "max_file_bytes": 1,
                "max_total_bytes": 1,
                "max_files": 1,
                "max_skipped_entries": 1,
            },
            "evidence_file_count": 1,
            "skipped_evidence": [],
        },
        "source_fingerprints": [
            {
                "path": source_path,
                "size": 1,
                "sha256": "0" * 64,
                "mtime_ns": 0,
                "redacted": False,
            }
        ],
        "profile": payload["profile"],
        "degradation": None,
    }


def write_fake_git(bin_dir: Path) -> None:
    write_python_executable(
        bin_dir / "git",
        "import sys\n"
        "if sys.argv[1:] == ['rev-parse', '--verify', 'HEAD']:\n"
        "    print('test-head')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n",
    )


if __name__ == "__main__":
    unittest.main()
