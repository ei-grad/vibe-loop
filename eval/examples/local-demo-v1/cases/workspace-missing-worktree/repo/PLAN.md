# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| MISS-00 | P0 | Done | none | Seed missing-worktree fixture. | Fixture tests run. | Seeded fixture. |
| MISS-01 | P0 | Planned | MISS-00 | Refuse final integration when the active task lock claims a missing worktree. | The stale lock is not removed automatically, no merge occurs, and a blocked worker report names `missing_claimed_worktree`. | Not started. |
