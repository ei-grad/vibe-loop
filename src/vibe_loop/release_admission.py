from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath


FULL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
RELEASE_CLASSIFICATION_SCHEMA_VERSION = 1
RELEASE_CLASSIFICATION_RECORD_TYPE = "skill_release_classification"
RELEASE_ADMISSION_SCHEMA_VERSION = 2
RELEASE_ADMISSION_RECORD_TYPE = "skill_release_admission"
READINESS_PROVENANCE_SCHEMA_VERSION = 1
READINESS_PROVENANCE_RECORD_TYPE = "skill_release_readiness_provenance"
READINESS_WORKFLOW_PATH = ".github/workflows/skill-readiness-evidence.yml"
OWNERSHIP_CONTRACT_VERSION = 1

# This is the authoritative ownership boundary for release-readiness classification.
OWNED_PATH_PREFIXES = (
    ".github/workflows/",
    "eval/",
    "src/vibe_loop/skills/",
)
OWNED_PATH_PATTERNS = (
    "src/vibe_loop/eval*.py",
    "tests/test_eval*.py",
)
OWNED_EXACT_PATHS = frozenset(
    {
        "Makefile",
        "docs/cli-reference.md",
        "docs/examples/release-readiness-dry-run.json",
        "docs/prd/evals-release.md",
        "docs/prd/skills.md",
        "docs/release-checklist.md",
        "docs/skill-eval-schema.md",
        "pyproject.toml",
        "src/vibe_loop/cli.py",
        "src/vibe_loop/release_admission.py",
        "tests/test_release_admission.py",
        "uv.lock",
    }
)


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


