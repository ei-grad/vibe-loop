from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from vibe_loop.processes import (
    ProcessNode,
    collect_owned_descendants,
    process_birth_identity,
    read_process_node,
    read_process_table,
)


BOOT_ID = "11111111-2222-3333-4444-555555555555"


def write_fake_proc(root: Path, entries: dict[int, tuple[int, int, int, int]]) -> None:
    """Materialize a fake ``/proc`` from ``pid -> (ppid, pgid, sid, starttime)``."""

    boot_path = root / "sys/kernel/random"
    boot_path.mkdir(parents=True, exist_ok=True)
    (boot_path / "boot_id").write_text(f"{BOOT_ID}\n", encoding="utf-8")
    for pid, (ppid, pgid, sid, starttime) in entries.items():
        process_dir = root / str(pid)
        process_dir.mkdir(parents=True, exist_ok=True)
        # Field layout mirrors proc(5): the comm value deliberately contains a
        # space and parentheses so the reader's rpartition split is exercised.
        fields = ["S", str(ppid), str(pgid), str(sid)] + ["0"] * 15
        fields.append(str(starttime))
        (process_dir / "stat").write_text(
            f"{pid} (weird ) name) " + " ".join(fields) + "\n",
            encoding="utf-8",
        )


def node(pid: int, ppid: int, pgid: int, sid: int, starttime: int) -> ProcessNode:
    return ProcessNode(
        pid=pid,
        parent_pid=ppid,
        process_group_id=pgid,
        session_id=sid,
        process_birth_id=f"{BOOT_ID}:{starttime}",
        state="S",
    )


@unittest.skipUnless(sys.platform == "linux", "/proc parsing is Linux-only")
class ProcessTableTests(unittest.TestCase):
    def test_reads_identity_from_stat_with_parenthesized_comm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fake_proc(root, {42: (7, 42, 42, 900)})
            parsed = read_process_node(42, proc_root=root)
            birth_id = process_birth_identity(42, proc_root=root)

        self.assertEqual(parsed, node(42, 7, 42, 42, 900))
        self.assertEqual(birth_id, f"{BOOT_ID}:900")

    def test_table_snapshot_skips_non_pid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fake_proc(root, {10: (1, 10, 10, 100), 11: (10, 10, 10, 101)})
            (root / "self").mkdir()
            table = read_process_table(proc_root=root)

        self.assertEqual(sorted(table), [10, 11])


class OwnedDescendantTests(unittest.TestCase):
    def _tree_table(self) -> dict[int, ProcessNode]:
        # supervisor(100) -> child(200) -> worker(300) -> reviewer(400), where
        # the reviewer ran setsid so it leads its own group and session. 900 is
        # an unrelated process that must never be collected.
        return {
            100: node(100, 1, 100, 100, 500),
            200: node(200, 100, 200, 200, 501),
            300: node(300, 200, 300, 300, 502),
            400: node(400, 300, 400, 400, 503),
            900: node(900, 1, 900, 900, 504),
        }

    def test_collects_subtree_deepest_first_across_groups_and_sessions(self) -> None:
        table = self._tree_table()
        ordered = collect_owned_descendants(table, {200: table[200].process_birth_id})

        self.assertEqual([entry.pid for entry in ordered], [400, 300, 200])
        self.assertNotIn(900, {entry.pid for entry in ordered})

    def test_multiple_roots_deduplicate_and_stay_deepest_first(self) -> None:
        table = self._tree_table()
        roots = {
            200: table[200].process_birth_id,
            300: table[300].process_birth_id,
        }
        ordered = collect_owned_descendants(table, roots)

        self.assertEqual([entry.pid for entry in ordered], [400, 300, 200])

    def test_root_with_mismatched_birth_id_contributes_nothing(self) -> None:
        table = self._tree_table()
        ordered = collect_owned_descendants(table, {200: f"{BOOT_ID}:reused"})

        self.assertEqual(ordered, [])

    def test_root_without_recorded_birth_id_contributes_nothing(self) -> None:
        table = self._tree_table()
        ordered = collect_owned_descendants(table, {200: ""})

        self.assertEqual(ordered, [])

    def test_missing_root_process_contributes_nothing(self) -> None:
        table = self._tree_table()
        ordered = collect_owned_descendants(table, {777: f"{BOOT_ID}:501"})

        self.assertEqual(ordered, [])


if __name__ == "__main__":
    unittest.main()
