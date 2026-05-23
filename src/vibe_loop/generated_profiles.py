from __future__ import annotations

import dataclasses
import json
import math
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
    AgentResolutionError,
    TaskSourceConfig,
    VibeConfig,
    reject_generated_command_adapters,
    shell_quote,
    prepare_shell_command,
)
from vibe_loop.generated_discovery import (
    EvidenceBundle,
    collect_generated_discovery_evidence,
)
from vibe_loop.retry import (
    retry_subprocess_run,
)


GENERATED_PROFILE_CONFIDENCE_THRESHOLD = 0.7
GENERATED_PROFILE_STATUSES = frozenset(
    {"profile", "planning_only", "needs_input", "unavailable", "rejected"}
)
DEGRADED_PROFILE_STATUSES = frozenset(
    {"planning_only", "needs_input", "unavailable", "rejected"}
)
PLANNING_PROFILE_ERROR_REASONS = frozenset(
    {"missing_stable_ids", "missing_status_map", "incomplete_status_map"}
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
        "approval_state",
        "dependencies",
        "design_refs",
        "evidence",
        "id",
        "paths",
        "priority",
        "requirement_ids",
        "resources",
        "scope",
        "section",
        "source_fingerprints",
        "spec_paths",
        "status",
        "title",
    }
)
ALLOWED_FIELD_MAPPING_KEYS = frozenset(
    {"column", "label", "none_values", "pattern", "prefix", "required", "strategy"}
)
ALLOWED_FIELD_STRATEGIES = frozenset(
    {"checkbox_status", "first_sentence", "full_text", "heading_text", "label_value"}
)
ALLOWED_STATUS_MAP_KEYS = frozenset({"blocked", "done", "runnable"})
PROFILE_FIELD_TOML_ORDER = (
    "id",
    "title",
    "status",
    "priority",
    "dependencies",
    "resources",
    "paths",
    "requirement_ids",
    "spec_paths",
    "design_refs",
    "approval_state",
    "source_fingerprints",
    "scope",
    "acceptance",
    "evidence",
    "section",
)
PROFILE_STATUS_MAP_TOML_ORDER = ("done", "runnable", "blocked")


@dataclasses.dataclass(frozen=True)
class GeneratedTaskConfigureResult:
    payload: dict[str, Any]
    cache_path: Path
    diagnostics: tuple[str, ...]
    exit_code: int
    cache_action: str = "wrote"
    dry_run: bool = False
    wrote_cache: bool = True
    promotion_toml: str | None = None
    promotion_diagnostics: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.payload.get("status"),
            "cache_path": str(self.cache_path),
            "exit_code": self.exit_code,
            "cache_action": self.cache_action,
            "dry_run": self.dry_run,
            "wrote_cache": self.wrote_cache,
            "diagnostics": list(self.diagnostics),
            "promotion_toml": self.promotion_toml,
            "promotion_diagnostics": list(self.promotion_diagnostics),
            "cache": self.payload,
        }


@dataclasses.dataclass(frozen=True)
class RuntimeTaskSourceResolution:
    task_source: TaskSourceConfig
    origin: str
    diagnostics: tuple[str, ...] = ()
    cache_path: Path | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "diagnostics": list(self.diagnostics),
            "cache_path": str(self.cache_path) if self.cache_path else None,
            "task_source": self.task_source.to_json(),
        }


class GeneratedTaskSourceRuntimeError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        origin: str,
        diagnostics: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.origin = origin
        self.diagnostics = diagnostics


