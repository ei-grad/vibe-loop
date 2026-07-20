from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from vibe_loop.tasks import MarkdownPlanSource


PLAN_TO_LOOPYARD_STATUS = {
    "blocked": "on-hold",
    "done": "done",
    "planned": "ready",
}
LOOPYARD_PROJECT = "vibe-loop"
LOOPYARD_SOURCE = "PLAN.md ## Task Plan"


@dataclass(frozen=True)
class StatusMismatch:
    task_id: str
    plan_status: str
    board_status: str
    expected_board_status: str | None

    def to_json(self) -> dict[str, str | None]:
        return {
            "task_id": self.task_id,
            "plan_status": self.plan_status,
            "board_status": self.board_status,
            "expected_board_status": self.expected_board_status,
        }


@dataclass(frozen=True)
class DriftReport:
    shared_ids: tuple[str, ...]
    plan_only_ids: tuple[str, ...]
    board_only_ids: tuple[str, ...]
    mismatches: tuple[StatusMismatch, ...]

    @property
    def clean(self) -> bool:
        return not self.mismatches

    def to_json(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "shared_count": len(self.shared_ids),
            "plan_only_count": len(self.plan_only_ids),
            "board_only_count": len(self.board_only_ids),
            "mismatches": [mismatch.to_json() for mismatch in self.mismatches],
        }


def load_plan_statuses(path: Path) -> dict[str, str]:
    tasks = MarkdownPlanSource(path, runnable_statuses=()).list_tasks()
    return {task.task_id: task.status for task in tasks}


def parse_board_statuses(payload: object) -> dict[str, str]:
    if not isinstance(payload, list):
        raise ValueError("loopyard task list output must be a JSON array")

    statuses: dict[str, str] = {}
    for index, value in enumerate(payload):
        if not isinstance(value, Mapping):
            raise ValueError(f"loopyard task at index {index} must be an object")
        task_id = value.get("key")
        status = value.get("status")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"loopyard task at index {index} has no valid key")
        if not isinstance(status, str) or not status.strip():
            raise ValueError(f"loopyard task {task_id!r} has no valid stored status")
        if task_id in statuses:
            raise ValueError(f"duplicate loopyard task key: {task_id}")
        statuses[task_id] = status
    return statuses


def parse_board_snapshot(payload: object) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("board snapshot must be a JSON object")
    project = payload.get("project")
    source = payload.get("source")
    if project != LOOPYARD_PROJECT:
        raise ValueError(
            f"board snapshot project must be {LOOPYARD_PROJECT!r}, got {project!r}"
        )
    if source != LOOPYARD_SOURCE:
        raise ValueError(f"loopyard source must be {LOOPYARD_SOURCE!r}, got {source!r}")
    return parse_board_statuses(payload.get("tasks"))


def run_loopyard_read(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise RuntimeError("loopyard executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"loopyard read timed out after 60 seconds: {' '.join(command[1:])}"
        ) from error
    if result.returncode != 0:
        raise RuntimeError(
            f"loopyard read failed with exit code {result.returncode}: "
            f"{' '.join(command[1:])}"
        )
    return result.stdout


def parse_export_jsonl(output: str) -> list[object]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def load_board_statuses(path: Path | None) -> dict[str, str]:
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return parse_board_snapshot(payload)

    settings = json.loads(
        run_loopyard_read(
            [
                "loopyard",
                "project",
                "settings",
                "get",
                "-p",
                LOOPYARD_PROJECT,
            ]
        )
    )
    if not isinstance(settings, Mapping):
        raise ValueError("loopyard project settings output must be a JSON object")
    source = settings.get("source")
    if source != LOOPYARD_SOURCE:
        raise ValueError(f"loopyard source must be {LOOPYARD_SOURCE!r}, got {source!r}")
    exported_tasks = parse_export_jsonl(
        run_loopyard_read(
            [
                "loopyard",
                "export",
                LOOPYARD_PROJECT,
                "--format",
                "jsonl",
            ]
        )
    )
    return parse_board_statuses(exported_tasks)


def compare_statuses(
    plan_statuses: Mapping[str, str], board_statuses: Mapping[str, str]
) -> DriftReport:
    plan_ids = set(plan_statuses)
    board_ids = set(board_statuses)
    shared_ids = tuple(sorted(plan_ids & board_ids))
    if not shared_ids:
        raise ValueError("PLAN and loopyard have no shared stable task IDs")
    mismatches: list[StatusMismatch] = []

    for task_id in shared_ids:
        plan_status = plan_statuses[task_id]
        board_status = board_statuses[task_id]
        expected = PLAN_TO_LOOPYARD_STATUS.get(plan_status.casefold())
        if expected is None or board_status.casefold() != expected:
            mismatches.append(
                StatusMismatch(
                    task_id=task_id,
                    plan_status=plan_status,
                    board_status=board_status,
                    expected_board_status=expected,
                )
            )

    return DriftReport(
        shared_ids=shared_ids,
        plan_only_ids=tuple(sorted(plan_ids - board_ids)),
        board_only_ids=tuple(sorted(board_ids - plan_ids)),
        mismatches=tuple(mismatches),
    )


def render_report(report: DriftReport) -> str:
    summary = (
        f"shared={len(report.shared_ids)} "
        f"plan-only={len(report.plan_only_ids)} "
        f"loopyard-only={len(report.board_only_ids)}"
    )
    if report.clean:
        return f"PLAN/loopyard status drift: clean ({summary})"

    lines = [
        f"PLAN/loopyard status drift: {len(report.mismatches)} mismatch(es) ({summary})"
    ]
    for mismatch in report.mismatches:
        expected = mismatch.expected_board_status or "unsupported PLAN status"
        lines.append(
            f"- {mismatch.task_id}: PLAN={mismatch.plan_status}; "
            f"loopyard={mismatch.board_status}; expected loopyard={expected}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read PLAN.md and the loopyard vibe-loop board, then report stored "
            "status drift for stable task IDs present in both sources."
        )
    )
    parser.add_argument("--plan", type=Path, default=Path("PLAN.md"))
    parser.add_argument(
        "--board-json",
        type=Path,
        help=(
            "read a fixture snapshot with project, source, and tasks from a file "
            "instead of invoking loopyard"
        ),
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = compare_statuses(
            load_plan_statuses(args.plan),
            load_board_statuses(args.board_json),
        )
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as error:
        print(f"PLAN/loopyard status drift check failed: {error}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
