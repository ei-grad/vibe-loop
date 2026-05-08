from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    command: str = "codex exec '$vibe-loop {task_id}'"
    selection_command: str = "codex exec {prompt}"


@dataclasses.dataclass(frozen=True)
class TaskSourceConfig:
    type: str = "markdown-plan"
    plan_path: str = "docs/PLAN.md"
    list_command: str | None = None
    next_command: str | None = None
    probe_command: str | None = None
    runnable_statuses: tuple[str, ...] = ("Active", "Next", "Planned")


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
    return AgentConfig(
        command=str(table.get("command") or AgentConfig.command),
        selection_command=str(
            table.get("selection_command") or AgentConfig.selection_command
        ),
    )


def parse_task_source(data: object) -> TaskSourceConfig:
    table = expect_table(data, "task_source")
    statuses = table.get("runnable_statuses")
    if statuses is None:
        runnable = TaskSourceConfig.runnable_statuses
    elif isinstance(statuses, list) and all(isinstance(item, str) for item in statuses):
        runnable = tuple(statuses)
    else:
        raise ValueError("task_source.runnable_statuses must be an array of strings")
    return TaskSourceConfig(
        type=str(table.get("type") or "markdown-plan"),
        plan_path=str(table.get("plan_path") or "docs/PLAN.md"),
        list_command=optional_string(table.get("list")),
        next_command=optional_string(table.get("next")),
        probe_command=optional_string(table.get("probe")),
        runnable_statuses=runnable,
    )


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
