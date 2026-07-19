from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_loop import locks


class ReplaceMetadataFileTests(unittest.TestCase):
    def test_retries_transient_windows_permission_error(self) -> None:
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky(self: Path, target: Path) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(self, target)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"
            target.write_text("old", encoding="utf-8")

            with (
                patch("vibe_loop.locks.sys.platform", "win32"),
                patch("vibe_loop.locks.time.sleep") as sleep,
                patch.object(Path, "replace", flaky),
            ):
                locks.replace_metadata_file(source, target)

            self.assertEqual(calls["n"], 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(sleep.call_count, 2)

    def test_reraises_windows_permission_error_after_timeout(self) -> None:
        clock = {"now": 0.0}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"

            def advancing_sleep(delay: float) -> None:
                clock["now"] += delay

            with (
                patch("vibe_loop.locks.sys.platform", "win32"),
                patch(
                    "vibe_loop.locks.time.monotonic",
                    side_effect=lambda: clock["now"],
                ),
                patch("vibe_loop.locks.time.sleep", side_effect=advancing_sleep),
                patch.object(
                    Path,
                    "replace",
                    side_effect=PermissionError(5, "Access is denied"),
                ),
            ):
                with self.assertRaises(PermissionError):
                    locks.replace_metadata_file(source, target)

    def test_reraises_immediately_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tmp"
            source.write_text("new", encoding="utf-8")
            target = Path(directory) / "lock.json"

            with (
                patch("vibe_loop.locks.sys.platform", "linux"),
                patch("vibe_loop.locks.time.sleep") as sleep,
                patch.object(
                    Path,
                    "replace",
                    side_effect=PermissionError(13, "denied"),
                ),
            ):
                with self.assertRaises(PermissionError):
                    locks.replace_metadata_file(source, target)

            sleep.assert_not_called()


class WriteMetadataTests(unittest.TestCase):
    def test_removes_temp_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_dir = Path(directory) / "TASK-01.lock"
            lock_dir.mkdir()

            with patch.object(
                Path,
                "replace",
                side_effect=PermissionError(13, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    locks.write_metadata(lock_dir, {"task_id": "TASK-01"})

            leftover = [
                entry.name
                for entry in lock_dir.iterdir()
                if entry.name.endswith(".tmp")
            ]
            self.assertEqual(leftover, [])


class CommandLockContextTests(unittest.TestCase):
    def test_context_covers_every_operation_and_protocol_values_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.py"
            records_path = root / "records.jsonl"
            adapter.write_text(
                "import json, os\n"
                "from pathlib import Path\n"
                "record = {key: os.environ.get(key, '') for key in (\n"
                "    'PROJECT_SELECTOR', 'VIBE_LOOP_LOCK_OPERATION',\n"
                "    'VIBE_LOOP_LOCK_TASK_ID', 'VIBE_LOOP_LOCK_RUN_ID')}\n"
                f"path = Path({str(records_path)!r})\n"
                "with path.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps(record) + '\\n')\n"
                "operation = record['VIBE_LOOP_LOCK_OPERATION']\n"
                "metadata = json.loads(os.environ['VIBE_LOOP_LOCK_METADATA_JSON'])\n"
                "if operation == 'list':\n"
                "    print('[]')\n"
                "elif operation == 'status':\n"
                "    print(json.dumps({'locked': False}))\n"
                "elif operation == 'release':\n"
                "    print(json.dumps({'released': True}))\n"
                "elif operation == 'update':\n"
                "    print(json.dumps({'updated': True, 'metadata': metadata}))\n"
                "else:\n"
                "    print(json.dumps({'acquired': True, 'metadata': metadata}))\n",
                encoding="utf-8",
            )
            command = f"{sys.executable} {adapter}"
            backend = locks.CommandLockBackend(
                repo=root,
                lock_root=root / "locks",
                acquire_command=command,
                release_command=command,
                status_command=command,
                list_command=command,
                runtime_context={
                    "PROJECT_SELECTOR": "entry-selector",
                    "VIBE_LOOP_LOCK_OPERATION": "spoofed-operation",
                    "VIBE_LOOP_LOCK_TASK_ID": "spoofed-task",
                    "VIBE_LOOP_LOCK_RUN_ID": "spoofed-run",
                },
            )

            with patch.dict(os.environ, {"PROJECT_SELECTOR": "host-selector"}):
                task_lock = backend.acquire("TASK-1", "run-1")
                task_lock = backend.update(task_lock, dict(task_lock.metadata))
                backend.release(task_lock)
                self.assertIsNone(backend.status("TASK-2"))
                self.assertEqual(backend.list_locks(), [])
                inherited_after = os.environ["PROJECT_SELECTOR"]

            records = [
                json.loads(line)
                for line in records_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(inherited_after, "host-selector")
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_OPERATION"] for record in records],
            ["acquire", "update", "release", "status", "list"],
        )
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_TASK_ID"] for record in records],
            ["TASK-1", "TASK-1", "TASK-1", "TASK-2", ""],
        )
        self.assertEqual(
            [record["VIBE_LOOP_LOCK_RUN_ID"] for record in records],
            ["run-1", "run-1", "run-1", "", ""],
        )
        self.assertTrue(
            all(record["PROJECT_SELECTOR"] == "entry-selector" for record in records)
        )


if __name__ == "__main__":
    unittest.main()
