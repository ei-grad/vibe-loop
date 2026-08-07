from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import ast
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from vibe_loop.cli import main as cli_main
from vibe_loop.release_admission import (
    ReleaseAdmissionError,
    build_release_admission,
    bundled_skill_fingerprints,
    discover_release_base,
    eval_release_provenance,
    render_release_admission_summary,
    verify_release_admission,
)


# Anything that would carry local-environment or eval state to GitHub. The
# release path must not reintroduce a remotely hosted publishing precondition.
FORBIDDEN_WORKFLOW_FRAGMENTS = (
    "skill-readiness-evidence",
    "release-readiness",
    "readiness_run_id",
    "release-classify",
    "release-gate",
    "install-skills",
    "verify-skills",
    "skill-manifest",
    "GH_TOKEN",
    "api.github.com",
)

# A transport the runtime could carry local state out over. `urllib.parse` is
# string handling, and `socket` is admissible only for the local hostname
# recorded in lock and run metadata, so both are constrained by attribute rather
# than excluded outright.
FORBIDDEN_TRANSPORT_MODULES = (
    "urllib.request",
    "urllib.error",
    "http",
    "http.client",
    "httpx",
    "requests",
    "aiohttp",
    "ssl",
    "ftplib",
    "smtplib",
    "xmlrpc",
)
ALLOWED_SOCKET_ATTRIBUTES = frozenset({"gethostname"})
FORBIDDEN_COMMAND_NAMES = frozenset({"gh", "curl", "wget"})


