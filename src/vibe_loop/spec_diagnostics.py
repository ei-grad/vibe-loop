from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from vibe_loop.config import VibeConfig
from vibe_loop.generated_discovery import (
    is_secret_like_directory_name,
    is_secret_like_path,
    is_webhook_like_evidence_path,
    redact_manifest_text,
)
from vibe_loop.generated_profiles import resolve_runtime_task_source
from vibe_loop.tasks import Task, build_task_source


COMPLETION_EVIDENCE_NONE_VALUES = {
    "",
    "-",
    "n/a",
    "none",
    "not run",
    "not started",
    "todo",
    "tbd",
}


@dataclasses.dataclass(frozen=True)
class SpecDiagnostic:
    code: str
    severity: str
    message: str
    task_id: str = ""
    path: str = ""
    source: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def to_json(self) -> dict[str, object]:
        payload = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.task_id:
            payload["task_id"] = self.task_id
        if self.path:
            payload["path"] = self.path
        if self.source:
            payload["source"] = self.source
        return payload


class SpecExecutionGateError(RuntimeError):
    def __init__(self, report: dict[str, object]):
        self.report = report
        diagnostics = report.get("diagnostics")
        blocking = (
            [
                diagnostic
                for diagnostic in diagnostics
                if isinstance(diagnostic, dict)
                and diagnostic.get("severity") == "error"
            ]
            if isinstance(diagnostics, list)
            else []
        )
        summary = "; ".join(format_diagnostic_summary(item) for item in blocking[:3])
        if len(blocking) > 3:
            summary = f"{summary}; +{len(blocking) - 3} more"
        super().__init__(f"spec execution gate blocked: {summary}")


def build_spec_diagnostics_report(
    config: VibeConfig,
    *,
    task_source_runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    if task_source_runtime is not None and not task_source_runtime.get("usable"):
        details = tuple(
            str(item) for item in task_source_runtime.get("diagnostics", [])
        )
        message = "spec diagnostics require a usable task source"
        if details:
            message = f"{message}: {'; '.join(details)}"
        diagnostic = SpecDiagnostic(
            code="task_source_unusable",
            severity="error" if config.specs.enforces_execution else "warning",
            message=message,
            source=str(task_source_runtime.get("cache_path") or ""),
        )
        return spec_report_from_tasks(
            config,
            (),
            (diagnostic,),
            task_source_usable=False,
        )
    try:
        resolution = resolve_runtime_task_source(config)
        if command_backed_task_source(resolution.task_source):
            return command_backed_source_report(config)
        source = build_task_source(config.repo, resolution.task_source)
        tasks = source.list_tasks()
    # Task sources may be parser-backed or command-backed; diagnostics must
    # report source failures without launching repair agents or crashing doctor.
    except Exception as exc:
        diagnostic = SpecDiagnostic(
            code="task_source_unusable",
            severity="error" if config.specs.enforces_execution else "warning",
            message=f"spec diagnostics could not read task source: {exc}",
        )
        return spec_report_from_tasks(
            config,
            (),
            (diagnostic,),
            task_source_usable=False,
        )
    return spec_diagnostics_for_tasks(config, tasks)


def command_backed_task_source(task_source: object) -> bool:
    return bool(
        getattr(task_source, "type", "") == "command"
        or getattr(task_source, "list_command", None)
        or getattr(task_source, "next_command", None)
        or getattr(task_source, "probe_command", None)
    )


def command_backed_source_report(config: VibeConfig) -> dict[str, object]:
    if not config.specs.explicit_keys:
        return spec_report_from_tasks(config, ())
    diagnostic = SpecDiagnostic(
        code="command_task_source_unchecked",
        severity="error" if config.specs.enforces_execution else "warning",
        message=(
            "spec diagnostics skipped command-backed task source to avoid running "
            "task_source.list as a doctor/specs side effect"
        ),
    )
    return spec_report_from_tasks(config, (), (diagnostic,))


def spec_diagnostics_for_tasks(
    config: VibeConfig,
    tasks: Iterable[Task],
) -> dict[str, object]:
    task_tuple = tuple(tasks)
    diagnostics: list[SpecDiagnostic] = []
    approved_states = {state.casefold() for state in config.specs.approved_states}
    for task in task_tuple:
        has_spec_context = task.has_traceability
        if not task.requirement_ids and (
            has_spec_context or config.specs.require_requirement_coverage
        ):
            diagnostics.append(
                task_diagnostic(
                    config,
                    "missing_requirement_coverage",
                    "task has no linked requirement IDs",
                    task,
                )
            )

        approval = task.approval_state.strip()
        if approval and approval.casefold() not in approved_states:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "unapproved_spec",
                    f"task approval_state is {approval!r}",
                    task,
                )
            )
        elif not approval and config.specs.require_approved:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "missing_spec_approval",
                    "task has no approval_state",
                    task,
                )
            )

        if task.source_fingerprints:
            diagnostics.extend(fingerprint_diagnostics(config, task))
        elif config.specs.require_current_fingerprints:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "missing_source_fingerprint",
                    "task has no source_fingerprints",
                    task,
                )
            )

        if (
            task.done
            and completion_evidence_missing(task.evidence)
            and (has_spec_context or config.specs.require_completion_evidence)
        ):
            diagnostics.append(
                task_diagnostic(
                    config,
                    "completed_task_missing_evidence",
                    "completed task has no completion evidence",
                    task,
                )
            )
    return spec_report_from_tasks(config, task_tuple, tuple(diagnostics))


