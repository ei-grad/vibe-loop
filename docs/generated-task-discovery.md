# Generated Task Discovery

## Repo-Specific Task Discovery Configuration

The current Markdown discovery model still assumes the repository exposes tasks
through a specific table:

`ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence`

That is acceptable for this repository's own plan, but it is the wrong generic
contract. A task-system-agnostic runner should not ask every repository to
reshape planning docs before it can discover work. The generic path should ask a
configured agent to analyze bounded repository evidence, generate a normalized
task-source profile, validate that profile mechanically, and cache it as
repo-local state.

The agent-generated profile should describe how to read existing repo artifacts,
not invent task state. It can map column names, heading/list conventions,
statuses, dependency syntax, done states, candidate files, and title/acceptance
extraction rules into the existing normalized `Task` model. The CLI must still
own validation: required fields, runnable statuses, dependency references,
duplicate IDs, parser output shape, and cache freshness are deterministic
checks. Generated profiles must be non-executable parser descriptions over
bounded repo-local evidence. Command adapters or any other executable task
source may only come from user-authored `.vibe-loop.toml`.

Cached configuration should live under the configured state directory, include
schema and prompt versions, source file fingerprints, provenance, confidence,
and the agent command used to generate it. Explicit `.vibe-loop.toml` settings
must override the cache, and `doctor`/task commands should report whether the
active task source came from user config, generated cache, or command output.
Default config values must not count as explicit user settings; the config
loader needs to preserve which `[task_source]` keys were present so generated
state can fill only unset behavior. Source-defining explicit settings such as
command adapters, profile paths, or plan paths win at source level. Non-source
settings such as explicitly configured runnable statuses override the matching
generated profile fields without disabling the generated source.

Stale or low-confidence cache should fail with an actionable configure command
instead of falling back to the fixed table format silently.

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
