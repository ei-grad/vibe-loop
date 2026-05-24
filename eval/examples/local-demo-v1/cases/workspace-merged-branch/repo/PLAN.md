# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| MERGED-00 | P0 | Done | none | Seed merged-branch fixture. | Fixture tests run. | Seeded fixture. |
| MERGED-01 | P0 | Planned | MERGED-00 | Refuse final integration when the active worker branch is already contained in main. | The branch/worktree/lock are not removed automatically, no extra merge occurs, and a blocked worker report names `branch_already_merged`. | Not started. |
