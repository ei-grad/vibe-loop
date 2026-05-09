from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from vibe_loop.generated_discovery import (
    is_secret_like_directory_name,
    is_secret_like_path,
)


EVAL_RUN_SCHEMA_VERSION = 1
EVAL_RUN_RECORD_TYPE = "skill_eval_run"
EVAL_CONDITIONS = frozenset(
    {
        "no_skill",
        "vibe_loop",
        "infinite_vibe_loop",
        "candidate_skill",
        "self_generated_skill",
    }
)
EVAL_OUTCOMES = frozenset(
    {
        "passed",
        "failed",
        "timeout",
        "infrastructure_error",
        "skipped",
    }
)
EVAL_FAILURE_TAXONOMY = frozenset(
    {
        "task_outcome",
        "workflow_contract",
        "trigger_false_negative",
        "trigger_false_positive",
        "unsafe_git",
        "secret_access",
        "state_contamination",
        "review_missing",
        "integration_missing",
        "unnecessary_user_prompt",
        "timeout",
        "harness_error",
        "grader_error",
        "flaky",
    }
)
REQUIRED_EVAL_ARTIFACT_ROLES = frozenset(
    {
        "prompt",
        "run_log",
        "transcript",
        "diff",
        "final_repo_state",
        "structured_result",
        "grader_outputs",
    }
)
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_WINDOWS_ABSOLUTE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


@dataclasses.dataclass(frozen=True)
class EvalSourceFingerprint:
    path: str
    sha256: str
    size: int
    mtime_ns: int | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }
        if self.mtime_ns is not None:
            payload["mtime_ns"] = self.mtime_ns
        return payload


@dataclasses.dataclass(frozen=True)
class EvalArtifactRef:
    role: str
    path: str
    sha256: str
    required: bool = True
    content_type: str = "application/octet-stream"

    def to_json(self) -> dict[str, object]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "required": self.required,
            "content_type": self.content_type,
        }


@dataclasses.dataclass(frozen=True)
class SkillEvalRunRecord:
    suite_id: str
    case_id: str
    trial: int
    condition: str
    run_id: str
    task: Mapping[str, object]
    skill_condition: Mapping[str, object]
    agent: Mapping[str, object]
    model: Mapping[str, object]
    harness: Mapping[str, object]
    budget: Mapping[str, object]
    source_fingerprints: Sequence[EvalSourceFingerprint]
    artifacts: Sequence[EvalArtifactRef]
    final_repo_state: Mapping[str, object]
    structured_result: Mapping[str, object]
    graders: Sequence[Mapping[str, object]]
    scoring: Mapping[str, object]
    reproducibility: Mapping[str, object]
    status: str
    started_at: str
    finished_at: str
    failure_taxonomy: Sequence[str] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": EVAL_RUN_SCHEMA_VERSION,
            "record_type": EVAL_RUN_RECORD_TYPE,
            "suite_id": self.suite_id,
            "case_id": self.case_id,
            "trial": self.trial,
            "condition": self.condition,
            "run_id": self.run_id,
            "task": dict(self.task),
            "skill_condition": dict(self.skill_condition),
            "agent": dict(self.agent),
            "model": dict(self.model),
            "harness": dict(self.harness),
            "budget": dict(self.budget),
            "source_fingerprints": [
                fingerprint.to_json() for fingerprint in self.source_fingerprints
            ],
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
            "final_repo_state": dict(self.final_repo_state),
            "structured_result": dict(self.structured_result),
            "graders": [dict(grader) for grader in self.graders],
            "scoring": dict(self.scoring),
            "reproducibility": dict(self.reproducibility),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_taxonomy": list(self.failure_taxonomy),
        }