class ReleaseAdmissionTests(unittest.TestCase):
    def test_release_workflows_carry_no_remote_eval_or_environment_evidence(
        self,
    ) -> None:
        workflows = repository_root() / ".github/workflows"
        self.assertFalse((workflows / "skill-readiness-evidence.yml").exists())
        for path in sorted(workflows.glob("*.yml")):
            content = path.read_text(encoding="utf-8")
            for fragment in FORBIDDEN_WORKFLOW_FRAGMENTS:
                with self.subTest(workflow=path.name, fragment=fragment):
                    self.assertNotIn(fragment, content)

        workflow = (workflows / "release.yml").read_text(encoding="utf-8")
        build_start = workflow.index("  build:")
        admission_start = workflow.index("  admission:")
        testpypi_start = workflow.index("  publish-testpypi:")
        pypi_start = workflow.index("  publish-pypi:")
        build = workflow[build_start:admission_start]
        admission = workflow[admission_start:testpypi_start]
        testpypi = workflow[testpypi_start:pypi_start]
        pypi = workflow[pypi_start:]

        self.assertIn("github.event_name == 'workflow_dispatch'", admission)
        self.assertNotIn("github.event_name == 'workflow_run'", admission)
        self.assertIn("vibe-loop eval release-admit", admission)
        self.assertIn("render_release_admission_summary", admission)
        self.assertIn("GITHUB_STEP_SUMMARY", admission)
        self.assertNotIn("release-admission.json", build)
        for publish_job in (testpypi, pypi):
            self.assertIn("- admission", publish_job)
            self.assertIn("- test", publish_job)
            self.assertIn("--verify", publish_job)
            self.assertLess(
                publish_job.index("Verify transferred admission and distributions"),
                publish_job.index("Publish distributions"),
            )

    def test_runtime_sources_carry_no_outbound_transport(self) -> None:
        sources = sorted((repository_root() / "src/vibe_loop").rglob("*.py"))
        self.assertTrue(sources)
        transports: list[str] = []
        socket_uses: list[str] = []
        commands: list[str] = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            location = str(path.relative_to(repository_root()))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    imported = (node.module,) if node.module else ()
                else:
                    imported = ()
                transports.extend(
                    f"{location}:{node.lineno} {module}"
                    for module in imported
                    if module in FORBIDDEN_TRANSPORT_MODULES
                )
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "socket"
                    and node.attr not in ALLOWED_SOCKET_ATTRIBUTES
                ):
                    socket_uses.append(f"{location}:{node.lineno} socket.{node.attr}")
                if (
                    isinstance(node, ast.Constant)
                    and node.value in FORBIDDEN_COMMAND_NAMES
                ):
                    commands.append(f"{location}:{node.lineno} {node.value!r}")

        self.assertEqual(transports, [])
        self.assertEqual(socket_uses, [])
        self.assertEqual(commands, [])

    def test_eval_provenance_rejects_untracked_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "contract\n")
            commit_all(repo, "base")
            write(repo / "src/vibe_loop/skills/new-skill/SKILL.md", "untracked\n")

            with self.assertRaisesRegex(ReleaseAdmissionError, "clean worktree"):
                eval_release_provenance(repo)

    def test_release_base_discovery_requires_a_reachable_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            write(repo / "src/vibe_loop/activity.py", "value\n")
            head = commit_all(repo, "head")

            with self.assertRaisesRegex(ReleaseAdmissionError, "no prior reachable"):
                discover_release_base(repo, head)

            run(repo, "tag", "v0.1.0", head)
            write(repo / "src/vibe_loop/activity.py", "next\n")
            later = commit_all(repo, "later")

            self.assertEqual(discover_release_base(repo, later), (head, "v0.1.0"))

    def test_admission_binds_distributions_to_the_built_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            fingerprints = bundled_skill_fingerprints(repo)
            head = run(repo, "rev-parse", "HEAD")
            wheel = repo / "dist/vibe_loop-1-py3-none-any.whl"
            build_wheel(wheel, fingerprints, repo)

            admission = build_release_admission(repo, distributions=(wheel,))

            self.assertEqual(admission["status"], "passed")
            self.assertEqual(admission["schema_version"], 3)
            self.assertEqual(admission["head"], head)
            self.assertEqual(admission["bundled_skills"], fingerprints)
            self.assertEqual(
                [record["name"] for record in admission["distributions"]],
                [wheel.name],
            )
            self.assertEqual(
                verify_release_admission(admission, repo=repo, distributions=(wheel,)),
                (),
            )
            self.assertEqual(
                render_release_admission_summary(admission).splitlines()[0],
                f"release admission: passed head={head}",
            )

            build_wheel(wheel, fingerprints, repo, content_override=b"substituted\n")
            diagnostics = verify_release_admission(
                admission, repo=repo, distributions=(wheel,)
            )

        self.assertTrue(
            any(
                "bundled skills do not match the built commit" in diagnostic
                for diagnostic in diagnostics
            )
        )
        self.assertTrue(
            any("transferred artifacts" in diagnostic for diagnostic in diagnostics)
        )

    def test_publication_needs_no_eval_record_for_bundled_skill_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "new contract\n")
            write(repo / "eval/fixture.json", "{}\n")
            commit_all(repo, "change bundled skills and eval fixtures")
            fingerprints = bundled_skill_fingerprints(repo)
            wheel = repo / "dist/vibe_loop-1-py3-none-any.whl"
            build_wheel(wheel, fingerprints, repo)

            admission = build_release_admission(repo, distributions=(wheel,))

            self.assertEqual(admission["status"], "passed")
            self.assertEqual(admission["diagnostics"], [])
            self.assertEqual(
                set(admission),
                {
                    "schema_version",
                    "record_type",
                    "status",
                    "head",
                    "bundled_skills",
                    "distributions",
                    "diagnostics",
                },
            )
            serialized = json.dumps(admission, sort_keys=True).lower()
            for fragment in ("readiness", "provenance", "github", "classification"):
                with self.subTest(fragment=fragment):
                    self.assertNotIn(fragment, serialized)

    def test_skill_sources_that_left_the_built_commit_block_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            fingerprints = bundled_skill_fingerprints(repo)
            wheel = repo / "dist/vibe_loop-1-py3-none-any.whl"
            build_wheel(wheel, fingerprints, repo)
            write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "uncommitted\n")

            modified = build_release_admission(repo, distributions=(wheel,))

            self.assertEqual(modified["status"], "blocked")
            self.assertIn(
                "bundled skill sources differ from the built commit",
                modified["diagnostics"],
            )

            run(repo, "checkout", "--", "src/vibe_loop/skills/vibe-loop/SKILL.md")
            write(repo / "src/vibe_loop/skills/extra/SKILL.md", "untracked\n")

            untracked = build_release_admission(repo, distributions=(wheel,))

            self.assertEqual(untracked["status"], "blocked")
            self.assertIn(
                "bundled skill sources contain untracked files",
                untracked["diagnostics"],
            )

    def test_missing_and_unreadable_distributions_block_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            absent = repo / "dist/vibe_loop-1-py3-none-any.whl"
            corrupt = repo / "dist/vibe_loop-1.tar.gz"
            write(corrupt, "not an archive\n")

            admission = build_release_admission(repo, distributions=(absent, corrupt))

            self.assertEqual(admission["status"], "blocked")
            self.assertIn(
                f"distribution is missing: {absent.name}", admission["diagnostics"]
            )
            self.assertIn(
                f"distribution skill validation failed: {corrupt.name}",
                admission["diagnostics"],
            )
            with self.assertRaisesRegex(ReleaseAdmissionError, "blocked"):
                render_release_admission_summary(admission)

            empty = build_release_admission(repo, distributions=())
            self.assertIn("release distributions are missing", empty["diagnostics"])

    def test_cli_builds_and_verifies_admission_and_rejects_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            fingerprints = bundled_skill_fingerprints(repo)
            head = run(repo, "rev-parse", "HEAD")
            wheel = repo / "dist/vibe_loop-1-py3-none-any.whl"
            build_wheel(wheel, fingerprints, repo)
            admission_path = repo / "admission.json"
            arguments = [
                "eval",
                "release-admit",
                "--repo",
                str(repo),
                "--distribution",
                str(wheel),
                "--output",
                str(admission_path),
            ]

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main(arguments), 0)
            self.assertIn(f"release admission: passed head={head}", output.getvalue())
            self.assertNotIn("github.com", output.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main([*arguments, "--verify"]), 0)

            tampered = json.loads(admission_path.read_text(encoding="utf-8"))
            tampered["signed_url"] = "https://example.invalid/secret-value"
            write_json(admission_path, tampered)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main([*arguments, "--verify", "--json"]), 1)
            self.assertIn('"status": "blocked"', output.getvalue())
            self.assertNotIn("secret-value", output.getvalue())

            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(arguments), 0)
            build_wheel(wheel, fingerprints, repo, content_override=b"substituted\n")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main([*arguments, "--verify"]), 1)
            self.assertIn("release admission blocked:", output.getvalue())

    def test_cli_rejects_the_removed_release_evidence_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = release_repo(Path(directory))
            for removed in (
                ["eval", "release-classify", "--repo", str(repo), "--output", "x.json"],
                [
                    "eval",
                    "release-admit",
                    "--repo",
                    str(repo),
                    "--readiness-record",
                    "readiness.json",
                    "--distribution",
                    "dist/x.whl",
                    "--output",
                    "admission.json",
                ],
            ):
                with self.subTest(arguments=removed[1]):
                    with self.assertRaises(SystemExit) as raised:
                        cli_main(removed)
                    self.assertEqual(raised.exception.code, 2)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def initialize_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    run(repo, "init", "-q")
    run(repo, "config", "user.name", "Test")
    run(repo, "config", "user.email", "test@example.com")
    return repo


def release_repo(root: Path) -> Path:
    repo = initialize_repo(root / "repo")
    write(repo / ".gitignore", "/dist/\n/admission.json\n")
    write(repo / "src/vibe_loop/skills/vibe-loop/SKILL.md", "contract\n")
    write(repo / "README.md", "base\n")
    commit_all(repo, "base")
    run(repo, "tag", "v0.1.0")
    return repo


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def build_wheel(
    path: Path,
    fingerprints: dict[str, str],
    root: Path,
    *,
    content_override: bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for source in fingerprints:
            content = content_override or (root / source).read_bytes()
            archive.writestr(source.removeprefix("src/"), content)
