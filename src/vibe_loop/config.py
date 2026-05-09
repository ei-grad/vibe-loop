from __future__ import annotations

import dataclasses
import math
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def shell_quote(s: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([s])
    return shlex.quote(s)


def prepare_shell_command(
    command: str,
) -> tuple[str | list[str], bool]:
    if sys.platform != "win32":
        return command, True
    parts = shlex.split(command, posix=True)
    resolved = shutil.which(parts[0])
    if resolved is None:
        return command, True
    return [resolved, *parts[1:]], False


DEFAULT_PLAN_PATHS = (
    "PLAN.md",
    "docs/PLAN.md",
    "plan.md",
    "docs/plan.md",
    "docs/plans.md",
    "docs/ROADMAP.md",
    "ROADMAP.md",
    "TODO.md",
)
DEFAULT_RUNNABLE_STATUSES = ("Active", "Next", "Planned")
GENERATED_TASK_PROFILE_CACHE_FILE = "generated-task-source.json"
GENERATED_TASK_PROFILE_SCHEMA_VERSION = 1
GENERATED_TASK_PROFILE_PROMPT_VERSION = 1
PLANNING_ANALYTICS_ARTIFACT_DIR = "planning-analytics"
PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY = "current-runner-parity"
PLANNING_ANALYTICS_SCHEDULE_POLICIES = (
    "current-runner-parity",
    "lightmetrics-parity",
)
PLANNING_ANALYTICS_DEFAULT_OUTPUTS = {
    "timeline_json": "timeline.json",
    "gantt_html": "gantt.html",
    "benchmark_json": "duration-benchmark.json",
    "benchmark_markdown": "duration-benchmark.md",
}
PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES = ("diagnostic", "disabled")
PLANNING_ANALYTICS_DURATION_MODEL_NAMES = ("robust-duration-baseline-v1",)
PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL = {
    "name": "robust-duration-baseline-v1",
    "group_min_sample_count": 2,
    "similarity_min_score": 0.35,
    "similarity_max_examples": 3,
    "similarity_blend_weight": 0.25,
    "fallback_minutes": 60,
}
TASK_SOURCE_SOURCE_KEYS = frozenset(
    {
        "type",
        "plan_path",
        "plan_paths",
        "list",
        "next",
        "probe",
        "profile",
    }
)
GENERATED_TASK_PROFILE_FORBIDDEN_KEYS = frozenset(
    {
        "command",
        "commands",
        "list",
        "next",
        "probe",
        "selection_command",
    }
)

AGENT_SKILL_REF_PREFIX = {
    "codex": "$",
    "claude": "/",
}
AGENT_COMMAND_DEFAULTS = {
    "codex": {
        "command": "codex exec {prompt}",
        "selection_command": "codex exec {prompt}",
    },
    "claude": {
        "command": "claude -p {prompt}",
        "selection_command": "claude -p {prompt}",
    },
}
SUPPORTED_AGENT_CLIS = tuple(AGENT_COMMAND_DEFAULTS)
AGENT_PREFERRED_CLI = "codex"
AGENT_DEFAULT_POLICY_SOURCE = "codex-first"
AGENT_DEFAULT_POLICY = (
    "Explicit .vibe-loop.toml agent commands win. Omitted commands use Codex "
    "when Codex is available, including when Claude is also available; otherwise "
    "they use Claude when it is the sole available supported CLI. If no "
    "supported CLI is available, configure a command explicitly or install Codex "
    "or Claude."
)


class AgentResolutionError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentDetection:
    codex: str | None = None
    claude: str | None = None

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(name for name in SUPPORTED_AGENT_CLIS if self.path_for(name))

    def path_for(self, name: str) -> str | None:
        return getattr(self, name)

    def summary(self) -> str:
        if not self.available:
            return "none"
        return ", ".join(f"{name}={self.path_for(name)}" for name in self.available)

    def to_json(self) -> dict[str, object]:
        return {
            "available": list(self.available),
            "codex": {
                "available": self.codex is not None,
                "path": self.codex,
            },
            "claude": {
                "available": self.claude is not None,
                "path": self.claude,
            },
        }


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    command: str | None = None
    selection_command: str | None = None
    command_source: str = "unresolved:no-supported-cli"
    selection_command_source: str = "unresolved:no-supported-cli"
    detected: AgentDetection = dataclasses.field(default_factory=AgentDetection)
    forward_stderr: bool = False
    resolved_cli: str | None = None

    @property
    def skill_ref_prefix(self) -> str:
        if self.resolved_cli:
            return AGENT_SKILL_REF_PREFIX.get(self.resolved_cli, "$")
        return "$"

    def require_command(self) -> str:
        if self.command:
            return self.command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.command",
                self.command_source,
                self.detected,
            )
        )

    def require_selection_command(self) -> str:
        if self.selection_command:
            return self.selection_command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.selection_command",
                self.selection_command_source,
                self.detected,
            )
        )

    def diagnostics(self) -> list[str]:
        messages: list[str] = []
        if not self.command:
            messages.append(
                unresolved_agent_command_message(
                    "agent.command",
                    self.command_source,
                    self.detected,
                )
            )
        if not self.selection_command:
            messages.append(
                unresolved_agent_command_message(
                    "agent.selection_command",
                    self.selection_command_source,
                    self.detected,
                )
            )
        return messages

    def to_json(self) -> dict[str, object]:
        return {
            "command": self.command,
            "command_source": self.command_source,
            "selection_command": self.selection_command,
            "selection_command_source": self.selection_command_source,
            "forward_stderr": self.forward_stderr,
            "resolved_cli": self.resolved_cli,
            "skill_ref_prefix": self.skill_ref_prefix,
            "detected": self.detected.to_json(),
            "default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "default_policy": AGENT_DEFAULT_POLICY,
            "diagnostics": self.diagnostics(),
        }


