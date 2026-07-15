from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop.config import (
    AgentResolutionError,
    AgentRoutingRule,
    GENERATED_TASK_PROFILE_CACHE_FILE,
    SUPERVISION_DEFAULT_COOLDOWN_SECONDS,
    SUPERVISION_DEFAULT_MAX_RESTARTS,
    SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS,
    VibeConfig,
    detect_agent_clis,
    load_config,
    parse_main_worktree_path,
    reject_generated_command_adapters,
    resolve_task_agent,
    resolve_task_agent_profile,
)
from vibe_loop.tasks import Task
from vibe_loop.generated_discovery import EvidenceBundle, EvidenceFile, EvidenceLimits
from vibe_loop.generated_profiles import (
    agent_name_from_config,
    validate_generated_profile,
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
        self.assertEqual(config.agent.agent_kind, "auto")
        self.assertEqual(config.agent.executable_kind, "codex")
        self.assertEqual(config.agent.prompt_dialect, "codex")
        self.assertEqual(config.agent.prompt_dialect_source, "auto:codex")
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
        self.assertEqual(config.agent.agent_kind, "auto")
        self.assertEqual(config.agent.executable_kind, "claude")
        self.assertEqual(config.agent.prompt_dialect, "claude")
        self.assertEqual(config.agent.prompt_dialect_source, "auto:claude")
        self.assertEqual(config.agent.skill_ref_prefix, "/")

    def test_codex_only_path_resolves_read_only_analysis_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(
            config.agent.analysis_command,
            "codex exec --sandbox read-only {prompt}",
        )
        self.assertEqual(config.agent.analysis_command_source, "auto:codex")
        self.assertEqual(
            config.agent.require_analysis_command(),
            "codex exec --sandbox read-only {prompt}",
        )

    def test_claude_only_path_resolves_read_only_analysis_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "claude")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(
            config.agent.analysis_command,
            "claude -p {prompt} --disallowedTools Edit Write NotebookEdit",
        )
        self.assertEqual(config.agent.analysis_command_source, "auto:claude")

    def test_explicit_analysis_command_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nanalysis_command = "reviewer --read-only {prompt}"\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.analysis_command, "reviewer --read-only {prompt}")
        self.assertEqual(config.agent.analysis_command_source, "explicit")

    def test_missing_cli_leaves_analysis_command_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertIsNone(config.agent.analysis_command)
        self.assertEqual(
            config.agent.analysis_command_source, "unresolved:no-supported-cli"
        )
        with self.assertRaisesRegex(AgentResolutionError, "install codex or claude"):
            config.agent.require_analysis_command()

    def test_agent_to_json_reports_analysis_command_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        payload = config.agent.to_json()
        self.assertTrue(payload["analysis_command_configured"])
        self.assertEqual(payload["analysis_command_source"], "auto:codex")

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
        self.assertIsNone(config.agent.prompt_dialect)
        self.assertIsNone(config.agent.skill_ref_prefix)
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
        self.assertEqual(config.agent.executable_kind, "codex")
        self.assertEqual(config.agent.prompt_dialect, "codex")
        self.assertEqual(
            config.agent.prompt_dialect_source,
            "auto:codex:codex-first",
        )
        self.assertEqual(config.agent.diagnostics(), [])

    def test_legacy_explicit_claude_commands_infer_prompt_dialect(
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
                'command = "claude -p {prompt}"\n'
                'selection_command = "claude -p {prompt}"\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command, "claude -p {prompt}")
        self.assertEqual(config.agent.command_source, "explicit")
        self.assertIsNone(config.agent.executable_kind)
        self.assertEqual(config.agent.selection_command, "claude -p {prompt}")
        self.assertEqual(config.agent.selection_command_source, "explicit")
        self.assertEqual(config.agent.prompt_dialect, "claude")
        self.assertEqual(
            config.agent.prompt_dialect_source,
            "legacy-command-inference:claude",
        )
        self.assertEqual(config.agent.skill_ref_prefix, "/")
        self.assertIn("legacy command parsing", "\n".join(config.agent.diagnostics()))

    def test_agent_kind_claude_sets_prompt_dialect_for_env_prefixed_command(
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
                'kind = "claude"\n'
                'command = "CLAUDE_HOME=.claude claude -p {prompt}"\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                config = load_config(repo)

        self.assertEqual(config.agent.command_source, "explicit")
        self.assertEqual(config.agent.agent_kind, "claude")
        self.assertIsNone(config.agent.executable_kind)
        self.assertEqual(config.agent.prompt_dialect, "claude")
        self.assertEqual(config.agent.prompt_dialect_source, "agent.kind:claude")
        self.assertEqual(config.agent.skill_ref_prefix, "/")
        self.assertEqual(config.agent.diagnostics(), [])

    def test_explicit_selection_command_reports_custom_executable_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "claude"\n'
                'command = "worker-wrapper {prompt}"\n'
                'selection_command = "selector-wrapper {prompt}"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.agent.agent_kind, "claude")
        self.assertIsNone(config.agent.executable_kind)
        self.assertEqual(config.agent.prompt_dialect, "claude")
        self.assertEqual(agent_name_from_config(config), "custom")

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
        self.assertEqual(config.agent.agent_kind, "auto")
        self.assertEqual(config.agent.prompt_dialect, "codex")
        self.assertEqual(config.agent.prompt_dialect_source, "legacy-default:codex")
        self.assertEqual(config.agent.skill_ref_prefix, "$")
        self.assertIn("legacy Codex-style", "\n".join(config.agent.diagnostics()))

    def test_custom_agent_kind_requires_prompt_dialect_or_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "custom"\ncommand = "custom-worker {prompt}"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.agent.agent_kind, "custom")
        self.assertEqual(config.agent.command_source, "explicit")
        self.assertIsNone(config.agent.prompt_dialect)
        self.assertIsNone(config.agent.skill_ref_prefix)
        self.assertIn("agent.kind is custom", "\n".join(config.agent.diagnostics()))
        with self.assertRaisesRegex(AgentResolutionError, "agent.kind is custom"):
            config.agent.require_skill_ref_prefix()

    def test_custom_agent_kind_accepts_explicit_prompt_dialect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'kind = "custom"\n'
                'command = "custom-worker {prompt}"\n'
                'prompt_dialect = "claude"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.agent.agent_kind, "custom")
        self.assertEqual(config.agent.prompt_dialect, "claude")
        self.assertEqual(
            config.agent.prompt_dialect_source,
            "explicit:agent.prompt_dialect",
        )
        self.assertEqual(config.agent.skill_ref_prefix, "/")
        self.assertNotIn(
            "worker prompt construction requires",
            "\n".join(config.agent.diagnostics()),
        )

    def test_agent_prompt_dialect_and_prefix_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nprompt_dialect = "codex"\nskill_ref_prefix = "/"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "disagree"):
                load_config(repo)

    def test_builtin_agent_kind_rejects_conflicting_prompt_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[agent]\nkind = "codex"\nprompt_dialect = "claude"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "agent.kind"):
                load_config(repo)

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

    def test_supervision_config_defaults_match_legacy_retry_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertEqual(config.supervision.max_restarts, 3)
        self.assertEqual(
            config.supervision.max_restarts,
            SUPERVISION_DEFAULT_MAX_RESTARTS,
        )
        self.assertEqual(config.supervision.cooldown_seconds, 30.0)
        self.assertEqual(
            config.supervision.cooldown_seconds,
            SUPERVISION_DEFAULT_COOLDOWN_SECONDS,
        )
        self.assertTrue(config.supervision.recover_unknown_runs)
        self.assertEqual(config.supervision.explicit_keys, frozenset())

    def test_autopilot_config_parses_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[autopilot]\n"
                "jobs = 2\n"
                "interval_seconds = 30.0\n"
                "min_ready = 2\n"
                "planning_recheck_seconds = 45.0\n"
                "require_clean_repo = false\n"
                'health_command = "scripts/health.sh"\n'
                'planning_command = "scripts/plan.sh"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.autopilot.jobs, 2)
        self.assertEqual(config.autopilot.interval_seconds, 30.0)
        self.assertEqual(config.autopilot.min_ready, 2)
        self.assertEqual(config.autopilot.planning_recheck_seconds, 45.0)
        self.assertEqual(config.autopilot.to_json()["planning_recheck_seconds"], 45.0)
        self.assertFalse(config.autopilot.require_clean_repo)
        self.assertEqual(
            config.autopilot.maintenance_command("health"), "scripts/health.sh"
        )
        self.assertEqual(
            config.autopilot.maintenance_command("planning"), "scripts/plan.sh"
        )
        self.assertIsNone(config.autopilot.maintenance_command("summary"))
        self.assertIsNone(config.autopilot.maintenance_command("troubleshoot"))

    def test_autopilot_config_defaults_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertIsNone(config.autopilot.jobs)
        self.assertIsNone(config.autopilot.interval_seconds)
        self.assertIsNone(config.autopilot.min_ready)
        self.assertTrue(config.autopilot.require_clean_repo)
        self.assertEqual(config.autopilot.planning_recheck_seconds, 60.0)
        self.assertEqual(config.autopilot.to_json()["planning_recheck_seconds"], 60.0)
        self.assertEqual(config.autopilot.explicit_keys, frozenset())

    def test_autopilot_config_rejects_invalid_values(self) -> None:
        cases = [
            ("jobs = 0\n", "autopilot.jobs"),
            ("min_ready = -1\n", "autopilot.min_ready"),
            ('interval_seconds = "soon"\n', "autopilot.interval_seconds"),
            ("planning_recheck_seconds = 0\n", "autopilot.planning_recheck_seconds"),
            ("planning_recheck_seconds = -5\n", "autopilot.planning_recheck_seconds"),
            # Below the 5s floor: a misconfigured tiny value could otherwise
            # generate ~1.8M probes across an interval.
            ("planning_recheck_seconds = 2.0\n", "at least 5.0 seconds"),
            (
                'planning_recheck_seconds = "soon"\n',
                "autopilot.planning_recheck_seconds",
            ),
            ("unsupported = true\n", "unsupported"),
        ]
        for toml, expected in cases:
            with self.subTest(toml=toml):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / ".vibe-loop.toml").write_text(
                        "[autopilot]\n" + toml,
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError) as caught:
                        load_config(repo)
                    self.assertIn(expected, str(caught.exception))

    def test_autopilot_maintenance_keys_are_forbidden_in_generated_profiles(
        self,
    ) -> None:
        from vibe_loop.config import (
            GENERATED_TASK_PROFILE_FORBIDDEN_KEYS,
            find_forbidden_generated_command_keys,
        )

        for key in (
            "health_command",
            "summary_command",
            "troubleshoot_command",
            "planning_command",
            "analysis_command",
            "autopilot",
        ):
            self.assertIn(key, GENERATED_TASK_PROFILE_FORBIDDEN_KEYS)
        forbidden = find_forbidden_generated_command_keys(
            {"profile": {"planning_command": "do bad"}}
        )
        self.assertTrue(any("planning_command" in path for path in forbidden))

    def test_supervision_config_parses_restart_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\nmax_restarts = 1\ncooldown_seconds = 0.25\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.supervision.max_restarts, 1)
        self.assertEqual(config.supervision.cooldown_seconds, 0.25)
        self.assertTrue(config.supervision.recover_unknown_runs)
        self.assertEqual(
            config.supervision.to_json()["explicit_keys"],
            ["cooldown_seconds", "max_restarts"],
        )

    def test_supervision_config_parses_recover_unknown_runs_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\nrecover_unknown_runs = false\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertFalse(config.supervision.recover_unknown_runs)
        self.assertEqual(
            config.supervision.to_json()["recover_unknown_runs"],
            False,
        )
        self.assertEqual(
            config.supervision.to_json()["explicit_keys"],
            ["recover_unknown_runs"],
        )

    def test_supervision_config_parses_resume_unknown_runs_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\nresume_unknown_runs = false\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        # Defaults on; explicit override turns it off without affecting recovery.
        self.assertFalse(config.supervision.resume_unknown_runs)
        self.assertTrue(config.supervision.recover_unknown_runs)
        self.assertEqual(
            config.supervision.to_json()["resume_unknown_runs"],
            False,
        )
        self.assertEqual(
            config.supervision.to_json()["explicit_keys"],
            ["resume_unknown_runs"],
        )

    def test_supervision_config_resume_unknown_runs_defaults_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))
        self.assertTrue(config.supervision.resume_unknown_runs)

    def test_supervision_config_limit_wall_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))
        self.assertTrue(config.supervision.limit_wall_detection)
        self.assertEqual(config.supervision.limit_wall_backoff_seconds, 1800.0)
        self.assertEqual(config.supervision.limit_wall_patterns, ())
        self.assertEqual(config.supervision.explicit_keys, frozenset())

    def test_supervision_config_parses_limit_wall_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\n"
                "limit_wall_detection = false\n"
                "limit_wall_backoff_seconds = 600\n"
                'limit_wall_patterns = ["custom wall", "another cap"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertFalse(config.supervision.limit_wall_detection)
        self.assertEqual(config.supervision.limit_wall_backoff_seconds, 600.0)
        self.assertEqual(
            config.supervision.limit_wall_patterns,
            ("custom wall", "another cap"),
        )
        self.assertEqual(
            config.supervision.to_json()["limit_wall_patterns"],
            ["custom wall", "another cap"],
        )
        self.assertEqual(
            config.supervision.to_json()["explicit_keys"],
            [
                "limit_wall_backoff_seconds",
                "limit_wall_detection",
                "limit_wall_patterns",
            ],
        )

    def test_supervision_config_worker_timeout_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))
        self.assertEqual(config.supervision.worker_timeout_seconds, 10800.0)
        self.assertEqual(
            config.supervision.worker_timeout_seconds,
            SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            config.supervision.to_json()["worker_timeout_seconds"],
            10800.0,
        )
        self.assertEqual(config.supervision.explicit_keys, frozenset())

    def test_supervision_config_parses_worker_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\nworker_timeout_seconds = 900\n",
                encoding="utf-8",
            )
            config = load_config(repo)
        self.assertEqual(config.supervision.worker_timeout_seconds, 900.0)
        self.assertIn("worker_timeout_seconds", config.supervision.explicit_keys)

    def test_supervision_config_worker_timeout_zero_is_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[supervision]\nworker_timeout_seconds = 0\n",
                encoding="utf-8",
            )
            config = load_config(repo)
        self.assertEqual(config.supervision.worker_timeout_seconds, 0.0)
        self.assertEqual(
            config.supervision.to_json()["worker_timeout_seconds"],
            0.0,
        )

    def test_supervision_config_rejects_invalid_values(self) -> None:
        cases = [
            ("max_restarts = -1\n", "supervision.max_restarts"),
            (
                "worker_timeout_seconds = -1\n",
                "supervision.worker_timeout_seconds",
            ),
            (
                'worker_timeout_seconds = "soon"\n',
                "supervision.worker_timeout_seconds",
            ),
            ('cooldown_seconds = "soon"\n', "supervision.cooldown_seconds"),
            ("cooldown_seconds = -0.1\n", "supervision.cooldown_seconds"),
            ('recover_unknown_runs = "yes"\n', "supervision.recover_unknown_runs"),
            ('resume_unknown_runs = "yes"\n', "supervision.resume_unknown_runs"),
            ('limit_wall_detection = "yes"\n', "supervision.limit_wall_detection"),
            (
                "limit_wall_backoff_seconds = -1\n",
                "supervision.limit_wall_backoff_seconds",
            ),
            ('limit_wall_patterns = "wall"\n', "supervision.limit_wall_patterns"),
            (
                'limit_wall_patterns = ["("]\n',
                "supervision.limit_wall_patterns",
            ),
            ("unsupported = true\n", "unsupported"),
        ]
        for toml, expected in cases:
            with self.subTest(toml=toml):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / ".vibe-loop.toml").write_text(
                        "[supervision]\n" + toml,
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, expected):
                        load_config(repo)

    def test_lock_config_defaults_to_directory_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertEqual(config.locks.type, "directory")
        self.assertFalse(config.locks.command_backend)
        self.assertIsNone(config.locks.lease_seconds)
        self.assertEqual(config.locks.explicit_keys, frozenset())

    def test_lock_config_parses_lease_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[locks]\nlease_seconds = 30\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.locks.lease_seconds, 30)
        self.assertEqual(config.locks.to_json()["lease_seconds"], 30)

    def test_lock_config_parses_command_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[locks]\n"
                'type = "command"\n'
                'acquire_command = "locks acquire"\n'
                'release_command = "locks release"\n'
                'status_command = "locks status"\n'
                'list_command = "locks list"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.locks.command_backend)
        self.assertEqual(config.locks.acquire_command, "locks acquire")
        self.assertEqual(config.locks.release_command, "locks release")
        self.assertEqual(config.locks.status_command, "locks status")
        self.assertEqual(config.locks.list_command, "locks list")
        self.assertEqual(
            config.locks.to_json()["explicit_keys"],
            [
                "acquire_command",
                "list_command",
                "release_command",
                "status_command",
                "type",
            ],
        )

    def test_lock_config_rejects_invalid_command_backend(self) -> None:
        cases = [
            ('type = "sqlite"\n', "locks.type"),
            ("lease_seconds = 0\n", "locks.lease_seconds"),
            ('lease_seconds = "soon"\n', "locks.lease_seconds"),
            (
                'type = "command"\n'
                'acquire_command = "locks acquire"\n'
                'release_command = "locks release"\n',
                "locks.list_command",
            ),
            ('acquire_command = "locks acquire"\n', "locks.type"),
            ('type = "directory"\nunknown = true\n', "unsupported"),
        ]
        for toml, expected in cases:
            with self.subTest(toml=toml):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / ".vibe-loop.toml").write_text(
                        "[locks]\n" + toml,
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, expected):
                        load_config(repo)

    def test_specs_config_parses_execution_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[specs]\n"
                "require_approved = true\n"
                "require_current_fingerprints = true\n"
                "require_requirement_coverage = true\n"
                "require_completion_evidence = true\n"
                'approved_states = ["approved", "accepted"]\n'
                'override_commands = ["make specs-override"]\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.specs.enforces_execution)
        self.assertTrue(config.specs.require_approved)
        self.assertTrue(config.specs.require_current_fingerprints)
        self.assertTrue(config.specs.require_requirement_coverage)
        self.assertTrue(config.specs.require_completion_evidence)
        self.assertEqual(config.specs.approved_states, ("approved", "accepted"))
        self.assertEqual(config.specs.override_commands, ("make specs-override",))
        self.assertEqual(
            config.specs.to_json()["explicit_keys"],
            [
                "approved_states",
                "override_commands",
                "require_approved",
                "require_completion_evidence",
                "require_current_fingerprints",
                "require_requirement_coverage",
            ],
        )

    def test_specs_config_rejects_invalid_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[specs]\nrequire_approved = "yes"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "specs.require_approved"):
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

    def test_task_source_respect_source_order_defaults_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text("[task_source]\n", encoding="utf-8")

            config = load_config(repo)

        self.assertFalse(config.task_source.respect_source_order)

    def test_task_source_respect_source_order_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\nrespect_source_order = true\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertTrue(config.task_source.respect_source_order)
        self.assertTrue(config.task_source.is_explicit("respect_source_order"))

    def test_task_source_respect_source_order_rejects_non_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\nrespect_source_order = "yes"\n',
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(repo)

    def test_load_config_falls_back_to_main_worktree_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_repo = root / "main"
            linked_repo = root / "linked"
            main_repo.mkdir()
            linked_repo.mkdir()
            main_config = main_repo / ".vibe-loop.toml"
            main_config.write_text(
                'state_dir = ".state/vibe-loop"\n'
                "[task_source]\n"
                'list = "python list_tasks.py"\n',
                encoding="utf-8",
            )

            with patch(
                "vibe_loop.config.main_worktree_config_path",
                return_value=main_config,
            ):
                config = load_config(linked_repo)

        self.assertEqual(config.config_source, "main_worktree")
        self.assertEqual(config.config_path, main_config.resolve())
        self.assertEqual(config.repo, linked_repo.resolve())
        self.assertEqual(config.task_source.list_command, "python list_tasks.py")
        self.assertEqual(config.state_path, linked_repo.resolve() / ".state/vibe-loop")

    def test_local_config_wins_over_main_worktree_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_repo = root / "main"
            linked_repo = root / "linked"
            main_repo.mkdir()
            linked_repo.mkdir()
            main_config = main_repo / ".vibe-loop.toml"
            linked_config = linked_repo / ".vibe-loop.toml"
            main_config.write_text(
                '[task_source]\nlist = "python main_tasks.py"\n',
                encoding="utf-8",
            )
            linked_config.write_text(
                '[task_source]\nlist = "python linked_tasks.py"\n',
                encoding="utf-8",
            )

            with patch(
                "vibe_loop.config.main_worktree_config_path",
                return_value=main_config,
            ) as fallback:
                config = load_config(linked_repo)

        fallback.assert_not_called()
        self.assertEqual(config.config_source, "repo")
        self.assertEqual(config.config_path, linked_config.resolve())
        self.assertEqual(config.task_source.list_command, "python linked_tasks.py")

    def test_parse_main_worktree_path_reads_first_porcelain_record(self) -> None:
        parsed = parse_main_worktree_path(
            "worktree /repo/main worktree\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/linked\n"
            "HEAD def456\n"
            "branch refs/heads/task\n"
        )

        self.assertEqual(parsed, Path("/repo/main worktree"))

    def test_task_source_defaults_do_not_block_generated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()

            config = load_config(repo)

        self.assertEqual(config.task_source.explicit_keys, frozenset())
        self.assertTrue(config.task_source.allows_generated_cache)
        self.assertEqual(
            config.generated_task_profile_path,
            repo / ".vibe-loop" / GENERATED_TASK_PROFILE_CACHE_FILE,
        )

    def test_generated_task_profile_path_uses_configured_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
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

    def test_task_source_reset_hook_is_parsed_and_blocks_generated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                '[task_source]\ntype = "command"\n'
                'list = "tracker list --json"\n'
                'reset = "tracker reset {task_id}"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.task_source.reset_command, "tracker reset {task_id}")
        self.assertFalse(config.task_source.allows_generated_cache)
        self.assertIn("reset", config.task_source.explicit_source_keys)
        self.assertTrue(config.task_source.is_explicit("reset"))

    def test_task_source_reset_hook_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text("[task_source]\n", encoding="utf-8")

            config = load_config(repo)

        self.assertIsNone(config.task_source.reset_command)

    def test_task_source_command_timeout_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text("[task_source]\n", encoding="utf-8")

            config = load_config(repo)

        self.assertEqual(config.task_source.command_timeout_seconds, 120.0)
        self.assertEqual(config.task_source.to_json()["command_timeout_seconds"], 120.0)

    def test_task_source_command_timeout_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\ncommand_timeout_seconds = 45.0\n",
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.task_source.command_timeout_seconds, 45.0)
        self.assertEqual(config.task_source.to_json()["command_timeout_seconds"], 45.0)
        self.assertTrue(config.task_source.is_explicit("command_timeout_seconds"))
        # A tuning knob, not a source selector: it must not block the generated
        # task-source cache the way an explicit list/type/reset command does.
        self.assertTrue(config.task_source.allows_generated_cache)
        self.assertEqual(config.task_source.explicit_source_keys, ())

    def test_task_source_command_timeout_rejects_invalid_values(self) -> None:
        cases = [
            ("command_timeout_seconds = 0\n", "task_source.command_timeout_seconds"),
            ("command_timeout_seconds = -5\n", "task_source.command_timeout_seconds"),
            # Below the 1s floor: a sub-second ceiling would kill a real backend
            # command (a loopyard CLI hitting Postgres) mid-flight.
            ("command_timeout_seconds = 0.5\n", "at least 1.0 seconds"),
            (
                'command_timeout_seconds = "soon"\n',
                "task_source.command_timeout_seconds",
            ),
        ]
        for toml, expected in cases:
            with self.subTest(toml=toml):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    (repo / ".vibe-loop.toml").write_text(
                        "[task_source]\n" + toml,
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError) as caught:
                        load_config(repo)
                    self.assertIn(expected, str(caught.exception))

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

    def test_explicit_ralphex_markdown_source_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\n"
                'type = "ralphex-markdown"\n'
                'plan_path = "docs/plans/checkout.md"\n',
                encoding="utf-8",
            )

            config = load_config(repo)

        self.assertEqual(config.task_source.type, "ralphex-markdown")
        self.assertEqual(config.task_source.plan_path, "docs/plans/checkout.md")
        self.assertFalse(config.task_source.allows_generated_cache)
        self.assertEqual(config.task_source.explicit_source_keys, ("plan_path", "type"))

    def test_generated_task_profiles_reject_command_adapters(self) -> None:
        profiles = [
            {"type": "command"},
            {"parser": {"list": "tracker list --json"}},
            {"task_source": {"probe": "tracker show {task_id} --json"}},
            {"locks": {"type": "command"}},
            {"lock_backend": {"acquire_command": "lock acquire"}},
            {"profile": {"status_command": "lock status"}},
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

    def test_generated_markdown_profile_allows_checkbox_status_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            content = "- [ ] TASK-01 Add runnable task\n"
            bundle = EvidenceBundle(
                repo=repo,
                limits=EvidenceLimits(),
                files=(
                    EvidenceFile(
                        path="TASKS.md",
                        size=len(content),
                        sha256="0" * 64,
                        mtime_ns=0,
                        content=content,
                    ),
                ),
                skipped=(),
            )

            error = validate_generated_profile(
                {
                    "kind": "markdown_list",
                    "source_paths": ["TASKS.md"],
                    "stable_ids": True,
                    "fields": {
                        "id": {
                            "pattern": r"^(?P<id>TASK-\d+)\b",
                            "strategy": "heading_text",
                        },
                        "title": {
                            "pattern": r"^TASK-\d+\s+(?P<title>.+)$",
                            "strategy": "heading_text",
                        },
                        "status": {"strategy": "checkbox_status"},
                    },
                    "status_map": {
                        "done": ["Done"],
                        "runnable": ["Planned"],
                    },
                },
                bundle,
            )

        self.assertIsNone(error)

    def test_generated_markdown_profile_rejects_checkbox_status_for_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            content = "- [ ] TASK-01 Add runnable task\n"
            bundle = EvidenceBundle(
                repo=repo,
                limits=EvidenceLimits(),
                files=(
                    EvidenceFile(
                        path="TASKS.md",
                        size=len(content),
                        sha256="0" * 64,
                        mtime_ns=0,
                        content=content,
                    ),
                ),
                skipped=(),
            )

            error = validate_generated_profile(
                {
                    "kind": "markdown_list",
                    "source_paths": ["TASKS.md"],
                    "stable_ids": True,
                    "fields": {
                        "id": {"strategy": "checkbox_status"},
                        "title": {
                            "pattern": r"^TASK-\d+\s+(?P<title>.+)$",
                            "strategy": "heading_text",
                        },
                        "status": {"strategy": "checkbox_status"},
                    },
                    "status_map": {
                        "done": ["Done"],
                        "runnable": ["Planned"],
                    },
                },
                bundle,
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error[0], "invalid_field_mapping_value")
        self.assertIn("requires the status field", error[1])

    def test_generated_markdown_profile_rejects_checkbox_status_for_tables(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            content = (
                "| ID | Task | Status |\n"
                "| --- | --- | --- |\n"
                "| TASK-01 | Add runnable task | Todo |\n"
            )
            bundle = EvidenceBundle(
                repo=repo,
                limits=EvidenceLimits(),
                files=(
                    EvidenceFile(
                        path="TASKS.md",
                        size=len(content),
                        sha256="0" * 64,
                        mtime_ns=0,
                        content=content,
                    ),
                ),
                skipped=(),
            )

            error = validate_generated_profile(
                {
                    "kind": "markdown_table",
                    "source_paths": ["TASKS.md"],
                    "stable_ids": True,
                    "fields": {
                        "id": {"column": "ID"},
                        "title": {"column": "Task"},
                        "status": {
                            "column": "Status",
                            "strategy": "checkbox_status",
                        },
                    },
                    "status_map": {
                        "done": ["Done"],
                        "runnable": ["Planned"],
                    },
                },
                bundle,
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error[0], "invalid_field_mapping_value")
        self.assertIn("requires markdown_list", error[1])

    def test_task_source_plan_paths_rejects_non_string_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[task_source]\nplan_paths = [123]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "task_source.plan_paths"):
                load_config(repo)


class AgentProfileRoutingTests(unittest.TestCase):
    def _load_with_both_clis(self, config_text: str) -> VibeConfig:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            bin_dir = Path(directory) / "bin"
            repo.mkdir()
            bin_dir.mkdir()
            write_executable(bin_dir / "codex")
            write_executable(bin_dir / "claude")
            (repo / ".vibe-loop.toml").write_text(config_text, encoding="utf-8")
            with patch.dict("os.environ", {"PATH": str(bin_dir)}):
                return load_config(repo)

    def test_absent_profiles_and_routing_leave_defaults_empty(self) -> None:
        config = self._load_with_both_clis('[agent]\nkind = "codex"\n')
        self.assertEqual(config.agent_profiles, {})
        self.assertEqual(config.agent_routing, ())

    def test_profile_resolves_command_through_kind_defaults(self) -> None:
        config = self._load_with_both_clis(
            '[agent]\nkind = "codex"\n\n[agent.profiles.opus]\nkind = "claude"\n'
        )
        opus = config.agent_profiles["opus"]
        self.assertEqual(opus.command, "claude -p {prompt}")
        self.assertEqual(opus.agent_kind, "claude")
        self.assertEqual(opus.skill_ref_prefix, "/")
        # The default [agent] is untouched by the profile.
        self.assertEqual(config.agent.command, "codex exec {prompt}")
        self.assertEqual(config.agent.agent_kind, "codex")

    def test_profile_command_override_is_authoritative(self) -> None:
        config = self._load_with_both_clis(
            "[agent.profiles.opus]\n"
            'kind = "claude"\n'
            'command = "claude -p {prompt} --model opus"\n'
        )
        self.assertEqual(
            config.agent_profiles["opus"].command,
            "claude -p {prompt} --model opus",
        )

    def test_routing_rule_parses_predicates(self) -> None:
        config = self._load_with_both_clis(
            "[agent.profiles.opus]\n"
            'kind = "claude"\n\n'
            "[[agent.routing]]\n"
            'profile = "opus"\n'
            'match_hazards_any = ["abi", "dma"]\n'
            'match_paths_glob = ["kernel/**"]\n'
        )
        self.assertEqual(len(config.agent_routing), 1)
        rule = config.agent_routing[0]
        self.assertEqual(rule.profile, "opus")
        self.assertEqual(rule.match_hazards_any, ("abi", "dma"))
        self.assertEqual(rule.match_paths_glob, ("kernel/**",))

    def test_routing_rule_referencing_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not defined in \\[agent.profiles\\]"):
            self._load_with_both_clis('[[agent.routing]]\nprofile = "missing"\n')

    def test_routing_rule_rejects_unknown_predicate_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported keys: match_hazard"):
            self._load_with_both_clis(
                "[agent.profiles.opus]\n"
                'kind = "claude"\n\n'
                "[[agent.routing]]\n"
                'profile = "opus"\n'
                'match_hazard = ["abi"]\n'
            )

    def test_routing_rule_rejects_invalid_regex(self) -> None:
        with self.assertRaisesRegex(ValueError, "match_task_id_regex is not a valid"):
            self._load_with_both_clis(
                "[agent.profiles.opus]\n"
                'kind = "claude"\n\n'
                "[[agent.routing]]\n"
                'profile = "opus"\n'
                'match_task_id_regex = "([unclosed"\n'
            )

    def test_profile_parse_error_is_labeled(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent.profiles.bad: agent.kind"):
            self._load_with_both_clis('[agent.profiles.bad]\nkind = "nope"\n')

    def test_resolve_profile_explicit_field_wins_over_routing(self) -> None:
        routing = (AgentRoutingRule(profile="routed", match_hazards_any=("abi",)),)
        task = Task(
            task_id="T1", title="t", status="Next", agent="explicit", hazards=("abi",)
        )
        self.assertEqual(
            resolve_task_agent_profile(task, routing), ("explicit", "task.agent")
        )

    def test_resolve_profile_first_matching_rule_wins(self) -> None:
        routing = (
            AgentRoutingRule(profile="first", match_hazards_any=("abi",)),
            AgentRoutingRule(profile="second", match_hazards_any=("abi",)),
        )
        task = Task(task_id="T1", title="t", status="Next", hazards=("abi",))
        name, source = resolve_task_agent_profile(task, routing)
        self.assertEqual(name, "first")
        self.assertEqual(source, "agent.routing[0]")

    def test_resolve_profile_hazard_and_path_predicates(self) -> None:
        routing = (
            AgentRoutingRule(profile="sec", match_hazards_any=("abi",)),
            AgentRoutingRule(profile="paths", match_paths_glob=("kernel/*.rs",)),
        )
        hazard_task = Task(task_id="H", title="t", status="Next", hazards=("abi",))
        path_task = Task(
            task_id="P", title="t", status="Next", paths=("kernel/mem.rs",)
        )
        plain_task = Task(task_id="N", title="t", status="Next")
        self.assertEqual(resolve_task_agent_profile(hazard_task, routing)[0], "sec")
        self.assertEqual(resolve_task_agent_profile(path_task, routing)[0], "paths")
        self.assertEqual(
            resolve_task_agent_profile(plain_task, routing), ("", "default")
        )

    def test_resolve_profile_all_predicates_must_match(self) -> None:
        routing = (
            AgentRoutingRule(
                profile="strict",
                match_hazards_any=("abi",),
                match_priority="P0",
            ),
        )
        # hazard matches but priority does not -> no match (AND within a rule).
        task = Task(
            task_id="T", title="t", status="Next", hazards=("abi",), priority="P1"
        )
        self.assertEqual(resolve_task_agent_profile(task, routing), ("", "default"))

    def test_resolve_task_agent_returns_profile_config(self) -> None:
        config = self._load_with_both_clis(
            '[agent]\nkind = "codex"\n\n'
            "[agent.profiles.opus]\n"
            'kind = "claude"\n\n'
            "[[agent.routing]]\n"
            'profile = "opus"\n'
            'match_hazards_any = ["abi"]\n'
        )
        routed = Task(task_id="T", title="t", status="Next", hazards=("abi",))
        default = Task(task_id="D", title="t", status="Next")
        routed_selection = resolve_task_agent(config, routed)
        self.assertEqual(routed_selection.profile, "opus")
        self.assertEqual(routed_selection.config.command, "claude -p {prompt}")
        default_selection = resolve_task_agent(config, default)
        self.assertEqual(default_selection.profile, "")
        self.assertEqual(default_selection.config.command, "codex exec {prompt}")

    def test_resolve_task_agent_unknown_profile_fails_closed(self) -> None:
        config = VibeConfig(repo=Path("."))
        task = Task(task_id="T", title="t", status="Next", agent="ghost")
        with self.assertRaisesRegex(AgentResolutionError, "'ghost'"):
            resolve_task_agent(config, task)


def write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    if sys.platform == "win32":
        cmd = path.with_name(path.name + ".cmd")
        cmd.write_text("@exit /b 0\r\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
