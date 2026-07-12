from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Protocol

from vibe_loop.config import (
    DEFAULT_PLAN_PATHS,
    DEFAULT_RUNNABLE_STATUSES,
    TaskSourceConfig,
)


DONE_STATUS = "Done"
BLOCKED_STATUSES = {"Done", "Gated", "Low"}
STATUS_RANK = {"Active": 0, "Next": 1, "Planned": 2}
DEFAULT_TASK_TABLE_COLUMNS = (
    "ID",
    "Priority",
    "Status",
    "Dependencies",
    "Scope",
    "Acceptance",
    "Evidence",
)
REQUIRED_TASK_FIELDS = ("id", "title", "status")
MARKDOWN_PROFILE_KINDS = {"markdown_table", "markdown_headings", "markdown_list"}
SPEC_TOOL_TASK_SOURCE_TYPES = {
    "kiro",
    "openspec",
    "spec-kit",
    "speckit",
}
MARKDOWN_FIELD_NAMES = {
    "acceptance",
    "approval_state",
    "dependencies",
    "design_refs",
    "evidence",
    "id",
    "priority",
    "paths",
    "requirement_ids",
    "resources",
    "scope",
    "section",
    "source_fingerprints",
    "spec_paths",
    "status",
    "title",
}
MARKDOWN_FIELD_MAPPING_KEYS = {
    "column",
    "label",
    "none_values",
    "pattern",
    "prefix",
    "required",
    "strategy",
}
DEPENDENCY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]*$")
SPEC_TOOL_DEPENDENCY_PATTERN = (
    r"(?:^|\n)\s*(?:[-*+]\s+)?"
    r"(?i:depends(?:\s+on)?|dependencies):\s*([^\n]+)"
    r"|\((?i:depends on)\s+([^)]+)\)"
)
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
LIST_ITEM_RE = re.compile(
    r"^(?P<indent>[ \t]*)[-*+]\s+"
    r"(?:\[(?P<mark>[ xX~.-])\]\s*)?(?P<body>.*)$"
)
CHECKBOX_ITEM_RE = re.compile(r"^\s*[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<body>.*)$")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s*)?"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _./-]{0,80})\s*:\s*(?P<value>.*)$"
)
RALPHEX_TASK_HEADING_RE = re.compile(
    r"^(?P<kind>Task|Iteration)\s+(?P<number>[A-Za-z0-9_.-]+)\s*:"
    r"\s*(?P<title>.+)$",
    re.IGNORECASE,
)
DISCOVERY_SKIP_DIRS = {
    ".git",
    ".vibe-loop",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
MAX_DISCOVERY_FILE_BYTES = 2 * 1024 * 1024
PLAN_NAME_TERMS = ("plan", "backlog", "roadmap", "task", "todo", "work")
ROOT_PATH_FILENAMES = {
    "Containerfile",
    "Dockerfile",
    "LICENSE",
    "Makefile",
    "NOTICE",
    "README",
}
MULTILINE_LABEL_TERMS = (
    "acceptance",
    "criteria",
    "conflict",
    "description",
    "design",
    "detail",
    "evidence",
    "fingerprint",
    "notes",
    "proof",
    "requirement",
    "scope",
    "spec",
    "trace",
)
TRACEABILITY_LIST_FIELDS = {
    "design_refs",
    "requirement_ids",
    "source_fingerprints",
    "spec_paths",
}


@dataclasses.dataclass(frozen=True)
class PlanCandidate:
    path: Path
    score: int
    task_count: int
    reasons: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    status: str
    section: str = ""
    priority: str = ""
    dependencies: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    conflict_domains_known: bool = False
    scope: str = ""
    acceptance: str = ""
    evidence: str = ""
    source: str = ""
    requirement_ids: tuple[str, ...] = ()
    spec_paths: tuple[str, ...] = ()
    design_refs: tuple[str, ...] = ()
    approval_state: str = ""
    source_fingerprints: tuple[dict[str, object], ...] = ()
    order: int = 0

    @property
    def done(self) -> bool:
        # Case-insensitive so command/JSON task sources that report a lowercase
        # "done" (or any case) are recognized as done — otherwise their
        # completed tasks never enter the done-set and no `depends_on` on a
        # downstream task ever resolves, silently stalling dependency chains.
        return self.status.casefold() == DONE_STATUS.casefold()

    @property
    def has_traceability(self) -> bool:
        return bool(
            self.requirement_ids
            or self.spec_paths
            or self.design_refs
            or self.approval_state
            or self.source_fingerprints
        )

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.task_id,
            "title": self.title,
            "status": self.status,
            "section": self.section,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
            "resources": list(self.resources),
            "paths": list(self.paths),
            "conflict_domains_known": self.conflict_domains_known,
            "scope": self.scope,
            "acceptance": self.acceptance,
            "evidence": self.evidence,
            "source": self.source,
        }
        if self.requirement_ids:
            payload["requirement_ids"] = list(self.requirement_ids)
        if self.spec_paths:
            payload["spec_paths"] = list(self.spec_paths)
        if self.design_refs:
            payload["design_refs"] = list(self.design_refs)
        if self.approval_state:
            payload["approval_state"] = self.approval_state
        if self.source_fingerprints:
            payload["source_fingerprints"] = [
                dict(fingerprint) for fingerprint in self.source_fingerprints
            ]
        return payload


