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

Markdown task sources must support the built-in table format and profile-driven
parsing for other Markdown tables, headings, and lists without requiring a
repository to adopt a prescribed task filename or layout.

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
the runnable-to-in-progress compare-and-set when the task is still runnable and
returns normalized post-state; read-only list and probe operations remain
available without activation.

The command keys are `list`, optional `probe`, required-for-launch `activate`,
and lifecycle hooks `complete`, `reset`, and `park`. Templates receive
shell-quoted task/run identifiers. The operator-authored template, including
pipelines and other shell operators, is trusted configuration; task identifiers
and run identifiers originate in task/backend or runtime data and are untrusted
template values. Only the documented exact placeholders are accepted, without
conversions or format specifications. On Windows, values containing quotes,
percent expansion, delayed-expansion markers, or line breaks fail closed
because `cmd.exe` cannot safely preserve them in an operator-authored shell
string. Activation runs only after the exact task lock is held. When `probe` is
configured, the runtime first reads the post-lock task state. A non-runnable
`active` state confirms that the lock backend already performed activation, so
the configured activation command is not repeated. Every other state reaches
the configured compare-and-set; a state changed by another actor must therefore
be refused rather than treated as lock-side activation. A runnable state
requires the activation command to atomically move the task to a non-runnable
in-progress state and return that normalized task. This keeps activation
correct whether or not lock acquisition updates task state. A continuation
probes the existing activated state rather than repeating the compare-and-set.

```toml
[task_source]
type = "command"
list = "my-task-tool list --json"
probe = "my-task-tool show {task_id} --json"
activate = "my-task-tool activate {task_id} --run {run_id} --json"
complete = "my-task-tool complete {task_id} --run {run_id} --json"
reset = "my-task-tool reset {task_id}"
park = "my-task-tool park {task_id} --run {run_id} --json"
```

