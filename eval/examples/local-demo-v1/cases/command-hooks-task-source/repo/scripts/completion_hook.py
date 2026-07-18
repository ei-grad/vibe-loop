from __future__ import annotations

import json
from pathlib import Path

from hook_events import record_event


def main() -> int:
    tasks = json.loads(Path("tasks.json").read_text(encoding="utf-8"))["tasks"]
    task = next(item for item in tasks if item["id"] == "HOOK-02")
    if task["status"] != "Done":
        raise SystemExit("HOOK-02 is not complete in the authoritative task source")
    record_event("completion", "HOOK-02")
    print(json.dumps({"hook": "completion", "task_id": "HOOK-02", "validated": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
