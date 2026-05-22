from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distribution as metadata_distribution
from importlib.metadata import version as metadata_version
from pathlib import Path

from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentResolutionError,
    load_config,
    planning_analytics_report,
)
from vibe_loop.eval_runner import (
    LocalSkillEvalConfig,
    parse_agent_command_specs,
    run_local_demo_eval,
)
from vibe_loop.eval_release import (
    build_release_readiness_record,
    load_external_benchmark_evidence,
    load_json_mapping,
    parse_parked_regression_specs,
    render_release_readiness_summary,
    write_release_readiness_record,
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
from vibe_loop.planning_artifacts import (
    build_planning_artifact_bundle,
    check_planning_artifacts,
    inspect_planning_artifacts,
    planning_artifact_paths,
    write_planning_artifacts,
)
from vibe_loop.planning_benchmark import (
    build_duration_benchmark,
    check_duration_benchmark_reports,
    write_duration_benchmark_reports,
)
from vibe_loop.planning_evidence import DEFAULT_GIT_COMMIT_LIMIT
from vibe_loop.planning_timeline import (
    build_planning_timeline,
    lookup_timeline_task,
    read_timeline_file,
)
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
from vibe_loop.workers import (
    StaleLock,
    WorkerView,
    WorkspaceClaimError,
    build_worker_views,
    claim_worker_workspace,
    clean_stale_locks,
    collect_stale_locks,
)

PACKAGE_NAME = "vibe-loop"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"{PACKAGE_NAME} {package_version()}")
        return 0
    if args.command is None:
        parser.error("the following arguments are required: command")
    try:
        return dispatch(args)
    except Exception as exc:
        print(f"vibe-loop: {exc}", file=sys.stderr)
        return 1


def package_version() -> str:
    try:
        version = metadata_version(PACKAGE_NAME)
    except PackageNotFoundError:
        version = "0+unknown"
    git_sha = package_git_commit_sha(version)
    if git_sha:
        return f"{version} (git {git_sha})"
    return version


def package_git_commit_sha(version: str) -> str:
    direct_url = package_direct_url()
    if direct_url is None:
        return source_tree_git_commit_sha(version)
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict) and vcs_info.get("vcs") == "git":
        requested_revision = str(vcs_info.get("requested_revision") or "")
        if requested_revision_is_release_tag(requested_revision, version):
            return ""
        return short_git_sha(str(vcs_info.get("commit_id") or ""))
    dir_info = direct_url.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
        return source_tree_git_commit_sha(version)
    return ""


def package_direct_url() -> dict[str, object] | None:
    try:
        raw = metadata_distribution(PACKAGE_NAME).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def requested_revision_is_release_tag(revision: str, version: str) -> bool:
    normalized = revision.strip().removeprefix("refs/tags/")
    return normalized in {version, f"v{version}"}


def source_tree_git_commit_sha(version: str) -> str:
    git_root = find_source_git_root(Path(__file__).resolve())
    if git_root is None or source_tree_has_release_tag(git_root, version):
        return ""
    return git_short_commit_sha(git_root)


