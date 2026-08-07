from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from importlib.metadata import (
    PackageNotFoundError,
    distribution as metadata_distribution,
)
from pathlib import Path

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]


MANIFEST_NAME = ".skill-manifest.json"
MANIFEST_VERSION = 1
PUBLISH_LOCK_NAME = ".skill-deploy.lock"
PUBLISH_LOCK_TIMEOUT_SECONDS = 5.0
PUBLISH_LOCK_POLL_SECONDS = 0.01
RUNTIME_TARGETS = (".codex", ".claude")
BLOCKING_STATES = frozenset({"stale", "runtime-edited", "branch-sourced"})
# A stale deployment still matches its manifest; only the recorded source moved
# on. That is the expected state between merging a bundled-skill change and
# refreshing the deployment, and it is repaired by the post-integration refresh
# rather than by an operator. Blocking worker launch on it would halt every
# board on the host until someone reinstalls by hand, so preflight blocks only
# where the installed content or its provenance is unknown.
WORKER_BLOCKING_STATES = frozenset({"runtime-edited", "branch-sourced"})
WORKER_ADVISORY_STATES = BLOCKING_STATES - WORKER_BLOCKING_STATES


class SkillDeploymentError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclasses.dataclass(frozen=True)
class SourceState:
    repo: str
    content_root: Path
    commit: str
    branch: str
    dirty: bool