@dataclasses.dataclass(frozen=True)
class TaskSourceConfig:
    type: str = "markdown-plan"
    plan_path: str | None = None
    plan_paths: tuple[str, ...] = DEFAULT_PLAN_PATHS
    profile: dict[str, Any] | None = None
    list_command: str | None = None
    next_command: str | None = None
    probe_command: str | None = None
    runnable_statuses: tuple[str, ...] = DEFAULT_RUNNABLE_STATUSES
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def explicit_source_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.explicit_keys & TASK_SOURCE_SOURCE_KEYS))

    @property
    def allows_generated_cache(self) -> bool:
        return not self.explicit_source_keys

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "type": self.type,
            "plan_path": self.plan_path,
            "plan_paths": list(self.plan_paths),
            "profile": self.profile,
            "list_command": self.list_command,
            "next_command": self.next_command,
            "probe_command": self.probe_command,
            "runnable_statuses": list(self.runnable_statuses),
            "explicit_keys": sorted(self.explicit_keys),
            "explicit_source_keys": list(self.explicit_source_keys),
            "allows_generated_cache": self.allows_generated_cache,
        }


@dataclasses.dataclass(frozen=True)
class CompletionConfig:
    commands: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsOutputs:
    timeline_json: str | None = None
    gantt_html: str | None = None
    benchmark_json: str | None = None
    benchmark_markdown: str | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def has_explicit_paths(self) -> bool:
        return any(
            getattr(self, key) is not None for key in PLANNING_ANALYTICS_DEFAULT_OUTPUTS
        )


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsDurationModelConfig:
    name: str = str(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["name"])
    group_min_sample_count: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["group_min_sample_count"]
    )
    similarity_min_score: float = float(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_min_score"]
    )
    similarity_max_examples: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_max_examples"]
    )
    similarity_blend_weight: float = float(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_blend_weight"]
    )
    fallback_minutes: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["fallback_minutes"]
    )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "parameters": {
                "group_min_sample_count": self.group_min_sample_count,
                "similarity_min_score": self.similarity_min_score,
                "similarity_max_examples": self.similarity_max_examples,
                "similarity_blend_weight": self.similarity_blend_weight,
                "fallback_minutes": self.fallback_minutes,
            },
        }


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsConfig:
    schedule_policy: str = PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY
    subject_matching: str = "diagnostic"
    worklog_command: str | None = None
    outputs: PlanningAnalyticsOutputs = dataclasses.field(
        default_factory=PlanningAnalyticsOutputs
    )
    duration_model: PlanningAnalyticsDurationModelConfig = dataclasses.field(
        default_factory=PlanningAnalyticsDurationModelConfig
    )
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys


