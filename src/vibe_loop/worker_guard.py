"""Block Linux worker execution until its supervisor publishes ownership."""

from __future__ import annotations

import os
import sys

GUARD_FAILURE_EXIT = 126


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
    mode = arguments[2]
    command = arguments[4:]
    if mode == "shell" and len(command) == 1:
        os.execvpe("/bin/sh", ["/bin/sh", "-c", command[0]], os.environ)
    if mode == "exec" and command:
        os.execvpe(command[0], command, os.environ)
    return GUARD_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
