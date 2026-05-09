from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any

import sys

from vibe_loop.config import VibeConfig, planning_analytics_output_report, prepare_shell_command
from vibe_loop.generated_discovery import (
    SkippedEvidence,
    is_secret_like_directory_name,
    is_secret_like_path,
    is_webhook_like_evidence_path,
    redact_manifest_text,
)
from vibe_loop.generated_profiles import resolve_runtime_task_source
from vibe_loop.runs import RUN_RECORD_TYPE, WORKER_REPORT_RECORD_TYPE, RunStore
from vibe_loop.tasks import DONE_STATUS, Task, build_task_source


PLANNING_EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_GIT_COMMIT_LIMIT = 500
WORKLOG_COMMAND_TIMEOUT_SECONDS = 300
MAX_WORKLOG_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_WORKLOG_RECORDS = 10_000
GIT_COMMAND_TIMEOUT_SECONDS = 30
EXPLICIT_COMMIT_REF_RE = re.compile(
    r"(?i)\b(?:commit|commits|commit_ref|commit-ref|hash|sha)\s*[:=#]?\s*"
    r"`?(?P<ref>[0-9a-f]{7,40})`?"
)
COMMIT_REF_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SUBJECT_TASK_BOUNDARY = r"(?<![A-Za-z0-9_.:/+-]){}(?![A-Za-z0-9_.:/+-])"


@dataclasses.dataclass(frozen=True)
class GitCommit:
    commit: str
    subject: str
    author_name: str
    author_email: str
    author_time: str
    committer_time: str
    parents: tuple[str, ...]
    plan_items: tuple[str, ...]
    changed_paths: tuple[str, ...]
    skipped_path_count: int = 0
    coverage_exempt_reason: str = ""

    def to_json(self) -> dict[str, object]:
        payload = {
            "commit": self.commit,
            "subject": self.subject,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "author_time": self.author_time,
            "committer_time": self.committer_time,
            "parents": list(self.parents),
            "parent_count": len(self.parents),
            "plan_items": list(self.plan_items),
            "changed_paths": list(self.changed_paths),
            "skipped_path_count": self.skipped_path_count,
        }
        if self.coverage_exempt_reason:
            payload["coverage_exempt_reason"] = self.coverage_exempt_reason
        return payload


@dataclasses.dataclass(frozen=True)
class PlanningEvidenceWarning:
    code: str
    message: str
    task_id: str = ""
    commit: str = ""
    source: str = ""
    diagnostic_task_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.task_id:
            payload["task_id"] = self.task_id
        if self.commit:
            payload["commit"] = self.commit
        if self.source:
            payload["source"] = self.source
        if self.diagnostic_task_ids:
            payload["diagnostic_task_ids"] = list(self.diagnostic_task_ids)
        return payload


@dataclasses.dataclass(frozen=True)
class PlanningEvidence:
    tasks: tuple[dict[str, object], ...]
    completion_evidence: tuple[dict[str, object], ...]
    run_attempts: tuple[dict[str, object], ...]
    commit_mappings: tuple[dict[str, object], ...]
    diagnostic_commit_mappings: tuple[dict[str, object], ...]
    commits: tuple[GitCommit, ...]
    skipped_evidence: tuple[SkippedEvidence, ...]
    warnings: tuple[PlanningEvidenceWarning, ...]
    task_source_origin: str
    git_commit_limit: int
    worklog_configured: bool

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
            "task_source_origin": self.task_source_origin,
            "git": {
                "commit_limit": self.git_commit_limit,
                "commits_collected": len(self.commits),
            },
            "worklog": {"configured": self.worklog_configured},
            "tasks": list(self.tasks),
            "completion_evidence": list(self.completion_evidence),
            "run_attempts": list(self.run_attempts),
            "commit_mappings": list(self.commit_mappings),
            "diagnostic_commit_mappings": list(self.diagnostic_commit_mappings),
            "commits": [commit.to_json() for commit in self.commits],
            "skipped_evidence": [
                skipped.to_json() for skipped in self.skipped_evidence
            ],
            "warnings": [warning.to_json() for warning in self.warnings],
        }