@dataclasses.dataclass(frozen=True)
class VibeConfig:
    repo: Path
    main_branch: str = "main"
    state_dir: str = ".vibe-loop"
    agent: AgentConfig = dataclasses.field(default_factory=AgentConfig)
    task_source: TaskSourceConfig = dataclasses.field(default_factory=TaskSourceConfig)
    completion: CompletionConfig = dataclasses.field(default_factory=CompletionConfig)
    planning_analytics: PlanningAnalyticsConfig = dataclasses.field(
        default_factory=PlanningAnalyticsConfig
    )

    @property
    def state_path(self) -> Path:
        return self.repo / self.state_dir

    @property
    def generated_task_profile_path(self) -> Path:
        return self.state_path / GENERATED_TASK_PROFILE_CACHE_FILE

    @property
    def planning_analytics_state_path(self) -> Path:
        return self.state_path / PLANNING_ANALYTICS_ARTIFACT_DIR


def load_config(repo: Path) -> VibeConfig:
    repo = repo.resolve()
    data = read_config_file(repo / ".vibe-loop.toml")
    task_source = parse_task_source(data.get("task_source", {}))
    completion = parse_completion(data.get("completion", {}), repo)
    agent = parse_agent(data.get("agent", {}))
    planning_analytics = parse_planning_analytics(data.get("planning_analytics", {}))
    return VibeConfig(
        repo=repo,
        main_branch=str(data.get("main_branch") or "main"),
        state_dir=str(data.get("state_dir") or ".vibe-loop"),
        agent=agent,
        task_source=task_source,
        completion=completion,
        planning_analytics=planning_analytics,
    )


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected TOML table")
    return payload


def parse_agent(data: object) -> AgentConfig:
    table = expect_table(data, "agent")
    detected = detect_agent_clis()
    configured_command = optional_nonempty_string(table.get("command"))
    configured_selection = optional_nonempty_string(table.get("selection_command"))
    command, command_source, resolved_cli = resolve_agent_default(
        "command",
        configured_command,
        detected,
    )
    selection_command, selection_command_source, _ = resolve_agent_default(
        "selection_command",
        configured_selection,
        detected,
    )
    return AgentConfig(
        command=command,
        selection_command=selection_command,
        command_source=command_source,
        selection_command_source=selection_command_source,
        detected=detected,
        forward_stderr=optional_bool(
            table.get("forward_stderr"), False, "agent.forward_stderr"
        ),
        resolved_cli=resolved_cli,
    )


def detect_agent_clis(path: str | None = None) -> AgentDetection:
    return AgentDetection(
        codex=shutil.which("codex", path=path),
        claude=shutil.which("claude", path=path),
    )


def resolve_agent_default(
    key: str,
    configured: str | None,
    detected: AgentDetection,
) -> tuple[str | None, str, str | None]:
    if configured is not None:
        return configured, "explicit", None
    available = detected.available
    if AGENT_PREFERRED_CLI in available:
        source = "auto:codex"
        if len(available) > 1:
            source = f"auto:codex:{AGENT_DEFAULT_POLICY_SOURCE}"
        return AGENT_COMMAND_DEFAULTS[AGENT_PREFERRED_CLI][key], source, AGENT_PREFERRED_CLI
    if len(available) == 1:
        agent_name = available[0]
        return AGENT_COMMAND_DEFAULTS[agent_name][key], f"auto:{agent_name}", agent_name
    if not available:
        return None, "unresolved:no-supported-cli", None
    return None, "unresolved:multiple-supported-clis", None


