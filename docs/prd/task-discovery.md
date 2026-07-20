# Task Discovery PRD

This PRD owns Level 2 contracts for turning repository planning artifacts into
normalized runnable tasks.

## PRD-TSK-001 Normalized Task Model

Every task source must normalize work into stable task records with at least an
ID, title, status, source provenance, and order. Optional fields should preserve
priority, dependencies, section, scope, acceptance, evidence, resource domains,
and path domains.

Acceptance must cover deterministic sorting, done/runnable/blocked status
classification, dependency readiness, JSON output, duplicate ID detection,
invalid dependency diagnostics, and backward compatibility for sources that omit
optional fields.

Related implementation IDs: `CORE-02`, `DISC-03`, `DISC-10`, `PAR-08`,
`GANTT-01`.

## PRD-TSK-002 Markdown Task Sources

Markdown task sources must support this repository's table format and
profile-driven parsing for other Markdown tables, headings, and lists without
requiring every repository to adopt `vibe-loop`'s local `PLAN.md` shape.

Acceptance must cover explicit `plan_path`, discovered plan candidates,
ambiguous-discovery failures, profile field mappings, required fields,
`none_values`, acceptance/evidence extraction, and future heading-based plans
such as ralphex-style task sections.

Related implementation IDs: `CORE-02`, `DISC-03`, `DISC-05`, `DISC-09`.

## PRD-TSK-003 Command Task Sources

Command-backed task sources must let user-authored tools enumerate and probe
tasks through bounded JSON contracts while keeping executable behavior explicit
in `.vibe-loop.toml`.

Acceptance must cover array and `{"tasks":[...]}` list output, probe behavior,
required fields, optional conflict domains, adapter failure diagnostics, and no
substitution of generated discovery when an explicit adapter fails. Worker
execution must additionally require an explicit activation command that owns
the runnable-to-in-progress compare-and-set and returns normalized post-state;
read-only list and probe operations remain available without activation.

Related implementation IDs: `DISC-01`, `DISC-04`, `PAR-08`.

## PRD-TSK-004 Generated Discovery Cache

Generated task-source discovery must create a versioned, repo-local,
non-executable parser cache from bounded repository evidence when explicitly
requested through configuration commands.

Acceptance must cover schema and prompt versions, source fingerprints,
confidence, redacted provenance, skipped evidence, agent identity, command-source
metadata, status `profile`, and cache freshness checks.

Related implementation IDs: `DISC-01`, `DISC-02`, `DISC-04`, `DISC-05`,
`DISC-08`.

## PRD-TSK-005 Generated Discovery Safety

Generated discovery must never introduce executable task adapters, raw commands,
shell snippets, imports, or URL execution rules. Generated profiles may only
describe how to parse bounded repo-local artifacts.

Acceptance must cover rejection of forbidden generated fields, secret-like path
skips, binary and size-limit skips, ignored build/state directories, no
environment variable dumps, redaction before prompt construction, and skipped
evidence reporting.

Related implementation IDs: `DISC-01`, `DISC-02`, `DISC-08`.

## PRD-TSK-006 Discovery Degradation

When discovery cannot safely produce runnable tasks, the CLI must preserve a
visible degraded state instead of guessing harder.

Acceptance must cover `planning_only`, `needs_input`, `unavailable`, and
`rejected` cache states; actionable diagnostics; stale-cache behavior;
read-only commands that do not launch agents; and `tasks configure --dry-run`,
`--force-refresh`, and `--promotion-toml` review paths.

Related implementation IDs: `DISC-02`, `DISC-04`, `DISC-05`, `DISC-07`.

## PRD-TSK-007 Task Selection Semantics

Runnable work must be selected from tasks whose status is allowed, dependencies
are done, and local locks do not block execution.

Acceptance must cover default runnable statuses `Active`, `Next`, and
`Planned`; deterministic ordering by status rank, priority rank, and source
order; case-insensitive semantic done, rank, and blocked-family comparisons;
exact-case configured runnable-status allowlists; lock exclusion; agent-assisted
selection validation; and conflict-domain filtering when parallel scheduling is
active.

Related implementation IDs: `CORE-02`, `DISC-10`, `PAR-01`, `PAR-07`,
`PAR-08`.
