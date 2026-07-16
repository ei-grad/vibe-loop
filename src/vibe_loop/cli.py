from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distribution as metadata_distribution
from importlib.metadata import version as metadata_version
from pathlib import Path

from vibe_loop.autopilot import (
    DEFAULT_WAIT_CYCLE_SECONDS,
    DEFAULT_WAIT_POLL_SECONDS,
    ProjectEntry,
    ProjectRegistry,
    ProjectStatus,
    collect_project_status,
    collect_registry_status,
    cycle_schedule_deadline,
    default_registry_path,
    parse_wait_deadline,
    run_autopilot,
    wait_for_processes,
)
from vibe_loop.config import (
    AGENT_DEFAULT_POLICY,
    AGENT_DEFAULT_POLICY_SOURCE,
    AgentResolutionError,
    load_config,
)
from vibe_loop.eval_runner import (
    LocalSkillEvalConfig,
    parse_agent_command_specs,
    run_local_demo_eval,
)
from vibe_loop.eval_release import (
    DEFAULT_RELEASE_GATE_TRIALS,
    build_release_readiness_record,
    load_external_benchmark_evidence,
    load_json_mapping,
    parse_parked_regression_specs,
    render_release_readiness_summary,
    release_gate_case_conditions,
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
from vibe_loop.locks import (
    LockBackendError,
    LockBusy,
    LockFencingMismatch,
    LockManager,
    LockOwnerMismatch,
    build_lock_manager,
)
from vibe_loop.locks import integration_lock_waitable
from vibe_loop.runner import VibeRunner
from vibe_loop.runs import (
    LOCK_ACQUIRED_RECORD_TYPE,
    LOCK_RELEASED_RECORD_TYPE,
    RunLifecycleEvent,
    RunResult,
    RunStore,
    TASK_RECOVERY_RECORD_TYPE,
    TASK_RESTART_RECORD_TYPE,
    WorkerReport,
    WORKER_REPORT_STATUSES,
    utc_now_iso,
)
from vibe_loop.skills import install_skills
from vibe_loop.spec_diagnostics import (
    build_spec_diagnostics_report,
    render_spec_diagnostics,
)
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
    record_expired_locks,
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

    autopilot = subparsers.add_parser(
        "autopilot",
        help="Persistent supervision above run-until-done",
    )
    add_repo_argument(autopilot)
    autopilot_subparsers = autopilot.add_subparsers(
        dest="autopilot_command",
        required=False,
    )
    autopilot_status = autopilot_subparsers.add_parser(
        "status",
        help="Show structured autopilot project status without launching a worker",
    )
    add_repo_argument(autopilot_status)
    autopilot_status.add_argument("--json", action="store_true")
    autopilot_run = autopilot_subparsers.add_parser(
        "run",
        help="Supervise run-until-done as a foreground child process",
    )
    add_repo_argument(autopilot_run)
    add_autopilot_run_arguments(autopilot_run)
    autopilot_projects = autopilot_subparsers.add_parser(
        "projects",
        help="Manage the optional multi-project autopilot registry",
    )
    projects_subparsers = autopilot_projects.add_subparsers(
        dest="autopilot_projects_command",
        required=True,
    )
    projects_register = projects_subparsers.add_parser(
        "register",
        help="Register a repository in the autopilot registry",
    )
    add_repo_argument(projects_register)
    projects_register.add_argument("--name", default="")
    add_registry_argument(projects_register)
    projects_register.add_argument("--json", action="store_true")
    projects_list = projects_subparsers.add_parser(
        "list",
        help="List registered repositories",
    )
    add_registry_argument(projects_list)
    projects_list.add_argument("--json", action="store_true")
    projects_inspect = projects_subparsers.add_parser(
        "inspect",
        help="Show status for one registered repository by name or path",
    )
    projects_inspect.add_argument("project")
    add_registry_argument(projects_inspect)
    projects_inspect.add_argument("--json", action="store_true")
    projects_remove = projects_subparsers.add_parser(
        "remove",
        help="Remove a repository from the registry by name or path",
    )
    projects_remove.add_argument("project")
    add_registry_argument(projects_remove)
    projects_remove.add_argument("--json", action="store_true")
    projects_status = projects_subparsers.add_parser(
        "status",
        help="Show aggregate status across all registered repositories",
    )
    add_registry_argument(projects_status)
    projects_status.add_argument("--json", action="store_true")
    wait_helper = subparsers.add_parser(
        "wait-helper",
        help="Block until a watched process exits or the next cycle boundary",
    )
    wait_helper.add_argument(
        "--pid",
        action="append",
        type=int,
        default=[],
        metavar="PID",
        help="Wake when this process exits (repeatable)",
    )
    wait_helper_when = wait_helper.add_mutually_exclusive_group()
    wait_helper_when.add_argument(
        "--deadline",
        default=None,
        help="Wake at this ISO-8601 UTC time, e.g. 2026-06-06T17:00:00Z",
    )
    wait_helper_when.add_argument(
        "--cycle-schedule",
        dest="cycle_schedule",
        nargs="?",
        const=DEFAULT_WAIT_CYCLE_SECONDS,
        type=positive_float,
        default=None,
        metavar="SECONDS",
        help=(
            "Wake at the next UTC */SECONDS wall-clock boundary "
            f"(default interval {int(DEFAULT_WAIT_CYCLE_SECONDS)}s when no "
            "deadline is given)"
        ),
    )
    wait_helper.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_WAIT_POLL_SECONDS,
        help="Process poll interval in seconds",
    )
    wait_helper.add_argument("--mode", choices=("any", "all"), default="any")
    wait_helper.add_argument("--json", action="store_true")

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
    claim_workspace.add_argument("--fencing-token", default="")
    heartbeat = worker_subparsers.add_parser(
        "heartbeat",
        help="Refresh the heartbeat timestamp on an active task lock",
    )
    add_repo_argument(heartbeat)
    heartbeat.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    heartbeat.add_argument("--run-id", default="")
    heartbeat.add_argument("--task-id", default="")
    heartbeat.add_argument("--fencing-token", default="")

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
    integration_acquire.add_argument("--wait", action="store_true")
    integration_acquire.add_argument("--timeout", type=nonnegative_float)
    integration_acquire.add_argument(
        "--poll-interval",
        type=positive_float,
        default=1.0,
    )
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
    integration_release.add_argument("--fencing-token", default="")

    report = subparsers.add_parser("report", help="Record a worker result report")
    add_repo_argument(report)
    report.add_argument("--run-id", required=True)
    report.add_argument("--task-id", required=True)
    report.add_argument("--status", required=True, choices=WORKER_REPORT_STATUSES)
    report.add_argument("--commit", default="")
    report.add_argument("--message", default="")
    report.add_argument("--fencing-token", default="")
    report.add_argument(
        "--metadata-json",
        help="JSON object with additional structured report metadata",
    )

    specs = subparsers.add_parser("specs", help="Inspect spec traceability checks")
    add_repo_argument(specs)
    specs_subparsers = specs.add_subparsers(dest="specs_command", required=True)
    specs_check = specs_subparsers.add_parser(
        "check",
        help="Run read-only spec coverage and drift diagnostics",
    )
    add_repo_argument(specs_check)
    specs_check.add_argument("--json", action="store_true")

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
    add_nested_eval_override(local_demo)

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
        default=DEFAULT_RELEASE_GATE_TRIALS,
        help="Required trials per release-gate case and condition",
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
    add_local_demo_eval_arguments(
        release_gate, default_trials=DEFAULT_RELEASE_GATE_TRIALS
    )
    release_gate.add_argument("--json", action="store_true")
    add_nested_eval_override(release_gate)

    benchmark = eval_subparsers.add_parser(
        "benchmark",
        help="Run external benchmark adapter eval",
    )
    add_repo_argument(benchmark)
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument(
        "--adapter",
        required=True,
        help="Adapter name (registered adapters: manifest, stub)",
    )
    benchmark.add_argument(
        "--manifest",
        type=Path,
        help="JSON manifest for the manifest benchmark adapter",
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
    add_nested_eval_override(benchmark)

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


def add_nested_eval_override(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-nested",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def add_autopilot_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help=(
            "Worker concurrency passed to the supervised run-until-done child "
            "(overrides [autopilot] jobs; default 1)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=nonnegative_float,
        default=None,
        help=(
            "Seconds to sleep between supervision cycles in the persistent loop "
            "(overrides [autopilot] interval_seconds; default 0)"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single supervision cycle and exit",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop after this many cycles (0 means unbounded)",
    )
    parser.add_argument("--ask-agent", action="store_true")
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--max-slices", type=int, default=0)
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument(
        "--min-ready",
        type=int,
        default=None,
        help=(
            "Minimum runnable tasks required before launching a child "
            "(overrides [autopilot] min_ready; default 1)"
        ),
    )


def add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=argparse.SUPPRESS)


def add_registry_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to the project registry JSON (default: ~/.vibe-loop/projects.json)",
    )


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
            ignored_dirty_paths=(config.state_path,),
        )
        if args.json:
            payloads = []
            for worker in workers:
                payload = worker.to_json()
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

    if args.command == "main-integration":
        return dispatch_main_integration(args, config)

    if args.command == "report":
        report_error = validate_report_fencing(args, config)
        if report_error is not None:
            return report_error
        report = WorkerReport(
            run_id=args.run_id,
            task_id=args.task_id,
            status=args.status,
            commit=resolve_report_commit(config.repo, args.commit),
            message=args.message,
            metadata=parse_metadata_json(args.metadata_json),
        )
        RunStore(config.state_path / "runs.jsonl").append_report(report)
        print(json.dumps(report.to_json(), indent=2))
        return 0

    if args.command == "specs":
        return dispatch_specs(args, config)

    if args.command == "eval":
        return dispatch_eval(args, config)

    if args.command == "autopilot":
        return dispatch_autopilot(args, config)

    if args.command == "wait-helper":
        return dispatch_wait_helper(args)

    if args.command == "doctor":
        task_source_runtime = runtime_task_source_report(config)
        runner = VibeRunner(config)
        workers = build_worker_views(
            runner.lock_manager,
            runner.run_store,
            repo=config.repo,
            main_branch=config.main_branch,
            ignored_dirty_paths=(config.state_path,),
        )
        stale = collect_stale_locks(
            runner.lock_manager,
            runner.run_store,
            repo=config.repo,
            main_branch=config.main_branch,
            ignored_dirty_paths=(config.state_path,),
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
                    "config": config.config_report(),
                    "main_branch": config.main_branch,
                    "state_dir": config.state_dir,
                    "task_source": redacted_task_source_config(config.task_source),
                    "task_source_runtime": redacted_task_source_report(
                        task_source_runtime,
                    ),
                    "generated_task_profile": generated_task_cache_report(config),
                    "specs": build_spec_diagnostics_report(
                        config,
                        task_source_runtime=task_source_runtime,
                    ),
                    "agent": config.agent.to_json(),
                    "locks": redacted_lock_config(config.locks),
                    "autopilot": redacted_autopilot_config(config.autopilot),
                    "completion": redacted_completion_config(config.completion),
                    "stale_locks": stale_report,
                    "concurrency_diagnostics": concurrency_diagnostics_report(workers),
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
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_run_inspection(inspection))
        return 0

    raise AssertionError(args.runs_command)


