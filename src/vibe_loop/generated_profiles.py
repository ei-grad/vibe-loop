from __future__ import annotations

import dataclasses
import json
import math
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    GENERATED_TASK_PROFILE_PROMPT_VERSION,
    GENERATED_TASK_PROFILE_SCHEMA_VERSION,
    AgentResolutionError,
    VibeConfig,
    reject_generated_command_adapters,
)
from vibe_loop.generated_discovery import (
    EvidenceBundle,
    collect_generated_discovery_evidence,
)


GENERATED_PROFILE_CONFIDENCE_THRESHOLD = 0.7
GENERATED_PROFILE_STATUSES = frozenset(
    {"profile", "planning_only", "needs_input", "unavailable", "rejected"}
)
DEGRADED_PROFILE_STATUSES = frozenset(
    {"planning_only", "needs_input", "unavailable", "rejected"}
)
SUPPORTED_PROFILE_KINDS = frozenset(
    {"markdown_table", "markdown_headings", "markdown_list"}
)
ALLOWED_PROFILE_KEYS = frozenset(
    {"kind", "source_paths", "stable_ids", "fields", "status_map"}
)
ALLOWED_PROFILE_FIELD_KEYS = frozenset(
    {
        "acceptance",
        "dependencies",
        "evidence",
        "id",
        "priority",
        "scope",
        "section",
        "status",
        "title",
    }
)
ALLOWED_FIELD_MAPPING_KEYS = frozenset(
    {"column", "label", "none_values", "pattern", "prefix", "required", "strategy"}
)
ALLOWED_FIELD_STRATEGIES = frozenset(
    {"first_sentence", "full_text", "heading_text", "label_value", "literal"}
)
ALLOWED_STATUS_MAP_KEYS = frozenset({"blocked", "done", "runnable"})


@dataclasses.dataclass(frozen=True)
class GeneratedTaskConfigureResult:
    payload: dict[str, Any]
    cache_path: Path
    diagnostics: tuple[str, ...]
    exit_code: int

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.payload.get("status"),
            "cache_path": str(self.cache_path),
            "exit_code": self.exit_code,
            "diagnostics": list(self.diagnostics),
            "cache": self.payload,
        }


