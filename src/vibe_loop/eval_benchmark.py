from __future__ import annotations

import dataclasses
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


BENCHMARK_EVAL_SCHEMA_VERSION = 1


@dataclasses.dataclass(frozen=True)
class BenchmarkInstance:
    instance_id: str
    dataset: str
    split: str
    repo: str = ""
    language: str = ""
    image: str = ""
    image_digest: str = ""
    metadata: Mapping[str, object] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "dataset": self.dataset,
            "split": self.split,
            "repo": self.repo,
            "language": self.language,
            "image": self.image,
            "image_digest": self.image_digest,
            "metadata": dict(self.metadata),
        }


@dataclasses.dataclass(frozen=True)
class BenchmarkGraderResult:
    instance_id: str
    passed: bool
    grader: str
    exit_code: int
    duration_seconds: float
    log: str = ""
    failure_reason: str = ""
    metadata: Mapping[str, object] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "passed": self.passed,
            "grader": self.grader,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 6),
            "log": self.log,
            "failure_reason": self.failure_reason,
            "metadata": dict(self.metadata),
        }


@dataclasses.dataclass(frozen=True)
class BenchmarkTrialResult:
    instance: BenchmarkInstance
    condition: str
    trial: int
    grader_result: BenchmarkGraderResult
    agent_command: str
    started_at: str
    finished_at: str
    duration_seconds: float
    artifact_root: str = ""
    timeout: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "instance": self.instance.to_json(),
            "condition": self.condition,
            "trial": self.trial,
            "grader_result": self.grader_result.to_json(),
            "agent_command": self.agent_command,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "artifact_root": self.artifact_root,
            "timeout": self.timeout,
        }


class BenchmarkAdapter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def list_instances(self) -> Sequence[BenchmarkInstance]: ...

    def setup_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None: ...

    def grade_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> BenchmarkGraderResult: ...

    def teardown_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> None: ...


@dataclasses.dataclass(frozen=True)
class BenchmarkEvalConfig:
    adapter: BenchmarkAdapter
    output_root: Path
    agent_commands: Mapping[str, str]
    instances: Sequence[str] = ()
    conditions: Sequence[str] = ()
    trials: int = 1
    timeout_seconds: int = 600


def run_benchmark_eval(config: BenchmarkEvalConfig) -> dict[str, object]:
    instances = config.adapter.list_instances()
    if config.instances:
        allowed = set(config.instances)
        instances = [i for i in instances if i.instance_id in allowed]

    conditions = list(config.agent_commands.keys())
    if config.conditions:
        allowed_conditions = set(config.conditions)
        conditions = [c for c in conditions if c in allowed_conditions]

    results: list[dict[str, object]] = []
    condition_summaries: dict[str, dict[str, object]] = {}

    for condition in conditions:
        command = config.agent_commands[condition]
        condition_results: list[BenchmarkTrialResult] = []

        for instance in instances:
            for trial in range(1, config.trials + 1):
                trial_result = _run_trial(
                    config, instance, condition, command, trial
                )
                condition_results.append(trial_result)
                results.append(trial_result.to_json())

        passed = sum(1 for r in condition_results if r.grader_result.passed)
        condition_summaries[condition] = {
            "trials": len(condition_results),
            "passed": passed,
            "pass_rate": round(passed / len(condition_results), 4)
            if condition_results
            else 0.0,
            "agent_command": command,
        }

    return {
        "schema_version": BENCHMARK_EVAL_SCHEMA_VERSION,
        "adapter": config.adapter.name,
        "adapter_version": config.adapter.version,
        "generated_at": datetime.now(UTC).isoformat(),
        "instances_total": len(instances),
        "conditions": condition_summaries,
        "results": results,
    }


def _run_trial(
    config: BenchmarkEvalConfig,
    instance: BenchmarkInstance,
    condition: str,
    command: str,
    trial: int,
) -> BenchmarkTrialResult:
    trial_dir = (
        config.output_root
        / config.adapter.name
        / instance.instance_id
        / condition
        / f"trial-{trial}"
    )
    trial_dir.mkdir(parents=True, exist_ok=True)
    workdir = trial_dir / "workspace"
    workdir.mkdir(exist_ok=True)

    started_at = datetime.now(UTC).isoformat()
    start_time = time.monotonic()
    timeout = False

    try:
        config.adapter.setup_instance(instance, workdir)
    except Exception as exc:
        return _error_trial(
            instance, condition, trial, command, started_at, start_time,
            f"setup failed: {exc}",
        )

    try:
        agent_result = subprocess.run(
            command,
            cwd=workdir,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timeout = True
        agent_result = None
    except OSError as exc:
        return _error_trial(
            instance, condition, trial, command, started_at, start_time,
            f"agent command failed: {exc}",
        )

    try:
        grader_result = config.adapter.grade_instance(instance, workdir)
    except Exception as exc:
        grader_result = BenchmarkGraderResult(
            instance_id=instance.instance_id,
            passed=False,
            grader=f"{config.adapter.name}/error",
            exit_code=-1,
            duration_seconds=0.0,
            failure_reason=f"grader error: {exc}",
        )

    finished_at = datetime.now(UTC).isoformat()
    duration = time.monotonic() - start_time

    log_path = trial_dir / "agent.log"
    if agent_result is not None:
        log_path.write_text(
            f"stdout:\n{agent_result.stdout}\nstderr:\n{agent_result.stderr}\n",
            encoding="utf-8",
        )

    try:
        config.adapter.teardown_instance(instance, workdir)
    except Exception:
        pass

    return BenchmarkTrialResult(
        instance=instance,
        condition=condition,
        trial=trial,
        grader_result=grader_result,
        agent_command=command,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        artifact_root=str(trial_dir),
        timeout=timeout,
    )


def _error_trial(
    instance: BenchmarkInstance,
    condition: str,
    trial: int,
    command: str,
    started_at: str,
    start_time: float,
    error_message: str,
) -> BenchmarkTrialResult:
    return BenchmarkTrialResult(
        instance=instance,
        condition=condition,
        trial=trial,
        grader_result=BenchmarkGraderResult(
            instance_id=instance.instance_id,
            passed=False,
            grader="error",
            exit_code=-1,
            duration_seconds=0.0,
            failure_reason=error_message,
        ),
        agent_command=command,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        duration_seconds=time.monotonic() - start_time,
    )
