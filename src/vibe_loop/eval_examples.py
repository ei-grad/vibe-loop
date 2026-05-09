from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    GENERATED_TASK_PROFILE_PROMPT_VERSION,
    GENERATED_TASK_PROFILE_SCHEMA_VERSION,
    GENERATED_TASK_PROFILE_CACHE_FILE,
)


EXAMPLE_SUITE_ID = "local-demo-v1"
EXAMPLES_RELATIVE_ROOT = Path("eval") / "examples" / EXAMPLE_SUITE_ID


@dataclasses.dataclass(frozen=True)
class EvalExampleCase:
    case_id: str
    title: str
    task_id: str | None
    positive: bool
    repo_path: Path
    prompt_paths: tuple[str, ...]
    conditions: tuple[str, ...]
    budget: dict[str, object]
    expected_artifact_roles: tuple[str, ...]
    task_source: dict[str, object]


@dataclasses.dataclass(frozen=True)
class EvalExampleGraderResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def default_eval_examples_root() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / EXAMPLES_RELATIVE_ROOT


def load_eval_example_manifest(examples_root: Path | None = None) -> dict[str, Any]:
    root = examples_root or default_eval_examples_root()
    manifest_path = root / "manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("suite_id") != EXAMPLE_SUITE_ID:
        raise ValueError(f"unsupported eval example suite: {manifest.get('suite_id')}")
    return manifest


def list_eval_example_cases(
    examples_root: Path | None = None,
) -> tuple[EvalExampleCase, ...]:
    root = examples_root or default_eval_examples_root()
    manifest = load_eval_example_manifest(root)
    cases = tuple(
        load_eval_example_case(case["case_id"], examples_root=root)
        for case in manifest.get("cases", ())
    )
    return tuple(sorted(cases, key=lambda case: case.case_id))