def redacted_task_source_report(report: dict[str, object]) -> dict[str, object]:
    payload = dict(report)
    task_source = payload.get("task_source")
    if isinstance(task_source, dict):
        payload["task_source"] = redact_task_source_payload(task_source)
    return payload


def redacted_task_source_config(task_source) -> dict[str, object]:
    return redact_task_source_payload(task_source.to_json())


def redact_task_source_payload(payload: dict[str, object]) -> dict[str, object]:
    redacted = dict(payload)
    for key in ("list_command", "next_command", "probe_command", "reset_command"):
        configured = bool(redacted.pop(key, None))
        redacted[f"{key}_configured"] = configured
        redacted[f"{key}_redacted"] = configured
    return redacted


def redacted_lock_config(locks) -> dict[str, object]:
    payload = dict(locks.to_json())
    for key in (
        "acquire_command",
        "release_command",
        "status_command",
        "list_command",
    ):
        configured = bool(payload.pop(key, None))
        payload[f"{key}_configured"] = configured
        payload[f"{key}_redacted"] = configured
    return payload


def redacted_completion_config(completion) -> dict[str, object]:
    count = len(completion.commands)
    return {
        "commands_configured": count,
        "commands_redacted": count > 0,
    }


def redacted_autopilot_config(autopilot) -> dict[str, object]:
    payload = dict(autopilot.to_json())
    for key in (
        "health_command",
        "summary_command",
        "troubleshoot_command",
        "planning_command",
    ):
        configured = bool(payload.pop(key, None))
        payload[f"{key}_configured"] = configured
        payload[f"{key}_redacted"] = configured
    return payload


