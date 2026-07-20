"""Verified local process identity read from ``/proc``.

Every identity here is anchored on a process-birth ID (boot ID plus the
kernel's start time for that PID). A bare PID is not an identity: the kernel
recycles PIDs, and a reparented descendant keeps running under PID 1 after its
launcher exits. Callers that signal or reap processes must compare birth IDs,
never names, PIDs alone, or ambient process lists.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

PROC_ROOT = Path("/proc")


@dataclasses.dataclass(frozen=True)
class ProcessNode:
    pid: int
    parent_pid: int
    process_group_id: int
    session_id: int
    process_birth_id: str
    state: str = ""


def boot_identity(proc_root: Path = PROC_ROOT) -> str:
    try:
        return (
            (proc_root / "sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return ""


def _stat_fields(pid: int, proc_root: Path) -> list[str]:
    """Fields of ``/proc/<pid>/stat`` after the ``pid (comm)`` prefix.

    Splitting on the last ``)`` is required because ``comm`` may itself contain
    spaces and parentheses. Index ``n`` here is ``proc(5)`` field ``n + 3``.
    """

    try:
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return []
    _prefix, separator, suffix = stat_text.rpartition(")")
    return suffix.split() if separator else []


def process_birth_identity(pid: int, *, proc_root: Path = PROC_ROOT) -> str:
    if sys.platform != "linux" or pid <= 0:
        return ""
    boot_id = boot_identity(proc_root)
    fields = _stat_fields(pid, proc_root)
    if not boot_id or len(fields) <= 19 or not fields[19].isdigit():
        return ""
    return f"{boot_id}:{fields[19]}"


def read_process_node(pid: int, *, proc_root: Path = PROC_ROOT) -> ProcessNode | None:
    if sys.platform != "linux" or pid <= 0:
        return None
    boot_id = boot_identity(proc_root)
    fields = _stat_fields(pid, proc_root)
    if not boot_id or len(fields) <= 19:
        return None
    parent_pid, process_group_id, session_id, start_time = (
        fields[1],
        fields[2],
        fields[3],
        fields[19],
    )
    if not all(
        value.lstrip("-").isdigit()
        for value in (parent_pid, process_group_id, session_id, start_time)
    ):
        return None
    return ProcessNode(
        pid=pid,
        parent_pid=int(parent_pid),
        process_group_id=int(process_group_id),
        session_id=int(session_id),
        process_birth_id=f"{boot_id}:{start_time}",
        state=fields[0],
    )


def read_process_table(*, proc_root: Path = PROC_ROOT) -> dict[int, ProcessNode]:
    """One snapshot of every readable process, keyed by PID.

    A single snapshot is taken so parent links stay mutually consistent; a
    process that exits mid-scan simply drops out rather than producing a node
    with a stale parent.
    """

    table: dict[int, ProcessNode] = {}
    if sys.platform != "linux":
        return table
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        node = read_process_node(int(entry.name), proc_root=proc_root)
        if node is not None:
            table[node.pid] = node
    return table


def collect_owned_descendants(
    table: dict[int, ProcessNode],
    roots: dict[int, str],
) -> list[ProcessNode]:
    """Nodes reachable as descendants of ``roots``, deepest first.

    ``roots`` maps a root PID to its expected birth ID; a root whose snapshot
    birth ID disagrees contributes nothing, so a recycled root PID can never
    drag an unrelated subtree into the result. Ordering is deepest-first so a
    caller signalling in list order reaches leaves before their parents.
    """

    children: dict[int, list[ProcessNode]] = {}
    for node in table.values():
        children.setdefault(node.parent_pid, []).append(node)

    ordered: list[ProcessNode] = []
    seen: set[int] = set()

    def walk(node: ProcessNode, depth: int) -> None:
        if node.pid in seen:
            return
        seen.add(node.pid)
        ordered.append(node)
        for child in children.get(node.pid, []):
            # A process whose parent link points at itself, or back into an
            # already-visited ancestor, cannot deepen the walk.
            if child.pid != node.pid:
                walk(child, depth + 1)

    for root_pid, expected_birth_id in sorted(roots.items()):
        node = table.get(root_pid)
        if node is None or not expected_birth_id:
            continue
        if node.process_birth_id != expected_birth_id:
            continue
        walk(node, 0)

    depths: dict[int, int] = {}

    def depth_of(pid: int) -> int:
        if pid in depths:
            return depths[pid]
        node = table.get(pid)
        depths[pid] = 0
        if node is not None and node.parent_pid in seen and node.parent_pid != pid:
            depths[pid] = depth_of(node.parent_pid) + 1
        return depths[pid]

    return sorted(ordered, key=lambda node: (-depth_of(node.pid), node.pid))
