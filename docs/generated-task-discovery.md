# Generated Task Discovery

## Cache Contract

Generated discovery writes one JSON cache file under the configured state
directory:

```text
<state_dir>/generated-task-source.json
```

`state_dir` defaults to `.vibe-loop`, but every reference to the cache path must
use the loaded configuration rather than a hard-coded directory. The cache is
repo-local diagnostic state. It may describe how to parse existing task
artifacts, but it must not contain executable task adapters.

The cache envelope is versioned independently from the package version:

```json
{
  "schema_version": 1,
  "prompt_version": 1,
  "status": "profile",
  "generated_at": "2026-05-08T00:00:00Z",
  "agent": {
    "name": "codex",
    "selection_command_source": "explicit"
  },
  "confidence": 0.86,
  "provenance": {
    "repo": "/path/to/repo",
    "evidence_limit": {
      "max_file_bytes": 2097152,
      "max_total_bytes": 10485760
    },
    "evidence_file_count": 1,
    "skipped_evidence": []
  },
  "source_fingerprints": [
    {
      "path": "PLAN.md",
      "size": 12345,
      "sha256": "hex",
      "mtime_ns": 1770000000000000000
    }
  ],
  "profile": {
    "kind": "markdown_table",
    "source_paths": ["PLAN.md"],
    "stable_ids": true,
    "fields": {
      "id": {"column": "ID"},
      "title": {"column": "Scope", "strategy": "first_sentence"},
      "status": {"column": "Status"},
      "dependencies": {"column": "Dependencies", "none_values": ["none"]}
    },
    "status_map": {
      "done": ["Done"],
      "runnable": ["Active", "Next", "Planned"],
      "blocked": ["Gated", "Low"]
    }
  },
  "degradation": null
}
```

Planning-only, needs-input, unavailable, and rejected cache records use the
same envelope. Their `degradation` object may include `missing_inputs`,
`proposed_config`, `candidate_sources`, and `questions` in addition to
`reason`, `message`, and `next_action`. Proposed configuration is diagnostic
only; generated records still cannot contain executable command adapters.

Only `schema_version` values supported by the running CLI may be loaded.
`prompt_version` is recorded so future refresh logic can invalidate profiles
when the prompt contract changes. `source_fingerprints` bind the profile to the
evidence that justified it; a changed fingerprint makes the cache stale unless a
future profile type explicitly marks that source as optional.

## Profile Status

The cache `status` determines whether read-only task commands may use it:

- `profile`: mechanically valid parser description. It is runnable only when
  source fingerprints match, confidence meets the configured threshold, stable
  IDs exist, statuses are mapped, and no explicit source-level config overrides
  it.
- `planning_only`: useful diagnostic parser sketch that must not feed runnable
  task selection. This covers synthetic IDs, missing status policy, ambiguous
  dependency syntax, or low confidence with enough structure to show likely
  tasks.
- `needs_input`: evidence suggests a task source exists, but user input is
  required, for example choosing between contradictory files or declaring stable
  ID rules.
- `unavailable`: bounded evidence did not contain a usable task source, or
  candidate sources were unreadable, too large, binary, secret-like, or outside
  evidence limits.
- `rejected`: an agent returned malformed JSON, unsupported schema, forbidden
  fields, executable adapter hints, or parser rules that fail deterministic
  validation.

Read-only commands such as `tasks list`, `tasks runnable`, `next`, and `doctor`
must never launch an agent to repair these states. They may read a fresh cache,
report the state, and print the explicit configure or refresh command that would
update it. If the cached source fingerprints no longer match current bounded
evidence, diagnostics mark the cache stale and point back to `tasks configure`.
`tasks configure --dry-run` validates a candidate without writing the cache, and
`tasks configure --force-refresh` regenerates the cache even when the current
profile is still fresh.

## Repo-Specific Task Discovery Configuration

This repository's built-in Markdown fallback recognizes a specific table shape:

`ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence`

That is acceptable as this repository's own plan format and as a small example,
but it is not the generic task-source contract. A task-system-agnostic runner
should not ask every repository to reshape planning docs before it can discover
work. The generic path asks a configured agent to analyze bounded repository
evidence, generate a normalized task-source profile, validate that profile
mechanically, and cache it as repo-local state.