def dispatch_eval(args: argparse.Namespace, config) -> int:
    if os.environ.get("VIBE_LOOP_EVAL_ACTIVE") == "1" and not args.allow_nested:
        print(
            "refusing nested vibe-loop eval inside an active eval worker "
            "(set --allow-nested only for explicit harness debugging)",
            file=sys.stderr,
        )
        return 2

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
        try:
            required_case_conditions = release_gate_case_conditions(
                cases=tuple(args.case),
                conditions=tuple(args.condition),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        local_suite_mode = "existing_aggregate"
        if not args.dry_run and args.aggregate is None:
            aggregate = run_local_demo_eval(
                local_demo_config_from_args(
                    args,
                    config,
                    output_root=output_root,
                    cases=tuple(required_case_conditions),
                    case_conditions=required_case_conditions,
                )
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
            required_case_conditions=required_case_conditions,
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

        try:
            adapter = resolve_benchmark_adapter(
                args.adapter,
                manifest_path=args.manifest,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
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


def dispatch_specs(args: argparse.Namespace, config) -> int:
    if args.specs_command == "check":
        task_source_runtime = runtime_task_source_report(config)
        report = build_spec_diagnostics_report(
            config,
            task_source_runtime=task_source_runtime,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(render_spec_diagnostics(report))
        return 1 if int(report["blocking_count"]) else 0

    raise AssertionError(args.specs_command)


def dispatch_autopilot(args: argparse.Namespace, config) -> int:
    command = getattr(args, "autopilot_command", None)
    if command == "status":
        status = collect_project_status(config)
        if getattr(args, "json", False):
            print(json.dumps(status.to_json(), indent=2, default=list))
        else:
            print(render_autopilot_status(status))
        return 0
    if command == "projects":
        return dispatch_autopilot_projects(args)
    if command in (None, "run"):
        ap = config.autopilot
        jobs = _first_set(getattr(args, "jobs", None), ap.jobs, 1)
        interval = _first_set(getattr(args, "interval", None), ap.interval_seconds, 0.0)
        min_ready = _first_set(getattr(args, "min_ready", None), ap.min_ready, 1)
        summary = run_autopilot(
            config,
            jobs=jobs,
            interval=interval,
            once=getattr(args, "once", False),
            max_cycles=getattr(args, "max_cycles", 0),
            ask_agent=getattr(args, "ask_agent", False),
            continue_on_failure=getattr(args, "continue_on_failure", False),
            max_slices=getattr(args, "max_slices", 0),
            max_tasks=getattr(args, "max_tasks", 0),
            min_ready=min_ready,
        )
        print(json.dumps(summary.to_json(), indent=2, default=list))
        return summary.exit_code
    raise AssertionError(command)


def _first_set(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def render_autopilot_status(status: ProjectStatus) -> str:
    lines = [f"repo: {status.display_name} ({status.repo})"]
    queue = status.queue
    if queue.source_error:
        lines.append(f"queue: unavailable ({queue.source_error})")
    else:
        lines.append(
            f"queue: {queue.runnable} runnable / {queue.total} total "
            f"({queue.active} active, {queue.done} done, {queue.blocked} blocked)"
        )
    supervisor = status.supervisor
    supervisor_line = f"supervisor: {supervisor.state}"
    if supervisor.pid:
        supervisor_line += f" pid={supervisor.pid}"
    lines.append(supervisor_line)
    if supervisor.log is not None:
        lines.append(f"log: {supervisor.log}")
    if status.blockers:
        lines.append("blockers:")
        lines.extend(f"  - {blocker}" for blocker in status.blockers)
    elif status.observations:
        lines.append("observations:")
        lines.extend(f"  - {observation}" for observation in status.observations)
    else:
        lines.append("blockers: none")
    if status.last_cycle is not None:
        cycle = status.last_cycle
        lines.append(
            f"last cycle: {cycle.cycle_id} {cycle.status} @ {cycle.occurred_at}"
        )
    if status.next_wake:
        lines.append(f"next wake: {status.next_wake}")
    return "\n".join(lines)


def dispatch_wait_helper(args: argparse.Namespace) -> int:
    now = time.time()
    if args.deadline is not None:
        deadline_text = args.deadline
        try:
            deadline_epoch = parse_wait_deadline(args.deadline)
        except ValueError as exc:
            print(f"invalid --deadline: {exc}", file=sys.stderr)
            return 2
    else:
        interval = (
            args.cycle_schedule
            if args.cycle_schedule is not None
            else DEFAULT_WAIT_CYCLE_SECONDS
        )
        deadline_text, deadline_epoch = cycle_schedule_deadline(interval, now=now)
    result = wait_for_processes(
        pids=args.pid,
        deadline_epoch=deadline_epoch,
        deadline_text=deadline_text,
        mode=args.mode,
        interval=args.interval,
    )
    payload = result.to_json(at=utc_now_iso())
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(" ".join(f"{key}={value}" for key, value in payload.items()))
    return 0


def dispatch_autopilot_projects(args: argparse.Namespace) -> int:
    registry_path = args.registry or default_registry_path()
    command = args.autopilot_projects_command
    use_json = getattr(args, "json", False)

    if command == "register":
        repo = getattr(args, "repo", Path.cwd()).resolve()
        name = args.name or repo.name
        entry = ProjectEntry(name=name, repo=repo)
        ProjectRegistry.load(registry_path).with_entry(entry).save()
        if use_json:
            print(json.dumps(entry.to_json(), indent=2))
        else:
            print(f"registered {name} -> {repo}")
        return 0

    if command == "list":
        registry = ProjectRegistry.load(registry_path)
        if use_json:
            print(json.dumps([entry.to_json() for entry in registry.entries], indent=2))
        else:
            print(render_project_registry(registry))
        return 0

    if command == "inspect":
        registry = ProjectRegistry.load(registry_path)
        entry = registry.find(args.project)
        if entry is None:
            print(f"not in registry: {args.project}", file=sys.stderr)
            return 2
        status = collect_project_status(load_config(entry.repo))
        if use_json:
            payload = {"name": entry.name, "repo": str(entry.repo)}
            payload.update(status.to_json())
            print(json.dumps(payload, indent=2, default=list))
        else:
            print(render_autopilot_status(status))
        return 0

    if command == "remove":
        registry = ProjectRegistry.load(registry_path)
        updated, removed = registry.without(args.project)
        if not removed:
            print(f"not in registry: {args.project}", file=sys.stderr)
            return 2
        updated.save()
        if use_json:
            print(json.dumps({"removed": args.project}, indent=2))
        else:
            print(f"removed {args.project}")
        return 0

    if command == "status":
        registry = ProjectRegistry.load(registry_path)
        results = collect_registry_status(registry)
        if use_json:
            print(
                json.dumps(
                    [result.to_json() for result in results], indent=2, default=list
                )
            )
        else:
            print(render_aggregate_status(results))
        return 1 if any(result.error for result in results) else 0

    raise AssertionError(command)


def render_project_registry(registry: ProjectRegistry) -> str:
    if not registry.entries:
        return f"no registered projects ({registry.path})"
    return "\n".join(f"{entry.name}\t{entry.repo}" for entry in registry.entries)


def render_aggregate_status(results) -> str:
    if not results:
        return "no registered projects"
    lines: list[str] = []
    for result in results:
        if result.error:
            lines.append(f"{result.name} ({result.repo}): error: {result.error}")
            continue
        status = result.status
        queue = status.queue
        queue_text = (
            f"queue unavailable ({queue.source_error})"
            if queue.source_error
            else f"{queue.runnable} runnable / {queue.total} total"
        )
        blockers = (
            f"; blockers: {', '.join(status.blockers)}" if status.blockers else ""
        )
        lines.append(
            f"{result.name} ({result.repo}): {queue_text}; "
            f"supervisor {status.supervisor.state}{blockers}"
        )
    return "\n".join(lines)


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


def resolve_benchmark_adapter(
    name: str,
    *,
    manifest_path: Path | None = None,
) -> object | None:
    if name == "manifest":
        if manifest_path is None:
            raise ValueError("manifest benchmark adapter requires --manifest")
        from vibe_loop.eval_benchmark_manifest import ManifestBenchmarkAdapter

        return ManifestBenchmarkAdapter(manifest_path)
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
    cases: Sequence[str] | None = None,
    case_conditions: Mapping[str, Sequence[str]] | None = None,
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
        cases=tuple(cases) if cases is not None else tuple(args.case),
        conditions=() if case_conditions is not None else tuple(args.condition),
        case_conditions=case_conditions,
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
        manager = build_lock_manager(
            config.repo,
            config.state_path / "locks",
            config.locks,
        )
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
                fencing_token=fencing_token_from_args(args),
                ignored_dirty_paths=(config.state_path,),
            )
        except WorkspaceClaimError as exc:
            run_store.append_lifecycle_event(
                RunLifecycleEvent.workspace_claim_mismatch(
                    run_id=run_id,
                    task_id=task_id,
                    reason=exc.code,
                    message=str(exc),
                    details=exc.details,
                    payload={
                        "branch": args.branch,
                        "worktree": str(args.worktree),
                        "started_at": active_run_started_at(
                            manager,
                            task_id=task_id,
                            run_id=run_id,
                        ),
                    },
                )
            )
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

    if args.worker_command == "heartbeat":
        run_id, task_id = worker_identity_from_args(args)
        if not run_id or not task_id:
            print(
                "worker heartbeat requires --run-id and --task-id "
                "or VIBE_LOOP_RUN_ID and VIBE_LOOP_TASK_ID",
                file=sys.stderr,
            )
            return 2
        manager = build_lock_manager(
            config.repo,
            config.state_path / "locks",
            config.locks,
        )
        try:
            task_lock = manager.heartbeat(
                task_id=task_id,
                run_id=run_id,
                fencing_token=fencing_token_from_args(args),
            )
        except LockOwnerMismatch as exc:
            return print_lock_mutation_refused(args, "owner_mismatch", exc.metadata)
        except LockFencingMismatch as exc:
            return print_lock_mutation_refused(
                args,
                "fencing_token_mismatch",
                exc.metadata,
                expected_token=exc.expected_token,
                actual_token=exc.actual_token,
            )
        except LockBackendError as exc:
            return print_lock_mutation_refused(args, "lock_unavailable", {}, str(exc))
        payload = {
            "heartbeat": True,
            "task_id": task_id,
            "run_id": run_id,
            "heartbeat_at": task_lock.metadata.get("heartbeat_at"),
            "fencing_token": task_lock.metadata.get("fencing_token"),
        }
        if json_requested(args):
            print(json.dumps(payload, indent=2))
        else:
            print(
                "worker heartbeat recorded "
                f"task={task_id} run={run_id} at={payload['heartbeat_at']}"
            )
        return 0

    raise AssertionError(args.worker_command)


def dispatch_main_integration(args: argparse.Namespace, config) -> int:
    manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
    )
    run_store = RunStore(config.state_path / "runs.jsonl")
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
        return acquire_main_integration_command(
            args,
            config,
            manager,
            run_store,
            run_id=run_id,
            task_id=task_id,
        )

    if args.main_integration_command == "release":
        before_status = manager.main_integration_status()
        try:
            released = manager.release_main_integration(
                task_id=task_id,
                run_id=run_id,
                fencing_token=explicit_fencing_token_from_args(args),
            )
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
        except LockFencingMismatch as exc:
            status = manager.main_integration_status()
            payload = {
                "released": False,
                "error": "fencing_token_mismatch",
                "expected_token": exc.expected_token,
                "actual_token": exc.actual_token,
                "status": status.to_json(),
            }
            if json_requested(args):
                print(json.dumps(payload, indent=2))
            else:
                print(
                    "main-integration release refused: fencing_token_mismatch",
                    file=sys.stderr,
                )
            return 1
        status = manager.main_integration_status()
        if released:
            run_store.append_lifecycle_event(
                RunLifecycleEvent.lock_event(
                    LOCK_RELEASED_RECORD_TYPE,
                    run_id=run_id,
                    task_id=task_id,
                    lock_kind="integration",
                    lock_path=before_status.path,
                    payload={
                        "resource": "main-integration",
                        "owner_task_id": task_id,
                        "started_at": str(
                            before_status.metadata.get("owner_started_at")
                            or before_status.metadata.get("started_at")
                            or ""
                        ),
                    },
                )
            )
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
            print(f"agent.kind: {config.agent.agent_kind}")
            print(f"agent.command source: {config.agent.command_source}")
            print(
                "agent.selection_command source: "
                f"{config.agent.selection_command_source}"
            )
            print(f"agent.prompt_dialect source: {config.agent.prompt_dialect_source}")
            print(
                f"agent.skill_ref_prefix source: {config.agent.skill_ref_prefix_source}"
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
    source_notice = None
    if cache_notice:
        if origin == "default_markdown_discovery":
            source_notice = (
                f"{cache_notice}; task discovery source=default_markdown_discovery"
            )
        else:
            source_notice = cache_notice
    elif origin == "generated_cache":
        source_notice = (
            f"task discovery source=generated_cache path={report.get('cache_path')}"
        )
    elif origin == "default_markdown_discovery":
        source_notice = "task discovery source=default_markdown_discovery"
    elif origin == "command_output":
        source_notice = "task discovery source=command_output"
    elif origin == "explicit_config":
        task_source = report.get("task_source")
        keys = []
        if isinstance(task_source, dict):
            explicit_keys = task_source.get("explicit_source_keys")
            if isinstance(explicit_keys, list):
                keys = [str(key) for key in explicit_keys]
        suffix = f" keys={','.join(keys)}" if keys else ""
        source_notice = f"task discovery source=explicit_config{suffix}"
    return config_fallback_task_notice(config, source_notice)


def config_fallback_task_notice(config, source_notice: str | None) -> str | None:
    warning = None
    if config.config_source == "main_worktree" and config.config_path is not None:
        warning = (
            "warning: using config_source=main_worktree "
            f"config_path={config.config_path}; tasks_repo={config.repo}"
        )
    if warning and source_notice:
        return f"{warning}; {source_notice}"
    return warning or source_notice


def task_views_for_tasks(config, tasks: list[Task]):
    ids = {task.task_id for task in tasks}
    return [view for view in all_task_views(config) if view.task.task_id in ids]


def selected_task_json(config, task: Task) -> dict[str, object]:
    payload = task.to_json()
    payload.update(
        {
            "task_source_runtime": redacted_task_source_report(
                runtime_task_source_report(config),
            ),
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
        lifecycle = f"\tlifecycle={payload['lifecycle_state'] or '-'}"
        restart_count = payload.get("restart_count")
        restarts = ""
        if isinstance(restart_count, int) and restart_count > 0:
            restarts = f"\trestarts={restart_count}/{payload['max_restarts']}"
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
            f"\tcommand={payload['command']}{workspace}{lifecycle}{result}"
            f"{restarts}{diagnostics}{stale}"
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


def concurrency_diagnostics_report(workers: list[WorkerView]) -> dict[str, object]:
    active_lock_count = len(workers)
    blocked_events = [
        lock_contention_event(worker)
        for worker in workers
        if worker_has_lock_contention(worker)
    ]
    return {
        "wip_count": sum(1 for worker in workers if worker.state == "running"),
        "blocked_ratio": (
            len(blocked_events) / active_lock_count if active_lock_count else 0.0
        ),
        "active_lock_count": active_lock_count,
        "lock_contention_events": blocked_events,
    }


def worker_has_lock_contention(worker: WorkerView) -> bool:
    return worker.state != "running" or worker.result_status in {
        "blocked",
        "failed",
        "unknown",
    }


def lock_contention_event(worker: WorkerView) -> dict[str, object]:
    return {
        "task_id": worker.active.task_id,
        "run_id": worker.active.run_id,
        "state": worker.state,
        "process_state": worker.process_state,
        "reason": lock_contention_reason(worker),
        "stale_reason": worker.stale_reason,
        "result_status": worker.result_status,
        "lock": str(worker.active.lock_path or ""),
    }


def lock_contention_reason(worker: WorkerView) -> str:
    if worker.state != "running":
        return worker.stale_reason or worker.state
    if worker.result_status:
        return f"result_{worker.result_status}"
    return ""


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
        ignored_dirty_paths=(config.state_path,),
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
        result = clean_stale_locks(stale, runner.lock_manager)
        record_expired_locks(runner.run_store, result.cleaned)
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
        f"lifecycle: {payload['lifecycle_state'] or '-'}",
        "missing_lifecycle: "
        + (
            ", ".join(payload["missing_lifecycle_transitions"])
            if payload["missing_lifecycle_transitions"]
            else "-"
        ),
        f"records: {payload['record_count']}",
    ]
    if payload["restart_count"] or payload["restart_exhausted"]:
        lines.insert(
            -1,
            f"restarts: {payload['restart_count']}/{payload['max_restarts']}",
        )
    if payload["restart_exhausted"]:
        lines.insert(
            -1,
            f"restart_exhausted: {payload['restart_exhausted_reason'] or '-'}",
        )
    if payload["worker_report"]:
        lines.append(
            "worker_report: " + json.dumps(payload["worker_report"], sort_keys=True)
        )
    lines.append("record_history:")
    for record in payload["records"]:
        record_type = record.get("record_type") or "run_result"
        status = record.get("status") or record.get("classification") or "-"
        if status == "-":
            if record_type == "run_state_transition":
                status = record.get("to_state") or "-"
            elif record_type == "workspace_claim":
                status = record.get("event_type") or "workspace_claimed"
            elif record_type == "workspace_claim_mismatch":
                status = record.get("reason") or "mismatch"
            elif record_type == TASK_RESTART_RECORD_TYPE:
                if record.get("exhausted") is True:
                    status = record.get("reason") or "restart_budget_exhausted"
                else:
                    status = "restart_scheduled"
            elif record_type == TASK_RECOVERY_RECORD_TYPE:
                phase = record.get("phase") or "recovery"
                outcome = record.get("outcome")
                status = f"{phase}:{outcome}" if outcome else str(phase)
            elif isinstance(record_type, str) and record_type.startswith("lock_"):
                status = record_type.removeprefix("lock_")
        updated = (
            record.get("finished_at")
            or record.get("reported_at")
            or record.get("occurred_at")
            or record.get("claimed_at")
            or "-"
        )
        restart = ""
        if record_type == TASK_RESTART_RECORD_TYPE:
            restart = (
                f"\trestart={record.get('restart_count')}/{record.get('max_restarts')}"
            )
        lines.append(f"- {record_type}\tstatus={status}\tupdated={updated}{restart}")
    return "\n".join(lines)


def worker_identity_from_args(args: argparse.Namespace) -> tuple[str, str]:
    run_id = args.run_id or os.environ.get("VIBE_LOOP_RUN_ID", "")
    task_id = args.task_id or os.environ.get("VIBE_LOOP_TASK_ID", "")
    return run_id, task_id


def fencing_token_from_args(args: argparse.Namespace) -> str:
    return optional_string(getattr(args, "fencing_token", "")) or os.environ.get(
        "VIBE_LOOP_FENCING_TOKEN", ""
    )


def explicit_fencing_token_from_args(args: argparse.Namespace) -> str:
    return optional_string(getattr(args, "fencing_token", "")) or ""


def validate_report_fencing(args: argparse.Namespace, config) -> int | None:
    fencing_token = fencing_token_from_args(args)
    if not fencing_token:
        return None
    manager = build_lock_manager(
        config.repo,
        config.state_path / "locks",
        config.locks,
    )
    try:
        manager.validate_owner(
            task_id=args.task_id,
            run_id=args.run_id,
            fencing_token=fencing_token,
        )
    except LockOwnerMismatch:
        print("worker report refused: owner_mismatch", file=sys.stderr)
        return 1
    except LockFencingMismatch:
        print("worker report refused: fencing_token_mismatch", file=sys.stderr)
        return 1
    except LockBackendError as exc:
        if str(exc).startswith(
            "active lock not found:"
        ) and not explicit_fencing_token_from_args(args):
            return None
        print(f"worker report refused: {exc}", file=sys.stderr)
        return 1
    return None


def resolve_report_commit(repo: Path, commit: str) -> str:
    value = optional_string(commit)
    if not value:
        return ""
    result = run_git(repo, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}")
    if result is None or result.returncode != 0:
        return value
    resolved = result.stdout.strip()
    return resolved or value


def print_lock_mutation_refused(
    args: argparse.Namespace,
    error: str,
    metadata: dict[str, object],
    message: str = "",
    *,
    expected_token: str = "",
    actual_token: str = "",
) -> int:
    payload: dict[str, object] = {
        "heartbeat": False,
        "updated": False,
        "error": error,
        "metadata": metadata,
    }
    if message:
        payload["message"] = message
    if expected_token or actual_token:
        payload["expected_token"] = expected_token
        payload["actual_token"] = actual_token
    if json_requested(args):
        print(json.dumps(payload, indent=2))
    else:
        detail = f": {message}" if message else ""
        print(f"lock update refused: {error}{detail}", file=sys.stderr)
    return 1


def poll_sleep(seconds: float) -> None:
    # Dedicated seam for the acquire-wait poll loop. Tests patch this instead of
    # the global time.sleep so they observe only the poll interval and not the
    # subprocess-internal wait sleeps that load_config's `git worktree list`
    # triggers on every dispatch.
    time.sleep(seconds)


def acquire_main_integration_command(
    args: argparse.Namespace,
    config,
    manager: LockManager,
    run_store: RunStore,
    *,
    run_id: str,
    task_id: str,
) -> int:
    wait_requested = bool(args.wait or args.timeout is not None)
    deadline = (
        None
        if args.timeout is None
        else time.monotonic() + max(0.0, float(args.timeout))
    )
    while True:
        preflight = main_integration_acquire_preflight(
            args,
            config,
            manager,
            run_id=run_id,
            task_id=task_id,
        )
        if preflight.get("error"):
            record_workspace_preflight_mismatch(
                run_store,
                run_id=run_id,
                task_id=task_id,
                preflight=preflight,
            )
            return finish_main_integration_preflight_error(args, manager, preflight)
        owner_metadata = preflight.get("metadata")
        if not isinstance(owner_metadata, dict):
            status = manager.main_integration_status()
            if status.locked:
                finish_main_integration_busy(
                    args,
                    status.to_json(),
                    timed_out=False,
                )
                return 1
            print(
                "main-integration acquire requires an active task lock with "
                "matching run_id/task_id or an explicit --pid",
                file=sys.stderr,
            )
            return 2
        try:
            integration_lock = manager.acquire_main_integration(
                task_id=task_id,
                run_id=run_id,
                metadata=owner_metadata,
            )
        except LockBusy:
            status = manager.main_integration_status()
            if not status.locked and wait_requested:
                continue
            if not wait_requested or not integration_lock_waitable(status):
                finish_main_integration_busy(
                    args,
                    status.to_json(),
                    timed_out=False,
                )
                return 1
            if deadline is None:
                poll_sleep(args.poll_interval)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                finish_main_integration_busy(
                    args,
                    status.to_json(),
                    timed_out=True,
                )
                return 1
            poll_sleep(min(args.poll_interval, remaining))
            continue
        status = manager.main_integration_status()
        post_acquire_preflight = main_integration_workspace_preflight_error(
            config,
            manager,
            run_id=run_id,
            task_id=task_id,
        )
        if post_acquire_preflight is not None:
            try:
                manager.release(integration_lock)
            except (LockFencingMismatch, LockBackendError):
                pass
            record_workspace_preflight_mismatch(
                run_store,
                run_id=run_id,
                task_id=task_id,
                preflight=post_acquire_preflight,
            )
            return finish_main_integration_preflight_error(
                args,
                manager,
                post_acquire_preflight,
            )
        run_store.append_lifecycle_event(
            RunLifecycleEvent.lock_event(
                LOCK_ACQUIRED_RECORD_TYPE,
                run_id=run_id,
                task_id=task_id,
                lock_kind="integration",
                lock_path=status.path,
                payload={
                    "resource": "main-integration",
                    "owner_task_id": task_id,
                    "started_at": str(
                        status.metadata.get("owner_started_at")
                        or status.metadata.get("started_at")
                        or ""
                    ),
                },
            )
        )
        payload = {
            "acquired": True,
            "status": status.to_json(),
            "timed_out": False,
        }
        if json_requested(args):
            print(json.dumps(payload, indent=2))
        else:
            print(render_main_integration_acquired(payload["status"]))
        return 0


def finish_main_integration_preflight_error(
    args: argparse.Namespace,
    manager: LockManager,
    preflight: dict[str, object],
) -> int:
    payload = dict(preflight)
    payload["acquired"] = False
    payload["status"] = manager.main_integration_status().to_json()
    payload["timed_out"] = False
    if json_requested(args):
        print(json.dumps(payload, indent=2))
    else:
        print(render_main_integration_preflight_error(payload), file=sys.stderr)
    return int(payload.get("exit_code", 1))


def record_workspace_preflight_mismatch(
    run_store: RunStore,
    *,
    run_id: str,
    task_id: str,
    preflight: dict[str, object],
) -> None:
    if preflight.get("error") != "workspace_preflight_failed":
        return
    diagnostics = preflight.get("workspace_diagnostics")
    diagnostic_payload = diagnostics if isinstance(diagnostics, list) else []
    run_store.append_lifecycle_event(
        RunLifecycleEvent.workspace_claim_mismatch(
            run_id=run_id,
            task_id=task_id,
            reason="workspace_preflight_failed",
            message=str(preflight.get("message") or ""),
            details={"workspace_diagnostics": diagnostic_payload},
            payload={
                "diagnostic_count": len(diagnostic_payload),
                "started_at": str(preflight.get("started_at") or ""),
            },
        )
    )


def finish_main_integration_busy(
    args: argparse.Namespace,
    status: dict[str, object],
    *,
    timed_out: bool,
) -> None:
    payload = {
        "acquired": False,
        "status": status,
        "timed_out": timed_out,
    }
    if json_requested(args):
        print(json.dumps(payload, indent=2))
        return
    message = render_main_integration_busy(payload["status"])
    if timed_out:
        message = f"{message} timed_out=true"
    print(message, file=sys.stderr)


def main_integration_acquire_preflight(
    args: argparse.Namespace,
    config,
    manager: LockManager,
    *,
    run_id: str,
    task_id: str,
) -> dict[str, object]:
    task_locks: list[dict[str, object]] = []
    matching_lock: dict[str, object] | None = None
    for lock_metadata in manager.list_locks():
        if lock_metadata.get("task_id") != task_id:
            continue
        task_locks.append(lock_metadata)
        if lock_metadata.get("run_id") != run_id:
            continue
        matching_lock = lock_metadata
        break
    if matching_lock is None:
        if task_locks:
            return {
                "error": "owner_mismatch",
                "message": (
                    "main-integration acquire refused: active task lock owner "
                    "does not match"
                ),
                "expected": {"run_id": run_id, "task_id": task_id},
                "active_run_ids": [
                    value
                    for value in (
                        optional_string(lock.get("run_id")) for lock in task_locks
                    )
                    if value
                ],
                "exit_code": 1,
            }
        if args.pid > 0:
            return {"metadata": {"pid": args.pid, "pid_source": "explicit_cli"}}
        return {}
    workspace_error = main_integration_workspace_preflight_error(
        config,
        manager,
        run_id=run_id,
        task_id=task_id,
    )
    if workspace_error is not None:
        return workspace_error
    if args.pid > 0:
        return {
            "metadata": {
                "pid": args.pid,
                "pid_source": "explicit_cli",
                "owner_started_at": optional_string(matching_lock.get("started_at"))
                or "",
            }
        }
    worker_pid = positive_int(matching_lock.get("worker_pid"))
    if worker_pid is not None:
        return {
            "metadata": {
                "pid": worker_pid,
                "pid_source": "active_task_lock:worker_pid",
                "owner_started_at": optional_string(matching_lock.get("started_at"))
                or "",
            }
        }
    legacy_pid = positive_int(matching_lock.get("pid"))
    if legacy_pid is not None:
        return {
            "metadata": {
                "pid": legacy_pid,
                "pid_source": "active_task_lock:pid",
                "owner_started_at": optional_string(matching_lock.get("started_at"))
                or "",
            }
        }
    return {
        "error": "missing_worker_pid",
        "message": (
            "main-integration acquire refused: active task lock has no "
            "usable worker pid"
        ),
        "expected": {"run_id": run_id, "task_id": task_id},
        "exit_code": 2,
    }


def main_integration_workspace_preflight_error(
    config,
    manager: LockManager,
    *,
    run_id: str,
    task_id: str,
) -> dict[str, object] | None:
    run_store = RunStore(config.state_path / "runs.jsonl")
    views = build_worker_views(
        manager,
        run_store,
        repo=config.repo,
        main_branch=config.main_branch,
        ignored_dirty_paths=(config.state_path,),
    )
    for view in views:
        if view.active.task_id != task_id or view.active.run_id != run_id:
            continue
        if view.active.workspace is None or not view.workspace_diagnostics:
            return None
        return {
            "error": "workspace_preflight_failed",
            "message": (
                "main-integration acquire refused: claimed workspace is not "
                "safe for final integration"
            ),
            "workspace": view.active.workspace.to_json(),
            "workspace_git_state": (
                view.workspace_git_state.to_json()
                if view.workspace_git_state is not None
                else None
            ),
            "workspace_diagnostics": [
                diagnostic.to_json() for diagnostic in view.workspace_diagnostics
            ],
            "started_at": view.active.started_at,
            "exit_code": 1,
        }
    return None


def active_run_started_at(
    manager: LockManager,
    *,
    task_id: str,
    run_id: str,
) -> str:
    for lock_metadata in manager.list_locks():
        if (
            lock_metadata.get("task_id") == task_id
            and lock_metadata.get("run_id") == run_id
        ):
            return optional_string(lock_metadata.get("started_at")) or ""
    return ""


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def render_main_integration_preflight_error(payload: dict[str, object]) -> str:
    error = payload.get("error")
    if error == "workspace_preflight_failed":
        diagnostics = payload.get("workspace_diagnostics")
        diagnostic_items = diagnostics if isinstance(diagnostics, list) else []
        codes = [
            item.get("code", "")
            for item in diagnostic_items
            if isinstance(item, dict) and item.get("code")
        ]
        hints = [
            item.get("recovery_hint", "")
            for item in diagnostic_items
            if isinstance(item, dict) and item.get("recovery_hint")
        ]
        code_text = ",".join(str(code) for code in codes) or "unknown"
        hint_text = f"; {hints[0]}" if hints else ""
        return (
            "main-integration acquire refused: workspace_preflight_failed "
            f"codes={code_text}{hint_text}"
        )
    if error == "owner_mismatch":
        active_run_ids = payload.get("active_run_ids")
        active_text = (
            ",".join(str(value) for value in active_run_ids)
            if isinstance(active_run_ids, list)
            else ""
        )
        return (
            "main-integration acquire refused: owner_mismatch "
            f"active_run_ids={active_text}"
        )
    if error == "missing_worker_pid":
        return "main-integration acquire refused: missing_worker_pid"
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    return "main-integration acquire refused"


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
