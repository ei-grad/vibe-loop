from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_SCHEMA_VERSION = 1
RUN_RECORD_TYPE = "run_result"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    classification: str
    exit_code: int
    log_path: Path
    start_main: str
    end_main: str
    message: str = ""
    finished_at: str = dataclasses.field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "classification": self.classification,
            "exit_code": self.exit_code,
            "log": str(self.log_path),
            "start_main": self.start_main,
            "end_main": self.end_main,
            "message": self.message,
            "finished_at": self.finished_at,
        }

    def to_record(self) -> dict[str, object]:
        record = self.to_json()
        record.update(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "record_type": RUN_RECORD_TYPE,
                "status": self.classification,
            }
        )
        return record


class RunStore:
    def __init__(self, path: Path):
        self.path = path

    def append_result(self, result: RunResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.to_record(), separators=(",", ":")) + "\n")

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def recent_records(self, max_runs: int = 5) -> list[dict[str, Any]]:
        return self.read_records()[-max_runs:]

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        records = self.recent_records(max_runs)
        if not records:
            return "No prior vibe-loop runs recorded."
        chunks = ["Recent vibe-loop runs:"]
        for record in records:
            chunks.append(json.dumps(record, sort_keys=True))
            log_path = record_log_path(record)
            if log_path is not None:
                chunks.append(f"Log tail for {log_path}:")
                chunks.extend(tail(log_path, tail_lines))
        return "\n".join(chunks)


def record_log_path(record: dict[str, Any]) -> Path | None:
    record_type = record.get("record_type")
    if record_type not in {None, RUN_RECORD_TYPE}:
        return None
    log = record.get("log")
    if not isinstance(log, str) or not log:
        return None
    path = Path(log)
    if not path.is_file():
        return None
    return path


def tail(path: Path, line_count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-line_count:]
