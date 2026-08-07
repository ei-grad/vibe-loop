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

## Worker launch and post-integration refresh

Worker preflight and `verify-skills` answer different questions. The verifier
reports every managed drift state and exits non-zero for it. Worker preflight
refuses to launch only where the installed content or its provenance is unknown:
`runtime-edited`, `branch-sourced`, or an invalid manifest. That content is not
the reviewed bundle and no automated step can decide what it is.

A `stale` entry does not block worker launch. It means the installed copy still
matches its manifest while the recorded source moved on, which is exactly what
merging a bundled-skill change produces on the host that supplies the bundle.
Blocking there halts every board on that host, across every repository, until an
operator reinstalls by hand, which makes a bundled-skill edit unmergeable in
practice. Preflight names the lagging paths and launches. It names the refresh
below as the repair only when this repository supplies the running bundle;
otherwise it names `install-skills`, because a bundle installed from a wheel or
tool directory is never made stale, or repaired, by a repository merge.

The runtime closes the window it opens. After a run advances `main`, it
reinstalls recorded deployments of its own bundle whose source moved. The
refresh is confined to that boundary:

- it runs only when the repository that advanced supplies the running bundle;
- it writes only the runtime roots that already record a stale deployment of
  that bundle, so a runtime the operator never installed into does not acquire
  one as a side effect. `install-skills` still writes both roots, because
  choosing to install is an operator action;
- it refuses the same non-mainline and dirty sources `install-skills` refuses;
- a failed refresh is reported and never fails the run.

A publish is not observable half-written. Managed files are replaced one at a
time and the manifest is written last, so a reader landing mid-publish would
otherwise see installed content that no longer matches the recorded digest and
call the root `runtime-edited`, which blocks worker launch. Every install holds
`.skill-deploy.lock` beside the runtime root for the whole overwrite check and
write, and verification waits for that lock. The lock sits outside the root so
the verifier still reports exactly the deployed tree, and it is only read, never
created, by verification. The wait is bounded: a writer that outlives it is
abandoned rather than stalling verification, and a root no writer has ever
locked is read directly.

Worker launch itself still performs no install: a global write there would
obscure when deployment changed and would repair drift the operator has not
seen. Supervisor and CLI startup verification likewise stays read-only; the
[Autopilot PRD](prd/autopilot.md#prd-aut-002b-supervisor-configuration-lifetime)
owns supervisor and CLI startup visibility for these deployment states.

Because these checks read the operator's real home, the test suite substitutes
an isolated one; see the
[test-suite gate contract](test-suite-gates.md#host-state-isolation).

The bundle-agnostic implementation lives in
`vibe_loop.skill_deployment`; `vibe_loop.skills` only supplies this repository's
resource root and skill names. Other skill-producing CLIs should use this
implementation instead of copying the deployment logic.
