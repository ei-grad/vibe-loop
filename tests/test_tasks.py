from __future__ import annotations

import ast
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import vibe_loop.tasks as tasks_module
from vibe_loop.config import TaskSourceConfig, shell_quote
from vibe_loop.tasks import (
    WITHHELD_ADAPTER_ENV,
    CommandTaskSource,
    MarkdownPlanSource,
    MarkdownProfileSource,
    Task,
    build_adapter_environment,
    build_task_source,
    run_json_command,
    run_reset_command,
    runnable_tasks,
    task_deliverable_path_collisions,
    task_from_mapping,
    task_sort_key,
)
from vibe_loop.task_views import build_task_views, filter_views, render_task_tree


PLAN = """# Plan

### Demo

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| DEMO-01 | P0 | Done | none | Finished base. | Works. | Worklog. |
| DEMO-02 | P1 | Next | DEMO-01 | Ready task. | Works. | Not started. |
| DEMO-03 | P1 | Next | MISSING-01 | Blocked task. | Works. | Not started. |
| DEMO-04 | P0 | Planned | DEMO-01 | Planned task. | Works. | Not started. |
| DEMO-05 | P0 | Gated | DEMO-01 | Gated task. | Works. | Gated. |
"""


def planning_warning(
    requested_path: str = "tools/new-helper.sh",
    existing_path: str = "tools/existing-helper.sh",
    match: str = "exact",
) -> dict[str, str]:
    return {
        "kind": "deliverable_path_collision",
        "requested_path": requested_path,
        "existing_path": existing_path,
        "match": match,
        "effect": "advisory_only",
        "precision": tasks_module.DELIVERABLE_COLLISION_PRECISION,
    }


class RespectSourceOrderTests(unittest.TestCase):
    @staticmethod
    def _source(tasks: list[Task]) -> object:
        class _Source:
            def list_tasks(self) -> list[Task]:
                return list(tasks)

            def probe(self, task_id: str) -> None:
                return None

        return _Source()

    def test_flag_makes_source_order_authoritative(self) -> None:
        # A low-priority task emitted first (order=0) — the "dragged to top"
        # case — must dispatch before a high-priority task emitted later.
        low_first = Task("LOW", "Dragged to top", "ready", priority="low", order=0)
        high_second = Task(
            "HIGH", "Higher priority, lower in list", "ready", priority="high", order=1
        )
        source = self._source([high_second, low_first])  # unsorted input

        respected = runnable_tasks(source, ("ready",), respect_source_order=True)
        self.assertEqual([task.task_id for task in respected], ["LOW", "HIGH"])

    def test_default_keeps_priority_leading(self) -> None:
        low_first = Task("LOW", "Emitted first", "ready", priority="low", order=0)
        high_second = Task("HIGH", "Emitted second", "ready", priority="high", order=1)
        source = self._source([low_first, high_second])

        default = runnable_tasks(source, ("ready",))
        self.assertEqual([task.task_id for task in default], ["HIGH", "LOW"])

    def test_task_sort_key_shapes(self) -> None:
        task = Task("T", "t", "ready", priority="low", order=3)
        self.assertEqual(task_sort_key(task, respect_source_order=True), (9, 3))
        self.assertEqual(task_sort_key(task), (9, 99, 3))

    def test_status_rank_is_case_insensitive(self) -> None:
        tasks = [
            Task("PLANNED", "Planned", "Planned", order=0),
            Task("ACTIVE", "Active", "active", order=1),
            Task("NEXT", "Next", "NEXT", order=2),
        ]

        ordered = sorted(tasks, key=task_sort_key)

        self.assertEqual(
            [task.task_id for task in ordered], ["ACTIVE", "NEXT", "PLANNED"]
        )

    def test_runnable_status_allowlist_is_case_sensitive(self) -> None:
        source = self._source([Task("LOWER", "Lowercase", "active")])

        self.assertEqual(runnable_tasks(source, ("Active",)), [])
        self.assertEqual(
            [task.task_id for task in runnable_tasks(source, ("active",))],
            ["LOWER"],
        )


