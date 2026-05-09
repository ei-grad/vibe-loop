from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import shlex
import shutil
import subprocess
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vibe_loop.eval_examples import (
    EXAMPLE_SUITE_ID,
    EvalExampleCase,
    list_eval_example_cases,
    materialize_eval_example,
    run_eval_example_grader,
)
from vibe_loop.evals import (
    EVAL_CONDITIONS,
    EvalArtifactRef,
    EvalSourceFingerprint,
    SkillEvalRunRecord,
    is_secret_like_eval_path,
    path_diagnostics,
    sha256_file,
    validate_skill_eval_run_record,
)


HARNESS_NAME = "vibe-loop-eval"
HARNESS_VERSION = "0.1"
DEFAULT_AGENT_NAME = "configured-agent"
DEFAULT_MODEL_PROVIDER = "unknown"
DEFAULT_MODEL_ID = "unknown"
ROLE_PATHS = {
    "prompt": "prompt.txt",
    "run_log": "logs/run.log",
    "transcript": "transcript.jsonl",
    "diff": "diff.patch",
    "final_repo_state": "final-repo-state.json",
    "structured_result": "run-result.json",
    "grader_outputs": "grader-outputs.json",
    "workflow_events": "workflow-events.json",
    "git_state_before": "git-state-before.json",
    "git_state_after": "git-state-after.json",
    "test_results": "test-results.json",
    "review_evidence": "review-evidence.json",
    "lock_evidence": "lock-evidence.json",
    "report_evidence": "report-evidence.json",
    "generated_profile": "generated-profile.json",
    "budget_evidence": "budget-evidence.json",
    "negative_prompt_results": "negative-prompt-results.json",
    "command_results": "command-results.json",
}
UNSAFE_COMMAND_FRAGMENTS = (
    "git reset --hard",
    "git checkout --",
    "git clean -fd",
    "git clean -xdf",
    "rm -rf /",
    "pkill ",
)


@dataclasses.dataclass(frozen=True)
class LocalSkillEvalConfig:
    output_root: Path
    agent_commands: Mapping[str, str]
    default_agent_command: str | None = None
    cases: Sequence[str] = ()
    conditions: Sequence[str] = ()
    trials: int = 1
    transcript_graders: Sequence[str] = ()
    timeout_seconds: int | None = None
    max_commands: int | None = None
    max_output_bytes: int | None = None
    examples_root: Path | None = None
    overwrite: bool = False
    agent_name: str = DEFAULT_AGENT_NAME
    model_provider: str = DEFAULT_MODEL_PROVIDER
    model_id: str = DEFAULT_MODEL_ID
    reasoning_effort: str = ""


@dataclasses.dataclass(frozen=True)
class CommandExecution:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration_seconds: float
    timeout: bool = False
    output_truncated: bool = False
    unsafe_refused: bool = False

    @property
    def output_bytes(self) -> int:
        return len(self.stdout.encode("utf-8")) + len(self.stderr.encode("utf-8"))

    def to_json(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "timeout": self.timeout,
            "duration_seconds": round(self.duration_seconds, 6),
            "stdout_bytes": len(self.stdout.encode("utf-8")),
            "stderr_bytes": len(self.stderr.encode("utf-8")),
            "output_bytes": self.output_bytes,
            "output_truncated": self.output_truncated,
            "unsafe_refused": self.unsafe_refused,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclasses.dataclass(frozen=True)
class TranscriptGraderResult:
    id: str
    command: str
    exit_code: int
    passed: bool
    payload: Mapping[str, Any]
    stdout: str
    stderr: str
    failure_taxonomy: tuple[str, ...]
    workflow_events: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": "transcript",
            "command": self.command,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "payload": dict(self.payload),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "failure_taxonomy": list(self.failure_taxonomy),
            "workflow_events": list(self.workflow_events),
        }


@dataclasses.dataclass(frozen=True)
class AgentCommandBatch:
    execution: CommandExecution
    command_results: tuple[dict[str, object], ...]
    workflow_events: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class TrialResult:
    record: dict[str, object]
    artifact_root: Path
    repo: Path

    @property
    def passed(self) -> bool:
        scoring = self.record.get("scoring")
        return bool(isinstance(scoring, Mapping) and scoring.get("passed") is True)


def run_local_demo_eval(config: LocalSkillEvalConfig) -> dict[str, object]:
    if config.trials < 1:
        raise ValueError("eval --trials must be at least 1")
    cases = selected_cases(config)
    if not cases:
        raise ValueError("no eval cases selected")
    output_root = config.output_root.resolve()
    suite_root = output_root / EXAMPLE_SUITE_ID
    suite_root.mkdir(parents=True, exist_ok=True)

    trial_results: list[TrialResult] = []
    run_order = 0
    for case in cases:
        conditions = selected_conditions(case, config.conditions)
        for condition in conditions:
            command = command_for_condition(condition, config)
            for trial in range(1, config.trials + 1):
                run_order += 1
                trial_results.append(
                    run_trial(
                        case,
                        condition=condition,
                        trial=trial,
                        run_order=run_order,
                        command_template=command,
                        suite_root=suite_root,
                        config=config,
                    )
                )

    aggregate = build_aggregate(trial_results, output_root=suite_root)
    write_json(suite_root / "aggregate.json", aggregate)
    (suite_root / "aggregate.md").write_text(
        render_aggregate_markdown(aggregate),
        encoding="utf-8",
    )
    write_json(
        suite_root / "manifest.json",
        {
            "suite_id": EXAMPLE_SUITE_ID,
            "generated_at": utc_now(),
            "cases": [case.case_id for case in cases],
            "trials": config.trials,
            "conditions": sorted(
                {
                    str(result.record["condition"])
                    for result in trial_results
                    if "condition" in result.record
                }
            ),
        },
    )
    return aggregate


def selected_cases(config: LocalSkillEvalConfig) -> list[EvalExampleCase]:
    cases = list(list_eval_example_cases(config.examples_root))
    if not config.cases:
        return cases
    selected = set(config.cases)
    known = {case.case_id for case in cases}
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError("unknown eval case(s): " + ", ".join(unknown))
    return [case for case in cases if case.case_id in selected]


def selected_conditions(
    case: EvalExampleCase,
    requested: Sequence[str],
) -> tuple[str, ...]:
    declared = case.conditions
    if not requested:
        return declared
    unknown = sorted(set(requested) - set(declared))
    if unknown:
        raise ValueError(
            f"{case.case_id} does not declare condition(s): " + ", ".join(unknown)
        )
    return tuple(condition for condition in declared if condition in set(requested))


def command_for_condition(
    condition: str,
    config: LocalSkillEvalConfig,
) -> str:
    command = config.agent_commands.get(condition)
    if command:
        return command
    if config.default_agent_command:
        return config.default_agent_command
    raise ValueError(f"missing eval agent command for condition: {condition}")


