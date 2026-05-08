from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Protocol

from vibe_loop.config import TaskSourceConfig


DONE_STATUS = "Done"
BLOCKED_STATUSES = {"Done", "Gated", "Low"}
STATUS_RANK = {"Active": 0, "Next": 1, "Planned": 2}


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
    if config.type == "markdown-plan":
        return MarkdownPlanSource(repo / config.plan_path, config.runnable_statuses)
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
        if task.status in allowed
        and task.status not in BLOCKED_STATUSES
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

    def list_tasks(self) -> list[Task]:
        if not self.path.exists():
            raise FileNotFoundError(f"plan file not found: {self.path}")
        tasks: list[Task] = []
        section = ""
        in_table = False
        saw_separator = False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.startswith("### "):
                section = line.removeprefix("### ").strip()
            cells = split_markdown_row(line)
            if cells == [
                "ID",
                "Priority",
                "Status",
                "Dependencies",
                "Scope",
                "Acceptance",
                "Evidence",
            ]:
                in_table = True
                saw_separator = False
                continue
            if not in_table:
                continue
            if is_separator_row(cells):
                saw_separator = True
                continue
            if not saw_separator or len(cells) != 7:
                continue
            task_id = cells[0]
            if not task_id or task_id == "ID":
                continue
            tasks.append(
                Task(
                    task_id=task_id,
                    title=first_sentence(cells[4]) or task_id,
                    section=section,
                    priority=cells[1],
                    status=cells[2],
                    dependencies=parse_dependencies(cells[3]),
                    scope=cells[4],
                    acceptance=cells[5],
                    evidence=cells[6],
                    source=f"{self.path}:{section}",
                    order=len(tasks),
                )
            )
        return tasks

    def probe(self, task_id: str) -> Task | None:
        return next(
            (task for task in self.list_tasks() if task.task_id == task_id), None
        )


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


def parse_dependencies(value: str) -> tuple[str, ...]:
    if value.lower() == "none":
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def first_sentence(value: str) -> str:
    for separator in (". ", ";"):
        if separator in value:
            return value.split(separator, 1)[0].strip()
    return value.strip()