def path_requires_readiness(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    return (
        normalized in OWNED_EXACT_PATHS
        or any(normalized.startswith(prefix) for prefix in OWNED_PATH_PREFIXES)
        or any(
            fnmatch.fnmatchcase(normalized, pattern) for pattern in OWNED_PATH_PATTERNS
        )
    )


def classify_release_changes(
    repo: Path,
    *,
    head: str = "HEAD",
    base: str | None = None,
) -> dict[str, object]:
    repo = repo.resolve()
    exact_head = resolve_exact_commit(repo, head)
    if base is None:
        exact_base, base_source = discover_release_base(repo, exact_head)
    else:
        if not FULL_COMMIT_PATTERN.fullmatch(base):
            raise ReleaseAdmissionError("release base must be a full Git commit")
        exact_base = resolve_exact_commit(repo, base)
        if exact_base != base:
            raise ReleaseAdmissionError("release base must be canonical")
        base_source = "explicit_exact_commit"

    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", exact_base, exact_head),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode == 1:
        raise ReleaseAdmissionError("release base is not an ancestor of release head")
    if ancestor.returncode != 0:
        raise ReleaseAdmissionError("cannot verify release-base ancestry")

    shallow = git_output(repo, "rev-parse", "--is-shallow-repository") == "true"
    raw = subprocess.run(
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            exact_base,
            exact_head,
        ),
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if raw.returncode != 0:
        raise ReleaseAdmissionError("cannot classify release changed paths")
    changes = parse_name_status(raw.stdout)
    owned_paths = sorted(
        {
            path
            for change in changes
            for path in change["paths"]
            if path_requires_readiness(path)
        }
    )
    uncertainty = ["shallow_repository"] if shallow else []
    required = bool(owned_paths or uncertainty)
    return {
        "schema_version": RELEASE_CLASSIFICATION_SCHEMA_VERSION,
        "record_type": RELEASE_CLASSIFICATION_RECORD_TYPE,
        "ownership_contract_version": OWNERSHIP_CONTRACT_VERSION,
        "base": exact_base,
        "base_source": base_source,
        "head": exact_head,
        "status": "readiness_required" if required else "unrelated_exemption",
        "changed_paths": changes,
        "owned_paths": owned_paths,
        "uncertainty": uncertainty,
    }


def parse_name_status(raw: bytes) -> list[dict[str, object]]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changes: list[dict[str, object]] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ReleaseAdmissionError("changed-path status is not ASCII") from exc
        index += 1
        kind = status[:1]
        path_count = 2 if kind in {"R", "C"} else 1
        if kind not in {"A", "C", "D", "M", "R", "T", "U", "X", "B"}:
            raise ReleaseAdmissionError(f"unknown changed-path status: {status}")
        if index + path_count > len(fields):
            raise ReleaseAdmissionError("incomplete changed-path record")
        try:
            paths = [
                fields[position].decode("utf-8")
                for position in range(index, index + path_count)
            ]
        except UnicodeDecodeError as exc:
            raise ReleaseAdmissionError("changed path is not UTF-8") from exc
        if any(
            not path or path.startswith("/") or ".." in PurePosixPath(path).parts
            for path in paths
        ):
            raise ReleaseAdmissionError("changed path is unsafe")
        changes.append({"status": status, "paths": paths})
        index += path_count
    return changes


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


def validate_readiness_record(
    record: Mapping[str, object],
    *,
    classification: Mapping[str, object],
    distributions: Sequence[Path],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if record.get("schema_version") != 2:
        diagnostics.append("readiness schema version is unsupported")
    if record.get("record_type") != "skill_release_readiness":
        diagnostics.append("readiness record type is invalid")
    if record.get("status") != "passed":
        diagnostics.append("readiness status is not passed")
    diagnostics.extend(validate_readiness_gate_payload(record))
    revision = record.get("revision")
    if not isinstance(revision, Mapping):
        diagnostics.append("readiness revision binding is missing")
    else:
        for field in ("base", "head"):
            value = revision.get(field)
            if not isinstance(value, str) or not FULL_COMMIT_PATTERN.fullmatch(value):
                diagnostics.append(f"readiness {field} revision is malformed")
            elif value != classification.get(field):
                diagnostics.append(
                    f"readiness {field} revision does not match classification"
                )
    expected = record.get("bundled_skills")
    if not isinstance(expected, Mapping) or not expected:
        diagnostics.append("bundled skill fingerprints are missing")
        expected = {}
    elif not all(
        isinstance(path, str)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        for path, digest in expected.items()
    ):
        diagnostics.append("bundled skill fingerprints are malformed")
    for distribution in distributions:
        try:
            actual = distribution_fingerprints(distribution)
        except (
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
            ReleaseAdmissionError,
        ):
            diagnostics.append(
                f"distribution skill validation failed: {distribution.name}"
            )
            continue
        if dict(expected) != actual:
            diagnostics.append(
                f"distribution bundled skills do not match readiness evidence: {distribution.name}"
            )
    return tuple(diagnostics)


def validate_readiness_provenance(
    record: Mapping[str, object],
    *,
    classification: Mapping[str, object],
    readiness_record: Mapping[str, object],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    expected_keys = {
        "schema_version",
        "record_type",
        "classification_head",
        "repository",
        "workflow",
        "run",
        "artifact",
        "evidence_reference",
    }
    if set(record) != expected_keys:
        diagnostics.append("readiness provenance fields are missing or unexpected")
    if record.get("schema_version") != READINESS_PROVENANCE_SCHEMA_VERSION:
        diagnostics.append("readiness provenance schema version is unsupported")
    if record.get("record_type") != READINESS_PROVENANCE_RECORD_TYPE:
        diagnostics.append("readiness provenance record type is invalid")

    head = record.get("classification_head")
    readiness_revision = readiness_record.get("revision")
    readiness_head = (
        readiness_revision.get("head")
        if isinstance(readiness_revision, Mapping)
        else None
    )
    if not isinstance(head, str) or not FULL_COMMIT_PATTERN.fullmatch(head):
        diagnostics.append("readiness provenance head is malformed")
    elif head != classification.get("head") or head != readiness_head:
        diagnostics.append(
            "readiness provenance head does not match classification and readiness"
        )

    repository = record.get("repository")
    repository_name: str | None = None
    repository_url: str | None = None
    if not isinstance(repository, Mapping) or set(repository) != {
        "full_name",
        "html_url",
    }:
        diagnostics.append("readiness provenance repository is malformed")
    else:
        full_name = repository.get("full_name")
        html_url = repository.get("html_url")
        if (
            not isinstance(full_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name)
            or full_name.startswith(".")
            or "/." in full_name
        ):
            diagnostics.append("readiness provenance repository identity is malformed")
        elif html_url != f"https://github.com/{full_name}":
            diagnostics.append("readiness provenance repository URL is not canonical")
        else:
            repository_name = full_name
            repository_url = html_url

    workflow = record.get("workflow")
    if not isinstance(workflow, Mapping) or set(workflow) != {"id", "path"}:
        diagnostics.append("readiness provenance workflow is malformed")
    else:
        if not _is_positive_integer(workflow.get("id")):
            diagnostics.append("readiness provenance workflow id is malformed")
        if workflow.get("path") != READINESS_WORKFLOW_PATH:
            diagnostics.append("readiness provenance workflow path is invalid")

    run = record.get("run")
    run_id: int | None = None
    if not isinstance(run, Mapping) or set(run) != {"id", "head", "conclusion"}:
        diagnostics.append("readiness provenance run is malformed")
    else:
        if not _is_positive_integer(run.get("id")):
            diagnostics.append("readiness provenance run id is malformed")
        else:
            run_id = run["id"]
        if run.get("head") != head:
            diagnostics.append("readiness provenance run head does not match")
        if run.get("conclusion") != "success":
            diagnostics.append("readiness provenance run was not successful")

    artifact = record.get("artifact")
    artifact_id: int | None = None
    if not isinstance(artifact, Mapping) or set(artifact) != {"id", "name"}:
        diagnostics.append("readiness provenance artifact is malformed")
    else:
        if not _is_positive_integer(artifact.get("id")):
            diagnostics.append("readiness provenance artifact id is malformed")
        else:
            artifact_id = artifact["id"]
        if artifact.get("name") != f"skill-release-readiness-{head}":
            diagnostics.append("readiness provenance artifact name does not match head")

    expected_reference = (
        f"{repository_url}/actions/runs/{run_id}/artifacts/{artifact_id}"
        if repository_name is not None
        and repository_url is not None
        and run_id is not None
        and artifact_id is not None
        else None
    )
    if record.get("evidence_reference") != expected_reference:
        diagnostics.append("readiness provenance evidence reference is not canonical")
    return tuple(diagnostics)


def build_github_readiness_provenance(
    *,
    expected_repository: str,
    expected_head: str,
    repository: Mapping[str, object],
    workflow: Mapping[str, object],
    run: Mapping[str, object],
    artifact: Mapping[str, object],
) -> dict[str, object]:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repository)
        or expected_repository.startswith(".")
        or "/." in expected_repository
    ):
        raise ReleaseAdmissionError("GitHub repository identity is malformed")
    if not FULL_COMMIT_PATTERN.fullmatch(expected_head):
        raise ReleaseAdmissionError("GitHub readiness head is malformed")
    repository_url = f"https://github.com/{expected_repository}"
    if (
        repository.get("full_name") != expected_repository
        or repository.get("html_url") != repository_url
    ):
        raise ReleaseAdmissionError("GitHub repository response does not match")

    workflow_id = workflow.get("id")
    if not _is_positive_integer(workflow_id):
        raise ReleaseAdmissionError("GitHub readiness workflow id is malformed")
    if workflow.get("path") != READINESS_WORKFLOW_PATH:
        raise ReleaseAdmissionError("GitHub readiness workflow path does not match")

    run_id = run.get("id")
    run_path = run.get("path")
    if not _is_positive_integer(run_id):
        raise ReleaseAdmissionError("GitHub readiness run id is malformed")
    if (
        run.get("head_sha") != expected_head
        or run.get("workflow_id") != workflow_id
        or not isinstance(run_path, str)
        or run_path.partition("@")[0] != READINESS_WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("conclusion") != "success"
    ):
        raise ReleaseAdmissionError("GitHub readiness run does not match")

    artifact_id = artifact.get("id")
    artifact_name = f"skill-release-readiness-{expected_head}"
    artifact_run = artifact.get("workflow_run")
    if not _is_positive_integer(artifact_id):
        raise ReleaseAdmissionError("GitHub readiness artifact id is malformed")
    if (
        artifact.get("name") != artifact_name
        or artifact.get("expired") is not False
        or not isinstance(artifact_run, Mapping)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_sha") != expected_head
    ):
        raise ReleaseAdmissionError("GitHub readiness artifact does not match")

    return {
        "schema_version": READINESS_PROVENANCE_SCHEMA_VERSION,
        "record_type": READINESS_PROVENANCE_RECORD_TYPE,
        "classification_head": expected_head,
        "repository": {
            "full_name": expected_repository,
            "html_url": repository_url,
        },
        "workflow": {"id": workflow_id, "path": READINESS_WORKFLOW_PATH},
        "run": {"id": run_id, "head": expected_head, "conclusion": "success"},
        "artifact": {"id": artifact_id, "name": artifact_name},
        "evidence_reference": (
            f"{repository_url}/actions/runs/{run_id}/artifacts/{artifact_id}"
        ),
    }


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_readiness_gate_payload(record: Mapping[str, object]) -> tuple[str, ...]:
    from vibe_loop.eval_release import release_gate_case_conditions

    diagnostics: list[str] = []
    gate = record.get("gate")
    if not isinstance(gate, Mapping):
        return ("readiness gate payload is missing",)
    expected_matrix = {
        case_id: list(conditions)
        for case_id, conditions in sorted(release_gate_case_conditions().items())
    }
    if gate.get("name") != "bundled_skill_release_readiness":
        diagnostics.append("readiness gate identity is invalid")
    if gate.get("required_case_conditions") != expected_matrix:
        diagnostics.append("readiness release matrix is incomplete or altered")
    minimum_trials = gate.get("minimum_trials_per_case_condition")
    if not isinstance(minimum_trials, int) or minimum_trials < 1:
        diagnostics.append("readiness minimum trial count is invalid")
    blockers = gate.get("blockers")
    if not isinstance(blockers, list) or blockers:
        diagnostics.append("readiness gate blockers are missing or unresolved")
    local_suite = record.get("local_suite")
    if (
        not isinstance(local_suite, Mapping)
        or local_suite.get("coverage_status") != "passed"
    ):
        diagnostics.append("readiness release-matrix coverage is not passed")
    trial_failures = record.get("trial_failures")
    if (
        not isinstance(trial_failures, Mapping)
        or trial_failures.get("status") != "passed"
        or trial_failures.get("total") != 0
    ):
        diagnostics.append("readiness required trial evidence is not passed")
    regressions = record.get("workflow_contract_regressions")
    if not isinstance(regressions, Mapping):
        diagnostics.append("readiness workflow-regression evidence is missing")
    else:
        if regressions.get("evidence_status") != "passed":
            diagnostics.append("readiness workflow-regression evidence is blocked")
        if regressions.get("unresolved"):
            diagnostics.append("readiness has unresolved workflow-contract regressions")
        if regressions.get("invalid_parked_ids"):
            diagnostics.append("readiness has invalid parked regressions")
        parked = regressions.get("parked")
        if not isinstance(parked, list) or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("parked_task_ids"), list)
            or not item.get("parked_task_ids")
            or not all(
                isinstance(task_id, str) and task_id.strip()
                for task_id in item["parked_task_ids"]
            )
            for item in parked
        ):
            diagnostics.append("readiness parked-regression evidence is invalid")
    provenance = record.get("release_provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("status") != "passed"
        or provenance.get("gaps") != []
    ):
        diagnostics.append("readiness exact-source provenance is not passed")
    return tuple(diagnostics)


