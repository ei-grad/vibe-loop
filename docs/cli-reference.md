# CLI Reference

This file is the operator-facing reference for `vibe-loop` command invocation
and flags. Start with the [README](../README.md) for installation, task
inspection, and routine diagnostics. Configuration options live in the
[configuration reference](configuration.md).

## Document Boundaries

This reference owns command syntax, flag meanings, defaults, and immediate
output conventions. The linked PRDs remain authoritative for lifecycle and
product behavior:

| Commands | Behavioral authority | Why |
| --- | --- | --- |
| `autopilot` | [Autopilot PRD](prd/autopilot.md#prd-aut-002-command-surface) | The PRD owns supervisor lifecycle, configuration lifetime, reload safety, and process identity. |
| `run`, `run-next`, `run-until-done` | [Run orchestration PRD](prd/run-orchestration.md#prd-orc-009-scheduler-and-runtime-separation) | The PRD owns scheduling, selection, conflict-domain, lifecycle, and budget contracts. |
| `report`, `worker`, `main-integration` | [Worker supervision PRD](prd/worker-supervision.md#prd-wrk-003-worker-reports) | The PRD owns worker reports, workspace claims, candidate declarations, settlement, and integration locking. |
| `eval` | [Evals and release PRD](prd/evals-release.md) | The PRD owns evaluation behavior, artifact contracts, external adapters, and release policy; the [evaluation strategy](skill-evaluation-strategy.md) provides methodology and rationale. |
| Session linkage, recovery, and usage telemetry | [Autopilot PRD](prd/autopilot.md#prd-aut-013-observed-agent-session-id-and-transcript-linkage) | PRD-AUT-013, PRD-AUT-014, and PRD-AUT-016 own these run-provenance contracts. |

## Global Options

### `vibe-loop --version`

Print the installed package version and exit. Fixed non-tag Git installs append
the recorded revision as `(git <short-sha>)`; release-tag and regular package
installs print only the package version.

Editable installs identify their live source root and revision as
`(editable: <source-root>, git <short-sha>)`. The revision gains a `-dirty`
suffix when tracked or untracked source changes are present, excluding runtime
state under `.vibe-loop`. If the source root or Git state cannot be read, the
editable annotation reports `revision unknown`. An editable source at the
matching release tag keeps release-revision suppression: a clean tree prints
only its source-root annotation, while modified source appends `, dirty`.

## Diagnostic Commands

### `vibe-loop doctor`

Print the resolved, redacted repository diagnostics:

```bash
vibe-loop doctor --repo . --json
```

- `--repo PATH` selects the repository. It defaults to the current directory.
- `--json` requests the structured document. Doctor retains its existing JSON
  output compatibility when the flag is omitted.

The top-level `task_source_adapter` object always has this fixed shape:

```json
{
  "capabilities_command_configured": true,
  "capabilities_command_redacted": true,
  "status": "available",
  "reason": null,
  "identity": {
    "schema_version": 1,
    "adapter": "loopyard-vibe",
    "package": "loopyard",
    "package_version": "0.1.2",
    "source_fingerprint": "sha256:<64 lowercase hexadecimal characters>",
    "editable_install": true,
    "capabilities": ["task-source-reset:fenced-owner:v1"]
  }
}
```

`status` is `not_configured`, `available`, or `deployment_gap`. `identity` is
the validated producer document only for `available`; otherwise it is `null`.
`reason` is `null` for `not_configured` and `available`. For `deployment_gap`,
it is `command_start_failed`, `command_failed`, `command_timeout`,
`stdout_limit_exceeded`, `invalid_json`, `invalid_document`, or
`required_capability_missing`. The command flags are both false when omitted and
both true when configured, independent of probe outcome. Doctor exits zero for
all three adapter diagnostic statuses.

The subtree never includes command text, arguments, environment values, raw
stdout or stderr, or working/source paths. Existing doctor repository, config,
state, workspace, and log path fields remain intentional. The
[configuration reference](configuration.md#task-source-configuration) owns the
option spelling; [PRD-TSK-008](prd/task-discovery.md#prd-tsk-008-adapter-capability-diagnostics)
owns execution and validation behavior.

## Run Commands

### `vibe-loop run`

Run one explicitly named task:

```bash
vibe-loop run TASK-01 --repo .
```

- `TASK_ID` names the task to run. It is required, and no other task is ever
  selected in its place.
- `--repo PATH` selects the repository. It defaults to the current directory.

Use this when the selection is already made. `run-next` answers "pick work for
me"; `run` answers "run this one", and reports why it cannot rather than
falling back to a different task. Output follows the same stdout, stderr, log,
and `run_id` conventions as `run-next`.

The explicit-dispatch and selection contract is
[Task Selection Semantics](prd/task-discovery.md#prd-tsk-007-task-selection-semantics).

### `vibe-loop run-next`

Run one selected task:

```bash
vibe-loop run-next --repo . --ask-agent
```

- `--repo PATH` selects the repository. It defaults to the current directory.
- `--ask-agent` requests configured agent-assisted selection.

Result JSON remains on stdout. Progress and mirrored agent output go to stderr,
and the complete streams are written to `.vibe-loop/runs/<run-id>.log`. The
result contains its `run_id`.

### `vibe-loop run-until-done`

Run finite slices until no runnable task remains:

```bash
vibe-loop run-until-done --repo . --ask-agent --jobs 2 \
  --max-slices 10 --max-tasks 5
```

- `--repo PATH` selects the repository.
- `--ask-agent` enables validated agent-assisted selection.
- `--jobs N` keeps up to `N` workers active. The default is `1`.
- `--max-slices N` caps dispatched attempts, regardless of outcome.
- `--max-tasks N` caps completed tasks.
- `--continue-on-failure` continues after an individual failed result.

`0` means unlimited for both stop limits. Whichever nonzero limit is reached
first stops the loop. Output follows the same stdout, stderr, log, and `run_id`
conventions as `run-next`.

The scheduler and runtime-owned lifecycle contract is
[PRD-ORC-009](prd/run-orchestration.md#prd-orc-009-scheduler-and-runtime-separation).
For session linkage, provider usage, and compatibility recovery, see
[PRD-AUT-013](prd/autopilot.md#prd-aut-013-observed-agent-session-id-and-transcript-linkage),
[PRD-AUT-016](prd/autopilot.md#prd-aut-016-provider-usage-run-telemetry), and
[PRD-AUT-014](prd/autopilot.md#prd-aut-014-unknown-run-recovery-and-continuation).

## Autopilot Commands

### `vibe-loop autopilot status`

Collect a read-only project snapshot without launching workers or changing
state:

```bash
vibe-loop autopilot status --repo . --json
```

- `--repo PATH` selects the repository.
- `--json` emits the structured `ProjectStatus` payload; without it, the
  command renders a human-readable summary.

The payload includes queue counts, runnable tasks, active workers, stale locks,
workspace and git diagnostics, the main-integration lock, supervisor state,
blockers, project binding, the last cycle, and `disk_headroom` with its live
filesystem reading, thresholds, verdict, and blocker evidence. It also includes
a `latest_main_verification_failure` projection when the latest terminal run
ended for that reason, naming the failed command, exit status, and retained
output tail. The key is an empty object when the latest terminal run has no such
failure. Human-readable output prints the failure immediately after the
non-closure summary, with the retained output on indented lines. The payload
also includes a bounded projection of the latest `worktree_disposition` journal
record,
prioritizing refused, failed, and otherwise reapable worktrees and including
their collection-time `keep_guardrails`, outcome guardrails, action errors, and
non-removal reasons. The projection reports the total and omitted worktree
counts when it truncates the detail list. Human-readable output prints the same
disk-headroom verdict, mount, free bytes, and warning and hard-stop thresholds
immediately after the repository line, followed later by the latest
per-worktree disposition evidence and reason. Status and inconsistency semantics
are defined by
[PRD-AUT-001](prd/autopilot.md#prd-aut-001-reusable-status-core).

### `vibe-loop autopilot run`

Run the foreground supervisor:

```bash
vibe-loop autopilot run --repo . --once
vibe-loop autopilot run --repo . --interval 60 --max-cycles 10 --jobs 2
```

- `--repo PATH` selects the repository.
- `--jobs N` sets child worker concurrency and overrides `[autopilot].jobs`;
  the default is `1`.
- `--interval SECONDS` enables persistent cycles. Positive values must be at
  least `60`; zero or omission selects drain mode.
- `--once` runs one cycle and exits.
- `--max-cycles N` caps cycles; `0` means unlimited.
- `--ask-agent`, `--continue-on-failure`, `--max-slices N`, and
  `--max-tasks N` are forwarded to each `run-until-done` child.
- `--min-ready N` sets the positive runnable depth below which planning refill
  runs and overrides `[autopilot].min_ready`; the default is `1`.
- `--dispatch-min-ready N` sets the independent positive runnable depth
  required before child launch and overrides
  `[autopilot].dispatch_min_ready`; the default is `1`.
- `--worktree-disposition report-only|reap` overrides the configured policy;
  the default is `report-only`.

The bare `vibe-loop autopilot` command is a shorthand for `autopilot run`.
Foreground supervision, scheduling, recovery, and disposition behavior are
defined by [PRD-AUT-004](prd/autopilot.md#prd-aut-004-child-supervisor),
[PRD-AUT-006](prd/autopilot.md#prd-aut-006-non-destructive-recovery-boundary),
and [PRD-AUT-010](prd/autopilot.md#prd-aut-010-native-worktree-disposition-health-step).

### `vibe-loop autopilot start`

Start and verify a detached POSIX supervisor:

```bash
vibe-loop autopilot start --repo . --interval 60 --jobs 2 --json
```

`start` accepts the same cycle and child options as `autopilot run`, plus
`--json`. Structured output contains the supervisor run ID, PID, process-group
ID, session ID, and log path. The command returns only after verifying the
process and matching singleton lock. It is not a boot service; the detached
lifecycle and platform boundary are defined by
[PRD-AUT-004](prd/autopilot.md#prd-aut-004-child-supervisor).

### `vibe-loop autopilot stop`

Stop a verified detached supervisor, or explicitly recover its stale singleton
lock:

```bash
vibe-loop autopilot stop --repo . --timeout 10 --json
vibe-loop autopilot stop --repo . --recover-stale \
  --run-id SUPERVISOR-RUN-ID --json
```

- `--repo PATH` selects the repository.
- `--timeout SECONDS` bounds the stop; the default is `10`.
- `--recover-stale` selects the absent-owner recovery path instead of signaling
  a live supervisor.
- `--run-id ID` supplies the exact recorded supervisor run required for stale
  recovery.
- `--json` emits the structured stop result.

The live stop path is Linux-only and never escalates to `SIGKILL`. Success
requires both the verified process tree and singleton lock to be absent.
Identity, drain, fencing, and recovery behavior are defined by
[PRD-AUT-004](prd/autopilot.md#prd-aut-004-child-supervisor).

### `vibe-loop autopilot reload`

Request an acknowledged configuration reload from the running detached
supervisor:

```bash
vibe-loop autopilot reload --repo . --timeout 10 --json
```

- `--repo PATH` selects the repository.
- `--timeout SECONDS` bounds the wait for supervisor acknowledgement; the
  default is `10`. A value of `0` submits the request without waiting.
- `--json` emits the reload state, supervisor identity, request ID, changed
  keys, load time, resulting fingerprint, and any refusal reason.

The command exits successfully for a loaded, unchanged, or queued request. If
the acknowledgement wait expires, output state is `pending`: the request
remains queued and the supervisor will still process it at its next cycle
boundary. Refused, invalid, or unverifiable requests exit nonzero.
Reload-safe settings and atomic refusal behavior are defined by
[Supervisor Configuration Lifetime](prd/autopilot.md#prd-aut-002b-supervisor-configuration-lifetime).

### `vibe-loop autopilot projects`

Manage the optional multi-project registry:

```bash
vibe-loop autopilot projects register --repo . --name my-project \
  --context LOOPYARD_PROJECT=vibe-loop --json
vibe-loop autopilot projects list --json
vibe-loop autopilot projects inspect my-project --json
vibe-loop autopilot projects remove my-project --json
vibe-loop autopilot projects status --json
```

- `register` accepts `--repo PATH`, `--name NAME`, repeatable
  `--context NAME=VALUE`, and the common registry and JSON options.
- `list` prints registered repositories.
- `inspect PROJECT` selects one entry by name or path.
- `remove PROJECT` removes one entry by name or path.
- `status` returns an aggregate status entry for every registered repository.
- `--registry PATH` selects the JSON registry; the default is
  `~/.vibe-loop/projects.json`.
- `--json` requests structured output for each subcommand.

Registry selector validation, literal adapter delivery, and redaction are
defined by [PRD-AUT-007](prd/autopilot.md#prd-aut-007-multi-project-shape);
binding resolution is defined by
[PRD-AUT-020](prd/autopilot.md#prd-aut-020-command-backend-project-binding).

### `vibe-loop runs summary`

Summarize a rolling provider-usage window:

```bash
vibe-loop runs summary --repo . --hours 24 --json
```

- `--repo PATH` selects the repository.
- `--hours NUMBER` sets a positive lookback window; the default is `24`.
- `--json` emits structured output.

Provider usage and quota behavior is defined by
[PRD-AUT-016](prd/autopilot.md#prd-aut-016-provider-usage-run-telemetry).

To copy normalized run records into an existing Loopyard project, use Loopyard's
own command:

```bash
loopyard runs sync .vibe-loop/runs.jsonl -p <project>
```

### `vibe-loop attempt-circuit status`

Show open cross-run attempt breakers:

```bash
vibe-loop attempt-circuit status --repo . --json
```

- `--repo PATH` selects the repository.
- `--json` emits structured output.

### `vibe-loop attempt-circuit reset`

Record an explicit operator reset for one task breaker:

```bash
vibe-loop attempt-circuit reset TASK-ID --repo . --json
```

- `TASK-ID` identifies the task whose breaker is reset.
- `--repo PATH` selects the repository.
- `--json` emits structured output.

## Wait Commands

### `vibe-loop wait-helper`

Wait for process completion, a wall-clock boundary, a direct message, or an
actionable runtime event:

```bash
vibe-loop wait-helper --pid 12345 --json
vibe-loop wait-helper --cycle-schedule 1800 --json
vibe-loop wait-helper --pid 12345 \
  --runtime-event-journal .vibe-loop/runs.jsonl \
  --runtime-event-cursor .vibe-loop/wait-runtime.cursor \
  --runtime-event-project my-project \
  --runtime-event-start-at-tail --json
```

- `--pid PID` is repeatable. The default `--mode any` wakes on the first exit;
  `--mode all` waits for every PID.
- `--deadline TIME` accepts an ISO-8601 UTC deadline.
- `--cycle-schedule [SECONDS]` wakes at the next UTC boundary. When no deadline
  or schedule is supplied, the default boundary is 1800 seconds.
- `--interval SECONDS` sets the process poll interval; the default is `5`.
- `--message-command COMMAND` enables the trusted direct-message adapter.
  `--session-ref REF` selects its recipient, falling back to
  `VIBE_LOOP_RUN_ID`; `--message-timeout SECONDS` defaults to `5`.
- `--runtime-event-command COMMAND` or `--runtime-event-journal PATH` selects
  one typed runtime-event source. It requires `--runtime-event-cursor PATH` and
  `--runtime-event-project PROJECT`; optional `--runtime-event-run-id` and
  `--runtime-event-task-id` narrow the scope. `--runtime-event-timeout SECONDS`
  defaults to `5`.
- `--runtime-event-start-at-tail` initializes a missing journal cursor at the
  validated current record boundary before waiting. The default remains replay
  from the beginning. An existing cursor is never changed unless
  `--runtime-event-replace-cursor` is also supplied explicitly.
- `--json` emits the structured result. `wake_reason` is `pid`,
  `all_complete`, `deadline`, `message`, `runtime_event`, or `adapter_error`.

The trusted-adapter schemas, redaction limits, durable cursor, and wake
precedence are defined by
[PRD-AUT-015](prd/autopilot.md#prd-aut-015-direct-user-message-wake).

## Worker Lifecycle Commands

These commands are intended for active worker runs. Internal fencing-token
options are omitted from operator examples; their contract is in the
[worker supervision PRD](prd/worker-supervision.md).

### `vibe-loop report`

Record a structured worker result:

```bash
vibe-loop report --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --status blocked --commit HEAD --message "waiting on reviewer" \
  --metadata-json '{"reason":"review"}'
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the claimed run.
- `--status STATUS` accepts `completed`, `blocked`, `failed`, or `unknown`.
- `--commit REF` records the relevant commit.
- `--message TEXT` records a concise human-readable result.
- `--metadata-json OBJECT` records structured metadata.

Report classification and task-source semantics are defined by
[PRD-WRK-003](prd/worker-supervision.md#prd-wrk-003-worker-reports).

### `vibe-loop worker claim-workspace`

Attach advisory branch and worktree metadata to an active task lock:

```bash
vibe-loop worker claim-workspace --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --branch "$BRANCH" --worktree "$WORKTREE"
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the active lock.
- `--branch NAME` and `--worktree PATH` are required.
- `--base-commit REF` can record an explicit base.
- `--json` emits structured output.

Workspace-claim behavior and safety boundaries are defined by
[PRD-WRK-006](prd/worker-supervision.md#prd-wrk-006-workspace-claims).

### `vibe-loop worker candidate`

Declare the committed candidate from the claimed workspace:

```bash
vibe-loop worker candidate --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --head HEAD
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the claimed run.
- `--head REF` is required.
- `--base-main REF` can supply the recorded base.
- `--changed-path PATH` can be repeated to declare changed paths.
- `--json` emits the candidate and its structured `scope_assessment`.
- Plain-text success output appends
  `scope_signal=candidate_scope_unenforceable` when no path domains can enforce
  the comparison.

Candidate validation and runtime-gate behavior are defined by
[PRD-ORC-004](prd/run-orchestration.md#prd-orc-004-runtime-gates-and-candidate-stabilization).

### `vibe-loop main-integration acquire`

Acquire the advisory integration lock:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --wait --timeout 300
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the active run.
- `--pid PID` supplies the worker process id when it cannot be derived.
- `--wait` enables waiting for the lock.
- `--timeout SECONDS` bounds that wait.
- `--poll-interval SECONDS` controls lock polling; the default is `1`.
- `--json` emits structured output.

### `vibe-loop main-integration release`

Release the advisory integration lock owned by the active run:

```bash
vibe-loop main-integration release --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID"
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the owner.
- `--json` emits structured output.

### `vibe-loop main-integration status`

Inspect the advisory integration lock:

```bash
vibe-loop main-integration status --repo . --json
```

- `--repo PATH` selects the repository.
- `--json` emits structured output.

## Evaluation Commands

Evaluation behavior and release policy are defined by the
[evals and release PRD](prd/evals-release.md). The
[skill evaluation strategy](skill-evaluation-strategy.md) explains methodology
and evidence interpretation.

### `vibe-loop eval local-demo`

Run the bundled local paired-condition suite:

```bash
vibe-loop eval local-demo --repo . --trials 3 \
  --agent-command '*=codex exec {prompt}'
```

Common flags are:

- `--repo PATH` and `--output PATH`
- repeatable `--case NAME` and `--condition NAME`
- `--trials N`
- repeatable `--agent-command CONDITION=COMMAND`
- repeatable `--transcript-grader COMMAND`
- `--timeout-seconds N`, `--max-commands N`, and `--max-output-bytes N`
- `--overwrite`
- `--agent-name NAME`, `--model-provider NAME`, `--model-id ID`, and
  `--reasoning-effort EFFORT`
- `--json`

Artifact and aggregate behavior is defined by
[PRD-EVL-002](prd/evals-release.md#prd-evl-002-artifact-schema) and
[PRD-EVL-004](prd/evals-release.md#prd-evl-004-aggregate-skill-quality-reporting).

### `vibe-loop eval release-gate`

Run or validate bundled-skill release readiness:

```bash
vibe-loop eval release-gate --repo . --overwrite \
  --record-output .vibe-loop/release-readiness.json
```

In addition to the local-demo selection, routing, budget, and provenance flags,
release-gate accepts:

- `--aggregate PATH` to validate an existing aggregate instead of running evals
- `--eval-output PATH`
- `--record-output PATH`
- `--dry-run`
- `--minimum-trials N` (default `1`)
- repeatable `--parked-regression REGRESSION_ID=TASK_ID`
- repeatable `--parked-workflow-regression TASK_ID`
- repeatable `--external-benchmark-json PATH`
- `--json`

The command resolves the current full commit and prior reachable `v*` release
tag, requires a clean tracked worktree for newly executed evals, and emits
schema-version-2 revision and bundled-skill bindings. Existing aggregates that
lack matching trial source evidence are diagnostic dry runs, not publishable
records.

### `vibe-loop eval release-classify`

Write the exact-base/head change classification used by publishing:

```bash
vibe-loop eval release-classify --repo . \
  --output release-classification.json --json
```

The base is discovered from reachable release-tag provenance; there is no
operator-supplied base option. `--output` is required and `--json` also prints
the record.

### `vibe-loop eval release-admit`

Build or verify the final distribution-bound admission record:

```bash
vibe-loop eval release-admit --repo . \
  --classification release-classification.json \
  --readiness-record release-readiness.json \
  --distribution dist/vibe_loop.whl \
  --output release-admission.json
```

`--classification`, repeatable `--distribution`, and `--output` are required.
`--readiness-record` is required by the classification, not for a validated
unrelated exemption. `--verify` re-hashes transferred inputs and rejects any
admission or distribution substitution.

Release-readiness behavior is defined by
[PRD-EVL-005](prd/evals-release.md#prd-evl-005-release-readiness-gate).

### `vibe-loop eval benchmark`

Run an explicit external benchmark adapter:

```bash
vibe-loop eval benchmark --repo . --output path/to/results \
  --adapter manifest --manifest path/to/benchmark.json
```

- `--repo PATH` selects the repository.
- `--output PATH` and `--adapter NAME` are required.
- `--manifest PATH` supplies a manifest to the manifest adapter.
- repeatable `--agent-command CONDITION=COMMAND`, `--instance ID`, and
  `--condition NAME` select execution.
- `--trials N` and `--timeout SECONDS` set repetition and timeout.

External-adapter behavior is defined by
[PRD-EVL-006](prd/evals-release.md#prd-evl-006-external-benchmark-adapters).
