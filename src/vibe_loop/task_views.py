from __future__ import annotations

import dataclasses
from collections import defaultdict

from vibe_loop.tasks import (
    DEFAULT_RUNNABLE_STATUSES,
    Task,
    priority_rank,
    status_rank,
)


@dataclasses.dataclass(frozen=True)
class TaskView:
    task: Task
    ready: bool
    locked: bool

    def to_json(self) -> dict[str, object]:
        payload = self.task.to_json()
        payload["ready"] = self.ready
        payload["locked"] = self.locked
        return payload


def build_task_views(
    tasks: list[Task],
    locked_ids: set[str],
    runnable_statuses: tuple[str, ...] = DEFAULT_RUNNABLE_STATUSES,
) -> list[TaskView]:
    done = {task.task_id for task in tasks if task.done}
    runnable = set(runnable_statuses)
    return [
        TaskView(
            task=task,
            ready=task.status in runnable
            and task.task_id not in locked_ids
            and all(dep in done for dep in task.dependencies),
            locked=task.task_id in locked_ids,
        )
        for task in tasks
    ]


def filter_views(
    views: list[TaskView],
    statuses: set[str] | None = None,
    ready_only: bool = False,
    include_done: bool = True,
) -> list[TaskView]:
    filtered = []
    for view in views:
        if statuses is not None and view.task.status not in statuses:
            continue
        if statuses is None and not include_done and view.task.done:
            continue
        if ready_only and not view.ready:
            continue
        filtered.append(view)
    return sorted(filtered, key=view_sort_key)


def view_sort_key(view: TaskView) -> tuple[int, int, int]:
    return (
        status_rank(view.task.status),
        priority_rank(view.task.priority),
        view.task.order,
    )


def render_task_list(views: list[TaskView]) -> str:
    lines: list[str] = []
    for view in views:
        markers = []
        if view.ready:
            markers.append("ready")
        if view.locked:
            markers.append("locked")
        suffix = f" ({', '.join(markers)})" if markers else ""
        lines.append(
            f"{view.task.task_id}\t{view.task.priority}\t{view.task.status}\t"
            f"{view.task.title}{suffix}"
        )
    return "\n".join(lines)


def render_task_tree(views: list[TaskView]) -> str:
    by_section: dict[str, list[TaskView]] = defaultdict(list)
    for view in views:
        by_section[view.task.section or "Tasks"].append(view)

    sections = sorted(by_section.items(), key=lambda item: section_order(item[1]))
    blocks: list[str] = []
    for section, section_views in sections:
        by_id = {view.task.task_id: view for view in section_views}
        children: dict[str, list[TaskView]] = defaultdict(list)
        roots: list[TaskView] = []
        for view in section_views:
            parents = [dep for dep in view.task.dependencies if dep in by_id]
            if not parents:
                roots.append(view)
            for parent in parents:
                children[parent].append(view)
        lines = [section]
        seen: set[str] = set()
        for root in sorted(roots, key=view_sort_key):
            append_tree_lines(lines, root, children, seen, depth=1)
        for view in sorted(section_views, key=view_sort_key):
            if view.task.task_id not in seen:
                append_tree_lines(lines, view, children, seen, depth=1)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def task_tree_json(views: list[TaskView]) -> list[dict[str, object]]:
    by_id = {view.task.task_id: view for view in views}
    children: dict[str, list[TaskView]] = defaultdict(list)
    roots: list[TaskView] = []
    for view in views:
        parents = [dep for dep in view.task.dependencies if dep in by_id]
        if not parents:
            roots.append(view)
        for parent in parents:
            children[parent].append(view)
    return [
        tree_node(root, children, set()) for root in sorted(roots, key=view_sort_key)
    ]


def append_tree_lines(
    lines: list[str],
    view: TaskView,
    children: dict[str, list[TaskView]],
    seen: set[str],
    depth: int,
) -> None:
    if view.task.task_id in seen:
        return
    seen.add(view.task.task_id)
    marker = " *" if view.ready else ""
    lock_marker = " locked" if view.locked else ""
    indent = "  " * depth
    lines.append(
        f"{indent}{view.task.task_id} [{view.task.status}/{view.task.priority}] "
        f"{view.task.title}{marker}{lock_marker}"
    )
    for child in sorted(children.get(view.task.task_id, []), key=view_sort_key):
        append_tree_lines(lines, child, children, seen, depth + 1)


def tree_node(
    view: TaskView,
    children: dict[str, list[TaskView]],
    seen: set[str],
) -> dict[str, object]:
    if view.task.task_id in seen:
        payload = view.to_json()
        payload["cycle"] = True
        payload["children"] = []
        return payload
    seen.add(view.task.task_id)
    payload = view.to_json()
    payload["children"] = [
        tree_node(child, children, set(seen))
        for child in sorted(children.get(view.task.task_id, []), key=view_sort_key)
    ]
    return payload


def section_order(views: list[TaskView]) -> tuple[int, str]:
    return (min(view.task.order for view in views), views[0].task.section)


def parse_status_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}
