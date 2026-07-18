from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from vibe_loop.cli import main
from vibe_loop.eval_benchmark import (
    BENCHMARK_STATUS_AGENT_FAILED,
    BENCHMARK_STATUS_INFRASTRUCTURE_FAILED,
    BenchmarkEvalConfig,
    BenchmarkGraderResult,
    BenchmarkInstance,
    run_benchmark_eval,
)
from vibe_loop.eval_benchmark_manifest import ManifestBenchmarkAdapter


class StubAdapter:
    def __init__(
        self,
        instances: list[BenchmarkInstance],
        grader_results: dict[str, bool] | None = None,
    ):
        self._instances = instances
        self._grader_results = grader_results or {}
        self.setup_calls: list[str] = []
        self.grade_calls: list[str] = []
        self.teardown_calls: list[str] = []

    @property
    def name(self) -> str:
        return "stub-benchmark"

    @property
    def version(self) -> str:
        return "1.0.0"

    def list_instances(self) -> Sequence[BenchmarkInstance]:
        return list(self._instances)

    def setup_instance(self, instance: BenchmarkInstance, workdir: Path) -> None:
        self.setup_calls.append(instance.instance_id)
        (workdir / "setup.txt").write_text("ready\n", encoding="utf-8")

    def grade_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> BenchmarkGraderResult:
        self.grade_calls.append(instance.instance_id)
        passed = self._grader_results.get(instance.instance_id, False)
        return BenchmarkGraderResult(
            instance_id=instance.instance_id,
            passed=passed,
            grader="stub-grader/v1",
            exit_code=0 if passed else 1,
            duration_seconds=0.01,
        )

    def teardown_instance(self, instance: BenchmarkInstance, workdir: Path) -> None:
        self.teardown_calls.append(instance.instance_id)