class TaskSource(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def probe(self, task_id: str) -> Task | None: ...


def build_task_source(repo: Path, config: TaskSourceConfig) -> TaskSource:
    if (
        config.type == "command"
        or config.list_command
        or config.next_command
        or config.probe_command
    ):
        return CommandTaskSource(repo, config)
    if config.type in {"markdown-plan", "markdown-profile"}:
        if config.profile is not None:
            return MarkdownProfileSource(repo, config.profile)
        if config.type == "markdown-profile":
            raise ValueError(
                "markdown-profile task source requires task_source.profile"
            )
        return MarkdownPlanSource(
            discover_markdown_plan(repo, config),
            config.runnable_statuses,
        )
    if config.type in {"ralphex-markdown", "ralphex-plan"}:
        return RalphexMarkdownSource(repo, ralphex_source_paths(repo, config))
    if config.type in SPEC_TOOL_TASK_SOURCE_TYPES:
        return SpecToolMarkdownSource(repo, config)
    raise ValueError(f"unsupported task source type: {config.type}")


def runnable_tasks(
    source: TaskSource,
    statuses: tuple[str, ...],
    respect_source_order: bool = False,
) -> list[Task]:
    tasks = source.list_tasks()
    done = {task.task_id for task in tasks if task.done}
    allowed = set(statuses)
    candidates = [
        task
        for task in tasks
        if not task.done
        and task.status in allowed
        and all(dep in done for dep in task.dependencies)
    ]
    candidates.sort(key=lambda task: task_sort_key(task, respect_source_order))
    return candidates


def task_sort_key(
    task: Task, respect_source_order: bool = False
) -> tuple[int, int] | tuple[int, int, int]:
    # respect_source_order drops the priority band so the task source's emitted
    # order (task.order) is the sole tie-break within the status band — the
    # source becomes the dispatch authority. A single sort call always uses one
    # mode, so the two key shapes are never compared against each other.
    if respect_source_order:
        return (STATUS_RANK.get(task.status, 9), task.order)
    return (
        STATUS_RANK.get(task.status, 9),
        priority_rank(task.priority),
        task.order,
    )


def priority_rank(priority: str) -> int:
    normalized = priority.upper()
    if normalized.startswith("P") and normalized[1:].isdigit():
        return int(normalized[1:])
    if normalized == "LOW":
        return 99
    return 50


class MarkdownPlanSource:
    def __init__(self, path: Path, runnable_statuses: tuple[str, ...]):
        self.path = path
        self.runnable_statuses = runnable_statuses
        self._source = MarkdownProfileSource(
            path.parent,
            default_markdown_plan_profile((str(path),)),
            required_columns=DEFAULT_TASK_TABLE_COLUMNS,
        )

    def list_tasks(self) -> list[Task]:
        return self._source.list_tasks()

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


@dataclasses.dataclass(frozen=True)
class FieldMapping:
    column: str | None = None
    label: str | None = None
    none_values: tuple[str, ...] = ()
    pattern: str | None = None
    prefix: str | None = None
    required: bool = False
    strategy: str = "full_text"


@dataclasses.dataclass(frozen=True)
class MarkdownTaskProfile:
    kind: str
    source_paths: tuple[str, ...]
    fields: dict[str, FieldMapping]
    status_map: dict[str, tuple[str, ...]]
    required_columns: tuple[str, ...] = ()

    @property
    def done_statuses(self) -> tuple[str, ...]:
        return self.status_map.get("done", (DONE_STATUS,))


@dataclasses.dataclass(frozen=True)
class MarkdownRecord:
    path: Path
    line_number: int
    section: str
    heading: str
    text: str
    checkbox_mark: str | None = None
    columns: dict[str, str] = dataclasses.field(default_factory=dict)
    labels: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def source(self) -> str:
        location = self.section or f"line {self.line_number}"
        return f"{self.path.as_posix()}:{location}"


@dataclasses.dataclass(frozen=True)
class RalphexConflictSurface:
    resources: str = ""
    paths: str = ""
    resources_present: bool | None = False
    paths_present: bool | None = False


@dataclasses.dataclass(frozen=True)
class SpecToolPreset:
    name: str
    display_name: str
    source_globs: tuple[str, ...]
    profile: dict[str, object]


class MarkdownProfileSource:
    def __init__(
        self,
        repo: Path,
        profile: dict[str, object],
        *,
        required_columns: tuple[str, ...] = (),
    ):
        self.repo = repo
        self.profile = parse_markdown_task_profile(
            profile,
            required_columns=required_columns,
        )
        self.paths = tuple(
            resolve_profile_path(repo, path) for path in self.profile.source_paths
        )

    def list_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for path in self.paths:
            if not path.exists():
                raise FileNotFoundError(f"task source file not found: {path}")
            text = path.read_text(encoding="utf-8", errors="replace")
            for record in iter_markdown_records(self.profile, path, text):
                tasks.append(
                    task_from_markdown_record(self.profile, record, len(tasks))
                )
        validate_task_set(tasks)
        return tasks

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


class RalphexMarkdownSource:
    def __init__(self, repo: Path, paths: tuple[Path, ...]):
        if not paths:
            raise ValueError("ralphex markdown task source requires at least one path")
        self.repo = repo
        self.paths = paths

    def list_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for path in self.paths:
            if not path.exists():
                raise FileNotFoundError(f"task source file not found: {path}")
            text = path.read_text(encoding="utf-8", errors="replace")
            tasks.extend(iter_ralphex_tasks(self.repo, path, text, len(tasks)))
        validate_task_set(tasks)
        return tasks

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


class SpecToolMarkdownSource:
    def __init__(self, repo: Path, config: TaskSourceConfig):
        preset = spec_tool_preset(config.type)
        self.repo = repo
        self.preset = preset
        self.paths = spec_tool_source_paths(repo, config, preset)

    def list_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for path in self.paths:
            source = MarkdownProfileSource(
                self.repo,
                spec_tool_profile_for_path(self.repo, self.preset, path),
            )
            prefix = spec_tool_task_prefix(self.repo, path)
            local_tasks = source.list_tasks()
            if not local_tasks:
                raise ValueError(
                    f"{path}: no {self.preset.display_name} tasks found; expected "
                    "checkbox list items with stable task IDs"
                )
            for local_task in local_tasks:
                tasks.append(prefix_spec_tool_task(local_task, prefix))
        validate_task_set(tasks)
        return tasks

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


def default_markdown_plan_profile(source_paths: tuple[str, ...]) -> dict[str, object]:
    return {
        "kind": "markdown_table",
        "source_paths": list(source_paths),
        "stable_ids": True,
        "fields": {
            "id": {"column": "ID"},
            "priority": {"column": "Priority"},
            "status": {"column": "Status"},
            "dependencies": {"column": "Dependencies", "none_values": ["none"]},
            "scope": {"column": "Scope"},
            "acceptance": {"column": "Acceptance"},
            "evidence": {"column": "Evidence"},
            "title": {"column": "Scope", "strategy": "first_sentence"},
        },
        "status_map": {
            "done": [DONE_STATUS],
            "runnable": list(DEFAULT_RUNNABLE_STATUSES),
            "blocked": sorted(BLOCKED_STATUSES),
        },
    }


def spec_tool_preset(source_type: str) -> SpecToolPreset:
    normalized = "spec-kit" if source_type == "speckit" else source_type
    if normalized == "spec-kit":
        return SpecToolPreset(
            name="spec-kit",
            display_name="Spec Kit",
            source_globs=("specs/*/tasks.md", ".specify/specs/*/tasks.md"),
            profile={
                "kind": "markdown_list",
                "source_paths": [],
                "stable_ids": True,
                "fields": {
                    "id": {
                        "pattern": r"^(?P<id>T\d{3,})\b",
                        "strategy": "heading_text",
                    },
                    "title": {
                        "pattern": (
                            r"^T\d{3,}\s+(?:\[P\]\s+)?"
                            r"(?:\[[A-Za-z0-9_-]+\]\s+)?"
                            r"(?P<title>.+?)(?:\s+\([Dd]epends on [^)]+\))?$"
                        ),
                        "strategy": "heading_text",
                    },
                    "status": {"strategy": "checkbox_status"},
                    "dependencies": {
                        "pattern": SPEC_TOOL_DEPENDENCY_PATTERN,
                        "none_values": ["none", "-"],
                    },
                    "resources": {
                        "label": "Conflict Resources",
                        "none_values": ["none", "-"],
                    },
                    "paths": {"label": "Conflict Paths", "none_values": ["none", "-"]},
                    "acceptance": {"label": "Acceptance"},
                    "evidence": {"label": "Evidence"},
                },
                "status_map": {
                    "done": [DONE_STATUS],
                    "runnable": list(DEFAULT_RUNNABLE_STATUSES),
                    "blocked": sorted(BLOCKED_STATUSES),
                },
            },
        )
    if normalized == "kiro":
        return SpecToolPreset(
            name="kiro",
            display_name="Kiro",
            source_globs=(".kiro/specs/*/tasks.md",),
            profile={
                "kind": "markdown_list",
                "source_paths": [],
                "stable_ids": True,
                "fields": {
                    "id": {
                        "pattern": r"^(?P<id>\d+(?:\.\d+)*)(?:\.\s+|\s+)",
                        "strategy": "heading_text",
                    },
                    "title": {
                        "pattern": (
                            r"^\d+(?:\.\d+)*(?:\.\s+|\s+)"
                            r"(?P<title>.+?)(?:\s+\([Dd]epends on [^)]+\))?$"
                        ),
                        "strategy": "heading_text",
                    },
                    "status": {"strategy": "checkbox_status"},
                    "dependencies": {
                        "pattern": SPEC_TOOL_DEPENDENCY_PATTERN,
                        "none_values": ["none", "-"],
                    },
                    "resources": {
                        "label": "Conflict Resources",
                        "none_values": ["none", "-"],
                    },
                    "paths": {"label": "Conflict Paths", "none_values": ["none", "-"]},
                    "acceptance": {"label": "Acceptance"},
                    "evidence": {"label": "Evidence"},
                },
                "status_map": {
                    "done": [DONE_STATUS],
                    "runnable": list(DEFAULT_RUNNABLE_STATUSES),
                    "blocked": sorted(BLOCKED_STATUSES),
                },
            },
        )
    if normalized == "openspec":
        return SpecToolPreset(
            name="openspec",
            display_name="OpenSpec",
            source_globs=("openspec/changes/*/tasks.md",),
            profile={
                "kind": "markdown_list",
                "source_paths": [],
                "stable_ids": True,
                "fields": {
                    "id": {
                        "pattern": r"^(?P<id>\d+(?:\.\d+)*)(?:\.\s+|\s+)",
                        "strategy": "heading_text",
                    },
                    "title": {
                        "pattern": (
                            r"^\d+(?:\.\d+)*(?:\.\s+|\s+)"
                            r"(?P<title>.+?)(?:\s+\([Dd]epends on [^)]+\))?$"
                        ),
                        "strategy": "heading_text",
                    },
                    "status": {"strategy": "checkbox_status"},
                    "dependencies": {
                        "pattern": SPEC_TOOL_DEPENDENCY_PATTERN,
                        "none_values": ["none", "-"],
                    },
                    "resources": {
                        "label": "Conflict Resources",
                        "none_values": ["none", "-"],
                    },
                    "paths": {"label": "Conflict Paths", "none_values": ["none", "-"]},
                    "acceptance": {"label": "Acceptance"},
                    "evidence": {"label": "Evidence"},
                },
                "status_map": {
                    "done": [DONE_STATUS],
                    "runnable": list(DEFAULT_RUNNABLE_STATUSES),
                    "blocked": sorted(BLOCKED_STATUSES),
                },
            },
        )
    raise ValueError(f"unsupported spec-driven task source type: {source_type}")


def spec_tool_profile_for_path(
    repo: Path,
    preset: SpecToolPreset,
    path: Path,
) -> dict[str, object]:
    profile = dict(preset.profile)
    profile["source_paths"] = [profile_source_path(repo, path)]
    return profile


def profile_source_path(repo: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path)


def parse_markdown_task_profile(
    profile: object,
    *,
    required_columns: tuple[str, ...] = (),
) -> MarkdownTaskProfile:
    if not isinstance(profile, dict):
        raise ValueError("markdown task profile must be a table")
    kind = str(profile.get("kind") or "")
    if kind not in MARKDOWN_PROFILE_KINDS:
        raise ValueError(
            "markdown task profile kind must be markdown_table, "
            "markdown_headings, or markdown_list"
        )
    source_paths = profile.get("source_paths")
    if not (
        isinstance(source_paths, list)
        and source_paths
        and all(isinstance(path, str) and path for path in source_paths)
    ):
        raise ValueError("markdown task profile.source_paths must be non-empty strings")
    fields = parse_profile_fields(profile.get("fields"))
    validate_profile_strategy_compatibility(kind, fields)
    status_map = parse_profile_status_map(profile.get("status_map"))
    return MarkdownTaskProfile(
        kind=kind,
        source_paths=tuple(source_paths),
        fields=fields,
        status_map=status_map,
        required_columns=required_columns,
    )


def parse_profile_fields(value: object) -> dict[str, FieldMapping]:
    if not isinstance(value, dict):
        raise ValueError("markdown task profile.fields must be a table")
    missing = [field for field in REQUIRED_TASK_FIELDS if field not in value]
    if missing:
        raise ValueError(
            "markdown task profile.fields is missing required fields: "
            f"{', '.join(missing)}"
        )
    fields: dict[str, FieldMapping] = {}
    for field_name, raw_mapping in value.items():
        field = str(field_name)
        if field not in MARKDOWN_FIELD_NAMES:
            raise ValueError(f"unsupported markdown task profile field: {field}")
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"markdown task profile.fields.{field} must be a table")
        fields[field] = parse_field_mapping(field, raw_mapping)
    return fields


