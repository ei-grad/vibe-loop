from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path, PureWindowsPath
from runpy import run_path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_BUDGET_SCRIPT = REPO_ROOT / "scripts/check-doc-budgets.py"
MD_LINK_SCRIPT = REPO_ROOT / "scripts/check-md-links.py"
DOC_COMMAND_SCRIPT = REPO_ROOT / "scripts/check-doc-commands.py"
PROJECT_BINDING_LINK_SCRIPT = REPO_ROOT / "scripts/check-project-binding-doc-linkage.py"


class TemporaryGitRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "test@example.com",
            ],
            check=True,
        )

    def write(self, relative_path: str, contents: str) -> None:
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )


class DocBudgetTests(TemporaryGitRepository):
    def write_config(self, *, baseline: int) -> None:
        self.write(
            "doc-budgets.toml",
            textwrap.dedent(
                f"""\
                [defaults]
                estimate = "bytes/4"

                [[budget]]
                paths = ["README.md"]
                target = 1
                baseline = {baseline}
                deadline = "2026-10-31"

                [structure]
                warn = 4000
                hard = 12000

                [structure.baselines]
                """
            ),
        )

    def run_budget(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                os.fspath(DOC_BUDGET_SCRIPT),
                "--config",
                "doc-budgets.toml",
                *args,
            ],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_growth_shrink_deadline_and_check_immutability(self) -> None:
        self.write("README.md", "12345678")
        self.write_config(baseline=2)
        self.git("add", "README.md", "doc-budgets.toml")
        original_config = (self.repo / "doc-budgets.toml").read_bytes()

        before_deadline = self.run_budget("--today", "2026-07-27")
        self.assertEqual(before_deadline.returncode, 0, before_deadline.stderr)
        self.assertIn("is 1 over target 1", before_deadline.stderr)

        self.write("README.md", "123456789")
        growth = self.run_budget("--today", "2026-07-27")
        self.assertEqual(growth.returncode, 1)
        self.assertIn("exceeds 2 hard limit by 1", growth.stderr)

        self.write("README.md", "1234")
        shrink = self.run_budget("--today", "2026-07-27")
        self.assertEqual(shrink.returncode, 0, shrink.stderr)

        self.write("README.md", "12345678")
        after_deadline = self.run_budget("--today", "2026-11-01")
        self.assertEqual(after_deadline.returncode, 1)
        self.assertIn("exceeds 1 hard limit by 1", after_deadline.stderr)
        self.assertEqual(
            (self.repo / "doc-budgets.toml").read_bytes(),
            original_config,
        )

    def test_refresh_only_ratchets_down_and_refuses_growth(self) -> None:
        self.write("README.md", "12345678")
        self.write_config(baseline=3)
        self.git("add", "README.md", "doc-budgets.toml")

        refresh = self.run_budget(
            "--today",
            "2026-07-27",
            "--update-baselines",
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        refreshed = (self.repo / "doc-budgets.toml").read_text(encoding="utf-8")
        self.assertIn("baseline = 2", refreshed)

        self.write("README.md", "1234567890123")
        before_failed_refresh = (self.repo / "doc-budgets.toml").read_bytes()
        refused = self.run_budget(
            "--today",
            "2026-07-27",
            "--update-baselines",
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("refusing to refresh while checks fail", refused.stderr)
        self.assertEqual(
            (self.repo / "doc-budgets.toml").read_bytes(),
            before_failed_refresh,
        )

    def test_staged_check_ignores_unstaged_markdown_growth(self) -> None:
        self.write("README.md", "12345678")
        self.write_config(baseline=2)
        self.git("add", "README.md", "doc-budgets.toml")
        self.git("commit", "-m", "Initial budget")
        self.write("README.md", "123456789")
        self.write("tracked.txt", "staged change\n")
        self.git("add", "tracked.txt")

        staged = self.run_budget("--today", "2026-07-27", "--staged")

        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertIn("doc budgets: ok (0 warnings)", staged.stdout)

    def test_refresh_rewrites_and_removes_structural_baseline(self) -> None:
        self.write("README.md", "1234567890")
        self.write(
            "doc-budgets.toml",
            textwrap.dedent(
                """\
                [defaults]
                estimate = "bytes/4"

                [[budget]]
                paths = ["README.md"]
                target = 1
                baseline = 3
                deadline = "2026-10-31"

                [structure]
                warn = 4
                hard = 8

                [structure.baselines]
                "README.md" = 12
                """
            ),
        )
        self.git("add", "README.md", "doc-budgets.toml")

        lowered = self.run_budget("--today", "2026-07-27", "--update-baselines")
        self.assertEqual(lowered.returncode, 0, lowered.stderr)
        lowered_config = (self.repo / "doc-budgets.toml").read_text(encoding="utf-8")
        self.assertIn('"README.md" = 10', lowered_config)

        self.write("README.md", "12345678")
        removed = self.run_budget("--today", "2026-07-27", "--update-baselines")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        removed_config = (self.repo / "doc-budgets.toml").read_text(encoding="utf-8")
        self.assertNotIn('"README.md" =', removed_config)


class MarkdownLinkHookTests(TemporaryGitRepository):
    def setUp(self) -> None:
        super().setUp()
        self.write("docs/README.md", "[Target](target.md)\n")
        self.write("docs/target.md", "# Target\n")
        self.write(
            "markdown-links.toml",
            textwrap.dedent(
                """\
                [reachability]
                roots = ["docs/README.md"]
                exclude = []
                max_hops = 3
                """
            ),
        )
        self.git("add", ".")
        self.git("commit", "-m", "Initial documentation")

    def run_links(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.fspath(MD_LINK_SCRIPT), *args],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_deleted_markdown_uses_full_pre_commit_gate(self) -> None:
        self.git("rm", "docs/target.md")
        full_check = self.run_links()
        self.assertEqual(full_check.returncode, 1)
        self.assertIn(
            "broken link: docs/README.md -> target.md",
            full_check.stdout,
        )

        fake_bin = self.repo / "bin"
        fake_bin.mkdir()
        hook_log = self.repo / "hook.log"
        fake_make = fake_bin / "make"
        fake_make.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" > "$HOOK_LOG"\n',
            encoding="utf-8",
        )
        fake_make.chmod(0o755)
        fake_ruff = fake_bin / "ruff"
        fake_ruff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_ruff.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        environment["HOOK_LOG"] = os.fspath(hook_log)

        result = subprocess.run(
            [os.fspath(REPO_ROOT / "scripts/hooks/pre-commit")],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hook_log.read_text(encoding="utf-8"), "doc-budget\n")


class DocumentedCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = run_path(
            os.fspath(DOC_COMMAND_SCRIPT),
            run_name="test_documented_commands",
        )

    def test_extracts_inline_and_fenced_commands_without_matching_prose(self) -> None:
        references = self.checker["documented_commands"](
            Path("docs/example.md"),
            textwrap.dedent(
                """\
                Use `vibe-loop run` for one task.
                The vibe-loop lifecycle is bounded.

                ```bash
                $ vibe-loop run-next --repo .
                printf 'vibe-loop not-a-command'
                ```

                ```mermaid
                Slice: vibe-loop lifecycle
                ```
                """
            ),
        )

        self.assertEqual(
            [(reference.line, reference.command) for reference in references],
            [(1, "run"), (5, "run-next")],
        )

    def test_unknown_inline_command_is_rejected(self) -> None:
        unresolved = self.checker["unresolved_references"](
            {Path("README.md"): "Use `vibe-loop vanished` here.\n"},
            {"run", "run-next"},
        )

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].path, Path("README.md"))
        self.assertEqual(unresolved[0].line, 1)
        self.assertEqual(unresolved[0].command, "vanished")

    def test_parser_surface_contains_only_top_level_commands(self) -> None:
        from vibe_loop.cli import build_parser

        commands = self.checker["top_level_commands"](build_parser())

        self.assertIn("run", commands)
        self.assertIn("tasks", commands)
        self.assertNotIn("list", commands)

    def test_current_documentation_resolves(self) -> None:
        result = subprocess.run(
            [sys.executable, os.fspath(DOC_COMMAND_SCRIPT)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "documented command surface: ok\n")


class CodeDocLinkageTrialTests(TemporaryGitRepository):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "scripts/check-project-binding-doc-linkage.py",
            PROJECT_BINDING_LINK_SCRIPT.read_text(encoding="utf-8"),
        )
        self.write(
            "scripts/check-md-links.py",
            MD_LINK_SCRIPT.read_text(encoding="utf-8"),
        )
        self.write(
            "src/vibe_loop/config.py",
            textwrap.dedent(
                '''\
                """Project-binding resolution entry points.

                ``resolve_project_binding`` and ``require_project_binding`` implement
                docs/prd/autopilot.md#prd-aut-020.
                """

                def resolve_project_binding():
                    pass

                def require_project_binding():
                    pass
                '''
            ),
        )
        for implementation_path in (
            "src/vibe_loop/cli.py",
            "src/vibe_loop/runner.py",
            "src/vibe_loop/autopilot.py",
        ):
            self.write(implementation_path, "")
        self.write(
            "docs/prd/autopilot.md",
            textwrap.dedent(
                """\
                <a id="prd-aut-020"></a>
                ## PRD-AUT-020 Command Backend Project Binding

                Resolution: [`config.py`](../../src/vibe_loop/config.py).
                Enforcement: [`cli.py`](../../src/vibe_loop/cli.py) and
                [`runner.py`](../../src/vibe_loop/runner.py).
                Status and transport: [`autopilot.py`](../../src/vibe_loop/autopilot.py).

                Contract.

                ## PRD-AUT-021 Another Contract
                """
            ),
        )

    def run_linkage(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                os.fspath(self.repo / "scripts/check-project-binding-doc-linkage.py"),
            ],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_reciprocal_references_resolve(self) -> None:
        result = self.run_linkage()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "project_binding code/doc linkage: ok\n")

    def test_path_rendering_is_platform_independent(self) -> None:
        checker = run_path(
            os.fspath(PROJECT_BINDING_LINK_SCRIPT),
            run_name="test_project_binding_linkage",
        )

        self.assertEqual(
            checker["anchored_reference"](
                PureWindowsPath("docs/prd/autopilot.md"),
                "prd-aut-020",
            ),
            "docs/prd/autopilot.md#prd-aut-020",
        )
        self.assertEqual(
            checker["display_path"](PureWindowsPath("src/vibe_loop/config.py")),
            "src/vibe_loop/config.py",
        )

    def test_missing_code_reference_is_reported(self) -> None:
        self.write(
            "src/vibe_loop/config.py",
            textwrap.dedent(
                """\
                def resolve_project_binding():
                    pass

                def require_project_binding():
                    pass
                """
            ),
        )

        result = self.run_linkage()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "module docstring does not reference docs/prd/autopilot.md#prd-aut-020",
            result.stdout,
        )

    def test_missing_anchor_is_reported(self) -> None:
        self.write(
            "docs/prd/autopilot.md",
            "## PRD-AUT-020 Command Backend Project Binding\n",
        )

        result = self.run_linkage()

        self.assertEqual(result.returncode, 1)
        self.assertIn('missing <a id="prd-aut-020"></a>', result.stdout)

    def test_missing_reciprocal_module_link_is_reported(self) -> None:
        self.write(
            "docs/prd/autopilot.md",
            textwrap.dedent(
                """\
                <a id="prd-aut-020"></a>
                ## PRD-AUT-020 Command Backend Project Binding

                Contract without an implementation link.
                """
            ),
        )

        result = self.run_linkage()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "PRD-AUT-020 does not link to src/vibe_loop/config.py",
            result.stdout,
        )

    def test_link_before_project_binding_section_does_not_satisfy_check(self) -> None:
        self.write(
            "docs/prd/autopilot.md",
            textwrap.dedent(
                """\
                <a id="prd-aut-020"></a>
                ## PRD-AUT-019 Unrelated Contract

                Resolution: [`config.py`](../../src/vibe_loop/config.py).

                ## PRD-AUT-020 Command Backend Project Binding

                No implementation links.
                """
            ),
        )

        result = self.run_linkage()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "PRD-AUT-020 does not link to src/vibe_loop/config.py",
            result.stdout,
        )

    def test_missing_enforcement_module_link_is_reported(self) -> None:
        self.write(
            "docs/prd/autopilot.md",
            textwrap.dedent(
                """\
                <a id="prd-aut-020"></a>
                ## PRD-AUT-020 Command Backend Project Binding

                Resolution: [`config.py`](../../src/vibe_loop/config.py).
                Enforcement: [`cli.py`](../../src/vibe_loop/cli.py).
                Status: [`autopilot.py`](../../src/vibe_loop/autopilot.py).
                """
            ),
        )

        result = self.run_linkage()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "PRD-AUT-020 does not link to src/vibe_loop/runner.py",
            result.stdout,
        )


class VendorIdentityTests(unittest.TestCase):
    def test_scripts_match_pinned_sources_after_provenance_header(self) -> None:
        expected_hashes = {
            DOC_BUDGET_SCRIPT: (
                "f3016986f92e772dd500f889bc7e05e57df72e26d765f6482f8f4039e25bf459"
            ),
            MD_LINK_SCRIPT: (
                "39fb051c7cf5d2ca5349989e18ce16ecfacdca8f4558a531c6fcf7baf20f60ff"
            ),
        }
        for script, expected_hash in expected_hashes.items():
            with self.subTest(script=script.name):
                lines = script.read_bytes().splitlines(keepends=True)
                without_provenance = b"".join((lines[0], *lines[2:]))
                self.assertEqual(
                    hashlib.sha256(without_provenance).hexdigest(),
                    expected_hash,
                )