def unresolved_agent_command_message(
    setting: str,
    source: str,
    detected: AgentDetection,
) -> str:
    if source == "unresolved:multiple-supported-clis":
        available = ", ".join(detected.available)
        return (
            f"{setting} is not configured and multiple supported agent CLIs are "
            f"available on PATH ({available}); set {setting} in .vibe-loop.toml "
            "to choose the command explicitly."
        )
    return (
        f"{setting} is not configured and no supported agent CLI was found on "
        "PATH; install codex or claude, or set the command explicitly in "
        ".vibe-loop.toml."
    )


def parse_task_source(data: object) -> TaskSourceConfig:
    table = expect_table(data, "task_source")
    explicit_keys = frozenset(str(key) for key in table)
    profile = optional_profile(table.get("profile"))
    statuses = table.get("runnable_statuses")
    if statuses is None:
        runnable = profile_runnable_statuses(profile) or DEFAULT_RUNNABLE_STATUSES
    elif isinstance(statuses, list) and all(isinstance(item, str) for item in statuses):
        runnable = tuple(statuses)
    else:
        raise ValueError("task_source.runnable_statuses must be an array of strings")
    plan_paths = table.get("plan_paths")
    if plan_paths is None:
        candidate_paths = DEFAULT_PLAN_PATHS
    elif isinstance(plan_paths, list) and all(
        isinstance(item, str) for item in plan_paths
    ):
        candidate_paths = tuple(plan_paths)
    else:
        raise ValueError("task_source.plan_paths must be an array of strings")
    return TaskSourceConfig(
        type=str(table.get("type") or "markdown-plan"),
        plan_path=optional_string(table.get("plan_path")),
        plan_paths=candidate_paths,
        profile=profile,
        list_command=optional_string(table.get("list")),
        next_command=optional_string(table.get("next")),
        probe_command=optional_string(table.get("probe")),
        runnable_statuses=runnable,
        explicit_keys=explicit_keys,
    )