def parse_field_mapping(field_name: str, mapping: dict[str, object]) -> FieldMapping:
    unknown_keys = sorted(
        str(key) for key in set(mapping) - MARKDOWN_FIELD_MAPPING_KEYS
    )
    if unknown_keys:
        raise ValueError(
            f"markdown task profile.fields.{field_name} contains unsupported keys: "
            f"{', '.join(unknown_keys)}"
        )
    column = optional_profile_string(mapping.get("column"))
    label = optional_profile_string(mapping.get("label"))
    pattern = optional_profile_string(mapping.get("pattern"))
    prefix = optional_profile_string(mapping.get("prefix"))
    strategy = str(mapping.get("strategy") or "full_text")
    if strategy not in {
        "checkbox_status",
        "first_sentence",
        "full_text",
        "heading_text",
        "label_value",
    }:
        raise ValueError(
            f"markdown task profile.fields.{field_name}.strategy is not supported: "
            f"{strategy}"
        )
    if strategy == "checkbox_status" and field_name != "status":
        raise ValueError(
            f"markdown task profile.fields.{field_name}.checkbox_status "
            "requires the status field"
        )
    if strategy == "label_value" and label is None:
        raise ValueError(
            f"markdown task profile.fields.{field_name}.label_value requires label"
        )
    none_values = mapping.get("none_values")
    if none_values is None:
        none = (
            ("none",)
            if field_name in {"dependencies", "resources", "paths"}
            or field_name in TRACEABILITY_LIST_FIELDS
            else ()
        )
    elif isinstance(none_values, list) and all(
        isinstance(item, str) and item for item in none_values
    ):
        none = tuple(none_values)
    else:
        raise ValueError(
            f"markdown task profile.fields.{field_name}.none_values must be strings"
        )
    required_value = mapping.get("required")
    if required_value is not None and not isinstance(required_value, bool):
        raise ValueError(
            f"markdown task profile.fields.{field_name}.required must be a boolean"
        )
    required = field_name in REQUIRED_TASK_FIELDS or bool(required_value)
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"markdown task profile.fields.{field_name}.pattern is invalid: {exc}"
            ) from exc
    return FieldMapping(
        column=column,
        label=label,
        none_values=none,
        pattern=pattern,
        prefix=prefix,
        required=required,
        strategy=strategy,
    )


def validate_profile_strategy_compatibility(
    kind: str,
    fields: dict[str, FieldMapping],
) -> None:
    if kind == "markdown_list":
        return
    for field_name, mapping in fields.items():
        if mapping.strategy == "checkbox_status":
            raise ValueError(
                f"markdown task profile.fields.{field_name}.checkbox_status "
                "requires markdown_list"
            )


def parse_profile_status_map(value: object) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {
            "done": (DONE_STATUS,),
            "runnable": DEFAULT_RUNNABLE_STATUSES,
            "blocked": tuple(sorted(BLOCKED_STATUSES)),
        }
    if not isinstance(value, dict):
        raise ValueError("markdown task profile.status_map must be a table")
    status_map: dict[str, tuple[str, ...]] = {}
    for key, statuses in value.items():
        if not (
            isinstance(statuses, list)
            and statuses
            and all(isinstance(status, str) and status for status in statuses)
        ):
            raise ValueError(
                f"markdown task profile.status_map.{key} must be non-empty strings"
            )
        status_map[str(key)] = tuple(statuses)
    status_map.setdefault("done", (DONE_STATUS,))
    status_map.setdefault("runnable", DEFAULT_RUNNABLE_STATUSES)
    status_map.setdefault("blocked", tuple(sorted(BLOCKED_STATUSES)))
    return status_map


def optional_profile_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "markdown task profile mapping values must be non-empty strings"
        )
    return value


def resolve_profile_path(repo: Path, source_path: str) -> Path:
    path = Path(source_path)
    if path.is_absolute():
        return path
    return repo / path


def iter_markdown_records(
    profile: MarkdownTaskProfile,
    path: Path,
    text: str,
) -> list[MarkdownRecord]:
    if profile.kind == "markdown_table":
        return list(iter_markdown_table_records(profile, path, text))
    if profile.kind == "markdown_headings":
        return list(iter_markdown_heading_records(profile, path, text))
    if profile.kind == "markdown_list":
        return list(iter_markdown_list_records(profile, path, text))
    raise AssertionError(profile.kind)


def iter_markdown_table_records(
    profile: MarkdownTaskProfile,
    path: Path,
    text: str,
) -> list[MarkdownRecord]:
    records: list[MarkdownRecord] = []
    first_missing_columns: list[str] = []
    section = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        heading = parse_heading(lines[index])
        if heading is not None and heading[0] == 3:
            section = heading[1]
        cells = split_markdown_row(lines[index])
        if not (
            cells
            and index + 1 < len(lines)
            and is_separator_row(split_markdown_row(lines[index + 1]))
        ):
            index += 1
            continue
        missing_columns = missing_required_table_columns(profile, cells)
        if missing_columns:
            if not first_missing_columns and header_looks_profile_related(
                profile, cells
            ):
                first_missing_columns = missing_columns
            index += 1
            continue
        index += 2
        while index < len(lines):
            row_cells = split_markdown_row(lines[index])
            if not row_cells:
                break
            if is_separator_row(row_cells):
                index += 1
                continue
            if len(row_cells) != len(cells):
                raise ValueError(
                    f"{path}:{index + 1}: markdown table row has "
                    f"{len(row_cells)} cells, expected {len(cells)}"
                )
            columns = dict(zip(cells, row_cells, strict=True))
            records.append(
                MarkdownRecord(
                    path=path,
                    line_number=index + 1,
                    section=section,
                    heading="",
                    text=" | ".join(row_cells),
                    columns=columns,
                )
            )
            index += 1
    if first_missing_columns:
        raise ValueError(
            f"{path}: missing required table columns: "
            f"{', '.join(first_missing_columns)}"
        )
    return records


