from __future__ import annotations

import json
from pathlib import Path

from hook_events import record_event


def main() -> int:
    tasks = json.loads(Path("tasks.json").read_text(encoding="utf-8"))["tasks"]
    task = next(item for item in tasks if item["id"] == "HOOK-02")
    if task["status"] != "Done":
        raise SystemExit("worklog cannot accept a task that is not Done")
    destination = Path(".vibe-loop/worklog.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"task_id": "HOOK-02", "status": "recorded"}) + "\n",
        encoding="utf-8",
    )
    record_event("worklog", "HOOK-02")
    print(json.dumps({"hook": "worklog", "task_id": "HOOK-02", "recorded": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
