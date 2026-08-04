from __future__ import annotations

import os


DISABLE_GIT_ISOLATION = "VIBE_LOOP_TEST_DISABLE_GIT_ISOLATION"
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


def isolate_test_environment() -> None:
    isolate_git_environment()
    for name in RUNTIME_ENVIRONMENT_VARIABLES:
        os.environ.pop(name, None)