def iter_markdown_heading_records(
    profile: MarkdownTaskProfile,
    path: Path,
    text: str,
) -> list[MarkdownRecord]:
    records: list[MarkdownRecord] = []
    section = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        heading = parse_heading(lines[index])
        if heading is None:
            index += 1
            continue
        level, title = heading
        block_start = index + 1
        next_index = block_start
        while next_index < len(lines) and parse_heading(lines[next_index]) is None:
            next_index += 1
        block_lines = lines[block_start:next_index]
        record = MarkdownRecord(
            path=path,
            line_number=index + 1,
            section=section,
            heading=title,
            text="\n".join([title, *block_lines]),
            labels=parse_label_values(block_lines),
        )
        if extract_profile_value(profile, record, "id", enforce_required=False):
            records.append(record)
        elif record_has_profile_values(profile, record):
            raise ValueError(f"{record.source}: missing required field id")
        else:
            section = title if level <= 3 else section
        index = next_index
    return records


def iter_markdown_list_records(
    profile: MarkdownTaskProfile,
    path: Path,
    text: str,
) -> list[MarkdownRecord]:
    records: list[MarkdownRecord] = []
    section = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        heading = parse_heading(lines[index])
        if heading is not None:
            section = heading[1]
            index += 1
            continue
        item = LIST_ITEM_RE.match(lines[index])
        if item is None:
            index += 1
            continue
        indent = indentation_width(item.group("indent"))
        body = item.group("body").strip()
        block_lines: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            if parse_heading(lines[next_index]) is not None:
                break
            next_item = LIST_ITEM_RE.match(lines[next_index])
            if (
                next_item is not None
                and indentation_width(next_item.group("indent")) <= indent
            ):
                break
            block_lines.append(lines[next_index])
            next_index += 1
        record = MarkdownRecord(
            path=path,
            line_number=index + 1,
            section=section,
            heading=body,
            text="\n".join([body, *block_lines]),
            checkbox_mark=item.group("mark"),
            labels=parse_label_values([body, *block_lines]),
        )
        if extract_profile_value(profile, record, "id", enforce_required=False):
            records.append(record)
            index = next_index
        elif record_has_profile_values(profile, record):
            direct_record = MarkdownRecord(
                path=path,
                line_number=index + 1,
                section=section,
                heading=body,
                text=body,
                checkbox_mark=item.group("mark"),
                labels=parse_label_values([body]),
            )
            if record_has_profile_values(profile, direct_record):
                raise ValueError(f"{record.source}: missing required field id")
            index += 1
        else:
            index += 1
    return records


def ralphex_source_paths(repo: Path, config: TaskSourceConfig) -> tuple[Path, ...]:
    if config.plan_path:
        return (resolve_profile_path(repo, config.plan_path),)
    if config.is_explicit("plan_paths") or config.plan_paths != DEFAULT_PLAN_PATHS:
        return tuple(resolve_profile_path(repo, path) for path in config.plan_paths)
    return (discover_ralphex_plan(repo, config),)


def spec_tool_source_paths(
    repo: Path,
    config: TaskSourceConfig,
    preset: SpecToolPreset,
) -> tuple[Path, ...]:
    if config.plan_path:
        return (resolve_profile_path(repo, config.plan_path),)
    if config.is_explicit("plan_paths") or config.plan_paths != DEFAULT_PLAN_PATHS:
        paths = tuple(resolve_profile_path(repo, path) for path in config.plan_paths)
        if not paths:
            raise ValueError(
                f"{preset.display_name} task source requires at least one path"
            )
        return paths
    paths: list[Path] = []
    for pattern in preset.source_globs:
        paths.extend(path for path in repo.glob(pattern) if path.is_file())
    if paths:
        return tuple(sorted(dedupe_preserving_order(paths), key=lambda path: str(path)))
    searched = ", ".join(preset.source_globs)
    raise FileNotFoundError(
        f"no {preset.display_name} task files found; set task_source.plan_path "
        f"or task_source.plan_paths. Candidate patterns: {searched}"
    )


def prefix_spec_tool_task(
    task: Task,
    prefix: str,
) -> Task:
    return dataclasses.replace(
        task,
        task_id=prefix_spec_tool_dependency(task.task_id, prefix),
        dependencies=tuple(
            prefix_spec_tool_dependency(dependency, prefix)
            for dependency in task.dependencies
        ),
    )


def prefix_spec_tool_dependency(
    task_id: str,
    prefix: str,
) -> str:
    if ":" in task_id:
        return task_id
    return f"{prefix}:{task_id}"


def spec_tool_task_prefix(repo: Path, path: Path) -> str:
    path = path.resolve()
    try:
        relative = path.relative_to(repo.resolve())
    except ValueError:
        relative = Path(path.name)
    if path.name == "tasks.md" and path.parent != path:
        raw = path.parent.name
    else:
        raw = relative.with_suffix("").as_posix()
    return sanitize_spec_tool_id_component(raw)


def sanitize_spec_tool_id_component(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.+-]+", "-", value.strip())
    text = text.strip("-._")
    return text or "tasks"


def iter_ralphex_tasks(
    repo: Path,
    path: Path,
    text: str,
    order_offset: int,
) -> list[Task]:
    lines = strip_markdown_fenced_blocks(text.splitlines())
    section = ralphex_plan_title(path, lines)
    validation_commands = ralphex_validation_commands(lines)
    evidence = ralphex_validation_evidence(validation_commands)
    plan_conflict_surface = ralphex_plan_conflict_surface(lines)
    records: list[Task] = []
    index = 0
    while index < len(lines):
        heading = parse_heading(lines[index])
        match = ralphex_task_heading_match(heading)
        if match is None:
            index += 1
            continue
        next_index = index + 1
        while next_index < len(lines):
            next_heading = parse_heading(lines[next_index])
            if next_heading is not None and next_heading[0] <= 3:
                break
            next_index += 1
        block_lines = lines[index + 1 : next_index]
        try:
            records.append(
                ralphex_task_from_block(
                    repo,
                    path,
                    section,
                    evidence,
                    match,
                    block_lines,
                    plan_conflict_surface,
                    line_number=index + 1,
                    order=order_offset + len(records),
                )
            )
        except ValueError as exc:
            raise ValueError(f"{path}:line {index + 1}: {exc}") from exc
        index = next_index
    return records


def ralphex_task_heading_match(
    heading: tuple[int, str] | None,
) -> re.Match[str] | None:
    if heading is None:
        return None
    level, title = heading
    if level != 3:
        return None
    return RALPHEX_TASK_HEADING_RE.match(title)


def strip_markdown_fenced_blocks(lines: list[str]) -> list[str]:
    stripped: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in lines:
        fence = markdown_fence_marker(line)
        if fence:
            if not in_fence:
                in_fence = True
                fence_marker = fence
            elif fence == fence_marker:
                in_fence = False
                fence_marker = ""
            stripped.append("")
            continue
        stripped.append("" if in_fence else line)
    return stripped