def resolve_runtime_task_source(config: VibeConfig) -> RuntimeTaskSourceResolution:
    if not config.task_source.allows_generated_cache:
        return RuntimeTaskSourceResolution(
            task_source=config.task_source,
            origin=explicit_task_source_origin(config.task_source),
            diagnostics=(),
            cache_path=None,
        )

    report = generated_task_cache_report(config)
    if not report.get("exists"):
        return RuntimeTaskSourceResolution(
            task_source=config.task_source,
            origin="default_markdown_discovery",
            diagnostics=tuple(str(item) for item in report.get("diagnostics", [])),
            cache_path=config.generated_task_profile_path,
        )

    payload = load_generated_task_cache_payload(config)
    status = payload.get("status")
    origin = str(report.get("origin") or cache_origin(config, payload, ()))
    diagnostics = tuple(str(item) for item in report.get("diagnostics", []))
    structural_diagnostics = runtime_cache_structural_diagnostics(payload)
    if structural_diagnostics:
        raise GeneratedTaskSourceRuntimeError(
            read_only_generated_cache_message(
                config,
                extra_diagnostics=structural_diagnostics,
                origin="invalid_generated_cache",
            ),
            origin="invalid_generated_cache",
            diagnostics=structural_diagnostics,
        )
    if report.get("stale_reasons"):
        raise GeneratedTaskSourceRuntimeError(
            read_only_generated_cache_message(config),
            origin=origin,
            diagnostics=diagnostics,
        )
    if status != "profile":
        return RuntimeTaskSourceResolution(
            task_source=config.task_source,
            origin="default_markdown_discovery",
            diagnostics=diagnostics,
            cache_path=config.generated_task_profile_path,
        )

    profile_diagnostics = runtime_profile_diagnostics(config, payload)
    if profile_diagnostics:
        raise GeneratedTaskSourceRuntimeError(
            read_only_generated_cache_message(
                config,
                extra_diagnostics=profile_diagnostics,
                origin="invalid_generated_cache",
                next_action=f"vibe-loop tasks configure --repo {config.repo}",
            ),
            origin="invalid_generated_cache",
            diagnostics=profile_diagnostics,
        )

    profile = payload.get("profile")
    assert isinstance(profile, dict)
    return RuntimeTaskSourceResolution(
        task_source=dataclasses.replace(
            config.task_source,
            type="markdown-profile",
            profile=profile,
            runnable_statuses=runtime_runnable_statuses(config, profile),
        ),
        origin="generated_cache",
        diagnostics=(),
        cache_path=config.generated_task_profile_path,
    )


def runtime_task_source_report(config: VibeConfig) -> dict[str, object]:
    try:
        resolution = resolve_runtime_task_source(config)
    except GeneratedTaskSourceRuntimeError as exc:
        return {
            "origin": exc.origin,
            "usable": False,
            "diagnostics": list(exc.diagnostics) or [str(exc)],
            "cache_path": str(config.generated_task_profile_path),
        }
    validation_diagnostics = runtime_task_source_validation_diagnostics(
        config,
        resolution,
    )
    return {
        **resolution.to_json(),
        "usable": not validation_diagnostics,
        "diagnostics": [
            *resolution.diagnostics,
            *validation_diagnostics,
        ],
    }


def explicit_task_source_origin(task_source: TaskSourceConfig) -> str:
    if (
        task_source.type == "command"
        or task_source.list_command
        or task_source.next_command
        or task_source.probe_command
    ):
        return "command_output"
    return "explicit_config"


def runtime_task_source_validation_diagnostics(
    config: VibeConfig,
    resolution: RuntimeTaskSourceResolution,
) -> tuple[str, ...]:
    try:
        from vibe_loop.tasks import build_task_source

        source = build_task_source(config.repo, resolution.task_source)
        if resolution.origin != "command_output":
            source.list_tasks()
    except (OSError, ValueError) as exc:
        return (f"task source is not usable: {exc}",)
    return ()


def load_generated_task_cache_payload(config: VibeConfig) -> dict[str, Any]:
    path = config.generated_task_profile_path
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise GeneratedTaskSourceRuntimeError(
            read_only_generated_cache_message(config),
            origin="invalid_generated_cache",
            diagnostics=(f"generated task-source cache cannot be read: {exc}",),
        ) from exc
    if not isinstance(payload, dict):
        raise GeneratedTaskSourceRuntimeError(
            read_only_generated_cache_message(config),
            origin="invalid_generated_cache",
            diagnostics=("generated task-source cache must contain a JSON object",),
        )
    return payload