`list` returns an array or `{"tasks":[...]}`. Each task provides `id`, `title`,
`status`, `priority`, `dependencies`, `scope`, `acceptance`, and `evidence`.
Optional `resources` and `paths` arrays declare scheduling conflicts;
[runtime candidate-scope behavior](run-orchestration.md#prd-orc-004-runtime-gates-and-candidate-stabilization)
is owned by the Run Orchestration PRD. A probe that reports conflict-domain
changes may also provide `conflict_domains_actor_kind` (`operator`, `worker`,
`runtime`, or `external-system`) and a non-empty `conflict_domains_actor`
identity. Missing provenance is not operator authorization. Optional
`requirement_ids`, `spec_paths`, `design_refs`, `approval_state`, and
`source_fingerprints` preserve traceability through task JSON, worker prompts,
and promotion.

Command task objects may provide an optional top-level `planning_warnings`
array. Omission or `null` means no persisted warnings; a member of `fields` is
not runtime metadata. The array contains at most three objects, each with
exactly the string keys `kind`, `requested_path`, `existing_path`, `match`,
`effect`, and `precision`. `kind` is `deliverable_path_collision`; `match` is
`exact` or `same_directory_sibling`; `effect` is `advisory_only`; and
`precision` is `high recall for paths governed by a creation verb in the same
clause; paths attached by modification prepositions are excluded`. Both paths
are non-empty repository-relative POSIX paths accepted by the normalized path
lock contract, name files with non-empty suffixes, and occupy at most 1,024
UTF-8 bytes each. A warning's compact JSON encoding occupies at most 2,048
UTF-8 bytes. Unknown or missing keys, invalid enums or paths, excess entries,
and malformed types reject the command source response; command list responses
normalize atomically rather than silently dropping an invalid task. Rejection
diagnostics always identify the zero-based list entry, then use the first usable
identity in the order `id`, `task_id`, title; when none is usable, the index is
the complete record context. This bounded context lets the authoritative
producer repair the exact record without copying unrelated task payloads or the
configured command into the diagnostic. Valid warning paths may name root-level
files or contain Unicode directory components and filenames, and round-trip in
normalized repository-relative POSIX form. Authoritative source-side storage
and explicit projection of this field are external producer prerequisites:
vibe-loop consumes them but does not implement or own Loopyard persistence or
projection. At dispatch, the runtime applies the
[persisted-first advisory union contract](autopilot.md#prd-aut-012-configuration-free-generic-cycle).

Command task objects may also provide `prior_findings` and
`review_budget_exhaustions`.
`prior_findings` uses the review finding wire shape (`id`, `severity`, `summary`,
`evidence`, `files`, `lines`, and `state`); `review_budget_exhaustions` is the
non-negative count of consecutive runs that exhausted the review-remediation
budget. These values may be top-level task properties or members of a top-level
`fields` object.

Runtime-owned command sources with activation must also configure `reset`, and
adapter-owned completion requires `complete`. `complete` must return a terminal
done state after integration. `park` must return a non-runnable held state after
terminal failure; when absent, the runtime uses its recorded reset/requeue
fallback. Unconfirmed settlement retains the task lock for fenced recovery.
External-confirmed completion instead requires probe capability and an explicit
completion actor of `operator` or `external-system`; a runtime-owned worker
cannot be that actor.

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

The active task source remains authoritative for dependencies and task status.
Run records are attempt history and never advance task state. Omitted or `null`
conflict-domain arrays mean undeclared domains; empty arrays explicitly declare
none. Resource names match exactly, while repository-relative path domains
conflict when one is an ancestor of another.

`run-next` selects from the runnable set. `run <task_id>` instead resolves only
the named task and never falls through to another task. Explicit dispatch
requires that task to exist, have an allowed runnable status, have all
dependencies done, have no existing task lock, and not conflict with domains
held by live runs. It then enters the same lock acquisition, task-source
activation, run-record, gate, review, and completion lifecycle as selected
dispatch. A concurrent lock or activation refusal terminates the command with a
diagnostic naming the requested task. Explicit dispatch does not reorder tasks
or otherwise mutate task-source priority.

Spec diagnostics are read-only by default. Repositories can opt into execution
gates:

```toml
[specs]
require_approved = true
require_current_fingerprints = true
require_requirement_coverage = true
require_completion_evidence = true
approved_states = ["approved"]
override_commands = ["make specs-override"]
```

The `require_*` settings gate `run`, `run-next`, and `run-until-done`;
read-only `doctor` and `specs check` remain available. Override commands are
reported as repository-owned recovery guidance and are not run automatically.

Related implementation IDs: `CORE-02`, `DISC-10`, `PAR-01`, `PAR-07`,
`PAR-08`.

## PRD-TSK-008 Adapter Capability Diagnostics

An external task-source adapter may expose a versioned deployment identity
through an explicitly configured diagnostic command. The option spelling is
owned by the
[configuration reference](../configuration.md#task-source-configuration).
It is observed deployment evidence only: it does not select a task source,
disable generated discovery, run during dispatch, gate lock acquisition, or
infer an expected adapter or fingerprint from PATH, other task-source commands,
adapter names, or local checkouts.

`doctor` runs the diagnostic only when explicitly configured, with the existing
task-source command timeout. It streams stdout and stderr concurrently, retains
at most 64 KiB from each, and drains/discards stderr beyond its retention cap.
On timeout or stdout overflow it terminates the owned process group and reaps
the process. Other task-source command execution semantics remain unchanged.

The consumer accepts schema version 1; bounded non-empty adapter, package, and
package-version strings; a complete `sha256:` fingerprint with 64 lowercase
hexadecimal characters; nullable boolean editable-install classification; and a
bounded list of bounded non-empty capability strings. It forwards only those
known producer fields. When reset is configured, the observed capabilities must
include `task-source-reset:fenced-owner:v1`; without reset, absence of that
capability is not a deployment gap. Vibe-loop does not label the observed
identity current, stale, intended, or matching because it has no independent
expected fingerprint.

The diagnostic contract produced by Loopyard remains owned by
[Loopyard's vibe-loop contracts](https://github.com/ei-grad/loopyard/blob/main/docs/vibe-loop-contracts.md).
The [CLI reference](../cli-reference.md#vibe-loop-doctor) owns the public doctor
subtree, statuses, reasons, and redaction-safe output shape.

Acceptance must cover opt-in execution, absent-option compatibility, bounded
live collection, timeout/overflow termination and reap, strict v1 validation,
complete fingerprint preservation, nullable editable-install classification,
unknown-field omission, conditional fenced-reset capability, fixed doctor
projection, command and stream secrecy, complete/park/capabilities command
redaction, and comparison against the real Loopyard producer.
