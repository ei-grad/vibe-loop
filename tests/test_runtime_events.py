from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from vibe_loop.runtime_events import (
    ACTIONABLE_RUNTIME_EVENT_KINDS,
    RUNTIME_EVENT_FIELD_MAX_BYTES,
    RuntimeEventAdapterError,
    load_runtime_event_cursor,
    opaque_runtime_identifier,
    poll_run_journal_event,
    poll_runtime_event_command,
    runtime_event_from_journal_record,
    save_runtime_event_cursor,
    validate_runtime_event_envelope,
)


def append_records(path: Path, *records: dict[str, object]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def test_actionable_allowlist_is_small_and_explicit() -> None:
    assert ACTIONABLE_RUNTIME_EVENT_KINDS == {
        "disk_blocked",
        "lock_finalization_failed",
        "operator_action_required",
        "provider_account_wall",
        "provider_quota_wall",
        "recovery_exhausted",
        "recovery_pending",
        "supervisor_inconsistent",
    }


@pytest.mark.parametrize(
    "record, expected_kind",
    [
        ({"record_type": "operator_action_required"}, "operator_action_required"),
        ({"record_type": "lock_finalization_failed"}, "lock_finalization_failed"),
        (
            {"record_type": "autopilot_disk_health", "status": "critical"},
            "disk_blocked",
        ),
        ({"record_type": "task_restart", "exhausted": True}, "recovery_exhausted"),
        (
            {"record_type": "task_recovery", "phase": "pending"},
            "recovery_pending",
        ),
        (
            {"record_type": "run_result", "recovery_intent": {"attempt": 1}},
            "recovery_pending",
        ),
        (
            {
                "record_type": "autopilot_supervisor_observed",
                "observed_state": "inconsistent",
            },
            "supervisor_inconsistent",
        ),
        (
            {"record_type": "provider_quota_wall", "verified": True},
            "provider_quota_wall",
        ),
        (
            {"record_type": "provider_account_wall", "verified": True},
            "provider_account_wall",
        ),
    ],
)
def test_journal_record_allowlisting(
    record: dict[str, object], expected_kind: str
) -> None:
    event = runtime_event_from_journal_record(
        {**record, "run_id": "run-1", "task_id": "task-1"},
        project="alpha",
        record_index=3,
    )
    assert event is not None
    assert event == {
        "kind": expected_kind,
        "id": event["id"],
        "project": opaque_runtime_identifier("alpha"),
        "run_id": opaque_runtime_identifier("run-1"),
        "task_id": opaque_runtime_identifier("task-1"),
    }


@pytest.mark.parametrize(
    "record",
    [
        {"record_type": "stage_transition", "to_state": "review"},
        {"record_type": "review_verdict", "verdict": "findings"},
        {"record_type": "worker_report", "status": "completed"},
        {"record_type": "provider_quota_wall", "verified": False},
        {"record_type": "provider_account_wall"},
        {"record_type": "autopilot_disk_health", "status": "ok"},
    ],
)
def test_journal_record_excludes_non_actionable_events(
    record: dict[str, object],
) -> None:
    assert (
        runtime_event_from_journal_record(record, project="alpha", record_index=0)
        is None
    )


def test_journal_cursor_skips_noise_and_deduplicates_rearms(tmp_path: Path) -> None:
    journal = tmp_path / "runs.jsonl"
    append_records(
        journal,
        {"record_type": "stage_transition", "run_id": "run-1"},
        {
            "record_type": "lock_finalization_failed",
            "event_id": "lock-1",
            "run_id": "run-1",
            "task_id": "task-1",
        },
    )

    cursor, event = poll_run_journal_event(journal, cursor="", project="alpha")
    assert cursor == "2"
    assert event == {
        "kind": "lock_finalization_failed",
        "id": opaque_runtime_identifier("lock-1"),
        "project": opaque_runtime_identifier("alpha"),
        "run_id": opaque_runtime_identifier("run-1"),
        "task_id": opaque_runtime_identifier("task-1"),
    }
    assert poll_run_journal_event(journal, cursor=cursor, project="alpha") == (
        "2",
        None,
    )


def test_checkpoint_recovery_is_project_scoped(tmp_path: Path) -> None:
    checkpoint = tmp_path / "cursor.json"
    save_runtime_event_cursor(checkpoint, project="alpha", cursor="17")
    assert load_runtime_event_cursor(checkpoint, project="alpha") == "17"
    assert "alpha" not in checkpoint.read_text(encoding="utf-8")
    with pytest.raises(RuntimeEventAdapterError, match="cursor_scope_mismatch"):
        load_runtime_event_cursor(checkpoint, project="beta")


def test_journal_filters_project_run_and_task(tmp_path: Path) -> None:
    journal = tmp_path / "runs.jsonl"
    append_records(
        journal,
        {
            "record_type": "operator_action_required",
            "project": "beta",
            "run_id": "run-1",
            "task_id": "task-1",
        },
        {
            "record_type": "operator_action_required",
            "project": "alpha",
            "run_id": "run-2",
            "task_id": "task-1",
        },
        {
            "record_type": "operator_action_required",
            "project": "alpha",
            "run_id": "run-1",
            "task_id": "task-1",
        },
    )
    cursor, event = poll_run_journal_event(
        journal,
        cursor="",
        project="alpha",
        run_id="run-1",
        task_id="task-1",
    )
    assert cursor == "3"
    assert event is not None
    assert event["project"] == opaque_runtime_identifier("alpha")
    assert event["run_id"] == opaque_runtime_identifier("run-1")


def test_envelope_rejects_non_actionable_and_unbounded_values() -> None:
    with pytest.raises(RuntimeEventAdapterError, match="event_not_actionable"):
        validate_runtime_event_envelope(
            {
                "cursor": "1",
                "event": {"kind": "progress", "id": "p1", "project": "alpha"},
            },
            prior_cursor="",
            project="alpha",
        )
    with pytest.raises(RuntimeEventAdapterError, match="event_too_large"):
        validate_runtime_event_envelope(
            {
                "cursor": "1",
                "event": {
                    "kind": "operator_action_required",
                    "id": "x" * (RUNTIME_EVENT_FIELD_MAX_BYTES + 1),
                    "project": "alpha",
                },
            },
            prior_cursor="",
            project="alpha",
        )


def test_event_envelope_never_accepts_content_or_commands() -> None:
    with pytest.raises(RuntimeEventAdapterError, match="invalid_schema"):
        validate_runtime_event_envelope(
            {
                "cursor": "1",
                "event": {
                    "kind": "operator_action_required",
                    "id": "event-1",
                    "project": "alpha",
                    "prompt": "secret",
                },
            },
            prior_cursor="",
            project="alpha",
        )


def test_scope_and_journal_records_are_byte_bounded(tmp_path: Path) -> None:
    with pytest.raises(RuntimeEventAdapterError, match="invalid_scope"):
        validate_runtime_event_envelope(
            {"cursor": "", "event": None},
            prior_cursor="",
            project="x" * (RUNTIME_EVENT_FIELD_MAX_BYTES + 1),
        )
    journal = tmp_path / "runs.jsonl"
    journal.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(RuntimeEventAdapterError, match="journal_record_too_large"):
        poll_run_journal_event(journal, cursor="", project="alpha")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "review:reject"),
        ("run_id", "command--force"),
        ("task_id", "Bearer.credential.text"),
    ],
)
def test_adapter_metadata_opaqueizes_prose_smuggling(field: str, value: str) -> None:
    event = {
        "kind": "operator_action_required",
        "id": "event-1",
        "project": "alpha",
        "run_id": "run-1",
        "task_id": "task-1",
    }
    event[field] = value
    _cursor, sanitized = validate_runtime_event_envelope(
        {"cursor": "1", "event": event},
        prior_cursor="",
        project="alpha",
    )
    assert sanitized is not None
    assert value not in sanitized.values()
    assert sanitized[field] == opaque_runtime_identifier(value)