def ensure_spec_execution_gate(config: VibeConfig, tasks: Iterable[Task]) -> None:
    if not config.specs.enforces_execution:
        return
    report = spec_diagnostics_for_tasks(config, tasks)
    if int(report["blocking_count"]):
        raise SpecExecutionGateError(report)


def task_diagnostic(
    config: VibeConfig,
    code: str,
    message: str,
    task: Task,
    *,
    path: str = "",
) -> SpecDiagnostic:
    return SpecDiagnostic(
        code=code,
        severity=diagnostic_severity(config, code),
        message=message,
        task_id=task.task_id,
        path=path,
        source=task.source,
    )


def fingerprint_diagnostics(
    config: VibeConfig, task: Task
) -> tuple[SpecDiagnostic, ...]:
    diagnostics: list[SpecDiagnostic] = []
    for fingerprint in task.source_fingerprints:
        raw_path = fingerprint.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            diagnostics.append(
                task_diagnostic(
                    config,
                    "invalid_source_fingerprint",
                    "source_fingerprints entry is missing path",
                    task,
                )
            )
            continue
        path = raw_path.strip().replace("\\", "/")
        path_error = source_fingerprint_path_error(path)
        if path_error:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "unsafe_source_fingerprint_path",
                    path_error,
                    task,
                    path=redact_manifest_text(path),
                )
            )
            continue
        expected_sha = fingerprint.get("sha256")
        expected_size = fingerprint.get("size")
        expected_size_is_int = isinstance(expected_size, int) and not isinstance(
            expected_size, bool
        )
        if not isinstance(expected_sha, str) and not expected_size_is_int:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "invalid_source_fingerprint",
                    "source_fingerprints entry must include sha256 or size",
                    task,
                    path=path,
                )
            )
            continue
        source_path, source_path_error = current_source_path(config, path)
        if source_path_error:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "unsafe_source_fingerprint_path",
                    source_path_error,
                    task,
                    path=path,
                )
            )
            continue
        assert source_path is not None
        try:
            actual_size = source_path.stat().st_size
        except OSError as exc:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "missing_fingerprinted_source",
                    f"fingerprinted source file cannot be read: {exc}",
                    task,
                    path=path,
                )
            )
            continue
        if expected_size_is_int and expected_size != actual_size:
            diagnostics.append(
                task_diagnostic(
                    config,
                    "stale_source_fingerprint",
                    f"fingerprinted source size changed from {expected_size} to {actual_size}",
                    task,
                    path=path,
                )
            )
            continue
        if isinstance(expected_sha, str):
            actual_sha = sha256_file(source_path)
            if expected_sha != actual_sha:
                diagnostics.append(
                    task_diagnostic(
                        config,
                        "stale_source_fingerprint",
                        "fingerprinted source sha256 changed",
                        task,
                        path=path,
                    )
                )
    return tuple(diagnostics)


