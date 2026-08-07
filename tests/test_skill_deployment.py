from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from vibe_loop import skill_deployment
from vibe_loop.cli import main
from vibe_loop.skill_deployment import (
    MANIFEST_NAME,
    SkillDeploymentError,
    deployment_drift_advisories,
    deploy_skill_bundle,
    verify_skill_deployments,
    verify_target_root,
    verify_worker_skill_deployments,
    worker_launch_verdict,
)
from vibe_loop.skills import (
    install_skills,
    refresh_stale_skill_deployments,
    repository_supplies_bundle,
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

    @contextmanager
    def bundled_source(self):
        """Make this fixture's repository the running `vibe_loop` skill bundle."""
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch(
                    "vibe_loop.skills.importlib.resources.files",
                    return_value=self.repo,
                )
            )
            stack.enter_context(
                mock.patch("vibe_loop.skills.SKILL_NAMES", ("example",))
            )
            yield

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

    def test_installed_skill_candidate_checkout_uses_current_contracts(
        self,
    ) -> None:
        candidate_home = self.root / "candidate-home"
        installed = install_skills(
            True,
            False,
            candidate_home,
            allow_unmerged=True,
        )
        source_root = (
            Path(__file__).resolve().parents[1] / "src" / "vibe_loop" / "skills"
        )
        expected_phrases = {
            "vibe-loop": (
                "using its forced-refresh option",
                "Never hand-edit the cache",
            ),
            "orchestrated-vibe-loop": (
                "Non-Negotiable Delegation Gates",
                "Candidate, Remediation, And Completion Sequence",
                "targeted closure review",
            ),
        }

        self.assertIn(
            candidate_home / ".codex" / "skills" / "vibe-loop",
            installed,
        )
        manifest = json.loads(
            (candidate_home / ".codex" / "skills" / MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        for skill_name, phrases in expected_phrases.items():
            source = source_root / skill_name / "SKILL.md"
            deployed = candidate_home / ".codex" / "skills" / skill_name / "SKILL.md"
            source_bytes = source.read_bytes()
            self.assertEqual(deployed.read_bytes(), source_bytes)
            self.assertEqual(
                manifest["entries"][f"{skill_name}/SKILL.md"]["sha256"],
                hashlib.sha256(source_bytes).hexdigest(),
            )
            content = deployed.read_text(encoding="utf-8")
            for phrase in phrases:
                self.assertIn(phrase, content)

    def test_non_git_package_source_uses_immutable_release_provenance(self) -> None:
        package_source = self.root / "installed-package" / "skills"
        skill = package_source / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("packaged\n", encoding="utf-8")
        package = mock.Mock(version="9.8.7")
        package.read_text.return_value = None

        with (
            mock.patch(
                "vibe_loop.skill_deployment.metadata_distribution",
                return_value=package,
            ),
            mock.patch(
                "vibe_loop.skills.importlib.resources.files",
                return_value=package_source.parent,
            ),
            mock.patch("vibe_loop.skills.SKILL_NAMES", ("example",)),
        ):
            install_skills(False, False, self.home)

        manifest = json.loads(
            (self.home / ".codex" / "skills" / MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        entry = manifest["entries"]["example/SKILL.md"]
        self.assertEqual(
            entry["source_repo"],
            "https://github.com/ei-grad/vibe-loop",
        )
        self.assertEqual(entry["source_commit"], "package:vibe-loop@9.8.7")
        self.assertEqual(entry["source_branch"], "main")
        self.assertFalse(entry["source_dirty"])
        self.assertEqual(entry["source_location"], str(package_source))
        self.assertFalse(
            any(report.drifted for report in verify_skill_deployments(self.home))
        )

    def test_non_git_vcs_package_preserves_non_mainline_revision_guard(self) -> None:
        package_source = self.root / "vcs-package" / "skills"
        skill = package_source / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("packaged\n", encoding="utf-8")
        package = mock.Mock(version="1.0.0")
        package.read_text.return_value = json.dumps(
            {
                "url": "https://example.invalid/example-package.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "a" * 40,
                    "requested_revision": "task-branch",
                },
            }
        )

        with mock.patch(
            "vibe_loop.skill_deployment.metadata_distribution",
            return_value=package,
        ):
            with self.assertRaisesRegex(SkillDeploymentError, "expected main"):
                deploy_skill_bundle(
                    source_root=package_source,
                    skill_names=("example",),
                    home=self.home,
                    package_name="example-package",
                )
            deploy_skill_bundle(
                source_root=package_source,
                skill_names=("example",),
                home=self.home,
                package_name="example-package",
                allow_unmerged=True,
            )

        reports = verify_skill_deployments(self.home)
        self.assertEqual(
            {entry.state for report in reports for entry in report.entries},
            {"branch-sourced"},
        )

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

    def test_force_replaces_invalid_manifest_after_reporting_error(self) -> None:
        self.deploy()
        codex_manifest = self.home / ".codex" / "skills" / MANIFEST_NAME
        codex_manifest.write_text(
            json.dumps({"version": 2, "entries": {}}),
            encoding="utf-8",
        )

        with self.assertRaises(SkillDeploymentError) as raised:
            self.deploy()
        self.assertIn("unsupported format", "\n".join(raised.exception.diagnostics))

        diagnostics: list[str] = []
        self.deploy(force=True, report_diagnostic=diagnostics.append)
        self.assertIn("unsupported format", "\n".join(diagnostics))
        reports = verify_skill_deployments(self.home)
        self.assertFalse(any(report.blocking for report in reports))

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

        runtime_script.write_text("VALUE = 1\n", encoding="utf-8")
        source.write_text("version one\n", encoding="utf-8")
        unmanaged_only = verify_skill_deployments(self.home, codex=True)[0]
        self.assertIn("local-memory/state.md", unmanaged_only.unmanaged)
        self.assertFalse(unmanaged_only.drifted)
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["verify-skills", "--codex", "--home", str(self.home)])
        self.assertEqual(exit_code, 0)
        self.assertIn("local-memory/state.md: unmanaged", stdout.getvalue())

    def test_worker_preflight_ignores_unmanaged_roots_without_a_manifest(self) -> None:
        unmanaged = self.home / ".codex" / "skills" / "local-memory" / "state.md"
        unmanaged.parent.mkdir(parents=True)
        unmanaged.write_text("mutable\n", encoding="utf-8")

        self.assertEqual(verify_worker_skill_deployments(self.home), ())

    def test_worker_launch_advises_stale_and_blocks_unknown_provenance(self) -> None:
        self.deploy()
        source = self.repo / "skills" / "example" / "SKILL.md"
        source.write_text("version two\n", encoding="utf-8")

        stale_reports = verify_worker_skill_deployments(self.home)
        blocking, advisories = worker_launch_verdict(stale_reports)

        # `verify-skills` keeps calling a lagging deployment blocking drift; only
        # worker launch treats it as advisory.
        self.assertTrue(all(report.blocking for report in stale_reports))
        self.assertFalse(any(report.worker_blocking for report in stale_reports))
        self.assertEqual(blocking, ())
        self.assertEqual(
            set(advisories),
            {
                f"{self.home / runtime / 'skills' / 'example' / 'SKILL.md'}: "
                "stale: source changed"
                for runtime in (".codex", ".claude")
            },
        )

        installed = self.home / ".codex" / "skills" / "example" / "script.py"
        installed.write_text("VALUE = 2\n", encoding="utf-8")
        edited_blocking, _ = worker_launch_verdict(
            verify_worker_skill_deployments(self.home)
        )
        self.assertEqual(
            [report.target_root for report in edited_blocking],
            [self.home / ".codex" / "skills"],
        )

    def test_refresh_reinstalls_a_stale_deployment_from_this_repository(self) -> None:
        with self.bundled_source():
            install_skills(False, False, self.home)
            source = self.repo / "skills" / "example" / "SKILL.md"
            source.write_text("version two\n", encoding="utf-8")
            self.git("commit", "--all", "-m", "Edit the bundled skill")

            refreshed = refresh_stale_skill_deployments(
                self.home,
                source_repo=self.repo,
            )

        self.assertEqual(
            set(refreshed),
            {
                self.home / runtime / "skills" / "example"
                for runtime in (".codex", ".claude")
            },
        )
        for runtime in (".codex", ".claude"):
            self.assertEqual(
                (self.home / runtime / "skills" / "example" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
                "version two\n",
            )
        reports = verify_worker_skill_deployments(self.home)
        self.assertTrue(reports)
        self.assertFalse(any(report.drifted for report in reports))

    def test_refresh_skips_an_in_sync_host_and_a_foreign_bundle(self) -> None:
        with self.bundled_source():
            install_skills(False, False, self.home)

            self.assertEqual(
                refresh_stale_skill_deployments(self.home, source_repo=self.repo),
                (),
            )

            source = self.repo / "skills" / "example" / "SKILL.md"
            source.write_text("version two\n", encoding="utf-8")
            self.git("commit", "--all", "-m", "Edit the bundled skill")
            # A repository that does not supply the running bundle must not
            # rewrite the host's deployment.
            self.assertEqual(
                refresh_stale_skill_deployments(
                    self.home,
                    source_repo=self.root / "other-repo",
                ),
                (),
            )
        self.assertEqual(
            (self.home / ".codex" / "skills" / "example" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            "version one\n",
        )

    def test_refresh_leaves_a_runtime_without_a_recorded_deployment_alone(self) -> None:
        with self.bundled_source():
            install_skills(False, False, self.home)
            # An operator who uses only one runtime: the other root carries no
            # recorded deployment and must not acquire one from a refresh.
            shutil.rmtree(self.home / ".codex")
            source = self.repo / "skills" / "example" / "SKILL.md"
            source.write_text("version two\n", encoding="utf-8")
            self.git("commit", "--all", "-m", "Edit the bundled skill")

            refreshed = refresh_stale_skill_deployments(
                self.home,
                source_repo=self.repo,
            )

        self.assertEqual(refreshed, (self.home / ".claude" / "skills" / "example",))
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(
            (self.home / ".claude" / "skills" / "example" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            "version two\n",
        )

    def test_publish_is_never_observed_half_written(self) -> None:
        self.deploy()
        source = self.repo / "skills" / "example" / "SKILL.md"
        source.write_text("version two\n", encoding="utf-8")
        self.git("commit", "--all", "-m", "Edit the bundled skill")
        codex_root = self.home / ".codex" / "skills"

        publishing = threading.Event()
        release = threading.Event()
        read_completed = threading.Event()
        observed: list[object] = []
        write_manifest = skill_deployment._write_manifest

        def hold_before_manifest(root: Path, payload: dict) -> None:
            # Managed files are already replaced here and the manifest still
            # records the previous digests, which is the window a reader would
            # misread as `runtime-edited`.
            publishing.set()
            release.wait(10)
            write_manifest(root, payload)

        def read_report() -> None:
            observed.append(verify_target_root(codex_root))
            read_completed.set()

        writer = threading.Thread(target=self.deploy)
        reader = threading.Thread(target=read_report)
        with mock.patch.object(
            skill_deployment,
            "_write_manifest",
            hold_before_manifest,
        ):
            writer.start()
            try:
                self.assertTrue(publishing.wait(10))
                reader.start()
                self.assertFalse(read_completed.wait(0.2))
            finally:
                release.set()
                writer.join(10)
        reader.join(10)

        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            {entry.state for entry in observed[0].entries},
            {"in-sync"},
        )

    def test_repository_supplies_bundle_only_for_an_in_tree_runtime(self) -> None:
        with self.bundled_source():
            self.assertTrue(repository_supplies_bundle(self.repo))
            self.assertFalse(repository_supplies_bundle(self.root / "other-repo"))

        packaged = self.root / "site-packages" / "vibe_loop"
        (packaged / "skills").mkdir(parents=True)
        with mock.patch(
            "vibe_loop.skills.importlib.resources.files",
            return_value=packaged,
        ):
            self.assertFalse(repository_supplies_bundle(self.repo))

    def test_install_rejects_a_root_outside_this_home(self) -> None:
        with self.assertRaisesRegex(SkillDeploymentError, "not a runtime root"):
            self.deploy(roots=(self.root / "elsewhere" / "skills",))

    def test_refresh_leaves_a_host_without_a_recorded_deployment_alone(self) -> None:
        with self.bundled_source():
            self.assertEqual(
                refresh_stale_skill_deployments(self.home, source_repo=self.repo),
                (),
            )

        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".claude").exists())

    def test_deployment_drift_advisory_reports_stale_bundled_skill(self) -> None:
        self.deploy()
        source = self.repo / "skills" / "example" / "SKILL.md"
        source.write_text("version two\n", encoding="utf-8")

        advisories = deployment_drift_advisories(
            self.home,
            source_root=self.repo / "skills",
            skill_names=("example",),
        )

        self.assertEqual(len(advisories), 1)
        advisory = advisories[0]
        self.assertEqual(advisory["code"], "skill_deployment_drift")
        self.assertEqual(advisory["affected_skills"], ["example"])
        self.assertIn(
            {
                "skill": "example",
                "path": "example/SKILL.md",
                "state": "stale",
                "detail": "source changed",
            },
            advisory["deployments"][0]["differences"],
        )

    def test_deployment_drift_advisory_reports_manifest_missing_root(self) -> None:
        unmanaged = self.home / ".codex" / "skills" / "example" / "SKILL.md"
        unmanaged.parent.mkdir(parents=True)
        unmanaged.write_text("copied without a manifest\n", encoding="utf-8")

        advisories = deployment_drift_advisories(
            self.home,
            source_root=self.repo / "skills",
            skill_names=("example",),
        )

        self.assertEqual(len(advisories), 1)
        differences = advisories[0]["deployments"][0]["differences"]
        self.assertEqual(
            {difference["state"] for difference in differences},
            {"manifest-missing", "unmanaged"},
        )
        self.assertTrue(
            all(difference["skill"] == "example" for difference in differences)
        )

    def test_deployment_drift_advisory_is_empty_for_in_sync_deployment(self) -> None:
        self.deploy()

        self.assertEqual(
            deployment_drift_advisories(
                self.home,
                source_root=self.repo / "skills",
                skill_names=("example",),
            ),
            (),
        )

    def test_deployment_drift_advisory_ignores_non_bundle_runtime_files(
        self,
    ) -> None:
        self.deploy()
        target = self.home / ".codex" / "skills" / "example"
        (target / "agents").mkdir()
        (target / "agents" / "openai.yaml").write_text(
            "runtime metadata\n",
            encoding="utf-8",
        )
        (target / "__pycache__").mkdir()
        (target / "__pycache__" / "generated.pyc").write_bytes(b"generated")

        self.assertEqual(
            deployment_drift_advisories(
                self.home,
                source_root=self.repo / "skills",
                skill_names=("example",),
            ),
            (),
        )

    def test_deployment_drift_advisory_reports_invalid_manifest(self) -> None:
        self.deploy()
        manifest = self.home / ".codex" / "skills" / MANIFEST_NAME
        manifest.write_text("not json\n", encoding="utf-8")

        advisories = deployment_drift_advisories(
            self.home,
            source_root=self.repo / "skills",
            skill_names=("example",),
        )

        differences = advisories[0]["deployments"][0]["differences"]
        self.assertIn("manifest-error", {item["state"] for item in differences})

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
