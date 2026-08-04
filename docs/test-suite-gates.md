# Test-Suite Gate

This file is authoritative for repository test-suite gate alignment. CI and
the runtime completion gate must both run this exact command:

```bash
uv run -m pytest tests/
```

The chosen alignment is that CI runs what the gate runs. This avoids making
integration depend on an unapplied change to the operator-owned, untracked
`.vibe-loop.toml`. The CI matrix runs the command on the minimum and latest
supported Python versions, and the tracked regression test requires the test
job to retain the exact gate command.

`uv run python -m unittest discover -s tests` remains a supported diagnostic
runner, but it is not an integration gate. Pytest and unittest have different
collection semantics, so they can expose different defects; changing which
runner blocks integration requires updating the operator configuration, CI,
this contract, and its regression test together.
