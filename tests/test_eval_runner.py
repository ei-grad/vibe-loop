from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main


class EvalRunnerCliTests(unittest.TestCase):
    def test_negative_case_passes_and_matches_golden_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_agent.py"
            write_negative_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
            )

        golden = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "eval"
                / "aggregate-negative-pass.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stable_aggregate(payload), golden)

    def test_positive_case_passes_with_stub_agent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "finite_agent.py"
            write_finite_agent(agent, pass_trial=True)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "finite-py-plan-table"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            run_log_exists = (trial_root / "logs" / "run.log").is_file()
            diff_exists = (trial_root / "diff.patch").is_file()

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["scoring"]["workflow_score"], 1.0)
        self.assertTrue(run_log_exists)
        self.assertTrue(diff_exists)

    def test_timeout_keeps_failed_trial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "sleep_agent.py"
            write_python_executable(
                agent,
                "import time\n"
                "time.sleep(2)\n",
            )

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--timeout-seconds",
                "1",
                "--agent-command",
                f"no_skill={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            run_log_exists = (trial_root / "logs" / "run.log").is_file()

        self.assertEqual(record["status"], "timeout")
        self.assertIn("timeout", record["failure_taxonomy"])
        self.assertTrue(run_log_exists)
        self.assertEqual(payload["records"][0]["status"], "timeout")

    def test_unsafe_command_is_refused_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                "no_skill=git reset --hard",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            log = (trial_root / "logs" / "run.log").read_text(encoding="utf-8")
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertIn("refused unsafe command", log)
        self.assertIn("unsafe_git", record["failure_taxonomy"])
        self.assertEqual(payload["conditions"]["no_skill"]["failure_taxonomy"]["unsafe_git"], 1)

    def test_output_budget_failure_remains_in_primary_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "chatty_agent.py"
            write_python_executable(agent, "print('x' * 200)\n")

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--max-output-bytes",
                "20",
                "--agent-command",
                f"no_skill={agent}",
            )
            condition = payload["conditions"]["no_skill"]

        self.assertEqual(condition["primary_trials"], 1)
        self.assertEqual(condition["pass_rate"], 0.0)
        self.assertEqual(condition["failure_taxonomy"]["workflow_contract"], 1)
        self.assertNotIn("harness_error", condition["failure_taxonomy"])

    def test_negative_prompt_metrics_sum_per_prompt_usage_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_metrics_agent.py"
            write_negative_metrics_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
            )
            condition = payload["conditions"]["no_skill"]

        self.assertEqual(condition["command_count"]["mean"], 16.0)
        self.assertEqual(condition["token_total"], 24.0)
        self.assertEqual(condition["cost_total"], 0.8)

    def test_missing_worker_report_is_a_workflow_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "missing_report_agent.py"
            write_worker_agent_without_report(agent)

            payload = run_eval(
                root,
                "--case",
                "supervised-worker-report",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "supervised-worker-report"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            graders = json.loads(
                (trial_root / "grader-outputs.json").read_text(encoding="utf-8")
            )

        self.assertIn("workflow_contract", record["failure_taxonomy"])
        self.assertIn("task_outcome", record["failure_taxonomy"])
        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 0.0)
        self.assertIn("report_evidence.latest.run_id", json.dumps(graders))

    def test_seeded_worker_report_run_id_can_pass_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "worker_report_agent.py"
            write_worker_agent_with_report(agent)

            payload = run_eval(
                root,
                "--case",
                "supervised-worker-report",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "supervised-worker-report"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["run_id"], "eval-run-wrk-01")
        self.assertEqual(record["structured_result"]["run_id"], "eval-run-wrk-01")

    def test_main_integration_lock_evidence_from_agent_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "main_integration_agent.py"
            write_main_integration_agent(agent)

            payload = run_eval(
                root,
                "--case",
                "main-integration-lock",
                "--condition",
                "vibe_loop",
                "--agent-command",
                f"vibe_loop={agent}",
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "main-integration-lock"
                / "vibe_loop"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            lock_evidence = json.loads(
                (trial_root / "lock-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["conditions"]["vibe_loop"]["pass_rate"], 1.0)
        self.assertEqual(record["run_id"], "eval-run-mil-01")
        self.assertEqual(lock_evidence["acquire"]["run_id"], "eval-run-mil-01")
        self.assertFalse(lock_evidence["final_status"]["locked"])

    def test_transcript_grader_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "negative_agent.py"
            grader = root / "transcript_grader.py"
            write_negative_agent(agent)
            write_python_executable(
                grader,
                "import json\n"
                "print(json.dumps({\n"
                "    'id': 'unsafe-scan',\n"
                "    'passed': False,\n"
                "    'failure_taxonomy': ['unsafe_git'],\n"
                "    'workflow_events': ['unsafe_git_command'],\n"
                "    'metrics': {'tokens': 42, 'cost_usd': 0.25},\n"
                "}))\n",
            )

            payload = run_eval(
                root,
                "--case",
                "negative-trigger-set",
                "--condition",
                "no_skill",
                "--agent-command",
                f"no_skill={agent}",
                "--transcript-grader",
                str(grader),
            )
            trial_root = (
                root
                / "eval-runs"
                / "local-demo-v1"
                / "cases"
                / "negative-trigger-set"
                / "no_skill"
                / "trial-1"
            )
            record = json.loads((trial_root / "run.json").read_text(encoding="utf-8"))
            graders = json.loads(
                (trial_root / "grader-outputs.json").read_text(encoding="utf-8")
            )

        self.assertIn("unsafe_git", record["failure_taxonomy"])
        self.assertEqual(payload["conditions"]["no_skill"]["failure_taxonomy"]["unsafe_git"], 1)
        self.assertEqual(payload["conditions"]["no_skill"]["token_total"], 42.0)
        self.assertEqual(payload["conditions"]["no_skill"]["cost_total"], 0.25)
        self.assertIn("unsafe-scan", json.dumps(graders))

    def test_flaky_trials_are_summarized_by_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "flaky_agent.py"
            write_finite_agent(agent, pass_trial=False)

            payload = run_eval(
                root,
                "--case",
                "finite-py-plan-table",
                "--condition",
                "vibe_loop",
                "--trials",
                "2",
                "--agent-command",
                f"vibe_loop={agent}",
            )

        condition = payload["conditions"]["vibe_loop"]
        self.assertEqual(condition["pass_count"], 1)
        self.assertEqual(condition["pass_rate"], 0.5)
        self.assertEqual(condition["flaky_case_ids"], ["finite-py-plan-table"])
        self.assertEqual(condition["failure_taxonomy"]["flaky"], 1)


def run_eval(root: Path, *args: str) -> dict[str, object]:
    stdout = StringIO()
    stderr = StringIO()
    output = root / "eval-runs"
    argv = [
        "eval",
        "local-demo",
        "--output",
        str(output),
        "--json",
        *args,
    ]
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    if exit_code != 0:
        raise AssertionError(stderr.getvalue() + stdout.getvalue())
    return json.loads(stdout.getvalue())


def stable_aggregate(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": payload["schema_version"],
        "suite_id": payload["suite_id"],
        "total_trials": payload["total_trials"],
        "conditions": {
            condition: {
                key: value
                for key, value in condition_payload.items()
                if key
                in {
                    "trials",
                    "primary_trials",
                    "pass_count",
                    "pass_rate",
                    "confidence_interval_95",
                    "absolute_uplift",
                    "normalized_gain",
                    "failure_taxonomy",
                }
            }
            for condition, condition_payload in payload["conditions"].items()
        },
        "cases": payload["cases"],
        "records": [
            {
                "case_id": record["case_id"],
                "condition": record["condition"],
                "trial": record["trial"],
                "status": record["status"],
                "failure_taxonomy": record["failure_taxonomy"],
            }
            for record in payload["records"]
        ],
    }


def write_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def write_negative_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "prompt_path = os.environ['VIBE_LOOP_EVAL_PROMPT_PATH']\n"
        "if prompt_path.endswith('neg-small-edit-no-skill.txt'):\n"
        "    readme = repo / 'README.md'\n"
        "    readme.write_text(\n"
        "        readme.read_text(encoding='utf-8').replace('teh', 'the'),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "print('## main python workflow-contract task outcome def add multiple space the')\n",
    )


def write_negative_metrics_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "prompt_path = os.environ['VIBE_LOOP_EVAL_PROMPT_PATH']\n"
        "if prompt_path.endswith('neg-small-edit-no-skill.txt'):\n"
        "    readme = repo / 'README.md'\n"
        "    readme.write_text(\n"
        "        readme.read_text(encoding='utf-8').replace('teh', 'the'),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "transcript = '\\n'.join([\n"
        "    json.dumps({'type': 'tool_call', 'name': 'one'}),\n"
        "    json.dumps({'type': 'tool_call', 'name': 'two'}),\n"
        "]) + '\\n'\n"
        "(artifact / 'transcript.jsonl').write_text(transcript, encoding='utf-8')\n"
        "(artifact / 'agent-result.json').write_text(\n"
        "    json.dumps({'usage': {'tokens': 3, 'cost_usd': 0.1}}) + '\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print('## main python workflow-contract task outcome def add multiple space the')\n",
    )


def write_finite_agent(path: Path, *, pass_trial: bool) -> None:
    guard = "os.environ['VIBE_LOOP_EVAL_TRIAL'] == '1'" if not pass_trial else "True"
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        f"if {guard}:\n"
        "    (repo / 'src' / 'finite_math' / 'calculator.py').write_text(\n"
        "        'from __future__ import annotations\\n\\n\\n'\n"
        "        'def loyalty_total(subtotal: int, *, member: bool) -> int:\\n'\n"
        "        '    discount = 10 if member else 0\\n'\n"
        "        '    return subtotal - discount\\n',\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "    plan = repo / 'PLAN.md'\n"
        "    plan.write_text(\n"
        "        plan.read_text(encoding='utf-8').replace(\n"
        "            '| FPY-01 | P0 | Planned |', '| FPY-01 | P0 | Done |'\n"
        "        ),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "    events = [\n"
        "        'skill_activated',\n"
        "        'instructions_inspected',\n"
        "        'worktree_state_inspected',\n"
        "        'branch_or_worktree_created',\n"
        "        'verification_ran',\n"
        "        'review_requested',\n"
        "        'commit_created',\n"
        "        'main_fast_forwarded',\n"
        "        'main_verification_ran',\n"
        "    ]\n"
        "    (artifact / 'workflow-events.json').write_text(\n"
        "        json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        "    )\n"
        "print('finite agent finished')\n",
    )


def write_worker_agent_without_report(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'worker_demo' / 'reports.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def count_lines(value: str) -> int:\\n'\n"
        "    '    return sum(1 for line in value.splitlines() if line.strip())\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| WRK-01 | P0 | Planned |', '| WRK-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'worker_report_emitted',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('worker report intentionally omitted')\n",
    )


def write_worker_agent_with_report(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'worker_demo' / 'reports.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def count_lines(value: str) -> int:\\n'\n"
        "    '    return sum(1 for line in value.splitlines() if line.strip())\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| WRK-01 | P0 | Planned |', '| WRK-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "runs = repo / '.vibe-loop' / 'runs.jsonl'\n"
        "runs.parent.mkdir(parents=True, exist_ok=True)\n"
        "runs.write_text(json.dumps({\n"
        "    'schema_version': 1,\n"
        "    'record_type': 'worker_report',\n"
        "    'run_id': 'eval-run-wrk-01',\n"
        "    'task_id': 'WRK-01',\n"
        "    'status': 'completed',\n"
        "    'commit': 'HEAD',\n"
        "    'message': 'completed',\n"
        "    'metadata': {},\n"
        "    'reported_at': '2026-05-09T00:00:00+00:00',\n"
        "}) + '\\n', encoding='utf-8')\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'worker_report_emitted',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "print('worker report emitted')\n",
    )


def write_main_integration_agent(path: Path) -> None:
    write_python_executable(
        path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(os.environ['VIBE_LOOP_EVAL_REPO'])\n"
        "artifact = Path(os.environ['VIBE_LOOP_EVAL_ARTIFACT_DIR'])\n"
        "(repo / 'src' / 'mil_demo' / 'progress.py').write_text(\n"
        "    'from __future__ import annotations\\n\\n\\n'\n"
        "    'def clamp_percent(value: int) -> int:\\n'\n"
        "    '    return max(0, min(100, value))\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "plan = repo / 'PLAN.md'\n"
        "plan.write_text(\n"
        "    plan.read_text(encoding='utf-8').replace(\n"
        "        '| MIL-01 | P0 | Planned |', '| MIL-01 | P0 | Done |'\n"
        "    ),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "events = [\n"
        "    'skill_activated',\n"
        "    'verification_ran',\n"
        "    'review_requested',\n"
        "    'commit_created',\n"
        "    'main_integration_lock_acquired',\n"
        "    'main_fast_forwarded',\n"
        "    'main_verification_ran',\n"
        "    'main_integration_lock_released',\n"
        "]\n"
        "(artifact / 'workflow-events.json').write_text(\n"
        "    json.dumps({'events': events}) + '\\n', encoding='utf-8'\n"
        ")\n"
        "(artifact / 'lock-evidence.json').write_text(json.dumps({\n"
        "    'acquire': {\n"
        "        'owner_task_id': 'MIL-01',\n"
        "        'run_id': 'eval-run-mil-01',\n"
        "        'pid_source': 'active_task_lock:worker_pid',\n"
        "    },\n"
        "    'release': {'released': True},\n"
        "    'final_status': {'locked': False},\n"
        "}) + '\\n', encoding='utf-8')\n"
        "print('main integration lock evidence emitted')\n",
    )


if __name__ == "__main__":
    unittest.main()
