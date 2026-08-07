from __future__ import annotations

import hashlib
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path


FULL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
RELEASE_ADMISSION_SCHEMA_VERSION = 3
RELEASE_ADMISSION_RECORD_TYPE = "skill_release_admission"
BUNDLED_SKILL_SOURCE_ROOT = "src/vibe_loop/skills"


class ReleaseAdmissionError(ValueError):
    pass


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git command failed"
        raise ReleaseAdmissionError(detail)
    return result.stdout.strip()


def resolve_exact_commit(repo: Path, revision: str) -> str:
    if not revision or any(character.isspace() for character in revision):
        raise ReleaseAdmissionError("revision is missing or malformed")
    commit = git_output(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not FULL_COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseAdmissionError("revision did not resolve to a full Git commit")
    return commit


def discover_release_base(repo: Path, head: str) -> tuple[str, str]:
    exact_head = resolve_exact_commit(repo, head)
    refs = git_output(
        repo,
        "for-each-ref",
        "--sort=-version:refname",
        "--format=%(refname:short)",
        "refs/tags/v*",
    ).splitlines()
    for ref in refs:
        try:
            commit = resolve_exact_commit(repo, ref)
        except ReleaseAdmissionError:
            continue
        if commit == exact_head:
            continue
        result = subprocess.run(
            ("git", "merge-base", "--is-ancestor", commit, exact_head),
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return commit, ref
        if result.returncode not in (0, 1):
            raise ReleaseAdmissionError("cannot verify release-base ancestry")
    raise ReleaseAdmissionError(
        "no prior reachable v* release tag provides an authoritative base"
    )


def bundled_skill_fingerprints(root: Path) -> dict[str, str]:
    skills_root = root / "src" / "vibe_loop" / "skills"
    fingerprints: dict[str, str] = {}
    for path in sorted(skills_root.glob("*/SKILL.md")):
        relative = path.relative_to(root).as_posix()
        fingerprints[relative] = sha256_file(path)
    if not fingerprints:
        raise ReleaseAdmissionError("bundled skill sources are missing")
    return fingerprints


def eval_release_provenance(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    head = resolve_exact_commit(repo, "HEAD")
    if git_output(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseAdmissionError("release eval provenance requires a clean worktree")
    return {
        "repository_head": head,
        "bundled_skills": bundled_skill_fingerprints(repo),
    }


def distribution_fingerprints(path: Path) -> dict[str, str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            members = {
                name: archive.read(name)
                for name in archive.namelist()
                if "/skills/" in name and name.endswith("/SKILL.md")
            }
    elif path.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        members = {}
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if (
                    not member.isfile()
                    or "/skills/" not in member.name
                    or not member.name.endswith("/SKILL.md")
                ):
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    members[member.name] = extracted.read()
    else:
        raise ReleaseAdmissionError(f"unsupported distribution format: {path.name}")
    normalized = {}
    for name, content in members.items():
        marker = "vibe_loop/skills/"
        offset = name.find(marker)
        if offset < 0:
            continue
        source_name = "src/" + name[offset:]
        normalized[source_name] = hashlib.sha256(content).hexdigest()
    if not normalized:
        raise ReleaseAdmissionError(f"distribution has no bundled skills: {path.name}")
    return normalized


def bundled_skill_revision_gaps(repo: Path, commit: str) -> tuple[str, ...]:
    """Report why the bundled skill sources on disk are not the ones in `commit`.

    Admission runs beside build outputs and downloaded records, so worktree
    cleanliness cannot be required globally; only the packaged skill sources
    have to correspond to the commit being published.
    """
    gaps: list[str] = []
    tracked = subprocess.run(
        ("git", "diff", "--quiet", commit, "--", BUNDLED_SKILL_SOURCE_ROOT),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked.returncode == 1:
        gaps.append("bundled skill sources differ from the built commit")
    elif tracked.returncode != 0:
        raise ReleaseAdmissionError(
            "cannot compare bundled skill sources with the built commit"
        )
    if git_output(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        BUNDLED_SKILL_SOURCE_ROOT,
    ):
        gaps.append("bundled skill sources contain untracked files")
    return tuple(gaps)


def build_release_admission(
    repo: Path,
    *,
    distributions: Sequence[Path],
    head: str = "HEAD",
) -> dict[str, object]:
    repo = repo.resolve()
    exact_head = resolve_exact_commit(repo, head)
    diagnostics: list[str] = list(bundled_skill_revision_gaps(repo, exact_head))
    fingerprints = bundled_skill_fingerprints(repo)
    if not distributions:
        diagnostics.append("release distributions are missing")
    distribution_records: list[dict[str, object]] = []
    for path in distributions:
        try:
            distribution_records.append(
                {
                    "name": path.name,
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        except OSError:
            diagnostics.append(f"distribution is missing: {path.name}")
            continue
        try:
            packaged = distribution_fingerprints(path)
        except (
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
            ReleaseAdmissionError,
        ):
            diagnostics.append(f"distribution skill validation failed: {path.name}")
            continue
        if packaged != fingerprints:
            diagnostics.append(
                f"distribution bundled skills do not match the built commit: {path.name}"
            )
    return {
        "schema_version": RELEASE_ADMISSION_SCHEMA_VERSION,
        "record_type": RELEASE_ADMISSION_RECORD_TYPE,
        "status": "passed" if not diagnostics else "blocked",
        "head": exact_head,
        "bundled_skills": dict(sorted(fingerprints.items())),
        "distributions": sorted(
            distribution_records, key=lambda record: str(record["name"])
        ),
        "diagnostics": diagnostics,
    }


def verify_release_admission(
    admission: Mapping[str, object],
    *,
    repo: Path,
    distributions: Sequence[Path],
    head: str = "HEAD",
) -> tuple[str, ...]:
    expected = build_release_admission(repo, distributions=distributions, head=head)
    diagnostics = list(expected["diagnostics"])
    if set(admission) != set(expected):
        diagnostics.append("admission fields are missing or unexpected")
    for field in (
        "schema_version",
        "record_type",
        "status",
        "head",
        "bundled_skills",
        "distributions",
        "diagnostics",
    ):
        if admission.get(field) != expected.get(field):
            diagnostics.append(
                f"admission {field} does not match transferred artifacts"
            )
    if admission.get("status") != "passed":
        diagnostics.append("admission status is not passed")
    return tuple(dict.fromkeys(diagnostics))


def render_release_admission_summary(admission: Mapping[str, object]) -> str:
    if admission.get("status") != "passed":
        raise ReleaseAdmissionError("cannot summarize a blocked release admission")
    head = admission.get("head")
    if not isinstance(head, str) or not FULL_COMMIT_PATTERN.fullmatch(head):
        raise ReleaseAdmissionError("release admission head is invalid")
    distributions = admission.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        raise ReleaseAdmissionError("release admission distributions are missing")
    lines = [f"release admission: passed head={head}"]
    for record in distributions:
        if not isinstance(record, Mapping):
            raise ReleaseAdmissionError("release admission distribution is malformed")
        name = record.get("name")
        digest = record.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ReleaseAdmissionError("release admission distribution is malformed")
        lines.append(f"distribution: {name} sha256={digest}")
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
