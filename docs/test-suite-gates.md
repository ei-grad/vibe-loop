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

## Host-state isolation

A gate result must depend on repository content, not on the machine running it.
`tests/_test_environment.py` is the single place that establishes this, and
every test module imports `_test_bootstrap` before anything else so the
environment is settled before product code is imported.

It replaces `HOME` with an empty per-process directory and drops the host's
`CLAUDE_HOME`, `CODEX_HOME`, and `XDG_*_HOME` overrides, so `Path.home()` reads
of `~/.claude/skills`, `~/.codex/skills`, and `~/.vibe-loop` see test state
only. Without this a candidate that edits a bundled `SKILL.md` fails its own
verification: merging it makes the operator's installed copies lag their source
and [skill deployment](skill-deployment.md) verification then reports drift for
host reasons the diff did not introduce. It also isolates Git configuration, so
a host `~/.gitconfig` cannot change what the suite observes.

Isolation supplies a different home; it never disables a check. Product code
still reads the effective home, and `tests/test_test_environment.py` proves both
halves: a drifted operator home is invisible to the suite, and the same suite
fails against that home when isolation is disabled.

`uv run python -m unittest discover -s tests` remains a supported diagnostic
runner, but it is not a repository integration or release gate. Pytest and
unittest have different collection semantics, so changing which runner blocks
integration or release requires updating the operator configuration, both
workflows, this contract, and its regression test together.