def build_release_admission(
    classification: Mapping[str, object],
    *,
    readiness_record: Mapping[str, object] | None,
    readiness_provenance: Mapping[str, object] | None,
    distributions: Sequence[Path],
    repo: Path | None = None,
) -> dict[str, object]:
    diagnostics: list[str] = []
    validated_provenance: dict[str, object] | None = None
    if classification.get("record_type") != RELEASE_CLASSIFICATION_RECORD_TYPE:
        diagnostics.append("classification record type is invalid")
    if classification.get("ownership_contract_version") != OWNERSHIP_CONTRACT_VERSION:
        diagnostics.append("classification ownership contract is unsupported")
    if repo is not None:
        diagnostics.extend(validate_release_classification(classification, repo=repo))
    status = classification.get("status")
    if status == "readiness_required":
        if readiness_record is None:
            diagnostics.append("readiness evidence is required but missing")
        else:
            diagnostics.extend(
                validate_readiness_record(
                    readiness_record,
                    classification=classification,
                    distributions=distributions,
                )
            )
        if readiness_provenance is None:
            diagnostics.append("readiness provenance is required but missing")
        elif readiness_record is not None:
            provenance_diagnostics = validate_readiness_provenance(
                readiness_provenance,
                classification=classification,
                readiness_record=readiness_record,
            )
            diagnostics.extend(provenance_diagnostics)
            if not provenance_diagnostics:
                validated_provenance = dict(readiness_provenance)
        decision = "readiness"
    elif status == "unrelated_exemption":
        decision = "exemption"
        if readiness_record is not None or readiness_provenance is not None:
            diagnostics.append("unrelated exemption contains readiness evidence")
        if classification.get("owned_paths"):
            diagnostics.append("unrelated exemption contains an owned path")
        if classification.get("uncertainty"):
            diagnostics.append(
                "unrelated exemption contains classification uncertainty"
            )
        if not isinstance(classification.get("changed_paths"), list):
            diagnostics.append("unrelated exemption changed paths are missing")
    else:
        decision = "invalid"
        diagnostics.append("classification status is invalid")
    distribution_records = []
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
    return {
        "schema_version": RELEASE_ADMISSION_SCHEMA_VERSION,
        "record_type": RELEASE_ADMISSION_RECORD_TYPE,
        "status": "passed" if not diagnostics else "blocked",
        "decision": decision,
        "base": classification.get("base"),
        "head": classification.get("head"),
        "classification_sha256": mapping_sha256(classification),
        "readiness_sha256": mapping_sha256(readiness_record)
        if readiness_record
        else None,
        "readiness_provenance": validated_provenance,
        "readiness_provenance_sha256": mapping_sha256(validated_provenance),
        "distributions": distribution_records,
        "diagnostics": diagnostics,
    }


