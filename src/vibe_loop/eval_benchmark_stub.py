from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from vibe_loop.eval_benchmark import BenchmarkGraderResult, BenchmarkInstance


class StubBenchmarkAdapter:
    @property
    def name(self) -> str:
        return "stub"

    @property
    def version(self) -> str:
        return "1.0.0"

    def list_instances(self) -> Sequence[BenchmarkInstance]:
        return [
            BenchmarkInstance(
                instance_id="stub-pass",
                dataset="stub-bench",
                split="smoke",
                repo="stub/example",
                language="python",
            ),
            BenchmarkInstance(
                instance_id="stub-fail",
                dataset="stub-bench",
                split="smoke",
                repo="stub/example",
                language="python",
            ),
        ]

    def setup_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None:
        (workdir / "task.txt").write_text(
            f"Implement feature for {instance.instance_id}\n",
            encoding="utf-8",
        )

    def grade_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> BenchmarkGraderResult:
        passed = instance.instance_id == "stub-pass"
        return BenchmarkGraderResult(
            instance_id=instance.instance_id,
            passed=passed,
            grader="stub/v1",
            exit_code=0 if passed else 1,
            duration_seconds=0.01,
            failure_reason="" if passed else "stub failure",
        )

    def teardown_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None:
        pass
