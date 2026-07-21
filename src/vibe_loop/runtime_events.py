from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vibe_loop.config import prepare_shell_command


ACTIONABLE_RUNTIME_EVENT_KINDS = frozenset(
    {
        "disk_blocked",
        "lock_finalization_failed",
        "operator_action_required",
        "provider_account_wall",
        "provider_quota_wall",
        "recovery_exhausted",
        "supervisor_inconsistent",
    }
)
RUNTIME_EVENT_OUTPUT_MAX_BYTES = 64 * 1024
RUNTIME_EVENT_FIELD_MAX_BYTES = 1024
RUNTIME_EVENT_MAX_BYTES = 4096
RUNTIME_EVENT_CURSOR_MAX_BYTES = 1024
RUNTIME_EVENT_CHECKPOINT_SCHEMA_VERSION = 1


class RuntimeEventAdapterError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def load_runtime_event_cursor(path: Path, *, project: str) -> str:
    _require_scope(project=project)
    if not path.exists():
        return ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeEventAdapterError("cursor_read_error") from exc
    if len(raw) > RUNTIME_EVENT_MAX_BYTES:
        raise RuntimeEventAdapterError("cursor_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeEventAdapterError("invalid_cursor") from exc
    if not isinstance(payload, dict):
        raise RuntimeEventAdapterError("invalid_cursor")
    if payload.get("schema_version") != RUNTIME_EVENT_CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeEventAdapterError("invalid_cursor")
    if payload.get("project") != opaque_runtime_identifier(project):
        raise RuntimeEventAdapterError("cursor_scope_mismatch")
    cursor = payload.get("cursor")
    if not _bounded_string(cursor, allow_empty=True):
        raise RuntimeEventAdapterError("invalid_cursor")
    return cursor


def save_runtime_event_cursor(path: Path, *, project: str, cursor: str) -> None:
    _require_scope(project=project)
    if not _bounded_string(cursor, allow_empty=True):
        raise RuntimeEventAdapterError("invalid_cursor")
    payload = {
        "schema_version": RUNTIME_EVENT_CHECKPOINT_SCHEMA_VERSION,
        "project": opaque_runtime_identifier(project),
        "cursor": cursor,
    }
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeEventAdapterError("cursor_write_error") from exc


def poll_runtime_event_command(
    command: str,
    *,
    cursor: str,
    project: str,
    run_id: str,
    task_id: str,
    timeout: float,
) -> tuple[str, dict[str, object] | None]:
    _require_scope(project=project, run_id=run_id, task_id=task_id)
    environment = os.environ.copy()
    environment["VIBE_LOOP_WAIT_EVENT_CURSOR"] = cursor
    environment["VIBE_LOOP_WAIT_EVENT_PROJECT"] = project
    environment["VIBE_LOOP_WAIT_EVENT_RUN_ID"] = run_id
    environment["VIBE_LOOP_WAIT_EVENT_TASK_ID"] = task_id
    stdout = _bounded_command_output(command, environment=environment, timeout=timeout)
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeEventAdapterError("invalid_json") from exc
    return validate_runtime_event_envelope(
        payload,
        prior_cursor=cursor,
        project=project,
        run_id=run_id,
        task_id=task_id,
    )


def validate_runtime_event_envelope(
    payload: object,
    *,
    prior_cursor: str,
    project: str,
    run_id: str = "",
    task_id: str = "",
) -> tuple[str, dict[str, object] | None]:
    _require_scope(project=project, run_id=run_id, task_id=task_id)
    if not isinstance(payload, dict) or set(payload) != {"cursor", "event"}:
        raise RuntimeEventAdapterError("invalid_schema")
    cursor = payload.get("cursor")
    if not _bounded_string(cursor, allow_empty=True):
        raise RuntimeEventAdapterError("invalid_schema")
    event = payload.get("event")
    if event is None:
        return cursor, None
    if cursor == prior_cursor:
        raise RuntimeEventAdapterError("cursor_not_advanced")
    return cursor, validate_runtime_event(
        event,
        project=project,
        run_id=run_id,
        task_id=task_id,
    )


def validate_runtime_event(
    value: object,
    *,
    project: str,
    run_id: str = "",
    task_id: str = "",
) -> dict[str, object]:
    _require_scope(project=project, run_id=run_id, task_id=task_id)
    allowed_fields = {"kind", "id", "project", "run_id", "task_id"}
    if not isinstance(value, dict) or not set(value).issubset(allowed_fields):
        raise RuntimeEventAdapterError("invalid_schema")
    kind = value.get("kind")
    event_id = value.get("id")
    event_project = value.get("project")
    if kind not in ACTIONABLE_RUNTIME_EVENT_KINDS:
        raise RuntimeEventAdapterError("event_not_actionable")
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        raise RuntimeEventAdapterError("invalid_schema")
    if not _bounded_string(event_project):
        raise RuntimeEventAdapterError("invalid_schema")
    if event_project != project:
        raise RuntimeEventAdapterError("event_scope_mismatch")
    normalized: dict[str, object] = {
        "kind": kind,
        "id": opaque_runtime_identifier(event_id),
        "project": opaque_runtime_identifier(event_project),
    }
    for key, expected in (("run_id", run_id), ("task_id", task_id)):
        field = value.get(key)
        if field is None:
            if expected:
                raise RuntimeEventAdapterError("event_scope_mismatch")
            field = ""
        if not _bounded_string(field, allow_empty=True):
            raise RuntimeEventAdapterError("invalid_schema")
        if expected and field != expected:
            raise RuntimeEventAdapterError("event_scope_mismatch")
        normalized[key] = opaque_runtime_identifier(field) if field else ""
    if isinstance(event_id, str):
        if not _bounded_string(event_id):
            raise RuntimeEventAdapterError("event_too_large")
    if (
        len(json.dumps(normalized, separators=(",", ":")).encode())
        > RUNTIME_EVENT_MAX_BYTES
    ):
        raise RuntimeEventAdapterError("event_too_large")
    return normalized


def poll_run_journal_event(
    journal: Path,
    *,
    cursor: str,
    project: str,
    run_id: str = "",
    task_id: str = "",
) -> tuple[str, dict[str, object] | None]:
    _require_scope(project=project, run_id=run_id, task_id=task_id)
    try:
        offset = int(cursor or "0")
    except ValueError as exc:
        raise RuntimeEventAdapterError("invalid_cursor") from exc
    if offset < 0:
        raise RuntimeEventAdapterError("invalid_cursor")
    if not journal.exists():
        return str(offset), None
    index = 0
    try:
        with journal.open("rb") as handle:
            while True:
                raw = handle.readline(RUNTIME_EVENT_OUTPUT_MAX_BYTES + 1)
                if not raw:
                    break
                if len(raw) > RUNTIME_EVENT_OUTPUT_MAX_BYTES:
                    raise RuntimeEventAdapterError("journal_record_too_large")
                if index < offset:
                    index += 1
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise RuntimeEventAdapterError("invalid_journal") from exc
                if not isinstance(record, dict):
                    raise RuntimeEventAdapterError("invalid_journal")
                event = runtime_event_from_journal_record(
                    record,
                    project=project,
                    record_index=index,
                    run_id=run_id,
                    task_id=task_id,
                )
                index += 1
                if event is not None:
                    return str(index), event
    except OSError as exc:
        raise RuntimeEventAdapterError("journal_read_error") from exc
    if offset > index:
        raise RuntimeEventAdapterError("cursor_out_of_range")
    return str(index), None


def runtime_event_from_journal_record(
    record: Mapping[str, Any],
    *,
    project: str,
    record_index: int,
    run_id: str = "",
    task_id: str = "",
) -> dict[str, object] | None:
    recorded_project = _string(record.get("project"))
    if recorded_project and recorded_project != project:
        return None
    recorded_run = _string(record.get("run_id"))
    recorded_task = _string(record.get("task_id"))
    if run_id and recorded_run != run_id:
        return None
    if task_id and recorded_task != task_id:
        return None
    kind = _journal_actionable_kind(record)
    if kind is None:
        return None
    event_id = record.get("event_id", record.get("id"))
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        identity = json.dumps(
            {
                "index": record_index,
                "kind": kind,
                "run_id": recorded_run,
                "task_id": recorded_task,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        event_id = f"journal:{record_index}:{digest}"
    event: dict[str, object] = {
        "kind": kind,
        "id": event_id,
        "project": project,
    }
    event["run_id"] = recorded_run
    event["task_id"] = recorded_task
    return validate_runtime_event(
        event,
        project=project,
        run_id=run_id,
        task_id=task_id,
    )


def _journal_actionable_kind(record: Mapping[str, Any]) -> str | None:
    record_type = _string(record.get("record_type"))
    if record_type in ACTIONABLE_RUNTIME_EVENT_KINDS:
        if record_type.startswith("provider_") and record.get("verified") is not True:
            return None
        return record_type
    if record_type == "autopilot_disk_health" and record.get("status") == "critical":
        return "disk_blocked"
    if (
        record_type in {"task_restart", "task_recovery"}
        and record.get("exhausted") is True
    ):
        return "recovery_exhausted"
    if (
        record_type == "autopilot_supervisor_observed"
        and record.get("observed_state") == "inconsistent"
    ):
        return "supervisor_inconsistent"
    return None


def _bounded_command_output(
    command: str,
    *,
    environment: dict[str, str],
    timeout: float,
) -> str:
    prepared, use_shell = prepare_shell_command(command)
    popen_kwargs: dict[str, object] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    deadline = time.monotonic() + max(timeout, 0.001)
    with tempfile.TemporaryFile() as buffer:
        try:
            process = subprocess.Popen(
                prepared,
                shell=use_shell,
                stdout=buffer,
                stderr=subprocess.DEVNULL,
                env=environment,
                **popen_kwargs,
            )
        except OSError as exc:
            raise RuntimeEventAdapterError("execution_error") from exc
        while True:
            return_code = process.poll()
            buffer.seek(0, os.SEEK_END)
            if buffer.tell() > RUNTIME_EVENT_OUTPUT_MAX_BYTES:
                _stop_adapter_process(process)
                raise RuntimeEventAdapterError("output_too_large")
            if return_code is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_adapter_process(process)
                raise RuntimeEventAdapterError("timeout")
            time.sleep(min(0.01, remaining))
        if return_code != 0:
            raise RuntimeEventAdapterError("nonzero_exit")
        buffer.seek(0)
        raw = buffer.read(RUNTIME_EVENT_OUTPUT_MAX_BYTES + 1)
    if len(raw) > RUNTIME_EVENT_OUTPUT_MAX_BYTES:
        raise RuntimeEventAdapterError("output_too_large")
    return raw.decode("utf-8", errors="replace")


def _stop_adapter_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait()


def _bounded_string(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value.encode("utf-8", errors="replace"))
        <= RUNTIME_EVENT_FIELD_MAX_BYTES
    )


def opaque_runtime_identifier(value: str | int) -> str:
    encoded = str(value).encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _require_scope(*, project: str, run_id: str = "", task_id: str = "") -> None:
    if not _bounded_string(project):
        raise RuntimeEventAdapterError("invalid_scope")
    if not _bounded_string(run_id, allow_empty=True):
        raise RuntimeEventAdapterError("invalid_scope")
    if not _bounded_string(task_id, allow_empty=True):
        raise RuntimeEventAdapterError("invalid_scope")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