def run_trial(
    case: EvalExampleCase,
    *,
    condition: str,
    trial: int,
    run_order: int,
    command_template: str,
    suite_root: Path,
    config: LocalSkillEvalConfig,
) -> TrialResult:
    trial_root = suite_root / "cases" / case.case_id / condition / f"trial-{trial}"
    if trial_root.exists():
        if not config.overwrite:
            raise FileExistsError(
                f"{trial_root} already exists; pass --overwrite to replace it"
            )
        shutil.rmtree(trial_root)
    trial_root.mkdir(parents=True)
    repo = trial_root / "repo"
    materialize_eval_example(
        case.case_id,
        repo,
        examples_root=config.examples_root,
        overwrite=False,
    )
    prompt_text = combined_prompt(repo, case)
    write_text_artifact(trial_root, "prompt", prompt_text)
    git_before = collect_git_state(repo)
    write_json_artifact(trial_root, "git_state_before", git_before)
    lock_before = collect_lock_state(repo, case.task_id)
    run_id = seeded_run_id(lock_before) or (
        f"{EXAMPLE_SUITE_ID}-{case.case_id}-{condition}-trial-{trial}"
    )

    budgets = trial_budget(case, config)
    agent_batch = execute_trial_agent_commands(
        case,
        condition=condition,
        trial=trial,
        run_id=run_id,
        command_template=command_template,
        repo=repo,
        trial_root=trial_root,
        prompt_text=prompt_text,
        budgets=budgets,
        examples_root=config.examples_root,
    )
    execution = agent_batch.execution
    command = execution.command
    write_run_log(trial_root, case, condition, run_id, execution)
    write_transcript_if_missing(trial_root, execution)
    git_after = collect_git_state(repo)
    write_json_artifact(trial_root, "git_state_after", git_after)
    write_text_artifact(trial_root, "diff", fixture_diff(repo, git_before))
    deterministic = deterministic_grader_output(repo)
    transcript_graders = run_transcript_graders(
        config.transcript_graders,
        repo=repo,
        artifact_root=trial_root,
        case=case,
        condition=condition,
        trial=trial,
        run_id=run_id,
        budgets=budgets,
    )
    if transcript_graders:
        workflow_events = workflow_events_for_trial(
            trial_root,
            execution,
            transcript_graders,
            allow_artifact_events=False,
        )
    else:
        workflow_events = list(agent_batch.workflow_events) or workflow_events_for_trial(
            trial_root,
            execution,
            transcript_graders,
            allow_artifact_events=True,
        )
    write_json_artifact(trial_root, "workflow_events", {"events": workflow_events})
    lock_after = collect_lock_state(repo, case.task_id)
    write_lock_evidence(trial_root, lock_before, lock_after)
    write_report_evidence(trial_root, latest_worker_report(repo, case.task_id))
    write_generated_profile_artifact(trial_root, repo)
    write_missing_case_role_artifacts(trial_root, case, deterministic, execution)
    command_results = list(agent_batch.command_results) + [
        {
            "command": result.command,
            "exit_code": result.exit_code,
            "type": "transcript_grader",
            "id": result.id,
        }
        for result in transcript_graders
    ]
    usage = usage_metrics(transcript_graders, trial_root, command_results)
    write_json_artifact(trial_root, "command_results", {"commands": command_results})

    initial_graders = [
        deterministic_grader_record(deterministic),
        *[result.to_json() for result in transcript_graders],
    ]
    initial_scoring = score_trial(
        case,
        condition,
        execution,
        deterministic,
        transcript_graders,
        artifact_result=None,
        schema_diagnostics=(),
        command_count=budgeted_command_count(
            command_results,
            transcript_graders,
            trial_root,
        ),
        max_commands=budgets["max_commands"],
    )
    structured_result = structured_trial_result(
        run_id,
        case,
        execution,
        initial_scoring,
        command_count=observed_command_count(
            command_results,
            transcript_graders,
            trial_root,
        ),
        usage=usage,
    )
    write_json_artifact(trial_root, "structured_result", structured_result)
    write_json_artifact(trial_root, "final_repo_state", git_after)
    write_json_artifact(trial_root, "grader_outputs", {"graders": initial_graders})
    record = build_run_record(
        case,
        condition=condition,
        trial=trial,
        run_order=run_order,
        run_id=run_id,
        command=command,
        prompt_text=prompt_text,
        budgets=budgets,
        config=config,
        execution=execution,
        artifacts=collect_artifacts(trial_root, case),
        final_repo_state=git_after,
        structured_result=structured_result,
        graders=initial_graders,
        scoring=initial_scoring,
        source_fingerprints=source_fingerprints(case, repo, condition),
    )
    write_json(trial_root / "run.json", record)

    artifact_result = artifact_grader_output(repo, trial_root)
    final_graders = [
        deterministic_grader_record(deterministic),
        *[result.to_json() for result in transcript_graders],
        artifact_grader_record(artifact_result),
    ]
    schema_diagnostics = validate_skill_eval_run_record(record, trial_root)
    final_scoring = score_trial(
        case,
        condition,
        execution,
        deterministic,
        transcript_graders,
        artifact_result=artifact_result,
        schema_diagnostics=schema_diagnostics,
        command_count=budgeted_command_count(
            command_results,
            transcript_graders,
            trial_root,
        ),
        max_commands=budgets["max_commands"],
    )
    structured_result = structured_trial_result(
        run_id,
        case,
        execution,
        final_scoring,
        command_count=observed_command_count(
            command_results,
            transcript_graders,
            trial_root,
        ),
        schema_diagnostics=schema_diagnostics,
        usage=usage,
    )
    write_json_artifact(trial_root, "structured_result", structured_result)
    write_json_artifact(trial_root, "grader_outputs", {"graders": final_graders})
    record = build_run_record(
        case,
        condition=condition,
        trial=trial,
        run_order=run_order,
        run_id=run_id,
        command=command,
        prompt_text=prompt_text,
        budgets=budgets,
        config=config,
        execution=execution,
        artifacts=collect_artifacts(trial_root, case),
        final_repo_state=git_after,
        structured_result=structured_result,
        graders=final_graders,
        scoring=final_scoring,
        source_fingerprints=source_fingerprints(case, repo, condition),
    )
    write_json(trial_root / "run.json", record)
    return TrialResult(record=record, artifact_root=trial_root, repo=repo)


def trial_budget(
    case: EvalExampleCase,
    config: LocalSkillEvalConfig,
) -> dict[str, int]:
    return {
        "timeout_seconds": int(
            config.timeout_seconds or case.budget["timeout_seconds"]
        ),
        "max_commands": int(config.max_commands or case.budget["max_commands"]),
        "max_output_bytes": int(
            config.max_output_bytes or case.budget["max_output_bytes"]
        ),
    }


def combined_prompt(repo: Path, case: EvalExampleCase) -> str:
    prompts = []
    for prompt_path in case.prompt_paths:
        prompts.append((repo / prompt_path).read_text(encoding="utf-8"))
    if len(prompts) == 1:
        return prompts[0]
    chunks = []
    for prompt_path, prompt in zip(case.prompt_paths, prompts, strict=True):
        chunks.append(f"### {prompt_path}\n{prompt}")
    return "\n\n".join(chunks)