@dataclasses.dataclass(frozen=True)
class WorklogCommandResult:
    returncode: int
    stdout: str
    timed_out: bool = False
    output_too_large: bool = False
    error: str = ""


def collect_planning_evidence(
    config: VibeConfig,
    *,
    git_commit_limit: int = DEFAULT_GIT_COMMIT_LIMIT,
) -> PlanningEvidence:
    if git_commit_limit < 1:
        raise ValueError("git_commit_limit must be positive")

    resolution = resolve_runtime_task_source(config)
    task_source = build_task_source(config.repo, resolution.task_source)
    tasks = task_source.list_tasks()
    task_ids = {task.task_id for task in tasks}
    task_payloads = tuple(task_to_evidence_json(config.repo, task) for task in tasks)

    run_records = RunStore(config.state_path / "runs.jsonl").read_records()
    run_attempts = normalize_run_attempts(config.repo, run_records)

    completion_evidence: list[dict[str, object]] = [
        {
            "task_id": task.task_id,
            "source": "task_source_status",
            "status": task.status,
            "authoritative": True,
        }
        for task in tasks
        if task.status == DONE_STATUS
    ]
    commit_mappings: list[dict[str, object]] = []
    warnings: list[PlanningEvidenceWarning] = []
    skipped: list[SkippedEvidence] = []

    git_commits, git_skipped, git_warnings = collect_git_commits(
        config,
        git_commit_limit=git_commit_limit,
    )
    skipped.extend(git_skipped)
    warnings.extend(git_warnings)

    commit_lookup = {commit.commit: commit for commit in git_commits}
    worklog_records = collect_worklog_records(config, skipped, warnings)

    add_run_record_evidence(
        config,
        run_records,
        task_ids,
        commit_lookup,
        completion_evidence,
        commit_mappings,
        warnings,
    )
    add_worklog_evidence(
        config,
        worklog_records,
        task_ids,
        commit_lookup,
        completion_evidence,
        commit_mappings,
        warnings,
    )
    add_task_evidence_commit_refs(
        config,
        tasks,
        commit_lookup,
        completion_evidence,
        commit_mappings,
        warnings,
    )
    add_plan_item_mappings(
        git_commits,
        task_ids,
        completion_evidence,
        commit_mappings,
        warnings,
    )

    commit_mappings = dedupe_json_records(commit_mappings)
    completion_evidence = dedupe_json_records(completion_evidence)
    diagnostic_mappings = diagnostic_subject_mappings(
        config,
        git_commits,
        task_ids,
        commit_mappings,
    )
    warnings.extend(
        coverage_warnings(
            tasks,
            git_commits,
            commit_mappings,
            completion_evidence,
            diagnostic_mappings,
        )
    )

    return PlanningEvidence(
        tasks=task_payloads,
        completion_evidence=tuple(completion_evidence),
        run_attempts=tuple(run_attempts),
        commit_mappings=tuple(sorted_commit_mappings(commit_mappings)),
        diagnostic_commit_mappings=tuple(diagnostic_mappings),
        commits=tuple(git_commits),
        skipped_evidence=tuple(
            sorted(skipped, key=lambda item: (item.path, item.reason, item.detail))
        ),
        warnings=tuple(dedupe_warnings(warnings)),
        task_source_origin=resolution.origin,
        git_commit_limit=git_commit_limit,
        worklog_configured=config.planning_analytics.worklog_command is not None,
    )


def task_to_evidence_json(repo: Path, task: Task) -> dict[str, object]:
    payload = task.to_json()
    payload["source"] = repo_relative_text(repo, str(payload.get("source") or ""))
    payload["order"] = task.order
    return payload


