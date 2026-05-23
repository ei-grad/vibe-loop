# Plan: Checkout Flow

## Overview

Implement a small checkout workflow.

## Validation Commands

- `uv run -m pytest tests/test_checkout.py`
- `uv run --with ruff ruff check src tests`

```markdown
### Task 99: Example task
- [ ] This is documentation, not a task
```

### Task 1: Add checkout API

- [x] Create the request model
- [ ] Add checkout handler
- [ ] Add API tests
- Resources: api, checkout
- Paths: src/checkout.py, tests/test_checkout.py

### Iteration 2.5: Tighten validation

- [x] Reject empty carts
- [x] Add edge-case tests
- Resources: none
- Paths: none
