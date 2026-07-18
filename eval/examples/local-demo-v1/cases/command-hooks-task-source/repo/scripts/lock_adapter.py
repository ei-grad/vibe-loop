from __future__ import annotations

import json
import os
from pathlib import Path


STATE_PATH = Path(".vibe-loop/command-locks.json")


def load_state() -> dict[str, object]:
    if not STATE_PATH.is_file():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")


def same_owner(current: object, run_id: str) -> bool:
    return isinstance(current, dict) and current.get("run_id") == run_id


def matching_fencing_token(current: object, metadata: object) -> bool:
    if not isinstance(current, dict) or not isinstance(metadata, dict):
        return False
    current_token = current.get("fencing_token")
    return current_token is None or metadata.get("fencing_token") == current_token


def main() -> int:
    operation = os.environ["VIBE_LOOP_LOCK_OPERATION"]
    task_id = os.environ.get("VIBE_LOOP_LOCK_TASK_ID", "")
    run_id = os.environ.get("VIBE_LOOP_LOCK_RUN_ID", "")
    metadata = json.loads(os.environ.get("VIBE_LOOP_LOCK_METADATA_JSON", "{}"))
    state = load_state()
    current = state.get(task_id)
    if operation == "acquire":
        if current is not None and not same_owner(current, run_id):
            print(json.dumps({"acquired": False, "metadata": current}))
            return 0
        state[task_id] = metadata
        save_state(state)
        print(json.dumps({"acquired": True, "metadata": metadata}))
        return 0
    if operation == "update":
        if not same_owner(current, run_id) or not matching_fencing_token(
            current, metadata
        ):
            print(json.dumps({"updated": False, "metadata": current or {}}))
            return 0
        state[task_id] = metadata
        save_state(state)
        print(json.dumps({"updated": True, "metadata": metadata}))
        return 0
    if operation == "release":
        if not same_owner(current, run_id) or not matching_fencing_token(
            current, metadata
        ):
            print(json.dumps({"released": False}))
            return 0
        state.pop(task_id, None)
        save_state(state)
        print(json.dumps({"released": True}))
        return 0
    if operation == "status":
        current = state.get(task_id)
        if current is None:
            print(json.dumps({"locked": False}))
        else:
            print(json.dumps({"locked": True, "metadata": current}))
        return 0
    if operation == "list":
        print(json.dumps({"locks": [{"metadata": item} for item in state.values()]}))
        return 0
    raise SystemExit(f"unsupported lock operation: {operation}")


if __name__ == "__main__":
    raise SystemExit(main())
