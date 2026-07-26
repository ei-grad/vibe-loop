"""Launch a Linux worker with a kernel-enforced parent-death signal."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

PR_SET_PDEATHSIG = 1
GUARD_FAILURE_EXIT = 126


def arm_parent_death_signal(expected_parent_pid: int) -> None:
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("worker supervisor exited before launch guard")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("worker supervisor exited while arming launch guard")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3 or arguments[2] != "--":
        return GUARD_FAILURE_EXIT
    try:
        expected_parent_pid = int(arguments[0])
        arm_parent_death_signal(expected_parent_pid)
    except (OSError, RuntimeError, ValueError):
        return GUARD_FAILURE_EXIT
    mode = arguments[1]
    command = arguments[3:]
    if mode == "shell" and len(command) == 1:
        os.execvpe("/bin/sh", ["/bin/sh", "-c", command[0]], os.environ)
    if mode == "exec" and command:
        os.execvpe(command[0], command, os.environ)
    return GUARD_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
