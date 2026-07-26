from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = ".skill-manifest.json"
MANIFEST_VERSION = 1
RUNTIME_TARGETS = (".codex", ".claude")
BLOCKING_STATES = frozenset({"stale", "runtime-edited", "branch-sourced"})


class SkillDeploymentError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclasses.dataclass(frozen=True)
class SourceState:
    repo: Path
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
            or self.unmanaged
            or any(entry.state != "in-sync" for entry in self.entries)
        )

    @property
    def blocking(self) -> bool:
        return bool(
            self.manifest_error
            or any(entry.state in BLOCKING_STATES for entry in self.entries)
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
        }


def target_roots(home: Path) -> tuple[Path, Path]:
    return (
        home / RUNTIME_TARGETS[0] / "skills",
        home / RUNTIME_TARGETS[1] / "skills",
    )


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


def inspect_source(source_root: Path) -> SourceState:
    source_root = source_root.resolve()
    repo = _git(source_root, "rev-parse", "--show-toplevel")
    commit = _git(source_root, "rev-parse", "--verify", "HEAD")
    branch = _git(source_root, "branch", "--show-current")
    dirty = bool(_git(source_root, "status", "--porcelain", "--untracked-files=all"))
    return SourceState(
        repo=Path(repo).resolve(),
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
    report_diagnostic: Callable[[str], None] | None = None,
) -> list[Path]:
    source_root = source_root.resolve()
    names = tuple(skill_names)
    source_state = inspect_source(source_root)
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
    roots = target_roots(home)
    manifests: dict[Path, dict[str, object]] = {}
    diagnostics: list[str] = []
    for root in roots:
        manifest = _load_manifest(root, allow_missing=True)
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

    for root in roots:
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
            temporary = target.with_name(f".{target.name}.skill-deploy-new")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            entries[relative_path] = {
                "source_repo": str(source_state.repo),
                "source_commit": source_state.commit,
                "source_branch": source_state.branch,
                "source_main_branch": main_branch,
                "source_dirty": source_state.dirty,
                "source_path": str(source.relative_to(source_state.repo)),
                "sha256": _sha256(source),
                "installed_at": installed_at,
            }
        payload = {
            "version": MANIFEST_VERSION,
            "installed_at": installed_at,
            "entries": dict(sorted(entries.items())),
        }
        _write_manifest(root, payload)

    reported_roots = selected_target_roots(home, codex=codex, claude=claude)
    return [root / name for root in reported_roots for name in names]


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


def verify_target_root(target_root: Path) -> VerificationReport:
    target_root = target_root.resolve()
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
    source_path = recorded.get("source_path")
    if not all(
        isinstance(value, str) and value
        for value in (recorded_hash, source_repo, source_path)
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

    source = Path(source_repo) / source_path
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
    source_repo: Path,
) -> list[str]:
    if not target.exists() and not target.is_symlink():
        return []
    recorded_repo = recorded.get("source_repo") if recorded is not None else None
    if isinstance(recorded_repo, str) and recorded_repo != str(source_repo):
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
    temporary = target_root / f".{MANIFEST_NAME}.new"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _owned_path(
    path: str,
    entry: dict[str, object],
    *,
    source_repo: Path,
    skill_names: Sequence[str],
) -> bool:
    first_component = path.split("/", 1)[0]
    return (
        entry.get("source_repo") == str(source_repo) and first_component in skill_names
    )


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


def _git(cwd: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SkillDeploymentError(f"could not inspect skill source Git state: {exc}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SkillDeploymentError(
            "could not inspect skill source Git state"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout.strip()