def verify_release_admission(
    admission: Mapping[str, object],
    *,
    classification: Mapping[str, object],
    readiness_record: Mapping[str, object] | None,
    readiness_provenance: Mapping[str, object] | None,
    distributions: Sequence[Path],
    repo: Path | None = None,
) -> tuple[str, ...]:
    expected = build_release_admission(
        classification,
        readiness_record=readiness_record,
        readiness_provenance=readiness_provenance,
        distributions=distributions,
        repo=repo,
    )
    diagnostics = list(expected["diagnostics"])
    if set(admission) != set(expected):
        diagnostics.append("admission fields are missing or unexpected")
    for field in (
        "schema_version",
        "record_type",
        "status",
        "decision",
        "base",
        "head",
        "classification_sha256",
        "readiness_sha256",
        "readiness_provenance",
        "readiness_provenance_sha256",
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
    if admission.get("decision") == "readiness":
        provenance = admission.get("readiness_provenance")
        if not isinstance(provenance, Mapping):
            raise ReleaseAdmissionError("readiness provenance is missing")
        reference = provenance.get("evidence_reference")
        if not isinstance(reference, str):
            raise ReleaseAdmissionError("readiness evidence reference is missing")
        return (
            f"release admission: readiness_required head={admission.get('head')}\n"
            f"readiness evidence: {reference}"
        )
    if admission.get("decision") == "exemption":
        return (
            f"release admission: unrelated_release_exemption "
            f"head={admission.get('head')}"
        )
    raise ReleaseAdmissionError("release admission decision is invalid")


def validate_release_classification(
    classification: Mapping[str, object], *, repo: Path
) -> tuple[str, ...]:
    base = classification.get("base")
    head = classification.get("head")
    if (
        not isinstance(base, str)
        or not isinstance(head, str)
        or not FULL_COMMIT_PATTERN.fullmatch(base)
        or not FULL_COMMIT_PATTERN.fullmatch(head)
    ):
        return ("classification base/head revisions are missing or malformed",)
    try:
        actual = classify_release_changes(repo, base=base, head=head)
    except ReleaseAdmissionError as exc:
        return (f"classification cannot be reproduced: {exc}",)
    diagnostics = []
    for field in (
        "schema_version",
        "record_type",
        "ownership_contract_version",
        "base",
        "head",
        "status",
        "changed_paths",
        "owned_paths",
        "uncertainty",
    ):
        if classification.get(field) != actual.get(field):
            diagnostics.append(f"classification {field} does not match Git history")
    return tuple(diagnostics)


def mapping_sha256(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
