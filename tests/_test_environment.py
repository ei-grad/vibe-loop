from __future__ import annotations

import os
import sys
from pathlib import Path


DISABLE_GIT_ISOLATION = "VIBE_LOOP_TEST_DISABLE_GIT_ISOLATION"


def isolate_git_environment() -> None:
    if os.environ.get(DISABLE_GIT_ISOLATION) == "1":
        return

    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            os.environ.pop(name)

    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


def configure_test_environment() -> None:
    argv = sys.orig_argv
    executable = Path(argv[0]).name
    module_runner = any(
        argv[index : index + 2] in (["-m", "pytest"], ["-m", "unittest"])
        for index in range(len(argv) - 1)
    )
    if executable in {"pytest", "py.test"} or module_runner:
        isolate_git_environment()
