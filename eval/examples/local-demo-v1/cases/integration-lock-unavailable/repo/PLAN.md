# Plan

| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| BUSY-00 | P0 | Done | none | Seed integration-lock-unavailable fixture. | Fixture tests run. | Seeded fixture. |
| BUSY-01 | P0 | Planned | BUSY-00 | Refuse final integration when the advisory main-integration lock is held by another live worker. | The live lock is not stolen or released, no merge occurs, and a blocked worker report names the unavailable integration lock. | Not started. |