def optional_profile(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("task_source.profile must be a TOML table")
    return value


def profile_runnable_statuses(profile: dict[str, Any] | None) -> tuple[str, ...] | None:
    if profile is None:
        return None
    status_map = profile.get("status_map")
    if not isinstance(status_map, dict):
        return None
    runnable = status_map.get("runnable")
    if runnable is None:
        return None
    if (
        isinstance(runnable, list)
        and runnable
        and all(isinstance(item, str) for item in runnable)
    ):
        return tuple(runnable)
    raise ValueError(
        "task_source.profile.status_map.runnable must be an array of strings"
    )


def reject_generated_command_adapters(profile: object) -> None:
    if not isinstance(profile, dict):
        raise ValueError("generated task-source profile must be a JSON object")
    forbidden = sorted(find_forbidden_generated_command_keys(profile))
    if forbidden:
        fields = ", ".join(forbidden)
        raise ValueError(
            "generated task-source profiles cannot define executable command "
            f"adapters: {fields}"
        )


def find_forbidden_generated_command_keys(
    value: object,
    path: str = "profile",
) -> set[str]:
    forbidden: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key in GENERATED_TASK_PROFILE_FORBIDDEN_KEYS:
                forbidden.add(child_path)
            if key == "type" and child == "command":
                forbidden.add(f"{child_path}=command")
            forbidden.update(find_forbidden_generated_command_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            forbidden.update(
                find_forbidden_generated_command_keys(child, f"{path}[{index}]")
            )
    return forbidden


def parse_completion(data: object, repo: Path) -> CompletionConfig:
    table = expect_table(data, "completion")
    commands = table.get("commands")
    if commands is None:
        return CompletionConfig(commands=default_completion_commands(repo))
    if isinstance(commands, list) and all(isinstance(item, str) for item in commands):
        return CompletionConfig(commands=tuple(commands))
    raise ValueError("completion.commands must be an array of strings")


def default_completion_commands(repo: Path) -> tuple[str, ...]:
    record = repo / "scripts" / "record_worklog.py"
    gantt = repo / "scripts" / "generate_gantt.py"
    if record.exists() and gantt.exists():
        return (
            "uv run python scripts/record_worklog.py --validate",
            "uv run python scripts/generate_gantt.py --coverage-check",
        )
    return ()


def parse_planning_analytics(data: object) -> PlanningAnalyticsConfig:
    table = expect_table(data, "planning_analytics")
    explicit_keys = frozenset(str(key) for key in table)
    schedule_policy = (
        optional_nonempty_string(table.get("schedule_policy"))
        or PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY
    )
    if schedule_policy not in PLANNING_ANALYTICS_SCHEDULE_POLICIES:
        allowed = ", ".join(PLANNING_ANALYTICS_SCHEDULE_POLICIES)
        raise ValueError(
            f"planning_analytics.schedule_policy must be one of: {allowed}"
        )
    subject_matching = (
        optional_nonempty_string(table.get("subject_matching")) or "diagnostic"
    )
    if subject_matching not in PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES:
        allowed = ", ".join(PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES)
        raise ValueError(
            f"planning_analytics.subject_matching must be one of: {allowed}"
        )
    return PlanningAnalyticsConfig(
        schedule_policy=schedule_policy,
        subject_matching=subject_matching,
        worklog_command=optional_nonempty_string(table.get("worklog_command")),
        outputs=parse_planning_analytics_outputs(table.get("outputs")),
        duration_model=parse_planning_analytics_duration_model(
            table.get("duration_model")
        ),
        explicit_keys=explicit_keys,
    )


def parse_planning_analytics_duration_model(
    data: object,
) -> PlanningAnalyticsDurationModelConfig:
    table = expect_table(data, "planning_analytics.duration_model")
    supported_keys = set(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL)
    unknown_keys = sorted(set(str(key) for key in table) - supported_keys)
    if unknown_keys:
        raise ValueError(
            "planning_analytics.duration_model contains unsupported keys: "
            f"{', '.join(unknown_keys)}"
        )
    name = (
        optional_nonempty_string(table.get("name"))
        or PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["name"]
    )
    if name not in PLANNING_ANALYTICS_DURATION_MODEL_NAMES:
        allowed = ", ".join(PLANNING_ANALYTICS_DURATION_MODEL_NAMES)
        raise ValueError(
            f"planning_analytics.duration_model.name must be one of: {allowed}"
        )
    return PlanningAnalyticsDurationModelConfig(
        name=str(name),
        group_min_sample_count=positive_int(
            table.get("group_min_sample_count"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["group_min_sample_count"]),
            "planning_analytics.duration_model.group_min_sample_count",
        ),
        similarity_min_score=bounded_float(
            table.get("similarity_min_score"),
            float(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_min_score"]),
            "planning_analytics.duration_model.similarity_min_score",
            minimum=0.0,
            maximum=1.0,
        ),
        similarity_max_examples=nonnegative_int(
            table.get("similarity_max_examples"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_max_examples"]),
            "planning_analytics.duration_model.similarity_max_examples",
        ),
        similarity_blend_weight=bounded_float(
            table.get("similarity_blend_weight"),
            float(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_blend_weight"]),
            "planning_analytics.duration_model.similarity_blend_weight",
            minimum=0.0,
            maximum=1.0,
        ),
        fallback_minutes=positive_int(
            table.get("fallback_minutes"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["fallback_minutes"]),
            "planning_analytics.duration_model.fallback_minutes",
        ),
    )


def parse_planning_analytics_outputs(data: object) -> PlanningAnalyticsOutputs:
    table = expect_table(data, "planning_analytics.outputs")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(set(explicit_keys) - set(PLANNING_ANALYTICS_DEFAULT_OUTPUTS))
    if unknown_keys:
        raise ValueError(
            "planning_analytics.outputs contains unsupported keys: "
            f"{', '.join(unknown_keys)}"
        )
    return PlanningAnalyticsOutputs(
        timeline_json=optional_repo_relative_path(
            table.get("timeline_json"),
            "planning_analytics.outputs.timeline_json",
        ),
        gantt_html=optional_repo_relative_path(
            table.get("gantt_html"),
            "planning_analytics.outputs.gantt_html",
        ),
        benchmark_json=optional_repo_relative_path(
            table.get("benchmark_json"),
            "planning_analytics.outputs.benchmark_json",
        ),
        benchmark_markdown=optional_repo_relative_path(
            table.get("benchmark_markdown"),
            "planning_analytics.outputs.benchmark_markdown",
        ),
        explicit_keys=explicit_keys,
    )


def planning_analytics_report(
    config: VibeConfig,
    task_source_runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    diagnostics: list[str] = []
    status = "ready"
    if task_source_runtime is not None and not task_source_runtime.get("usable"):
        status = "task_source_unusable"
        diagnostics.append("planning analytics requires a usable task source")
    if config.planning_analytics.worklog_command is None:
        diagnostics.append(
            "project worklog adapter is not configured; authoritative evidence "
            "will come from task source state, run reports, explicit commit "
            "mapping, and bounded git metadata"
        )
    return {
        "status": status,
        "schedule_policy": config.planning_analytics.schedule_policy,
        "schedule_policy_source": (
            "explicit"
            if config.planning_analytics.is_explicit("schedule_policy")
            else "default"
        ),
        "subject_matching": config.planning_analytics.subject_matching,
        "subject_matching_source": (
            "explicit"
            if config.planning_analytics.is_explicit("subject_matching")
            else "default"
        ),
        "worklog_adapter": {
            "configured": config.planning_analytics.worklog_command is not None,
            "source": (
                "explicit"
                if config.planning_analytics.worklog_command is not None
                else "none"
            ),
        },
        "duration_model": config.planning_analytics.duration_model.to_json(),
        "coverage": {
            "authoritative_evidence": [
                "task source completion state",
                "worker reports with explicit task ids",
                "project worklog adapter records",
                "explicit commit refs or Plan-Item trailers",
            ],
            "diagnostic_only": [
                "subject matching",
                "branch names",
                "raw run log text",
            ],
        },
        "outputs": planning_analytics_output_report(config),
        "repo_artifact_outputs_enabled": (
            config.planning_analytics.outputs.has_explicit_paths
        ),
        "diagnostics": diagnostics,
    }


def planning_analytics_output_report(config: VibeConfig) -> dict[str, object]:
    outputs = config.planning_analytics.outputs
    report: dict[str, object] = {}
    for key, default_name in PLANNING_ANALYTICS_DEFAULT_OUTPUTS.items():
        explicit_path = getattr(outputs, key)
        if explicit_path is None:
            path = config.planning_analytics_state_path / default_name
            source = "default_state_dir"
        else:
            path = config.repo / explicit_path
            source = "explicit"
        report[key] = {
            "path": str(path),
            "source": source,
        }
    return report


def expect_table(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_nonempty_string(value: object) -> str | None:
    text = optional_string(value)
    if not text:
        return None
    return text


def optional_repo_relative_path(value: object, name: str) -> str | None:
    text = optional_nonempty_string(value)
    if text is None:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a repo-relative path")
    return text


def optional_bool(value: object, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def positive_int(value: object, default: int, name: str) -> int:
    parsed = optional_int(value, default, name)
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def nonnegative_int(value: object, default: int, name: str) -> int:
    parsed = optional_int(value, default, name)
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def optional_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def bounded_float(
    value: object,
    default: float,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if value is None:
        parsed = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    else:
        parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
