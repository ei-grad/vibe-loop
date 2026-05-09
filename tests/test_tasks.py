from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe_loop.config import TaskSourceConfig
from vibe_loop.tasks import (
    MarkdownPlanSource,
    MarkdownProfileSource,
    build_task_source,
    runnable_tasks,
    task_from_mapping,
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


class MarkdownPlanTests(unittest.TestCase):
    def test_runnable_tasks_filter_dependencies_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PLAN.md"
            path.write_text(PLAN, encoding="utf-8")
            source = MarkdownPlanSource(path, ("Active", "Next", "Planned"))

            tasks = runnable_tasks(source, ("Active", "Next", "Planned"))
            done_included = runnable_tasks(source, ("Done", "Next", "Planned"))

        self.assertEqual([task.task_id for task in tasks], ["DEMO-02", "DEMO-04"])
        self.assertNotIn("DEMO-01", [task.task_id for task in done_included])

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

            self.assertEqual(
                source.list_tasks()[0].source, f"{repo}/planning/backlog.md:Demo"
            )

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


if __name__ == "__main__":
    unittest.main()
