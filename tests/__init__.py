import os


# Test-owned repositories must not inherit Git repository or configuration controls.
for name in tuple(os.environ):
    if name.startswith("GIT_"):
        os.environ.pop(name)

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


for name in (
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
):
    os.environ.pop(name, None)