def find_source_git_root(path: Path) -> Path | None:
    for parent in (path.parent, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def source_tree_has_release_tag(git_root: Path, version: str) -> bool:
    result = run_git(git_root, "tag", "--points-at", "HEAD")
    if result is None or result.returncode != 0:
        return False
    release_tags = {version, f"v{version}"}
    return any(tag.strip() in release_tags for tag in result.stdout.splitlines())


def git_short_commit_sha(git_root: Path) -> str:
    result = run_git(git_root, "rev-parse", "--short=12", "HEAD")
    if result is None or result.returncode != 0:
        return ""
    return short_git_sha(result.stdout.strip())


def run_git(git_root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("git", "-C", str(git_root), *args),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def short_git_sha(commit: str) -> str:
    value = commit.strip().lower()
    if len(value) < 7 or any(char not in "0123456789abcdef" for char in value):
        return ""
    return value[:12]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe-loop")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed vibe-loop version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")

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
    tasks_configure_output = tasks_configure.add_mutually_exclusive_group()
    tasks_configure_output.add_argument("--json", action="store_true")
    tasks_configure_output.add_argument(
        "--promotion-toml",
        action="store_true",
        help="Print a .vibe-loop.toml task_source snippet for a valid profile",
    )
    tasks_configure.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate a candidate profile without writing the cache",
    )
    tasks_configure.add_argument(
        "--force-refresh",
        action="store_true",
        help="Regenerate the profile even when the current cache is fresh",
    )

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
    run_all.add_argument("--max-tasks", type=int, default=0)
    run_all.add_argument("--continue-on-failure", action="store_true")
    run_all.add_argument("--jobs", type=int, default=1)

    worker = subparsers.add_parser("worker", help="Update current worker state")
    add_repo_argument(worker)
    worker.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    worker_subparsers = worker.add_subparsers(dest="worker_command", required=True)
    claim_workspace = worker_subparsers.add_parser(
        "claim-workspace",
        help="Attach branch/worktree metadata to an active task lock",
    )
    add_repo_argument(claim_workspace)
    claim_workspace.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS
    )
    claim_workspace.add_argument("--run-id", default="")
    claim_workspace.add_argument("--task-id", default="")
    claim_workspace.add_argument("--branch", required=True)
    claim_workspace.add_argument("--worktree", type=Path, required=True)
    claim_workspace.add_argument("--base-commit", default="")

    workers = subparsers.add_parser("workers", help="List active worker runs")
    add_repo_argument(workers)
    workers.add_argument("--json", action="store_true")
    workers_subparsers = workers.add_subparsers(dest="workers_command")
    workers_clean = workers_subparsers.add_parser(
        "clean", help="Remove stale task and integration locks"
    )
    add_repo_argument(workers_clean)
    workers_clean.add_argument("--json", action="store_true")
    workers_clean.add_argument(
        "--force",
        action="store_true",
        help="Actually remove stale locks (default is dry-run)",
    )

    runs = subparsers.add_parser("runs", help="Inspect recorded run results")
    add_repo_argument(runs)
    runs_subparsers = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_subparsers.add_parser("list", help="List recent run results")
    add_repo_argument(runs_list)
    runs_list.add_argument("--json", action="store_true")
    runs_list.add_argument("--limit", type=int, default=20)
    runs_inspect = runs_subparsers.add_parser("inspect", help="Show one run result")
    add_repo_argument(runs_inspect)
    runs_inspect.add_argument("run_id")
    runs_inspect.add_argument("--json", action="store_true")

    planning = subparsers.add_parser("planning", help="Generate planning analytics")
    add_repo_argument(planning)
    planning_subparsers = planning.add_subparsers(
        dest="planning_command",
        required=True,
    )
    planning_timeline = planning_subparsers.add_parser(
        "timeline",
        help="Generate planning timeline JSON",
    )
    add_repo_argument(planning_timeline)
    planning_timeline.add_argument("--json", action="store_true")
    planning_timeline.add_argument(
        "--git-commit-limit",
        type=int,
        default=DEFAULT_GIT_COMMIT_LIMIT,
    )
    planning_artifacts = planning_subparsers.add_parser(
        "artifacts",
        help="Generate timeline JSON and static Gantt artifacts",
    )
    add_repo_argument(planning_artifacts)
    planning_artifacts_mode = planning_artifacts.add_mutually_exclusive_group()
    planning_artifacts_mode.add_argument("--check", action="store_true")
    planning_artifacts_mode.add_argument("--inspect", action="store_true")
    planning_artifacts.add_argument("--json", action="store_true")
    planning_artifacts.add_argument("--output", type=Path)
    planning_artifacts.add_argument("--html-output", type=Path)
    planning_artifacts.add_argument(
        "--git-commit-limit",
        type=int,
        default=DEFAULT_GIT_COMMIT_LIMIT,
    )
    planning_benchmark_duration = planning_subparsers.add_parser(
        "benchmark-duration",
        help="Benchmark projected duration estimators",
    )
    add_repo_argument(planning_benchmark_duration)
    planning_benchmark_duration.add_argument("--check", action="store_true")
    planning_benchmark_duration.add_argument("--json", action="store_true")
    planning_benchmark_duration.add_argument(
        "--git-commit-limit",
        type=int,
        default=DEFAULT_GIT_COMMIT_LIMIT,
    )

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

    eval_parser = subparsers.add_parser("eval", help="Run local skill evaluations")
    add_repo_argument(eval_parser)
    eval_subparsers = eval_parser.add_subparsers(
        dest="eval_command",
        required=True,
    )
    local_demo = eval_subparsers.add_parser(
        "local-demo",
        help="Run the bundled local demo skill eval suite",
    )
    add_repo_argument(local_demo)
    local_demo.add_argument("--output", type=Path)
    add_local_demo_eval_arguments(local_demo, default_trials=1)
    local_demo.add_argument("--json", action="store_true")

    release_gate = eval_subparsers.add_parser(
        "release-gate",
        help="Run or check release readiness for bundled skill changes",
    )
    add_repo_argument(release_gate)
    release_gate.add_argument(
        "--aggregate",
        type=Path,
        help="Existing local-demo aggregate.json to check instead of running evals",
    )
    release_gate.add_argument(
        "--eval-output",
        type=Path,
        help="Output root for a local demo suite run",
    )
    release_gate.add_argument(
        "--record-output",
        type=Path,
        help="Write the release-readiness JSON record to this path",
    )
    release_gate.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not run evals; check an existing aggregate and mark the record dry-run",
    )
    release_gate.add_argument(
        "--minimum-trials",
        type=int,
        default=3,
        help="Required trials per local-demo case and condition",
    )
    release_gate.add_argument(
        "--parked-regression",
        action="append",
        default=[],
        help="Park one regression with REGRESSION_ID=TASK_ID",
    )
    release_gate.add_argument(
        "--parked-workflow-regression",
        action="append",
        default=[],
        help="Park every current workflow-contract regression under TASK_ID",
    )
    release_gate.add_argument(
        "--external-benchmark-json",
        type=Path,
        action="append",
        default=[],
        help="Optional external benchmark smoke summary JSON",
    )
    add_local_demo_eval_arguments(release_gate, default_trials=3)
    release_gate.add_argument("--json", action="store_true")

    benchmark = eval_subparsers.add_parser(
        "benchmark",
        help="Run external benchmark adapter eval",
    )
    add_repo_argument(benchmark)
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument(
        "--adapter",
        required=True,
        help="Adapter name (registered adapters: stub)",
    )
    benchmark.add_argument(
        "--agent-command",
        action="append",
        default=[],
        help="CONDITION=COMMAND pairs",
    )
    benchmark.add_argument("--instance", action="append", default=[])
    benchmark.add_argument("--condition", action="append", default=[])
    benchmark.add_argument("--trials", type=int, default=1)
    benchmark.add_argument("--timeout", type=int, default=600)

    doctor = subparsers.add_parser("doctor", help="Print resolved configuration")
    add_repo_argument(doctor)
    doctor.add_argument("--json", action="store_true")

    install = subparsers.add_parser("install-skills", help="Install bundled skills")
    add_repo_argument(install)
    install.add_argument("--codex", action="store_true")
    install.add_argument("--claude", action="store_true")
    install.add_argument("--home", type=Path, default=Path.home())
    return parser


