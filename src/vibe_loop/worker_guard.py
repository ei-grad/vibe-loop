"""Contain a Linux worker below a published, subreaping process root."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

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


def wait_for_contained_tree(command: list[str]) -> int:
    """Return the command status after every adopted descendant has exited."""
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