def markdown_fence_marker(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("```"):
        return "```"
    if stripped.startswith("~~~"):
        return "~~~"
    return ""


def ralphex_task_from_block(
    repo: Path,
    path: Path,
    section: str,
    evidence: str,
    match: re.Match[str],
    block_lines: list[str],
    plan_conflict_surface: RalphexConflictSurface,
    *,
    line_number: int,
    order: int,
) -> Task:
    checkboxes = ralphex_checkboxes(block_lines)
    labels = parse_label_values(block_lines)
    dependencies_value = first_label(labels, "dependencies", "depends", "depends on")
    resource_label = ralphex_resource_label(labels)
    path_label = ralphex_path_label(labels)
    resources_value = (
        resource_label.resources
        if resource_label.resources_present is not False
        else plan_conflict_surface.resources
    )
    paths_value = (
        path_label.paths
        if path_label.paths_present is not False
        else plan_conflict_surface.paths
    )
    resources_present = ralphex_domain_known(
        resource_label.resources_present,
        plan_conflict_surface.resources_present,
    )
    paths_present = ralphex_domain_known(
        path_label.paths_present,
        plan_conflict_surface.paths_present,
    )
    try:
        dependencies = parse_dependencies(dependencies_value, none_values=("none", "-"))
        resources = parse_resource_list(resources_value, none_values=("none", "-"))
        paths = parse_path_list(paths_value, none_values=("none", "-"))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return Task(
        task_id=ralphex_task_id(repo, path, match.group("kind"), match.group("number")),
        title=match.group("title").strip(),
        status=ralphex_checkbox_status(checkboxes),
        section=section,
        dependencies=dependencies,
        resources=resources,
        paths=paths,
        conflict_domains_known=resources_present or paths_present,
        scope=ralphex_scope(block_lines),
        acceptance=ralphex_acceptance(checkboxes),
        evidence=evidence,
        source=f"{path}:line {line_number}",
        order=order,
    )


def ralphex_checkboxes(lines: list[str]) -> tuple[tuple[bool, str], ...]:
    checkboxes: list[tuple[bool, str]] = []
    for line in lines:
        match = CHECKBOX_ITEM_RE.match(line)
        if match is None:
            continue
        checkboxes.append(
            (
                match.group("mark").casefold() == "x",
                match.group("body").strip(),
            )
        )
    return tuple(checkboxes)


def ralphex_checkbox_status(checkboxes: tuple[tuple[bool, str], ...]) -> str:
    if checkboxes and all(done for done, _ in checkboxes):
        return DONE_STATUS
    return "Planned"


def ralphex_scope(lines: list[str]) -> str:
    return "\n".join(line.rstrip() for line in lines).strip()


def ralphex_acceptance(checkboxes: tuple[tuple[bool, str], ...]) -> str:
    return "\n".join(body for _, body in checkboxes)


def ralphex_plan_title(path: Path, lines: list[str]) -> str:
    for line in lines:
        heading = parse_heading(line)
        if heading is not None and heading[0] == 1:
            title = heading[1]
            return title.removeprefix("Plan:").strip() or title
    return path.stem


def ralphex_validation_commands(lines: list[str]) -> tuple[str, ...]:
    commands: list[str] = []
    in_section = False
    for line in lines:
        heading = parse_heading(line)
        if heading is not None:
            if heading[0] <= 2:
                in_section = normalize_label(heading[1]) in {
                    "validation",
                    "validation command",
                    "validation commands",
                }
            elif in_section:
                in_section = False
            continue
        if not in_section:
            continue
        command = ralphex_validation_command(line)
        if command:
            commands.append(command)
    return tuple(commands)


def ralphex_plan_conflict_surface(lines: list[str]) -> RalphexConflictSurface:
    section_lines: list[str] = []
    in_section = False
    for line in lines:
        heading = parse_heading(line)
        if heading is not None:
            if heading[0] <= 2:
                in_section = normalize_label(heading[1]) in {
                    "conflict surface",
                    "conflict surfaces",
                }
            elif in_section:
                in_section = False
            continue
        if in_section:
            section_lines.append(line)
    labels = parse_label_values(section_lines)
    resources = ralphex_resource_label(labels)
    paths = ralphex_path_label(labels)
    unlabeled_paths = ralphex_unlabeled_conflict_paths(section_lines)
    path_value = paths.paths
    path_present = paths.paths_present
    if unlabeled_paths:
        path_value = ", ".join(
            [value for value in (paths.paths, unlabeled_paths) if value]
        )
        path_present = True
    return RalphexConflictSurface(
        resources=resources.resources,
        paths=path_value,
        resources_present=resources.resources_present,
        paths_present=path_present,
    )


def ralphex_domain_known(
    task_present: bool | None,
    plan_present: bool | None,
) -> bool:
    if task_present is None:
        return False
    if task_present:
        return True
    return bool(plan_present)


def ralphex_resource_label(labels: dict[str, str]) -> RalphexConflictSurface:
    value, present = first_label_value(
        labels,
        "resources",
        "resource",
        "conflict resources",
        "conflict surface resources",
    )
    if present:
        return RalphexConflictSurface(
            resources=value,
            resources_present=True if value.strip() else None,
        )
    conflict_value, conflict_present = first_label_value(
        labels,
        "conflict surface",
        "conflict surfaces",
    )
    if not conflict_present:
        return RalphexConflictSurface()
    resources, paths = split_conflict_surface_value(conflict_value)
    return RalphexConflictSurface(
        resources=resources,
        paths=paths,
        resources_present=bool(resources.strip()),
        paths_present=bool(paths.strip()),
    )


def ralphex_path_label(labels: dict[str, str]) -> RalphexConflictSurface:
    value, present = first_label_value(labels, "paths", "path", "conflict paths")
    if present:
        return RalphexConflictSurface(
            paths=value,
            paths_present=True if value.strip() else None,
        )
    conflict_value, conflict_present = first_label_value(
        labels,
        "conflict surface",
        "conflict surfaces",
    )
    if not conflict_present:
        return RalphexConflictSurface()
    resources, paths = split_conflict_surface_value(conflict_value)
    return RalphexConflictSurface(
        resources=resources,
        paths=paths,
        resources_present=bool(resources.strip()),
        paths_present=bool(paths.strip()),
    )


def split_conflict_surface_value(value: str) -> tuple[str, str]:
    resources: list[str] = []
    paths: list[str] = []
    for segment in (part.strip() for part in value.split(";")):
        if not segment:
            continue
        key, raw_value = split_conflict_surface_segment(segment)
        if key in {"path", "paths"}:
            paths.append(raw_value)
        elif key in {"resource", "resources"}:
            resources.append(raw_value)
        else:
            resources.append(segment)
    return ", ".join(resources), ", ".join(paths)


def split_conflict_surface_segment(segment: str) -> tuple[str, str]:
    for separator in (":", "="):
        if separator not in segment:
            continue
        key, value = segment.split(separator, 1)
        normalized = normalize_label(key)
        if normalized in {"path", "paths", "resource", "resources"}:
            return normalized, value.strip()
    return "", segment


def ralphex_unlabeled_conflict_paths(lines: list[str]) -> str:
    paths: list[str] = []
    for line in lines:
        if CHECKBOX_ITEM_RE.match(line) or LABEL_RE.match(line):
            continue
        item = LIST_ITEM_RE.match(line)
        if item is None:
            continue
        for value in ralphex_conflict_path_candidates(item.group("body").strip()):
            if ralphex_looks_like_path(value):
                paths.append(value)
    return ", ".join(paths)


def ralphex_conflict_path_candidates(value: str) -> tuple[str, ...]:
    code_spans = tuple(
        clean_path_token(candidate)
        for candidate in CODE_SPAN_RE.findall(value)
        if clean_path_token(candidate)
    )
    if code_spans:
        return code_spans
    stripped = strip_markdown_code_span(value)
    words = stripped.split()
    if len(words) > 1:
        token = clean_path_token(words[0])
        return (token,) if token else ()
    token = clean_path_token(stripped)
    return (token,) if token else ()


def clean_path_token(value: str) -> str:
    return value.strip().rstrip(".,;:")


def ralphex_looks_like_path(value: str) -> bool:
    path = value.strip()
    if not path or any(char.isspace() for char in path):
        return False
    try:
        normalize_path_lock(path)
    except ValueError:
        return False
    return (
        "/" in path
        or "\\" in path
        or path.startswith(".")
        or bool(PurePosixPath(path).suffix)
        or path in ROOT_PATH_FILENAMES
    )


def strip_markdown_code_span(value: str) -> str:
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1].strip()
    return value


def first_label_value(labels: dict[str, str], *names: str) -> tuple[str, bool]:
    for name in names:
        normalized = normalize_label(name)
        if normalized in labels:
            return labels[normalized], True
    return "", False


