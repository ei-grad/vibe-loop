# Checkout mutation tasks

- [x] 1.1 Update the checkout capability
  - Acceptance: The capability names idempotent mutations.
  - Evidence: `python -m unittest discover`

- [-] 1.2 Preserve the active checkout story
  - Depends on: 1.1
  - Acceptance:
    - The selected story records the idempotency constraint.
  - Acceptance: The result names the active OpenSpec task.
  - Evidence: `python -m unittest discover`
