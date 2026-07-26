# Ralphex Markdown Plan Example

Configure the source in `.vibe-loop.toml`:

```toml
[task_source]
type = "ralphex-markdown"
plan_path = "docs/plans/checkout.md"
```

The referenced plan can declare conflict domains for the whole plan and refine
them per task:

```markdown
# Checkout Plan

## Conflict Surface

- Paths: src/checkout.py
- Resources: payments-api

### Task 1: Add checkout API

- [ ] Add checkout handler
- Resources: api, checkout
- Paths: src/checkout.py, tests/test_checkout.py
- Conflict Surface: resources: api, checkout; paths: src/checkout.py

## Validation Commands

- `uv run -m pytest tests/test_checkout.py`
```

The parser derives `Done` only when every checkbox in a task block is checked.
It copies `## Validation Commands` into task evidence and derives a stable ID
such as `docs.plans.checkout:task-1`. Unlabeled plan-level bullets that look like
repository-relative paths also count as path domains.
