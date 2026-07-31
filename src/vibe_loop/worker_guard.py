"""Contain a Linux worker below a published, subreaping process root."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

from vibe_loop.processes import (
    ProcessNode,
    collect_owned_descendants,
    read_process_node,
    read_process_table,
)

GUARD_FAILURE_EXIT = 126
PR_SET_CHILD_SUBREAPER = 36


def become_child_subreaper() -> bool:
    """Keep orphaned worker descendants attached to this guard."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0:
        return True
    return False


def live_contained_descendants(
    root: ProcessNode,
) -> list[ProcessNode] | None:
    """Return the live ancestry boundary while the guard identity is intact."""
    table = read_process_table()
    current_root = table.get(root.pid)
    if (
        current_root is None
        or current_root.process_birth_id != root.process_birth_id
        or current_root.state in {"Z", "X", "x"}
    ):
        return None
    return [
        node
        for node in collect_owned_descendants(table, {root.pid: root.process_birth_id})
        if node.pid != root.pid and node.state not in {"Z", "X", "x"}
    ]


def drain_contained_descendants(*, signal_grace_seconds: float = 2.0) -> bool:
    """Stop residual command descendants before allowing the guard to exit."""
    root = read_process_node(os.getpid())
    if root is None:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + signal_grace_seconds
        empty_scans = 0
        while time.monotonic() < deadline:
            descendants = live_contained_descendants(root)
            if descendants is None:
                return False
            if not descendants:
                empty_scans += 1
                if empty_scans >= 2:
                    return True
            else:
                empty_scans = 0
                for node in descendants:
                    current = read_process_node(node.pid)
                    if (
                        current is None
                        or current.process_birth_id != node.process_birth_id
                        or current.state in {"Z", "X", "x"}
                    ):
                        continue
                    try:
                        os.kill(node.pid, sig)
                    except ProcessLookupError:
                        pass
                    except (PermissionError, OSError):
                        return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    return False


def wait_for_contained_tree(command: list[str]) -> int:
    """Return the command status after stopping residual adopted descendants."""
    try:
        child = subprocess.Popen(command)
    except OSError:
        return GUARD_FAILURE_EXIT
    command_status: int | None = None
    while True:
        try:
            pid, status = os.wait()
        except ChildProcessError:
            return command_status if command_status is not None else GUARD_FAILURE_EXIT
        if pid == child.pid:
            command_status = os.waitstatus_to_exitcode(status)
            if drain_contained_descendants():
                return command_status


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[3] != "--":
        return GUARD_FAILURE_EXIT
    try:
        expected_parent_pid = int(arguments[0])
        gate_fd = int(arguments[1])
    except ValueError:
        return GUARD_FAILURE_EXIT
    if os.getppid() != expected_parent_pid:
        return GUARD_FAILURE_EXIT
    try:
        released = os.read(gate_fd, 1)
    except OSError:
        return GUARD_FAILURE_EXIT
    finally:
        os.close(gate_fd)
    if released != b"\0":
        return GUARD_FAILURE_EXIT
    if not become_child_subreaper():
        return GUARD_FAILURE_EXIT
    mode = arguments[2]
    command = arguments[4:]
    if mode == "shell" and len(command) == 1:
        return wait_for_contained_tree(["/bin/sh", "-c", command[0]])
    if mode == "exec" and command:
        return wait_for_contained_tree(command)
    return GUARD_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
