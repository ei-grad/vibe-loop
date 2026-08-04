# Test-Suite Gates

This file is authoritative for repository Python test-suite gate alignment.
Pre-integration CI and the runtime completion gate both run this exact command:

```bash
uv run -m pytest tests/
```

The chosen alignment is that CI runs what the gate runs. This avoids making
integration depend on an unapplied change to the operator-owned, untracked
`.vibe-loop.toml`. The CI matrix runs the command on the minimum and latest
supported Python versions, and the tracked regression test requires the test
job to retain the exact gate command.

The release workflow tests the built wheel in an isolated environment with the
same pytest suite:

```bash
uv run --no-project --with pytest --with dist/*.whl python -m pytest tests/
```

Installing pytest explicitly keeps the wheel environment independent of the
repository development environment while collecting pytest-style functions and
fixtures. The tracked regression test requires both workflow jobs to retain
their respective commands.

`uv run python -m unittest discover -s tests` remains a supported diagnostic
runner, but it is not a repository integration or release gate. Pytest and
unittest have different collection semantics, so changing which runner blocks
integration or release requires updating the operator configuration, both
workflows, this contract, and its regression test together.
