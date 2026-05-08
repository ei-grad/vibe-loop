from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentResolutionError,
    load_config,
)
from vibe_loop.generated_profiles import (
    configure_generated_task_source,
    generated_task_cache_report,
    read_only_generated_cache_notice,
    read_only_generated_cache_message,
)
from vibe_loop.runner import VibeRunner
from vibe_loop.skills import install_skills
from vibe_loop.task_views import (
    build_task_views,
    filter_views,
    parse_status_filter,
    render_task_list,
    render_task_tree,
    task_tree_json,
)
from vibe_loop.tasks import Task


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except Exception as exc:
        print(f"vibe-loop: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe-loop")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    tasks_parser = subparsers.add_parser("tasks", help="Inspect task graph")
    add_repo_argument(tasks_parser)
    tasks_parser.add_argument("--json", action="store_true")
    task_subparsers = tasks_parser.add_subparsers(dest="tasks_command")

    tasks_list = task_subparsers.add_parser("list", help="List tasks")
    add_repo_argument(tasks_list)
    add_task_filter_arguments(tasks_list)
    tasks_list.add_argument("--json", action="store_true")

    tasks_runnable = task_subparsers.add_parser(
        "runnable", help="List dependency-ready unlocked tasks"
    )
    add_repo_argument(tasks_runnable)
    tasks_runnable.add_argument("--json", action="store_true")

    tasks_next = task_subparsers.add_parser("next", help="Print the next task")
    add_repo_argument(tasks_next)
    tasks_next.add_argument("--ask-agent", action="store_true")
    tasks_next.add_argument("--json", action="store_true")

    tasks_inspect = task_subparsers.add_parser("inspect", help="Show one task")
    add_repo_argument(tasks_inspect)
    tasks_inspect.add_argument("task_id")
    tasks_inspect.add_argument("--json", action="store_true")

    tasks_tree = task_subparsers.add_parser("tree", help="Show dependency tree")
    add_repo_argument(tasks_tree)
    add_task_filter_arguments(tasks_tree, default_show_done=False)
    tasks_tree.add_argument("--json", action="store_true")

    tasks_locks = task_subparsers.add_parser("locks", help="List task locks")
    add_repo_argument(tasks_locks)
    tasks_locks.add_argument("--json", action="store_true")

    tasks_configure = task_subparsers.add_parser(
        "configure",
        help="Report task configuration readiness",
    )
    add_repo_argument(tasks_configure)
    tasks_configure.add_argument("--json", action="store_true")

    next_parser = subparsers.add_parser("next", help="Print the next runnable task")
    add_repo_argument(next_parser)
    next_parser.add_argument("--ask-agent", action="store_true")
    next_parser.add_argument("--json", action="store_true")

    run_next = subparsers.add_parser("run-next", help="Run one selected task")
    add_repo_argument(run_next)
    run_next.add_argument("--ask-agent", action="store_true")

    run_all = subparsers.add_parser(
        "run-until-done",
        help="Run one-slice loops until no runnable tasks remain",
    )
    add_repo_argument(run_all)
    run_all.add_argument("--ask-agent", action="store_true")
    run_all.add_argument("--max-slices", type=int, default=0)
    run_all.add_argument("--continue-on-failure", action="store_true")

    doctor = subparsers.add_parser("doctor", help="Print resolved configuration")
    add_repo_argument(doctor)

    install = subparsers.add_parser("install-skills", help="Install bundled skills")
    add_repo_argument(install)
    install.add_argument("--codex", action="store_true")
    install.add_argument("--claude", action="store_true")
    install.add_argument("--home", type=Path, default=Path.home())
    return parser


def add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=argparse.SUPPRESS)


def add_task_filter_arguments(
    parser: argparse.ArgumentParser,
    default_show_done: bool = True,
) -> None:
    parser.add_argument("--status", help="Comma-separated status filter")
    parser.add_argument("--ready-only", action="store_true")
    if default_show_done:
        parser.add_argument("--hide-done", action="store_true")
    else:
        parser.add_argument("--show-done", action="store_true")


def dispatch(args: argparse.Namespace) -> int:
    config = load_config(args.repo)
    if args.command == "tasks":
        return dispatch_tasks(args, config)

    if args.command == "next":
        task = read_only_task_operation(
            config,
            lambda: VibeRunner(config).select_task(ask_agent=args.ask_agent),
        )
        if task is None:
            return 2
        if args.json:
            print(json.dumps(selected_task_json(config, task), indent=2))
        else:
            print(task.task_id)
        return 0

    if args.command == "run-next":
        runner = VibeRunner(config)
        result = runner.run_next(ask_agent=args.ask_agent)
        if result is None:
            print("no runnable tasks", file=sys.stderr)
            return 2
        print(json.dumps(result.to_json(), indent=2))
        return 0 if result.classification == "completed" else 1

    if args.command == "run-until-done":
        runner = VibeRunner(config)
        results = runner.run_until_done(
            ask_agent=args.ask_agent,
            max_slices=args.max_slices,
            continue_on_failure=args.continue_on_failure,
        )
        print(json.dumps([result.to_json() for result in results], indent=2))
        if not results:
            return 0
        if results[-1].classification in {"failed", "unknown"}:
            return 1
        return 0

    if args.command == "doctor":
        print(
            json.dumps(
                {
                    "repo": str(config.repo),
                    "main_branch": config.main_branch,
                    "state_dir": config.state_dir,
                    "task_source": config.task_source.to_json(),
                    "generated_task_profile": generated_task_cache_report(config),
                    "agent": config.agent.to_json(),
                    "completion": config.completion.__dict__,
                },
                indent=2,
                default=list,
            )
        )
        return 0

    if args.command == "install-skills":
        installed = install_skills(args.codex, args.claude, args.home)
        for path in installed:
            print(path)
        return 0

    raise AssertionError(args.command)


