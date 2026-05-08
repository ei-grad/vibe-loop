from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vibe_loop.config import load_config
from vibe_loop.runner import VibeRunner
from vibe_loop.skills import install_skills
from vibe_loop.tasks import build_task_source, runnable_tasks


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

    tasks_parser = subparsers.add_parser("tasks", help="List runnable tasks")
    add_repo_argument(tasks_parser)
    tasks_parser.add_argument("--json", action="store_true")

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


def dispatch(args: argparse.Namespace) -> int:
    config = load_config(args.repo)
    if args.command == "tasks":
        source = build_task_source(config.repo, config.task_source)
        tasks = runnable_tasks(source, config.task_source.runnable_statuses)
        if args.json:
            print(json.dumps([task.to_json() for task in tasks], indent=2))
        else:
            for task in tasks:
                print(f"{task.task_id}\t{task.priority}\t{task.status}\t{task.title}")
        return 0

    if args.command == "next":
        runner = VibeRunner(config)
        task = runner.select_task(ask_agent=args.ask_agent)
        if task is None:
            return 2
        if args.json:
            print(json.dumps(task.to_json(), indent=2))
        else:
            print(task.task_id)
        return 0

    if args.command == "run-next":
        runner = VibeRunner(config)
        result = runner.run_next(ask_agent=args.ask_agent)
        if result is None:
            print("no runnable tasks")
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
                    "task_source": config.task_source.__dict__,
                    "agent": config.agent.__dict__,
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
