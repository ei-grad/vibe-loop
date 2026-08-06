# Recorded skill deployment

Bundled skills are copied into `~/.codex/skills/` and
`~/.claude/skills/`. These directories are deployment targets, not sources of
truth. A symlink to a live Git worktree is deliberately not used: switching a
branch, editing a file, or resolving a conflict in that worktree would
immediately and silently change the instructions loaded by unrelated agents.

`install-skills` writes both runtime roots on every invocation. `--codex` and
`--claude` filter only the paths printed after installation; they do not permit
the two runtimes to be updated independently.

Each target root contains `.skill-manifest.json`. Every managed file records:

- the canonical source repository and repository-relative source path;
- the source commit and branch;
- whether the source worktree was dirty;
- the deployed SHA-256 digest and installation time.

When run from a Git checkout, the installer requires clean `main`. A dirty,
detached, or non-`main` source is refused unless `--allow-unmerged` is supplied.
The override is intentionally visible in the manifest, so worker preflight
subsequently classifies that deployment as `branch-sourced` and blocks worker
launch. A wheel or other immutable package installation instead records its
package release identity; VCS package metadata retains an explicit requested
revision and applies the same mainline guard.

Before writing either runtime, the installer checks every existing managed
file. If its digest differs from the recorded digest, installation stops
without writing either root. `--force` prints the affected file diffs before
overwriting them. A first recorded deployment can adopt an existing file
without an override only when it already matches the source.

Use the read-only verifier to inspect deployment state:

```bash
vibe-loop verify-skills
vibe-loop verify-skills --codex --json
```

The verifier reports these managed-file states:

- `in-sync`: the runtime, manifest, and current source content agree;
- `stale`: runtime content still matches the manifest but source content moved;
- `runtime-edited`: runtime content no longer matches the manifest;
- `branch-sourced`: the manifest records a dirty or non-mainline source.

It also reports target-only files as `unmanaged` and never removes them. This is
required because runtime roots can contain independently managed skills and
mutable state such as `local-memory`.

`verify-skills` exits non-zero for managed drift or an invalid or missing
manifest. Unmanaged paths are reported but do not affect the exit status or
block workers. A root without a manifest is a migration state and remains
advisory to worker preflight until its first recorded install.

An invalid manifest fails closed by default. `install-skills --force` prints the
manifest error before replacing it, providing an explicit recovery path without
deleting target-only content.

Stale deployments block rather than auto-repair. Automatic repair would make a
global write part of worker launch and obscure when deployment changed. The
operator must inspect the verifier output and install explicitly from clean
`main`.

The [Autopilot PRD](prd/autopilot.md#prd-aut-002b-supervisor-configuration-lifetime)
owns supervisor and CLI startup visibility for these deployment states.

The bundle-agnostic implementation lives in
`vibe_loop.skill_deployment`; `vibe_loop.skills` only supplies this repository's
resource root and skill names. Other skill-producing CLIs should use this
implementation instead of copying the deployment logic.
