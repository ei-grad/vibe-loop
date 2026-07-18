from __future__ import annotations

import json
from pathlib import Path


def record_event(kind: str, task_id: str) -> None:
    path = Path(".vibe-loop/hook-events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "task_id": task_id}) + "\n")