def first_label(labels: dict[str, str], *names: str) -> str:
    value, _ = first_label_value(labels, *names)
    return value


def ralphex_validation_command(line: str) -> str:
    if CHECKBOX_ITEM_RE.match(line):
        return ""
    item = LIST_ITEM_RE.match(line)
    if item is None:
        return ""
    value = item.group("body").strip()
    return strip_markdown_code_span(value)


def ralphex_validation_evidence(commands: tuple[str, ...]) -> str:
    if not commands:
        return ""
    return "Validation commands:\n" + "\n".join(f"- {command}" for command in commands)


def ralphex_task_id(repo: Path, path: Path, kind: str, number: str) -> str:
    try:
        relative = path.resolve().relative_to(repo.resolve())
    except ValueError:
        relative = Path(path.name)
    plan = ".".join(
        normalize_task_id_part(part) for part in relative.with_suffix("").parts
    )
    normalized_kind = normalize_task_id_part(kind).lower()
    normalized_number = normalize_task_id_part(number).lower()
    return f"{plan}:{normalized_kind}-{normalized_number}"


def normalize_task_id_part(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-._" else "-" for char in value.strip()
    ).strip("-._")


def discover_ralphex_plan(repo: Path, config: TaskSourceConfig) -> Path:
    candidates = find_ralphex_plan_candidates(repo, config)
    if len(candidates) == 1:
        return candidates[0].path
    if len(candidates) > 1:
        best_score = candidates[0].score
        best = [candidate for candidate in candidates if candidate.score == best_score]
        if len(best) == 1:
            return best[0].path
        paths = ", ".join(
            f"{candidate.path.relative_to(repo)} score={candidate.score}"
            for candidate in best
        )
        raise ValueError(
            f"multiple ralphex markdown plan files tied; set task_source.plan_path "
            f"or task_source.plan_paths: {paths}"
        )
    searched = ", ".join(config.plan_paths)
    raise FileNotFoundError(
        "no ralphex markdown plan file found; set task_source.plan_path, "
        "task_source.plan_paths, or add headings like '### Task 1:'. "
        f"Candidate paths: {searched}"
    )


def find_ralphex_plan_candidates(
    repo: Path,
    config: TaskSourceConfig,
) -> list[PlanCandidate]:
    configured = {
        (repo / path).resolve(): len(config.plan_paths) - index
        for index, path in enumerate(config.plan_paths)
    }
    matches: list[PlanCandidate] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in DISCOVERY_SKIP_DIRS)
        root_path = Path(root)
        for filename in sorted(files):
            if not filename.lower().endswith(".md"):
                continue
            path = root_path / filename
            candidate = evaluate_ralphex_plan(repo, path, configured)
            if candidate is not None:
                matches.append(candidate)
    return sorted(
        matches,
        key=lambda candidate: (
            -candidate.score,
            str(candidate.path),
        ),
    )


def evaluate_ralphex_plan(
    repo: Path,
    path: Path,
    configured: dict[Path, int],
) -> PlanCandidate | None:
    try:
        if path.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    task_count = len(iter_ralphex_tasks(repo, path, text, 0))
    if task_count == 0:
        return None
    score = 100 + min(task_count, 20)
    reasons = ["ralphex-task-headings", f"tasks={task_count}"]
    validation_commands = ralphex_validation_commands(
        strip_markdown_fenced_blocks(text.splitlines())
    )
    if validation_commands:
        score += 5
        reasons.append("validation-commands")
    relative_path = path.relative_to(repo)
    if "plans" in {part.lower() for part in relative_path.parts}:
        score += 10
        reasons.append("plans-dir")
    if any(term in path.name.lower() for term in PLAN_NAME_TERMS):
        score += 5
        reasons.append("plan-like-name")
    configured_bonus = configured.get(path.resolve(), 0)
    if configured_bonus:
        score += configured_bonus
        reasons.append("configured-candidate")
    return PlanCandidate(
        path=path,
        score=score,
        task_count=task_count,
        reasons=tuple(reasons),
    )


def table_header_matches_profile(
    profile: MarkdownTaskProfile,
    header: list[str],
) -> bool:
    return not missing_required_table_columns(profile, header)


def missing_required_table_columns(
    profile: MarkdownTaskProfile,
    header: list[str],
) -> list[str]:
    required_columns = required_table_columns(profile)
    missing: list[str] = []
    for column in required_columns:
        if column_value(header, column) is None:
            missing.append(column)
    return missing


def header_looks_profile_related(
    profile: MarkdownTaskProfile,
    header: list[str],
) -> bool:
    required_columns = required_table_columns(profile)
    present = sum(
        1 for column in required_columns if column_value(header, column) is not None
    )
    return present >= min(2, len(required_columns))


def required_table_columns(profile: MarkdownTaskProfile) -> tuple[str, ...]:
    columns: list[str] = []
    for column in profile.required_columns:
        if column not in columns:
            columns.append(column)
    for mapping in profile.fields.values():
        if (
            mapping.required
            and mapping.column is not None
            and mapping.column not in columns
        ):
            columns.append(mapping.column)
    return tuple(columns)


def record_has_profile_values(
    profile: MarkdownTaskProfile,
    record: MarkdownRecord,
) -> bool:
    for field_name in profile.fields:
        if field_name in {"id", "section", "title"}:
            continue
        if extract_profile_value(profile, record, field_name, enforce_required=False):
            return True
    return False


def task_from_markdown_record(
    profile: MarkdownTaskProfile,
    record: MarkdownRecord,
    order: int,
) -> Task:
    task_id = extract_profile_value(profile, record, "id")
    raw_status = extract_profile_value(profile, record, "status")
    title = extract_profile_value(profile, record, "title")
    section = extract_profile_value(profile, record, "section", fallback=record.section)
    dependencies_value = extract_profile_value(profile, record, "dependencies")
    dependency_mapping = profile.fields.get("dependencies")
    try:
        dependencies = parse_dependencies(
            dependencies_value,
            none_values=dependency_mapping.none_values
            if dependency_mapping
            else ("none",),
        )
    except ValueError as exc:
        raise ValueError(f"{record.source}: {exc}") from exc
    resources_mapping = profile.fields.get("resources")
    paths_mapping = profile.fields.get("paths")
    resources_value = extract_profile_value(profile, record, "resources")
    paths_value = extract_profile_value(profile, record, "paths")
    requirement_ids_mapping = profile.fields.get("requirement_ids")
    spec_paths_mapping = profile.fields.get("spec_paths")
    design_refs_mapping = profile.fields.get("design_refs")
    source_fingerprints_mapping = profile.fields.get("source_fingerprints")
    requirement_ids_value = extract_profile_value(profile, record, "requirement_ids")
    spec_paths_value = extract_profile_value(profile, record, "spec_paths")
    design_refs_value = extract_profile_value(profile, record, "design_refs")
    source_fingerprints_value = extract_profile_value(
        profile,
        record,
        "source_fingerprints",
    )
    try:
        resources = parse_resource_list(
            resources_value,
            none_values=resources_mapping.none_values
            if resources_mapping
            else ("none",),
        )
        paths = parse_path_list(
            paths_value,
            none_values=paths_mapping.none_values if paths_mapping else ("none",),
        )
        requirement_ids = parse_requirement_id_list(
            requirement_ids_value,
            none_values=requirement_ids_mapping.none_values
            if requirement_ids_mapping
            else ("none",),
        )
        spec_paths = parse_path_list(
            spec_paths_value,
            none_values=spec_paths_mapping.none_values
            if spec_paths_mapping
            else ("none",),
        )
        design_refs = parse_trace_ref_list(
            design_refs_value,
            value_name="design reference",
            none_values=design_refs_mapping.none_values
            if design_refs_mapping
            else ("none",),
        )
        source_fingerprints = parse_source_fingerprint_text(
            source_fingerprints_value,
            none_values=source_fingerprints_mapping.none_values
            if source_fingerprints_mapping
            else ("none",),
        )
    except ValueError as exc:
        raise ValueError(f"{record.source}: {exc}") from exc
    return Task(
        task_id=task_id,
        title=title,
        status=normalize_status(raw_status, profile.done_statuses),
        section=section,
        priority=extract_profile_value(profile, record, "priority"),
        dependencies=dependencies,
        resources=resources,
        paths=paths,
        conflict_domains_known=(
            resource_declaration_present(resources_mapping, resources_value)
            or resource_declaration_present(paths_mapping, paths_value)
        ),
        scope=extract_profile_value(profile, record, "scope"),
        acceptance=extract_profile_value(profile, record, "acceptance"),
        evidence=extract_profile_value(profile, record, "evidence"),
        source=record.source,
        requirement_ids=requirement_ids,
        spec_paths=spec_paths,
        design_refs=design_refs,
        approval_state=extract_profile_value(profile, record, "approval_state"),
        source_fingerprints=source_fingerprints,
        order=order,
    )


