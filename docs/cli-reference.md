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
- `--json` emits structured output.

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
