from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from vibe_loop.config import DEFAULT_RUNNABLE_STATUSES, TaskSourceConfig


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
MARKDOWN_FIELD_NAMES = {
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
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)[-*+]\s+(?:\[[ xX]\]\s*)?(?P<body>.*)$")
LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s*)?"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _./-]{0,80})\s*:\s*(?P<value>.*)$"
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
    scope: str = ""
    acceptance: str = ""
    evidence: str = ""
    source: str = ""
    order: int = 0

    @property
    def done(self) -> bool:
        return self.status == DONE_STATUS

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.task_id,
            "title": self.title,
            "status": self.status,
            "section": self.section,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
            "scope": self.scope,
            "acceptance": self.acceptance,
            "evidence": self.evidence,
            "source": self.source,
        }


class TaskSource(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def probe(self, task_id: str) -> Task | None: ...


def build_task_source(repo: Path, config: TaskSourceConfig) -> TaskSource:
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
    if config.type == "command":
        return CommandTaskSource(repo, config)
    raise ValueError(f"unsupported task source type: {config.type}")


def runnable_tasks(source: TaskSource, statuses: tuple[str, ...]) -> list[Task]:
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
    candidates.sort(key=task_sort_key)
    return candidates


def task_sort_key(task: Task) -> tuple[int, int, int]:
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
    columns: dict[str, str] = dataclasses.field(default_factory=dict)
    labels: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def source(self) -> str:
        location = self.section or f"line {self.line_number}"
        return f"{self.path}:{location}"


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
        "first_sentence",
        "full_text",
        "heading_text",
        "label_value",
    }:
        raise ValueError(
            f"markdown task profile.fields.{field_name}.strategy is not supported: "
            f"{strategy}"
        )
    if strategy == "label_value" and label is None:
        raise ValueError(
            f"markdown task profile.fields.{field_name}.label_value requires label"
        )
    none_values = mapping.get("none_values")
    if none_values is None:
        none = ("none",) if field_name == "dependencies" else ()
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
                labels=parse_label_values([body]),
            )
            if record_has_profile_values(profile, direct_record):
                raise ValueError(f"{record.source}: missing required field id")
            index += 1
        else:
            index += 1
    return records


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
    return Task(
        task_id=task_id,
        title=title,
        status=normalize_status(raw_status, profile.done_statuses),
        section=section,
        priority=extract_profile_value(profile, record, "priority"),
        dependencies=dependencies,
        scope=extract_profile_value(profile, record, "scope"),
        acceptance=extract_profile_value(profile, record, "acceptance"),
        evidence=extract_profile_value(profile, record, "evidence"),
        source=record.source,
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
    if field_name in match.groupdict():
        return match.group(field_name)
    if "value" in match.groupdict():
        return match.group("value")
    if match.groups():
        return match.group(1)
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
    labels: dict[str, str] = {}
    for line in lines:
        match = LABEL_RE.match(line)
        if match is None:
            continue
        labels[normalize_label(match.group("label"))] = match.group("value").strip()
    return labels


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
    return Task(
        task_id=str(value.get("id") or value.get("task_id") or ""),
        title=str(value.get("title") or value.get("id") or value.get("task_id") or ""),
        status=str(value.get("status") or ""),
        section=str(value.get("section") or ""),
        priority=str(value.get("priority") or ""),
        dependencies=tuple(str(item) for item in dependencies),
        scope=str(value.get("scope") or ""),
        acceptance=str(value.get("acceptance") or ""),
        evidence=str(value.get("evidence") or ""),
        source=str(value.get("source") or ""),
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


def first_sentence(value: str) -> str:
    for separator in (". ", ";"):
        if separator in value:
            return value.split(separator, 1)[0].strip()
    return value.strip()