Repositories that already use ralphex-style plan files can opt into the built-in
`ralphex-markdown` source instead of generating a profile or writing a command
adapter. It reads `### Task N:` and `### Iteration N:` sections, derives status
from task checkboxes, records `## Validation Commands` as evidence, and maps
plan-level or task-local `Resources`, `Paths`, and `Conflict Surface` labels
into normalized conflict domains. Plan-level `## Conflict Surface` sections can
also contain unlabeled bullet items that look like repo-relative paths. This
source is explicit configuration, so setting `task_source.type =
"ralphex-markdown"` disables generated cache use for the active task source in
the same way as other user-authored source selectors.

Repositories using common spec-driven development tools can opt into built-in
non-executable presets rather than asking generated discovery to infer those
formats or writing command adapters:

| `task_source.type` | Default source paths | Task shape |
| --- | --- | --- |
| `spec-kit` | `specs/*/tasks.md`, `.specify/specs/*/tasks.md` | Checkbox list items with `T001`-style IDs, optional `[P]` and story markers |
| `kiro` | `.kiro/specs/*/tasks.md` | Numbered checkbox list items such as `1. Implement task` |
| `openspec` | `openspec/changes/*/tasks.md` | Numbered checkbox list items such as `1.2 Implement task` |

The presets normalize checked boxes to `Done`, unchecked boxes to `Planned`,
and in-progress markers such as `[-]` or `[~]` to `Active`. Nested `Depends`,
`Depends on`, `Dependencies`, `Acceptance`, and `Evidence` labels are copied
into the normalized task when present, and inline dependency text such as
`(depends on T012, T013)` is parsed as a local dependency list. Acceptance and
evidence labels can be single-line values or followed by nested bullet text.
Task IDs are prefixed with the parent spec or change directory so multiple
`tasks.md` files can be exposed together without collisions, and local
dependency IDs are rewritten with the same prefix. These presets are explicit
source selectors; setting `task_source.type` to one of them disables generated
cache use for the active source. If a task file is missing stable IDs, contains
duplicates, has no parseable spec-tool tasks, or uses invalid dependency
syntax, parsing fails with an actionable diagnostic instead of creating
synthetic runnable tasks.

The agent-generated profile should describe how to read existing repo artifacts,
not invent task state. It can map column names, heading/list conventions,
statuses, dependency syntax, done states, candidate files, and title/acceptance
extraction rules into the existing normalized `Task` model. Optional
`resources` and `paths` field mappings declare conflict domains for parallel
scheduling; if a repository does not provide those fields, the runner treats the
domains as unknown whenever conflict-domain scheduling is active. The CLI must
still own validation: required fields, runnable statuses, dependency references,
duplicate IDs, parser output shape, and cache freshness are deterministic
checks. Generated profiles must be non-executable parser descriptions over
bounded repo-local evidence. Command adapters or any other executable task
source may only come from user-authored `.vibe-loop.toml`.

Cached configuration should live under the configured state directory, include
schema and prompt versions, source file fingerprints, provenance, confidence,
and redacted generator metadata such as agent name and selection command source.
It must not persist raw agent command strings because configured commands may
contain local flags, inline environment assignments, or other sensitive material.
Explicit `.vibe-loop.toml` settings must override the cache, and `doctor`/task
commands should report whether the active task source came from user config,
generated cache, or command output. Default config values must not count as
explicit user settings; the config loader needs to preserve which
`[task_source]` keys were present so generated state can fill only unset
behavior. Source-defining explicit settings such as command adapters, profile
paths, or plan paths win at source level. Non-source settings such as explicitly
configured runnable statuses override the matching generated profile fields
without disabling the generated source.

Stale, structurally invalid, or otherwise non-runnable `profile` cache records
should fail with an actionable configure command instead of silently treating
this repository's example table shape as a requirement. Fresh degraded cache
records such as `planning_only`, `needs_input`, `unavailable`, or `rejected` are
diagnostic only; read-only commands should report their diagnostics and may
continue to default Markdown fallback discovery when no explicit source
configuration is active.

## Precedence

Precedence is resolved before cache loading performs parser validation:

1. User-authored command adapters in `.vibe-loop.toml` are authoritative. If
   `task_source.type = "command"` or any of `task_source.list`,
   `task_source.next`, or `task_source.probe` is explicitly set, generated
   discovery is disabled for the active source. Adapter failures are reported as
   adapter failures, not replaced by generated discovery.
2. User-authored non-command source selectors are authoritative. Explicit
   `task_source.type`, `task_source.plan_path`, `task_source.plan_paths`, or
   `task_source.profile` disable generated discovery for the active source.
