from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from tools.check_plan_board_drift import (
    LOOPYARD_PROJECT,
    LOOPYARD_SOURCE,
    compare_statuses,
    load_board_statuses,
    main,
    parse_board_snapshot,
    parse_board_statuses,
)


PLAN = """\
# Plan

## Task Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| EVAL-10 | P2 | Planned | none | Scope | Acceptance | Evidence |
| HOLD-01 | P1 | Blocked | none | Scope | Acceptance | Evidence |
| DONE-01 | P1 | Done | none | Scope | Acceptance | Evidence |
| PLAN-ONLY | P2 | Planned | none | Scope | Acceptance | Evidence |
"""


class PlanBoardDriftTests(unittest.TestCase):
    @staticmethod
    def write_board_snapshot(path: Path, tasks: list[dict[str, str]]) -> None:
        path.write_text(
            json.dumps(
                {
                    "project": LOOPYARD_PROJECT,
                    "source": LOOPYARD_SOURCE,
                    "tasks": tasks,
                }
            ),
            encoding="utf-8",
        )

    def test_shared_statuses_use_repository_mapping_and_ignore_unique_ids(self) -> None:
        report = compare_statuses(
            {
                "EVAL-10": "Planned",
                "HOLD-01": "Blocked",
                "DONE-01": "Done",
                "PLAN-ONLY": "Planned",
            },
            {
                "EVAL-10": "done",
                "HOLD-01": "on-hold",
                "DONE-01": "done",
                "BOARD-ONLY": "ready",
            },
        )

        self.assertEqual(report.shared_ids, ("DONE-01", "EVAL-10", "HOLD-01"))
        self.assertEqual(report.plan_only_ids, ("PLAN-ONLY",))
        self.assertEqual(report.board_only_ids, ("BOARD-ONLY",))
        self.assertEqual(len(report.mismatches), 1)
        mismatch = report.mismatches[0]
        self.assertEqual(mismatch.task_id, "EVAL-10")
        self.assertEqual(mismatch.expected_board_status, "ready")
        self.assertEqual(mismatch.board_status, "done")

    def test_cli_reports_mismatch_without_modifying_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "PLAN.md"
            board_path = root / "board.json"
            plan_path.write_text(PLAN, encoding="utf-8")
            self.write_board_snapshot(
                board_path,
                [
                    {"key": "EVAL-10", "status": "done"},
                    {"key": "HOLD-01", "status": "on-hold"},
                    {"key": "DONE-01", "status": "done"},
                    {"key": "BOARD-ONLY", "status": "ready"},
                ],
            )
            before = (plan_path.read_bytes(), board_path.read_bytes())

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    ["--plan", str(plan_path), "--board-json", str(board_path)]
                )

            self.assertEqual(result, 1)
            self.assertIn("EVAL-10: PLAN=Planned; loopyard=done", output.getvalue())
            self.assertIn("loopyard-only=1", output.getvalue())
            self.assertEqual(
                (plan_path.read_bytes(), board_path.read_bytes()),
                before,
            )

    def test_cli_returns_clean_for_matching_shared_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "PLAN.md"
            board_path = root / "board.json"
            plan_path.write_text(
                PLAN.replace("| EVAL-10 | P2 | Planned |", "| EVAL-10 | P2 | Done |"),
                encoding="utf-8",
            )
            self.write_board_snapshot(
                board_path,
                [
                    {"key": "EVAL-10", "status": "done"},
                    {"key": "HOLD-01", "status": "on-hold"},
                    {"key": "DONE-01", "status": "done"},
                    {"key": "BOARD-ONLY", "status": "ready"},
                ],
            )

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    ["--plan", str(plan_path), "--board-json", str(board_path)]
                )

        self.assertEqual(result, 0)
        self.assertIn("status drift: clean", output.getvalue())
        self.assertIn("loopyard-only=1", output.getvalue())

    def test_exported_stored_status_ignores_derived_effective_status(self) -> None:
        statuses = parse_board_statuses(
            [
                {
                    "key": "EVAL-10",
                    "status": "ready",
                    "effective_status": "blocked",
                }
            ]
        )

        report = compare_statuses({"EVAL-10": "Planned"}, statuses)

        self.assertTrue(report.clean)

    def test_board_loader_invokes_complete_read_only_explicit_project_commands(
        self,
    ) -> None:
        with patch("tools.check_plan_board_drift.subprocess.run") as run:
            run.side_effect = [
                CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"source": LOOPYARD_SOURCE}),
                ),
                CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='{"key": "EVAL-10", "status": "done"}\n',
                ),
            ]

            statuses = load_board_statuses(None)

        self.assertEqual(statuses, {"EVAL-10": "done"})
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "loopyard",
                "project",
                "settings",
                "get",
                "-p",
                "vibe-loop",
            ],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["loopyard", "export", "vibe-loop", "--format", "jsonl"],
        )
        self.assertTrue(all(not call.kwargs["check"] for call in run.call_args_list))

    def test_complete_snapshot_finds_mismatch_after_one_thousand_rows(self) -> None:
        board_statuses = {f"BOARD-{index:04d}": "ready" for index in range(1000)}
        board_statuses["EVAL-10"] = "done"

        report = compare_statuses({"EVAL-10": "Planned"}, board_statuses)

        self.assertEqual(len(report.mismatches), 1)
        self.assertEqual(report.mismatches[0].task_id, "EVAL-10")

    def test_snapshot_rejects_wrong_project_or_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "project must be 'vibe-loop'"):
            parse_board_snapshot(
                {"project": "other", "source": LOOPYARD_SOURCE, "tasks": []}
            )
        with self.assertRaisesRegex(ValueError, "loopyard source must be"):
            parse_board_snapshot(
                {"project": LOOPYARD_PROJECT, "source": "other", "tasks": []}
            )

    def test_cli_rejects_empty_board_instead_of_reporting_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "PLAN.md"
            board_path = root / "board.json"
            plan_path.write_text(PLAN, encoding="utf-8")
            self.write_board_snapshot(board_path, [])

            error = StringIO()
            with redirect_stderr(error):
                result = main(
                    ["--plan", str(plan_path), "--board-json", str(board_path)]
                )

        self.assertEqual(result, 2)
        self.assertIn("no shared stable task IDs", error.getvalue())

    def test_board_parser_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate loopyard task key"):
            parse_board_statuses(
                [
                    {"key": "EVAL-10", "status": "ready"},
                    {"key": "EVAL-10", "status": "done"},
                ]
            )


if __name__ == "__main__":
    unittest.main()