class MarkdownPlanTests(unittest.TestCase):
    def test_deliverable_collision_flags_exact_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tools").mkdir()
            (repo / "tools" / "existing-helper.sh").write_text("#!/bin/sh\n")
            task = Task(
                "TASK-EXACT",
                "Add helper",
                "Next",
                scope="Deliver tools/existing-helper.sh.",
            )

            collisions = task_deliverable_path_collisions(repo, task)

        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["requested_path"], "tools/existing-helper.sh")
        self.assertEqual(collisions[0]["existing_path"], "tools/existing-helper.sh")
        self.assertEqual(collisions[0]["match"], "exact")
        self.assertEqual(collisions[0]["effect"], "advisory_only")

    def test_deliverable_collision_flags_close_same_directory_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tools").mkdir()
            (repo / "tools" / "qemu-proof-outcome.sh").write_text("#!/bin/sh\n")
            task = Task(
                "TASK-SIBLING",
                "Add startup guard",
                "Next",
                acceptance=(
                    "Create tools/qemu-proof-startup-guard.sh and source it "
                    "from proof harnesses."
                ),
            )

            collisions = task_deliverable_path_collisions(repo, task)

        self.assertEqual(len(collisions), 1)
        self.assertEqual(
            collisions[0]["existing_path"],
            "tools/qemu-proof-outcome.sh",
        )
        self.assertEqual(collisions[0]["match"], "same_directory_sibling")

    def test_deliverable_collision_ignores_no_match_and_incidental_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tools").mkdir()
            (repo / "tools" / "unrelated.py").write_text("")
            task = Task(
                "TASK-CLEAR",
                "Add focused helper",
                "Next",
                scope="Create `tools/qemu-proof-startup-guard.sh`.",
                evidence="Existing behavior is documented in tools/unrelated.py.",
            )

            collisions = task_deliverable_path_collisions(repo, task)

        self.assertEqual(collisions, ())

    def test_deliverable_collision_ignores_existing_modification_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "src").mkdir()
            (repo / "src" / "existing.py").write_text("")
            task = Task(
                "TASK-MODIFY",
                "Add validation",
                "Next",
                scope=(
                    "Add a bounded validator to src/existing.py and wire the "
                    "new gate through src/existing.py."
                ),
            )

            collisions = task_deliverable_path_collisions(repo, task)

        self.assertEqual(collisions, ())

    def test_command_task_uses_body_for_planning_validation_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tools").mkdir()
            (repo / "tools" / "new-helper.sh").write_text("#!/bin/sh\n")
            task = task_from_mapping(
                {
                    "id": "TASK-BODY",
                    "status": "ready",
                    "body": "Create tools/new-helper.sh.",
                },
                0,
            )

            collisions = task_deliverable_path_collisions(repo, task)

        self.assertEqual(task.scope, "")
        self.assertEqual(task.body, "Create tools/new-helper.sh.")
        self.assertNotIn("body", task.to_json())
        self.assertEqual(collisions[0]["existing_path"], "tools/new-helper.sh")

    def test_command_task_planning_warnings_round_trip(self) -> None:
        warnings = [
            planning_warning(),
            planning_warning(
                "tools/new-startup-guard.sh",
                "tools/startup-guard.sh",
                "same_directory_sibling",
            ),
        ]

        task = task_from_mapping(
            {
                "id": "TASK-WARNINGS",
                "status": "ready",
                "planning_warnings": warnings,
            },
            0,
        )

        self.assertEqual(task.planning_warnings, tuple(warnings))
        self.assertEqual(task.to_json()["planning_warnings"], warnings)

    def test_command_task_planning_warnings_omission_and_null_are_empty(self) -> None:
        omitted = task_from_mapping({"id": "OMITTED", "status": "ready"}, 0)
        null = task_from_mapping(
            {"id": "NULL", "status": "ready", "planning_warnings": None},
            0,
        )

        self.assertEqual(omitted.planning_warnings, ())
        self.assertEqual(null.planning_warnings, ())
        self.assertNotIn("planning_warnings", omitted.to_json())
        self.assertNotIn("planning_warnings", null.to_json())

    def test_command_task_ignores_nested_planning_warnings(self) -> None:
        task = task_from_mapping(
            {
                "id": "NESTED",
                "status": "ready",
                "fields": {"planning_warnings": [planning_warning()]},
            },
            0,
        )

        self.assertEqual(task.planning_warnings, ())

    def test_command_task_rejects_invalid_planning_warning_collection(self) -> None:
        invalid_values = (
            planning_warning(),
            [planning_warning()] * 4,
            ["warning"],
        )

        for value in invalid_values:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "planning_warnings"),
            ):
                task_from_mapping(
                    {"id": "INVALID", "status": "ready", "planning_warnings": value},
                    0,
                )

    def test_command_task_rejects_unknown_or_missing_planning_warning_keys(
        self,
    ) -> None:
        unknown = planning_warning() | {"detail": "extra"}
        missing = planning_warning()
        del missing["effect"]

        for warning in (unknown, missing):
            with (
                self.subTest(warning=warning),
                self.assertRaisesRegex(ValueError, "must contain exactly"),
            ):
                task_from_mapping(
                    {
                        "id": "INVALID",
                        "status": "ready",
                        "planning_warnings": [warning],
                    },
                    0,
                )

    def test_command_task_rejects_invalid_planning_warning_enums(self) -> None:
        invalid_values = {
            "kind": "other_warning",
            "match": "similar",
            "effect": "blocking",
            "precision": "approximate",
        }

        for key, value in invalid_values.items():
            warning = planning_warning()
            warning[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                task_from_mapping(
                    {
                        "id": "INVALID",
                        "status": "ready",
                        "planning_warnings": [warning],
                    },
                    0,
                )

    def test_command_task_rejects_invalid_planning_warning_paths(self) -> None:
        invalid_paths = (
            "/tools/helper.sh",
            "tools/../helper.sh",
            "tools/helper",
            "tools/.env",
        )

        for path_key in ("requested_path", "existing_path"):
            for path in invalid_paths:
                warning = planning_warning()
                warning[path_key] = path
                with (
                    self.subTest(path_key=path_key, path=path),
                    self.assertRaisesRegex(
                        ValueError,
                        rf"task planning_warnings\[0\]\.{path_key}",
                    ),
                ):
                    task_from_mapping(
                        {
                            "id": "INVALID",
                            "status": "ready",
                            "planning_warnings": [warning],
                        },
                        0,
                    )

    def test_command_task_rejects_planning_warning_path_over_byte_limit(self) -> None:
        warning = planning_warning(requested_path=f"tools/{'é' * 509}.py")

        with self.assertRaisesRegex(ValueError, "1024 UTF-8 bytes"):
            task_from_mapping(
                {
                    "id": "INVALID",
                    "status": "ready",
                    "planning_warnings": [warning],
                },
                0,
            )

    def test_command_task_rejects_planning_warning_over_entry_byte_limit(self) -> None:
        long_path = f"tools/{'a' * 990}.py"
        warning = planning_warning(long_path, long_path)

        with self.assertRaisesRegex(ValueError, "2048 UTF-8 bytes"):
            task_from_mapping(
                {
                    "id": "INVALID",
                    "status": "ready",
                    "planning_warnings": [warning],
                },
                0,
            )

    def test_command_task_rejects_non_string_planning_warning_value(self) -> None:
        warning = planning_warning()
        warning["requested_path"] = 7  # type: ignore[assignment]

        with self.assertRaisesRegex(ValueError, "values must be strings"):
            task_from_mapping(
                {
                    "id": "INVALID",
                    "status": "ready",
                    "planning_warnings": [warning],
                },
                0,
            )

    def test_command_task_planning_warning_error_identifies_invalid_task(self) -> None:
        invalid_warning = planning_warning()
        invalid_warning["precision"] += "."
        payload = [
            {"id": "TASK-A", "status": "ready"},
            {
                "id": "TASK-B",
                "title": "Invalid warning producer",
                "status": "ready",
                "planning_warnings": [invalid_warning],
            },
            {"id": "TASK-C", "status": "ready"},
        ]
        source = CommandTaskSource(
            Path("."),
            TaskSourceConfig(type="command", list_command="unused"),
        )

        with (
            mock.patch.object(tasks_module, "run_json_command", return_value=payload),
            self.assertRaisesRegex(
                ValueError,
                r"task_source\.list entry 1 \(id='TASK-B'\): "
                r"task planning_warnings\[0\]\.precision must use the canonical value",
            ),
        ):
            source.list_tasks()

    def test_task_json_omits_empty_traceability_fields(self) -> None:
        payload = Task("TASK-01", "Plain task", "Next").to_json()

        self.assertNotIn("requirement_ids", payload)
        self.assertNotIn("spec_paths", payload)
        self.assertNotIn("design_refs", payload)
        self.assertNotIn("approval_state", payload)
        self.assertNotIn("source_fingerprints", payload)

    def test_command_task_preserves_source_status_reason(self) -> None:
        task = task_from_mapping(
            {
                "id": "TASK-GATED",
                "status": "gated",
                "reason": "agent profile is not registered",
            },
            0,
        )

        self.assertEqual(task.status_reason, "agent profile is not registered")
        self.assertEqual(
            task.to_json()["status_reason"],
            "agent profile is not registered",
        )

    def test_command_task_preserves_review_budget_carryover_fields(self) -> None:
        finding = {
            "id": "F-1",
            "severity": "P1",
            "summary": "Open finding",
            "evidence": "reproduction",
            "files": ["src/example.py"],
            "lines": ["12"],
            "state": "open",
        }
        task = task_from_mapping(
            {
                "id": "TASK-REVIEW",
                "status": "ready",
                "fields": {
                    "prior_findings": [finding],
                    "review_budget_exhaustions": 2,
                },
            },
            0,
        )

        self.assertEqual(task.prior_findings, (finding,))
        self.assertEqual(task.review_budget_exhaustions, 2)
        self.assertEqual(task.to_json()["prior_findings"], [finding])
        self.assertEqual(task.to_json()["review_budget_exhaustions"], 2)

    def test_runnable_tasks_filter_dependencies_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))

            tasks = runnable_tasks(source, ("Active", "Next", "Planned"))
            done_included = runnable_tasks(source, ("Done", "Next", "Planned"))

        self.assertEqual([task.task_id for task in tasks], ["DEMO-02", "DEMO-04"])
        self.assertNotIn("DEMO-01", [task.task_id for task in done_included])

    def test_lowercase_done_status_resolves_dependencies(self) -> None:
        # A command/JSON task source reporting a lowercase "done" must be
        # recognized as done so a dependent "ready" task becomes runnable.
        done_task = task_from_mapping({"id": "DEP", "status": "done"}, 0)
        gated = task_from_mapping(
            {"id": "GATED", "status": "ready", "dependencies": ["DEP"]}, 1
        )
        self.assertTrue(done_task.done)

        class _Source:
            def list_tasks(self):
                return [done_task, gated]

            def probe(self, task_id):
                return None

        runnable = runnable_tasks(_Source(), ("ready",))
        self.assertEqual([task.task_id for task in runnable], ["GATED"])

    def test_lowercase_done_status_resolves_task_views_and_is_hidden(self) -> None:
        done_task = Task("DEP", "Dependency", "done")
        dependent = Task(
            "DEPENDENT",
            "Dependent",
            "Next",
            dependencies=("DEP",),
        )

        views = build_task_views([done_task, dependent], locked_ids=set())

        by_id = {view.task.task_id: view for view in views}
        self.assertTrue(by_id["DEPENDENT"].ready)
        self.assertEqual(
            [view.task.task_id for view in filter_views(views, include_done=False)],
            ["DEPENDENT"],
        )

    def test_task_views_sort_status_bands_case_insensitively(self) -> None:
        views = build_task_views(
            [
                Task("PLANNED", "Planned", "Planned", order=0),
                Task("ACTIVE", "Active", "active", order=1),
            ],
            locked_ids=set(),
            runnable_statuses=("active", "Planned"),
        )

        self.assertEqual(
            [view.task.task_id for view in filter_views(views)],
            ["ACTIVE", "PLANNED"],
        )

    def test_plan_tasks_include_section_for_tree_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))
            views = build_task_views(source.list_tasks(), locked_ids=set())

            output = render_task_tree(views)

        self.assertIn("Demo", output)
        self.assertIn("DEMO-02 [Next/P1] Ready task", output)

    def test_task_tree_surfaces_repeated_review_budget_exhaustion(self) -> None:
        task = Task(
            "REVIEW-1",
            "Repeated review failure",
            "ready",
            review_budget_exhaustions=2,
        )

        output = render_task_tree(build_task_views([task], locked_ids=set()))

        self.assertIn("review-budget-exhaustions=2", output)

    def test_default_markdown_source_discovers_nonstandard_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "planning").mkdir()
            (repo / "planning" / "backlog.md").write_text(PLAN, encoding="utf-8")

            source = build_task_source(repo, TaskSourceConfig())

            expected = f"{(repo / 'planning' / 'backlog.md').as_posix()}:Demo"
            self.assertEqual(source.list_tasks()[0].source, expected)

    def test_explicit_plan_path_wins_over_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
            (repo / "custom.md").write_text(
                PLAN.replace("DEMO-02", "CUSTOM-02"),
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(plan_path="custom.md"),
            )

            self.assertIn("CUSTOM-02", [task.task_id for task in source.list_tasks()])

    def test_markdown_discovery_requires_explicit_path_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.md").write_text(PLAN, encoding="utf-8")
            (repo / "b.md").write_text(PLAN, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "multiple markdown plan files"):
                build_task_source(repo, TaskSourceConfig())

    def test_markdown_discovery_uses_candidate_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "notes.md").write_text(PLAN, encoding="utf-8")
            (repo / "roadmap.md").write_text(
                PLAN.replace("DEMO-02", "ROADMAP-02"),
                encoding="utf-8",
            )

            source = build_task_source(repo, TaskSourceConfig())

            self.assertIn("ROADMAP-02", [task.task_id for task in source.list_tasks()])

    def test_markdown_discovery_tolerates_invalid_utf8_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "plan.md").write_bytes(PLAN.encode("utf-8") + b"\xff\n")

            source = build_task_source(repo, TaskSourceConfig())

            self.assertIn("DEMO-02", [task.task_id for task in source.list_tasks()])

    def test_default_markdown_plan_ignores_unrelated_metadata_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(
                PLAN
                + "\n| ID | Name |\n"
                + "| --- | --- |\n"
                + "| meta | Metadata row. |\n",
                encoding="utf-8",
            )
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))

            tasks = source.list_tasks()

        self.assertIn("DEMO-02", [task.task_id for task in tasks])

    def test_ralphex_markdown_source_extracts_headings_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(ralphex_fixture_text(), encoding="utf-8")
            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()
            candidates = runnable_tasks(source, ("Active", "Next", "Planned"))

        self.assertEqual(
            [task.task_id for task in tasks],
            [
                "docs.plans.checkout:task-1",
                "docs.plans.checkout:iteration-2.5",
            ],
        )
        self.assertEqual(tasks[0].title, "Add checkout API")
        self.assertEqual(tasks[0].section, "Checkout Flow")
        self.assertEqual(tasks[0].status, "Planned")
        self.assertEqual(tasks[0].resources, ("api", "checkout"))
        self.assertEqual(
            tasks[0].paths,
            ("src/checkout.py", "tests/test_checkout.py"),
        )
        self.assertTrue(tasks[0].conflict_domains_known)
        self.assertIn("Add checkout handler", tasks[0].acceptance)
        self.assertIn(
            "uv run -m pytest tests/test_checkout.py",
            tasks[0].evidence,
        )
        self.assertNotIn("Add checkout handler", tasks[0].evidence)
        self.assertNotIn("Resources:", tasks[0].evidence)
        self.assertEqual(tasks[1].status, "Done")
        self.assertEqual(tasks[1].resources, ())
        self.assertEqual(tasks[1].paths, ())
        self.assertTrue(tasks[1].conflict_domains_known)
        self.assertEqual([task.task_id for task in candidates], [tasks[0].task_id])

    def test_ralphex_markdown_source_discovers_single_plan_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(ralphex_fixture_text(), encoding="utf-8")

            source = build_task_source(
                repo,
                TaskSourceConfig(type="ralphex-markdown"),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].task_id, "docs.plans.checkout:task-1")

    def test_ralphex_markdown_discovery_ignores_fenced_validation_examples(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            first = repo / "a.md"
            second = repo / "b.md"
            first.write_text(
                "# Plan: A\n\n"
                "```markdown\n"
                "## Validation Commands\n"
                "- fake validate\n"
                "```\n\n"
                "### Task 1: A\n"
                "- [ ] Work\n",
                encoding="utf-8",
            )
            second.write_text(
                "# Plan: B\n\n### Task 1: B\n- [ ] Work\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "multiple ralphex markdown plan files tied",
            ):
                build_task_source(repo, TaskSourceConfig(type="ralphex-markdown"))

    def test_ralphex_markdown_source_sanitizes_plan_path_in_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout flow.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(ralphex_fixture_text(), encoding="utf-8")

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout flow.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].task_id, "docs.plans.checkout-flow:task-1")

    def test_ralphex_markdown_source_uses_plan_level_conflict_surface(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                "# Plan: Checkout Flow\n\n"
                "## Conflict Surface\n"
                "- Resources: checkout, api\n"
                "- Paths: src/checkout.py, tests/test_checkout.py\n\n"
                "## Validation Commands\n"
                "- `uv run -m pytest tests/test_checkout.py`\n\n"
                "### Task 1: Add checkout API\n"
                "- [ ] Add checkout handler\n",
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ("checkout", "api"))
        self.assertEqual(
            tasks[0].paths,
            ("src/checkout.py", "tests/test_checkout.py"),
        )
        self.assertTrue(tasks[0].conflict_domains_known)

    def test_ralphex_markdown_source_reads_unlabeled_conflict_surface_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                "# Plan: Checkout Flow\n\n"
                "## Conflict Surface\n"
                "Owned by this plan:\n"
                "- `src/checkout.py`\n"
                "- tests/test_checkout.py\n"
                "- Makefile\n"
                "- README.md\n"
                "- `.vibe-loop.toml`\n"
                "- `tools/task-tool` plus tests\n\n"
                "- src/a.py, plus tests\n"
                "- docs/notes.md.\n"
                "- Kernel, scheduler, and runtime behavior\n\n"
                "### Task 1: Add checkout API\n"
                "- [ ] Add checkout handler\n",
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(
            tasks[0].paths,
            (
                "src/checkout.py",
                "tests/test_checkout.py",
                "Makefile",
                "README.md",
                ".vibe-loop.toml",
                "tools/task-tool",
                "src/a.py",
                "docs/notes.md",
            ),
        )
        self.assertTrue(tasks[0].conflict_domains_known)

    def test_ralphex_markdown_source_blank_task_labels_override_plan_domains(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                "# Plan: Checkout Flow\n\n"
                "## Conflict Surface\n"
                "- Resources: checkout\n"
                "- Paths: src/checkout.py\n\n"
                "### Task 1: Add checkout API\n"
                "- [ ] Add checkout handler\n"
                "- Resources:\n"
                "- Paths:\n",
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ())
        self.assertEqual(tasks[0].paths, ())
        self.assertFalse(tasks[0].conflict_domains_known)

    def test_ralphex_blank_task_label_clears_earlier_task_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                "# Plan: Checkout Flow\n\n"
                "### Task 1: Add checkout API\n"
                "- [ ] Add checkout handler\n"
                "- Resources: checkout\n"
                "- Resources:\n"
                "- Paths: src/checkout.py\n"
                "- Paths:\n",
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ())
        self.assertEqual(tasks[0].paths, ())
        self.assertFalse(tasks[0].conflict_domains_known)

    def test_ralphex_markdown_source_splits_task_conflict_surface_label(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = repo / "docs" / "plans" / "checkout.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                "# Plan: Checkout Flow\n\n"
                "### Task 1: Add checkout API\n"
                "- [ ] Add checkout handler\n"
                "- Conflict Surface: resources: checkout, api; "
                "paths: src/checkout.py, tests/test_checkout.py\n",
                encoding="utf-8",
            )

            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_path="docs/plans/checkout.md",
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ("checkout", "api"))
        self.assertEqual(
            tasks[0].paths,
            ("src/checkout.py", "tests/test_checkout.py"),
        )
        self.assertTrue(tasks[0].conflict_domains_known)

    def test_ralphex_markdown_source_reads_multiple_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            first = repo / "docs" / "plans" / "checkout.md"
            second = repo / "docs" / "plans" / "refund.md"
            first.parent.mkdir(parents=True)
            first.write_text(ralphex_fixture_text(), encoding="utf-8")
            second.write_text(
                ralphex_fixture_text().replace(
                    "# Plan: Checkout Flow",
                    "# Plan: Refund Flow",
                ),
                encoding="utf-8",
            )
            source = build_task_source(
                repo,
                TaskSourceConfig(
                    type="ralphex-markdown",
                    plan_paths=(
                        "docs/plans/checkout.md",
                        "docs/plans/refund.md",
                    ),
                    explicit_keys=frozenset({"type", "plan_paths"}),
                ),
            )

            tasks = source.list_tasks()

        self.assertEqual(
            [task.task_id for task in tasks],
            [
                "docs.plans.checkout:task-1",
                "docs.plans.checkout:iteration-2.5",
                "docs.plans.refund:task-1",
                "docs.plans.refund:iteration-2.5",
            ],
        )

    def test_spec_kit_source_extracts_prefixed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "specs" / "001-checkout" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                spec_driven_fixture_text("spec-kit-tasks.md"),
                encoding="utf-8",
            )
            source = build_task_source(repo, TaskSourceConfig(type="spec-kit"))

            tasks = source.list_tasks()
            candidates = runnable_tasks(source, ("Active", "Next", "Planned"))

        self.assertEqual(
            [task.task_id for task in tasks],
            ["001-checkout:T001", "001-checkout:T002"],
        )
        self.assertEqual(tasks[0].status, "Done")
        self.assertEqual(tasks[1].status, "Planned")
        self.assertEqual(tasks[1].title, "Add checkout API contract test")
        self.assertEqual(tasks[1].dependencies, ("001-checkout:T001",))
        self.assertIn("contract test fails", tasks[1].acceptance)
        self.assertIn("specs/001-checkout/tasks.md", tasks[1].source)
        self.assertEqual([task.task_id for task in candidates], ["001-checkout:T002"])

    def test_spec_tool_source_extracts_conflict_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "specs" / "001-checkout" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Tasks\n\n"
                "- [ ] T001 Add checkout API\n"
                "  - Resources: API docs and reviewer setup\n"
                "  - Conflict Resources:\n"
                "    - api\n"
                "    - checkout\n"
                "  - Conflict Paths:\n"
                "    - src/api\n"
                "    - src/checkout.py\n"
                "- [ ] T002 Update docs\n"
                "  - Conflict Resources: none\n"
                "  - Conflict Paths: none\n"
                "- [ ] T003 Missing conflict metadata\n",
                encoding="utf-8",
            )
            source = build_task_source(repo, TaskSourceConfig(type="spec-kit"))

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ("api", "checkout"))
        self.assertEqual(tasks[0].paths, ("src/api", "src/checkout.py"))
        self.assertTrue(tasks[0].conflict_domains_known)
        self.assertEqual(tasks[1].resources, ())
        self.assertEqual(tasks[1].paths, ())
        self.assertTrue(tasks[1].conflict_domains_known)
        self.assertEqual(tasks[2].resources, ())
        self.assertEqual(tasks[2].paths, ())
        self.assertFalse(tasks[2].conflict_domains_known)

    def test_kiro_source_discovers_tasks_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / ".kiro" / "specs" / "session-refresh" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                spec_driven_fixture_text("kiro-tasks.md"),
                encoding="utf-8",
            )
            source = build_task_source(repo, TaskSourceConfig(type="kiro"))

            tasks = source.list_tasks()

        self.assertEqual(
            [task.task_id for task in tasks],
            ["session-refresh:1", "session-refresh:2"],
        )
        self.assertEqual(tasks[1].dependencies, ("session-refresh:1",))
        self.assertEqual(tasks[1].status, "Planned")
        self.assertEqual(tasks[1].title, "Implement session refresh")
        self.assertIn("repository abstraction", tasks[1].acceptance)

    def test_openspec_source_treats_in_progress_checkbox_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "openspec" / "changes" / "checkout-mutation" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                spec_driven_fixture_text("openspec-tasks.md"),
                encoding="utf-8",
            )
            source = build_task_source(repo, TaskSourceConfig(type="openspec"))

            tasks = source.list_tasks()
            candidates = runnable_tasks(source, ("Active", "Next", "Planned"))

        self.assertEqual(
            [task.task_id for task in tasks],
            ["checkout-mutation:1.1", "checkout-mutation:1.2"],
        )
        self.assertEqual(tasks[1].status, "Active")
        self.assertEqual(tasks[1].dependencies, ("checkout-mutation:1.1",))
        self.assertIn("idempotency keys", tasks[1].acceptance)
        self.assertIn("duplicate request", tasks[1].acceptance)
        self.assertEqual(
            [task.task_id for task in candidates], ["checkout-mutation:1.2"]
        )

    def test_spec_tool_sources_degrade_when_stable_ids_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "openspec" / "changes" / "ambiguous" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text("- [ ] Implement ambiguous task\n", encoding="utf-8")
            source = build_task_source(repo, TaskSourceConfig(type="openspec"))

            with self.assertRaisesRegex(ValueError, "missing required field id"):
                source.list_tasks()

    def test_spec_tool_sources_reject_empty_task_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / ".kiro" / "specs" / "unsupported" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Tasks\n\nNo checkbox tasks here.\n", encoding="utf-8")
            source = build_task_source(repo, TaskSourceConfig(type="kiro"))

            with self.assertRaisesRegex(ValueError, "no Kiro tasks found"):
                source.list_tasks()

    def test_spec_tool_sources_reject_explicit_empty_plan_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            with self.assertRaisesRegex(ValueError, "requires at least one path"):
                build_task_source(
                    repo,
                    TaskSourceConfig(
                        type="openspec",
                        plan_paths=(),
                        explicit_keys=frozenset({"type", "plan_paths"}),
                    ),
                )

    def test_spec_tool_sources_reject_invalid_dependency_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "specs" / "broken-deps" / "tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "- [x] T001 Base task\n"
                "- [ ] T002 Dependent task\n"
                "  - Dependencies: T001 T003\n",
                encoding="utf-8",
            )
            source = build_task_source(repo, TaskSourceConfig(type="spec-kit"))

            with self.assertRaisesRegex(ValueError, "invalid dependency syntax"):
                source.list_tasks()

    def test_profile_table_supports_column_aliases_and_reordered_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Summary | Depends On | State | Key | Prio | Proof |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| Finish base. More detail. | none | Closed | WORK-01 | P0 | merged |\n"
                "| Use base. | WORK-01 | Todo | WORK-02 | P1 | pending |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, work_table_profile())

            tasks = source.list_tasks()
            candidates = runnable_tasks(source, ("Todo",))

        self.assertEqual([task.task_id for task in tasks], ["WORK-01", "WORK-02"])
        self.assertEqual(tasks[0].status, "Done")
        self.assertEqual(tasks[0].title, "Finish base")
        self.assertEqual(tasks[1].dependencies, ("WORK-01",))
        self.assertEqual(tasks[1].priority, "P1")
        self.assertEqual([task.task_id for task in candidates], ["WORK-02"])

    def test_profile_runnable_statuses_are_not_blocked_by_default_status_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary |\n"
                "| --- | --- | --- |\n"
                "| WORK-01 | Low | Explicitly runnable. |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, work_table_profile())

            candidates = runnable_tasks(source, ("Low",))

        self.assertEqual([task.task_id for task in candidates], ["WORK-01"])

    def test_profile_table_extracts_conflict_domains(self) -> None:
        profile = work_table_profile()
        fields = profile["fields"]
        assert isinstance(fields, dict)
        fields["resources"] = {"column": "Resources"}
        fields["paths"] = {"column": "Paths"}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary | Resources | Paths |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| WORK-01 | Todo | API change. | api, schema | src/api, db/schema.sql |\n"
                "| WORK-02 | Todo | No writes. | none | none |\n"
                "| WORK-03 | Todo | Missing declaration. |  |  |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, profile)

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].resources, ("api", "schema"))
        self.assertEqual(tasks[0].paths, ("src/api", "db/schema.sql"))
        self.assertTrue(tasks[0].conflict_domains_known)
        self.assertEqual(tasks[1].resources, ())
        self.assertEqual(tasks[1].paths, ())
        self.assertTrue(tasks[1].conflict_domains_known)
        self.assertEqual(tasks[2].resources, ())
        self.assertEqual(tasks[2].paths, ())
        self.assertFalse(tasks[2].conflict_domains_known)

    def test_command_task_source_extracts_conflict_domains(self) -> None:
        task = task_from_mapping(
            {
                "id": "CMD-01",
                "title": "Command task",
                "status": "Next",
                "resources": ["api", "api", "db"],
                "paths": ["src/api", "src/api/", "db/schema.sql"],
            },
            0,
        )

        self.assertEqual(task.resources, ("api", "db"))
        self.assertEqual(task.paths, ("src/api", "db/schema.sql"))
        self.assertTrue(task.conflict_domains_known)

        unknown = task_from_mapping(
            {
                "id": "CMD-02",
                "title": "Unknown domains",
                "status": "Next",
                "resources": None,
                "paths": None,
            },
            0,
        )
        empty = task_from_mapping(
            {
                "id": "CMD-03",
                "title": "Explicitly empty domains",
                "status": "Next",
                "resources": [],
                "paths": [],
            },
            0,
        )

        self.assertFalse(unknown.conflict_domains_known)
        self.assertTrue(empty.conflict_domains_known)

    def test_command_task_source_extracts_domain_change_provenance(self) -> None:
        task = task_from_mapping(
            {
                "id": "CMD-01",
                "title": "Command task",
                "status": "Next",
                "paths": ["src"],
                "conflict_domains_actor_kind": "operator",
                "conflict_domains_actor": "alice",
            },
            0,
        )

        self.assertEqual(task.conflict_domains_actor_kind, "operator")
        self.assertEqual(task.conflict_domains_actor, "alice")

    def test_command_task_source_rejects_invalid_domain_actor_kind(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "conflict_domains_actor_kind must be one of",
        ):
            task_from_mapping(
                {
                    "id": "CMD-01",
                    "title": "Command task",
                    "status": "Next",
                    "paths": ["src"],
                    "conflict_domains_actor_kind": "implementer",
                },
                0,
            )

    def test_command_task_source_parses_agent_model_and_hazards(self) -> None:
        task = task_from_mapping(
            {
                "id": "CMD-04",
                "title": "Security task",
                "status": "Next",
                "agent": "claude-opus",
                "model": "opus",
                "hazards": ["abi", "abi", "  dma  ", ""],
            },
            0,
        )

        self.assertEqual(task.agent, "claude-opus")
        self.assertEqual(task.model, "opus")
        self.assertEqual(task.hazards, ("abi", "dma"))
        payload = task.to_json()
        self.assertEqual(payload["agent"], "claude-opus")
        self.assertEqual(payload["model"], "opus")
        self.assertEqual(payload["hazards"], ["abi", "dma"])

    def test_command_task_source_agent_model_and_hazards_are_absent_safe(self) -> None:
        task = task_from_mapping(
            {"id": "CMD-05", "title": "Plain task", "status": "Next"}, 0
        )

        self.assertEqual(task.agent, "")
        self.assertEqual(task.model, "")
        self.assertEqual(task.hazards, ())
        payload = task.to_json()
        self.assertNotIn("agent", payload)
        self.assertNotIn("model", payload)
        self.assertNotIn("hazards", payload)

    def test_command_task_source_rejects_non_string_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "task model must be a string"):
            task_from_mapping({"id": "CMD-06", "status": "Next", "model": 42}, 0)

    def test_command_task_source_rejects_non_string_hazards(self) -> None:
        with self.assertRaisesRegex(ValueError, "task hazards"):
            task_from_mapping({"id": "CMD-06", "status": "Next", "hazards": [1, 2]}, 0)

    def test_command_task_source_preserves_traceability_fields(self) -> None:
        task = task_from_mapping(
            {
                "id": "CMD-01",
                "title": "Command task",
                "status": "Next",
                "requirement_ids": ["PRD-SDE-003", "PRD-SDE-003", "REQ-9"],
                "spec_paths": ["docs/prd/spec-driven-execution.md"],
                "design_refs": ["ADR-7", "docs/design.md#traceability"],
                "approval_state": "approved",
                "source_fingerprints": [
                    {
                        "path": "docs/prd/spec-driven-execution.md",
                        "size": 20,
                        "sha256": "a" * 64,
                        "redacted": False,
                    }
                ],
            },
            0,
        )

        payload = task.to_json()

        self.assertEqual(task.requirement_ids, ("PRD-SDE-003", "REQ-9"))
        self.assertEqual(task.spec_paths, ("docs/prd/spec-driven-execution.md",))
        self.assertEqual(
            payload["source_fingerprints"],
            [
                {
                    "path": "docs/prd/spec-driven-execution.md",
                    "size": 20,
                    "sha256": "a" * 64,
                    "redacted": False,
                }
            ],
        )
        self.assertEqual(payload["approval_state"], "approved")

    def test_command_task_source_reset_invokes_hook_with_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            marker = repo / "reset.log"
            source = CommandTaskSource(
                repo,
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    reset_command=f"printf '%s' {{task_id}} > {marker}",
                ),
            )

            invoked = source.reset("TASK-42")

            self.assertTrue(invoked)
            self.assertEqual(marker.read_text(encoding="utf-8"), "TASK-42")

    def test_command_task_source_probe_preserves_untrusted_task_id_argument(
        self,
    ) -> None:
        probe_program = (
            "import json, sys; print(json.dumps(dict(id=sys.argv[1], status='Next')))"
        )
        probe_command = (
            f"{shell_quote(sys.executable)} -c {shell_quote(probe_program)} {{task_id}}"
        )
        task_ids = (
            "TASK; echo injected",
            "TASK$(echo injected)",
            "TASK 'single' \"double\" whitespace",
            "TASK\nnewline",
            "TASK & | < > ( ) ^ % !",
        )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    probe_command=probe_command,
                ),
            )

            for task_id in task_ids:
                with self.subTest(task_id=task_id):
                    task = source.probe(task_id)
                    self.assertIsNotNone(task)
                    self.assertEqual(task.task_id, task_id)

    def test_command_task_source_probe_does_not_execute_injected_command(
        self,
    ) -> None:
        probe_program = (
            "import json, sys; print(json.dumps(dict(id=sys.argv[1], status='Next')))"
        )
        write_marker_program = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('injected', encoding='utf-8')"
        )
        separator = "&" if sys.platform == "win32" else ";"

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            marker = repo / "injected"
            probe_command = (
                f"{shell_quote(sys.executable)} -c {shell_quote(probe_program)} "
                "{task_id}"
            )
            injected_task_id = (
                f"TASK-42 {separator} {shell_quote(sys.executable)} "
                f"-c {shell_quote(write_marker_program)} {shell_quote(str(marker))}"
            )
            source = CommandTaskSource(
                repo,
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    probe_command=probe_command,
                ),
            )

            task = source.probe(injected_task_id)

            self.assertIsNotNone(task)
            self.assertEqual(task.task_id, injected_task_id)
            self.assertFalse(marker.exists())

    def test_command_task_source_probe_preserves_configured_pipeline(self) -> None:
        probe_program = (
            "import json, sys; print(json.dumps(dict(id=sys.argv[1], status='Next')))"
        )
        passthrough_program = "import sys; sys.stdout.write(sys.stdin.read())"

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    probe_command=(
                        f"{shell_quote(sys.executable)} -c "
                        f"{shell_quote(probe_program)} {{task_id}} | "
                        f"{shell_quote(sys.executable)} -c "
                        f"{shell_quote(passthrough_program)}"
                    ),
                ),
            )

            task = source.probe("TASK-42")

            self.assertIsNotNone(task)
            self.assertEqual(task.task_id, "TASK-42")

    def test_command_task_source_reset_preserves_untrusted_task_id_argument(
        self,
    ) -> None:
        reset_program = (
            "import json, sys; "
            "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(sys.argv[2:]))"
        )
        task_id = "TASK; $(command) 'single' \"double\"\nspace &|<>()^%!"

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            captured = repo / "reset-argv.json"
            source = CommandTaskSource(
                repo,
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    reset_command=(
                        f"{shell_quote(sys.executable)} -c "
                        f"{shell_quote(reset_program)} "
                        f"{shell_quote(str(captured))} {{task_id}}"
                    ),
                ),
            )

            self.assertTrue(source.reset(task_id))

            self.assertEqual(
                json.loads(captured.read_text(encoding="utf-8")),
                [task_id],
            )

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX shell injection matrix; Windows adapters fail closed below",
    )
    def test_command_task_source_adapters_do_not_execute_hostile_task_ids(
        self,
    ) -> None:
        recorder_program = (
            "import json, sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text("
            "json.dumps(sys.argv[2:]), encoding='utf-8'); "
            "print(json.dumps(dict(id=sys.argv[2], status='Next')))"
        )
        marker_program = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('injected', encoding='utf-8')"
        )

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            captured = repo / "captured.json"
            injected = repo / "injected"
            injected_command = (
                f"{shell_quote(sys.executable)} -c {shell_quote(marker_program)} "
                f"{shell_quote(str(injected))}"
            )
            task_ids = (
                f"TASK; {injected_command}",
                f"TASK && {injected_command}",
                f"TASK `{injected_command}`",
                f"TASK $({injected_command})",
                "TASK 'single' \"double\"",
                f"TASK\n{injected_command}",
            )
            command = (
                f"{shell_quote(sys.executable)} -c "
                f"{shell_quote(recorder_program)} "
                f"{shell_quote(str(captured))} {{task_id}}"
            )

            for adapter in ("probe", "activate", "reset", "transition"):
                for task_id in task_ids:
                    with self.subTest(adapter=adapter, task_id=task_id):
                        captured.unlink(missing_ok=True)
                        injected.unlink(missing_ok=True)
                        source = CommandTaskSource(
                            repo,
                            TaskSourceConfig(
                                type="command",
                                list_command="echo '[]'",
                                probe_command=command,
                                activate_command=command,
                                reset_command=command,
                                complete_command=command,
                            ),
                        )

                        if adapter == "probe":
                            source.probe(task_id)
                        elif adapter == "activate":
                            source.activate(task_id, "run-1")
                        elif adapter == "reset":
                            source.reset(task_id)
                        else:
                            source.complete(task_id, "run-1")

                        self.assertEqual(
                            json.loads(captured.read_text(encoding="utf-8")),
                            [task_id],
                        )
                        self.assertFalse(injected.exists())

    def test_command_task_source_has_no_direct_format_or_fstring_command_assignments(
        self,
    ) -> None:
        module = ast.parse(Path(tasks_module.__file__).read_text(encoding="utf-8"))
        command_source = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "CommandTaskSource"
        )
        unsafe_lines: list[int] = []
        for node in ast.walk(command_source):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            target_names = {
                target.id for target in targets if isinstance(target, ast.Name)
            }
            if not any(name.endswith("command") for name in target_names):
                continue
            value = node.value
            if any(
                isinstance(child, ast.JoinedStr)
                or (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "format"
                )
                for child in ast.walk(value)
            ):
                unsafe_lines.append(node.lineno)

        self.assertEqual(
            unsafe_lines,
            [],
            "task-source command variables must not use direct .format() "
            f"or f-string assignments; found at lines {unsafe_lines}",
        )

    def test_command_task_source_probe_and_reset_reject_unsafe_templates(
        self,
    ) -> None:
        templates = (
            "adapter {unsupported}",
            "adapter {task_id!r}",
            "adapter {task_id:>10}",
            "adapter {task_id",
        )

        with tempfile.TemporaryDirectory() as directory:
            for template in templates:
                with self.subTest(template=template):
                    source = CommandTaskSource(
                        Path(directory),
                        TaskSourceConfig(
                            type="command",
                            list_command="echo '[]'",
                            probe_command=template,
                            reset_command=template,
                        ),
                    )
                    with self.assertRaisesRegex(ValueError, "may only use"):
                        source.probe("TASK-42")
                    with self.assertRaisesRegex(ValueError, "may only use"):
                        source.reset("TASK-42")

    def test_windows_command_task_source_adapters_fail_closed_on_unsafe_ids(
        self,
    ) -> None:
        unsafe_task_ids = (
            'TASK" & calc & "X',
            "TASK%USERPROFILE%",
            "TASK!delayed!",
            "TASK\ncommand",
        )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    activate_command="activate {task_id} {run_id}",
                    complete_command="complete {task_id} {run_id}",
                    reset_command="reset {task_id}",
                    park_command="park {task_id} {run_id}",
                ),
            )
            with mock.patch("vibe_loop.config.sys.platform", "win32"):
                with mock.patch("vibe_loop.tasks.subprocess.run") as run:
                    for task_id in unsafe_task_ids:
                        with self.subTest(task_id=task_id):
                            adapter_calls = (
                                lambda: source.probe(task_id),
                                lambda: source.activate(task_id, "run-1"),
                                lambda: source.activate("TASK-1", task_id),
                                lambda: source.complete(task_id, "run-1"),
                                lambda: source.complete("TASK-1", task_id),
                                lambda: source.reset(task_id),
                                lambda: source.park(task_id, "run-1"),
                                lambda: source.park("TASK-1", task_id),
                            )
                            for call in adapter_calls:
                                with self.assertRaisesRegex(ValueError, "cmd.exe"):
                                    call()
                    run.assert_not_called()

    def test_command_task_source_activate_returns_confirmed_task(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured["command"] = args[0]
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout='{"id":"TASK-42","title":"Claimed","status":"active"}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    activate_command="activate {task_id} --run {run_id}",
                ),
                runtime_context={"PROJECT_SELECTOR": "configured"},
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.activate(
                    "TASK-42",
                    "run-7",
                    runtime_context={
                        "PROJECT_SELECTOR": "ambient",
                        "VIBE_LOOP_FENCING_TOKEN": "generation-3",
                    },
                )

        self.assertIsNotNone(task)
        self.assertEqual(task.task_id, "TASK-42")
        self.assertEqual(task.status, "active")
        self.assertEqual(captured["command"], "activate TASK-42 --run run-7")
        environment = captured["env"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["PROJECT_SELECTOR"], "configured")
        self.assertEqual(environment["VIBE_LOOP_FENCING_TOKEN"], "generation-3")

    def test_command_task_source_accepts_lock_side_activation(self) -> None:
        calls: list[str] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            command = str(args[0])
            calls.append(command)
            self.assertTrue(command.startswith("probe "))
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout='{"id":"TASK-42","title":"Claimed","status":"active"}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    activate_command="transition {task_id} --expect ready",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.activate("TASK-42", "run-7")

        self.assertIsNotNone(task)
        self.assertEqual(task.status, "active")
        self.assertEqual(calls, ["probe TASK-42"])

    def test_command_task_source_activates_when_lock_leaves_task_runnable(
        self,
    ) -> None:
        calls: list[str] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            command = str(args[0])
            calls.append(command)
            status = "Next" if command.startswith("probe ") else "active"
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=json.dumps(
                    {"id": "TASK-42", "title": "Claimed", "status": status}
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    activate_command="transition {task_id} --expect ready",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.activate("TASK-42", "run-7")

        self.assertIsNotNone(task)
        self.assertEqual(task.status, "active")
        self.assertEqual(
            calls,
            ["probe TASK-42", "transition TASK-42 --expect ready"],
        )

    def test_command_task_source_does_not_accept_other_non_runnable_states(
        self,
    ) -> None:
        for probed_status in ("Parked", "review"):
            with self.subTest(status=probed_status):
                calls: list[str] = []

                def fake_run(
                    *args: object,
                    **kwargs: object,
                ) -> subprocess.CompletedProcess:
                    command = str(args[0])
                    calls.append(command)
                    status = probed_status if command.startswith("probe ") else "active"
                    return subprocess.CompletedProcess(
                        args[0],
                        0,
                        stdout=json.dumps(
                            {
                                "id": "TASK-42",
                                "title": "Claimed",
                                "status": status,
                            }
                        ),
                        stderr="",
                    )

                with tempfile.TemporaryDirectory() as directory:
                    source = CommandTaskSource(
                        Path(directory),
                        TaskSourceConfig(
                            type="command",
                            list_command="list-tasks",
                            probe_command="probe {task_id}",
                            activate_command="transition {task_id} --expect ready",
                            runnable_statuses=("ready",),
                        ),
                    )
                    with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                        task = source.activate("TASK-42", "run-7")

                self.assertIsNotNone(task)
                self.assertEqual(task.status, "active")
                self.assertEqual(
                    calls,
                    ["probe TASK-42", "transition TASK-42 --expect ready"],
                )

    def test_command_task_source_activation_failure_includes_stderr(self) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            raise subprocess.CalledProcessError(
                3,
                args[0],
                stderr="expected ready but task is active\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    activate_command="transition {task_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                with self.assertRaisesRegex(
                    subprocess.CalledProcessError,
                    "expected ready but task is active",
                ):
                    source.activate("TASK-42", "run-7")

    def test_command_task_source_activate_is_required_for_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(type="command", list_command="list-tasks"),
            )

            with self.assertRaisesRegex(ValueError, "requires task_source.activate"):
                source.activate("TASK-42", "run-7")

    def test_command_task_source_activate_quotes_template_values(self) -> None:
        captured: list[str] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured.append(str(args[0]))
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=(
                    '{"id":"TASK-42; touch /tmp/injected","title":"Claimed",'
                    '"status":"active"}'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    activate_command="activate {task_id} --run {run_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                source.activate(
                    "TASK-42; touch /tmp/injected",
                    "run-7; touch /tmp/run-injected",
                )

        self.assertEqual(
            captured,
            [
                "activate 'TASK-42; touch /tmp/injected' --run "
                "'run-7; touch /tmp/run-injected'"
            ],
        )

    def test_command_task_source_activate_rejects_unknown_template_field(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    activate_command="activate {unsupported}",
                ),
            )

            with self.assertRaisesRegex(ValueError, "may only use"):
                source.activate("TASK-42", "run-7")

    def test_command_task_source_completion_and_park_return_confirmation(
        self,
    ) -> None:
        calls: list[str] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            command = str(args[0])
            calls.append(command)
            status = "done" if command.startswith("complete") else "on-hold"
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=json.dumps(
                    {"id": "TASK unsafe", "title": "Task", "status": status}
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    complete_command="complete {task_id} --run {run_id}",
                    park_command="park {task_id} --run {run_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                completed = source.complete("TASK unsafe", "run unsafe")
                parked = source.park("TASK unsafe", "run unsafe")

        self.assertIsNotNone(completed)
        self.assertTrue(completed.done)
        self.assertIsNotNone(parked)
        self.assertEqual(parked.status, "on-hold")
        self.assertEqual(
            calls,
            [
                "complete 'TASK unsafe' --run 'run unsafe'",
                "park 'TASK unsafe' --run 'run unsafe'",
            ],
        )

    def test_command_task_source_completion_rejects_unconfirmed_shape(self) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    complete_command="complete {task_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                with self.assertRaisesRegex(ValueError, "normalized task JSON"):
                    source.complete("TASK-42", "run-7")

    def test_command_task_source_continuation_confirms_without_reactivation(
        self,
    ) -> None:
        calls: list[str] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            calls.append(str(args[0]))
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout='{"id":"TASK-42","title":"Claimed","status":"active"}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    activate_command="activate {task_id} --run {run_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.activate("TASK-42", "run-8", continuation=True)

        self.assertIsNotNone(task)
        self.assertEqual(calls, ["probe TASK-42"])

    def test_command_task_source_continuation_rejects_missing_task(self) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout="null",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    activate_command="activate {task_id} --run {run_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                with self.assertRaisesRegex(ValueError, "returned no task"):
                    source.activate("TASK-42", "run-8", continuation=True)

    def test_command_task_source_reset_without_hook_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = CommandTaskSource(
                repo,
                TaskSourceConfig(type="command", list_command="echo '[]'"),
            )

            self.assertFalse(source.reset("TASK-42"))

    def test_command_task_source_reset_propagates_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = CommandTaskSource(
                repo,
                TaskSourceConfig(
                    type="command",
                    list_command="echo '[]'",
                    reset_command="exit 3",
                ),
            )

            with self.assertRaises(subprocess.CalledProcessError):
                source.reset("TASK-42")

    def test_command_task_source_list_applies_configured_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured.update(kwargs)
            return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    command_timeout_seconds=7.5,
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                self.assertEqual(source.list_tasks(), [])

        self.assertEqual(captured["timeout"], 7.5)

    def test_command_task_source_probe_applies_configured_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured.update(kwargs)
            return subprocess.CompletedProcess(
                args[0], 0, stdout='{"id": "TASK-1", "status": "Next"}', stderr=""
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    command_timeout_seconds=9.0,
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.probe("TASK-1")

        self.assertIsNotNone(task)
        self.assertEqual(captured["timeout"], 9.0)

    def test_command_task_source_reset_applies_configured_timeout(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured.update(kwargs)
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    reset_command="reset {task_id}",
                    command_timeout_seconds=3.0,
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                self.assertTrue(source.reset("TASK-1"))

        self.assertEqual(captured["timeout"], 3.0)

    def test_command_task_source_context_covers_list_probe_and_reset(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []

        def fake_run(
            command: str, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            calls.append((command, dict(environment), id(environment)))
            environment["PROJECT_SELECTOR"] = "mutated-copy"
            if command == "list-tasks":
                stdout = "[]"
            elif command == "probe TASK-1":
                stdout = '{"id": "TASK-1", "status": "Next"}'
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    probe_command="probe {task_id}",
                    reset_command="reset {task_id}",
                ),
                runtime_context={"PROJECT_SELECTOR": "entry-selector"},
            )
            with mock.patch.dict(os.environ, {"PROJECT_SELECTOR": "host-selector"}):
                with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                    self.assertEqual(source.list_tasks(), [])
                    self.assertEqual(source.probe("TASK-1").task_id, "TASK-1")
                    self.assertTrue(
                        source.reset(
                            "TASK-1",
                            runtime_context={
                                "VIBE_LOOP_RUN_ID": "run-1",
                                "VIBE_LOOP_FENCING_TOKEN": "dynamic-generation",
                            },
                        )
                    )
                inherited_after = os.environ["PROJECT_SELECTOR"]

        self.assertEqual(inherited_after, "host-selector")
        self.assertEqual(
            [command for command, _environment, _identity in calls],
            ["list-tasks", "probe TASK-1", "reset TASK-1"],
        )
        self.assertTrue(
            all(
                environment["PROJECT_SELECTOR"] == "entry-selector"
                for _command, environment, _identity in calls
            )
        )
        self.assertEqual(
            len({identity for _command, _environment, identity in calls}), 3
        )
        reset_environment = calls[-1][1]
        self.assertEqual(reset_environment["VIBE_LOOP_RUN_ID"], "run-1")
        self.assertEqual(
            reset_environment["VIBE_LOOP_FENCING_TOKEN"],
            "dynamic-generation",
        )

    def test_command_task_source_redacts_active_token_from_json_output(self) -> None:
        token = "1"

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=json.dumps(
                    [
                        {
                            "id": "TASK-1",
                            "status": "Next",
                            "title": f"leaked {token}",
                            "acceptance": "source-generation-",
                            "priority": 1,
                        }
                    ]
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(type="command", list_command="list-tasks"),
                runtime_context={"VIBE_LOOP_FENCING_TOKEN": token},
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                task = source.list_tasks()[0]

        self.assertEqual(task.title, "leaked <redacted>")
        self.assertEqual(task.acceptance, "source-generation-")
        self.assertEqual(task.task_id, "TASK-1")
        self.assertEqual(task.priority, "1")

    def test_command_task_source_redacts_active_token_from_errors(self) -> None:
        token = "source-generation-5"

        def fail_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            raise subprocess.CalledProcessError(
                7,
                f"backend --generation {token}",
                output=f"stdout token={token}",
                stderr=f"stderr token={token}".encode(),
            )

        with tempfile.TemporaryDirectory() as directory:
            source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(type="command", list_command="list-tasks"),
                runtime_context={"VIBE_LOOP_FENCING_TOKEN": token},
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", fail_run):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    source.list_tasks()

        error = caught.exception
        self.assertNotIn(token, str(error))
        self.assertEqual(error.output, "stdout token=<redacted>")
        self.assertEqual(error.stderr, b"stderr token=<redacted>")

    def test_json_command_diagnoses_empty_stdout(self) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("vibe_loop.tasks.subprocess.run", fake_run):
                with self.assertRaisesRegex(ValueError, "returned empty stdout"):
                    run_json_command(Path(directory), "adapter")

    def test_command_task_source_surfaces_timeout_as_subprocess_error(self) -> None:
        # A hung backend command expires as TimeoutExpired — a SubprocessError
        # (so every caller's (SubprocessError, OSError) fail-safe covers it) but
        # not a CalledProcessError (so it is never mistaken for a JSON failure).
        self.assertTrue(
            issubclass(subprocess.TimeoutExpired, subprocess.SubprocessError)
        )
        self.assertFalse(
            issubclass(subprocess.TimeoutExpired, subprocess.CalledProcessError)
        )

        def raise_timeout(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

        with tempfile.TemporaryDirectory() as directory:
            list_source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(type="command", list_command="list-tasks"),
            )
            reset_source = CommandTaskSource(
                Path(directory),
                TaskSourceConfig(
                    type="command",
                    list_command="list-tasks",
                    reset_command="reset {task_id}",
                ),
            )
            with mock.patch("vibe_loop.tasks.subprocess.run", raise_timeout):
                with self.assertRaises(subprocess.TimeoutExpired):
                    list_source.list_tasks()
                with self.assertRaises(subprocess.TimeoutExpired):
                    reset_source.reset("TASK-1")

    def test_profile_table_extracts_traceability_fields(self) -> None:
        profile = work_table_profile()
        fields = profile["fields"]
        assert isinstance(fields, dict)
        fields["requirement_ids"] = {"column": "Requirements"}
        fields["spec_paths"] = {"column": "Spec Paths"}
        fields["design_refs"] = {"column": "Design Refs"}
        fields["approval_state"] = {"column": "Approval"}
        fields["source_fingerprints"] = {"column": "Fingerprints"}
        fingerprint = {
            "path": "docs/spec.md",
            "size": 10,
            "sha256": "b" * 64,
            "redacted": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary | Requirements | Spec Paths | Design Refs | Approval | Fingerprints |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| WORK-01 | Todo | Trace task. | PRD-SDE-003, REQ-2 | docs/spec.md | ADR-1, docs/design.md#trace | approved | "
                f"{json_fingerprint(fingerprint)} |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, profile)

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].requirement_ids, ("PRD-SDE-003", "REQ-2"))
        self.assertEqual(tasks[0].spec_paths, ("docs/spec.md",))
        self.assertEqual(tasks[0].design_refs, ("ADR-1", "docs/design.md#trace"))
        self.assertEqual(tasks[0].approval_state, "approved")
        self.assertEqual(tasks[0].source_fingerprints, (fingerprint,))

    def test_profile_heading_docs_extract_tasks_from_heading_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n"
                "## HEAD-01: Build heading parser\n"
                "Status: Complete\n"
                "Priority: P0\n"
                "Depends: none\n"
                "Acceptance: Works.\n\n"
                "## HEAD-02: Use heading parser\n"
                "Status: Ready\n"
                "Priority: P1\n"
                "Depends: HEAD-01\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, heading_profile())

            tasks = source.list_tasks()

        self.assertEqual([task.task_id for task in tasks], ["HEAD-01", "HEAD-02"])
        self.assertEqual(tasks[0].status, "Done")
        self.assertEqual(tasks[0].title, "Build heading parser")
        self.assertEqual(tasks[0].section, "Backlog")
        self.assertEqual(tasks[1].dependencies, ("HEAD-01",))

    def test_profile_list_docs_extract_tasks_from_items_and_nested_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "TODO.md").write_text(
                "# Tasks\n\n"
                "- LIST-01 | closed | Build list parser\n"
                "  - State: Closed\n"
                "  - Depends: none\n"
                "- LIST-02 | ready | Use list parser\n"
                "  - State: Todo\n"
                "  - Depends: LIST-01\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, list_profile())

            tasks = source.list_tasks()

        self.assertEqual([task.task_id for task in tasks], ["LIST-01", "LIST-02"])
        self.assertEqual(tasks[0].status, "Done")
        self.assertEqual(tasks[1].title, "Use list parser")
        self.assertEqual(tasks[1].section, "Tasks")
        self.assertEqual(tasks[1].dependencies, ("LIST-01",))

    def test_profile_list_extracts_nested_tasks_under_grouping_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "TODO.md").write_text(
                "# Tasks\n\n"
                "- Backend\n"
                "  - LIST-01 | ready | Build API\n"
                "    - State: Todo\n"
                "    - Depends: none\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, list_profile())

            tasks = source.list_tasks()

        self.assertEqual([task.task_id for task in tasks], ["LIST-01"])
        self.assertEqual(tasks[0].title, "Build API")

    def test_profile_parsing_rejects_duplicate_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary |\n"
                "| --- | --- | --- |\n"
                "| DUP-01 | Todo | First. |\n"
                "| DUP-01 | Todo | Second. |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, work_table_profile())

            with self.assertRaisesRegex(ValueError, "duplicate task id DUP-01"):
                source.list_tasks()

    def test_profile_table_rejects_missing_required_columns(self) -> None:
        cases = [
            (
                "| Key | Summary |\n"
                "| --- | --- |\n"
                "| WORK-01 | Missing status column. |\n",
                "State",
            ),
            (
                "| Key | State |\n| --- | --- |\n| WORK-01 | Todo |\n",
                "Summary",
            ),
        ]
        for table, missing_column in cases:
            with self.subTest(missing_column=missing_column):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / "WORK.md").write_text(
                        f"# Work\n\n{table}",
                        encoding="utf-8",
                    )
                    source = MarkdownProfileSource(repo, work_table_profile())

                    with self.assertRaisesRegex(
                        ValueError,
                        f"missing required table columns: {missing_column}",
                    ):
                        source.list_tasks()

    def test_profile_table_rejects_later_profile_related_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary |\n"
                "| --- | --- | --- |\n"
                "| WORK-01 | Todo | Valid task. |\n\n"
                "| Key | Summary |\n"
                "| --- | --- |\n"
                "| WORK-02 | Missing status column. |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, work_table_profile())

            with self.assertRaisesRegex(
                ValueError, "missing required table columns: State"
            ):
                source.list_tasks()

    def test_profile_parsing_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n## HEAD-01: Missing status\nDepends: none\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, heading_profile())

            with self.assertRaisesRegex(ValueError, "missing required field status"):
                source.list_tasks()

    def test_profile_heading_rejects_task_like_record_without_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n## Missing ID\nStatus: Ready\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, heading_profile())

            with self.assertRaisesRegex(ValueError, "missing required field id"):
                source.list_tasks()

    def test_profile_heading_title_only_sections_do_not_trigger_missing_id(
        self,
    ) -> None:
        profile = heading_profile()
        fields = profile["fields"]
        assert isinstance(fields, dict)
        fields["id"] = {
            "pattern": r"^(?P<id>[A-Z]+-\d+)$",
            "strategy": "heading_text",
        }
        fields["title"] = {"strategy": "heading_text"}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n## HEAD-01\nStatus: Ready\nDepends: none\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, profile)

            tasks = source.list_tasks()

        self.assertEqual([task.task_id for task in tasks], ["HEAD-01"])
        self.assertEqual(tasks[0].title, "HEAD-01")

    def test_profile_heading_full_text_strategy_extracts_record_text(self) -> None:
        profile = heading_profile()
        fields = profile["fields"]
        assert isinstance(fields, dict)
        fields["id"] = {"label": "ID"}
        fields["title"] = {"strategy": "full_text"}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n"
                "## Full Text Task\n"
                "ID: HEAD-01\n"
                "Status: Ready\n"
                "Depends: none\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, profile)

            tasks = source.list_tasks()

        self.assertEqual([task.task_id for task in tasks], ["HEAD-01"])
        self.assertIn("Full Text Task", tasks[0].title)
        self.assertIn("Status: Ready", tasks[0].title)

    def test_profile_heading_scalar_labels_do_not_absorb_following_prose(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "BACKLOG.md").write_text(
                "# Backlog\n\n"
                "## HEAD-01: Build heading parser\n"
                "Status: Ready\n"
                "This prose belongs to the task body, not the status label.\n"
                "Acceptance:\n"
                "- Parser keeps scalar labels bounded.\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, heading_profile())

            tasks = source.list_tasks()

        self.assertEqual(tasks[0].status, "Ready")
        self.assertIn("Parser keeps scalar labels", tasks[0].acceptance)

    def test_profile_list_rejects_task_like_record_without_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "TODO.md").write_text(
                "# Tasks\n\n- Missing ID\n  - State: Todo\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, list_profile())

            with self.assertRaisesRegex(ValueError, "missing required field id"):
                source.list_tasks()

    def test_profile_parser_rejects_unimplemented_literal_strategy(self) -> None:
        profile = work_table_profile()
        fields = profile["fields"]
        assert isinstance(fields, dict)
        title = fields["title"]
        assert isinstance(title, dict)
        title["strategy"] = "literal"

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "strategy is not supported"):
                MarkdownProfileSource(repo, profile)

    def test_profile_parsing_rejects_dependency_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "WORK.md").write_text(
                "# Work\n\n"
                "| Key | State | Summary | Depends On |\n"
                "| --- | --- | --- | --- |\n"
                "| WORK-01 | Closed | Base. | none |\n"
                "| WORK-02 | Todo | Broken deps. | WORK-01 WORK-03 |\n",
                encoding="utf-8",
            )
            source = MarkdownProfileSource(repo, work_table_profile())

            with self.assertRaisesRegex(ValueError, "invalid dependency syntax"):
                source.list_tasks()


def work_table_profile() -> dict[str, object]:
    return {
        "kind": "markdown_table",
        "source_paths": ["WORK.md"],
        "stable_ids": True,
        "fields": {
            "id": {"column": "Key"},
            "title": {"column": "Summary", "strategy": "first_sentence"},
            "status": {"column": "State"},
            "dependencies": {"column": "Depends On", "none_values": ["none", "-"]},
            "priority": {"column": "Prio"},
            "evidence": {"column": "Proof"},
        },
        "status_map": {
            "done": ["Closed"],
            "runnable": ["Todo"],
            "blocked": ["Blocked"],
        },
    }


def heading_profile() -> dict[str, object]:
    return {
        "kind": "markdown_headings",
        "source_paths": ["BACKLOG.md"],
        "stable_ids": True,
        "fields": {
            "id": {
                "pattern": r"^(?P<id>[A-Z]+-\d+):",
                "strategy": "heading_text",
            },
            "title": {
                "pattern": r"^[A-Z]+-\d+:\s*(?P<title>.+)$",
                "strategy": "heading_text",
            },
            "status": {"label": "Status"},
            "priority": {"label": "Priority"},
            "dependencies": {"label": "Depends", "none_values": ["none"]},
            "acceptance": {"label": "Acceptance"},
        },
        "status_map": {
            "done": ["Complete"],
            "runnable": ["Ready"],
            "blocked": ["Blocked"],
        },
    }


def list_profile() -> dict[str, object]:
    return {
        "kind": "markdown_list",
        "source_paths": ["TODO.md"],
        "stable_ids": True,
        "fields": {
            "id": {
                "pattern": r"^(?P<id>[A-Z]+-\d+)\b",
                "strategy": "heading_text",
            },
            "title": {
                "pattern": r"^[A-Z]+-\d+\s*\|\s*[^|]+\|\s*(?P<title>.+)$",
                "strategy": "heading_text",
            },
            "status": {"label": "State"},
            "dependencies": {"label": "Depends", "none_values": ["none"]},
        },
        "status_map": {
            "done": ["Closed"],
            "runnable": ["Todo"],
            "blocked": ["Blocked"],
        },
    }


def json_fingerprint(value: dict[str, object]) -> str:
    import json

    return json.dumps([value], separators=(",", ":"))


def ralphex_fixture_text() -> str:
    return (Path(__file__).parent / "fixtures" / "ralphex-plan.md").read_text(
        encoding="utf-8"
    )


def spec_driven_fixture_text(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "spec-driven" / name).read_text(
        encoding="utf-8"
    )


class WithheldAdapterEnvironmentTests(unittest.TestCase):
    """A name the runtime withholds must be absent in the adapter process.

    The runtime asserts absence by leaving a name out of the runtime context,
    but the child environment starts from `os.environ.copy()` and `dict.update`
    cannot remove a key. Without an explicit removal an ambient value satisfies
    a name the runtime deliberately withheld, converting absent attribution
    into attribution a consumer then acts on.
    """

    AMBIENT = {
        "VIBE_LOOP_BRANCH": "ambient-branch",
        "VIBE_LOOP_FENCING_TOKEN": "ambient-generation",
        "VIBE_LOOP_IMPLEMENTER_SESSION": "ambient-implementer",
        "VIBE_LOOP_PRIOR_FINDINGS": '[{"id":"ambient"}]',
        "VIBE_LOOP_REPO": "/ambient/repo",
        "VIBE_LOOP_REVIEW_BUDGET_EXHAUSTIONS": "99",
        "VIBE_LOOP_REVIEWER_SESSION": "ambient-reviewer",
        "VIBE_LOOP_REVIEWER_SESSION_ATTESTATION": "runtime-bound",
        "VIBE_LOOP_WORKTREE": "/ambient/worktree",
    }

    def test_withheld_names_are_removed_from_an_inherited_environment(self) -> None:
        with mock.patch.dict(os.environ, self.AMBIENT):
            environment = build_adapter_environment({"PROJECT_SELECTOR": "configured"})

        for name in WITHHELD_ADAPTER_ENV:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PROJECT_SELECTOR"], "configured")
        # Only the withheld set is affected; unrelated inheritance is intact.
        self.assertEqual(environment["PATH"], os.environ["PATH"])

    def test_supplied_names_override_the_ambient_value(self) -> None:
        with mock.patch.dict(os.environ, self.AMBIENT):
            environment = build_adapter_environment(
                {
                    "VIBE_LOOP_REVIEWER_SESSION": "derived-reviewer",
                    "VIBE_LOOP_REVIEWER_SESSION_ATTESTATION": "agent-reported",
                }
            )

        self.assertEqual(environment["VIBE_LOOP_REVIEWER_SESSION"], "derived-reviewer")
        self.assertEqual(
            environment["VIBE_LOOP_REVIEWER_SESSION_ATTESTATION"],
            "agent-reported",
        )
        self.assertNotIn("VIBE_LOOP_IMPLEMENTER_SESSION", environment)

    def test_state_dir_and_run_identity_are_still_inherited(self) -> None:
        # Deliberately not withheld: these locate shared control state for a
        # nested `vibe-loop` call, and no contract rests on their absence.
        ambient = {
            "VIBE_LOOP_STATE_DIR": "/ambient/state",
            "VIBE_LOOP_RUN_ID": "ambient-run",
            "VIBE_LOOP_TASK_ID": "AMBIENT-01",
            "VIBE_LOOP_PRIMARY_REPO": "/ambient/primary",
            "VIBE_LOOP_LOG": "/ambient/run.log",
        }
        with mock.patch.dict(os.environ, ambient):
            environment = build_adapter_environment(None)

        for name, value in ambient.items():
            self.assertEqual(environment[name], value)

    def _report_command(self, directory: str) -> str:
        script = Path(directory) / "report_env.py"
        script.write_text(
            "import json\n"
            "import os\n"
            "names = sorted(n for n in os.environ if n.startswith('VIBE_LOOP_'))\n"
            "print(json.dumps({'names': names}))\n",
            encoding="utf-8",
        )
        return f"{shlex.quote(sys.executable)} report_env.py"

    def test_json_adapter_process_does_not_observe_withheld_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = self._report_command(directory)
            with mock.patch.dict(os.environ, self.AMBIENT):
                payload = run_json_command(
                    Path(directory),
                    command,
                    runtime_context={"VIBE_LOOP_TASK_ID": "TASK-01"},
                )

        assert isinstance(payload, dict)
        self.assertEqual(
            [name for name in payload["names"] if name in WITHHELD_ADAPTER_ENV],
            [],
        )
        self.assertIn("VIBE_LOOP_TASK_ID", payload["names"])

    def test_reset_adapter_process_does_not_observe_withheld_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observed = Path(directory) / "observed.json"
            script = Path(directory) / "reset_env.py"
            script.write_text(
                "import json\n"
                "import os\n"
                "import pathlib\n"
                "names = sorted(n for n in os.environ if n.startswith('VIBE_LOOP_'))\n"
                f"pathlib.Path({str(observed)!r}).write_text(\n"
                "    json.dumps({'names': names}), encoding='utf-8'\n"
                ")\n",
                encoding="utf-8",
            )
            command = f"{shlex.quote(sys.executable)} reset_env.py"
            with mock.patch.dict(os.environ, self.AMBIENT):
                run_reset_command(Path(directory), command)

            payload = json.loads(observed.read_text(encoding="utf-8"))

        self.assertEqual(
            [name for name in payload["names"] if name in WITHHELD_ADAPTER_ENV],
            [],
        )

    def test_an_ambient_token_does_not_become_the_redaction_target(self) -> None:
        # `run_json_command` reads its redaction token from the environment it
        # built. An inherited token would both fake a fence for the adapter and
        # redirect redaction at a generation the runtime never issued.
        token = "ambient-generation"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "leak.py"
            script.write_text(
                f"import json\nprint(json.dumps({{'echo': {token!r}}}))\n",
                encoding="utf-8",
            )
            command = f"{shlex.quote(sys.executable)} leak.py"
            with mock.patch.dict(os.environ, self.AMBIENT):
                payload = run_json_command(Path(directory), command)

        assert isinstance(payload, dict)
        self.assertEqual(payload["echo"], token)


class TaskSourceCapabilitiesDiagnosticTests(unittest.TestCase):
    FINGERPRINT = "sha256:" + "a" * 64

    @classmethod
    def identity(cls, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "adapter": "loopyard-vibe",
            "package": "loopyard",
            "package_version": "0.1.2",
            "source_fingerprint": cls.FINGERPRINT,
            "editable_install": None,
            "capabilities": ["task-source-reset:fenced-owner:v1"],
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def command(script: str) -> str:
        return shlex.join([sys.executable, "-c", script])

    def test_unconfigured_report_does_not_launch_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("vibe_loop.tasks.subprocess.Popen") as popen:
                report = tasks_module.task_source_adapter_report(
                    Path(directory),
                    TaskSourceConfig(),
                )

        popen.assert_not_called()
        self.assertEqual(
            report,
            {
                "capabilities_command_configured": False,
                "capabilities_command_redacted": False,
                "status": "not_configured",
                "reason": None,
                "identity": None,
            },
        )

    def test_valid_identity_is_bounded_to_public_fields(self) -> None:
        payload = self.identity(unknown_path="/private/source")
        command = self.command(f"import json; print(json.dumps({payload!r}))")
        config = TaskSourceConfig(capabilities_command=command)

        with tempfile.TemporaryDirectory() as directory:
            report = tasks_module.task_source_adapter_report(
                Path(directory),
                config,
            )

        self.assertEqual(report["status"], "available")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["identity"], self.identity())
        assert isinstance(report["identity"], dict)
        self.assertNotIn("unknown_path", report["identity"])
        self.assertEqual(
            report["identity"]["source_fingerprint"],
            self.FINGERPRINT,
        )
        self.assertIsNone(report["identity"]["editable_install"])

    def test_reset_capability_is_required_only_with_reset_adapter(self) -> None:
        payload = self.identity(capabilities=[])
        command = self.command(f"import json; print(json.dumps({payload!r}))")

        with tempfile.TemporaryDirectory() as directory:
            without_reset = tasks_module.task_source_adapter_report(
                Path(directory),
                TaskSourceConfig(capabilities_command=command),
            )
            with_reset = tasks_module.task_source_adapter_report(
                Path(directory),
                TaskSourceConfig(
                    capabilities_command=command,
                    reset_command="reset {task_id}",
                ),
            )

        self.assertEqual(without_reset["status"], "available")
        self.assertEqual(with_reset["status"], "deployment_gap")
        self.assertEqual(with_reset["reason"], "required_capability_missing")
        self.assertIsNone(with_reset["identity"])

    def test_malformed_json_and_documents_have_fixed_reasons(self) -> None:
        invalid_documents = (
            [],
            self.identity(schema_version=True),
            self.identity(adapter=""),
            self.identity(source_fingerprint="sha256:" + "A" * 64),
            self.identity(editable_install="yes"),
            self.identity(capabilities="reset"),
            self.identity(capabilities=["x" * 257]),
        )
        commands = [self.command("print('{')")]
        commands.extend(
            self.command(f"import json; print(json.dumps({payload!r}))")
            for payload in invalid_documents
        )

        with tempfile.TemporaryDirectory() as directory:
            reports = [
                tasks_module.task_source_adapter_report(
                    Path(directory),
                    TaskSourceConfig(capabilities_command=command),
                )
                for command in commands
            ]

        self.assertEqual(reports[0]["reason"], "invalid_json")
        self.assertTrue(
            all(report["reason"] == "invalid_document" for report in reports[1:])
        )
        self.assertTrue(all(report["status"] == "deployment_gap" for report in reports))
        self.assertTrue(all(report["identity"] is None for report in reports))

    def test_start_failure_and_nonzero_exit_do_not_surface_secret_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            start_failures = []
            for error in (OSError("private executable path"), ValueError("private")):
                with mock.patch(
                    "vibe_loop.tasks.subprocess.Popen",
                    side_effect=error,
                ):
                    start_failures.append(
                        tasks_module.task_source_adapter_report(
                            repo,
                            TaskSourceConfig(capabilities_command="private-command"),
                        )
                    )
            secret = "CAPABILITY_STDERR_SENTINEL"
            command = self.command(
                f"import sys; sys.stderr.write({secret!r} * 8192); raise SystemExit(7)"
            )
            command_failed = tasks_module.task_source_adapter_report(
                repo,
                TaskSourceConfig(capabilities_command=command),
            )

        self.assertTrue(
            all(report["reason"] == "command_start_failed" for report in start_failures)
        )
        self.assertEqual(command_failed["reason"], "command_failed")
        self.assertNotIn(secret, json.dumps(command_failed))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_stdout_overflow_terminates_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            pid_path = repo / "child.pid"
            command = self.command(
                "import os, pathlib, sys, time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "sys.stdout.write('x' * 70000); sys.stdout.flush(); time.sleep(30)"
            )
            report = tasks_module.task_source_adapter_report(
                repo,
                TaskSourceConfig(
                    capabilities_command=command,
                    command_timeout_seconds=5.0,
                ),
            )
            child_pid = int(pid_path.read_text(encoding="utf-8"))

        self.assertEqual(report["reason"], "stdout_limit_exceeded")
        self.assert_process_exited(child_pid)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_timeout_terminates_owned_process_group_and_discards_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            pid_path = repo / "child.pid"
            secret = "TIMEOUT_STDERR_SENTINEL"
            command = self.command(
                "import os, pathlib, sys, time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                f"sys.stderr.write({secret!r}); sys.stderr.flush(); time.sleep(30)"
            )
            report = tasks_module.task_source_adapter_report(
                repo,
                TaskSourceConfig(
                    capabilities_command=command,
                    command_timeout_seconds=0.1,
                ),
            )
            child_pid = int(pid_path.read_text(encoding="utf-8"))

        self.assertEqual(report["reason"], "command_timeout")
        self.assertNotIn(secret, json.dumps(report))
        self.assert_process_exited(child_pid)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process isolation")
    def test_timeout_returns_when_escaped_descendant_keeps_pipes_open(self) -> None:
        child_pid = 0
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            pid_path = repo / "escaped.pid"
            command = self.command(
                "import os, pathlib, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    os.setsid()\n"
                "    time.sleep(30)\n"
                "    os._exit(0)\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child))\n"
                "time.sleep(30)\n"
            )
            started = time.monotonic()
            try:
                report = tasks_module.task_source_adapter_report(
                    repo,
                    TaskSourceConfig(
                        capabilities_command=command,
                        command_timeout_seconds=0.1,
                    ),
                )
                elapsed = time.monotonic() - started
                child_pid = int(pid_path.read_text(encoding="utf-8"))
            finally:
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        self.assertEqual(report["reason"], "command_timeout")
        self.assertLess(elapsed, 2.0)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_sigterm_grace_reaps_leader_before_testing_process_group(self) -> None:
        process = mock.Mock(pid=12345)
        process.returncode = None
        polled = False
        sent_signals: list[int] = []

        def poll() -> int:
            nonlocal polled
            polled = True
            process.returncode = -signal.SIGTERM
            return process.returncode

        def killpg(_pid: int, sent_signal: int) -> None:
            sent_signals.append(sent_signal)
            if sent_signal == 0 and polled:
                raise ProcessLookupError

        process.poll.side_effect = poll
        with mock.patch("vibe_loop.tasks.os.killpg", side_effect=killpg):
            tasks_module._terminate_capabilities_process_group(process)

        self.assertEqual(sent_signals, [signal.SIGTERM, 0])
        process.wait.assert_called_once_with()

    def assert_process_exited(self, pid: int) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"capability diagnostic child still exists: {pid}")


if __name__ == "__main__":
    unittest.main()
