from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    payload = json.loads(Path("tasks.json").read_text(encoding="utf-8"))
    tasks = payload["tasks"]
    operation = sys.argv[1] if len(sys.argv) > 1 else "list"
    if operation == "list":
        print(json.dumps({"tasks": tasks}))
        return 0
    if operation == "probe" and len(sys.argv) == 3:
        task = next((item for item in tasks if item["id"] == sys.argv[2]), None)
        print(json.dumps(task))
        return 0
    raise SystemExit("usage: task_adapter.py list | probe TASK_ID")


if __name__ == "__main__":
    raise SystemExit(main())
