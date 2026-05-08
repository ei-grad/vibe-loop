from __future__ import annotations

import dataclasses
import shutil
import tomllib
from pathlib import Path
from typing import Any


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
TASK_SOURCE_SOURCE_KEYS = frozenset(
    {
        "type",
        "plan_path",
        "plan_paths",
        "list",
        "next",
        "probe",
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

AGENT_COMMAND_DEFAULTS = {
    "codex": {
        "command": "codex exec '$vibe-loop {task_id}'",
        "selection_command": "codex exec {prompt}",
    },
    "claude": {
        "command": "claude -p '$vibe-loop {task_id}'",
        "selection_command": "claude -p {prompt}",
    },
}
SUPPORTED_AGENT_CLIS = tuple(AGENT_COMMAND_DEFAULTS)
AGENT_DEFAULT_POLICY = (
    "Use the sole available supported CLI for omitted agent commands; require "
    "explicit .vibe-loop.toml settings when multiple or no supported CLIs are "
    "available."
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
            "detected": self.detected.to_json(),
            "default_policy": AGENT_DEFAULT_POLICY,
            "diagnostics": self.diagnostics(),
        }


@dataclasses.dataclass(frozen=True)
class TaskSourceConfig:
    type: str = "markdown-plan"
    plan_path: str | None = None
    plan_paths: tuple[str, ...] = DEFAULT_PLAN_PATHS
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
class VibeConfig:
    repo: Path
    main_branch: str = "main"
    state_dir: str = ".vibe-loop"
    agent: AgentConfig = dataclasses.field(default_factory=AgentConfig)
    task_source: TaskSourceConfig = dataclasses.field(default_factory=TaskSourceConfig)
    completion: CompletionConfig = dataclasses.field(default_factory=CompletionConfig)

    @property
    def state_path(self) -> Path:
        return self.repo / self.state_dir

    @property
    def generated_task_profile_path(self) -> Path:
        return self.state_path / GENERATED_TASK_PROFILE_CACHE_FILE


def load_config(repo: Path) -> VibeConfig:
    repo = repo.resolve()
    data = read_config_file(repo / ".vibe-loop.toml")
    task_source = parse_task_source(data.get("task_source", {}))
    completion = parse_completion(data.get("completion", {}), repo)
    agent = parse_agent(data.get("agent", {}))
    return VibeConfig(
        repo=repo,
        main_branch=str(data.get("main_branch") or "main"),
        state_dir=str(data.get("state_dir") or ".vibe-loop"),
        agent=agent,
        task_source=task_source,
        completion=completion,
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
    command, command_source = resolve_agent_default(
        "command",
        configured_command,
        detected,
    )
    selection_command, selection_command_source = resolve_agent_default(
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
) -> tuple[str | None, str]:
    if configured is not None:
        return configured, "explicit"
    available = detected.available
    if len(available) == 1:
        agent_name = available[0]
        return AGENT_COMMAND_DEFAULTS[agent_name][key], f"auto:{agent_name}"
    if not available:
        return None, "unresolved:no-supported-cli"
    return None, "unresolved:multiple-supported-clis"


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
    statuses = table.get("runnable_statuses")
    if statuses is None:
        runnable = DEFAULT_RUNNABLE_STATUSES
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
        list_command=optional_string(table.get("list")),
        next_command=optional_string(table.get("next")),
        probe_command=optional_string(table.get("probe")),
        runnable_statuses=runnable,
        explicit_keys=explicit_keys,
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


def optional_bool(value: object, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")
