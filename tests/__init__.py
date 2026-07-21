import os


for name in (
    "VIBE_LOOP_BRANCH",
    "VIBE_LOOP_FENCING_TOKEN",
    "VIBE_LOOP_LOG",
    "VIBE_LOOP_REPO",
    "VIBE_LOOP_RUN_ID",
    "VIBE_LOOP_STATE_DIR",
    "VIBE_LOOP_TASK_ID",
    "VIBE_LOOP_WORKTREE",
):
    os.environ.pop(name, None)