def normalize_run_attempts(
    repo: Path,
    records: list[dict[str, Any]],
) -> tuple[dict[str, object], ...]:
    attempts: list[dict[str, object]] = []
    for index, record in enumerate(records):
        run_id = string_value(record.get("run_id"))
        task_id = string_value(record.get("task_id"))
        if not run_id or not task_id:
            continue
        record_type = string_value(record.get("record_type")) or RUN_RECORD_TYPE
        if record_type not in {RUN_RECORD_TYPE, WORKER_REPORT_RECORD_TYPE}:
            continue
        payload: dict[str, object] = {
            "record_index": index,
            "record_type": record_type,
            "run_id": run_id,
            "task_id": task_id,
            "status": string_value(record.get("status"))
            or string_value(record.get("classification")),
        }
        optional_copy(payload, "finished_at", record)
        optional_copy(payload, "reported_at", record)
        optional_copy(payload, "exit_code", record)
        optional_copy(payload, "session_id", record)
        optional_copy(payload, "session_id_source", record)
        optional_copy(payload, "classification_source", record)
        optional_copy(payload, "start_main", record)
        optional_copy(payload, "end_main", record)
        commit = worker_report_commit(record) or string_value(record.get("commit"))
        if commit:
            payload["commit"] = commit
        log = string_value(record.get("log"))
        if log:
            payload["log"] = repo_relative_text(repo, log)
        attempts.append(payload)
    return tuple(attempts)


