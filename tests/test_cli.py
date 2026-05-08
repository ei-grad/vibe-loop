from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vibe_loop.cli import main


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


class CliTests(unittest.TestCase):
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
                "print('session id: codex-native-123')\n"
                "print(f'codex out: {sys.argv[2]}')\n",
            )
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            agent_args = (repo / "agent-args.txt").read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertNotEqual(payload["session_id"], payload["run_id"])
        self.assertEqual(payload["session_id"], "codex-native-123")
        self.assertEqual(payload["session_id_source"], "native:stdout")
        self.assertEqual(payload["agent_command_source"], "auto:codex")
        self.assertEqual(payload["agent_selection_command_source"], "auto:codex")
        self.assertEqual(run_records[0]["session_id"], "codex-native-123")
        self.assertEqual(run_records[0]["session_id_source"], "native:stdout")
        self.assertEqual(agent_args, "exec\n$vibe-loop TASK-01")
        self.assertIn("agent command source: auto:codex", stderr.getvalue())
        self.assertIn("agent_command_source=auto:codex", log_text)
        self.assertIn("session_id_source=native:stdout", log_text)

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

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            agent_args = (repo / "agent-args.txt").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"], "completed")
        self.assertEqual(agent_args, "-p\n$vibe-loop TASK-01")
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

            payload = json.loads(stdout.getvalue())
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
        self.assertEqual(agent_args, "exec\n$vibe-loop TASK-01")
        self.assertIn("agent command source: auto:codex:codex-first", stderr.getvalue())
        self.assertIn(
            "agent selection command source: auto:codex:codex-first",
            stderr.getvalue(),
        )
        self.assertIn("agent default policy source: codex-first", stderr.getvalue())
        self.assertIn("agent default policy:", stderr.getvalue())
        self.assertIn("agent_command_source=auto:codex:codex-first", log_text)
        self.assertIn(
            "agent_selection_command_source=auto:codex:codex-first",
            log_text,
        )
        self.assertIn("agent_default_policy_source=codex-first", log_text)
        self.assertIn("agent_default_policy=", log_text)

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

            payload = json.loads(stdout.getvalue())

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
        self.assertEqual(payload["agent"]["command_source"], "auto:codex")
        self.assertEqual(payload["agent"]["selection_command_source"], "auto:codex")
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

    def test_tasks_configure_reports_agent_resolution_without_running_agent(
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
                    exit_code = main(
                        ["tasks", "configure", "--repo", str(repo), "--json"]
                    )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["status"], "not_implemented")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex")
        self.assertFalse((repo / "should-not-run").exists())

    def test_tasks_configure_reports_codex_first_policy_with_both_agents(
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
                "Path('codex-should-not-run').write_text('ran', encoding='utf-8')\n",
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

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex:codex-first")
        self.assertEqual(
            payload["agent"]["selection_command_source"],
            "auto:codex:codex-first",
        )
        self.assertEqual(payload["agent"]["default_policy_source"], "codex-first")
        self.assertIn("Codex", payload["agent"]["default_policy"])
        self.assertFalse((repo / "codex-should-not-run").exists())
        self.assertFalse((repo / "claude-should-not-run").exists())

    def test_tasks_configure_text_reports_detected_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_python_executable(bin_dir / "claude", "raise SystemExit(0)\n")
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["tasks", "configure", "--repo", str(repo)])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("detected agents: claude=", stdout.getvalue())
        self.assertIn("agent default policy source: codex-first", stdout.getvalue())
        self.assertIn("agent default policy:", stdout.getvalue())
        self.assertIn("agent.command source: auto:claude", stdout.getvalue())

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
        self.assertIn(f"[vibe-loop] session_id={payload['session_id']}", log_text)
        self.assertIn("[vibe-loop] session_id_source=fallback:run_id", log_text)
        self.assertIn("agent out", log_text)
        self.assertIn("agent err", log_text)

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
        self.assertEqual(run_records[0]["session_id"], "explicit-native-456")
        self.assertEqual(run_records[0]["session_id_source"], "native:stderr")

    def test_run_next_supports_configured_claude_prompt_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            bin_dir = repo / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                "if sys.argv[1] != '-p':\n"
                "    raise SystemExit(64)\n"
                "prompt = sys.argv[2]\n"
                "print(f'claude out: {prompt}')\n"
                "print(f'claude err: {prompt}', file=sys.stderr)\n"
                "plan = Path('docs/PLAN.md')\n"
                "text = plan.read_text(encoding='utf-8')\n"
                "plan.write_text(\n"
                "    text.replace('| TASK-01 | P0 | Next |', '| TASK-01 | P0 | Done |'),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\ncommand = \"claude -p '$vibe-loop {task_id}'\"\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["run-next", "--repo", str(repo)])
            finally:
                os.environ["PATH"] = original_path

            payload = json.loads(stdout.getvalue())
            log_text = Path(str(payload["log"])).read_text(encoding="utf-8")
            run_records = [
                json.loads(line)
                for line in (repo / ".vibe-loop" / "runs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["task_id"], "TASK-01")
        self.assertEqual(payload["classification"], "completed")
        self.assertIn("claude out: $vibe-loop TASK-01", stderr.getvalue())
        self.assertNotIn("claude err", stderr.getvalue())
        self.assertIn("claude out: $vibe-loop TASK-01", log_text)
        self.assertIn("claude err: $vibe-loop TASK-01", log_text)
        self.assertEqual(run_records[0]["task_id"], "TASK-01")
        self.assertEqual(run_records[0]["status"], "completed")

    def test_next_supports_configured_claude_prompt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(TWO_TASK_PLAN, encoding="utf-8")
            bin_dir = repo / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1] != '-p':\n"
                "    raise SystemExit(64)\n"
                "Path('selection-prompt.txt').write_text(sys.argv[2], encoding='utf-8')\n"
                "print(json.dumps({'task_id': 'TASK-02', 'reason': 'ready'}))\n",
                encoding="utf-8",
            )
            claude.chmod(0o755)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nselection_command = "claude -p {prompt}"\n',
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(["next", "--repo", str(repo), "--ask-agent"])
            finally:
                os.environ["PATH"] = original_path

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


def write_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


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