def load_eval_example_case(
    case_id: str,
    *,
    examples_root: Path | None = None,
) -> EvalExampleCase:
    root = examples_root or default_eval_examples_root()
    case_path = root / "cases" / case_id
    with (case_path / "case.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    repo_path = case_path / "repo"
    return EvalExampleCase(
        case_id=payload["case_id"],
        title=payload["title"],
        task_id=payload.get("task_id"),
        positive=payload["positive"],
        repo_path=repo_path,
        prompt_paths=tuple(payload["prompt_paths"]),
        conditions=tuple(payload["conditions"]),
        budget=dict(payload["budget"]),
        expected_artifact_roles=tuple(payload["expected_artifact_roles"]),
        task_source=dict(payload.get("task_source", {})),
    )


def materialize_eval_example(
    case_id: str,
    destination: Path,
    *,
    examples_root: Path | None = None,
    overwrite: bool = False,
    include_reference_patch: bool = False,
) -> Path:
    case = load_eval_example_case(case_id, examples_root=examples_root)
    target = Path(destination)
    if target.exists():
        if not overwrite:
            raise FileExistsError(target)
        teardown_eval_example(target)
    shutil.copytree(case.repo_path, target, symlinks=False)
    if not include_reference_patch:
        reference_patch = target / "eval" / "reference.patch"
        if reference_patch.exists():
            reference_patch.unlink()
    copy_case_metadata(case, target)
    seed_generated_task_profile_cache(target)
    refresh_active_lock_metadata(target)
    initialize_git_checkout(target)
    apply_seed_user_state(target)
    return target


def copy_case_metadata(case: EvalExampleCase, target: Path) -> None:
    metadata_path = target / "eval" / "case.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": EXAMPLE_SUITE_ID,
                "case_id": case.case_id,
                "title": case.title,
                "task_id": case.task_id,
                "positive": case.positive,
                "prompt_paths": list(case.prompt_paths),
                "conditions": list(case.conditions),
                "budget": case.budget,
                "expected_artifact_roles": list(case.expected_artifact_roles),
                "task_source": case.task_source,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def seed_generated_task_profile_cache(target: Path) -> None:
    profile_path = target / "eval" / "expected-task-source-profile.json"
    if not profile_path.is_file():
        return
    with profile_path.open(encoding="utf-8") as handle:
        profile = json.load(handle)
    source_paths = [
        path
        for path in profile.get("source_paths", ())
        if isinstance(path, str) and path
    ]
    cache = {
        "schema_version": GENERATED_TASK_PROFILE_SCHEMA_VERSION,
        "prompt_version": GENERATED_TASK_PROFILE_PROMPT_VERSION,
        "status": "profile",
        "generated_at": utc_now(),
        "agent": {
            "name": "fixture_stub",
            "selection_command_source": "explicit:agent.selection_command",
            "default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "default_policy": AGENT_DEFAULT_POLICY,
        },
        "confidence": 0.95,
        "provenance": {
            "repo": str(target),
            "evidence_limit": None,
            "evidence_file_count": len(source_paths),
            "skipped_evidence": [
                {
                    "path": ".env.example",
                    "reason": "secret_like_path",
                }
            ],
        },
        "source_fingerprints": [
            fingerprint_file(target, source_path) for source_path in source_paths
        ],
        "profile": profile,
        "degradation": None,
    }
    cache_path = target / ".vibe-loop" / GENERATED_TASK_PROFILE_CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fingerprint_file(repo: Path, relative_path: str) -> dict[str, object]:
    path = repo / relative_path
    content = path.read_bytes()
    stat = path.stat()
    return {
        "path": relative_path,
        "size": stat.st_size,
        "sha256": hashlib.sha256(content).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "redacted": False,
    }


def refresh_active_lock_metadata(target: Path) -> None:
    lock_root = target / ".vibe-loop" / "locks"
    if not lock_root.is_dir():
        return
    for lock_path in sorted(lock_root.glob("*.lock/lock.json")):
        with lock_path.open(encoding="utf-8") as handle:
            metadata = json.load(handle)
        if metadata.get("record_type") != "active_run":
            continue
        pid = os.getpid()
        metadata.update(
            {
                "pid": pid,
                "worker_pid": pid,
                "pid_source": "popen",
                "host": socket.gethostname(),
                "started_at": utc_now(),
            }
        )
        lock_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def initialize_git_checkout(target: Path) -> None:
    run_git(target, "init")
    run_git(target, "checkout", "-B", "main")
    run_git(target, "config", "user.name", "vibe-loop eval fixture")
    run_git(target, "config", "user.email", "vibe-loop-eval@example.invalid")
    run_git(target, "add", "-A")
    run_git(target, "commit", "-m", "seed fixture")


def apply_seed_user_state(target: Path) -> None:
    seed_path = target / "eval" / "seed-user-state.json"
    if not seed_path.is_file():
        return
    with seed_path.open(encoding="utf-8") as handle:
        seed = json.load(handle)
    tracked = seed.get("tracked_modification")
    if isinstance(tracked, dict):
        path_value = tracked.get("path")
        append_text = tracked.get("append_text")
        if isinstance(path_value, str) and isinstance(append_text, str):
            with (target / path_value).open("a", encoding="utf-8") as handle:
                handle.write(append_text)
    untracked = seed.get("untracked_file")
    if isinstance(untracked, dict):
        path_value = untracked.get("path")
        content = untracked.get("content")
        if isinstance(path_value, str) and isinstance(content, str):
            destination = target / path_value
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        text=True,
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def teardown_eval_example(path: Path) -> None:
    target = Path(path)
    if not is_materialized_eval_example(target):
        raise ValueError(f"refusing to remove non-eval-example path: {target}")
    shutil.rmtree(target)


def is_materialized_eval_example(path: Path) -> bool:
    target = Path(path)
    marker = target / "eval" / "expected-artifacts.json"
    grader = target / "eval" / "graders" / "grade.py"
    if not marker.is_file() or not grader.is_file():
        return False
    try:
        with marker.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("suite_id") == EXAMPLE_SUITE_ID and isinstance(
        payload.get("case_id"),
        str,
    )


def run_eval_example_grader(
    repo: Path,
    *,
    artifact_root: Path | None = None,
) -> EvalExampleGraderResult:
    command = [
        sys.executable,
        "-m",
        "vibe_loop.eval_example_grader",
        "--repo",
        str(repo),
    ]
    if artifact_root is not None:
        command.extend(["--artifacts", str(artifact_root)])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
        text=True,
    )
    return EvalExampleGraderResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
