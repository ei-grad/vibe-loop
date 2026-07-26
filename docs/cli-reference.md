# CLI Reference

This file is the operator-facing reference for `vibe-loop` command invocation
and flags. Start with the [README](../README.md) for installation, task
inspection, and routine diagnostics. Configuration options live in the
[configuration reference](configuration.md).

## Authority Map

This reference owns invocation syntax only. The linked documents remain
authoritative for behavior:

| Commands | Behavioral authority | Why |
| --- | --- | --- |
| `run-next`, `run-until-done` | [Run orchestration PRD](prd/run-orchestration.md#prd-orc-009-scheduler-and-runtime-separation) | The PRD owns scheduling, selection, conflict-domain, lifecycle, and budget contracts. |
| `report`, `worker`, `main-integration` | [Worker supervision PRD](prd/worker-supervision.md#prd-wrk-003-worker-reports) | The PRD owns worker reports, workspace claims, candidate declarations, settlement, and integration locking. |
| `eval` | [Skill evaluation strategy](skill-evaluation-strategy.md) | The strategy owns trial design, grading, artifact interpretation, and release-readiness policy. |
| Session linkage, recovery, and usage telemetry | [Autopilot PRD](prd/autopilot.md#prd-aut-013-observed-agent-session-id-and-transcript-linkage) | PRD-AUT-013, PRD-AUT-014, and PRD-AUT-016 own these run-provenance contracts. |

The former README accounts were reconciled into those owners rather than copied.
The PRDs were authoritative where they reflected current implementation and
tests. PRD-AUT-013 and PRD-AUT-014 were corrected where the README and current
implementation had advanced beyond their stale Codex-session and report-less
recovery wording.

## Run Commands

### `vibe-loop run-next`

Run one selected task:

```bash
vibe-loop run-next --repo . --ask-agent
```

- `--repo PATH` selects the repository. It defaults to the current directory.
- `--ask-agent` lets the configured analysis agent choose from the mechanically
  safe candidate set. The runtime validates the answer and falls back to
  deterministic ready order.

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
- `--max-tasks N` caps completed tasks. Parallel dispatch does not exceed the
  remaining completion budget.
- `--continue-on-failure` continues after an individual failed result.

`0` means unlimited for both stop limits. Whichever nonzero limit is reached
first stops the loop. Output follows the same stdout, stderr, log, and `run_id`
conventions as `run-next`.

The scheduler and runtime-owned lifecycle contract is
[PRD-ORC-009](prd/run-orchestration.md#prd-orc-009-scheduler-and-runtime-separation).
Session and transcript linkage is
[PRD-AUT-013](prd/autopilot.md#prd-aut-013-observed-agent-session-id-and-transcript-linkage);
provider usage is
[PRD-AUT-016](prd/autopilot.md#prd-aut-016-provider-usage-run-telemetry); and
compatibility recovery is
[PRD-AUT-014](prd/autopilot.md#prd-aut-014-unknown-run-recovery-and-continuation).
Recognized Claude worker commands request
`--output-format stream-json --verbose`; a command that already supplies
`--session-id` keeps that id. If the provider is invoked with
`--no-session-persistence`, the best-effort transcript path may not exist.
Recognized Codex workers use `codex exec --json`.

### `vibe-loop runs summary`

Summarize a rolling provider-usage window:

```bash
vibe-loop runs summary --repo . --hours 24 --json
```

- `--repo PATH` selects the repository.
- `--hours NUMBER` sets a positive lookback window; the default is `24`.
- `--json` emits structured output.

The summary groups durable run provenance by project, provider, model, and
phase. It reports launch, completion, failure, restart, worker-time, token,
cache, cost, productivity, quota, and typed budget evidence without inferring
missing provider data or switching providers.

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

These commands are intended for active worker runs. Fencing tokens are internal
lock capabilities: do not place them in command arguments, logs, reports, or
diagnostics. The runtime supplies the capability through the worker environment
where required.

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
- `--metadata-json OBJECT` records structured metadata. Known workflows can use
  allowlisted values such as `phase` and `work_kind`; arbitrary metadata is not
  promoted into usage telemetry.

A report classifies a run; it does not mutate the task source or mark a task
done. Runtime-owned implementation workers report only that a candidate is
ready for runtime gates and review.

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

Normal supervised launches are already claimed by the runtime. This compatibility
command verifies and records ownership; it never creates, deletes, resets,
merges, or cleans branches or worktrees.

### `vibe-loop worker candidate`

Declare the committed candidate from the claimed workspace:

```bash
vibe-loop worker candidate --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --head HEAD
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the claimed run.
- `--head REF` is required and must match the clean tracked workspace.
- `--base-main REF` can supply the recorded base.
- `--changed-path PATH` can be repeated to declare changed paths.
- `--json` emits structured output.

The command validates the candidate against the active claim and records
identity only. Runtime-owned gates consume that declaration.

### `vibe-loop main-integration acquire`

Acquire the advisory integration lock:

```bash
vibe-loop main-integration acquire --repo "$VIBE_LOOP_REPO" \
  --run-id "$VIBE_LOOP_RUN_ID" --task-id "$VIBE_LOOP_TASK_ID" \
  --wait --timeout 300
```

- `--repo PATH`, `--run-id ID`, and `--task-id ID` identify the active run.
- `--pid PID` supplies the worker process id when it cannot be derived.
- `--wait` waits while the current holder is live or unknown.
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

Evaluation behavior and evidence interpretation are owned by the
[skill evaluation strategy](skill-evaluation-strategy.md). The commands below
are invocation entry points.

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

The command materializes fresh fixtures and writes `aggregate.json` and
`aggregate.md`, including the `skill_quality` comparison.

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
- `--minimum-trials N`
- repeatable `--parked-regression REGRESSION_ID=TASK_ID`
- repeatable `--parked-workflow-regression TASK_ID`
- repeatable `--external-benchmark-json PATH`
- `--json`

Without `--aggregate` or `--dry-run`, the command runs the bundled release
matrix. Unresolved workflow-contract regressions block readiness unless parked
under an explicit task id.

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

External benchmark evidence is optional context and does not replace the local
release gate.