def execute_trial_agent_commands(
    case: EvalExampleCase,
    *,
    condition: str,
    trial: int,
    run_id: str,
    command_template: str,
    repo: Path,
    trial_root: Path,
    prompt_text: str,
    budgets: Mapping[str, int],
    examples_root: Path | None,
) -> AgentCommandBatch:
    if is_negative_prompt_set(case):
        return execute_negative_prompt_set(
            case,
            condition=condition,
            trial=trial,
            run_id=run_id,
            command_template=command_template,
            trial_root=trial_root,
            budgets=budgets,
            examples_root=examples_root,
        )
    command = format_agent_command(
        command_template,
        prompt=prompt_text,
        prompt_path=case.prompt_paths[0] if case.prompt_paths else "",
        repo=repo,
        artifact_dir=trial_root,
        case_id=case.case_id,
        condition=condition,
        trial=trial,
        run_id=run_id,
        task_id=case.task_id or case.case_id,
    )
    execution = execute_agent_command(
        command,
        cwd=repo,
        artifact_root=trial_root,
        case=case,
        condition=condition,
        trial=trial,
        run_id=run_id,
        prompt_text=prompt_text,
        prompt_paths=case.prompt_paths,
        budgets=budgets,
    )
    return AgentCommandBatch(
        execution=execution,
        command_results=({"type": "agent", **execution.to_json()},),
    )


def is_negative_prompt_set(case: EvalExampleCase) -> bool:
    return "negative_prompt_results" in case.expected_artifact_roles and (
        len(case.prompt_paths) > 1
    )


def execute_negative_prompt_set(
    case: EvalExampleCase,
    *,
    condition: str,
    trial: int,
    run_id: str,
    command_template: str,
    trial_root: Path,
    budgets: Mapping[str, int],
    examples_root: Path | None,
) -> AgentCommandBatch:
    executions: list[CommandExecution] = []
    command_results: list[dict[str, object]] = []
    prompt_results: list[dict[str, object]] = []
    workflow_events: list[str] = []
    for prompt_path in case.prompt_paths:
        prompt_id = Path(prompt_path).stem
        prompt_root = trial_root / "prompt-runs" / prompt_id
        prompt_repo = prompt_root / "repo"
        prompt_artifact_root = prompt_root / "artifacts"
        prompt_artifact_root.mkdir(parents=True)
        materialize_eval_example(
            case.case_id,
            prompt_repo,
            examples_root=examples_root,
            overwrite=False,
        )
        prompt_text = (prompt_repo / prompt_path).read_text(encoding="utf-8")
        prompt_before = collect_git_state(prompt_repo)
        command = format_agent_command(
            command_template,
            prompt=prompt_text,
            prompt_path=prompt_path,
            repo=prompt_repo,
            artifact_dir=prompt_artifact_root,
            case_id=case.case_id,
            condition=condition,
            trial=trial,
            run_id=run_id,
            task_id=case.task_id or case.case_id,
        )
        execution = execute_agent_command(
            command,
            cwd=prompt_repo,
            artifact_root=prompt_artifact_root,
            case=case,
            condition=condition,
            trial=trial,
            run_id=run_id,
            prompt_text=prompt_text,
            prompt_paths=(prompt_path,),
            budgets=budgets,
        )
        write_run_log(prompt_artifact_root, case, condition, run_id, execution)
        write_transcript_if_missing(prompt_artifact_root, execution)
        prompt_command_count = command_count_from_transcript(
            artifact_path(prompt_artifact_root, "transcript")
        ) or 1
        prompt_usage = usage_metrics((), prompt_artifact_root, ())
        prompt_after = collect_git_state(prompt_repo)
        prompt_events = workflow_events_for_trial(
            prompt_artifact_root,
            execution,
            (),
            allow_artifact_events=True,
        )
        workflow_events.extend(prompt_events)
        executions.append(execution)
        command_results.append(
            {
                "type": "agent_prompt",
                "prompt_id": prompt_id,
                "prompt_path": prompt_path,
                "repo": str(prompt_repo),
                "artifact_root": str(prompt_artifact_root),
                "observed_command_count": prompt_command_count,
                "usage": prompt_usage,
                **execution.to_json(),
            }
        )
        prompt_results.append(
            {
                "id": prompt_id,
                "path": prompt_path,
                "skill_activated": "skill_activated" in prompt_events,
                "repository_changed": repository_changed(prompt_before, prompt_after),
                "response": execution.stdout + execution.stderr,
            }
        )
    write_json_artifact(
        trial_root,
        "negative_prompt_results",
        {"results": prompt_results},
    )
    return AgentCommandBatch(
        execution=summarize_executions(
            executions,
            command=f"negative prompt set ({len(executions)} commands)",
        ),
        command_results=tuple(command_results),
        workflow_events=tuple(unique_preserving_order(workflow_events)),
    )


def summarize_executions(
    executions: Sequence[CommandExecution],
    *,
    command: str,
) -> CommandExecution:
    if not executions:
        now = utc_now()
        return CommandExecution(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
        )
    exit_code = next(
        (execution.exit_code for execution in executions if execution.exit_code != 0),
        0,
    )
    return CommandExecution(
        command=command,
        exit_code=exit_code,
        stdout="\n".join(
            f"[prompt {index + 1} stdout]\n{execution.stdout}"
            for index, execution in enumerate(executions)
        ),
        stderr="\n".join(
            f"[prompt {index + 1} stderr]\n{execution.stderr}"
            for index, execution in enumerate(executions)
        ),
        started_at=executions[0].started_at,
        finished_at=executions[-1].finished_at,
        duration_seconds=sum(execution.duration_seconds for execution in executions),
        timeout=any(execution.timeout for execution in executions),
        output_truncated=any(execution.output_truncated for execution in executions),
        unsafe_refused=any(execution.unsafe_refused for execution in executions),
    )


def repository_changed(
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> bool:
    return before.get("head") != after.get("head") or after.get("dirty") is True


def format_agent_command(
    template: str,
    *,
    prompt: str,
    prompt_path: str,
    repo: Path,
    artifact_dir: Path,
    case_id: str,
    condition: str,
    trial: int,
    run_id: str,
    task_id: str,
) -> str:
    values = {
        "prompt": shlex.quote(prompt),
        "prompt_path": shlex.quote(prompt_path),
        "repo": shlex.quote(str(repo)),
        "artifact_dir": shlex.quote(str(artifact_dir)),
        "case_id": shlex.quote(case_id),
        "condition": shlex.quote(condition),
        "trial": str(trial),
        "run_id": shlex.quote(run_id),
        "task_id": shlex.quote(task_id),
    }
    return template.format(**values)


def execute_agent_command(
    command: str,
    *,
    cwd: Path,
    artifact_root: Path,
    case: EvalExampleCase,
    condition: str,
    trial: int,
    run_id: str,
    prompt_text: str,
    prompt_paths: Sequence[str],
    budgets: Mapping[str, int],
) -> CommandExecution:
    started_at = utc_now()
    start = time.monotonic()
    unsafe = unsafe_command_reason(command)
    if unsafe:
        finished_at = utc_now()
        return CommandExecution(
            command=command,
            exit_code=126,
            stdout="",
            stderr=f"refused unsafe command: {unsafe}\n",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=time.monotonic() - start,
            unsafe_refused=True,
        )

    env = os.environ.copy()
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "VIBE_LOOP_EVAL_RUN_ID": run_id,
            "VIBE_LOOP_EVAL_CASE_ID": case.case_id,
            "VIBE_LOOP_EVAL_CONDITION": condition,
            "VIBE_LOOP_EVAL_TRIAL": str(trial),
            "VIBE_LOOP_EVAL_REPO": str(cwd),
            "VIBE_LOOP_EVAL_ARTIFACT_DIR": str(artifact_root),
            "VIBE_LOOP_EVAL_PROMPT": prompt_text,
            "VIBE_LOOP_EVAL_PROMPT_PATH": prompt_paths[0] if prompt_paths else "",
            "VIBE_LOOP_EVAL_PROMPT_PATHS": json.dumps(list(prompt_paths)),
            "VIBE_LOOP_EVAL_TASK_ID": case.task_id or "",
            "VIBE_LOOP_EVAL_SKILLS_AVAILABLE": (
                "0" if condition == "no_skill" else "1"
            ),
            "VIBE_LOOP_EVAL_SKILL_ID": skill_id_for_condition(condition),
            "VIBE_LOOP_RUN_ID": run_id,
            "VIBE_LOOP_TASK_ID": case.task_id or "",
            "VIBE_LOOP_REPO": str(cwd),
        }
    )
    stdout, stderr, exit_code, timeout, truncated = run_process_with_budgets(
        command,
        cwd=cwd,
        env=env,
        timeout_seconds=budgets["timeout_seconds"],
        max_output_bytes=budgets["max_output_bytes"],
    )
    finished_at = utc_now()
    return CommandExecution(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=time.monotonic() - start,
        timeout=timeout,
        output_truncated=truncated,
    )


