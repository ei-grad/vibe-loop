from __future__ import annotations

import json
from pathlib import Path

from hook_events import record_event


def main() -> int:
    tasks = json.loads(Path("tasks.json").read_text(encoding="utf-8"))["tasks"]
    runnable = [item["id"] for item in tasks if item["status"] == "Planned"]
    record_event("planning", "")
    print(json.dumps({"hook": "planning", "runnable_task_ids": runnable}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