def runtime_cache_structural_diagnostics(
    payload: dict[str, Any],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if payload.get("schema_version") != GENERATED_TASK_PROFILE_SCHEMA_VERSION:
        diagnostics.append("unsupported generated cache schema_version")
    if payload.get("prompt_version") != GENERATED_TASK_PROFILE_PROMPT_VERSION:
        diagnostics.append("unsupported generated cache prompt_version")
    if payload.get("status") not in GENERATED_PROFILE_STATUSES:
        diagnostics.append("unsupported generated cache status")
    try:
        reject_generated_command_adapters(payload)
    except ValueError as exc:
        diagnostics.append(str(exc))
    return tuple(diagnostics)


def runtime_profile_diagnostics(
    config: VibeConfig,
    payload: dict[str, Any],
) -> tuple[str, ...]:
    diagnostics = [
        diagnostic
        for diagnostic in diagnostics_for_cache_payload(config, payload)
        if diagnostic not in runtime_cache_structural_diagnostics(payload)
    ]
    bundle = collect_generated_discovery_evidence(
        config.repo,
        state_dir=config.state_dir,
    )
    profile_error = validate_generated_profile(payload.get("profile"), bundle)
    if profile_error is not None:
        diagnostics.append(": ".join(profile_error))
        return tuple(diagnostics)
    profile = payload.get("profile")
    if isinstance(profile, dict):
        diagnostics.extend(runtime_profile_parser_diagnostics(config, profile))
    if payload.get("status") != "profile":
        diagnostics.append("generated cache status is not runnable profile")
    return tuple(diagnostics)


def runtime_profile_parser_diagnostics(
    config: VibeConfig,
    profile: dict[str, Any],
) -> tuple[str, ...]:
    try:
        from vibe_loop.tasks import build_task_source

        task_source = dataclasses.replace(
            config.task_source,
            type="markdown-profile",
            profile=profile,
            runnable_statuses=runtime_runnable_statuses(config, profile),
        )
        build_task_source(config.repo, task_source).list_tasks()
    except (OSError, ValueError) as exc:
        return (f"generated profile cannot parse task source: {exc}",)
    return ()


def runtime_runnable_statuses(
    config: VibeConfig,
    profile: dict[str, Any],
) -> tuple[str, ...]:
    if config.task_source.is_explicit("runnable_statuses"):
        return config.task_source.runnable_statuses
    status_map = profile.get("status_map")
    if isinstance(status_map, dict):
        runnable = status_map.get("runnable")
        if is_nonempty_string_list(runnable):
            return tuple(runnable)
    return config.task_source.runnable_statuses


def configure_generated_task_source(
    config: VibeConfig,
    *,
    dry_run: bool = False,
    force_refresh: bool = False,
    write_cache: bool = True,
) -> GeneratedTaskConfigureResult:
    if not config.task_source.allows_generated_cache:
        payload = build_config_disabled_cache(config)
        return build_configure_result(
            config,
            payload=payload,
            exit_code=2,
            cache_action="disabled",
            dry_run=dry_run,
            wrote_cache=False,
        )

    if not dry_run and not force_refresh:
        reusable = reusable_generated_profile_cache(config)
        if reusable is not None:
            return reusable

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
        return finish_configure_result(
            config,
            payload,
            dry_run=dry_run,
            write_cache=write_cache,
        )

    prompt = build_generated_task_source_prompt(bundle)
    command_str = command_template.format(prompt=shell_quote(prompt))
    cmd, use_shell = prepare_shell_command(command_str)
    try:
        result = retry_subprocess_run(
            cmd,
            cwd=config.repo,
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            on_retry=_configure_retry_callback,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = build_degraded_cache(
            config,
            bundle,
            "unavailable",
            "agent_execution_failed",
            exc.__class__.__name__,
        )
        return finish_configure_result(
            config,
            payload,
            dry_run=dry_run,
            write_cache=write_cache,
        )

    if result.returncode != 0:
        payload = build_degraded_cache(
            config,
            bundle,
            "unavailable",
            "agent_exit_code",
            f"agent exited with code {result.returncode}",
        )
        return finish_configure_result(
            config,
            payload,
            dry_run=dry_run,
            write_cache=write_cache,
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

    return finish_configure_result(
        config,
        payload,
        dry_run=dry_run,
        write_cache=write_cache,
    )


def reusable_generated_profile_cache(
    config: VibeConfig,
) -> GeneratedTaskConfigureResult | None:
    report = generated_task_cache_report(config)
    if not report.get("exists"):
        return None
    if report.get("status") != "profile" or not report.get("fresh"):
        return None
    if report.get("diagnostics"):
        return None
    try:
        payload = load_generated_task_cache_payload(config)
    except GeneratedTaskSourceRuntimeError:
        return None
    diagnostics = (
        *runtime_cache_structural_diagnostics(payload),
        *runtime_profile_diagnostics(config, payload),
    )
    if diagnostics:
        return None
    return build_configure_result(
        config,
        payload=payload,
        diagnostics=diagnostics,
        exit_code=0,
        cache_action="reused",
        dry_run=False,
        wrote_cache=False,
    )


def finish_configure_result(
    config: VibeConfig,
    payload: dict[str, Any],
    *,
    dry_run: bool,
    write_cache: bool,
) -> GeneratedTaskConfigureResult:
    if write_cache and not dry_run:
        write_generated_task_cache(config.generated_task_profile_path, payload)
    diagnostics = diagnostics_for_cache_payload(config, payload)
    exit_code = 0 if payload.get("status") == "profile" else 2
    if dry_run:
        cache_action = "dry_run"
    elif write_cache:
        cache_action = "wrote"
    else:
        cache_action = "preview"
    return build_configure_result(
        config,
        payload=payload,
        diagnostics=diagnostics,
        exit_code=exit_code,
        cache_action=cache_action,
        dry_run=dry_run,
        wrote_cache=write_cache and not dry_run,
    )


def build_configure_result(
    config: VibeConfig,
    *,
    payload: dict[str, Any],
    diagnostics: tuple[str, ...] | None = None,
    exit_code: int | None = None,
    cache_action: str,
    dry_run: bool,
    wrote_cache: bool,
) -> GeneratedTaskConfigureResult:
    if diagnostics is None:
        diagnostics = diagnostics_for_cache_payload(config, payload)
    if exit_code is None:
        exit_code = 0 if payload.get("status") == "profile" else 2
    promotion_diagnostics = generated_profile_promotion_diagnostics(config, payload)
    promotion_toml = (
        None
        if promotion_diagnostics
        else generated_profile_promotion_toml(config, payload)
    )
    return GeneratedTaskConfigureResult(
        payload=payload,
        cache_path=config.generated_task_profile_path,
        diagnostics=diagnostics,
        exit_code=exit_code,
        cache_action=cache_action,
        dry_run=dry_run,
        wrote_cache=wrote_cache,
        promotion_toml=promotion_toml,
        promotion_diagnostics=promotion_diagnostics,
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
            "profile.optional_traceability_fields": [
                "requirement_ids",
                "spec_paths",
                "design_refs",
                "approval_state",
                "source_fingerprints",
            ],
            "profile.field_strategies": sorted(ALLOWED_FIELD_STRATEGIES),
            "profile.status_map": ["done", "runnable"],
        },
        "degradation_requirements": {
            "statuses": sorted(DEGRADED_PROFILE_STATUSES - {"rejected"}),
            "degradation": {
                "reason": "machine-readable reason",
                "message": "human-readable diagnostic",
                "next_action": "safe next action",
                "missing_inputs": "optional array of missing user decisions or source data",
                "proposed_config": "optional non-executable task_source profile sketch",
                "candidate_sources": "optional array of candidate source paths or objects",
                "questions": "optional array of questions for the user",
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
        if profile_error[0] in PLANNING_PROFILE_ERROR_REASONS:
            planning_error = validate_generated_profile(
                profile,
                bundle,
                planning_only=True,
            )
            if planning_error is None:
                return build_cache_envelope(
                    config,
                    bundle,
                    status="planning_only",
                    confidence=confidence,
                    profile=profile,
                    degradation=planning_degradation_for_profile_error(
                        profile_error[0],
                        profile_error[1],
                        profile,
                    ),
                )
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
            degradation=build_planning_degradation(
                "low_confidence",
                (
                    "agent profile confidence is below the runnable threshold "
                    f"of {GENERATED_PROFILE_CONFIDENCE_THRESHOLD}"
                ),
                "review the cache or rerun tasks configure after clarifying task docs",
                missing_inputs=("higher-confidence task-source evidence",),
                proposed_config=proposed_task_source_config(profile),
            ),
        )

    if profile.get("stable_ids") is not True:
        return build_cache_envelope(
            config,
            bundle,
            status="planning_only",
            confidence=confidence,
            profile=profile,
            degradation=build_planning_degradation(
                "unstable_ids",
                "agent profile did not confirm stable task identifiers",
                "define stable task IDs in the source or promote explicit config",
                missing_inputs=("stable task identifiers",),
                proposed_config=proposed_task_source_config(profile),
            ),
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
    profile_error: tuple[str, str] | None = None
    if profile is not None:
        profile_error = validate_generated_profile(profile, bundle, planning_only=True)
        if profile_error is not None:
            profile = None
    degradation = normalize_degradation(
        raw_payload.get("degradation"),
        str(status),
        raw_payload,
    )
    if profile_error is not None:
        degradation["profile_error"] = {
            "reason": profile_error[0],
            "message": profile_error[1],
        }
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
    if fields is None and planning_only:
        fields = {}
    elif not isinstance(fields, dict):
        return ("missing_fields", "profile.fields must be an object")
    required_fields = ("id", "title", "status")
    missing_fields = [field for field in required_fields if field not in fields]
    if missing_fields and not planning_only:
        return (
            "incomplete_fields",
            f"profile.fields is missing required fields: {', '.join(missing_fields)}",
        )
    for field_name in required_fields:
        if field_name not in fields and planning_only:
            continue
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
        elif not has_nonempty_mapping_value(mapping, field_name):
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
    if status_map is None and planning_only:
        status_map = {}
    elif not isinstance(status_map, dict):
        return ("missing_status_map", "profile.status_map must be an object")
    for key, value in status_map.items():
        if planning_only and key in {"done", "runnable"} and value == []:
            continue
        if not is_nonempty_string_list(value):
            return (
                "incomplete_status_map",
                f"profile.status_map.{key} must be a non-empty array of strings",
            )
    if not planning_only:
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
            checkbox_strategy_error = validate_checkbox_status_strategy(
                profile.get("kind"),
                field_name,
                mapping,
            )
            if checkbox_strategy_error is not None:
                return checkbox_strategy_error

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


def validate_checkbox_status_strategy(
    kind: object,
    field_name: object,
    mapping: dict[Any, Any],
) -> tuple[str, str] | None:
    if mapping.get("strategy") != "checkbox_status":
        return None
    if field_name != "status":
        return (
            "invalid_field_mapping_value",
            f"profile.fields.{field_name}.checkbox_status requires the status field",
        )
    if kind != "markdown_list":
        return (
            "invalid_field_mapping_value",
            "profile.fields.status.checkbox_status requires markdown_list",
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
    if mapping.get("strategy") == "label_value" and not (
        isinstance(mapping.get("label"), str) and mapping.get("label", "").strip()
    ):
        return (
            "invalid_field_mapping_value",
            f"profile.fields.{field_name}.label_value requires label",
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
            "name": agent_name_from_config(config),
            "kind": config.agent.agent_kind,
            "prompt_dialect": config.agent.prompt_dialect,
            "prompt_dialect_source": config.agent.prompt_dialect_source,
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
            "kind": config.agent.agent_kind,
            "prompt_dialect": config.agent.prompt_dialect,
            "prompt_dialect_source": config.agent.prompt_dialect_source,
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
        degradation=build_planning_degradation(
            reason,
            message,
            "run vibe-loop tasks configure after fixing the diagnostic",
            missing_inputs=missing_inputs_for_degraded_cache(reason, bundle),
        ),
    )


def write_generated_task_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generated_profile_promotion_diagnostics(
    config: VibeConfig,
    payload: dict[str, Any],
) -> tuple[str, ...]:
    if payload.get("status") != "profile":
        return ("generated cache status is not a promotable profile",)
    diagnostics = (
        *runtime_cache_structural_diagnostics(payload),
        *runtime_profile_diagnostics(config, payload),
    )
    return diagnostics


def generated_profile_promotion_toml(
    config: VibeConfig,
    payload: dict[str, Any],
) -> str | None:
    if payload.get("status") != "profile":
        return None
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        return None
    lines = [
        "[task_source]",
        'type = "markdown-profile"',
    ]
    if config.task_source.is_explicit("runnable_statuses"):
        lines.append(
            f"runnable_statuses = {toml_string_array(config.task_source.runnable_statuses)}"
        )
    lines.extend(
        [
            "",
            "[task_source.profile]",
            f"kind = {toml_value(profile.get('kind'))}",
            f"source_paths = {toml_string_array(profile.get('source_paths'))}",
        ]
    )
    if "stable_ids" in profile:
        lines.append(f"stable_ids = {toml_value(profile.get('stable_ids'))}")

    fields = profile.get("fields")
    if isinstance(fields, dict):
        for field_name in ordered_profile_keys(fields, PROFILE_FIELD_TOML_ORDER):
            mapping = fields.get(field_name)
            if not isinstance(mapping, dict):
                continue
            lines.extend(["", f"[task_source.profile.fields.{field_name}]"])
            for key in ordered_profile_keys(
                mapping,
                (
                    "column",
                    "label",
                    "pattern",
                    "prefix",
                    "strategy",
                    "required",
                    "none_values",
                ),
            ):
                if key == "none_values":
                    lines.append(f"{key} = {toml_string_array(mapping[key])}")
                    continue
                lines.append(f"{key} = {toml_value(mapping[key])}")

    status_map = profile.get("status_map")
    if isinstance(status_map, dict):
        lines.extend(["", "[task_source.profile.status_map]"])
        for key in ordered_profile_keys(status_map, PROFILE_STATUS_MAP_TOML_ORDER):
            lines.append(f"{key} = {toml_string_array(status_map[key])}")

    return "\n".join(lines) + "\n"


def ordered_profile_keys(
    value: dict[str, Any],
    preferred: tuple[str, ...],
) -> tuple[str, ...]:
    preferred_keys = [key for key in preferred if key in value]
    extra_keys = sorted(str(key) for key in value if str(key) not in preferred)
    return tuple(preferred_keys + extra_keys)


def toml_string_array(value: object) -> str:
    if not (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("promotion TOML expected a non-empty string array")
    return "[" + ", ".join(toml_value(item) for item in value) + "]"


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    raise ValueError(f"promotion TOML cannot represent value: {value!r}")


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
                "origin": "explicit_config",
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
                "origin": "no_usable_source",
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
                "origin": "invalid_generated_cache",
                "diagnostics": [f"generated task-source cache cannot be read: {exc}"],
                "next_action": f"vibe-loop tasks configure --repo {config.repo}",
            }
        )
        return report
    if not isinstance(payload, dict):
        report.update(
            {
                "status": "invalid",
                "origin": "invalid_generated_cache",
                "diagnostics": [
                    "generated task-source cache must contain a JSON object"
                ],
                "next_action": f"vibe-loop tasks configure --repo {config.repo}",
            }
        )
        return report

    stale_reasons = cache_stale_reasons(config, payload)
    diagnostics = diagnostics_for_cache_payload(
        config,
        payload,
        stale_reasons=stale_reasons,
    )
    degradation = payload.get("degradation")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    report.update(
        {
            "status": payload.get("status", "invalid"),
            "origin": cache_origin(config, payload, stale_reasons),
            "fresh": not stale_reasons,
            "stale_reasons": list(stale_reasons),
            "schema_version": payload.get("schema_version"),
            "prompt_version": payload.get("prompt_version"),
            "confidence": payload.get("confidence"),
            "generated_at": payload.get("generated_at"),
            "diagnostics": list(diagnostics),
            "degradation": degradation,
            "missing_inputs": degradation_value(degradation, "missing_inputs", []),
            "proposed_config": degradation_value(degradation, "proposed_config", None),
            "candidate_sources": degradation_value(
                degradation,
                "candidate_sources",
                [],
            ),
            "questions": degradation_value(degradation, "questions", []),
            "source_fingerprints": payload.get("source_fingerprints", []),
            "skipped_evidence": provenance.get("skipped_evidence", []),
            "evidence_file_count": provenance.get("evidence_file_count"),
            "next_action": cache_next_action(config, payload, stale_reasons),
        }
    )
    return report


def read_only_generated_cache_message(
    config: VibeConfig,
    *,
    extra_diagnostics: tuple[str, ...] = (),
    origin: str | None = None,
    next_action: str | None = None,
) -> str:
    report = generated_task_cache_report(config)
    status = report.get("status")
    message_origin = origin or report.get("origin")
    diagnostics = [*(report.get("diagnostics") or []), *extra_diagnostics]
    parts = [
        f"generated task-source cache status={status}",
        f"origin={message_origin}",
        f"path={report.get('path')}",
    ]
    if diagnostics:
        parts.append(
            "diagnostics=" + " | ".join(str(diagnostic) for diagnostic in diagnostics)
        )
    action = next_action or report.get("next_action")
    if action:
        parts.append(f"next_action={action}")
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
    *,
    stale_reasons: tuple[str, ...] | None = None,
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
    active_stale_reasons = (
        stale_reasons
        if stale_reasons is not None
        else cache_stale_reasons(config, payload)
    )
    for reason in active_stale_reasons:
        diagnostics.append(f"generated cache is stale: {reason}")
    degradation = payload.get("degradation")
    if status in DEGRADED_PROFILE_STATUSES and isinstance(degradation, dict):
        reason = degradation.get("reason")
        message = degradation.get("message")
        if reason or message:
            diagnostics.append(
                ": ".join(str(item) for item in (reason, message) if item)
            )
        missing_inputs = normalize_string_list(degradation.get("missing_inputs"))
        if missing_inputs:
            diagnostics.append(f"missing inputs: {', '.join(missing_inputs)}")
    if status == "profile":
        confidence = parse_confidence(payload.get("confidence"))
        if confidence is None:
            diagnostics.append("profile confidence is missing or invalid")
        elif confidence < GENERATED_PROFILE_CONFIDENCE_THRESHOLD:
            diagnostics.append("profile confidence is below runnable threshold")
    return tuple(diagnostics)


def cache_next_action(
    config: VibeConfig,
    payload: dict[str, Any],
    stale_reasons: tuple[str, ...] | None = None,
) -> str:
    active_stale_reasons = (
        stale_reasons
        if stale_reasons is not None
        else cache_stale_reasons(config, payload)
    )
    if active_stale_reasons:
        return f"vibe-loop tasks configure --repo {config.repo}"
    degradation = payload.get("degradation")
    if isinstance(degradation, dict) and degradation.get("next_action"):
        return str(degradation["next_action"])
    if payload.get("status") == "profile":
        return "generated cache is active for runtime task discovery"
    return f"vibe-loop tasks configure --repo {config.repo}"


def normalize_degradation(
    value: object,
    status: str,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_message = status
    if not isinstance(value, dict):
        default_message = f"agent returned {status} without a degradation object"
        value = {}
    reason = str(value.get("reason") or status)
    message = str(value.get("message") or default_message)
    next_action = str(
        value.get("next_action") or "inspect task docs and rerun tasks configure"
    )
    result: dict[str, Any] = {
        "reason": reason,
        "message": message,
        "next_action": next_action,
    }
    for key in ("missing_inputs", "questions"):
        items = normalize_string_list(
            value.get(key) if key in value else (raw_payload or {}).get(key)
        )
        if items:
            result[key] = items
    proposed_config = normalize_diagnostic_object(
        value.get("proposed_config")
        if "proposed_config" in value
        else (raw_payload or {}).get("proposed_config")
    )
    if proposed_config is not None:
        result["proposed_config"] = proposed_config
    candidate_sources = normalize_diagnostic_json(
        value.get("candidate_sources")
        if "candidate_sources" in value
        else (raw_payload or {}).get("candidate_sources")
    )
    if candidate_sources is not None:
        result["candidate_sources"] = candidate_sources
    return result


def build_planning_degradation(
    reason: str,
    message: str,
    next_action: str,
    *,
    missing_inputs: tuple[str, ...] = (),
    proposed_config: object = None,
    candidate_sources: object = None,
    questions: tuple[str, ...] = (),
) -> dict[str, Any]:
    degradation: dict[str, Any] = {
        "reason": reason,
        "message": message,
        "next_action": next_action,
    }
    normalized_missing = normalize_string_list(list(missing_inputs))
    if normalized_missing:
        degradation["missing_inputs"] = normalized_missing
    normalized_questions = normalize_string_list(list(questions))
    if normalized_questions:
        degradation["questions"] = normalized_questions
    normalized_config = normalize_diagnostic_object(proposed_config)
    if normalized_config is not None:
        degradation["proposed_config"] = normalized_config
    normalized_sources = normalize_diagnostic_json(candidate_sources)
    if normalized_sources is not None:
        degradation["candidate_sources"] = normalized_sources
    return degradation


def planning_degradation_for_profile_error(
    reason: str,
    message: str,
    profile: object,
) -> dict[str, Any]:
    normalized_reason = reason
    missing_inputs: tuple[str, ...]
    if reason == "missing_stable_ids":
        normalized_reason = "unstable_ids"
        missing_inputs = ("stable task identifiers",)
    elif reason in {"missing_status_map", "incomplete_status_map"}:
        normalized_reason = "unmapped_statuses"
        missing_inputs = ("status mapping for runnable and done tasks",)
    else:
        missing_inputs = ("runnable task-source profile requirements",)
    return build_planning_degradation(
        normalized_reason,
        message,
        "review the planning cache, clarify the missing task-source inputs, and rerun tasks configure",
        missing_inputs=missing_inputs,
        proposed_config=proposed_task_source_config(profile),
    )


def proposed_task_source_config(profile: object) -> dict[str, object] | None:
    if not isinstance(profile, dict):
        return None
    return {
        "task_source": {
            "type": "markdown-profile",
            "profile": profile,
        }
    }


def missing_inputs_for_degraded_cache(
    reason: str,
    bundle: EvidenceBundle,
) -> tuple[str, ...]:
    missing: list[str] = []
    if reason == "agent_unavailable":
        missing.append("agent.selection_command")
    elif reason in {"agent_execution_failed", "agent_exit_code"}:
        missing.append("successful planning agent run")
    elif reason in {"malformed_json", "invalid_json_shape", "unsupported_status"}:
        missing.append("valid strict JSON task-source response")
    if not bundle.files:
        missing.append("repo-local task source evidence")
    return tuple(missing)


def cache_origin(
    config: VibeConfig,
    payload: dict[str, Any],
    stale_reasons: tuple[str, ...],
) -> str:
    if not config.task_source.allows_generated_cache:
        return "explicit_config"
    if stale_reasons:
        return "stale_generated_cache"
    status = payload.get("status")
    if status == "profile":
        return "generated_cache"
    if status == "planning_only":
        return "planning_only_cache"
    if status == "needs_input":
        return "needs_input_cache"
    if status == "unavailable":
        return "unavailable_cache"
    if status == "rejected":
        return "rejected_cache"
    return "no_usable_source"


def cache_stale_reasons(config: VibeConfig, payload: dict[str, Any]) -> tuple[str, ...]:
    if not config.task_source.allows_generated_cache:
        return ()
    status = payload.get("status")
    if status not in GENERATED_PROFILE_STATUSES:
        return ()
    fingerprints = payload.get("source_fingerprints")
    if not isinstance(fingerprints, list):
        return ("source_fingerprints is missing or invalid",)
    bundle = collect_generated_discovery_evidence(
        config.repo,
        state_dir=config.state_dir,
    )
    current = {file.path: file.fingerprint_json() for file in bundle.files}
    if not fingerprints:
        if generated_source_paths(payload.get("profile")):
            return ("source_fingerprints is empty for generated profile source paths",)
        provenance = payload.get("provenance")
        evidence_count = (
            provenance.get("evidence_file_count")
            if isinstance(provenance, dict)
            else None
        )
        if evidence_count == 0 and bundle.files:
            return (
                "cache was generated with no evidence, but evidence is now available",
            )
        return ()
    stale: list[str] = []
    cached_paths: set[str] = set()
    for raw_fingerprint in fingerprints:
        if not isinstance(raw_fingerprint, dict):
            stale.append("source_fingerprints contains a non-object entry")
            continue
        path = raw_fingerprint.get("path")
        if not isinstance(path, str) or not path:
            stale.append("source_fingerprints contains an entry without path")
            continue
        cached_paths.add(path)
        current_fingerprint = current.get(path)
        if current_fingerprint is None:
            stale.append(f"{path} is missing or outside current evidence")
            continue
        changed_fields = [
            field
            for field in ("size", "sha256", "redacted")
            if raw_fingerprint.get(field) != current_fingerprint.get(field)
        ]
        if changed_fields:
            stale.append(f"{path} changed ({', '.join(changed_fields)})")
    if not generated_source_paths(payload.get("profile")):
        for path in sorted(set(current) - cached_paths):
            stale.append(f"{path} is new bounded evidence")
    return tuple(stale)


def degradation_value(
    degradation: object,
    key: str,
    default: object,
) -> object:
    if not isinstance(degradation, dict):
        return default
    return degradation.get(key, default)


def normalize_string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return items or None


def normalize_diagnostic_object(value: object) -> dict[str, object] | None:
    normalized = normalize_diagnostic_json(value)
    if isinstance(normalized, dict):
        return normalized
    return None


def normalize_diagnostic_json(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            normalized = normalize_diagnostic_json(child)
            if normalized is not None:
                result[str(key)] = normalized
        return result
    if isinstance(value, list):
        return [
            normalized
            for item in value
            if (normalized := normalize_diagnostic_json(item)) is not None
        ]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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


def has_nonempty_mapping_value(
    mapping: dict[object, object],
    field_name: object = "",
) -> bool:
    if mapping.get("strategy") == "checkbox_status":
        return field_name == "status"
    if mapping.get("strategy") in {"full_text", "heading_text"}:
        return True
    return any(
        isinstance(mapping.get(key), str) and str(mapping[key]).strip()
        for key in ("column", "label", "pattern", "prefix")
    )


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


def agent_name_from_config(config: VibeConfig) -> str:
    return agent_name_from_source(config.agent.selection_command_source)


def agent_name_from_source(source: str) -> str:
    if source.startswith("auto:"):
        return source.split(":", 2)[1]
    if source.startswith("agent.kind:"):
        return source.split(":", 1)[1]
    if source == "explicit":
        return "custom"
    if source.startswith("unresolved:"):
        return "unresolved"
    return "unknown"


def _configure_retry_callback(attempt: int, delay: float, reason: str) -> None:
    print(
        f"[vibe-loop] task configure retry {attempt} after {delay:.1f}s: {reason}",
        file=sys.stderr,
    )


def generated_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