def run_process_with_budgets(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[str, str, int, bool, bool]:
    popen_kwargs: dict[str, object] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        **popen_kwargs,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    state = {"bytes": 0, "truncated": False}
    state_lock = threading.Lock()
    kill_requested = threading.Event()

    def request_kill() -> None:
        if kill_requested.is_set():
            return
        kill_requested.set()
        kill_process_group(process)

    stdout_thread = threading.Thread(
        target=read_pipe_limited,
        args=(
            process.stdout,
            stdout_chunks,
            state,
            state_lock,
            max_output_bytes,
            request_kill,
        ),
    )
    stderr_thread = threading.Thread(
        target=read_pipe_limited,
        args=(
            process.stderr,
            stderr_chunks,
            state,
            state_lock,
            max_output_bytes,
            request_kill,
        ),
    )
    stdout_thread.start()
    stderr_thread.start()
    timeout = False
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timeout = True
        request_kill()
        exit_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if timeout:
        stderr += "\n[vibe-loop-eval] command timed out\n"
    if state["truncated"]:
        stderr += "\n[vibe-loop-eval] output truncated by budget\n"
    return stdout, stderr, exit_code, timeout, bool(state["truncated"])


def read_pipe_limited(
    pipe,
    chunks: list[bytes],
    state: dict[str, object],
    state_lock: threading.Lock,
    max_output_bytes: int,
    request_kill,
) -> None:
    if pipe is None:
        return
    try:
        for chunk in iter(lambda: pipe.read(8192), b""):
            with state_lock:
                current = int(state["bytes"])
                remaining = max_output_bytes - current
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                state["bytes"] = current + len(chunk)
                if state["bytes"] > max_output_bytes:
                    state["truncated"] = True
                    request_kill()
    finally:
        pipe.close()


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if not hasattr(os, "killpg"):
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        process.kill()


def write_run_log(
    artifact_root: Path,
    case: EvalExampleCase,
    condition: str,
    run_id: str,
    execution: CommandExecution,
) -> None:
    content = (
        f"[vibe-loop-eval] run_id={run_id}\n"
        f"[vibe-loop-eval] suite_id={EXAMPLE_SUITE_ID}\n"
        f"[vibe-loop-eval] case_id={case.case_id}\n"
        f"[vibe-loop-eval] condition={condition}\n"
        f"[vibe-loop-eval] command={execution.command}\n"
        f"[vibe-loop-eval] exit_code={execution.exit_code}\n"
        f"[vibe-loop-eval] timeout={json.dumps(execution.timeout)}\n"
        "[vibe-loop-eval] stdout:\n"
        f"{execution.stdout}"
        "\n[vibe-loop-eval] stderr:\n"
        f"{execution.stderr}"
    )
    write_text_artifact(artifact_root, "run_log", content)


def write_transcript_if_missing(
    artifact_root: Path,
    execution: CommandExecution,
) -> None:
    path = artifact_path(artifact_root, "transcript")
    if path.exists():
        return
    records = []
    for stream_name, text in (("stdout", execution.stdout), ("stderr", execution.stderr)):
        for line in text.splitlines():
            records.append({"stream": stream_name, "text": line})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def deterministic_grader_output(repo: Path) -> dict[str, object]:
    result = run_eval_example_grader(repo)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "schema_version": 1,
            "grader": "local-demo-v1",
            "passed": False,
            "checks": [
                {
                    "id": "deterministic-grader-json",
                    "passed": False,
                    "message": "grader stdout was not JSON",
                }
            ],
        }
    payload["exit_code"] = result.exit_code
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def artifact_grader_output(repo: Path, artifact_root: Path) -> dict[str, object]:
    result = run_eval_example_grader(repo, artifact_root=artifact_root)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "schema_version": 1,
            "grader": "local-demo-v1-artifacts",
            "passed": False,
            "checks": [
                {
                    "id": "artifact-grader-json",
                    "passed": False,
                    "message": "artifact grader stdout was not JSON",
                }
            ],
        }
    payload["exit_code"] = result.exit_code
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def deterministic_grader_record(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": "local-demo-deterministic",
        "type": "deterministic",
        "passed": payload.get("passed") is True,
        "output": dict(payload),
    }


def artifact_grader_record(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": "local-demo-artifacts",
        "type": "deterministic_artifact",
        "passed": payload.get("passed") is True,
        "output": dict(payload),
    }


def run_transcript_graders(
    commands: Sequence[str],
    *,
    repo: Path,
    artifact_root: Path,
    case: EvalExampleCase,
    condition: str,
    trial: int,
    run_id: str,
    budgets: Mapping[str, int],
) -> list[TranscriptGraderResult]:
    results: list[TranscriptGraderResult] = []
    for index, command_template in enumerate(commands, start=1):
        command = format_agent_command(
            command_template,
            prompt=(artifact_root / ROLE_PATHS["prompt"]).read_text(encoding="utf-8"),
            prompt_path=case.prompt_paths[0] if case.prompt_paths else "",
            repo=repo,
            artifact_dir=artifact_root,
            case_id=case.case_id,
            condition=condition,
            trial=trial,
            run_id=run_id,
            task_id=case.task_id or case.case_id,
        )
        execution = execute_agent_command(
            command,
            cwd=repo,
            artifact_root=artifact_root,
            case=case,
            condition=condition,
            trial=trial,
            run_id=run_id,
            prompt_text="",
            prompt_paths=case.prompt_paths,
            budgets=budgets,
        )
        payload = parse_grader_payload(execution.stdout)
        grader_id = str(payload.get("id") or f"transcript-grader-{index}")
        passed = payload.get("passed") is True and execution.exit_code == 0
        failure_taxonomy = tuple(
            item
            for item in string_list(payload.get("failure_taxonomy"))
            if isinstance(item, str)
        )
        workflow_events = tuple(
            item for item in string_list(payload.get("workflow_events"))
        ) or tuple(item for item in string_list(payload.get("events")))
        results.append(
            TranscriptGraderResult(
                id=grader_id,
                command=command,
                exit_code=execution.exit_code,
                passed=passed,
                payload=payload,
                stdout=execution.stdout,
                stderr=execution.stderr,
                failure_taxonomy=failure_taxonomy,
                workflow_events=workflow_events,
            )
        )
    return results


