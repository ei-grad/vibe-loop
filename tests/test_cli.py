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


WORK_TABLE = """# Work

| Key | State | Summary |
| --- | --- | --- |
| DISC-X | Todo | Configure generated discovery. |
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
        self.assertEqual(cache["agent"]["selection_command_source"], "auto:codex")
        self.assertEqual(payload["agent"]["command_source"], "auto:codex")
        self.assertNotIn("command", cache["agent"])
        self.assertEqual(prompt["evidence"]["files"][0]["path"], "WORK.md")

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
        self.assertIn("agent.command source: auto:claude", stdout.getvalue())
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
            state_dir = repo / ".vibe-loop"
            repo.mkdir()
            bin_dir.mkdir()
            state_dir.mkdir()
            (state_dir / "generated-task-source.json").write_text(
                json.dumps(generated_profile_cache("WORK.md")),
                encoding="utf-8",
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

        self.assertEqual(results["tasks-list"][0], 1)
        self.assertIn(
            "generated task-source cache status=profile", results["tasks-list"][2]
        )
        self.assertEqual(results["tasks-runnable"][0], 1)
        self.assertIn(
            "generated task-source cache status=profile",
            results["tasks-runnable"][2],
        )
        self.assertEqual(results["next"][0], 1)
        self.assertIn("generated task-source cache status=profile", results["next"][2])
        self.assertEqual(results["doctor"][0], 0)
        self.assertEqual(results["doctor"][2], "")
        self.assertEqual(doctor_payload["generated_task_profile"]["status"], "profile")
        self.assertFalse((repo / "should-not-run").exists())

    def test_read_only_task_success_reports_degraded_cache_without_running_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            state_dir = repo / ".vibe-loop"
            repo.mkdir()
            bin_dir.mkdir()
            state_dir.mkdir()
            (repo / "PLAN.md").write_text(PLAN, encoding="utf-8")
            cache = generated_profile_cache("WORK.md")
            cache["status"] = "planning_only"
            cache["degradation"] = {
                "reason": "low_confidence",
                "message": "agent was unsure",
                "next_action": "rerun tasks configure",
            }
            (state_dir / "generated-task-source.json").write_text(
                json.dumps(cache),
                encoding="utf-8",
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

        self.assertEqual(results["tasks-list"][0], 0)
        self.assertIn("TASK-01", results["tasks-list"][1])
        self.assertIn(
            "generated task-source cache status=planning_only",
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


def write_configure_agent(path: Path, payload: object) -> None:
    if isinstance(payload, str):
        emit = f"print({payload!r})\n"
    else:
        emit = f"print(json.dumps({payload!r}))\n"
    write_python_executable(
        path,
        "from pathlib import Path\n"
        "import json\n"
        "import sys\n"
        "if sys.argv[1] not in {'exec', '-p'}:\n"
        "    raise SystemExit(64)\n"
        "Path('configure-prompt.json').write_text(sys.argv[2], encoding='utf-8')\n"
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