class BenchmarkEvalTests(unittest.TestCase):
    def test_pinned_swe_rebench_v2_manifest_preserves_selection_contract(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "benchmarks"
            / "swe-rebench-v2-smoke.json"
        )
        adapter = ManifestBenchmarkAdapter(manifest)
        instances = adapter.list_instances()

        self.assertEqual(adapter.version, "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0")
        self.assertEqual(len(instances), 24)
        self.assertEqual(
            Counter(instance.language for instance in instances),
            Counter(
                {
                    "go": 4,
                    "java": 4,
                    "js": 4,
                    "python": 4,
                    "rust": 4,
                    "ts": 4,
                }
            ),
        )
        self.assertTrue(adapter.metadata["non_leaderboard"])
        for instance in instances:
            self.assertTrue(instance.repo)
            self.assertTrue(instance.image.startswith("docker.io/swerebenchv2/"))
            self.assertTrue(instance.metadata["base_commit"])
            self.assertGreaterEqual(instance.metadata["fail_to_pass_count"], 1)
            confounders = instance.metadata["confounders"]
            self.assertEqual(confounders["code"], "A")
            self.assertEqual(confounders["intent_completeness"], "complete")
            self.assertEqual(confounders["test_alignment_issues"], [])
            self.assertFalse(any(confounders["detected_issues"].values()))

    def test_runs_paired_conditions_across_instances(self) -> None:
        instances = [
            BenchmarkInstance(
                instance_id="test-001",
                dataset="stub-bench",
                split="smoke",
                repo="stub/repo",
                language="python",
            ),
            BenchmarkInstance(
                instance_id="test-002",
                dataset="stub-bench",
                split="smoke",
                repo="stub/repo2",
                language="python",
            ),
        ]
        adapter = StubAdapter(instances, {"test-001": True, "test-002": False})
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={
                    "no_skill": "echo no-skill",
                    "with_skill": "echo with-skill",
                },
                trials=1,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["adapter"], "stub-benchmark")
        self.assertEqual(payload["adapter_version"], "1.0.0")
        self.assertEqual(payload["instances_total"], 2)
        self.assertEqual(payload["resource_budget"]["timeout_seconds"], 30)
        self.assertEqual(payload["resource_budget"]["trials"], 1)
        self.assertEqual(len(payload["results"]), 4)
        self.assertEqual(
            set(payload["conditions"]),
            {"no_skill", "with_skill"},
        )
        no_skill = payload["conditions"]["no_skill"]
        self.assertEqual(no_skill["trials"], 2)
        self.assertEqual(no_skill["passed"], 1)
        self.assertEqual(no_skill["pass_rate"], 0.5)
        self.assertEqual(adapter.setup_calls.count("test-001"), 2)
        self.assertEqual(adapter.teardown_calls.count("test-001"), 2)

    def test_filters_instances_and_conditions(self) -> None:
        instances = [
            BenchmarkInstance(instance_id="inc", dataset="d", split="s"),
            BenchmarkInstance(instance_id="exc", dataset="d", split="s"),
        ]
        adapter = StubAdapter(instances, {"inc": True})
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={
                    "no_skill": "echo a",
                    "with_skill": "echo b",
                },
                instances=("inc",),
                conditions=("no_skill",),
                trials=1,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        self.assertEqual(payload["instances_total"], 1)
        self.assertEqual(len(payload["results"]), 1)
        self.assertIn("no_skill", payload["conditions"])
        self.assertNotIn("with_skill", payload["conditions"])

    def test_rejects_unknown_selectors_and_invalid_resource_limits(self) -> None:
        adapter = StubAdapter(
            [BenchmarkInstance(instance_id="known", dataset="d", split="s")]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "adapter": adapter,
                "output_root": root,
                "agent_commands": {"known-condition": "true"},
            }
            invalid_configs = (
                BenchmarkEvalConfig(**base, instances=("unknown",)),
                BenchmarkEvalConfig(**base, conditions=("unknown",)),
                BenchmarkEvalConfig(**base, trials=0),
                BenchmarkEvalConfig(**base, timeout_seconds=0),
            )
            for config in invalid_configs:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    run_benchmark_eval(config)

    def test_selected_languages_match_selected_instances(self) -> None:
        instances = [
            BenchmarkInstance(
                instance_id="python-task",
                dataset="d",
                split="s",
                language="python",
            ),
            BenchmarkInstance(
                instance_id="rust-task",
                dataset="d",
                split="s",
                language="rust",
            ),
        ]

        class MetadataAdapter(StubAdapter):
            @property
            def metadata(self):
                return {"languages": ["python", "rust"]}

        adapter = MetadataAdapter(instances, {"python-task": True})
        with tempfile.TemporaryDirectory() as directory:
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=adapter,
                    output_root=Path(directory),
                    agent_commands={"test": "true"},
                    instances=("python-task",),
                )
            )

        self.assertEqual(payload["languages"], ["python"])
        self.assertEqual(payload["adapter_metadata"]["languages"], ["python"])

    def test_records_grader_provenance(self) -> None:
        instances = [
            BenchmarkInstance(
                instance_id="t1",
                dataset="bench",
                split="test",
                image="registry/img:v1",
                image_digest="sha256:abc",
            ),
        ]
        adapter = StubAdapter(instances, {"t1": True})
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={"baseline": "echo ok"},
                trials=1,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        result = payload["results"][0]
        self.assertEqual(result["instance"]["instance_id"], "t1")
        self.assertEqual(result["instance"]["dataset"], "bench")
        self.assertEqual(result["instance"]["image"], "registry/img:v1")
        self.assertEqual(result["instance"]["image_digest"], "sha256:abc")
        self.assertEqual(result["grader_result"]["grader"], "stub-grader/v1")
        self.assertTrue(result["grader_result"]["passed"])
        self.assertIn("started_at", result)
        self.assertIn("finished_at", result)
        self.assertIn("duration_seconds", result)

    def test_setup_failure_produces_error_result(self) -> None:
        instances = [
            BenchmarkInstance(instance_id="broken", dataset="d", split="s"),
        ]

        class FailingAdapter(StubAdapter):
            def setup_instance(self, instance, workdir):
                raise RuntimeError("setup exploded")

        adapter = FailingAdapter(instances)
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={"test": "echo x"},
                trials=1,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        result = payload["results"][0]
        self.assertFalse(result["grader_result"]["passed"])
        self.assertIn("setup failed", result["grader_result"]["failure_reason"])
        self.assertEqual(result["status"], BENCHMARK_STATUS_INFRASTRUCTURE_FAILED)
        self.assertEqual(result["failure_phase"], "setup")
        self.assertEqual(result["agent_status"], "not_run")
        self.assertEqual(payload["summary"]["infrastructure_failed"], 1)
        self.assertEqual(payload["conditions"]["test"]["agent_failures"], 0)
        self.assertEqual(payload["conditions"]["test"]["infrastructure_failures"], 1)

    def test_multiple_trials(self) -> None:
        instances = [
            BenchmarkInstance(instance_id="t1", dataset="d", split="s"),
        ]
        adapter = StubAdapter(instances, {"t1": True})
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={"cond": "echo ok"},
                trials=3,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        self.assertEqual(len(payload["results"]), 3)
        self.assertEqual(payload["conditions"]["cond"]["trials"], 3)
        self.assertEqual(payload["conditions"]["cond"]["passed"], 3)
        self.assertEqual(payload["conditions"]["cond"]["pass_rate"], 1.0)
        trials = [r["trial"] for r in payload["results"]]
        self.assertEqual(trials, [1, 2, 3])

    def test_instance_to_json_round_trip(self) -> None:
        instance = BenchmarkInstance(
            instance_id="i-1",
            dataset="swe-bench-pro",
            split="public",
            repo="python/cpython",
            language="python",
            image="swebench/cpython:latest",
            image_digest="sha256:deadbeef",
            metadata={"difficulty": "hard"},
        )
        payload = instance.to_json()
        self.assertEqual(payload["instance_id"], "i-1")
        self.assertEqual(payload["dataset"], "swe-bench-pro")
        self.assertEqual(payload["split"], "public")
        self.assertEqual(payload["repo"], "python/cpython")
        self.assertEqual(payload["language"], "python")
        self.assertEqual(payload["image"], "swebench/cpython:latest")
        self.assertEqual(payload["image_digest"], "sha256:deadbeef")
        self.assertEqual(payload["metadata"], {"difficulty": "hard"})

    def test_empty_instances_returns_empty_results(self) -> None:
        adapter = StubAdapter([])
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={"test": "echo x"},
                trials=1,
                timeout_seconds=30,
            )
            payload = run_benchmark_eval(config)

        self.assertEqual(payload["instances_total"], 0)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["conditions"]["test"]["trials"], 0)
        self.assertEqual(payload["conditions"]["test"]["pass_rate"], 0.0)

    def test_agent_timeout_produces_graded_result_with_timeout_flag(self) -> None:
        instances = [
            BenchmarkInstance(instance_id="slow", dataset="d", split="s"),
        ]
        adapter = StubAdapter(instances, {"slow": False})
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkEvalConfig(
                adapter=adapter,
                output_root=Path(directory),
                agent_commands={"test": "sleep 60"},
                trials=1,
                timeout_seconds=1,
            )
            payload = run_benchmark_eval(config)

        result = payload["results"][0]
        self.assertTrue(result["timeout"])
        self.assertIn("grader_result", result)
        self.assertFalse(result["grader_result"]["passed"])
        self.assertEqual(result["status"], BENCHMARK_STATUS_AGENT_FAILED)
        self.assertEqual(result["agent_status"], "timed_out")

    def test_agent_command_failure_is_separate_from_infrastructure_failure(
        self,
    ) -> None:
        instances = [BenchmarkInstance(instance_id="failed", dataset="d", split="s")]
        adapter = StubAdapter(instances, {"failed": True})
        with tempfile.TemporaryDirectory() as directory:
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=adapter,
                    output_root=Path(directory),
                    agent_commands={"test": "exit 9"},
                    trials=1,
                    timeout_seconds=30,
                )
            )

        result = payload["results"][0]
        self.assertTrue(result["grader_result"]["passed"])
        self.assertEqual(result["status"], BENCHMARK_STATUS_AGENT_FAILED)
        self.assertEqual(result["agent_status"], "failed")
        self.assertEqual(result["agent_exit_code"], 9)
        self.assertEqual(payload["summary"]["agent_failed"], 1)
        self.assertEqual(payload["summary"]["infrastructure_failed"], 0)

    def test_grader_infrastructure_failure_remains_distinct_from_agent_exit(
        self,
    ) -> None:
        class BrokenGraderAdapter(StubAdapter):
            def grade_instance(self, instance, workdir):
                raise RuntimeError("grader unavailable")

        instances = [BenchmarkInstance(instance_id="failed", dataset="d", split="s")]
        adapter = BrokenGraderAdapter(instances)
        with tempfile.TemporaryDirectory() as directory:
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=adapter,
                    output_root=Path(directory),
                    agent_commands={"test": "exit 9"},
                    trials=1,
                    timeout_seconds=30,
                )
            )

        result = payload["results"][0]
        self.assertEqual(result["status"], BENCHMARK_STATUS_INFRASTRUCTURE_FAILED)
        self.assertEqual(result["failure_phase"], "grader")
        self.assertEqual(result["agent_status"], "failed")
        self.assertEqual(payload["summary"]["agent_failed"], 0)
        self.assertEqual(payload["summary"]["infrastructure_failed"], 1)

    def test_manifest_defaults_metadata_and_instance_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "benchmark.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "environment-smoke",
                        "version": "pinned-revision",
                        "metadata": {
                            "benchmark": "SWE-rebench V2 smoke",
                            "dataset": "nebius/SWE-rebench-V2",
                            "dataset_revision": "abc123",
                            "split": "train",
                            "languages": ["python"],
                            "non_leaderboard": True,
                        },
                        "defaults": {
                            "timeout_seconds": 30,
                            "setup": {
                                "command": (
                                    'test "$VIBE_LOOP_BENCHMARK_INSTANCE_ID" = env-001 '
                                    "&& touch setup.txt"
                                )
                            },
                            "grader": {
                                "name": "environment-grader",
                                "provenance": "test fixture",
                                "command": (
                                    'test "$VIBE_LOOP_BENCHMARK_REPO" = example/project '
                                    "&& test -f setup.txt && test -f agent.txt"
                                ),
                            },
                        },
                        "instances": [
                            {
                                "instance_id": "env-001",
                                "dataset": "nebius/SWE-rebench-V2",
                                "split": "train",
                                "repo": "example/project",
                                "language": "python",
                                "image": "registry/example:pinned",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=ManifestBenchmarkAdapter(manifest),
                    output_root=root / "out",
                    agent_commands={
                        "smoke": (
                            'test "$VIBE_LOOP_BENCHMARK_LANGUAGE" = python '
                            "&& touch agent.txt"
                        )
                    },
                    trials=1,
                    timeout_seconds=30,
                )
            )

        self.assertEqual(payload["benchmark"], "SWE-rebench V2 smoke")
        self.assertEqual(payload["dataset_revision"], "abc123")
        self.assertEqual(payload["sample_size"], 1)
        self.assertTrue(payload["non_leaderboard"])
        self.assertEqual(payload["summary"]["passed"], 1)
        self.assertEqual(payload["results"][0]["status"], "passed")

    def test_manifest_infrastructure_exit_code_is_not_an_agent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "benchmark.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "infrastructure-exit",
                        "defaults": {
                            "grader": {
                                "command": "exit 2",
                                "infrastructure_exit_codes": [2],
                            }
                        },
                        "instances": [
                            {
                                "instance_id": "task-1",
                                "dataset": "fixture",
                                "split": "smoke",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=ManifestBenchmarkAdapter(manifest),
                    output_root=root / "out",
                    agent_commands={"test": "true"},
                )
            )

        self.assertEqual(payload["summary"]["infrastructure_failed"], 1)
        self.assertEqual(payload["summary"]["agent_failed"], 0)

    def test_manifest_adapter_runs_configured_smoke_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "expected.txt").write_text("ok\n", encoding="utf-8")
            manifest = root / "benchmark.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "swe-style-smoke",
                        "version": "2026-05-24",
                        "harness": {
                            "name": "manifest",
                            "version": "1",
                            "provenance": "local fixture",
                        },
                        "instances": [
                            {
                                "instance_id": "sample-001",
                                "dataset": "swe-style-public",
                                "split": "smoke",
                                "repo": "example/project",
                                "language": "python",
                                "image": "registry/example:latest",
                                "image_digest": "sha256:abc",
                                "setup": {
                                    "copy": [
                                        {"from": "fixture", "to": "fixture"},
                                    ],
                                },
                                "grader": {
                                    "name": "fixture-grader",
                                    "provenance": "local",
                                    "command": (
                                        "test -f generated.txt && "
                                        "test -f fixture/expected.txt"
                                    ),
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            adapter = ManifestBenchmarkAdapter(manifest)
            payload = run_benchmark_eval(
                BenchmarkEvalConfig(
                    adapter=adapter,
                    output_root=root / "out",
                    agent_commands={"smoke": "touch generated.txt"},
                    trials=1,
                    timeout_seconds=30,
                )
            )

        self.assertEqual(payload["adapter"], "swe-style-smoke")
        self.assertEqual(payload["adapter_version"], "2026-05-24")
        result = payload["results"][0]
        self.assertTrue(result["grader_result"]["passed"])
        self.assertEqual(result["instance"]["dataset"], "swe-style-public")
        self.assertEqual(result["instance"]["image_digest"], "sha256:abc")
        self.assertEqual(
            result["grader_result"]["metadata"]["harness"]["provenance"],
            "local fixture",
        )

    def test_cli_runs_manifest_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            manifest = root / "benchmark.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "manifest-cli-smoke",
                        "instances": [
                            {
                                "instance_id": "cli-001",
                                "dataset": "fixture",
                                "split": "smoke",
                                "grader": {
                                    "command": "test -f generated.txt",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "eval",
                        "benchmark",
                        "--repo",
                        str(repo),
                        "--output",
                        str(root / "out"),
                        "--adapter",
                        "manifest",
                        "--manifest",
                        str(manifest),
                        "--agent-command",
                        "smoke=touch generated.txt",
                        "--timeout",
                        "30",
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["adapter"], "manifest-cli-smoke")
        self.assertTrue(payload["results"][0]["grader_result"]["passed"])

    def test_cli_records_missing_swe_rebench_prerequisites_as_infrastructure(
        self,
    ) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "benchmarks"
            / "swe-rebench-v2-smoke.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "SWE_REBENCH_V2_HARNESS": "",
                        "SWE_REBENCH_V2_TASKS_JSON": "",
                    },
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "eval",
                        "benchmark",
                        "--repo",
                        str(root),
                        "--output",
                        str(root / "out"),
                        "--adapter",
                        "manifest",
                        "--manifest",
                        str(manifest),
                        "--instance",
                        "elastic__synthetics-316",
                        "--agent-command",
                        "smoke=true",
                    ]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["sample_size"], 1)
        self.assertTrue(payload["non_leaderboard"])
        self.assertEqual(payload["summary"]["infrastructure_failed"], 1)
        self.assertEqual(payload["summary"]["agent_failed"], 0)
        self.assertEqual(payload["results"][0]["failure_phase"], "setup")

    def test_cli_rejects_unknown_benchmark_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "benchmark.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "selector-smoke",
                        "instances": [
                            {
                                "instance_id": "known",
                                "dataset": "fixture",
                                "split": "smoke",
                                "grader": {"command": "true"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "eval",
                        "benchmark",
                        "--repo",
                        str(root),
                        "--output",
                        str(root / "out"),
                        "--adapter",
                        "manifest",
                        "--manifest",
                        str(manifest),
                        "--instance",
                        "unknown",
                        "--agent-command",
                        "smoke=true",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unknown benchmark instances: unknown", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