def current_source_path(config: VibeConfig, path: str) -> tuple[Path | None, str]:
    repo = config.repo.resolve()
    source_path = (config.repo / path).resolve()
    try:
        source_path.relative_to(repo)
    except ValueError:
        return None, "source fingerprint path resolves outside repository"
    if not source_path.is_file():
        return None, "fingerprinted source file is missing"
    return source_path, ""


def diagnostic_severity(config: VibeConfig, code: str) -> str:
    if code in {"unapproved_spec", "missing_spec_approval"}:
        return "error" if config.specs.require_approved else "warning"
    if code in {
        "invalid_source_fingerprint",
        "missing_fingerprinted_source",
        "missing_source_fingerprint",
        "stale_source_fingerprint",
        "unsafe_source_fingerprint_path",
    }:
        return "error" if config.specs.require_current_fingerprints else "warning"
    if code == "missing_requirement_coverage":
        return "error" if config.specs.require_requirement_coverage else "warning"
    if code == "completed_task_missing_evidence":
        return "error" if config.specs.require_completion_evidence else "warning"
    return "warning"


def source_fingerprint_path_error(path: str) -> str:
    pure_path = PurePosixPath(path)
    if (
        pure_path.is_absolute()
        or any(part in {"", ".."} for part in pure_path.parts)
        or not pure_path.parts
    ):
        return "source fingerprint path must be safe and repo-relative"
    if is_webhook_like_evidence_path(path):
        return "source fingerprint path is secret-like"
    if any(is_secret_like_directory_name(part) for part in pure_path.parts[:-1]):
        return "source fingerprint path contains a secret-like directory"
    if is_secret_like_path(Path(pure_path.name)):
        return "source fingerprint path is secret-like"
    return ""


def completion_evidence_missing(value: str) -> bool:
    normalized = value.strip().strip(".").casefold()
    return normalized in COMPLETION_EVIDENCE_NONE_VALUES


def spec_report_from_tasks(
    config: VibeConfig,
    tasks: Iterable[Task],
    diagnostics: Iterable[SpecDiagnostic],
    *,
    task_source_usable: bool = True,
) -> dict[str, object]:
    task_tuple = tuple(tasks)
    diagnostic_tuple = tuple(diagnostics)
    traceable_count = sum(1 for task in task_tuple if task.has_traceability)
    blocking = [diagnostic for diagnostic in diagnostic_tuple if diagnostic.blocking]
    if not task_source_usable:
        status = "task_source_unusable"
    elif blocking:
        status = "blocked"
    elif diagnostic_tuple:
        status = "issues"
    elif traceable_count == 0 and not config.specs.explicit_keys:
        status = "not_configured"
    else:
        status = "ok"
    return {
        "status": status,
        "configured": bool(config.specs.explicit_keys),
        "enforced": config.specs.enforces_execution,
        "task_source_usable": task_source_usable,
        "task_count": len(task_tuple),
        "traceable_task_count": traceable_count,
        "diagnostic_count": len(diagnostic_tuple),
        "blocking_count": len(blocking),
        "approved_states": list(config.specs.approved_states),
        "override_commands": list(config.specs.override_commands),
        "config": config.specs.to_json(),
        "diagnostics": [diagnostic.to_json() for diagnostic in diagnostic_tuple],
    }


def format_diagnostic_summary(diagnostic: dict[str, object]) -> str:
    parts = [str(diagnostic.get("code") or "diagnostic")]
    task_id = diagnostic.get("task_id")
    if task_id:
        parts.append(f"task={task_id}")
    path = diagnostic.get("path")
    if path:
        parts.append(f"path={path}")
    return " ".join(parts)


def render_spec_diagnostics(report: dict[str, object]) -> str:
    lines = [
        f"spec diagnostics: status={report.get('status')} "
        f"diagnostics={report.get('diagnostic_count')} "
        f"blocking={report.get('blocking_count')}"
    ]
    diagnostics = report.get("diagnostics")
    if isinstance(diagnostics, list):
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            summary = format_diagnostic_summary(diagnostic)
            message = diagnostic.get("message")
            severity = diagnostic.get("severity")
            lines.append(f"{severity}: {summary}: {message}")
    override_commands = report.get("override_commands")
    if isinstance(override_commands, list) and override_commands:
        lines.append("override commands:")
        lines.extend(f"- {command}" for command in override_commands)
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