def collect_worklog_records(
    config: VibeConfig,
    skipped: list[SkippedEvidence],
    warnings: list[PlanningEvidenceWarning],
) -> tuple[dict[str, Any], ...]:
    command = config.planning_analytics.worklog_command
    if command is None:
        return ()
    result = run_worklog_command(
        command,
        config.repo,
        timeout_seconds=WORKLOG_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes=MAX_WORKLOG_OUTPUT_BYTES,
    )
    if result.timed_out:
        warnings.append(
            PlanningEvidenceWarning(
                code="worklog_command_failed",
                message="worklog command timed out",
                source="worklog_command",
            )
        )
        return ()
    if result.error:
        warnings.append(
            PlanningEvidenceWarning(
                code="worklog_command_failed",
                message=f"worklog command failed: {result.error}",
                source="worklog_command",
            )
        )
        return ()
    if result.output_too_large:
        skipped.append(
            SkippedEvidence(
                path="worklog",
                reason="worklog_output_too_large",
                detail=f"stdout exceeded {MAX_WORKLOG_OUTPUT_BYTES} bytes",
            )
        )
        warnings.append(
            PlanningEvidenceWarning(
                code="worklog_output_too_large",
                message="worklog command stdout exceeded the configured collector cap",
                source="worklog_command",
            )
        )
        return ()
    if result.returncode != 0:
        warnings.append(
            PlanningEvidenceWarning(
                code="worklog_command_failed",
                message=f"worklog command exited with code {result.returncode}",
                source="worklog_command",
            )
        )
        return ()
    records: list[dict[str, Any]] = []
    text = result.stdout.strip()
    if not text:
        return ()
    try:
        payload = json.loads(text, parse_constant=reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        for index, line in enumerate(text.splitlines(), start=1):
            if index > MAX_WORKLOG_RECORDS:
                skipped.append(
                    SkippedEvidence(
                        path="worklog",
                        reason="worklog_record_limit",
                        detail=f"more than {MAX_WORKLOG_RECORDS} records",
                    )
                )
                break
            if not line.strip():
                continue
            try:
                payload = json.loads(line, parse_constant=reject_json_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                skipped.append(
                    SkippedEvidence(
                        path=f"worklog:{index}",
                        reason="invalid_worklog_json",
                        detail=str(exc),
                    )
                )
                continue
            if isinstance(payload, dict):
                records.append(payload)
            else:
                skipped.append(
                    SkippedEvidence(
                        path=f"worklog:{index}",
                        reason="invalid_worklog_record",
                        detail="record must be a JSON object",
                    )
                )
        return tuple(records)
    return tuple(worklog_records_from_json(payload, skipped))


def run_worklog_command(
    command: str,
    repo: Path,
    *,
    timeout_seconds: int,
    max_stdout_bytes: int,
) -> WorklogCommandResult:
    cmd, use_shell = prepare_shell_command(command)
    popen_kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            cmd,
            cwd=repo,
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError as exc:
        return WorklogCommandResult(127, "", error=exc.__class__.__name__)
    assert process.stdout is not None
    chunks: list[bytes] = []
    state = {"bytes": 0, "too_large": False}

    def read_stdout() -> None:
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            next_size = state["bytes"] + len(chunk)
            if next_size > max_stdout_bytes:
                remaining = max(0, max_stdout_bytes - state["bytes"])
                if remaining:
                    chunks.append(chunk[:remaining])
                state["bytes"] = next_size
                state["too_large"] = True
                terminate_process_group(process)
                return
            chunks.append(chunk)
            state["bytes"] = next_size

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(process)
        returncode = process.wait()
    reader.join(timeout=1)
    if reader.is_alive():
        terminate_process_group(process)
        process.stdout.close()
        reader.join(timeout=1)
    else:
        process.stdout.close()
    stdout = b"".join(chunks).decode("utf-8", errors="replace")
    return WorklogCommandResult(
        returncode,
        stdout,
        timed_out=timed_out,
        output_too_large=bool(state["too_large"]),
    )


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def worklog_records_from_json(
    payload: object,
    skipped: list[SkippedEvidence],
) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_records = payload.get("records", payload.get("worklog", payload))
    else:
        raw_records = payload
    if isinstance(raw_records, dict):
        raw_records = [raw_records]
    if not isinstance(raw_records, list):
        skipped.append(
            SkippedEvidence(
                path="worklog",
                reason="invalid_worklog_record",
                detail="worklog output must be a JSON object, array, or JSONL",
            )
        )
        return []
    records: list[dict[str, Any]] = []
    for index, record in enumerate(raw_records, start=1):
        if index > MAX_WORKLOG_RECORDS:
            skipped.append(
                SkippedEvidence(
                    path="worklog",
                    reason="worklog_record_limit",
                    detail=f"more than {MAX_WORKLOG_RECORDS} records",
                )
            )
            break
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped.append(
                SkippedEvidence(
                    path=f"worklog:{index}",
                    reason="invalid_worklog_record",
                    detail="record must be a JSON object",
                )
            )
    return records


def collect_git_commits(
    config: VibeConfig,
    *,
    git_commit_limit: int,
) -> tuple[list[GitCommit], list[SkippedEvidence], list[PlanningEvidenceWarning]]:
    skipped: list[SkippedEvidence] = []
    warnings: list[PlanningEvidenceWarning] = []
    if not is_git_repo(config.repo):
        warnings.append(
            PlanningEvidenceWarning(
                code="git_unavailable",
                message="git metadata is unavailable for this repository",
                source="git",
            )
        )
        return [], skipped, warnings

    format_spec = (
        "%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%P%x1f%s%x1f"
        "%(trailers:key=Plan-Item,valueonly,separator=%x1d)"
    )
    result = run_git(
        config.repo,
        [
            "log",
            f"--max-count={git_commit_limit + 1}",
            f"--format={format_spec}",
            "--name-only",
        ],
    )
    if result.returncode != 0:
        warnings.append(
            PlanningEvidenceWarning(
                code="git_log_failed",
                message="git log failed while collecting planning evidence",
                source="git",
            )
        )
        return [], skipped, warnings

    commits: list[GitCommit] = []
    for chunk in result.stdout.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        if not lines:
            continue
        metadata = lines[0].split("\x1f")
        if len(metadata) != 8:
            warnings.append(
                PlanningEvidenceWarning(
                    code="git_log_parse_failed",
                    message="git log returned an unexpected metadata record",
                    source="git",
                )
            )
            continue
        raw_paths = tuple(line.strip() for line in lines[1:] if line.strip())
        changed_paths, skipped_count = safe_git_paths(raw_paths, skipped)
        coverage_exempt_reason = commit_coverage_exempt_reason(
            config,
            raw_paths,
            changed_paths,
        )
        commit = GitCommit(
            commit=metadata[0],
            author_name=metadata[1],
            author_email=metadata[2],
            author_time=metadata[3],
            committer_time=metadata[4],
            parents=tuple(parent for parent in metadata[5].split() if parent),
            subject=metadata[6],
            plan_items=parse_plan_item_values(metadata[7]),
            changed_paths=changed_paths,
            skipped_path_count=skipped_count,
            coverage_exempt_reason=coverage_exempt_reason,
        )
        if commit.coverage_exempt_reason == "generated_artifact":
            continue
        commits.append(commit)
    if len(commits) > git_commit_limit:
        commits = commits[:git_commit_limit]
        warnings.append(
            PlanningEvidenceWarning(
                code="git_history_truncated",
                message=f"git metadata collection stopped at {git_commit_limit} commits",
                source="git",
            )
        )
    return commits, skipped, warnings


def is_git_repo(repo: Path) -> bool:
    return (
        run_git(repo, ["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
    )


def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=repo,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, exc.stdout or "", str(exc))


def safe_git_paths(
    paths: tuple[str, ...],
    skipped: list[SkippedEvidence],
) -> tuple[tuple[str, ...], int]:
    safe: list[str] = []
    skipped_count = 0
    for path in paths:
        reason = git_path_skip_reason(path)
        if reason:
            skipped_count += 1
            skipped.append(
                SkippedEvidence(
                    path=redact_manifest_text(path),
                    reason=reason,
                    detail="git changed path",
                )
            )
            continue
        safe.append(path)
    return tuple(sorted(dict.fromkeys(safe))), skipped_count


def git_path_skip_reason(path: str) -> str | None:
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return "outside_repo"
    if is_webhook_like_evidence_path(path):
        return "secret_path"
    if any(is_secret_like_directory_name(part) for part in pure_path.parts[:-1]):
        return "secret_directory"
    if is_secret_like_path(Path(path)):
        return "secret_path"
    return None


def commit_coverage_exempt_reason(
    config: VibeConfig,
    raw_paths: tuple[str, ...],
    changed_paths: tuple[str, ...],
) -> str:
    if raw_paths and not changed_paths:
        return "secret_paths_only"
    if changed_paths and all(is_metadata_path(config, path) for path in changed_paths):
        return "metadata_only"
    if changed_paths and all(
        is_generated_artifact_path(config, path) for path in changed_paths
    ):
        return "generated_artifact"
    return ""


def is_metadata_path(config: VibeConfig, path: str) -> bool:
    state_dir = PurePosixPath(config.state_dir)
    candidate = PurePosixPath(path)
    return candidate == state_dir or state_dir in candidate.parents


def is_generated_artifact_path(config: VibeConfig, path: str) -> bool:
    outputs = planning_analytics_output_report(config)
    candidate = PurePosixPath(path)
    for output in outputs.values():
        if not isinstance(output, dict) or output.get("source") != "explicit":
            continue
        output_path = output.get("path")
        if not isinstance(output_path, str):
            continue
        try:
            relative = Path(output_path).resolve().relative_to(config.repo).as_posix()
        except ValueError:
            continue
        if candidate == PurePosixPath(relative):
            return True
    return False


def add_run_record_evidence(
    config: VibeConfig,
    records: list[dict[str, Any]],
    task_ids: set[str],
    commit_lookup: dict[str, GitCommit],
    completion_evidence: list[dict[str, object]],
    commit_mappings: list[dict[str, object]],
    warnings: list[PlanningEvidenceWarning],
) -> None:
    for index, record in enumerate(records):
        task_id = string_value(record.get("task_id"))
        if not task_id:
            continue
        if task_id not in task_ids:
            warnings.append(
                unknown_task_reference_warning(
                    task_id,
                    source="worker_report"
                    if record.get("record_type") == WORKER_REPORT_RECORD_TYPE
                    else "run_result",
                )
            )
            continue
        record_type = string_value(record.get("record_type")) or RUN_RECORD_TYPE
        status = string_value(record.get("status")) or string_value(
            record.get("classification")
        )
        commit_ref = worker_report_commit(record) or string_value(record.get("commit"))
        embedded_report_completed = worker_report_status(record) == "completed"
        direct_report_completed = (
            record_type == WORKER_REPORT_RECORD_TYPE and status == "completed"
        )
        if embedded_report_completed:
            completion_evidence.append(
                {
                    "task_id": task_id,
                    "source": "run_result_worker_report",
                    "run_id": string_value(record.get("run_id")),
                    "status": "completed",
                    "authoritative": True,
                }
            )
        elif direct_report_completed:
            completion_evidence.append(
                {
                    "task_id": task_id,
                    "source": "worker_report",
                    "run_id": string_value(record.get("run_id")),
                    "status": status,
                    "authoritative": True,
                }
            )
        if not commit_ref:
            continue
        commit = resolve_commit_ref(config.repo, commit_ref, commit_lookup, warnings)
        if commit is None:
            continue
        commit_mappings.append(
            {
                "task_id": task_id,
                "commit": commit,
                "source": "worker_report"
                if record_type == WORKER_REPORT_RECORD_TYPE
                else "run_result_worker_report",
                "run_id": string_value(record.get("run_id")),
                "record_index": index,
                "authoritative": embedded_report_completed or direct_report_completed,
            }
        )


def add_worklog_evidence(
    config: VibeConfig,
    records: tuple[dict[str, Any], ...],
    task_ids: set[str],
    commit_lookup: dict[str, GitCommit],
    completion_evidence: list[dict[str, object]],
    commit_mappings: list[dict[str, object]],
    warnings: list[PlanningEvidenceWarning],
) -> None:
    for index, record in enumerate(records):
        record_task_ids = worklog_task_ids(record)
        if not record_task_ids:
            continue
        known_task_ids = tuple(
            task_id for task_id in record_task_ids if task_id in task_ids
        )
        for task_id in record_task_ids:
            if task_id not in task_ids:
                warnings.append(
                    unknown_task_reference_warning(task_id, source="worklog")
                )
        if not known_task_ids:
            continue
        status = string_value(record.get("status")) or string_value(record.get("state"))
        for task_id in known_task_ids:
            if status.casefold() in {"done", "completed", "complete"}:
                completion_evidence.append(
                    {
                        "task_id": task_id,
                        "source": "worklog",
                        "status": status,
                        "record_index": index,
                        "authoritative": True,
                    }
                )
        for commit_ref in worklog_commit_refs(record):
            commit = resolve_commit_ref(
                config.repo, commit_ref, commit_lookup, warnings
            )
            if commit is None:
                continue
            for task_id in known_task_ids:
                commit_mappings.append(
                    {
                        "task_id": task_id,
                        "commit": commit,
                        "source": "worklog",
                        "record_index": index,
                        "authoritative": True,
                    }
                )


def add_task_evidence_commit_refs(
    config: VibeConfig,
    tasks: list[Task],
    commit_lookup: dict[str, GitCommit],
    completion_evidence: list[dict[str, object]],
    commit_mappings: list[dict[str, object]],
    warnings: list[PlanningEvidenceWarning],
) -> None:
    for task in tasks:
        for commit_ref in explicit_commit_refs(task.evidence):
            commit = resolve_commit_ref(
                config.repo, commit_ref, commit_lookup, warnings
            )
            if commit is None:
                continue
            commit_mappings.append(
                {
                    "task_id": task.task_id,
                    "commit": commit,
                    "source": "task_evidence_commit_ref",
                    "authoritative": True,
                }
            )
            completion_evidence.append(
                {
                    "task_id": task.task_id,
                    "commit": commit,
                    "source": "task_evidence_commit_ref",
                    "authoritative": True,
                }
            )


def add_plan_item_mappings(
    commits: list[GitCommit],
    task_ids: set[str],
    completion_evidence: list[dict[str, object]],
    commit_mappings: list[dict[str, object]],
    warnings: list[PlanningEvidenceWarning],
) -> None:
    for commit in commits:
        for plan_item in commit.plan_items:
            if plan_item not in task_ids:
                warnings.append(
                    PlanningEvidenceWarning(
                        code="unknown_plan_item",
                        message=f"Plan-Item trailer references unknown task {plan_item}",
                        task_id=plan_item,
                        commit=commit.commit,
                        source="plan_item_trailer",
                    )
                )
                continue
            commit_mappings.append(
                {
                    "task_id": plan_item,
                    "commit": commit.commit,
                    "source": "plan_item_trailer",
                    "authoritative": True,
                }
            )
            completion_evidence.append(
                {
                    "task_id": plan_item,
                    "commit": commit.commit,
                    "source": "plan_item_trailer",
                    "authoritative": True,
                }
            )


def diagnostic_subject_mappings(
    config: VibeConfig,
    commits: list[GitCommit],
    task_ids: set[str],
    authoritative_mappings: list[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    if config.planning_analytics.subject_matching != "diagnostic":
        return ()
    authoritative = {
        (str(mapping.get("task_id")), str(mapping.get("commit")))
        for mapping in authoritative_mappings
    }
    diagnostics: list[dict[str, object]] = []
    for commit in commits:
        for task_id in sorted(task_ids):
            if (task_id, commit.commit) in authoritative:
                continue
            pattern = SUBJECT_TASK_BOUNDARY.format(re.escape(task_id))
            if re.search(pattern, commit.subject):
                diagnostics.append(
                    {
                        "task_id": task_id,
                        "commit": commit.commit,
                        "source": "subject_match",
                        "authoritative": False,
                    }
                )
    return tuple(diagnostics)


def coverage_warnings(
    tasks: list[Task],
    commits: list[GitCommit],
    commit_mappings: list[dict[str, object]],
    completion_evidence: list[dict[str, object]],
    diagnostic_mappings: tuple[dict[str, object], ...],
) -> tuple[PlanningEvidenceWarning, ...]:
    warnings: list[PlanningEvidenceWarning] = []
    mapped_tasks = {
        str(mapping.get("task_id"))
        for mapping in commit_mappings
        if mapping.get("authoritative") is True
    }
    completed_by_source = {
        str(evidence.get("task_id"))
        for evidence in completion_evidence
        if evidence.get("authoritative") is True
        and evidence.get("source") != "task_source_status"
    }
    for task in tasks:
        if (
            task.status == DONE_STATUS
            and task.task_id not in mapped_tasks | completed_by_source
        ):
            warnings.append(
                PlanningEvidenceWarning(
                    code="done_task_without_authoritative_mapping",
                    message=(
                        "done task has no authoritative completion mapping beyond "
                        "task-source status"
                    ),
                    task_id=task.task_id,
                    source="task_source_status",
                )
            )

    mapped_commits = {
        str(mapping.get("commit"))
        for mapping in commit_mappings
        if mapping.get("authoritative") is True
    }
    diagnostic_by_commit: dict[str, list[str]] = {}
    for mapping in diagnostic_mappings:
        diagnostic_by_commit.setdefault(str(mapping.get("commit")), []).append(
            str(mapping.get("task_id"))
        )
    for commit in commits:
        if commit.coverage_exempt_reason:
            continue
        if commit.commit in mapped_commits:
            continue
        warnings.append(
            PlanningEvidenceWarning(
                code="unmapped_commit",
                message="non-generated commit lacks authoritative task mapping",
                commit=commit.commit,
                source="git",
                diagnostic_task_ids=tuple(
                    sorted(diagnostic_by_commit.get(commit.commit, []))
                ),
            )
        )
    return tuple(warnings)


def explicit_commit_refs(text: str) -> tuple[str, ...]:
    refs = [match.group("ref") for match in EXPLICIT_COMMIT_REF_RE.finditer(text)]
    return tuple(dict.fromkeys(refs))


def worklog_task_ids(record: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item
            for item in [
                *string_list(record.get("task_ids")),
                *string_list(record.get("tasks")),
                string_value(record.get("task_id")),
            ]
            if item
        )
    )


def worklog_commit_refs(record: dict[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    for key in ("commit", "commit_hash", "commit_ref", "sha"):
        value = string_value(record.get(key))
        if value:
            refs.append(value)
    refs.extend(string_list(record.get("commits")))
    refs.extend(string_list(record.get("commit_refs")))
    return tuple(dict.fromkeys(refs))


def resolve_commit_ref(
    repo: Path,
    commit_ref: str,
    commit_lookup: dict[str, GitCommit],
    warnings: list[PlanningEvidenceWarning],
) -> str | None:
    if not COMMIT_REF_RE.fullmatch(commit_ref):
        warnings.append(
            PlanningEvidenceWarning(
                code="invalid_commit_ref",
                message="commit reference must be a 7-40 character hex hash",
                source="commit_ref",
            )
        )
        return None
    for commit in commit_lookup:
        if commit.startswith(commit_ref):
            return commit
    result = run_git(repo, ["rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
    if result.returncode == 0:
        return result.stdout.strip()
    warnings.append(
        PlanningEvidenceWarning(
            code="unresolved_commit_ref",
            message=f"commit reference could not be resolved: {commit_ref}",
            source="commit_ref",
        )
    )
    return None


def unknown_task_reference_warning(
    task_id: str,
    *,
    source: str,
) -> PlanningEvidenceWarning:
    return PlanningEvidenceWarning(
        code="unknown_task_reference",
        message=f"{source} references unknown task {task_id}",
        task_id=task_id,
        source=source,
    )


def parse_plan_item_values(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    items: list[str] = []
    for trailer_value in value.split("\x1d"):
        for part in re.split(r"[,;\s]+", trailer_value.strip()):
            if part:
                items.append(part)
    return tuple(dict.fromkeys(items))


def worker_report_commit(record: dict[str, Any]) -> str:
    worker_report = record.get("worker_report")
    if isinstance(worker_report, dict):
        return string_value(worker_report.get("commit"))
    return ""


def worker_report_status(record: dict[str, Any]) -> str:
    worker_report = record.get("worker_report")
    if isinstance(worker_report, dict):
        return string_value(worker_report.get("status"))
    return ""


def sorted_commit_mappings(
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(
        mappings,
        key=lambda mapping: (
            str(mapping.get("commit", "")),
            str(mapping.get("task_id", "")),
            str(mapping.get("source", "")),
            str(mapping.get("run_id", "")),
            int_value(mapping.get("record_index")),
        ),
    )


def dedupe_json_records(
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for record in records:
        key = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def dedupe_warnings(
    warnings: list[PlanningEvidenceWarning],
) -> list[PlanningEvidenceWarning]:
    seen: set[tuple[object, ...]] = set()
    deduped: list[PlanningEvidenceWarning] = []
    for warning in warnings:
        key = (
            warning.code,
            warning.message,
            warning.task_id,
            warning.commit,
            warning.source,
            warning.diagnostic_task_ids,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped


def optional_copy(
    payload: dict[str, object],
    key: str,
    record: dict[str, Any],
) -> None:
    value = record.get(key)
    if value is not None and value != "":
        payload[key] = value


def string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in (string_value(item) for item in value) if item]
    return []


def repo_relative_text(repo: Path, value: str) -> str:
    if not value:
        return ""
    try:
        return Path(value).resolve().relative_to(repo).as_posix()
    except (OSError, ValueError):
        repo_text = repo.as_posix()
        if value.startswith(f"{repo_text}/"):
            return value[len(repo_text) + 1 :]
        return value


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
