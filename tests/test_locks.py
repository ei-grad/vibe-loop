from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
