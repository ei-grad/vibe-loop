from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentResolutionError,
    load_config,
)
from vibe_loop.generated_profiles import (
    GeneratedTaskSourceRuntimeError,
    configure_generated_task_source,
    generated_task_cache_report,
    read_only_generated_cache_notice,
    read_only_generated_cache_message,
    runtime_task_source_report,
)
from vibe_loop.locks import LockBusy, LockManager, LockOwnerMismatch
from vibe_loop.runner import VibeRunner
from vibe_loop.runs import RunResult, RunStore, WorkerReport, WORKER_REPORT_STATUSES
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
from vibe_loop.workers import WorkerView, build_worker_views


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
    run_all.add_argument("--jobs", type=int, default=1)

    workers = subparsers.add_parser("workers", help="List active worker runs")
    add_repo_argument(workers)
    workers.add_argument("--json", action="store_true")

    integration = subparsers.add_parser(
        "main-integration",
        help="Manage the advisory main integration lock",
    )
    add_repo_argument(integration)
    integration.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    integration_subparsers = integration.add_subparsers(
        dest="main_integration_command",
        required=True,
    )
    integration_status = integration_subparsers.add_parser(
        "status",
        help="Show the main integration lock",
    )
    add_repo_argument(integration_status)
    integration_status.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS
    )
    integration_acquire = integration_subparsers.add_parser(
        "acquire",
        help="Acquire the main integration lock",
    )
    add_repo_argument(integration_acquire)
    integration_acquire.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS
    )
    integration_acquire.add_argument("--run-id", default="")
    integration_acquire.add_argument("--task-id", default="")
    integration_acquire.add_argument("--pid", type=int, default=0)
    integration_release = integration_subparsers.add_parser(
        "release",
        help="Release the main integration lock",
    )
    add_repo_argument(integration_release)
    integration_release.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS
    )
    integration_release.add_argument("--run-id", default="")
    integration_release.add_argument("--task-id", default="")

    report = subparsers.add_parser("report", help="Record a worker result report")
    add_repo_argument(report)
    report.add_argument("--run-id", required=True)
    report.add_argument("--task-id", required=True)
    report.add_argument("--status", required=True, choices=WORKER_REPORT_STATUSES)
    report.add_argument("--commit", default="")
    report.add_argument("--message", default="")
    report.add_argument(
        "--metadata-json",
        help="JSON object with additional structured report metadata",
    )

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
            jobs=args.jobs,
        )
        print(json.dumps([result.to_json() for result in results], indent=2))
        return run_until_done_exit_code(results)

    if args.command == "workers":
        runner = VibeRunner(config)
        workers = build_worker_views(runner.lock_manager, runner.run_store)
        if args.json:
            print(json.dumps([worker.to_json() for worker in workers], indent=2))
        else:
            output = render_workers(workers)
            if output:
                print(output)
        return 0

    if args.command == "main-integration":
        return dispatch_main_integration(args, config)

    if args.command == "report":
        report = WorkerReport(
            run_id=args.run_id,
            task_id=args.task_id,
            status=args.status,
            commit=args.commit,
            message=args.message,
            metadata=parse_metadata_json(args.metadata_json),
        )
        RunStore(config.state_path / "runs.jsonl").append_report(report)
        print(json.dumps(report.to_json(), indent=2))
        return 0

    if args.command == "doctor":
        print(
            json.dumps(
                {
                    "repo": str(config.repo),
                    "main_branch": config.main_branch,
                    "state_dir": config.state_dir,
                    "task_source": config.task_source.to_json(),
                    "task_source_runtime": runtime_task_source_report(config),
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


def dispatch_main_integration(args: argparse.Namespace, config) -> int:
    manager = LockManager(config.state_path / "locks")
    if args.main_integration_command == "status":
        status = manager.main_integration_status()
        if json_requested(args):
            print(json.dumps(status.to_json(), indent=2))
        else:
            output = render_main_integration_status(status.to_json())
            if output:
                print(output)
        return 0

    run_id, task_id = worker_identity_from_args(args)
    if not run_id or not task_id:
        print(
            "main-integration requires --run-id and --task-id "
            "or VIBE_LOOP_RUN_ID and VIBE_LOOP_TASK_ID",
            file=sys.stderr,
        )
        return 2

    if args.main_integration_command == "acquire":
        owner_metadata = main_integration_owner_metadata(
            args,
            manager,
            run_id=run_id,
            task_id=task_id,
        )
        if owner_metadata is None:
            status = manager.main_integration_status()
            if status.locked:
                payload = {"acquired": False, "status": status.to_json()}
                if json_requested(args):
                    print(json.dumps(payload, indent=2))
                else:
                    print(
                        render_main_integration_busy(payload["status"]),
                        file=sys.stderr,
                    )
                return 1
            print(
                "main-integration acquire requires an active task lock with "
                "matching run_id/task_id or an explicit --pid",
                file=sys.stderr,
            )
            return 2
        try:
            manager.acquire_main_integration(
                task_id=task_id,
                run_id=run_id,
                metadata=owner_metadata,
            )
        except LockBusy:
            status = manager.main_integration_status()
            payload = {"acquired": False, "status": status.to_json()}
            if json_requested(args):
                print(json.dumps(payload, indent=2))
            else:
                print(render_main_integration_busy(payload["status"]), file=sys.stderr)
            return 1
        status = manager.main_integration_status()
        payload = {"acquired": True, "status": status.to_json()}
        if json_requested(args):
            print(json.dumps(payload, indent=2))
        else:
            print(render_main_integration_acquired(payload["status"]))
        return 0

    if args.main_integration_command == "release":
        try:
            released = manager.release_main_integration(task_id=task_id, run_id=run_id)
        except LockOwnerMismatch as exc:
            status = manager.main_integration_status()
            payload = {
                "released": False,
                "error": "owner_mismatch",
                "expected": {"run_id": exc.run_id, "task_id": exc.task_id},
                "status": status.to_json(),
            }
            if json_requested(args):
                print(json.dumps(payload, indent=2))
            else:
                print(
                    "main-integration release refused: owner_mismatch "
                    f"holder_run={status.to_json()['run_id']} "
                    f"holder_task={status.to_json()['owner_task_id']}",
                    file=sys.stderr,
                )
            return 1
        status = manager.main_integration_status()
        payload = {"released": released, "status": status.to_json()}
        if json_requested(args):
            print(json.dumps(payload, indent=2))
        elif released:
            print("main-integration released")
        else:
            print("main-integration lock not found", file=sys.stderr)
        return 0 if released else 1

    raise AssertionError(args.main_integration_command)


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
    runtime_task_source = runner.source_resolution.task_source
    locked_ids = {
        str(task_lock.get("task_id"))
        for task_lock in runner.lock_manager.list_locks()
        if task_lock.get("task_id")
    }
    return build_task_views(
        tasks,
        locked_ids,
        runnable_statuses=runtime_task_source.runnable_statuses,
    )


def read_only_task_operation(config, operation):
    try:
        result = operation()
    except GeneratedTaskSourceRuntimeError as exc:
        raise RuntimeError(str(exc)) from exc
    except AgentResolutionError:
        raise
    except (FileNotFoundError, ValueError) as exc:
        message = read_only_generated_cache_message(config)
        raise RuntimeError(f"{exc}; {message}") from exc
    notice = read_only_task_source_notice(config)
    if notice:
        print(f"vibe-loop: {notice}", file=sys.stderr)
    return result


def read_only_task_source_notice(config) -> str | None:
    cache_notice = read_only_generated_cache_notice(config)
    report = runtime_task_source_report(config)
    origin = report.get("origin")
    if cache_notice:
        if origin == "default_markdown_discovery":
            return f"{cache_notice}; task discovery source=default_markdown_discovery"
        return cache_notice
    if origin == "generated_cache":
        return f"task discovery source=generated_cache path={report.get('cache_path')}"
    if origin == "default_markdown_discovery":
        return "task discovery source=default_markdown_discovery"
    if origin == "command_output":
        return "task discovery source=command_output"
    if origin == "explicit_config":
        task_source = report.get("task_source")
        keys = []
        if isinstance(task_source, dict):
            explicit_keys = task_source.get("explicit_source_keys")
            if isinstance(explicit_keys, list):
                keys = [str(key) for key in explicit_keys]
        suffix = f" keys={','.join(keys)}" if keys else ""
        return f"task discovery source=explicit_config{suffix}"
    return None


def task_views_for_tasks(config, tasks: list[Task]):
    ids = {task.task_id for task in tasks}
    return [view for view in all_task_views(config) if view.task.task_id in ids]


def selected_task_json(config, task: Task) -> dict[str, object]:
    payload = task.to_json()
    payload.update(
        {
            "task_source_runtime": runtime_task_source_report(config),
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


def render_workers(workers: list[WorkerView]) -> str:
    lines: list[str] = []
    for worker in workers:
        payload = worker.to_json()
        pid = payload["pid"] if payload["pid"] is not None else "-"
        stale = f"\t{payload['stale_reason']}" if payload["stale_reason"] else ""
        lines.append(
            f"{payload['task_id']}\t{payload['run_id']}\t{payload['state']}"
            f"\tprocess={payload['process_state']}\tpid={pid}"
            f"\tstarted={payload['started_at']}\tlog={payload['log']}"
            f"\tcommand={payload['command']}{stale}"
        )
    return "\n".join(lines)


def worker_identity_from_args(args: argparse.Namespace) -> tuple[str, str]:
    run_id = args.run_id or os.environ.get("VIBE_LOOP_RUN_ID", "")
    task_id = args.task_id or os.environ.get("VIBE_LOOP_TASK_ID", "")
    return run_id, task_id


def main_integration_owner_metadata(
    args: argparse.Namespace,
    manager: LockManager,
    *,
    run_id: str,
    task_id: str,
) -> dict[str, object] | None:
    if args.pid > 0:
        return {"pid": args.pid, "pid_source": "explicit_cli"}
    for lock_metadata in manager.list_locks():
        if lock_metadata.get("task_id") != task_id:
            continue
        if lock_metadata.get("run_id") != run_id:
            continue
        worker_pid = positive_int(lock_metadata.get("worker_pid"))
        if worker_pid is not None:
            return {
                "pid": worker_pid,
                "pid_source": "active_task_lock:worker_pid",
            }
        legacy_pid = positive_int(lock_metadata.get("pid"))
        if legacy_pid is not None:
            return {"pid": legacy_pid, "pid_source": "active_task_lock:pid"}
    return None


def json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def render_main_integration_status(payload: dict[str, object]) -> str:
    if not payload["locked"]:
        return f"main-integration\t{payload['state']}\tpath={payload['path']}"
    stale = f"\t{payload['stale_reason']}" if payload["stale_reason"] else ""
    return (
        f"main-integration\t{payload['state']}"
        f"\tprocess={payload['process_state']}"
        f"\trun={payload['run_id']}"
        f"\ttask={payload['owner_task_id']}"
        f"\tpid={payload['pid'] if payload['pid'] is not None else '-'}"
        f"\tpid_source={payload['pid_source']}"
        f"\tstarted={payload['started_at']}"
        f"\tpath={payload['path']}{stale}"
    )


def render_main_integration_acquired(payload: dict[str, object]) -> str:
    return (
        "main-integration acquired "
        f"run={payload['run_id']} task={payload['owner_task_id']} "
        f"path={payload['path']}"
    )


def render_main_integration_busy(payload: dict[str, object]) -> str:
    return (
        "main-integration busy "
        f"state={payload['state']} process={payload['process_state']} "
        f"run={payload['run_id']} task={payload['owner_task_id']} "
        f"path={payload['path']}"
    )


def parse_metadata_json(value: str | None) -> dict[str, object]:
    if value is None:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--metadata-json must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--metadata-json must be a JSON object")
    return payload


def run_until_done_exit_code(results: list[RunResult]) -> int:
    if not results:
        return 0
    if any(result.classification in {"failed", "unknown"} for result in results):
        return 1
    return 0
