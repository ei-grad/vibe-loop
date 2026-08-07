from __future__ import annotations

from _test_bootstrap import TEST_ENVIRONMENT_CONFIGURED as TEST_ENVIRONMENT_CONFIGURED

import ast
import io
import json
import re
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
# Splits a string constant into the words a shell would run, so a command buried
# in a `shell=True` line is matched as well as a bare argv element. Regex
# character classes such as `gh[oprsu]_` stay one token and do not match.
COMMAND_SEPARATOR = re.compile(r"[\s;|&()<>]+")


def imported_module_names(node: ast.AST) -> tuple[str, ...]:
    """Dotted names an import binds, as written plus each `from` target."""

    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return (node.module,) + tuple(
            f"{node.module}.{alias.name}" for alias in node.names
        )
    return ()


def local_socket_names(tree: ast.AST) -> set[str]:
    """Local names bound to the `socket` module, including aliases."""

    names = {"socket"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "socket" or alias.name.startswith("socket."):
                    names.add((alias.asname or alias.name).partition(".")[0])
    return names


def outbound_transport_violations(source: str, location: str) -> list[str]:
    """Report each lexical way `source` could carry local state off the host."""

    tree = ast.parse(source)
    socket_names = local_socket_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        for module in imported_module_names(node):
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in FORBIDDEN_TRANSPORT_MODULES
            ):
                violations.append(f"{location}:{node.lineno} transport {module}")
            root, _, attribute = module.partition(".")
            if (
                root == "socket"
                and attribute
                and attribute not in ALLOWED_SOCKET_ATTRIBUTES
            ):
                violations.append(f"{location}:{node.lineno} socket use {module}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in socket_names
            and node.attr not in ALLOWED_SOCKET_ATTRIBUTES
        ):
            violations.append(
                f"{location}:{node.lineno} socket use {node.value.id}.{node.attr}"
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for token in COMMAND_SEPARATOR.split(node.value):
                if token in FORBIDDEN_COMMAND_NAMES:
                    violations.append(
                        f"{location}:{node.lineno} command {token} in "
                        f"{node.value[:60]!r}"
                    )
    return violations


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
        self.assertIn("publish-admission-${{ github.sha }}", admission)
        # The admission record is the one artifact a publisher requires, so the
        # dependency, its transfer, and revalidation must all precede the step
        # that holds publishing credentials.
        for publish_job in (testpypi, pypi):
            self.assertIn("- admission", publish_job)
            self.assertIn("- test", publish_job)
            self.assertIn("--verify", publish_job)
            self.assertIn("publish-admission-${{ github.sha }}", publish_job)
            self.assertLess(
                publish_job.index("Download publish admission"),
                publish_job.index("Verify transferred admission and distributions"),
            )
            self.assertLess(
                publish_job.index("Verify transferred admission and distributions"),
                publish_job.index("Publish distributions"),
            )

    def test_runtime_sources_carry_no_outbound_transport(self) -> None:
        root = repository_root()
        sources = sorted((root / "src/vibe_loop").rglob("*.py"))
        self.assertTrue(sources)
        violations: list[str] = []
        for path in sources:
            violations.extend(
                outbound_transport_violations(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(root)),
                )
            )

        self.assertEqual(violations, [])

    def test_transport_guard_reports_indirect_egress_forms(self) -> None:
        # The guard is only worth having if it survives the obvious rewrites, so
        # each admissible and each forbidden form is asserted directly.
        admissible = outbound_transport_violations(
            "import socket\n"
            "import urllib.parse\n"
            "from socket import gethostname\n"
            "from urllib.parse import unquote\n"
            "host = socket.gethostname()\n"
            'pattern = "(?i)gh[oprsu]_[A-Za-z0-9_]+"\n'
            'url = "https://github.com/ei-grad/vibe-loop"\n',
            "admissible.py",
        )
        self.assertEqual(admissible, [])

        for label, source in (
            ("dotted import", "import urllib.request\n"),
            ("from import", "from urllib import request\n"),
            ("from submodule", "from urllib.request import urlopen\n"),
            ("package import", "from http import client\n"),
            ("third party", "import httpx\n"),
            ("socket from import", "from socket import create_connection\n"),
            (
                "aliased socket",
                "import socket as sk\nsk.create_connection(('h', 1))\n",
            ),
            ("argv command", 'run(["gh", "api", "repos"])\n'),
            (
                "shell string command",
                'run("cd /tmp && curl -X POST https://example", shell=True)\n',
            ),
        ):
            with self.subTest(form=label):
                self.assertTrue(
                    outbound_transport_violations(source, "candidate.py"),
                    f"{label} escaped the transport guard",
                )

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