def validate_skill_eval_run_record(
    record: Mapping[str, Any],
    artifact_root: Path,
    *,
    current_source_fingerprints: Mapping[str, str | Mapping[str, object]] | None = None,
    required_artifact_roles: frozenset[str] = REQUIRED_EVAL_ARTIFACT_ROLES,
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    artifact_root = artifact_root.resolve()

    if record.get("schema_version") != EVAL_RUN_SCHEMA_VERSION:
        diagnostics.append("unsupported eval run schema_version")
    if record.get("record_type") != EVAL_RUN_RECORD_TYPE:
        diagnostics.append("unsupported eval run record_type")

    for field in (
        "suite_id",
        "case_id",
        "condition",
        "run_id",
        "status",
        "started_at",
        "finished_at",
    ):
        if not nonempty_string(record.get(field)):
            diagnostics.append(f"missing or invalid string field: {field}")

    trial = record.get("trial")
    if not isinstance(trial, int) or trial < 1:
        diagnostics.append("missing or invalid positive integer field: trial")

    condition = record.get("condition")
    if isinstance(condition, str) and condition not in EVAL_CONDITIONS:
        diagnostics.append(f"unsupported eval condition: {condition}")

    status = record.get("status")
    if isinstance(status, str) and status not in EVAL_OUTCOMES:
        diagnostics.append(f"unsupported eval status: {status}")

    for field in (
        "task",
        "skill_condition",
        "agent",
        "model",
        "harness",
        "budget",
        "final_repo_state",
        "structured_result",
        "scoring",
        "reproducibility",
    ):
        if not isinstance(record.get(field), Mapping):
            diagnostics.append(f"missing or invalid object field: {field}")

    diagnostics.extend(validate_task_metadata(record.get("task")))
    diagnostics.extend(validate_skill_condition(record.get("skill_condition")))
    diagnostics.extend(
        validate_condition_skill_matrix(
            record.get("condition"),
            record.get("skill_condition"),
        )
    )
    diagnostics.extend(validate_agent_identity(record.get("agent")))
    diagnostics.extend(validate_model_identity(record.get("model")))
    diagnostics.extend(validate_harness_identity(record.get("harness")))
    diagnostics.extend(validate_budget(record.get("budget")))
    diagnostics.extend(validate_final_repo_state(record.get("final_repo_state")))
    diagnostics.extend(validate_structured_result(record.get("structured_result")))
    diagnostics.extend(validate_scoring(record.get("scoring")))
    diagnostics.extend(validate_reproducibility(record.get("reproducibility")))
    diagnostics.extend(validate_failure_taxonomy(record.get("failure_taxonomy")))
    diagnostics.extend(
        validate_source_fingerprints(
            record.get("source_fingerprints"),
            current_source_fingerprints=current_source_fingerprints,
        )
    )
    diagnostics.extend(
        validate_artifacts(
            record.get("artifacts"),
            artifact_root,
            required_artifact_roles=required_artifact_roles,
        )
    )

    graders = record.get("graders")
    if not isinstance(graders, Sequence) or isinstance(graders, (str, bytes)):
        diagnostics.append("missing or invalid list field: graders")
    elif not all(isinstance(grader, Mapping) for grader in graders):
        diagnostics.append("grader entries must be objects")

    return tuple(diagnostics)


def validate_source_fingerprints(
    value: object,
    *,
    current_source_fingerprints: Mapping[str, str | Mapping[str, object]] | None = None,
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ("missing or invalid list field: source_fingerprints",)
    if not value:
        return ("source_fingerprints must not be empty",)

    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            diagnostics.append(f"source fingerprint entry {index} must be an object")
            continue
        path = entry.get("path")
        sha256 = entry.get("sha256")
        size = entry.get("size")
        diagnostics.extend(path_diagnostics("source fingerprint", path))
        if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
            diagnostics.append(f"source fingerprint has invalid sha256: {path}")
        if not isinstance(size, int) or size < 0:
            diagnostics.append(f"source fingerprint has invalid size: {path}")
        if (
            current_source_fingerprints is not None
            and isinstance(path, str)
            and isinstance(sha256, str)
            and _SHA256_PATTERN.fullmatch(sha256)
        ):
            current_sha = current_source_sha(path, current_source_fingerprints)
            if current_sha is None:
                diagnostics.append(
                    f"source fingerprint missing from current sources: {path}"
                )
            elif current_sha != sha256:
                diagnostics.append(f"source fingerprint stale: {path}")
    return tuple(diagnostics)


def validate_artifacts(
    value: object,
    artifact_root: Path,
    *,
    required_artifact_roles: frozenset[str] = REQUIRED_EVAL_ARTIFACT_ROLES,
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ("missing or invalid list field: artifacts",)

    satisfied_required_roles: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            diagnostics.append(f"artifact entry {index} must be an object")
            continue
        role = entry.get("role")
        path = entry.get("path")
        sha256 = entry.get("sha256")
        required = entry.get("required", True)
        if not nonempty_string(role):
            diagnostics.append(f"artifact entry {index} has invalid role")
        elif role in required_artifact_roles and required is False:
            diagnostics.append(f"required artifact role marked optional: {role}")
        elif role in required_artifact_roles and required is True:
            satisfied_required_roles.add(role)
        path_errors = path_diagnostics("artifact", path)
        diagnostics.extend(path_errors)
        if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
            diagnostics.append(f"artifact has invalid sha256: {path}")
        if not isinstance(required, bool):
            diagnostics.append(f"artifact has invalid required flag: {path}")
        if path_errors:
            continue
        if required is False:
            continue
        assert isinstance(path, str)
        artifact_link_path = artifact_root / Path(path)
        if has_symlink_component(artifact_root, Path(path)):
            diagnostics.append(f"artifact path must not be a symlink: {path}")
            continue
        artifact_path = artifact_link_path.resolve()
        try:
            resolved_relative = artifact_path.relative_to(artifact_root)
        except ValueError:
            diagnostics.append(f"artifact resolves outside artifact root: {path}")
            continue
        resolved_relative_path = resolved_relative.as_posix()
        if is_secret_like_eval_path(resolved_relative_path):
            diagnostics.append(
                "artifact resolved path is secret-like: "
                f"{redacted_eval_path(resolved_relative_path)}"
            )
            continue
        if not artifact_path.is_file():
            diagnostics.append(f"required artifact missing: {path}")
            continue
        if isinstance(sha256, str) and _SHA256_PATTERN.fullmatch(sha256):
            actual_sha = sha256_file(artifact_path)
            if actual_sha != sha256:
                diagnostics.append(f"artifact sha256 mismatch: {path}")

    for role in sorted(required_artifact_roles - satisfied_required_roles):
        diagnostics.append(f"required artifact role missing: {role}")

    return tuple(diagnostics)


def validate_task_metadata(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    if not nonempty_string(value.get("id")):
        diagnostics.append("task.id is required")
    prompt_sha = value.get("prompt_sha256")
    if not isinstance(prompt_sha, str) or not _SHA256_PATTERN.fullmatch(prompt_sha):
        diagnostics.append("task.prompt_sha256 must be a SHA-256 hex digest")
    if not nonempty_string(value.get("expected_skill")):
        diagnostics.append("task.expected_skill is required")
    if not isinstance(value.get("should_trigger"), bool):
        diagnostics.append("task.should_trigger must be a boolean")
    return tuple(diagnostics)


def validate_skill_condition(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    if not nonempty_string(value.get("id")):
        diagnostics.append("skill_condition.id is required")
    skills_available = value.get("skills_available")
    if not isinstance(skills_available, bool):
        diagnostics.append("skill_condition.skills_available must be a boolean")
    if skills_available is True:
        if not nonempty_string(value.get("skill_id")):
            diagnostics.append("skill_condition.skill_id is required when skills exist")
        skill_sha = value.get("skill_sha256")
        if not isinstance(skill_sha, str) or not _SHA256_PATTERN.fullmatch(skill_sha):
            diagnostics.append(
                "skill_condition.skill_sha256 must be a SHA-256 hex digest"
            )
    return tuple(diagnostics)


def validate_condition_skill_matrix(
    condition: object,
    skill_condition: object,
) -> tuple[str, ...]:
    if not isinstance(condition, str) or not isinstance(skill_condition, Mapping):
        return ()
    skills_available = skill_condition.get("skills_available")
    skill_id = skill_condition.get("skill_id")
    diagnostics: list[str] = []
    if condition == "no_skill":
        if skills_available is not False:
            diagnostics.append("no_skill condition must have skills_available=false")
        if nonempty_string(skill_id):
            diagnostics.append("no_skill condition must not expose skill_id")
    elif condition in EVAL_CONDITIONS:
        if skills_available is not True:
            diagnostics.append(f"{condition} condition must have skills_available=true")
        expected_skill_id = expected_skill_for_condition(condition)
        if expected_skill_id is not None and skill_id != expected_skill_id:
            diagnostics.append(
                f"{condition} condition must expose skill_id={expected_skill_id}"
            )
    return tuple(diagnostics)


def expected_skill_for_condition(condition: str) -> str | None:
    if condition == "vibe_loop":
        return "vibe-loop"
    if condition == "infinite_vibe_loop":
        return "infinite-vibe-loop"
    return None


def validate_agent_identity(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    for key in ("name", "command_source"):
        if not nonempty_string(value.get(key)):
            diagnostics.append(f"agent.{key} is required")
    return tuple(diagnostics)


def validate_model_identity(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    for key in ("provider", "id"):
        if not nonempty_string(value.get(key)):
            diagnostics.append(f"model.{key} is required")
    return tuple(diagnostics)


def validate_harness_identity(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    for key in ("name", "version", "command"):
        if not nonempty_string(value.get(key)):
            diagnostics.append(f"harness.{key} is required")
    return tuple(diagnostics)


def validate_budget(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    for key in ("timeout_seconds", "max_commands", "max_output_bytes"):
        if not positive_int(value.get(key)):
            diagnostics.append(f"budget.{key} must be a positive integer")
    return tuple(diagnostics)


def validate_final_repo_state(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    for key in ("head", "branch"):
        if not nonempty_string(value.get(key)):
            diagnostics.append(f"final_repo_state.{key} is required")
    if not isinstance(value.get("dirty"), bool):
        diagnostics.append("final_repo_state.dirty must be a boolean")
    return tuple(diagnostics)


def validate_structured_result(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    if not isinstance(value.get("exit_code"), int):
        diagnostics.append("structured_result.exit_code must be an integer")
    if not isinstance(value.get("timeout"), bool):
        diagnostics.append("structured_result.timeout must be a boolean")
    if not nonempty_string(value.get("task_status")):
        diagnostics.append("structured_result.task_status is required")
    if not isinstance(value.get("workflow_contract_completed"), bool):
        diagnostics.append(
            "structured_result.workflow_contract_completed must be a boolean"
        )
    return tuple(diagnostics)


def validate_scoring(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    if not isinstance(value.get("passed"), bool):
        diagnostics.append("scoring.passed must be a boolean")
    for key in ("task_score", "workflow_score"):
        if not score_in_range(value.get(key)):
            diagnostics.append(f"scoring.{key} must be between 0.0 and 1.0")
    trigger_score = value.get("trigger_score")
    if trigger_score is not None and not score_in_range(trigger_score):
        diagnostics.append("scoring.trigger_score must be between 0.0 and 1.0")
    excluded = value.get("excluded_from_primary")
    if excluded is not None and not isinstance(excluded, bool):
        diagnostics.append("scoring.excluded_from_primary must be a boolean")
    return tuple(diagnostics)


def validate_reproducibility(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    diagnostics: list[str] = []
    fixture_sha = value.get("fixture_sha256")
    if not isinstance(fixture_sha, str) or not _SHA256_PATTERN.fullmatch(fixture_sha):
        diagnostics.append("reproducibility.fixture_sha256 must be a SHA-256 digest")
    if not positive_int(value.get("run_order")):
        diagnostics.append("reproducibility.run_order must be a positive integer")
    if value.get("fresh_workspace") is not True:
        diagnostics.append("reproducibility.fresh_workspace must be true")
    if value.get("state_reused") is not False:
        diagnostics.append("reproducibility.state_reused must be false")
    return tuple(diagnostics)


def validate_failure_taxonomy(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ("failure_taxonomy must be a list",)
    diagnostics: list[str] = []
    for label in value:
        if not isinstance(label, str) or label not in EVAL_FAILURE_TAXONOMY:
            diagnostics.append(f"unsupported failure taxonomy label: {label}")
    return tuple(diagnostics)


def path_diagnostics(label: str, value: object) -> tuple[str, ...]:
    if not nonempty_string(value):
        return (f"{label} path is required",)
    assert isinstance(value, str)
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or _WINDOWS_ABSOLUTE_PATTERN.match(value)
        or any(part in {"", ".."} for part in path.parts)
    ):
        return (f"{label} path must be a safe relative path: {value}",)
    if is_secret_like_eval_path(normalized):
        return (f"{label} path is secret-like: {redacted_eval_path(normalized)}",)
    return ()


def is_secret_like_eval_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    return any(is_secret_like_directory_name(part) for part in parts[:-1]) or (
        is_secret_like_path(Path(parts[-1]))
    )


def redacted_eval_path(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    if not parts:
        return path
    redacted: list[str] = []
    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        if is_secret_like_directory_name(part) or (
            is_last and is_secret_like_path(Path(part))
        ):
            redacted.append("<redacted>")
        else:
            redacted.append(part)
    return "/".join(redacted)


def current_source_sha(
    path: str,
    current_source_fingerprints: Mapping[str, str | Mapping[str, object]],
) -> str | None:
    value = current_source_fingerprints.get(path)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        sha256 = value.get("sha256")
        return sha256 if isinstance(sha256, str) else None
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def score_in_range(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )


def has_symlink_component(root: Path, relative_path: Path) -> bool:
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