def add_local_demo_eval_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_trials: int,
) -> None:
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--condition", action="append", default=[])
    parser.add_argument("--trials", type=int, default=default_trials)
    parser.add_argument(
        "--agent-command",
        action="append",
        default=[],
        help=(
            "Agent command template. Use CONDITION=COMMAND for per-condition "
            "commands or *=COMMAND/default COMMAND for all conditions."
        ),
    )
    parser.add_argument("--transcript-grader", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--max-commands", type=int)
    parser.add_argument("--max-output-bytes", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--agent-name", default="configured-agent")
    parser.add_argument("--model-provider", default="unknown")
    parser.add_argument("--model-id", default="unknown")
    parser.add_argument("--reasoning-effort", default="")


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
            max_tasks=args.max_tasks,
        )
        print(json.dumps([result.to_json() for result in results], indent=2))
        return run_until_done_exit_code(results)

    if args.command == "worker":
        return dispatch_worker(args, config)

    if args.command == "workers":
        if getattr(args, "workers_command", None) == "clean":
            return dispatch_workers_clean(args, config)
        runner = VibeRunner(config)
        workers = build_worker_views(
            runner.lock_manager,
            runner.run_store,
            repo=config.repo,
            main_branch=config.main_branch,
        )
        if args.json:
            payloads = []
            for worker in workers:
                payload = worker.to_json()
                ref = _timeline_ref_for_task(config, worker.active.task_id)
                if ref is not None:
                    payload["timeline_task"] = ref
                payloads.append(payload)
            print(json.dumps(payloads, indent=2))
        else:
            output = render_workers(workers)
            if output:
                print(output)
            stale = [w for w in workers if w.state == "stale"]
            if stale:
                print(
                    f"\n{len(stale)} stale lock(s) found."
                    " Run 'vibe-loop workers clean' to review,"
                    " 'vibe-loop workers clean --force' to remove."
                )
        return 0

    if args.command == "runs":
        return dispatch_runs(args, config)

    if args.command == "planning":
        return dispatch_planning(args, config)

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

    if args.command == "eval":
        return dispatch_eval(args, config)

    if args.command == "doctor":
        task_source_runtime = runtime_task_source_report(config)
        analytics_report = planning_analytics_report(
            config,
            task_source_runtime=task_source_runtime,
        )
        analytics_report["artifacts"] = inspect_planning_artifacts(config)
        runner = VibeRunner(config)
        workers = build_worker_views(
            runner.lock_manager,
            runner.run_store,
            repo=config.repo,
            main_branch=config.main_branch,
        )
        stale = collect_stale_locks(
            runner.lock_manager,
            runner.run_store,
            repo=config.repo,
            main_branch=config.main_branch,
        )
        stale_report = {
            "count": len(stale),
            "locks": [s.to_json() for s in stale],
        }
        if stale:
            stale_report["next_command"] = "vibe-loop workers clean --force"
        print(
            json.dumps(
                {
                    "repo": str(config.repo),
                    "main_branch": config.main_branch,
                    "state_dir": config.state_dir,
                    "task_source": config.task_source.to_json(),
                    "task_source_runtime": task_source_runtime,
                    "generated_task_profile": generated_task_cache_report(config),
                    "planning_analytics": analytics_report,
                    "agent": config.agent.to_json(),
                    "completion": config.completion.__dict__,
                    "stale_locks": stale_report,
                    "workspace_diagnostics": workspace_diagnostics_report(workers),
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


def dispatch_runs(args: argparse.Namespace, config) -> int:
    run_store = RunStore(config.state_path / "runs.jsonl")
    if args.runs_command == "list":
        if args.limit < 0:
            print("runs list --limit must be non-negative", file=sys.stderr)
            return 2
        runs = run_store.list_runs(limit=args.limit)
        if args.json:
            print(json.dumps([run.to_json() for run in runs], indent=2))
        else:
            output = render_runs(runs)
            if output:
                print(output)
        return 0

    if args.runs_command == "inspect":
        inspection = run_store.inspect_run(args.run_id)
        if inspection is None:
            print(f"run not found: {args.run_id}", file=sys.stderr)
            return 2
        payload = inspection.to_json()
        timeline_ref = _timeline_ref_for_task(config, str(payload.get("task_id") or ""))
        if timeline_ref is not None:
            payload["timeline_task"] = timeline_ref
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_run_inspection(inspection))
            if timeline_ref:
                print(
                    f"timeline: status={timeline_ref.get('status', '-')}"
                    f" actual={'yes' if timeline_ref.get('has_actual') else 'no'}"
                    f" projected={'yes' if timeline_ref.get('has_projected') else 'no'}"
                )
        return 0

    raise AssertionError(args.runs_command)


def dispatch_planning(args: argparse.Namespace, config) -> int:
    if args.planning_command == "timeline":
        timeline = build_planning_timeline(
            config,
            git_commit_limit=args.git_commit_limit,
        )
        print(json.dumps(timeline, indent=2))
        return 0

    if args.planning_command == "artifacts":
        return dispatch_planning_artifacts(args, config)

    if args.planning_command == "benchmark-duration":
        report = build_duration_benchmark(
            config,
            git_commit_limit=args.git_commit_limit,
        )
        if args.check:
            errors = check_duration_benchmark_reports(config, report)
            if args.json:
                print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
            elif errors:
                for error in errors:
                    print(error, file=sys.stderr)
            else:
                print("duration benchmark reports are up to date")
            return 0 if not errors else 1
        json_path, markdown_path = write_duration_benchmark_reports(config, report)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"duration benchmark JSON: {json_path}")
            print(f"duration benchmark Markdown: {markdown_path}")
        return 0

    raise AssertionError(args.planning_command)


def dispatch_planning_artifacts(args: argparse.Namespace, config) -> int:
    if args.inspect:
        report = inspect_planning_artifacts(
            config,
            output=args.output,
            html_output=args.html_output,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(render_planning_artifact_inspection(report))
        return 0

    bundle = build_planning_artifact_bundle(
        config,
        output=args.output,
        html_output=args.html_output,
        git_commit_limit=args.git_commit_limit,
    )
    if args.check:
        errors = check_planning_artifacts(bundle)
        report = {
            "ok": not errors,
            "errors": errors,
            "paths": bundle.paths.to_json(),
            "warning_count": bundle.warning_count,
        }
        if args.json:
            print(json.dumps(report, indent=2))
        elif errors:
            for error in errors:
                print(error, file=sys.stderr)
        else:
            print("planning artifacts are up to date")
        return 0 if not errors else 1

    write_planning_artifacts(bundle)
    report = {
        "action": "generated",
        "paths": bundle.paths.to_json(),
        "warning_count": bundle.warning_count,
        "artifacts": inspect_planning_artifacts(
            config,
            output=args.output,
            html_output=args.html_output,
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"timeline JSON: {bundle.paths.timeline_json}")
        print(f"gantt HTML: {bundle.paths.gantt_html}")
        print(f"warnings: {bundle.warning_count}")
    return 0


def dispatch_eval(args: argparse.Namespace, config) -> int:
    if args.eval_command == "local-demo":
        output_root = args.output or (config.state_path / "eval-runs")
        aggregate = run_local_demo_eval(
            local_demo_config_from_args(args, config, output_root=output_root)
        )
        if args.json:
            print(json.dumps(aggregate, indent=2, sort_keys=True))
        else:
            print(f"aggregate: {output_root / 'local-demo-v1' / 'aggregate.json'}")
            for condition, payload in aggregate.get("conditions", {}).items():
                if isinstance(payload, dict):
                    print(
                        f"{condition}: pass_rate={payload.get('pass_rate')} "
                        f"trials={payload.get('trials')}"
                    )
        return 0

    if args.eval_command == "release-gate":
        output_root = args.eval_output or (config.state_path / "eval-runs")
        aggregate_path = args.aggregate or (
            output_root / "local-demo-v1" / "aggregate.json"
        )
        local_suite_mode = "existing_aggregate"
        if not args.dry_run and args.aggregate is None:
            aggregate = run_local_demo_eval(
                local_demo_config_from_args(args, config, output_root=output_root)
            )
            aggregate_path = output_root / "local-demo-v1" / "aggregate.json"
            local_suite_mode = "executed"
        else:
            aggregate = load_json_mapping(aggregate_path)
        record = build_release_readiness_record(
            aggregate,
            aggregate_path=aggregate_path,
            dry_run=args.dry_run,
            minimum_trials=args.minimum_trials,
            local_suite_mode=local_suite_mode,
            parked_regressions=parse_parked_regression_specs(args.parked_regression),
            parked_workflow_regression_task_ids=tuple(args.parked_workflow_regression),
            external_benchmarks=load_external_benchmark_evidence(
                args.external_benchmark_json
            ),
        )
        if args.record_output:
            write_release_readiness_record(args.record_output, record)
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print(render_release_readiness_summary(record), end="")
            if args.record_output:
                print(f"record: {args.record_output}")
        return 0 if record.get("status") == "passed" else 1

    if args.eval_command == "benchmark":
        from vibe_loop.eval_benchmark import BenchmarkEvalConfig, run_benchmark_eval

        adapter = resolve_benchmark_adapter(args.adapter)
        if adapter is None:
            print(f"unknown benchmark adapter: {args.adapter}", file=sys.stderr)
            return 2
        agent_commands = _parse_benchmark_agent_commands(args.agent_command)
        if not agent_commands:
            print(
                "at least one --agent-command CONDITION=COMMAND is required",
                file=sys.stderr,
            )
            return 2
        bench_config = BenchmarkEvalConfig(
            adapter=adapter,
            output_root=args.output,
            agent_commands=agent_commands,
            instances=tuple(args.instance),
            conditions=tuple(args.condition),
            trials=args.trials,
            timeout_seconds=args.timeout,
        )
        payload = run_benchmark_eval(bench_config)
        output_path = args.output / f"{adapter.name}-results.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return 0

    raise AssertionError(args.eval_command)


def _parse_benchmark_agent_commands(
    specs: list[str],
) -> dict[str, str]:
    commands: dict[str, str] = {}
    for spec in specs:
        key, separator, value = spec.partition("=")
        if not separator or not key or not value:
            continue
        commands[key] = value
    return commands


def resolve_benchmark_adapter(name: str) -> object | None:
    adapters: dict[str, type] = {}
    try:
        from vibe_loop.eval_benchmark_stub import StubBenchmarkAdapter

        adapters["stub"] = StubBenchmarkAdapter
    except ImportError:
        pass
    cls = adapters.get(name)
    if cls is None:
        return None
    return cls()


def local_demo_config_from_args(
    args: argparse.Namespace,
    config,
    *,
    output_root: Path,
) -> LocalSkillEvalConfig:
    agent_commands, default_agent_command = parse_agent_command_specs(
        args.agent_command
    )
    if not agent_commands and default_agent_command is None:
        default_agent_command = config.agent.require_selection_command()
    return LocalSkillEvalConfig(
        output_root=output_root,
        agent_commands=agent_commands,
        default_agent_command=default_agent_command,
        cases=tuple(args.case),
        conditions=tuple(args.condition),
        trials=args.trials,
        transcript_graders=tuple(args.transcript_grader),
        timeout_seconds=args.timeout_seconds,
        max_commands=args.max_commands,
        max_output_bytes=args.max_output_bytes,
        overwrite=args.overwrite,
        agent_name=args.agent_name,
        model_provider=args.model_provider,
        model_id=args.model_id,
        reasoning_effort=args.reasoning_effort,
    )


def dispatch_worker(args: argparse.Namespace, config) -> int:
    if args.worker_command == "claim-workspace":
        run_id, task_id = worker_identity_from_args(args)
        if not run_id or not task_id:
            print(
                "worker claim-workspace requires --run-id and --task-id "
                "or VIBE_LOOP_RUN_ID and VIBE_LOOP_TASK_ID",
                file=sys.stderr,
            )
            return 2
        manager = LockManager(config.state_path / "locks")
        run_store = RunStore(config.state_path / "runs.jsonl")
        try:
            claim = claim_worker_workspace(
                manager,
                run_store,
                task_id=task_id,
                run_id=run_id,
                branch=args.branch,
                worktree=args.worktree,
                repo=config.repo,
                base_commit=args.base_commit,
            )
        except WorkspaceClaimError as exc:
            payload = {
                "claimed": False,
                "error": exc.code,
                "message": str(exc),
                "details": exc.details,
            }
            if json_requested(args):
                print(json.dumps(payload, indent=2))
            else:
                print(
                    f"worker claim-workspace refused: {exc.code}: {exc}",
                    file=sys.stderr,
                )
            return 1
        payload = {"claimed": True, "workspace": claim.to_json()}
        if json_requested(args):
            print(json.dumps(payload, indent=2))
        else:
            print(
                "worker workspace claimed "
                f"task={task_id} run={run_id} branch={claim.branch} "
                f"worktree={claim.worktree}"
            )
        return 0

    raise AssertionError(args.worker_command)


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
        result = configure_generated_task_source(
            config,
            dry_run=args.dry_run,
            force_refresh=args.force_refresh,
            write_cache=not args.dry_run and not args.promotion_toml,
        )
        payload = result.to_json()
        payload["agent"] = config.agent.to_json()
        if args.promotion_toml:
            if result.promotion_toml is None:
                diagnostics = (
                    result.promotion_diagnostics
                    or result.diagnostics
                    or ("no valid generated profile is available for promotion",)
                )
                for diagnostic in diagnostics:
                    print(diagnostic, file=sys.stderr)
                return 2
            print(result.promotion_toml, end="")
            return result.exit_code
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            label = "candidate" if result.dry_run else "cache"
            print(f"tasks configure: {label} status={payload['status']}")
            print(f"cache: {result.cache_path}")
            print(f"cache action: {result.cache_action}")
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
        result = (
            f"\tresult={payload['result_status']}" if payload["result_status"] else ""
        )
        workspace = ""
        if isinstance(payload["workspace"], dict):
            dirty = "dirty" if payload["workspace"].get("dirty") else "clean"
            workspace = (
                f"\tworkspace={payload['workspace'].get('branch')}"
                f"@{payload['workspace'].get('worktree')}:{dirty}"
            )
        diagnostics = ""
        if payload["workspace_diagnostics"]:
            diagnostics = (
                f"\tworkspace_diagnostics={len(payload['workspace_diagnostics'])}"
            )
        lines.append(
            f"{payload['task_id']}\t{payload['run_id']}\t{payload['state']}"
            f"\tprocess={payload['process_state']}\tpid={pid}"
            f"\tstarted={payload['started_at']}\tlog={payload['log']}"
            f"\tcommand={payload['command']}{workspace}{result}{diagnostics}{stale}"
        )
    return "\n".join(lines)


def workspace_diagnostics_report(workers: list[WorkerView]) -> dict[str, object]:
    diagnostics: list[dict[str, object]] = []
    for worker in workers:
        workspace = worker.active.workspace
        for diagnostic in worker.workspace_diagnostics:
            payload = diagnostic.to_json()
            payload.update(
                {
                    "task_id": worker.active.task_id,
                    "run_id": worker.active.run_id,
                    "branch": workspace.branch if workspace else "",
                    "worktree": str(workspace.worktree) if workspace else "",
                }
            )
            diagnostics.append(payload)
    return {"count": len(diagnostics), "diagnostics": diagnostics}


def render_stale_locks(stale_locks: list[StaleLock]) -> str:
    lines: list[str] = []
    for lock in stale_locks:
        lines.append(
            f"{lock.task_id}\t{lock.kind}\treason={lock.stale_reason}"
            f"\trun_id={lock.run_id}\tpath={lock.lock_path}"
        )
        lines.append(f"  recovery: {lock.recovery_command}")
    return "\n".join(lines)


def dispatch_workers_clean(args: argparse.Namespace, config) -> int:
    runner = VibeRunner(config)
    stale = collect_stale_locks(
        runner.lock_manager,
        runner.run_store,
        repo=config.repo,
        main_branch=config.main_branch,
    )
    if not stale:
        if args.json:
            print(
                json.dumps({"stale_locks": [], "cleaned": [], "errors": []}, indent=2)
            )
        else:
            print("No stale locks found.")
        return 0
    if args.force:
        result = clean_stale_locks(stale)
        if args.json:
            print(
                json.dumps(
                    {
                        "stale_locks": [s.to_json() for s in stale],
                        "cleaned": [s.to_json() for s in result.cleaned],
                        "errors": [
                            {"lock": s.to_json(), "error": msg}
                            for s, msg in result.errors
                        ],
                    },
                    indent=2,
                )
            )
        else:
            if result.cleaned:
                print(f"Removed {len(result.cleaned)} stale lock(s):")
                print(render_stale_locks(result.cleaned))
            for lock, msg in result.errors:
                print(f"error: {lock.task_id}: {msg}", file=sys.stderr)
            if not result.cleaned and result.errors:
                print("No locks were removed due to errors.", file=sys.stderr)
        return 1 if result.errors and not result.cleaned else 0
    if args.json:
        print(
            json.dumps(
                {
                    "stale_locks": [s.to_json() for s in stale],
                    "cleaned": [],
                    "errors": [],
                },
                indent=2,
            )
        )
    else:
        print(f"{len(stale)} stale lock(s) found (dry-run, use --force to remove):")
        print(render_stale_locks(stale))
    return 0


def render_runs(runs) -> str:
    lines: list[str] = []
    for run in runs:
        payload = run.to_json()
        exit_code = payload["exit_code"] if payload["exit_code"] is not None else "-"
        lines.append(
            f"{payload['run_id']}\t{payload['task_id']}\t{payload['status']}"
            f"\trecord={payload['record_type']}\tupdated={payload['updated_at']}"
            f"\texit={exit_code}\tlog={payload['log']}"
        )
    return "\n".join(lines)


def render_run_inspection(inspection) -> str:
    payload = inspection.to_json()
    exit_code = payload["exit_code"] if payload["exit_code"] is not None else "-"
    lines = [
        f"run: {payload['run_id']}",
        f"task: {payload['task_id'] or '-'}",
        f"status: {payload['status'] or '-'}",
        f"record: {payload['record_type']}",
        f"updated: {payload['updated_at'] or '-'}",
        f"exit: {exit_code}",
        f"session: {payload['session_id']} ({payload['session_id_source'] or '-'})",
        f"log: {payload['log'] or '-'}",
        f"message: {payload['message'] or '-'}",
        f"records: {payload['record_count']}",
    ]
    if payload["worker_report"]:
        lines.append(
            "worker_report: " + json.dumps(payload["worker_report"], sort_keys=True)
        )
    lines.append("record_history:")
    for record in payload["records"]:
        record_type = record.get("record_type") or "run_result"
        status = record.get("status") or record.get("classification") or "-"
        updated = record.get("finished_at") or record.get("reported_at") or "-"
        lines.append(f"- {record_type}\tstatus={status}\tupdated={updated}")
    return "\n".join(lines)


def render_planning_artifact_inspection(report: dict[str, object]) -> str:
    lines: list[str] = []
    for key, label in (
        ("timeline_json", "timeline JSON"),
        ("gantt_html", "gantt HTML"),
    ):
        artifact = report.get(key, {})
        if not isinstance(artifact, dict):
            continue
        warning_count = artifact.get("warning_count")
        warning_text = "-" if warning_count is None else str(warning_count)
        schema_status = artifact.get("schema_status") or "-"
        lines.append(
            f"{label}: {artifact.get('path')}"
            f"\tsource={artifact.get('source')}"
            f"\tfreshness={artifact.get('freshness')}"
            f"\tschema={schema_status}"
            f"\twarnings={warning_text}"
        )
        error = artifact.get("error")
        if error:
            lines.append(f"{label} error: {error}")
    timeline = report.get("timeline_json", {})
    if isinstance(timeline, dict):
        warnings = timeline.get("warnings", [])
        if isinstance(warnings, list) and warnings:
            lines.append("warnings:")
            for warning in warnings:
                if not isinstance(warning, dict):
                    continue
                task_id = warning.get("task_id")
                task_text = f" task={task_id}" if task_id else ""
                lines.append(
                    f"- {warning.get('code')}{task_text}: {warning.get('message')}"
                )
    commands = report.get("next_repair_commands", [])
    if isinstance(commands, list) and commands:
        lines.append("next repair commands:")
        for command in commands:
            lines.append(f"- {command}")
    return "\n".join(lines)


def _timeline_ref_for_task(config, target_task_id: str) -> dict[str, object] | None:
    if not target_task_id:
        return None
    try:
        paths = planning_artifact_paths(config)
    except (ValueError, OSError):
        return None
    timeline = read_timeline_file(paths.timeline_json)
    if timeline is None:
        return None
    return lookup_timeline_task(timeline, target_task_id)


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
