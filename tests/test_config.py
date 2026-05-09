from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.config import (
    AgentResolutionError,
    GENERATED_TASK_PROFILE_CACHE_FILE,
    PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY,
    detect_agent_clis,
    load_config,
    planning_analytics_report,
    reject_generated_command_adapters,
)


class ConfigTests(unittest.TestCase):
    def test_detect_agent_clis_reports_supported_binaries_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            write_executable(bin_dir / "codex")

            detected = detect_agent_clis(path=str(bin_dir))

        self.assertEqual(detected.available, ("codex",))
        self.assertTrue(detected.to_json()["codex"]["available"])
        self.assertFalse(detected.to_json()["claude"]["available"])

    def test_codex_only_path_resolves_default_agent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "codex exec {prompt}")
        self.assertEqual(config.agent.command_source, "auto:codex")
        self.assertEqual(config.agent.selection_command, "codex exec {prompt}")
        self.assertEqual(config.agent.selection_command_source, "auto:codex")
        self.assertEqual(config.agent.resolved_cli, "codex")
        self.assertEqual(config.agent.skill_ref_prefix, "$")

    def test_claude_only_path_resolves_default_agent_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "claude")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "claude -p {prompt}")
        self.assertEqual(config.agent.command_source, "auto:claude")
        self.assertEqual(config.agent.selection_command, "claude -p {prompt}")
        self.assertEqual(config.agent.selection_command_source, "auto:claude")
        self.assertEqual(config.agent.resolved_cli, "claude")
        self.assertEqual(config.agent.skill_ref_prefix, "/")

    def test_missing_agent_cli_leaves_defaults_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertIsNone(config.agent.command)
        self.assertEqual(config.agent.command_source, "unresolved:no-supported-cli")
        with self.assertRaisesRegex(AgentResolutionError, "install codex or claude"):
            config.agent.require_command()

    def test_both_agent_clis_resolve_to_codex_first_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")
            write_executable(bin_dir / "claude")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "codex exec {prompt}")
        self.assertEqual(
            config.agent.command_source,
            "auto:codex:codex-first",
        )
        self.assertEqual(config.agent.selection_command, "codex exec {prompt}")
        self.assertEqual(
            config.agent.selection_command_source,
            "auto:codex:codex-first",
        )
        self.assertEqual(config.agent.resolved_cli, "codex")
        self.assertEqual(config.agent.diagnostics(), [])

    def test_explicit_claude_commands_remain_authoritative_with_both_clis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")
            write_executable(bin_dir / "claude")
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                "command = \"claude -p '$vibe-loop {task_id}'\"\n"
                'selection_command = "claude -p {prompt}"\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "claude -p '$vibe-loop {task_id}'")
        self.assertEqual(config.agent.command_source, "explicit")
        self.assertIsNone(config.agent.resolved_cli)
        self.assertEqual(config.agent.selection_command, "claude -p {prompt}")
        self.assertEqual(config.agent.selection_command_source, "explicit")

    def test_explicit_agent_commands_remain_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'command = "custom-worker {task_id}"\n'
                'selection_command = "custom-selector {prompt}"\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "custom-worker {task_id}")
        self.assertEqual(config.agent.command_source, "explicit")
        self.assertEqual(config.agent.selection_command, "custom-selector {prompt}")
        self.assertEqual(config.agent.selection_command_source, "explicit")

    def test_agent_forward_stderr_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertFalse(config.agent.forward_stderr)

    def test_agent_forward_stderr_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\nforward_stderr = true\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.agent.forward_stderr)

    def test_agent_forward_stderr_rejects_non_bool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nforward_stderr = "yes"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "agent.forward_stderr"):
                load_config(repo)

    def test_task_source_plan_paths_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_paths = ["WORK.md", "docs/BACKLOG.md"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.task_source.plan_paths, ("WORK.md", "docs/BACKLOG.md"))

    def test_task_source_defaults_do_not_block_generated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            config = load_config(repo)

        self.assertEqual(config.task_source.explicit_keys, frozenset())
        self.assertTrue(config.task_source.allows_generated_cache)
        self.assertEqual(
            config.generated_task_profile_path,
            repo / ".vibe-loop" / GENERATED_TASK_PROFILE_CACHE_FILE,
        )

    def test_generated_task_profile_path_uses_configured_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                'state_dir = ".state/vibe-loop"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(
            config.generated_task_profile_path,
            repo / ".state" / "vibe-loop" / GENERATED_TASK_PROFILE_CACHE_FILE,
        )

    def test_explicit_plan_path_overrides_generated_cache_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nplan_path = "WORK.md"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertFalse(config.task_source.allows_generated_cache)
        self.assertEqual(config.task_source.explicit_source_keys, ("plan_path",))
        self.assertTrue(config.task_source.is_explicit("plan_path"))

    def test_explicit_command_adapter_overrides_generated_cache_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\ntype = "command"\nlist = "tracker list --json"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertFalse(config.task_source.allows_generated_cache)
        self.assertEqual(config.task_source.explicit_source_keys, ("list", "type"))
        self.assertEqual(config.task_source.list_command, "tracker list --json")

    def test_explicit_statuses_override_generated_without_blocking_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nrunnable_statuses = ["Todo", "Doing"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.task_source.allows_generated_cache)
        self.assertTrue(config.task_source.is_explicit("runnable_statuses"))
        self.assertEqual(config.task_source.runnable_statuses, ("Todo", "Doing"))

    def test_explicit_task_source_profile_supplies_runnable_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source.profile]\n"
                'kind = "markdown_table"\n'
                'source_paths = ["WORK.md"]\n'
                "stable_ids = true\n\n"
                "[task_source.profile.fields.id]\n"
                'column = "Key"\n\n'
                "[task_source.profile.fields.title]\n"
                'column = "Summary"\n\n'
                "[task_source.profile.fields.status]\n"
                'column = "State"\n\n'
                "[task_source.profile.status_map]\n"
                'done = ["Closed"]\n'
                'runnable = ["Todo", "Doing"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertFalse(config.task_source.allows_generated_cache)
        self.assertEqual(config.task_source.explicit_source_keys, ("profile",))
        self.assertEqual(config.task_source.runnable_statuses, ("Todo", "Doing"))

    def test_generated_task_profiles_reject_command_adapters(self) -> None:
        profiles = [
            {"type": "command"},
            {"parser": {"list": "tracker list --json"}},
            {"task_source": {"probe": "tracker show {task_id} --json"}},
        ]
        for profile in profiles:
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "executable command adapters"):
                    reject_generated_command_adapters(profile)

    def test_generated_task_profile_envelope_allows_redacted_agent_metadata(
        self,
    ) -> None:
        reject_generated_command_adapters(
            {
                "schema_version": 1,
                "prompt_version": 1,
                "status": "profile",
                "agent": {
                    "name": "codex",
                    "selection_command_source": "explicit",
                },
                "profile": {
                    "kind": "markdown_table",
                    "source_paths": ["PLAN.md"],
                    "fields": {
                        "id": {"column": "ID"},
                        "status": {"column": "Status"},
                    },
                },
            }
        )

    def test_generated_task_profile_envelope_rejects_command_fields(
        self,
    ) -> None:
        envelopes = [
            {"list": "tracker list --json"},
            {"agent": {"command": "INLINE_VAR=value codex exec {prompt}"}},
            {"profile": {"list": "tracker list --json"}},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                with self.assertRaisesRegex(ValueError, "executable command adapters"):
                    reject_generated_command_adapters(
                        {
                            "schema_version": 1,
                            "prompt_version": 1,
                            "status": "profile",
                            "agent": {
                                "name": "codex",
                                "selection_command_source": "explicit",
                            },
                            "profile": {
                                "kind": "markdown_table",
                                "source_paths": ["PLAN.md"],
                            },
                            **envelope,
                        }
                    )

    def test_task_source_plan_paths_rejects_non_string_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\nplan_paths = [123]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "task_source.plan_paths"):
                load_config(repo)

    def test_planning_analytics_defaults_use_state_dir_without_mutating_docs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            docs = repo / "docs"
            docs.mkdir()
            plan = docs / "PLAN.md"
            plan.write_text("# Plan\n", encoding="utf-8")

            config = load_config(repo)
            report = planning_analytics_report(config)
            state_dir_exists = (repo / ".vibe-loop").exists()
            plan_text = plan.read_text(encoding="utf-8")

        self.assertEqual(
            config.planning_analytics.schedule_policy,
            PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY,
        )
        self.assertEqual(report["schedule_policy_source"], "default")
        self.assertFalse(report["repo_artifact_outputs_enabled"])
        self.assertEqual(
            report["outputs"]["timeline_json"]["path"],
            str(repo / ".vibe-loop" / "planning-analytics" / "timeline.json"),
        )
        self.assertEqual(
            report["outputs"]["timeline_json"]["source"],
            "default_state_dir",
        )
        self.assertFalse(state_dir_exists)
        self.assertEqual(plan_text, "# Plan\n")

    def test_planning_analytics_explicit_artifact_paths_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics.outputs]\n"
                'timeline_json = "docs/planning/timeline.json"\n'
                'gantt_html = "docs/planning/gantt.html"\n',
                encoding="utf-8",
            )

            config = load_config(repo)
            report = planning_analytics_report(config)

        self.assertTrue(report["repo_artifact_outputs_enabled"])
        self.assertEqual(
            report["outputs"]["timeline_json"],
            {
                "path": str(repo / "docs" / "planning" / "timeline.json"),
                "source": "explicit",
            },
        )
        self.assertEqual(
            report["outputs"]["gantt_html"],
            {
                "path": str(repo / "docs" / "planning" / "gantt.html"),
                "source": "explicit",
            },
        )
        self.assertEqual(
            report["outputs"]["benchmark_json"]["source"],
            "default_state_dir",
        )

    def test_planning_analytics_schedule_policy_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[planning_analytics]\nschedule_policy = "lightmetrics-parity"\n',
                encoding="utf-8",
            )

            config = load_config(repo)
            report = planning_analytics_report(config)

        self.assertEqual(
            config.planning_analytics.schedule_policy, "lightmetrics-parity"
        )
        self.assertEqual(report["schedule_policy"], "lightmetrics-parity")
        self.assertEqual(report["schedule_policy_source"], "explicit")

    def test_planning_analytics_duration_model_config_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics.duration_model]\n"
                "group_min_sample_count = 3\n"
                "similarity_min_score = 0.5\n"
                "similarity_max_examples = 2\n"
                "similarity_blend_weight = 0.1\n"
                "fallback_minutes = 90\n",
                encoding="utf-8",
            )

            config = load_config(repo)
            report = planning_analytics_report(config)

        self.assertEqual(
            report["duration_model"],
            {
                "name": "robust-duration-baseline-v1",
                "parameters": {
                    "group_min_sample_count": 3,
                    "similarity_min_score": 0.5,
                    "similarity_max_examples": 2,
                    "similarity_blend_weight": 0.1,
                    "fallback_minutes": 90,
                },
            },
        )

    def test_planning_analytics_rejects_invalid_duration_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics.duration_model]\nsimilarity_blend_weight = 2.0\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "similarity_blend_weight"):
                load_config(repo)

    def test_planning_analytics_rejects_non_finite_duration_model_float(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[planning_analytics.duration_model]\nsimilarity_min_score = nan\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "similarity_min_score"):
                load_config(repo)

    def test_planning_analytics_rejects_paths_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[planning_analytics.outputs]\ntimeline_json = "../timeline.json"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "repo-relative path"):
                load_config(repo)


def write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
