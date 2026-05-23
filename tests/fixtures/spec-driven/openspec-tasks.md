# Tasks

- [x] 1.1 Update capability spec
  - Acceptance: new requirement is visible in the spec diff
  - Evidence: openspec validate checkout --strict

- [-] 1.2 Implement checkout mutation
  - Depends on: 1.1
  - Acceptance:
    - mutation stores idempotency keys
  - Acceptance: duplicate request returns original result
  - Evidence: pytest tests/test_checkout.py
