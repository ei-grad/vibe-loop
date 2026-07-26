from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_loop.cli import main
from vibe_loop.skill_deployment import (
    MANIFEST_NAME,
    SkillDeploymentError,
    deploy_skill_bundle,
    verify_skill_deployments,
    verify_worker_skill_deployments,
)


class SkillDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "source"
        self.home = self.root / "home"
        skill = self.repo / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("version one\n", encoding="utf-8")
        (skill / "script.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Skill Test")
        self.git("config", "user.email", "skill-test@example.invalid")
        self.git("add", "skills")
        self.git("commit", "-m", "Add example skill")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def deploy(self, **kwargs: object) -> list[Path]:
        return deploy_skill_bundle(
            source_root=self.repo / "skills",
            skill_names=("example",),
            home=self.home,
            **kwargs,
        )

    def test_install_writes_both_runtimes_and_records_file_provenance(self) -> None:
        installed = self.deploy(codex=True)

        codex_root = self.home / ".codex" / "skills"
        claude_root = self.home / ".claude" / "skills"
        self.assertEqual(installed, [codex_root / "example"])
        self.assertEqual(
            (codex_root / "example" / "SKILL.md").read_text(encoding="utf-8"),
            "version one\n",
        )
        self.assertEqual(
            (claude_root / "example" / "SKILL.md").read_text(encoding="utf-8"),
            "version one\n",
        )
        for target_root in (codex_root, claude_root):
            manifest = json.loads(
                (target_root / MANIFEST_NAME).read_text(encoding="utf-8")
            )
            entry = manifest["entries"]["example/SKILL.md"]
            self.assertEqual(entry["source_repo"], str(self.repo))
            self.assertEqual(entry["source_commit"], self.git("rev-parse", "HEAD"))
            self.assertEqual(entry["source_branch"], "main")
            self.assertFalse(entry["source_dirty"])
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertTrue(entry["installed_at"])

        reports = verify_skill_deployments(self.home)
        self.assertEqual(
            {entry.state for report in reports for entry in report.entries},
            {"in-sync"},
        )
        self.assertFalse(any(report.drifted for report in reports))

    def test_install_refuses_dirty_or_non_mainline_source_without_override(
        self,
    ) -> None:
        source = self.repo / "skills" / "example" / "SKILL.md"
        source.write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillDeploymentError, "source tree is dirty"):
            self.deploy()
        self.deploy(allow_unmerged=True)
        dirty_manifest = json.loads(
            (self.home / ".codex" / "skills" / MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(dirty_manifest["entries"]["example/SKILL.md"]["source_dirty"])

        self.git("restore", "skills/example/SKILL.md")
        self.git("switch", "-c", "task-branch")
        with self.assertRaisesRegex(SkillDeploymentError, "expected main"):
            self.deploy()

        self.deploy(allow_unmerged=True)
        reports = verify_skill_deployments(self.home)
        self.assertEqual(
            {entry.state for report in reports for entry in report.entries},
            {"branch-sourced"},
        )
        self.assertTrue(all(report.blocking for report in reports))

    def test_install_refuses_runtime_edit_and_force_reports_diff(self) -> None:
        self.deploy()
        installed = self.home / ".codex" / "skills" / "example" / "SKILL.md"
        installed.write_text("runtime edit\n", encoding="utf-8")

        with self.assertRaises(SkillDeploymentError) as raised:
            self.deploy()
        self.assertIn("runtime edit", "\n".join(raised.exception.diagnostics))
        self.assertEqual(installed.read_text(encoding="utf-8"), "runtime edit\n")

        diagnostics: list[str] = []

        def capture_before_overwrite(diagnostic: str) -> None:
            self.assertEqual(
                installed.read_text(encoding="utf-8"),
                "runtime edit\n",
            )
            diagnostics.append(diagnostic)

        self.deploy(force=True, report_diagnostic=capture_before_overwrite)
        self.assertTrue(diagnostics)
        self.assertEqual(installed.read_text(encoding="utf-8"), "version one\n")

    def test_verify_classifies_stale_runtime_edits_and_unmanaged_paths(self) -> None:
        self.deploy()
        source = self.repo / "skills" / "example" / "SKILL.md"
        source.write_text("version two\n", encoding="utf-8")
        stale_reports = verify_skill_deployments(self.home)
        stale_states = {
            entry.relative_path: entry.state for entry in stale_reports[0].entries
        }
        self.assertEqual(stale_states["example/SKILL.md"], "stale")

        runtime_script = self.home / ".codex" / "skills" / "example" / "script.py"
        runtime_script.write_text("VALUE = 2\n", encoding="utf-8")
        unmanaged = self.home / ".codex" / "skills" / "local-memory" / "state.md"
        unmanaged.parent.mkdir(parents=True)
        unmanaged.write_text("mutable\n", encoding="utf-8")
        edited_report = verify_skill_deployments(self.home, codex=True)[0]
        edited_states = {
            entry.relative_path: entry.state for entry in edited_report.entries
        }
        self.assertEqual(edited_states["example/script.py"], "runtime-edited")
        self.assertIn("local-memory/state.md", edited_report.unmanaged)
        self.assertTrue(edited_report.blocking)

    def test_worker_preflight_ignores_unmanaged_roots_without_a_manifest(self) -> None:
        unmanaged = self.home / ".codex" / "skills" / "local-memory" / "state.md"
        unmanaged.parent.mkdir(parents=True)
        unmanaged.write_text("mutable\n", encoding="utf-8")

        self.assertEqual(verify_worker_skill_deployments(self.home), ())

    def test_verify_skills_cli_is_read_only_and_filters_its_report(self) -> None:
        self.deploy()
        stdout = StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "verify-skills",
                    "--codex",
                    "--home",
                    str(self.home),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload), 1)
        self.assertEqual(
            payload[0]["target_root"],
            str(self.home / ".codex" / "skills"),
        )
        self.assertFalse(payload[0]["drifted"])


if __name__ == "__main__":
    unittest.main()
