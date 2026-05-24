# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| DIRTY-00 | P0 | Done | none | Seed dirty-worktree fixture. | Fixture tests run. | Seeded fixture. |
| DIRTY-01 | P0 | Planned | DIRTY-00 | Refuse final integration when another worker's claimed worktree has uncommitted changes. | The dirty worktree is not mutated or cleaned, no merge occurs, and a blocked worker report names `foreign_dirty_claimed_worktree`. | Not started. |
