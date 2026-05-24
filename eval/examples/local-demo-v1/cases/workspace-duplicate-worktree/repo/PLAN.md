# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| DUP-00 | P0 | Done | none | Seed duplicate-worktree fixture. | Fixture imports and tests run. | Seeded fixture. |
| DUP-01 | P0 | Planned | DUP-00 | Refuse final integration when the claimed branch is checked out in duplicate worktrees. | No duplicate worktree is removed automatically, no merge occurs, and a blocked worker report names `duplicate_branch_worktrees`. | Not started. |
