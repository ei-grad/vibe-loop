from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from vibe_loop.eval_benchmark import (
    BenchmarkEvalConfig,
    BenchmarkGraderResult,
    BenchmarkInstance,
    run_benchmark_eval,
)


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

    def setup_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None:
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

    def teardown_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None:
        self.teardown_calls.append(instance.instance_id)


class BenchmarkEvalTests(unittest.TestCase):
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
        adapter = StubAdapter(
            instances, {"test-001": True, "test-002": False}
        )
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

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["adapter"], "stub-benchmark")
        self.assertEqual(payload["adapter_version"], "1.0.0")
        self.assertEqual(payload["instances_total"], 2)
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
            BenchmarkInstance(
                instance_id="inc", dataset="d", split="s"
            ),
            BenchmarkInstance(
                instance_id="exc", dataset="d", split="s"
            ),
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
            BenchmarkInstance(
                instance_id="broken", dataset="d", split="s"
            ),
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

    def test_multiple_trials(self) -> None:
        instances = [
            BenchmarkInstance(
                instance_id="t1", dataset="d", split="s"
            ),
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


if __name__ == "__main__":
    unittest.main()