def configure_generated_task_source(config: VibeConfig) -> GeneratedTaskConfigureResult:
    if not config.task_source.allows_generated_cache:
        payload = build_config_disabled_cache(config)
        return GeneratedTaskConfigureResult(
            payload=payload,
            cache_path=config.generated_task_profile_path,
            diagnostics=diagnostics_for_cache_payload(config, payload),
            exit_code=2,
        )

    bundle = collect_generated_discovery_evidence(
        config.repo,
        state_dir=config.state_dir,
    )
    try:
        command_template = config.agent.require_selection_command()
    except AgentResolutionError as exc:
        payload = build_degraded_cache(
            config,
            bundle,
            "unavailable",
            "agent_unavailable",
            str(exc),
        )
        write_generated_task_cache(config.generated_task_profile_path, payload)
        return GeneratedTaskConfigureResult(
            payload=payload,
            cache_path=config.generated_task_profile_path,
            diagnostics=diagnostics_for_cache_payload(config, payload),
            exit_code=2,
        )

    prompt = build_generated_task_source_prompt(bundle)
    command = command_template.format(prompt=shlex.quote(prompt))
    try:
        result = subprocess.run(
            command,
            cwd=config.repo,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = build_degraded_cache(
            config,
            bundle,
            "unavailable",
            "agent_execution_failed",
            exc.__class__.__name__,
        )
        write_generated_task_cache(config.generated_task_profile_path, payload)
        return GeneratedTaskConfigureResult(
            payload=payload,
            cache_path=config.generated_task_profile_path,
            diagnostics=diagnostics_for_cache_payload(config, payload),
            exit_code=2,
        )

    if result.returncode != 0:
        payload = build_degraded_cache(
            config,
            bundle,
            "unavailable",
            "agent_exit_code",
            f"agent exited with code {result.returncode}",
        )
        write_generated_task_cache(config.generated_task_profile_path, payload)
        return GeneratedTaskConfigureResult(
            payload=payload,
            cache_path=config.generated_task_profile_path,
            diagnostics=diagnostics_for_cache_payload(config, payload),
            exit_code=2,
        )

    try:
        raw_payload = json.loads(
            result.stdout,
            parse_constant=reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        message = getattr(exc, "msg", str(exc))
        payload = build_degraded_cache(
            config,
            bundle,
            "rejected",
            "malformed_json",
            f"agent stdout was not strict JSON: {message}",
        )
    else:
        payload = normalize_agent_generated_payload(config, bundle, raw_payload)

    write_generated_task_cache(config.generated_task_profile_path, payload)
    diagnostics = diagnostics_for_cache_payload(config, payload)
    exit_code = 0 if payload.get("status") == "profile" else 2
    return GeneratedTaskConfigureResult(
        payload=payload,
        cache_path=config.generated_task_profile_path,
        diagnostics=diagnostics,
        exit_code=exit_code,
    )


def build_generated_task_source_prompt(bundle: EvidenceBundle) -> str:
    contract = {
        "return": "JSON only",
        "schema_version": GENERATED_TASK_PROFILE_SCHEMA_VERSION,
        "prompt_version": GENERATED_TASK_PROFILE_PROMPT_VERSION,
        "allowed_statuses": sorted(GENERATED_PROFILE_STATUSES - {"rejected"}),
        "profile_requirements": {
            "status": "profile",
            "confidence": f">= {GENERATED_PROFILE_CONFIDENCE_THRESHOLD}",
            "profile.kind": sorted(SUPPORTED_PROFILE_KINDS),
            "profile.source_paths": "non-empty repo-relative paths from evidence",
            "profile.stable_ids": True,
            "profile.fields": ["id", "title", "status"],
            "profile.status_map": ["done", "runnable"],
        },
        "degradation_requirements": {
            "statuses": sorted(DEGRADED_PROFILE_STATUSES - {"rejected"}),
            "degradation": {
                "reason": "machine-readable reason",
                "message": "human-readable diagnostic",
                "next_action": "safe next action",
            },
        },
        "forbidden": [
            "raw command strings",
            "shell snippets",
            "task_source command adapters",
            "list/next/probe/command/commands/selection_command fields",
        ],
    }
    prompt = {
        "instruction": (
            "Analyze the bounded repository evidence and produce a vibe-loop "
            "task-source profile or a structured degradation result. Do not "
            "invent task state. Return one strict JSON object and no prose."
        ),
        "contract": contract,
        "evidence": bundle.prompt_input_json(),
    }
    return json.dumps(prompt, allow_nan=False, indent=2, sort_keys=True)


def normalize_agent_generated_payload(
    config: VibeConfig,
    bundle: EvidenceBundle,
    raw_payload: object,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        return build_degraded_cache(
            config,
            bundle,
            "rejected",
            "invalid_json_shape",
            "agent JSON must be an object",
        )
    try:
        reject_generated_command_adapters(raw_payload)
    except ValueError as exc:
        return build_degraded_cache(
            config,
            bundle,
            "rejected",
            "forbidden_command_adapter",
            str(exc),
        )

    status = raw_payload.get("status")
    if status == "profile":
        return normalize_profile_payload(config, bundle, raw_payload)
    if status in DEGRADED_PROFILE_STATUSES - {"rejected"}:
        return normalize_degraded_agent_payload(config, bundle, raw_payload, status)
    return build_degraded_cache(
        config,
        bundle,
        "rejected",
        "unsupported_status",
        "agent JSON status must be profile, planning_only, needs_input, or unavailable",
    )


def normalize_profile_payload(
    config: VibeConfig,
    bundle: EvidenceBundle,
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    confidence = parse_confidence(raw_payload.get("confidence"))
    if confidence is None:
        return build_degraded_cache(
            config,
            bundle,
            "rejected",
            "invalid_confidence",
            "profile confidence must be a number between 0 and 1",
        )

    profile = raw_payload.get("profile")
    profile_error = validate_generated_profile(profile, bundle)
    if profile_error is not None:
        return build_degraded_cache(
            config,
            bundle,
            "rejected",
            profile_error[0],
            profile_error[1],
        )

    if confidence < GENERATED_PROFILE_CONFIDENCE_THRESHOLD:
        return build_cache_envelope(
            config,
            bundle,
            status="planning_only",
            confidence=confidence,
            profile=profile,
            degradation={
                "reason": "low_confidence",
                "message": (
                    "agent profile confidence is below the runnable threshold "
                    f"of {GENERATED_PROFILE_CONFIDENCE_THRESHOLD}"
                ),
                "next_action": "review the cache or rerun tasks configure after clarifying task docs",
            },
        )

    if profile.get("stable_ids") is not True:
        return build_cache_envelope(
            config,
            bundle,
            status="planning_only",
            confidence=confidence,
            profile=profile,
            degradation={
                "reason": "unstable_ids",
                "message": "agent profile did not confirm stable task identifiers",
                "next_action": "define stable task IDs in the source or promote explicit config",
            },
        )

    return build_cache_envelope(
        config,
        bundle,
        status="profile",
        confidence=confidence,
        profile=profile,
        degradation=None,
    )


def normalize_degraded_agent_payload(
    config: VibeConfig,
    bundle: EvidenceBundle,
    raw_payload: dict[str, Any],
    status: object,
) -> dict[str, Any]:
    confidence = parse_confidence(raw_payload.get("confidence"))
    profile = raw_payload.get("profile")
    if profile is not None:
        profile_error = validate_generated_profile(profile, bundle, planning_only=True)
        if profile_error is not None:
            profile = None
    degradation = normalize_degradation(raw_payload.get("degradation"), str(status))
    return build_cache_envelope(
        config,
        bundle,
        status=str(status),
        confidence=confidence,
        profile=profile,
        degradation=degradation,
    )


def validate_generated_profile(
    profile: object,
    bundle: EvidenceBundle,
    *,
    planning_only: bool = False,
) -> tuple[str, str] | None:
    if not isinstance(profile, dict):
        return ("missing_profile", "profile status requires a profile object")
    schema_error = validate_profile_schema(profile)
    if schema_error is not None:
        return schema_error
    kind = profile.get("kind")
    if kind not in SUPPORTED_PROFILE_KINDS:
        return (
            "unsupported_profile_kind",
            "profile.kind must be markdown_table, markdown_headings, or markdown_list",
        )
    source_paths = profile.get("source_paths")
    if not is_nonempty_string_list(source_paths):
        return (
            "missing_source_paths",
            "profile.source_paths must be a non-empty array of strings",
        )
    evidence_paths = {file.path for file in bundle.files}
    for source_path in source_paths:
        if Path(source_path).is_absolute() or ".." in Path(source_path).parts:
            return (
                "invalid_source_path",
                "profile.source_paths must be repo-relative evidence paths",
            )
        if source_path not in evidence_paths:
            return (
                "unknown_source_path",
                f"profile.source_paths contains evidence path not collected: {source_path}",
            )
    fields = profile.get("fields")
    if not isinstance(fields, dict):
        return ("missing_fields", "profile.fields must be an object")
    required_fields = ("id", "title", "status")
    missing_fields = [field for field in required_fields if field not in fields]
    if missing_fields:
        return (
            "incomplete_fields",
            f"profile.fields is missing required fields: {', '.join(missing_fields)}",
        )
    for field_name in required_fields:
        mapping = fields.get(field_name)
        if not isinstance(mapping, dict):
            return (
                "invalid_field_mapping",
                f"profile.fields.{field_name} must be an object",
            )
        if profile.get("kind") == "markdown_table":
            column = mapping.get("column")
            if not isinstance(column, str) or not column.strip():
                return (
                    "incomplete_field_mapping",
                    f"profile.fields.{field_name}.column must be a non-empty string",
                )
            if not markdown_table_column_exists(bundle, source_paths, column):
                return (
                    "unknown_field_column",
                    f"profile.fields.{field_name}.column was not found in source evidence: {column}",
                )
        elif not has_nonempty_mapping_value(mapping):
            return (
                "incomplete_field_mapping",
                f"profile.fields.{field_name} must contain a non-empty mapping rule",
            )
    for field_name, mapping in fields.items():
        if field_name in required_fields:
            continue
        if profile.get("kind") == "markdown_table":
            column = mapping.get("column")
            if not isinstance(column, str) or not column.strip():
                return (
                    "incomplete_field_mapping",
                    f"profile.fields.{field_name}.column must be a non-empty string",
                )
            if not markdown_table_column_exists(bundle, source_paths, column):
                return (
                    "unknown_field_column",
                    f"profile.fields.{field_name}.column was not found in source evidence: {column}",
                )
    status_map = profile.get("status_map")
    if not isinstance(status_map, dict):
        return ("missing_status_map", "profile.status_map must be an object")
    for key, value in status_map.items():
        if not is_nonempty_string_list(value):
            return (
                "incomplete_status_map",
                f"profile.status_map.{key} must be a non-empty array of strings",
            )
    for key in ("done", "runnable"):
        if not is_nonempty_string_list(status_map.get(key)):
            return (
                "incomplete_status_map",
                f"profile.status_map.{key} must be a non-empty array of strings",
            )
    if not planning_only and "stable_ids" not in profile:
        return (
            "missing_stable_ids",
            "profile.stable_ids must be present for runnable profiles",
        )
    return None


def validate_profile_schema(profile: dict[str, Any]) -> tuple[str, str] | None:
    unknown_profile_keys = sorted(
        str(key) for key in set(profile) - ALLOWED_PROFILE_KEYS
    )
    if unknown_profile_keys:
        return (
            "unsupported_profile_key",
            f"profile contains unsupported keys: {', '.join(unknown_profile_keys)}",
        )

    fields = profile.get("fields")
    if isinstance(fields, dict):
        unknown_fields = sorted(
            str(key) for key in set(fields) - ALLOWED_PROFILE_FIELD_KEYS
        )
        if unknown_fields:
            return (
                "unsupported_profile_field",
                f"profile.fields contains unsupported fields: {', '.join(unknown_fields)}",
            )
        for field_name, mapping in fields.items():
            if not isinstance(mapping, dict):
                return (
                    "invalid_field_mapping",
                    f"profile.fields.{field_name} must be an object",
                )
            unknown_mapping_keys = sorted(
                str(key) for key in set(mapping) - ALLOWED_FIELD_MAPPING_KEYS
            )
            if unknown_mapping_keys:
                return (
                    "unsupported_field_mapping_key",
                    (
                        f"profile.fields.{field_name} contains unsupported keys: "
                        f"{', '.join(unknown_mapping_keys)}"
                    ),
                )
            mapping_value_error = validate_field_mapping_values(field_name, mapping)
            if mapping_value_error is not None:
                return mapping_value_error

    status_map = profile.get("status_map")
    if isinstance(status_map, dict):
        unknown_statuses = sorted(
            str(key) for key in set(status_map) - ALLOWED_STATUS_MAP_KEYS
        )
        if unknown_statuses:
            return (
                "unsupported_status_map_key",
                f"profile.status_map contains unsupported keys: {', '.join(str(key) for key in unknown_statuses)}",
            )
    return None


def validate_field_mapping_values(
    field_name: object,
    mapping: dict[Any, Any],
) -> tuple[str, str] | None:
    for key, value in mapping.items():
        if key in {"column", "label", "pattern", "prefix"}:
            if not isinstance(value, str) or not value.strip():
                return (
                    "invalid_field_mapping_value",
                    f"profile.fields.{field_name}.{key} must be a non-empty string",
                )
            continue
        if key == "strategy":
            if value not in ALLOWED_FIELD_STRATEGIES:
                return (
                    "invalid_field_mapping_value",
                    f"profile.fields.{field_name}.strategy is not supported: {value}",
                )
            continue
        if key == "none_values":
            if not is_nonempty_string_list(value):
                return (
                    "invalid_field_mapping_value",
                    f"profile.fields.{field_name}.none_values must be a non-empty array of strings",
                )
            continue
        if key == "required" and not isinstance(value, bool):
            return (
                "invalid_field_mapping_value",
                f"profile.fields.{field_name}.required must be a boolean",
            )
    return None


def build_cache_envelope(
    config: VibeConfig,
    bundle: EvidenceBundle,
    *,
    status: str,
    confidence: float | None,
    profile: object,
    degradation: object,
) -> dict[str, Any]:
    source_paths = generated_source_paths(profile)
    fingerprints = source_fingerprints(bundle, source_paths)
    return {
        "schema_version": GENERATED_TASK_PROFILE_SCHEMA_VERSION,
        "prompt_version": GENERATED_TASK_PROFILE_PROMPT_VERSION,
        "status": status,
        "generated_at": generated_timestamp(),
        "agent": {
            "name": agent_name_from_source(config.agent.selection_command_source),
            "selection_command_source": config.agent.selection_command_source,
            "default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "default_policy": AGENT_DEFAULT_POLICY,
        },
        "confidence": confidence,
        "provenance": {
            "repo": str(config.repo),
            "evidence_limit": bundle.limits.to_json(),
            "evidence_file_count": len(bundle.files),
            "skipped_evidence": [skipped.to_json() for skipped in bundle.skipped],
        },
        "source_fingerprints": fingerprints,
        "profile": profile,
        "degradation": degradation,
    }


def build_config_disabled_cache(config: VibeConfig) -> dict[str, Any]:
    return {
        "schema_version": GENERATED_TASK_PROFILE_SCHEMA_VERSION,
        "prompt_version": GENERATED_TASK_PROFILE_PROMPT_VERSION,
        "status": "unavailable",
        "generated_at": generated_timestamp(),
        "agent": {
            "name": "not_run",
            "selection_command_source": config.agent.selection_command_source,
            "default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "default_policy": AGENT_DEFAULT_POLICY,
        },
        "confidence": None,
        "provenance": {
            "repo": str(config.repo),
            "evidence_limit": None,
            "evidence_file_count": 0,
            "skipped_evidence": [],
        },
        "source_fingerprints": [],
        "profile": None,
        "degradation": {
            "reason": "explicit_task_source_config",
            "message": (
                "explicit task_source source settings disable generated discovery "
                f"for the active source: {', '.join(config.task_source.explicit_source_keys)}"
            ),
            "next_action": "remove explicit source settings before generating a cache",
        },
    }


def build_degraded_cache(
    config: VibeConfig,
    bundle: EvidenceBundle,
    status: str,
    reason: str,
    message: str,
) -> dict[str, Any]:
    return build_cache_envelope(
        config,
        bundle,
        status=status,
        confidence=None,
        profile=None,
        degradation={
            "reason": reason,
            "message": message,
            "next_action": "run vibe-loop tasks configure after fixing the diagnostic",
        },
    )


def write_generated_task_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generated_task_cache_report(config: VibeConfig) -> dict[str, object]:
    path = config.generated_task_profile_path
    report: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
        "source_allowed": config.task_source.allows_generated_cache,
        "explicit_source_keys": list(config.task_source.explicit_source_keys),
    }
    if not config.task_source.allows_generated_cache:
        report.update(
            {
                "status": "disabled_by_explicit_task_source",
                "diagnostics": [
                    "explicit task_source source settings disable generated cache as an active source"
                ],
                "next_action": (
                    "fix the explicit task_source source settings or remove them "
                    "before generating a cache"
                ),
            }
        )
        return report
    if not path.exists():
        report.update(
            {
                "status": "missing",
                "diagnostics": ["generated task-source cache is missing"],
                "next_action": f"vibe-loop tasks configure --repo {config.repo}",
            }
        )
        return report
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report.update(
            {
                "status": "invalid",
                "diagnostics": [f"generated task-source cache cannot be read: {exc}"],
                "next_action": f"vibe-loop tasks configure --repo {config.repo}",
            }
        )
        return report
    if not isinstance(payload, dict):
        report.update(
            {
                "status": "invalid",
                "diagnostics": [
                    "generated task-source cache must contain a JSON object"
                ],
                "next_action": f"vibe-loop tasks configure --repo {config.repo}",
            }
        )
        return report

    diagnostics = diagnostics_for_cache_payload(config, payload)
    report.update(
        {
            "status": payload.get("status", "invalid"),
            "schema_version": payload.get("schema_version"),
            "prompt_version": payload.get("prompt_version"),
            "confidence": payload.get("confidence"),
            "generated_at": payload.get("generated_at"),
            "diagnostics": list(diagnostics),
            "degradation": payload.get("degradation"),
            "source_fingerprints": payload.get("source_fingerprints", []),
            "next_action": cache_next_action(config, payload),
        }
    )
    return report


def read_only_generated_cache_message(config: VibeConfig) -> str:
    report = generated_task_cache_report(config)
    status = report.get("status")
    diagnostics = report.get("diagnostics") or []
    parts = [
        f"generated task-source cache status={status}",
        f"path={report.get('path')}",
    ]
    if diagnostics:
        parts.append(f"diagnostic={diagnostics[0]}")
    next_action = report.get("next_action")
    if next_action:
        parts.append(f"next_action={next_action}")
    return "; ".join(parts)


def read_only_generated_cache_notice(config: VibeConfig) -> str | None:
    if not config.task_source.allows_generated_cache:
        return None
    report = generated_task_cache_report(config)
    if not report.get("exists"):
        return None
    status = report.get("status")
    diagnostics = report.get("diagnostics") or []
    if status == "profile" and not diagnostics:
        return None
    return read_only_generated_cache_message(config)


def diagnostics_for_cache_payload(
    config: VibeConfig,
    payload: dict[str, Any],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if payload.get("schema_version") != GENERATED_TASK_PROFILE_SCHEMA_VERSION:
        diagnostics.append("unsupported generated cache schema_version")
    if payload.get("prompt_version") != GENERATED_TASK_PROFILE_PROMPT_VERSION:
        diagnostics.append("unsupported generated cache prompt_version")
    status = payload.get("status")
    if status not in GENERATED_PROFILE_STATUSES:
        diagnostics.append("unsupported generated cache status")
    if not config.task_source.allows_generated_cache:
        diagnostics.append(
            "explicit task_source source settings disable generated cache as an active source"
        )
    degradation = payload.get("degradation")
    if status in DEGRADED_PROFILE_STATUSES and isinstance(degradation, dict):
        reason = degradation.get("reason")
        message = degradation.get("message")
        if reason or message:
            diagnostics.append(
                ": ".join(str(item) for item in (reason, message) if item)
            )
    if status == "profile":
        confidence = parse_confidence(payload.get("confidence"))
        if confidence is None:
            diagnostics.append("profile confidence is missing or invalid")
        elif confidence < GENERATED_PROFILE_CONFIDENCE_THRESHOLD:
            diagnostics.append("profile confidence is below runnable threshold")
    return tuple(diagnostics)


def cache_next_action(config: VibeConfig, payload: dict[str, Any]) -> str:
    degradation = payload.get("degradation")
    if isinstance(degradation, dict) and degradation.get("next_action"):
        return str(degradation["next_action"])
    if payload.get("status") == "profile":
        return "wait for generated-cache runtime loading or promote explicit task_source config"
    return f"vibe-loop tasks configure --repo {config.repo}"


def normalize_degradation(value: object, status: str) -> dict[str, str]:
    if not isinstance(value, dict):
        return {
            "reason": status,
            "message": f"agent returned {status} without a degradation object",
            "next_action": "inspect task docs and rerun tasks configure",
        }
    reason = str(value.get("reason") or status)
    message = str(value.get("message") or reason)
    next_action = str(
        value.get("next_action") or "inspect task docs and rerun tasks configure"
    )
    return {
        "reason": reason,
        "message": message,
        "next_action": next_action,
    }


def parse_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        return None
    return confidence


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def generated_source_paths(profile: object) -> tuple[str, ...]:
    if not isinstance(profile, dict):
        return ()
    source_paths = profile.get("source_paths")
    if not is_nonempty_string_list(source_paths):
        return ()
    return tuple(source_paths)


def source_fingerprints(
    bundle: EvidenceBundle,
    source_paths: tuple[str, ...],
) -> list[dict[str, object]]:
    source_path_set = set(source_paths)
    if not source_path_set:
        return [file.fingerprint_json() for file in bundle.files]
    return [
        file.fingerprint_json() for file in bundle.files if file.path in source_path_set
    ]


def is_nonempty_string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def has_nonempty_mapping_value(mapping: dict[object, object]) -> bool:
    return any(isinstance(value, str) and value.strip() for value in mapping.values())


def markdown_table_column_exists(
    bundle: EvidenceBundle,
    source_paths: object,
    column: str,
) -> bool:
    if not is_nonempty_string_list(source_paths):
        return False
    source_path_set = set(source_paths)
    for file in bundle.files:
        if file.path not in source_path_set:
            continue
        lines = file.content.splitlines()
        for index, line in enumerate(lines[:-1]):
            cells = split_markdown_row(line)
            if column in cells and is_markdown_separator_row(
                split_markdown_row(lines[index + 1])
            ):
                return True
    return False


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":", " "} for cell in cells)


def agent_name_from_source(source: str) -> str:
    if source.startswith("auto:"):
        return source.split(":", 2)[1]
    if source == "explicit":
        return "custom"
    if source.startswith("unresolved:"):
        return "unresolved"
    return "unknown"


def generated_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
