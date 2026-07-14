from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_loop.config import TaskSourceConfig
from vibe_loop.tasks import (
    CommandTaskSource,
    MarkdownPlanSource,
    MarkdownProfileSource,
    Task,
    build_task_source,
    runnable_tasks,
    task_from_mapping,
    task_sort_key,
)
from vibe_loop.task_views import build_task_views, render_task_tree


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


class MarkdownPlanTests(unittest.TestCase):
    def test_task_json_omits_empty_traceability_fields(self) -> None:
        payload = Task("TASK-01", "Plain task", "Next").to_json()

        self.assertNotIn("requirement_ids", payload)
        self.assertNotIn("spec_paths", payload)
        self.assertNotIn("design_refs", payload)
        self.assertNotIn("approval_state", payload)
        self.assertNotIn("source_fingerprints", payload)

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

    def test_plan_tasks_include_section_for_tree_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))
            views = build_task_views(source.list_tasks(), locked_ids=set())

            output = render_task_tree(views)

        self.assertIn("Demo", output)
        self.assertIn("DEMO-02 [Next/P1] Ready task", output)

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


if __name__ == "__main__":
    unittest.main()