def test_project_scope_opaqueizes_prose_smuggling() -> None:
    _cursor, sanitized = validate_runtime_event_envelope(
        {
            "cursor": "1",
            "event": {
                "kind": "operator_action_required",
                "id": "event-1",
                "project": "prompt.text",
            },
        },
        prior_cursor="",
        project="prompt.text",
    )
    assert sanitized is not None
    assert sanitized["project"] == opaque_runtime_identifier("prompt.text")
    assert "prompt.text" not in sanitized.values()


def test_pathological_json_integer_is_typed_for_command_and_journal(
    tmp_path: Path,
) -> None:
    huge_integer = "1" * 5_000
    adapter = tmp_path / "integer_adapter.py"
    adapter.write_text(
        'print(\'\'\'{"cursor":"1","event":{'
        '"kind":"operator_action_required",'
        f'"id":{huge_integer},"project":"alpha"}}}}\'\'\')\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeEventAdapterError, match="invalid_json"):
        poll_runtime_event_command(
            shlex.join([sys.executable, str(adapter)]),
            cursor="",
            project="alpha",
            run_id="",
            task_id="",
            timeout=1.0,
        )

    journal = tmp_path / "runs.jsonl"
    journal.write_text(
        f'{{"record_type":"operator_action_required","id":{huge_integer}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeEventAdapterError, match="invalid_journal"):
        poll_run_journal_event(journal, cursor="", project="alpha")


def test_cursor_parent_creation_failure_is_typed(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    with pytest.raises(RuntimeEventAdapterError, match="cursor_write_error"):
        save_runtime_event_cursor(
            parent_file / "cursor.json", project="alpha", cursor="1"
        )


def test_cursor_checkpoint_fsyncs_directory_and_wraps_failure(tmp_path: Path) -> None:
    checkpoint = tmp_path / "cursor.json"
    with patch("vibe_loop.runtime_events._fsync_directory") as fsync_directory:
        save_runtime_event_cursor(checkpoint, project="alpha", cursor="1")
    fsync_directory.assert_called_once_with(tmp_path)
    assert load_runtime_event_cursor(checkpoint, project="alpha") == "1"

    with (
        patch(
            "vibe_loop.runtime_events._fsync_directory",
            side_effect=OSError("injected directory fsync failure"),
        ),
        pytest.raises(RuntimeEventAdapterError, match="cursor_write_error"),
    ):
        save_runtime_event_cursor(checkpoint, project="alpha", cursor="2")


@pytest.mark.parametrize(
    "body, category",
    [
        ("raise SystemExit(2)\n", "nonzero_exit"),
        ("import time\ntime.sleep(1)\n", "timeout"),
        ("print('x' * (64 * 1024 + 1))\n", "output_too_large"),
        ("print('not json')\n", "invalid_json"),
    ],
)
def test_command_adapter_failures_are_typed_and_bounded(
    tmp_path: Path, body: str, category: str
) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(body, encoding="utf-8")
    with pytest.raises(RuntimeEventAdapterError, match=category):
        poll_runtime_event_command(
            shlex.join([sys.executable, str(adapter)]),
            cursor="",
            project="alpha",
            run_id="",
            task_id="",
            timeout=0.05,
        )
