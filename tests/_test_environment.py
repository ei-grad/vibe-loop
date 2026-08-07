from __future__ import annotations

import atexit
import os
import shutil
import tempfile


DISABLE_GIT_ISOLATION = "VIBE_LOOP_TEST_DISABLE_GIT_ISOLATION"
DISABLE_HOME_ISOLATION = "VIBE_LOOP_TEST_DISABLE_HOME_ISOLATION"
TEST_HOME_VARIABLE = "VIBE_LOOP_TEST_HOME"
HOST_HOME_VARIABLES = (
    "CLAUDE_HOME",
    "CODEX_HOME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
RUNTIME_ENVIRONMENT_VARIABLES = (
    "VIBE_LOOP_BRANCH",
    "VIBE_LOOP_FENCING_TOKEN",
    "VIBE_LOOP_IMPLEMENTER_SESSION",
    "VIBE_LOOP_LOG",
    "VIBE_LOOP_REPO",
    "VIBE_LOOP_REVIEWER_SESSION",
    "VIBE_LOOP_REVIEWER_SESSION_ATTESTATION",
    "VIBE_LOOP_RUN_ID",
    "VIBE_LOOP_STATE_DIR",
    "VIBE_LOOP_TASK_ID",
    "VIBE_LOOP_WORKTREE",
)


def isolate_git_environment() -> None:
    if os.environ.get(DISABLE_GIT_ISOLATION) == "1":
        return

    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            os.environ.pop(name)

    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


def isolate_home_environment() -> None:
    """Point `Path.home()` at an empty directory owned by this test process.

    Product code reads the operator's runtime roots (`~/.claude/skills`,
    `~/.codex/skills`, `~/.vibe-loop`) through `Path.home()`. Without this the
    suite's verdict depends on whatever is installed on the host, so a candidate
    that edits a bundled skill source fails its own verification.
    """
    if os.environ.get(DISABLE_HOME_ISOLATION) == "1":
        return

    home = os.environ.get(TEST_HOME_VARIABLE)
    if not home:
        # The process that creates the directory owns its removal; test
        # subprocesses inherit the same home so a nested suite observes one
        # host state instead of allocating and leaking its own.
        home = tempfile.mkdtemp(prefix="vibe-loop-test-home-")
        atexit.register(shutil.rmtree, home, ignore_errors=True)
        os.environ[TEST_HOME_VARIABLE] = home
    os.makedirs(home, exist_ok=True)

    for name in HOST_HOME_VARIABLES:
        os.environ.pop(name, None)
    os.environ["HOME"] = home


def isolate_test_environment() -> None:
    isolate_git_environment()
    isolate_home_environment()
    for name in RUNTIME_ENVIRONMENT_VARIABLES:
        os.environ.pop(name, None)