@dataclasses.dataclass(frozen=True)
class VerificationEntry:
    target_root: Path
    relative_path: str
    state: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        payload = {
            "target_root": str(self.target_root),
            "path": self.relative_path,
            "state": self.state,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclasses.dataclass(frozen=True)
class VerificationReport:
    target_root: Path
    entries: tuple[VerificationEntry, ...]
    unmanaged: tuple[str, ...]
    manifest_error: str = ""

    @property
    def drifted(self) -> bool:
        return bool(
            self.manifest_error
            or any(entry.state != "in-sync" for entry in self.entries)
        )

    @property
    def blocking(self) -> bool:
        return bool(
            self.manifest_error
            or any(entry.state in BLOCKING_STATES for entry in self.entries)
        )

    @property
    def worker_blocking(self) -> bool:
        return bool(
            self.manifest_error
            or any(entry.state in WORKER_BLOCKING_STATES for entry in self.entries)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "target_root": str(self.target_root),
            "manifest": str(self.target_root / MANIFEST_NAME),
            "manifest_error": self.manifest_error,
            "entries": [entry.to_json() for entry in self.entries],
            "unmanaged": list(self.unmanaged),
            "drifted": self.drifted,
            "blocking": self.blocking,
            "worker_blocking": self.worker_blocking,
        }


def target_roots(home: Path) -> tuple[Path, Path]:
    return (
        home / RUNTIME_TARGETS[0] / "skills",
        home / RUNTIME_TARGETS[1] / "skills",
    )


def publish_lock_path(target_root: Path) -> Path:
    """Publish mutex for one runtime root.

    It sits beside the root rather than inside it so the verifier keeps
    reporting exactly the deployed tree and never counts the lock as unmanaged.
    """
    return target_root.parent / PUBLISH_LOCK_NAME


@contextmanager
def publishing_target_root(target_root: Path) -> Iterator[None]:
    """Hold the publish mutex while one runtime root is rewritten.

    Managed files are replaced one at a time and the manifest is written last,
    so a reader landing mid-publish would see installed content that no longer
    matches the recorded digest and classify the root as `runtime-edited`. That
    became reachable without an operator present once integration started
    refreshing deployments, so writers take this lock and readers wait for it.
    """
    path = publish_lock_path(target_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _lock_handle(handle, exclusive=True)
        try:
            yield
        finally:
            _unlock_handle(handle)


@contextmanager
def reading_target_root(target_root: Path) -> Iterator[None]:
    """Wait for an in-flight publish before reading one runtime root.

    Verification stays read-only: a root that no writer has ever locked has no
    lock file and is read directly. A writer that outlives the timeout is worse
    than a torn read, so the wait is bounded and then abandoned.
    """
    path = publish_lock_path(target_root)
    if fcntl is None or not path.is_file():
        yield
        return
    try:
        handle = path.open("rb")
    except OSError:
        yield
        return
    with handle:
        locked = _lock_handle_until(handle, PUBLISH_LOCK_TIMEOUT_SECONDS)
        try:
            yield
        finally:
            if locked:
                _unlock_handle(handle)


def _lock_handle(handle, *, exclusive: bool) -> None:
    if fcntl is None:  # pragma: no cover - platform dependent
        return
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0 and exclusive:
        handle.write(b"\0")
        handle.flush()
    fcntl.flock(
        handle.fileno(),
        fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
    )


def _lock_handle_until(handle, timeout_seconds: float) -> bool:
    if fcntl is None:  # pragma: no cover - platform dependent
        return False
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(PUBLISH_LOCK_POLL_SECONDS)
        else:
            return True


def _unlock_handle(handle) -> None:
    if fcntl is None:  # pragma: no cover - platform dependent
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def selected_target_roots(
    home: Path,
    *,
    codex: bool,
    claude: bool,
) -> tuple[Path, ...]:
    codex_root, claude_root = target_roots(home)
    if codex and not claude:
        return (codex_root,)
    if claude and not codex:
        return (claude_root,)
    return (codex_root, claude_root)


def inspect_source(
    source_root: Path,
    *,
    package_name: str | None = None,
    source_repository: str | None = None,
    main_branch: str = "main",
) -> SourceState:
    source_root = source_root.resolve()
    repo = _git_toplevel(source_root)
    if repo is None:
        if package_name is None:
            raise SkillDeploymentError(
                "skill source is not in a Git checkout and no package "
                "provenance was provided"
            )
        return _package_source_state(
            source_root,
            package_name=package_name,
            source_repository=source_repository,
            main_branch=main_branch,
        )
    commit = _git(source_root, "rev-parse", "--verify", "HEAD")
    branch = _git(source_root, "branch", "--show-current")
    dirty = bool(_git(source_root, "status", "--porcelain", "--untracked-files=all"))
    repo_path = Path(repo).resolve()
    return SourceState(
        repo=str(repo_path),
        content_root=repo_path,
        commit=commit,
        branch=branch,
        dirty=dirty,
    )


def deploy_skill_bundle(
    *,
    source_root: Path,
    skill_names: Iterable[str],
    home: Path,
    codex: bool = False,
    claude: bool = False,
    force: bool = False,
    allow_unmerged: bool = False,
    main_branch: str = "main",
    package_name: str | None = None,
    source_repository: str | None = None,
    report_diagnostic: Callable[[str], None] | None = None,
    roots: Sequence[Path] | None = None,
) -> list[Path]:
    """Install `skill_names` into this home's runtime roots.

    An explicit `roots` writes exactly those runtime roots instead of both, so
    a caller repairing an existing deployment does not create one the operator
    never installed. It also selects the reported paths, replacing `codex` and
    `claude`, which only filter the report of an install that wrote both.
    """
    source_root = source_root.resolve()
    names = tuple(skill_names)
    write_roots = target_roots(home) if roots is None else _requested_roots(home, roots)
    source_state = inspect_source(
        source_root,
        package_name=package_name,
        source_repository=source_repository,
        main_branch=main_branch,
    )
    if (
        source_state.branch != main_branch or source_state.dirty
    ) and not allow_unmerged:
        conditions = []
        if source_state.branch != main_branch:
            conditions.append(
                f"branch is {source_state.branch or '<detached>'}, expected {main_branch}"
            )
        if source_state.dirty:
            conditions.append("source tree is dirty")
        raise SkillDeploymentError(
            "refusing to install skills from a non-mainline or dirty source tree: "
            + "; ".join(conditions)
        )

    source_files = _source_files(source_root, names)
    installed_at = datetime.now(timezone.utc).isoformat()
    manifests: dict[Path, dict[str, object]] = {}
    diagnostics: list[str] = []
    with ExitStack() as publish:
        # Locks are taken in one fixed order and held across the overwrite
        # check as well as the write, so a concurrent installer can neither
        # interleave a publish nor invalidate the check that authorized it.
        for root in write_roots:
            publish.enter_context(publishing_target_root(root))
        _publish_skill_bundle(
            source_files=source_files,
            source_state=source_state,
            names=names,
            write_roots=write_roots,
            manifests=manifests,
            diagnostics=diagnostics,
            installed_at=installed_at,
            main_branch=main_branch,
            force=force,
            report_diagnostic=report_diagnostic,
        )

    reported_roots = (
        write_roots
        if roots is not None
        else selected_target_roots(home, codex=codex, claude=claude)
    )
    return [root / name for root in reported_roots for name in names]


def _requested_roots(home: Path, roots: Sequence[Path]) -> tuple[Path, ...]:
    # Callers derive roots from verification reports, which carry resolved
    # paths, so match on the resolved form and write the home-relative one.
    known = {root.resolve(): root for root in target_roots(home)}
    selected: list[Path] = []
    unknown: list[str] = []
    for root in roots:
        match = known.get(Path(root).resolve())
        if match is None:
            unknown.append(str(root))
        elif match not in selected:
            selected.append(match)
    if unknown:
        raise SkillDeploymentError(
            "refusing to install into a path that is not a runtime root of this "
            f"home: {', '.join(sorted(unknown))}"
        )
    return tuple(root for root in target_roots(home) if root in selected)


def _publish_skill_bundle(
    *,
    source_files: dict[str, Path],
    source_state: SourceState,
    names: Sequence[str],
    write_roots: Sequence[Path],
    manifests: dict[Path, dict[str, object]],
    diagnostics: list[str],
    installed_at: str,
    main_branch: str,
    force: bool,
    report_diagnostic: Callable[[str], None] | None,
) -> None:
    for root in write_roots:
        try:
            manifest = _load_manifest(root, allow_missing=True)
        except SkillDeploymentError as exc:
            diagnostic = (
                f"{root / MANIFEST_NAME}: {exc}; "
                "the manifest will be replaced because --force was supplied"
            )
            if not force:
                raise SkillDeploymentError(
                    "refusing to replace an invalid skill manifest; rerun with "
                    "--force after reviewing the error",
                    diagnostics=(diagnostic,),
                ) from exc
            diagnostics.append(diagnostic)
            manifest = {"version": MANIFEST_VERSION, "entries": {}}
        manifests[root] = manifest
        entries = _manifest_entries(manifest)
        for relative_path, source in source_files.items():
            target = root / relative_path
            recorded = entries.get(relative_path)
            diagnostics.extend(
                _overwrite_diagnostics(
                    source=source,
                    target=target,
                    recorded=recorded,
                    relative_path=relative_path,
                    target_root=root,
                    source_repo=source_state.repo,
                )
            )

    if diagnostics and not force:
        raise SkillDeploymentError(
            "refusing to overwrite installed skill files that differ from "
            "their recorded deployment; rerun with --force after reviewing "
            "the diff",
            diagnostics=diagnostics,
        )
    if report_diagnostic is not None:
        for diagnostic in diagnostics:
            report_diagnostic(diagnostic)

    for root in write_roots:
        root.mkdir(parents=True, exist_ok=True)
        manifest = manifests[root]
        old_entries = _manifest_entries(manifest)
        entries = {
            path: entry
            for path, entry in old_entries.items()
            if not _owned_path(
                path,
                entry,
                source_repo=source_state.repo,
                skill_names=names,
            )
        }
        for relative_path, source in source_files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{_scratch_suffix()}")
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            entries[relative_path] = {
                "source_repo": str(source_state.repo),
                "source_commit": source_state.commit,
                "source_branch": source_state.branch,
                "source_main_branch": main_branch,
                "source_dirty": source_state.dirty,
                "source_location": str(source_state.content_root),
                "source_path": str(source.relative_to(source_state.content_root)),
                "sha256": _sha256(source),
                "installed_at": installed_at,
            }
        payload = {
            "version": MANIFEST_VERSION,
            "installed_at": installed_at,
            "entries": dict(sorted(entries.items())),
        }
        _write_manifest(root, payload)


def verify_skill_deployments(
    home: Path,
    *,
    codex: bool = False,
    claude: bool = False,
) -> tuple[VerificationReport, ...]:
    return tuple(
        verify_target_root(root)
        for root in selected_target_roots(home, codex=codex, claude=claude)
    )


def verify_worker_skill_deployments(home: Path) -> tuple[VerificationReport, ...]:
    reports = []
    for root in target_roots(home):
        if not root.exists():
            continue
        report = verify_target_root(root)
        if report.manifest_error == "manifest missing":
            continue
        reports.append(report)
    return tuple(reports)


def worker_launch_verdict(
    reports: Sequence[VerificationReport],
) -> tuple[tuple[VerificationReport, ...], tuple[str, ...]]:
    """Split worker preflight reports into blocking reports and advisory lines.

    Callers must refuse to launch when the blocking tuple is non-empty and
    surface the advisory lines otherwise; see `WORKER_BLOCKING_STATES`.
    """
    blocking = tuple(report for report in reports if report.worker_blocking)
    advisories = tuple(
        f"{entry.target_root / entry.relative_path}: {entry.state}"
        + (f": {entry.detail}" if entry.detail else "")
        for report in reports
        for entry in report.entries
        if entry.state in WORKER_ADVISORY_STATES
    )
    return blocking, advisories


def stale_deployment_entries(
    reports: Sequence[VerificationReport],
    *,
    skill_names: Iterable[str],
) -> tuple[VerificationEntry, ...]:
    """Entries whose installed copy still matches a manifest the source left."""
    names = frozenset(skill_names)
    return tuple(
        entry
        for report in reports
        for entry in report.entries
        if entry.state == "stale" and entry.relative_path.split("/", 1)[0] in names
    )


def deployment_drift_advisories(
    home: Path,
    *,
    source_root: Path,
    skill_names: Iterable[str],
) -> tuple[dict[str, object], ...]:
    names = tuple(skill_names)
    bundled_names = frozenset(names)
    bundled_source_paths = frozenset(_source_files(source_root.resolve(), names))
    deployments: list[dict[str, object]] = []
    affected_skills: set[str] = set()
    for report in verify_skill_deployments(home):
        differences: list[dict[str, str]] = []
        root_skills: set[str] = set()
        for entry in report.entries:
            skill_name = _bundled_skill_name(entry.relative_path, bundled_names)
            if skill_name is None or entry.state == "in-sync":
                continue
            difference = {
                "skill": skill_name,
                "path": entry.relative_path,
                "state": entry.state,
            }
            if entry.detail:
                difference["detail"] = entry.detail
            differences.append(difference)
            root_skills.add(skill_name)
        for relative_path in report.unmanaged:
            if relative_path not in bundled_source_paths:
                continue
            skill_name = relative_path.split("/", 1)[0]
            differences.append(
                {
                    "skill": skill_name,
                    "path": relative_path,
                    "state": "unmanaged",
                }
            )
            root_skills.add(skill_name)
        if report.manifest_error and root_skills:
            manifest_state = (
                "manifest-missing"
                if report.manifest_error == "manifest missing"
                else "manifest-error"
            )
            differences.extend(
                {
                    "skill": skill_name,
                    "path": MANIFEST_NAME,
                    "state": manifest_state,
                    "detail": report.manifest_error,
                }
                for skill_name in sorted(root_skills)
            )
        if not differences:
            continue
        affected_skills.update(root_skills)
        deployments.append(
            {
                "target_root": str(report.target_root),
                "manifest_error": report.manifest_error,
                "differences": differences,
            }
        )

    if not deployments:
        return ()
    affected_names = sorted(affected_skills)
    return (
        {
            "code": "skill_deployment_drift",
            "severity": "warning",
            "affected_skills": affected_names,
            "deployments": deployments,
            "message": (
                "bundled skill deployments differ from their recorded source for "
                f"{', '.join(affected_names)}; run `vibe-loop verify-skills`, then "
                "reinstall from a clean main checkout"
            ),
        },
    )


def verify_target_root(target_root: Path) -> VerificationReport:
    target_root = target_root.resolve()
    with reading_target_root(target_root):
        try:
            manifest = _load_manifest(target_root, allow_missing=False)
        except SkillDeploymentError as exc:
            return VerificationReport(
                target_root=target_root,
                entries=(),
                unmanaged=_unmanaged_paths(target_root, frozenset()),
                manifest_error=str(exc),
            )

        manifest_entries = _manifest_entries(manifest)
        entries = tuple(
            _verify_entry(target_root, relative_path, recorded)
            for relative_path, recorded in sorted(manifest_entries.items())
        )
        return VerificationReport(
            target_root=target_root,
            entries=entries,
            unmanaged=_unmanaged_paths(target_root, frozenset(manifest_entries)),
        )


def render_verification_reports(
    reports: Sequence[VerificationReport],
) -> tuple[str, ...]:
    lines: list[str] = []
    for report in reports:
        if report.manifest_error:
            lines.append(f"{report.target_root}: unverifiable: {report.manifest_error}")
        for entry in report.entries:
            suffix = f": {entry.detail}" if entry.detail else ""
            lines.append(
                f"{entry.target_root / entry.relative_path}: {entry.state}{suffix}"
            )
        for relative_path in report.unmanaged:
            lines.append(f"{report.target_root / relative_path}: unmanaged")
    return tuple(lines)


def _verify_entry(
    target_root: Path,
    relative_path: str,
    recorded: dict[str, object],
) -> VerificationEntry:
    source_main_branch = recorded.get("source_main_branch", "main")
    if (
        recorded.get("source_branch") != source_main_branch
        or recorded.get("source_dirty") is True
    ):
        return VerificationEntry(
            target_root,
            relative_path,
            "branch-sourced",
            _source_description(recorded),
        )

    recorded_hash = recorded.get("sha256")
    source_repo = recorded.get("source_repo")
    source_location = recorded.get("source_location", source_repo)
    source_path = recorded.get("source_path")
    if not all(
        isinstance(value, str) and value
        for value in (recorded_hash, source_repo, source_location, source_path)
    ):
        return VerificationEntry(
            target_root,
            relative_path,
            "runtime-edited",
            "manifest entry is incomplete",
        )
    if not _is_safe_relative_path(source_path):
        return VerificationEntry(
            target_root,
            relative_path,
            "runtime-edited",
            "manifest source path is unsafe",
        )

    target = target_root / relative_path
    target_hash = _safe_sha256(target)
    if target_hash != recorded_hash:
        detail = "installed file is missing" if target_hash is None else "hash changed"
        return VerificationEntry(
            target_root,
            relative_path,
            "runtime-edited",
            detail,
        )

    source = Path(source_location) / source_path
    source_hash = _safe_sha256(source)
    if source_hash != recorded_hash:
        detail = "source file is missing" if source_hash is None else "source changed"
        return VerificationEntry(target_root, relative_path, "stale", detail)
    return VerificationEntry(target_root, relative_path, "in-sync")


def _source_description(recorded: dict[str, object]) -> str:
    branch = str(recorded.get("source_branch") or "<detached>")
    dirty = recorded.get("source_dirty") is True
    return f"recorded branch={branch}, dirty={'yes' if dirty else 'no'}"


def _source_files(source_root: Path, skill_names: Sequence[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for name in skill_names:
        skill_root = source_root / name
        if not skill_root.is_dir():
            raise SkillDeploymentError(
                f"bundled skill directory is missing: {skill_root}"
            )
        for source in sorted(skill_root.rglob("*")):
            if source.is_symlink():
                raise SkillDeploymentError(
                    f"refusing to deploy symlinked skill content: {source}"
                )
            if source.is_file():
                files[str(source.relative_to(source_root))] = source
    return files


def _overwrite_diagnostics(
    *,
    source: Path,
    target: Path,
    recorded: dict[str, object] | None,
    relative_path: str,
    target_root: Path,
    source_repo: str,
) -> list[str]:
    if not target.exists() and not target.is_symlink():
        return []
    recorded_repo = recorded.get("source_repo") if recorded is not None else None
    if isinstance(recorded_repo, str) and recorded_repo != source_repo:
        return [
            f"{target_root / relative_path}: ownership collision; "
            f"manifest owner={recorded_repo}, installing owner={source_repo}"
        ]
    actual_hash = _safe_sha256(target)
    source_hash = _sha256(source)
    recorded_hash = recorded.get("sha256") if recorded is not None else None
    expected_hash = recorded_hash if isinstance(recorded_hash, str) else source_hash
    if actual_hash == expected_hash:
        return []
    return [
        _render_diff(
            source=source,
            target=target,
            label=str(target_root / relative_path),
            actual_hash=actual_hash,
            expected_hash=expected_hash,
        )
    ]


def _render_diff(
    *,
    source: Path,
    target: Path,
    label: str,
    actual_hash: str | None,
    expected_hash: str,
) -> str:
    try:
        source_text = source.read_text(encoding="utf-8").splitlines()
        target_text = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return (
            f"{label}: installed sha256={actual_hash or '<missing>'}, "
            f"recorded sha256={expected_hash}"
        )
    diff = "\n".join(
        difflib.unified_diff(
            target_text,
            source_text,
            fromfile=f"{label} (installed)",
            tofile=f"{label} (source)",
            lineterm="",
        )
    )
    if diff:
        return diff
    return (
        f"{label}: installed sha256={actual_hash or '<missing>'}, "
        f"recorded sha256={expected_hash}"
    )


def _load_manifest(target_root: Path, *, allow_missing: bool) -> dict[str, object]:
    path = target_root / MANIFEST_NAME
    if not path.exists():
        if allow_missing:
            return {"version": MANIFEST_VERSION, "entries": {}}
        raise SkillDeploymentError("manifest missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillDeploymentError(f"manifest is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise SkillDeploymentError("manifest has an unsupported format")
    _manifest_entries(payload)
    return payload


def _manifest_entries(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, dict):
        raise SkillDeploymentError("manifest entries are invalid")
    entries: dict[str, dict[str, object]] = {}
    for path, entry in raw_entries.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            raise SkillDeploymentError("manifest entries are invalid")
        if not _is_safe_relative_path(path):
            raise SkillDeploymentError("manifest contains an unsafe path")
        entries[path] = entry
    return entries


def _write_manifest(target_root: Path, payload: dict[str, object]) -> None:
    path = target_root / MANIFEST_NAME
    temporary = target_root / f".{MANIFEST_NAME}.{_scratch_suffix()}"
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _scratch_suffix() -> str:
    # Deployment is now also driven automatically after integration, so two
    # runtimes can install concurrently. A shared scratch name would let one
    # writer publish the other's partially copied file.
    return f"{os.getpid()}.{uuid.uuid4().hex}.skill-deploy-new"


def _owned_path(
    path: str,
    entry: dict[str, object],
    *,
    source_repo: str,
    skill_names: Sequence[str],
) -> bool:
    first_component = path.split("/", 1)[0]
    return entry.get("source_repo") == source_repo and first_component in skill_names


def _unmanaged_paths(target_root: Path, managed: frozenset[str]) -> tuple[str, ...]:
    if not target_root.exists():
        return ()
    paths: list[str] = []
    for path in sorted(target_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = str(path.relative_to(target_root))
        if relative == MANIFEST_NAME or relative in managed:
            continue
        paths.append(relative)
    return tuple(paths)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_sha256(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return _sha256(path)
    except OSError:
        return None


def _is_safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _bundled_skill_name(
    relative_path: str,
    bundled_names: frozenset[str],
) -> str | None:
    skill_name = relative_path.split("/", 1)[0]
    return skill_name if skill_name in bundled_names else None


def _package_source_state(
    source_root: Path,
    *,
    package_name: str,
    source_repository: str | None,
    main_branch: str,
) -> SourceState:
    try:
        package = metadata_distribution(package_name)
    except PackageNotFoundError as exc:
        raise SkillDeploymentError(
            f"package provenance is unavailable for {package_name}"
        ) from exc

    repo = source_repository or f"package:{package_name}"
    commit = f"package:{package_name}@{package.version}"
    branch = main_branch
    dirty = False
    direct_url_text = package.read_text("direct_url.json")
    if direct_url_text:
        try:
            direct_url = json.loads(direct_url_text)
        except json.JSONDecodeError as exc:
            raise SkillDeploymentError(
                f"package provenance for {package_name} is unreadable"
            ) from exc
        if not isinstance(direct_url, dict):
            raise SkillDeploymentError(
                f"package provenance for {package_name} is invalid"
            )
        direct_repo = direct_url.get("url")
        if isinstance(direct_repo, str) and direct_repo:
            repo = direct_repo
        vcs_info = direct_url.get("vcs_info")
        if isinstance(vcs_info, dict):
            commit_id = vcs_info.get("commit_id")
            requested_revision = vcs_info.get("requested_revision")
            if isinstance(commit_id, str) and commit_id:
                commit = commit_id
            if isinstance(requested_revision, str) and requested_revision:
                branch = requested_revision
        dir_info = direct_url.get("dir_info")
        if isinstance(dir_info, dict):
            branch = "local-package"
            dirty = True

    return SourceState(
        repo=repo,
        content_root=source_root,
        commit=commit,
        branch=branch,
        dirty=dirty,
    )


def _git_toplevel(cwd: Path) -> str | None:
    completed = _run_git(cwd, "rev-parse", "--show-toplevel")
    if completed.returncode == 0:
        return completed.stdout.strip()
    detail = completed.stderr.strip() or completed.stdout.strip()
    if "not a git repository" in detail.lower():
        return None
    raise SkillDeploymentError(
        "could not inspect skill source Git state" + (f": {detail}" if detail else "")
    )


def _git(cwd: Path, *args: str) -> str:
    completed = _run_git(cwd, *args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SkillDeploymentError(
            "could not inspect skill source Git state"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout.strip()


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SkillDeploymentError(f"could not inspect skill source Git state: {exc}")