def dispatch_tasks(args: argparse.Namespace, config) -> int:
    if args.tasks_command in {None, "runnable"}:
        tasks = read_only_task_operation(
            config,
            lambda: VibeRunner(config).list_candidates(),
        )
        if args.json:
            print(json.dumps([task.to_json() for task in tasks], indent=2))
        else:
            print(render_task_list(task_views_for_tasks(config, tasks)))
        return 0

    if args.tasks_command == "next":
        task = read_only_task_operation(
            config,
            lambda: VibeRunner(config).select_task(ask_agent=args.ask_agent),
        )
        if task is None:
            return 2
        if args.json:
            print(json.dumps(selected_task_json(config, task), indent=2))
        else:
            print(task.task_id)
        return 0

    if args.tasks_command == "locks":
        runner = VibeRunner(config)
        locks = runner.lock_manager.list_locks()
        if args.json:
            print(json.dumps(locks, indent=2))
        else:
            for task_lock in locks:
                print(
                    f"{task_lock.get('task_id', '')}\t{task_lock.get('run_id', '')}\t"
                    f"{task_lock.get('started_at', '')}\t{task_lock.get('path', '')}"
                )
        return 0

    if args.tasks_command == "configure":
        result = configure_generated_task_source(config)
        payload = result.to_json()
        payload["agent"] = config.agent.to_json()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"tasks configure: cache status={payload['status']}")
            print(f"cache: {result.cache_path}")
            print(f"detected agents: {config.agent.detected.summary()}")
            print(f"agent default policy source: {AGENT_DEFAULT_POLICY_SOURCE}")
            print(f"agent default policy: {AGENT_DEFAULT_POLICY}")
            print(f"agent.command source: {config.agent.command_source}")
            print(
                "agent.selection_command source: "
                f"{config.agent.selection_command_source}"
            )
            diagnostics = list(result.diagnostics) + config.agent.diagnostics()
            if diagnostics:
                print("diagnostics:")
                for diagnostic in diagnostics:
                    print(f"- {diagnostic}")
        return result.exit_code

    views = read_only_task_operation(config, lambda: all_task_views(config))
    if args.tasks_command == "inspect":
        view = next(
            (
                candidate
                for candidate in views
                if candidate.task.task_id == args.task_id
            ),
            None,
        )
        if view is None:
            print(f"task not found: {args.task_id}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(view.to_json(), indent=2))
        else:
            print(render_task_inspect(view))
        return 0

    if args.tasks_command == "list":
        selected = filter_views(
            views,
            statuses=parse_status_filter(args.status),
            ready_only=args.ready_only,
            include_done=not args.hide_done,
        )
        if args.json:
            print(json.dumps([view.to_json() for view in selected], indent=2))
        else:
            print(render_task_list(selected))
        return 0

    if args.tasks_command == "tree":
        selected = filter_views(
            views,
            statuses=parse_status_filter(args.status),
            ready_only=args.ready_only,
            include_done=args.show_done,
        )
        if args.json:
            print(json.dumps(task_tree_json(selected), indent=2))
        else:
            print(render_task_tree(selected))
        return 0

    raise AssertionError(args.tasks_command)


def all_task_views(config):
    runner = VibeRunner(config)
    tasks = runner.source.list_tasks()
    locked_ids = {
        str(task_lock.get("task_id"))
        for task_lock in runner.lock_manager.list_locks()
        if task_lock.get("task_id")
    }
    return build_task_views(tasks, locked_ids)


def read_only_task_operation(config, operation):
    try:
        result = operation()
    except AgentResolutionError:
        raise
    except (FileNotFoundError, ValueError) as exc:
        message = read_only_generated_cache_message(config)
        raise RuntimeError(f"{exc}; {message}") from exc
    notice = read_only_generated_cache_notice(config)
    if notice:
        print(f"vibe-loop: {notice}", file=sys.stderr)
    return result


def task_views_for_tasks(config, tasks: list[Task]):
    ids = {task.task_id for task in tasks}
    return [view for view in all_task_views(config) if view.task.task_id in ids]


def selected_task_json(config, task: Task) -> dict[str, object]:
    payload = task.to_json()
    payload.update(
        {
            "agent_selection_command_source": config.agent.selection_command_source,
            "agent_default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "agent_default_policy": AGENT_DEFAULT_POLICY,
        }
    )
    return payload


def render_task_inspect(view) -> str:
    task = view.task
    lines = [
        f"{task.task_id} [{task.status}/{task.priority}] {task.title}",
        f"section: {task.section or '-'}",
        f"ready: {'yes' if view.ready else 'no'}",
        f"locked: {'yes' if view.locked else 'no'}",
        f"dependencies: {', '.join(task.dependencies) if task.dependencies else 'none'}",
        f"source: {task.source or '-'}",
        "",
        "scope:",
        task.scope or "-",
        "",
        "acceptance:",
        task.acceptance or "-",
        "",
        "evidence:",
        task.evidence or "-",
    ]
    return "\n".join(lines)