def extract_profile_value(
    profile: MarkdownTaskProfile,
    record: MarkdownRecord,
    field_name: str,
    *,
    enforce_required: bool = True,
    fallback: str = "",
) -> str:
    mapping = profile.fields.get(field_name)
    if mapping is None:
        return fallback
    value = raw_profile_value(mapping, record, field_name)
    if mapping.strategy == "first_sentence":
        value = first_sentence(value)
    elif mapping.strategy == "heading_text" and mapping.pattern is None and not value:
        value = record.heading
    value = value.strip()
    if enforce_required and mapping.required and not value:
        raise ValueError(f"{record.source}: missing required field {field_name}")
    return value or fallback


def raw_profile_value(
    mapping: FieldMapping,
    record: MarkdownRecord,
    field_name: str,
) -> str:
    value = ""
    if mapping.column is not None:
        value = column_value_from_record(record, mapping.column)
    elif mapping.label is not None:
        value = record.labels.get(normalize_label(mapping.label), "")
    elif mapping.prefix is not None:
        value = prefixed_value(record, mapping.prefix)
    elif mapping.strategy == "checkbox_status":
        value = checkbox_status_value(record)
    elif mapping.strategy == "full_text":
        value = record.text
    elif mapping.strategy == "heading_text":
        value = record.heading
    if mapping.pattern is not None:
        target = value or (
            record.heading if mapping.strategy == "heading_text" else record.text
        )
        value = regex_value(mapping.pattern, target, field_name)
    return value


def checkbox_status_value(record: MarkdownRecord) -> str:
    if record.checkbox_mark is None:
        return ""
    mark = record.checkbox_mark.strip().casefold()
    if mark == "x":
        return DONE_STATUS
    if mark in {"-", "~", "."}:
        return "Active"
    return "Planned"


def column_value_from_record(record: MarkdownRecord, column: str) -> str:
    value = record.columns.get(column)
    if value is not None:
        return value
    return column_value(list(record.columns), column, record.columns) or ""


def column_value(
    header: list[str],
    column: str,
    values: dict[str, str] | None = None,
) -> str | None:
    if column in header:
        return values[column] if values is not None else column
    normalized = normalize_label(column)
    matches = [name for name in header if normalize_label(name) == normalized]
    if len(matches) == 1:
        return values[matches[0]] if values is not None else matches[0]
    return None


def prefixed_value(record: MarkdownRecord, prefix: str) -> str:
    for line in record.text.splitlines():
        text = line.strip()
        if text.startswith(prefix):
            return text.removeprefix(prefix).strip()
    return ""


def regex_value(pattern: str, text: str, field_name: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        return ""
    if field_name in match.groupdict() and match.group(field_name):
        return match.group(field_name)
    if "value" in match.groupdict() and match.group("value"):
        return match.group("value")
    for group in match.groups():
        if group:
            return group
    return match.group(0)


def normalize_status(value: str, done_statuses: tuple[str, ...]) -> str:
    done = {status.casefold() for status in done_statuses}
    if value.casefold() in done:
        return DONE_STATUS
    return value


def validate_task_set(tasks: list[Task]) -> None:
    seen: dict[str, str] = {}
    for task in tasks:
        if task.task_id in seen:
            raise ValueError(
                f"duplicate task id {task.task_id}: {seen[task.task_id]} and "
                f"{task.source}"
            )
        seen[task.task_id] = task.source


def parse_label_values(lines: list[str]) -> dict[str, str]:
    labels: dict[str, list[str]] = {}
    current_label: str | None = None
    for line in lines:
        match = LABEL_RE.match(line)
        if match is not None:
            current_label = normalize_label(match.group("label"))
            value = match.group("value").strip()
            if value:
                if label_allows_continuation(current_label):
                    labels.setdefault(current_label, []).append(value)
                else:
                    labels[current_label] = [value]
                current_label = None
            elif label_allows_continuation(current_label):
                labels.setdefault(current_label, [])
            else:
                labels[current_label] = []
                current_label = None
            continue
        if current_label is None:
            continue
        value = label_continuation_value(line)
        if value:
            labels[current_label].append(value)
    return {label: "\n".join(parts).strip() for label, parts in labels.items()}


def label_allows_continuation(label: str) -> bool:
    return any(term in label for term in MULTILINE_LABEL_TERMS)


def label_continuation_value(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    item = LIST_ITEM_RE.match(line)
    if item is not None:
        return item.group("body").strip()
    return stripped


def normalize_label(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def parse_heading(line: str) -> tuple[int, str] | None:
    match = HEADING_RE.match(line)
    if match is None:
        return None
    return len(match.group("marks")), match.group("title").strip()


def indentation_width(value: str) -> int:
    return len(value.expandtabs(4))


def discover_markdown_plan(repo: Path, config: TaskSourceConfig) -> Path:
    if config.plan_path:
        return repo / config.plan_path
    candidates = find_markdown_plan_candidates(repo, config)
    if len(candidates) == 1:
        return candidates[0].path
    if len(candidates) > 1:
        best_score = candidates[0].score
        best = [candidate for candidate in candidates if candidate.score == best_score]
        if len(best) == 1:
            return best[0].path
        paths = ", ".join(
            f"{candidate.path.relative_to(repo)} score={candidate.score}"
            for candidate in best
        )
        raise ValueError(
            f"multiple markdown plan files tied; set task_source.plan_path: {paths}"
        )
    searched = ", ".join(config.plan_paths)
    raise FileNotFoundError(
        "no markdown plan file found; set task_source.plan_path or add a "
        f"markdown task table. Candidate paths: {searched}"
    )


def find_markdown_plan_candidates(
    repo: Path,
    config: TaskSourceConfig,
) -> list[PlanCandidate]:
    configured = {
        (repo / path).resolve(): len(config.plan_paths) - index
        for index, path in enumerate(config.plan_paths)
    }
    matches: list[PlanCandidate] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in DISCOVERY_SKIP_DIRS)
        root_path = Path(root)
        for filename in sorted(files):
            if filename.lower().endswith(".md"):
                path = root_path / filename
                candidate = evaluate_markdown_plan(repo, path, configured)
                if candidate is not None:
                    matches.append(candidate)
    return sorted(
        matches,
        key=lambda candidate: (
            -candidate.score,
            str(candidate.path),
        ),
    )


def evaluate_markdown_plan(
    repo: Path,
    path: Path,
    configured: dict[Path, int],
) -> PlanCandidate | None:
    if not contains_task_table(path):
        return None
    tasks = MarkdownPlanSource(path, ()).list_tasks()
    if not tasks:
        return None
    score = 100
    reasons = ["task-table"]
    task_points = min(len(tasks), 20)
    score += task_points
    reasons.append(f"tasks={len(tasks)}")
    name = path.name.lower()
    if any(term in name for term in PLAN_NAME_TERMS):
        score += 10
        reasons.append("plan-like-name")
    relative_path = path.relative_to(repo)
    if any(
        term in str(part).lower()
        for part in relative_path.parts
        for term in ("doc", "plan")
    ):
        score += 5
        reasons.append("plan-like-path")
    configured_bonus = configured.get(path.resolve(), 0)
    if configured_bonus:
        score += configured_bonus
        reasons.append("configured-candidate")
    return PlanCandidate(
        path=path,
        score=score,
        task_count=len(tasks),
        reasons=tuple(reasons),
    )


def contains_task_table(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
            return False
        profile = parse_markdown_task_profile(
            default_markdown_plan_profile((str(path),)),
            required_columns=DEFAULT_TASK_TABLE_COLUMNS,
        )
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines[:-1]):
            cells = split_markdown_row(line)
            if table_header_matches_profile(profile, cells) and is_separator_row(
                split_markdown_row(lines[index + 1])
            ):
                return True
    except OSError:
        return False
    return False


class CommandTaskSource:
    def __init__(self, repo: Path, config: TaskSourceConfig):
        self.repo = repo
        self.config = config
        if not config.list_command:
            raise ValueError("command task source requires task_source.list")

    def list_tasks(self) -> list[Task]:
        payload = run_json_command(self.repo, self.config.list_command or "")
        raw_tasks = (
            payload.get("tasks", payload) if isinstance(payload, dict) else payload
        )
        if not isinstance(raw_tasks, list):
            raise ValueError(
                "task_source.list must return a JSON array or {tasks:[...]}"
            )
        return [task_from_mapping(item, index) for index, item in enumerate(raw_tasks)]

    def probe(self, task_id: str) -> Task | None:
        if self.config.probe_command:
            command = self.config.probe_command.format(task_id=task_id)
            payload = run_json_command(self.repo, command)
            if payload is None:
                return None
            return task_from_mapping(payload, 0)
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


def run_json_command(repo: Path, command: str) -> object:
    result = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(result.stdout)


def task_from_mapping(value: object, order: int) -> Task:
    if not isinstance(value, dict):
        raise ValueError("task JSON entries must be objects")
    dependencies = value.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise ValueError("task dependencies must be an array")
    resources_present = "resources" in value and value.get("resources") is not None
    paths_present = "paths" in value and value.get("paths") is not None
    resources = parse_task_string_array(value.get("resources"), "resources")
    paths = tuple(
        normalize_path_lock(path)
        for path in parse_task_string_array(value.get("paths"), "paths")
    )
    requirement_ids = tuple(
        normalize_requirement_id(requirement_id)
        for requirement_id in parse_task_string_array(
            value.get("requirement_ids"),
            "requirement_ids",
        )
    )
    spec_paths = tuple(
        normalize_path_lock(path)
        for path in parse_task_string_array(value.get("spec_paths"), "spec_paths")
    )
    design_refs = parse_task_string_array(value.get("design_refs"), "design_refs")
    approval_state = optional_task_string(value.get("approval_state"), "approval_state")
    source_fingerprints = normalize_source_fingerprints(
        value.get("source_fingerprints"),
        "source_fingerprints",
    )
    return Task(
        task_id=str(value.get("id") or value.get("task_id") or ""),
        title=str(value.get("title") or value.get("id") or value.get("task_id") or ""),
        status=str(value.get("status") or ""),
        section=str(value.get("section") or ""),
        priority=str(value.get("priority") or ""),
        dependencies=tuple(str(item) for item in dependencies),
        resources=dedupe_preserving_order(
            normalize_resource(resource) for resource in resources
        ),
        paths=dedupe_preserving_order(paths),
        conflict_domains_known=bool(value.get("conflict_domains_known"))
        or resources_present
        or paths_present,
        scope=str(value.get("scope") or ""),
        acceptance=str(value.get("acceptance") or ""),
        evidence=str(value.get("evidence") or ""),
        source=str(value.get("source") or ""),
        requirement_ids=dedupe_preserving_order(requirement_ids),
        spec_paths=dedupe_preserving_order(spec_paths),
        design_refs=dedupe_preserving_order(
            ref.strip() for ref in design_refs if ref.strip()
        ),
        approval_state=approval_state,
        source_fingerprints=source_fingerprints,
        order=order,
    )


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":", " "} for cell in cells)