def parse_grader_payload(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "passed": False,
            "failure_taxonomy": ["grader_error"],
            "message": "transcript grader stdout was not JSON",
        }
    if not isinstance(payload, dict):
        return {
            "passed": False,
            "failure_taxonomy": ["grader_error"],
            "message": "transcript grader stdout must be an object",
        }
    return payload


def usage_metrics(
    transcript_graders: Sequence[TranscriptGraderResult],
    artifact_root: Path,
    command_results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    usage: dict[str, object] = {}
    for path in (artifact_path(artifact_root, "structured_result"), artifact_root / "agent-result.json"):
        payload = load_json(path)
        if isinstance(payload, Mapping):
            merge_usage(usage, payload)
    for result in command_results:
        nested_usage = result.get("usage")
        if isinstance(nested_usage, Mapping):
            merge_usage(usage, {"usage": nested_usage})
    for result in transcript_graders:
        merge_usage(usage, result.payload)
    return usage


def merge_usage(target: dict[str, object], payload: Mapping[str, object]) -> None:
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        for key in ("tokens", "input_tokens", "output_tokens", "cost_usd"):
            add_numeric_usage(target, key, usage.get(key))
    metrics = payload.get("metrics")
    if isinstance(metrics, Mapping):
        for key in ("tokens", "input_tokens", "output_tokens", "cost_usd"):
            add_numeric_usage(target, key, metrics.get(key))
    for key in ("tokens", "input_tokens", "output_tokens", "cost_usd"):
        add_numeric_usage(target, key, payload.get(key))


def add_numeric_usage(
    target: dict[str, object],
    key: str,
    value: object,
) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return
    current = target.get(key, 0)
    if not isinstance(current, int | float) or isinstance(current, bool):
        current = 0
    target[key] = current + value


def workflow_events_for_trial(
    artifact_root: Path,
    execution: CommandExecution,
    transcript_graders: Sequence[TranscriptGraderResult],
    *,
    allow_artifact_events: bool,
) -> list[str]:
    existing = artifact_path(artifact_root, "workflow_events")
    if allow_artifact_events and existing.is_file():
        loaded = load_json(existing)
        raw_events = loaded.get("events") if isinstance(loaded, Mapping) else loaded
        events = normalize_events(raw_events)
        if events:
            return events
    events: list[str] = []
    for result in transcript_graders:
        events.extend(result.workflow_events)
    events.extend(events_from_text(execution.stdout))
    events.extend(events_from_text(execution.stderr))
    if execution.unsafe_refused or unsafe_command_reason(execution.stdout + execution.stderr):
        events.append("unsafe_git_command")
    return unique_preserving_order(events)


def events_from_text(text: str) -> list[str]:
    events = []
    for line in text.splitlines():
        marker = "vibe-loop-eval-event:"
        if marker in line:
            event = line.split(marker, 1)[1].strip()
            if event:
                events.append(event)
    return events


def normalize_events(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    events: list[str] = []
    for item in value:
        if isinstance(item, str):
            events.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("event"), str):
            events.append(item["event"])
    return events


def unique_preserving_order(values: Sequence[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def write_missing_case_role_artifacts(
    artifact_root: Path,
    case: EvalExampleCase,
    deterministic: Mapping[str, object],
    execution: CommandExecution,
) -> None:
    write_json_artifact(
        artifact_root,
        "test_results",
        {"deterministic": deterministic},
        overwrite=False,
    )
    write_json_artifact(artifact_root, "review_evidence", {}, overwrite=False)
    write_json_artifact(
        artifact_root,
        "budget_evidence",
        {
            "timeout": execution.timeout,
            "duration_seconds": round(execution.duration_seconds, 6),
            "output_bytes": execution.output_bytes,
        },
        overwrite=False,
    )
    if "negative_prompt_results" in case.expected_artifact_roles:
        write_json_artifact(
            artifact_root,
            "negative_prompt_results",
            default_negative_prompt_results(artifact_root, execution),
            overwrite=False,
        )
    for role in case.expected_artifact_roles:
        if role in ROLE_PATHS and not artifact_path(artifact_root, role).exists():
            write_json_artifact(artifact_root, role, {}, overwrite=False)


def default_negative_prompt_results(
    artifact_root: Path,
    execution: CommandExecution,
) -> dict[str, object]:
    response = execution.stdout + execution.stderr
    state = load_json(artifact_path(artifact_root, "git_state_after"))
    repo_changed = bool(isinstance(state, Mapping) and state.get("dirty") is True)
    spec = load_json(artifact_root / "repo" / "eval" / "expected-artifacts.json")
    prompts = spec.get("negative_prompts") if isinstance(spec, Mapping) else []
    events = workflow_events_for_trial(
        artifact_root,
        execution,
        (),
        allow_artifact_events=True,
    )
    return {
        "results": [
            {
                "id": str(prompt.get("id", "")),
                "path": str(prompt.get("path", "")),
                "skill_activated": "skill_activated" in events,
                "repository_changed": repo_changed,
                "response": response,
            }
            for prompt in prompts
            if isinstance(prompt, Mapping)
        ]
    }


def write_generated_profile_artifact(artifact_root: Path, repo: Path) -> None:
    source = repo / ".vibe-loop" / "generated-task-source.json"
    if source.is_file():
        artifact_path(artifact_root, "generated_profile").write_text(
            source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def score_trial(
    case: EvalExampleCase,
    condition: str,
    execution: CommandExecution,
    deterministic: Mapping[str, object],
    transcript_graders: Sequence[TranscriptGraderResult],
    *,
    artifact_result: Mapping[str, object] | None,
    schema_diagnostics: Sequence[str],
    command_count: int,
    max_commands: int,
) -> dict[str, object]:
    deterministic_passed = deterministic.get("passed") is True
    transcript_passed = all(result.passed for result in transcript_graders)
    artifact_passed = True if artifact_result is None else artifact_result.get("passed") is True
    failure_taxonomy = failure_taxonomy_for_trial(
        case,
        condition,
        execution,
        deterministic,
        transcript_graders,
        artifact_result=artifact_result,
        schema_diagnostics=schema_diagnostics,
        command_count=command_count,
        max_commands=max_commands,
    )
    passed = (
        execution.exit_code == 0
        and not execution.timeout
        and not execution.output_truncated
        and deterministic_passed
        and transcript_passed
        and artifact_passed
        and not schema_diagnostics
        and command_count <= max_commands
        and "unsafe_git" not in failure_taxonomy
    )
    workflow_score = 1.0
    if (
        not transcript_passed
        or not artifact_passed
        or schema_diagnostics
        or "workflow_contract" in failure_taxonomy
        or "unsafe_git" in failure_taxonomy
    ):
        workflow_score = 0.0
    trigger_score = trigger_score_for_case(case, condition, artifact_result)
    return {
        "passed": passed,
        "task_score": 1.0 if deterministic_passed else 0.0,
        "workflow_score": workflow_score,
        "trigger_score": trigger_score,
        "excluded_from_primary": "harness_error" in failure_taxonomy
        or "grader_error" in failure_taxonomy,
        "failure_taxonomy": sorted(failure_taxonomy),
    }


def observed_command_count(
    command_results: Sequence[Mapping[str, object]],
    transcript_graders: Sequence[TranscriptGraderResult],
    artifact_root: Path,
) -> int:
    prompt_counts = prompt_command_counts(command_results)
    counts = [sum(prompt_counts) if prompt_counts else len(command_results)]
    transcript_count = command_count_from_transcript(artifact_path(artifact_root, "transcript"))
    if transcript_count is not None:
        counts.append(transcript_count)
    for result in transcript_graders:
        payload_count = command_count_from_payload(result.payload)
        if payload_count is not None:
            counts.append(payload_count)
    return max(counts)


def budgeted_command_count(
    command_results: Sequence[Mapping[str, object]],
    transcript_graders: Sequence[TranscriptGraderResult],
    artifact_root: Path,
) -> int:
    prompt_counts = prompt_command_counts(command_results)
    if prompt_counts:
        grader_count = len(
            [result for result in command_results if result.get("type") == "transcript_grader"]
        )
        return max(prompt_counts) + grader_count
    return observed_command_count(command_results, transcript_graders, artifact_root)


def prompt_command_counts(
    command_results: Sequence[Mapping[str, object]],
) -> list[int]:
    return [
        int(result["observed_command_count"])
        for result in command_results
        if result.get("type") == "agent_prompt"
        and isinstance(result.get("observed_command_count"), int)
        and not isinstance(result.get("observed_command_count"), bool)
    ]


def command_count_from_payload(payload: Mapping[str, Any]) -> int | None:
    direct = payload.get("command_count")
    if isinstance(direct, int) and not isinstance(direct, bool) and direct >= 0:
        return direct
    metrics = payload.get("metrics")
    if isinstance(metrics, Mapping):
        value = metrics.get("command_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def command_count_from_transcript(path: Path) -> int | None:
    if not path.is_file():
        return None
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        event_type = payload.get("type") or payload.get("event")
        if event_type in {"command", "tool_call", "shell_command"}:
            count += 1
    return count if count else None


def trigger_score_for_case(
    case: EvalExampleCase,
    condition: str,
    artifact_result: Mapping[str, object] | None,
) -> float:
    if condition == "no_skill":
        return 1.0 if not case.positive else 0.0
    if artifact_result is None:
        return 1.0
    checks = artifact_result.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
        return 1.0
    for check in checks:
        if not isinstance(check, Mapping) or check.get("passed") is True:
            continue
        message = str(check.get("message", ""))
        if "skill_activated" in message or "trigger" in message:
            return 0.0
    return 1.0


def failure_taxonomy_for_trial(
    case: EvalExampleCase,
    condition: str,
    execution: CommandExecution,
    deterministic: Mapping[str, object],
    transcript_graders: Sequence[TranscriptGraderResult],
    *,
    artifact_result: Mapping[str, object] | None,
    schema_diagnostics: Sequence[str],
    command_count: int,
    max_commands: int,
) -> set[str]:
    labels: set[str] = set()
    if execution.timeout:
        labels.add("timeout")
    if execution.unsafe_refused or unsafe_command_reason(execution.stdout + execution.stderr):
        labels.add("unsafe_git")
    if execution.output_truncated:
        labels.add("workflow_contract")
    if execution.exit_code != 0 and not execution.timeout and not execution.unsafe_refused:
        labels.add("task_outcome")
    if deterministic.get("passed") is not True:
        labels.add("task_outcome")
    if command_count > max_commands:
        labels.add("workflow_contract")
    if schema_diagnostics:
        labels.add("harness_error")
    for result in transcript_graders:
        if not result.passed:
            labels.add("workflow_contract")
        labels.update(result.failure_taxonomy)
        if result.exit_code != 0:
            labels.add("grader_error")
    if artifact_result is not None and artifact_result.get("passed") is not True:
        labels.update(artifact_failure_labels(case, condition, artifact_result))
    return labels


def artifact_failure_labels(
    case: EvalExampleCase,
    condition: str,
    artifact_result: Mapping[str, object],
) -> set[str]:
    labels: set[str] = set()
    checks = artifact_result.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
        return {"grader_error"}
    for check in checks:
        if not isinstance(check, Mapping) or check.get("passed") is True:
            continue
        check_id = str(check.get("id", ""))
        message = str(check.get("message", ""))
        if check_id.startswith("artifact"):
            labels.add("workflow_contract")
        if "unsafe_git_command" in message:
            labels.add("unsafe_git")
        if condition != "no_skill" and case.positive and "skill_activated" in message:
            labels.add("trigger_false_negative")
        if condition != "no_skill" and not case.positive and "skill_activated" in message:
            labels.add("trigger_false_positive")
    return labels or {"task_outcome"}


def structured_trial_result(
    run_id: str,
    case: EvalExampleCase,
    execution: CommandExecution,
    scoring: Mapping[str, object],
    *,
    command_count: int,
    schema_diagnostics: Sequence[str] = (),
    usage: Mapping[str, object] | None = None,
) -> dict[str, object]:
    passed = scoring.get("passed") is True
    if execution.timeout:
        task_status = "timeout"
    elif passed:
        task_status = "completed"
    else:
        task_status = "failed"
    payload: dict[str, object] = {
        "run_id": run_id,
        "task_id": case.task_id or case.case_id,
        "exit_code": execution.exit_code,
        "timeout": execution.timeout,
        "task_status": task_status,
        "task_completed": passed,
        "workflow_contract_completed": scoring.get("workflow_score") == 1.0,
        "duration_seconds": round(execution.duration_seconds, 6),
        "latency_seconds": round(execution.duration_seconds, 6),
        "command_count": command_count,
        "output_bytes": execution.output_bytes,
        "schema_diagnostics": list(schema_diagnostics),
    }
    if usage:
        payload["usage"] = dict(usage)
    return payload


def build_run_record(
    case: EvalExampleCase,
    *,
    condition: str,
    trial: int,
    run_order: int,
    run_id: str,
    command: str,
    prompt_text: str,
    budgets: Mapping[str, int],
    config: LocalSkillEvalConfig,
    execution: CommandExecution,
    artifacts: Sequence[EvalArtifactRef],
    final_repo_state: Mapping[str, object],
    structured_result: Mapping[str, object],
    graders: Sequence[Mapping[str, object]],
    scoring: Mapping[str, object],
    source_fingerprints: Sequence[EvalSourceFingerprint],
) -> dict[str, object]:
    status = eval_status(scoring, execution)
    failure_taxonomy = tuple(string_list(scoring.get("failure_taxonomy")))
    model: dict[str, object] = {
        "provider": config.model_provider,
        "id": config.model_id,
    }
    if config.reasoning_effort:
        model["reasoning_effort"] = config.reasoning_effort
    record = SkillEvalRunRecord(
        suite_id=EXAMPLE_SUITE_ID,
        case_id=case.case_id,
        trial=trial,
        condition=condition,
        run_id=run_id,
        task={
            "id": case.task_id or case.case_id,
            "prompt_sha256": hash_text(prompt_text),
            "expected_skill": "vibe-loop",
            "should_trigger": case.positive,
        },
        skill_condition=skill_condition(condition, command),
        agent={
            "name": config.agent_name,
            "command_source": "cli",
        },
        model=model,
        harness={
            "name": HARNESS_NAME,
            "version": HARNESS_VERSION,
            "command": command,
        },
        budget={
            "timeout_seconds": budgets["timeout_seconds"],
            "max_commands": budgets["max_commands"],
            "max_output_bytes": budgets["max_output_bytes"],
        },
        source_fingerprints=source_fingerprints,
        artifacts=artifacts,
        final_repo_state=final_repo_state,
        structured_result=structured_result,
        graders=graders,
        scoring=scoring,
        reproducibility={
            "fixture_sha256": fixture_sha256(case.repo_path),
            "run_order": run_order,
            "fresh_workspace": True,
            "state_reused": False,
            "artifact_root": str(Path("cases") / case.case_id / condition / f"trial-{trial}"),
        },
        status=status,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        failure_taxonomy=failure_taxonomy,
    )
    return record.to_json()


def eval_status(scoring: Mapping[str, object], execution: CommandExecution) -> str:
    if execution.timeout:
        return "timeout"
    if scoring.get("passed") is True:
        return "passed"
    labels = set(string_list(scoring.get("failure_taxonomy")))
    if "harness_error" in labels or "grader_error" in labels:
        return "infrastructure_error"
    return "failed"


def collect_artifacts(
    artifact_root: Path,
    case: EvalExampleCase,
) -> list[EvalArtifactRef]:
    roles = list(dict.fromkeys([*case.expected_artifact_roles, "command_results"]))
    artifacts = []
    for role in roles:
        relative_path = ROLE_PATHS.get(role, f"{role}.json")
        path = artifact_root / relative_path
        if not path.exists():
            continue
        artifacts.append(
            EvalArtifactRef(
                role=role,
                path=relative_path,
                sha256=sha256_file(path),
                content_type=content_type_for_path(path),
            )
        )
    return artifacts


def content_type_for_path(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".jsonl":
        return "application/jsonl"
    if path.suffix in {".txt", ".log", ".patch"}:
        return "text/plain"
    return "application/octet-stream"


def source_fingerprints(
    case: EvalExampleCase,
    repo: Path,
    condition: str,
) -> list[EvalSourceFingerprint]:
    paths = [
        "eval/case.json",
        "eval/expected-artifacts.json",
        "eval/graders/grade.py",
        *case.prompt_paths,
    ]
    fingerprints = []
    for relative_path in dict.fromkeys(paths):
        if path_diagnostics("source fingerprint", relative_path):
            continue
        path = repo / relative_path
        if not path.is_file() or is_secret_like_eval_path(relative_path):
            continue
        stat = path.stat()
        fingerprints.append(
            EvalSourceFingerprint(
                path=relative_path,
                sha256=sha256_file(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    skill_path = skill_file_path(condition)
    if skill_path is not None and skill_path.is_file():
        stat = skill_path.stat()
        fingerprints.append(
            EvalSourceFingerprint(
                path=f"skills/{skill_path.parent.name}/{skill_path.name}",
                sha256=sha256_file(skill_path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return fingerprints


def skill_condition(condition: str, command: str) -> dict[str, object]:
    if condition == "no_skill":
        return {"id": condition, "skills_available": False}
    skill_path = skill_file_path(condition)
    skill_sha = sha256_file(skill_path) if skill_path and skill_path.is_file() else hash_text(command)
    payload: dict[str, object] = {
        "id": condition,
        "skills_available": True,
        "skill_id": skill_id_for_condition(condition),
        "skill_sha256": skill_sha,
    }
    if skill_path is not None:
        payload["skill_path"] = str(skill_path)
    return payload


def skill_id_for_condition(condition: str) -> str:
    if condition == "vibe_loop":
        return "vibe-loop"
    if condition == "infinite_vibe_loop":
        return "infinite-vibe-loop"
    return condition.replace("_", "-")


def skill_file_path(condition: str) -> Path | None:
    skill_id = skill_id_for_condition(condition)
    candidate = Path(__file__).resolve().parent / "skills" / skill_id / "SKILL.md"
    if candidate.is_file():
        return candidate
    return None


def collect_git_state(repo: Path) -> dict[str, object]:
    status = git_output(repo, "status", "--short")
    return {
        "head": git_output(repo, "rev-parse", "--verify", "HEAD"),
        "branch": git_output(repo, "branch", "--show-current") or "HEAD",
        "dirty": bool(status.strip()),
        "status_short": status.splitlines(),
        "branches": git_output(repo, "branch", "--format=%(refname:short)").splitlines(),
        "worktrees": git_output(repo, "worktree", "list", "--porcelain").splitlines(),
    }


def collect_lock_state(repo: Path, task_id: str | None) -> dict[str, object]:
    if not task_id:
        return {}
    lock_path = repo / ".vibe-loop" / "locks" / f"{task_id}.lock" / "lock.json"
    payload = load_json(lock_path)
    return dict(payload) if isinstance(payload, Mapping) else {}


def seeded_run_id(lock_state: Mapping[str, object]) -> str | None:
    run_id = lock_state.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def write_lock_evidence(
    artifact_root: Path,
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> None:
    existing = load_json(artifact_path(artifact_root, "lock_evidence"))
    evidence = dict(existing) if isinstance(existing, Mapping) else {}
    evidence.setdefault("before", dict(before))
    evidence.setdefault("after", dict(after))
    write_json_artifact(artifact_root, "lock_evidence", evidence)


def write_report_evidence(
    artifact_root: Path,
    latest: Mapping[str, object] | None,
) -> None:
    existing = load_json(artifact_path(artifact_root, "report_evidence"))
    evidence = dict(existing) if isinstance(existing, Mapping) else {}
    evidence.setdefault("latest", dict(latest) if latest is not None else None)
    write_json_artifact(artifact_root, "report_evidence", evidence)


def latest_worker_report(repo: Path, task_id: str | None) -> dict[str, object] | None:
    runs_path = repo / ".vibe-loop" / "runs.jsonl"
    if not runs_path.is_file():
        return None
    records = []
    for line in runs_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if task_id is not None and payload.get("task_id") != task_id:
            continue
        records.append(payload)
    return records[-1] if records else None


def fixture_diff(repo: Path, git_before: Mapping[str, object]) -> str:
    base = git_before.get("head")
    committed = ""
    if isinstance(base, str) and base:
        committed = git_output(repo, "diff", "--binary", base, "HEAD")
    worktree = git_output(repo, "diff", "--binary")
    staged = git_output(repo, "diff", "--binary", "--cached")
    chunks = [chunk for chunk in (committed, staged, worktree) if chunk]
    return "\n".join(chunks)


def git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def artifact_path(root: Path, role: str) -> Path:
    return root / ROLE_PATHS[role]


def write_text_artifact(
    root: Path,
    role: str,
    content: str,
    *,
    overwrite: bool = True,
) -> None:
    path = artifact_path(root, role)
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_artifact(
    root: Path,
    role: str,
    payload: object,
    *,
    overwrite: bool = True,
) -> None:
    path = artifact_path(root, role)
    if path.exists() and not overwrite:
        return
    write_json(path, payload)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def build_aggregate(
    trial_results: Sequence[TrialResult],
    *,
    output_root: Path,
) -> dict[str, object]:
    records = [result.record for result in trial_results]
    by_condition: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    by_case_condition: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        condition = str(record.get("condition", ""))
        case_id = str(record.get("case_id", ""))
        by_condition[condition].append(record)
        by_case_condition[(case_id, condition)].append(record)

    conditions = {
        condition: condition_summary(condition, condition_records)
        for condition, condition_records in sorted(by_condition.items())
    }
    baseline = conditions.get("no_skill", {}).get("pass_rate")
    if isinstance(baseline, int | float):
        for condition, summary in conditions.items():
            pass_rate = summary.get("pass_rate")
            if not isinstance(pass_rate, int | float):
                continue
            summary["absolute_uplift"] = round(pass_rate - baseline, 6)
            summary["normalized_gain"] = normalized_gain(pass_rate, baseline)
    flaky = flaky_case_conditions(by_case_condition)
    for condition, case_ids in flaky.items():
        if condition in conditions:
            taxonomy = conditions[condition].setdefault("failure_taxonomy", {})
            if isinstance(taxonomy, dict):
                taxonomy["flaky"] = len(case_ids)
            conditions[condition]["flaky_case_ids"] = case_ids

    return {
        "schema_version": 1,
        "suite_id": EXAMPLE_SUITE_ID,
        "generated_at": utc_now(),
        "artifact_root": str(output_root),
        "total_trials": len(records),
        "conditions": conditions,
        "cases": case_summaries(by_case_condition),
        "records": [
            {
                "case_id": record.get("case_id"),
                "condition": record.get("condition"),
                "trial": record.get("trial"),
                "run_id": record.get("run_id"),
                "status": record.get("status"),
                "artifact_root": record.get("reproducibility", {}).get("artifact_root")
                if isinstance(record.get("reproducibility"), Mapping)
                else None,
                "failure_taxonomy": record.get("failure_taxonomy", []),
            }
            for record in records
        ],
    }


def condition_summary(
    condition: str,
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    included = [
        record
        for record in records
        if not excluded_from_primary(record)
    ]
    pass_count = sum(1 for record in included if record_passed(record))
    primary_total = len(included)
    pass_rate = pass_count / primary_total if primary_total else 0.0
    lower, upper = wilson_interval(pass_count, primary_total)
    latencies = numeric_structured_values(records, "latency_seconds")
    command_counts = numeric_structured_values(records, "command_count")
    token_total = sum_optional_usage(records, "tokens")
    cost_total = sum_optional_usage(records, "cost_usd")
    return {
        "condition": condition,
        "trials": len(records),
        "primary_trials": primary_total,
        "pass_count": pass_count,
        "pass_rate": round(pass_rate, 6),
        "confidence_interval_95": {
            "method": "wilson",
            "lower": round(lower, 6),
            "upper": round(upper, 6),
        },
        "latency_seconds": mean_summary(latencies),
        "command_count": mean_summary(command_counts),
        "token_total": token_total,
        "cost_total": cost_total,
        "failure_taxonomy": dict(sorted(failure_counts(records).items())),
    }


def excluded_from_primary(record: Mapping[str, object]) -> bool:
    scoring = record.get("scoring")
    return bool(isinstance(scoring, Mapping) and scoring.get("excluded_from_primary"))


def record_passed(record: Mapping[str, object]) -> bool:
    scoring = record.get("scoring")
    return bool(isinstance(scoring, Mapping) and scoring.get("passed") is True)


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    phat = successes / total
    denominator = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denominator
    margin = z * ((phat * (1 - phat) + z * z / (4 * total)) / total) ** 0.5 / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def mean_summary(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {
        "mean": round(sum(values) / len(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def numeric_structured_values(
    records: Sequence[Mapping[str, object]],
    key: str,
) -> list[float]:
    values = []
    for record in records:
        structured = record.get("structured_result")
        if isinstance(structured, Mapping) and isinstance(
            structured.get(key), int | float
        ):
            values.append(float(structured[key]))
    return values


def sum_optional_usage(
    records: Sequence[Mapping[str, object]],
    key: str,
) -> float | None:
    values = []
    for record in records:
        structured = record.get("structured_result")
        usage = structured.get("usage") if isinstance(structured, Mapping) else None
        if isinstance(usage, Mapping) and isinstance(usage.get(key), int | float):
            values.append(float(usage[key]))
    if not values:
        return None
    return round(sum(values), 6)


def failure_counts(records: Sequence[Mapping[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        for label in string_list(record.get("failure_taxonomy")):
            counts[label] += 1
    return counts


def normalized_gain(pass_rate: float, baseline: float) -> float | None:
    if baseline >= 1.0:
        return 0.0 if pass_rate >= baseline else -1.0
    return round((pass_rate - baseline) / (1.0 - baseline), 6)


def flaky_case_conditions(
    by_case_condition: Mapping[tuple[str, str], Sequence[Mapping[str, object]]],
) -> dict[str, list[str]]:
    flaky: dict[str, list[str]] = defaultdict(list)
    for (case_id, condition), records in by_case_condition.items():
        outcomes = {record_passed(record) for record in records}
        if len(outcomes) > 1:
            flaky[condition].append(case_id)
    return {condition: sorted(case_ids) for condition, case_ids in flaky.items()}


def case_summaries(
    by_case_condition: Mapping[tuple[str, str], Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    cases: dict[str, dict[str, object]] = {}
    for (case_id, condition), records in sorted(by_case_condition.items()):
        case_summary = cases.setdefault(case_id, {})
        pass_count = sum(1 for record in records if record_passed(record))
        case_summary[condition] = {
            "trials": len(records),
            "pass_count": pass_count,
            "pass_rate": round(pass_count / len(records), 6) if records else 0.0,
            "failure_taxonomy": dict(sorted(failure_counts(records).items())),
        }
    return cases


def render_aggregate_markdown(aggregate: Mapping[str, object]) -> str:
    lines = [
        f"# {aggregate.get('suite_id')} aggregate",
        "",
        "| Condition | Trials | Primary | Pass rate | Uplift | Normalized gain | Failures |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    conditions = aggregate.get("conditions")
    if isinstance(conditions, Mapping):
        for condition, payload in conditions.items():
            if not isinstance(payload, Mapping):
                continue
            failures = payload.get("failure_taxonomy")
            failure_text = ""
            if isinstance(failures, Mapping) and failures:
                failure_text = ", ".join(
                    f"{key}={value}" for key, value in sorted(failures.items())
                )
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(condition),
                        str(payload.get("trials", 0)),
                        str(payload.get("primary_trials", 0)),
                        str(payload.get("pass_rate", 0)),
                        str(payload.get("absolute_uplift", "")),
                        str(payload.get("normalized_gain", "")),
                        failure_text,
                    ]
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def fixture_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(path).as_posix()
        if is_secret_like_eval_path(relative):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def unsafe_command_reason(command: str) -> str:
    normalized = " ".join(command.lower().split())
    for fragment in UNSAFE_COMMAND_FRAGMENTS:
        if fragment in normalized:
            return fragment
    return ""


def parse_agent_command_specs(
    specs: Sequence[str],
) -> tuple[dict[str, str], str | None]:
    commands: dict[str, str] = {}
    default: str | None = None
    for spec in specs:
        key, separator, value = spec.partition("=")
        if separator and (key in EVAL_CONDITIONS or key == "*"):
            if not value:
                raise ValueError(f"empty eval agent command for condition: {key}")
            if key == "*":
                default = value
            else:
                commands[key] = value
        else:
            default = spec
    return commands, default


def string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
