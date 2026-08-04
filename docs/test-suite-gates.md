# Test-Suite Gates

This file is authoritative for repository test-suite gate alignment. CI and
the runtime completion gate must both run these commands:

```bash
make test
uv run python -m unittest discover -s tests
```

Both commands are required because pytest and unittest have different
collection semantics. A failure collected by either supported runner must
block integration. The tracked CI workflow runs both commands on the minimum
and latest supported Python versions.

The repository's `.vibe-loop.toml` is operator-owned and untracked. Its
`[completion].commands` list must contain the same two command lines. The
tracked collection regression test prevents CI from silently dropping either
runner; changes to the operator configuration must be reconciled against this
contract.