def parse_dependencies(
    value: str,
    *,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[str, ...]:
    text = value.strip()
    if not text:
        return ()
    if text.casefold() in {none.casefold() for none in none_values}:
        return ()
    if ";" in text:
        raise ValueError("invalid dependency syntax: use comma-separated task IDs")
    parts = [part.strip() for part in text.replace("\n", ",").split(",")]
    if any(not part for part in parts):
        raise ValueError("invalid dependency syntax: empty dependency")
    invalid = [
        part
        for part in parts
        if not DEPENDENCY_ID_RE.fullmatch(part) or any(char.isspace() for char in part)
    ]
    if invalid:
        raise ValueError(
            "invalid dependency syntax: "
            f"{', '.join(invalid)} must be comma-separated task IDs"
        )
    return tuple(parts)


def parse_resource_list(
    value: str,
    *,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[str, ...]:
    return dedupe_preserving_order(
        normalize_resource(part)
        for part in parse_comma_separated_values(
            value,
            none_values=none_values,
            value_name="resource",
        )
    )


def resource_declaration_present(
    mapping: FieldMapping | None,
    value: str,
) -> bool:
    return mapping is not None and bool(value.strip())


def parse_path_list(
    value: str,
    *,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[str, ...]:
    return dedupe_preserving_order(
        normalize_path_lock(part)
        for part in parse_comma_separated_values(
            value,
            none_values=none_values,
            value_name="path",
        )
    )


def parse_requirement_id_list(
    value: str,
    *,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[str, ...]:
    return dedupe_preserving_order(
        normalize_requirement_id(part)
        for part in parse_comma_separated_values(
            value,
            none_values=none_values,
            value_name="requirement id",
        )
    )


def parse_trace_ref_list(
    value: str,
    *,
    value_name: str,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[str, ...]:
    return dedupe_preserving_order(
        part
        for part in parse_comma_separated_values(
            value,
            none_values=none_values,
            value_name=value_name,
        )
    )


def parse_source_fingerprint_text(
    value: str,
    *,
    none_values: tuple[str, ...] = ("none",),
) -> tuple[dict[str, object], ...]:
    text = value.strip()
    if not text:
        return ()
    if text.casefold() in {none.casefold() for none in none_values}:
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "invalid source fingerprint syntax: expected a JSON array of objects"
        ) from exc
    return normalize_source_fingerprints(payload, "source_fingerprints")


def parse_comma_separated_values(
    value: str,
    *,
    none_values: tuple[str, ...],
    value_name: str,
) -> tuple[str, ...]:
    text = value.strip()
    if not text:
        return ()
    if text.casefold() in {none.casefold() for none in none_values}:
        return ()
    if ";" in text:
        raise ValueError(f"invalid {value_name} syntax: use comma-separated values")
    parts = [part.strip() for part in text.replace("\n", ",").split(",")]
    if any(not part for part in parts):
        raise ValueError(f"invalid {value_name} syntax: empty {value_name}")
    return tuple(parts)


def parse_task_string_array(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"task {name} must be an array of strings")
    return tuple(value)


def optional_task_string(value: object, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"task {name} must be a string")
    return value.strip()


def normalize_requirement_id(value: str) -> str:
    requirement_id = value.strip()
    if (
        not requirement_id
        or not DEPENDENCY_ID_RE.fullmatch(requirement_id)
        or any(char.isspace() for char in requirement_id)
    ):
        raise ValueError(f"invalid requirement id syntax: {value}")
    return requirement_id


def normalize_source_fingerprints(
    value: object,
    name: str,
) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"task {name} must be an array of objects")
    fingerprints: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"task {name}[{index}] must be an object")
        normalized: dict[str, object] = {}
        for key, child in item.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"task {name}[{index}] keys must be strings")
            normalized[key] = normalize_json_value(child, f"task {name}[{index}].{key}")
        fingerprints.append(normalized)
    return tuple(fingerprints)


def normalize_json_value(value: object, name: str) -> object:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc


def normalize_resource(value: str) -> str:
    resource = value.strip()
    if (
        not resource
        or not DEPENDENCY_ID_RE.fullmatch(resource)
        or any(char.isspace() for char in resource)
    ):
        raise ValueError(f"invalid resource syntax: {value}")
    return resource


def normalize_path_lock(value: str) -> str:
    path = value.strip().replace("\\", "/")
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"invalid path lock syntax: {value}")
    return pure.as_posix().rstrip("/")


def dedupe_preserving_order(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


def first_sentence(value: str) -> str:
    for separator in (". ", ";"):
        if separator in value:
            return value.split(separator, 1)[0].strip()
    return value.strip()