3. Default config values are not explicit. Omitted `task_source.plan_path`,
   default `plan_paths`, and default runnable statuses do not block a generated
   cache.
4. User-authored non-source settings override generated fields without disabling
   the generated source. The current example is `task_source.runnable_statuses`,
   which replaces `profile.status_map.runnable` during normalization.
5. Generated profile fields fill only unset behavior. They never override a
   present `.vibe-loop.toml` key.

`doctor` should report the active task-source origin as one of explicit config,
command output, generated cache, planning-only cache, unavailable cache, or no
usable source. `tasks configure --json` should expose the same origin, cache
path, schema and prompt versions, confidence, fingerprints, skipped evidence,
and the next safe action. Users override generated behavior by adding explicit
`[task_source]` settings to `.vibe-loop.toml`. Promotion must copy only the
non-executable parser profile into explicit configuration. Agent metadata,
fingerprints, provenance, and degradation state remain cache diagnostics and do
not belong in committed config.

## Forbidden Generated Fields

Generated profiles are non-executable parser descriptions. The validator must
reject all executable task-source adapters or raw command fields from the full
cache envelope, including:

- `type = "command"`
- `list`, `next`, or `probe`
- generic command fields such as `command`, `commands`, or `selection_command`
- shell snippets, Python module/function imports, URLs to execute, or any rule
  that requires running code to enumerate tasks

Executable task sources are allowed only when a user writes them explicitly in
`.vibe-loop.toml`.

## Discovery Degradation Modes

Generated discovery should degrade in visible states, not by guessing harder.
The runner can tolerate missing optional metadata, but it needs a stable way to
decide whether a result is runnable, planning-only, or unusable.

- Explicit config or command adapters remain authoritative. If they are present
  and fail, report that failure instead of replacing them with generated state.
- A valid generated profile can omit optional fields such as priority, evidence,
  or acceptance, but must still produce stable IDs, titles, statuses, and source
  locations. Missing optional fields should be rendered as unknown, not invented.
- If dependencies cannot be inferred, tasks should be treated as independent
  only when the source format clearly has no dependency concept. Ambiguous
  dependency syntax should make the profile low-confidence.
- If statuses cannot be mapped, the agent may propose a conservative planning
  profile, but the CLI should not run tasks from it until a status policy is
  explicit.
- If stable task IDs do not exist, cache-local synthetic IDs are acceptable only
  for planning review. They must be tied to source fingerprints and treated as
  unstable across refreshes unless promoted into explicit configuration.
- If candidate task sources are missing, too large, unreadable, contradictory,
  or outside evidence-gathering limits, cache an unavailable/needs-input result
  with precise missing information instead of repeating an agent call on every
  command.

When task information is insufficient for runnable work, `vibe-loop` can launch
a bounded planning agent rather than fail blind, but only from explicit
configuration commands such as `tasks configure`, refresh, or a clearly named
opt-in flag. Read-only commands such as `tasks list`, `tasks runnable`, `next`,
and `doctor` should never launch an agent as a side effect. They may read fresh
cache and report the command that would refresh it.

Fresh runnable profiles are reused by `tasks configure` so repeated diagnostics
do not spend agent calls unnecessarily. Passing `--force-refresh` bypasses that
reuse. Passing `--dry-run` always keeps the current cache untouched and prints
the validated candidate for review.

The planning agent should inspect the same bounded evidence set, write a
structured planning result under the configured state directory, and classify
what is missing: candidate source paths, likely task format, unsupported fields,
questions for the user, and proposed explicit configuration. References to
`.vibe-loop/` in examples mean the default configured state directory, not a
hard-coded path. This planning cache is diagnostic state, not a task source,
until it passes the normal profile validation. It gives `doctor`,
`tasks configure`, and `tasks list` enough context to explain the degraded state
without polluting repository docs or silently creating runnable work.

The evidence collector for generated discovery should be deterministic and
testable. It should gather repo-local docs and config likely to describe work,
respect ignored build/state directories, enforce per-file and total byte limits,
skip binary files, skip secret-like paths such as `.env`, key files, credential
directories, and private state, and avoid environment-variable dumps entirely.
Any redaction should happen before prompt construction, and skipped evidence
should be listed by reason in the cache so users can see whether missing
information is a deliberate safety boundary or an actual absence of planning
data.
