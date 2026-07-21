# Autopilot PRD

This PRD owns Level 2 contracts for persistent supervision above
`run-until-done`. Autopilot keeps a repository's existing `vibe-loop` workflow
alive, visible, and restartable without becoming the owner of task authoring,
worker implementation, branch/worktree management, review, merge, or cleanup.

## PRD-AUT-001 Reusable Status Core

Autopilot must be implemented as a reusable service/status core with thin CLI
rendering, so external views (such as the loopyard web UI) and multi-project
tooling can consume structured state without scraping terminal text.

Acceptance must cover a dedicated autopilot module, structured project status
objects, one-cycle result objects, bounded git/task/worker/lock/supervisor
summaries, text rendering separated from state collection, and no dependency on
live process memory for read-only status.

Supervisor liveness is derived by correlating the current supervisor lock with
the matching `autopilot_supervisor_started` or
`autopilot_supervisor_observed` journal records. Later PID-less cycle records
remain the latest cycle state and must not hide a still-live supervisor. A
released or stale supervisor lock must not be reported as live.

Autopilot must stay repository-agnostic. Its source, documentation, bundled eval
fixtures, and command output must not embed downstream project names or absolute
developer-machine paths, and a release check must assert this so the feature is
safe to ship and to surface in a future shared dashboard.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-05`.

## PRD-AUT-002a Cross-Run Attempt Circuit Breaker

Autopilot must not let fresh run IDs, supervisor recovery, or task-source
resets repeatedly spend implementation capacity on unchanged evidence. It
records an append-only attempt identity containing only revision digests and
safe base/candidate/routing labels. After the configurable threshold (default
three) of non-completed attempts with the same task revision, candidate/base,
relevant configuration, and blocker class, it withholds further launches until
that fingerprint changes or an explicit operator reset is journaled. Provider
or account walls use their existing backoff and never consume the attempt
budget. Status and `runs summary` expose breaker state, safe fingerprint
inputs, opening reason, reset provenance, and avoided-launch counts without
task prompts, commands, credentials, or fencing values.

## PRD-AUT-002 Command Surface

The CLI must expose autopilot through a subcommand group:
`vibe-loop autopilot run`, `vibe-loop autopilot start`,
`vibe-loop autopilot stop`, and `vibe-loop autopilot status`. The bare
`vibe-loop autopilot` command may remain as a shorthand for `run`, but command
semantics must be explicit in help and tests.

Acceptance must cover `--repo`, `--jobs`, `--interval`, `--once`,
`--max-cycles`, `--ask-agent`, `--continue-on-failure`, `--max-slices`,
`--max-tasks`, `--min-ready`, `--worktree-disposition`, and `--json` where
scriptable output is promised. Human cycle output should be compact by default:
repo, queue, supervisor state, blockers or actions, log path, and next wake.

Related implementation IDs: `AUTO-02`, `AUTO-03`.

## PRD-AUT-003 Append-Only Cycle Records

Autopilot state must be recorded through additive records in the target
repository's configured runtime journal, such as `.vibe-loop/runs.jsonl`, rather
than a separate hidden state file.

Records include `autopilot_cycle`, `autopilot_supervisor_started`,
`autopilot_supervisor_observed`, `autopilot_command_result`, and
`autopilot_idle_wait`. Each cycle record
must carry schema version, record type, occurrence time, cycle id, repo, queue
counts, worker and lock summaries, current and previous main refs when
available, actions, blockers, child pid/log path when relevant, and next wake.
Cycle and supervisor records also carry the configured worktree-disposition
policy. Worktree-disposition records carry that policy plus candidate counts,
evidence, reasons, and outcomes. Existing run readers must keep tolerating
unknown record types. Idle-wait records carry the originating cycle id, outer
deadline, wake reason, runnable count, task-source poll count, wake-adapter call
count, and bounded source/adapter error categories. This provenance must
distinguish a task change, deadline, operator message, and a failing source
without persisting operator-message content.

Related implementation IDs: `AUTO-01`, `AUTO-03`, `AUTO-04`.

## PRD-AUT-004 Child Supervisor

`autopilot run` must supervise `run-until-done` as a child process instead of
duplicating worker scheduling logic. It starts a child only when no live
autopilot-owned child is already active, preflight state permits launch, and
runnable work exists.

Acceptance must cover child command construction, log redirection under the
configured state directory, pid/log observation, no duplicate supervisor
launch, one-cycle mode, bounded cycle counts, foreground interval sleeping,
signal behavior, and classification of clean drain versus restartable exit
versus blocked state. Idle foreground waits use an adaptive full-list fallback
whose delays grow to a configured cap while preserving the outer interval
deadline; repeated task-source or wake-adapter failures must not spin. With the
default settings, a 30-minute empty interval performs substantially fewer than
30 task-source listings. Each logical fallback poll performs one task-source
listing, derives runnable work from that snapshot, and bounds a command-backed
listing by the remaining absolute monotonic deadline. Candidate filtering uses
the cycle's active-run/conflict snapshot so the fallback does not introduce an
independently timed lock-backend query; a lock-only change is observed through
the trusted wake adapter or at the outer deadline.

`autopilot run` remains the foreground supervisor contract. On POSIX systems,
`autopilot start` must provide the supported detached lifecycle by starting
that same supervisor in a new session with standard streams disconnected or
redirected. It must not report success until the process is live and owns the
matching autopilot lock. The launch result and append-only supervisor
observation must correlate the run ID, PID, process-group or session identity,
and real log path. Concurrent starts remain lock-fenced. Acceptance includes a
regression test in which the launcher caller exits while the supervisor remains
live. A supervisor lock with a configured lease must be heartbeated while the
foreground supervisor is running, including during child cycles longer than the
lease. Acceptance also includes documentation that this mechanism is not reboot
persistence and that platform service managers are required for restart
policies and non-POSIX hosts. Plain `nohup` is not a sufficient supported
lifecycle in harnesses that reap child jobs.

`autopilot stop` must request graceful termination only after correlating the
live lock with an append-only detached observation and a stable process
identity. The verified live path is Linux-only and must use the recorded run,
PID, process-group/session identity, kernel process-birth identity, and pidfd
signaling so PID reuse cannot redirect the signal. It must return success only
after both the exact process and supervisor lock are absent; timeout,
interruption, missing or foreign identity, and backend errors fail closed
without automatic `SIGKILL`. The foreground supervisor must translate
supported termination signals into normal unwinding, terminate any active
child process group, stop its heartbeat, and release its lock with the acquired
fencing token. Once signal cleanup begins, repeated supported signals must be
coalesced until bounded child and backend cleanup completes so a second signal
cannot interrupt fenced release.

Signalling the supervisor alone is not a stop. Its `run-until-done` child, that
child's workers, and any process those workers detached into their own group or
session all survive the supervisor and reparent to PID 1, where they keep
holding task locks and burning provider quota while the operator has been told
the run stopped. `autopilot stop` must therefore drain the exact recorded
process tree before it may report success.

The drain set is derived only from records this installation wrote: the
`autopilot_child_started` record the cycle appends before waiting on its child,
the `worker_process_started` record appended immediately after `Popen`, the
active-run locks in this repository's own lock root, and the `/proc` ancestry
rooted at those verified processes. A command backend may quarantine worker
group, session, and birth fields from the lock status projection. The local
record may restore only omitted fields after the task, run, PID, host, and
supervisor all match; projected conflicts are never overwritten. Command names,
process-group sweeps, and ambient process listings are never admissible, and
`killpg` is never used, so a peer installation's processes can never enter the
set.

A worker is an independently verifiable root, never one inferred from a live
child. A worker reparents to PID 1 precisely because its launching child died,
so deriving worker liveness from the child would skip exactly the processes that
outlive a stop. The worker's own recorded birth identity proves which process it
is, and its active-run lock proves the run owns it; the child records supply
only attribution. Attribution spans every child a supervisor run recorded, not
just its current one: a supervisor runs many cycles, and a worker orphaned by an
earlier cycle is still this run's, so comparing it against only the latest child
would block the stop on exactly the orphan it exists to drain.

A recorded worker whose live birth identity no longer matches is unverifiable,
even when the recycled PID still appears below a verified child in the ancestry
snapshot. The mismatch blocks the stop with zero signals rather than admitting
the PID again as an ordinary descendant. A live worker this run cannot attribute
to any recorded child, or whose birth identity was never recorded, fails the
same way — including for a supervisor that predates these records.

Every candidate requires a kernel process-birth identity, an open pidfd, and a
post-open recheck of birth, parent, group, and session. Any missing or changed
identity blocks the stop with zero signals sent, rather than leaving a
half-signalled tree. After every identity passes, the verified supervisor pidfd
is stopped so a completed child cannot trigger another cycle while shutdown is
in progress. Submission of `SIGSTOP` is not sufficient: the stop path polls the
exact supervisor's birth identity and kernel process state until it is observed
stopped or the original deadline expires. It then rereads synchronous
child-start records and
active task locks, draining anything created between the first snapshot and
supervisor quiescence. This rescan also runs after an initially empty snapshot.
Termination goes to exact pidfds, deepest descendants first, then worker roots,
then the child, and only then the supervisor; the supervisor is resumed to handle
its pending termination normally. The pidfds are retained across the whole wait,
so a process that reparents mid-drain is still observed to exit; a PID-based
recheck cannot do this. Enumeration, task-lock status and release, process exit,
and singleton-lock state must all settle within the caller's one bounded
deadline, with no post-drain grace or fresh backend timeout.

Timeout, signal refusal, or interruption returns `stopped=false` with the exact
remaining role, run, task, and PID, writes no terminal record, and releases no
task lock, so the operator can verify and retry against named processes rather
than searching for orphans. On success, a drained worker that filed no terminal
report receives a terminal non-success `run_result` and lifecycle transition,
both attributed to the stop; success is never synthesized. An earlier
same-run `unknown` result is non-terminal and is followed by the explicit
terminated result rather than suppressing it. A task lock is
released only when its run and task identity still
match and its fencing generation equals the one this installation recorded
acquiring, read from a local record rather than from the backend status being
released; a lock re-created out of band therefore fails closed instead of
agreeing with itself. The authoritative task stays active and the committed
worktree is preserved so the slice can be picked up again. Incomplete ownership
or any reconciliation failure retains the affected lock, returns
`stopped=false`, and does not append the operator stop record.

Diagnostics report whether a birth identity is known, never its value, since it
embeds the host boot identity. This applies to worker and status output as well
as to stop results; the raw value lives only in local identity records and lock
metadata retained by backends, where identity verification needs it.

Detached-start readiness must be proven by a local trusted contract, not by
lock-wire metadata. The supervisor appends its own started record only after
installing termination handlers, and the launcher verifies that record before
declaring the detached supervisor live. Backends are entitled to quarantine
unknown wire fields, so a lock-metadata readiness flag is not an admissible
signal.

An already-absent process with a remaining lock requires the separate
`autopilot stop --recover-stale --run-id <exact-run>` path. Recovery requires
the exact recorded run and the fencing generation this installation last
successfully acquired, read from a local record rather than from the backend
status being recovered; comparing a backend token against itself would fence
nothing. Only a granted acquire may advance that record: a refused acquire
against the very lock being recovered must not fence the operator out of it. A
generation this installation never issued, a live owner, or a run mismatch all
fail closed. Recovery releases through the configured directory or command
backend and verifies absence afterward. Fencing tokens must not be accepted in
argv or rendered in diagnostics.

Because both release and recovery need that local witness, an acquire whose
witness write fails gives the granted lock straight back, and the ordinary case
leaves the singleton immediately reacquirable. That give-back is best effort,
not an invariant: when it also fails, no local record witnesses the granted
generation and the resulting holder state is unknown, because a backend can
remove the lock and still fail to report that cleanly. If the lock does survive,
neither release nor recovery can resolve it. That outcome must be reported as a
single failure naming both the witness write and the compensating give-back,
with each role distinguishable and no token value rendered, rather than silently
reduced to either one, so the operator knows to determine the holder state out
of band. Directory owner and fencing mismatches raised during the give-back are
compensation failures of that same kind, not replacements for the original
witness failure.

Successful ordinary, signal, and recovery exits append a terminal supervisor
record only after lock release. Status reports `stopped` only when such a
terminal record exists AND the recorded process is verifiably absent. A stop
record with a live process, a live process without the singleton lock, a
vanished process with no terminal record, or any supervisor record carrying no
PID at all must each report an `inconsistent` supervisor state with a specific
blocker, so an unresolved supervisor is never presented as a clean stop or
masked by an older cycle status. A record without a PID is inconsistent by
construction: absence cannot be verified against an identity that was never
recorded. Recovery therefore never writes a terminal record without a PID. A
command-backed lock is entitled to record no PID; recovery must then derive the
exact PID from the matching local `autopilot_supervisor_started` record for the
requested run, verify that exact process absent, release with the exact run and
locally minted generation, and prove lock absence so the singleton is
immediately reacquirable. Only when no PID exists in the lock or the local
records does recovery refuse outright.

Related implementation IDs: `AUTO-03`.

## PRD-AUT-005 Configured Maintenance Hooks

Autopilot may run optional project-configured health, summary, troubleshoot, or
planning commands, but only when those commands are explicitly user-authored in
`.vibe-loop.toml`.

Acceptance must cover an `[autopilot]` config section, bounded command output,
safe environment variables, command-result records, command redaction in status
JSON, low-ready queue handling, and the rule that generated task-source
profiles cannot introduce maintenance commands. An explicitly configured
`planning_command` takes precedence over native planning.

Related implementation IDs: `AUTO-04`.

## PRD-AUT-006 Non-Destructive Recovery Boundary

Autopilot recovery must be diagnostic and conservative by default. It must not
reset branches, steal live locks, kill arbitrary processes, merge, push, rebase,
edit tracked project files, or mutate task sources. It must not delete worktrees
or branches except under the bounded, evidence-gated worktree-disposition
exception defined below. Configured maintenance commands are external
user-authored checks or planners; their presence does not authorize autopilot to
perform destructive recovery.

Two bounded exceptions are permitted, and no others:

1. **Stale-lock release.** Autopilot may automatically release stale worker task
   locks whose recorded worker process is missing, using the same validated
   release path and `lock_expired` audit records as `workers clean --force`. This
   recovers aborted workers without deleting worktrees or branches. It must not
   release a lock that has not yet observed a worker PID (so a just-started
   worker cannot lose its task lock before its launcher writes PID metadata), and
   it must not take over locks held by live processes.

2. **Worktree disposition.** The default policy is `report-only`; merely starting
   autopilot authorizes evidence collection and journaling, not removal. Autopilot
   may remove a worker-created worktree and delete its branch (`git worktree
   remove` plus `git branch -d`) only after an operator explicitly selects the
   bounded `reap` policy and under the evidence-gated, agent-decided contract in
   `PRD-AUT-010`. It must never force-remove a dirty or unmerged worktree, never
   touch a worktree claimed by a live run, act only on a per-worktree reap
   decision the read-only analysis agent returned with a reason (no blanket
   reap), and journal the policy, evidence, decision, and action to the
   append-only run store. Salvageable unmerged or dirty work-in-progress must be
   kept, not reaped.

Acceptance must cover unsafe workspace diagnostics, dirty repo state, missing
task source, unavailable agent command, no runnable work, and child launch
failure as explicit blockers or observations rather than destructive cleanup
triggers; stale locks with a still-live or PID-unobserved owner remain blocking,
while stale locks with a missing worker process are recovered and audited; and
default worktree disposition reports eligible candidates without deletion, and
explicit `reap` disposition removes only clean remnants with unambiguous
released ownership, a matching completed worker report, and containment in
both local and remote `main`, under the `PRD-AUT-010` guardrails while keeping
all salvageable work-in-progress.

Unknown-run recovery (`PRD-AUT-014`) does not breach this boundary: it launches
a new continuation worker against the existing claimed branch/worktree and never
deletes, resets, steals, merges, or mutates another worker's committed work.

Related implementation IDs: `AUTO-01`, `AUTO-03`, `AUTO-04`, `AUTO-13`,
`AUTO-14`, `AUTO-21`.

## PRD-AUT-007 Multi-Project Shape

The first implementation may operate on one repository at a time, but the data
model must not assume a single project forever. Status payloads should represent
project identity and be composable into a list for future project registries and
dashboards.

Acceptance must cover stable fields for repo, display name, state directory,
current main ref, dirty state, queue counts, active workers, stale locks,
workspace diagnostics, supervisor, blockers, last cycle, and next wake. Per-repo
status also exposes the configured worktree-disposition policy so a multi-project
operator can distinguish report-only inspection from an explicit reaping opt-in.
Per-repo state stays under that repo's configured state directory; no global
registry is required for the first implementation.

Registry entries may carry an optional bounded `context` object for non-secret
runtime selectors required by command-backed task-source and lock adapters, for
example `LOOPYARD_PROJECT`. The context is copied into each adapter subprocess
environment without mutating the supervisor environment or interpolating values
into shell commands. It does not apply to worker-agent or maintenance commands.
Entries are limited to 16 variables, 4 KiB per value, and 16 KiB total. Secret-
like names and values, process-loader variables, shell startup controls, command
lookup paths, credential/config selectors, and `VIBE_LOOP_*` protocol variables
are rejected. Names must be selector-shaped, using a suffix such as `_PROJECT`,
`_BOARD`, `_TENANT`, `_WORKSPACE`, `_NAMESPACE`, `_REPO`, `_TEAM`, or
`_SELECTOR`; arbitrary environment controls are not accepted. Context values are
persisted only in the registry and recursively redacted from project list,
inspect, and aggregate status output, including adapter-derived payloads and
diagnostics.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-06`.

## PRD-AUT-020 Command Backend Project Binding

A command-backed task source or lock adapter that routes by an ambient
environment selector binds repository A to project B whenever that selector is
absent or wrong in the launching shell. Ambient export guidance is not an
enforceable contract: it fails silently, and the resulting cross-project
supervisor lock and queue analysis look valid.

A repository must therefore be able to declare its namespace binding explicitly
in configuration. A `[project_binding]` table names the required selector
variables in `require` and may pin their values in `context`. A required
selector resolves from exactly two explicit sources: the per-project registry
`context` for that entry, or the repository's own pinned `context`. A value
present only in the ambient process environment does not resolve it. Explicit
selector values must contain at least one non-whitespace character.

Resolution must fail closed before any observable effect. Supervisor run,
start, stop, and stale-recovery operations; task selection; worker inspection
and cleanup; integration locking; and fenced reporting refuse a missing,
ambient-only, or conflicting binding before invoking a command adapter.
Diagnostics name the variable and the failure reason (`unset`, `ambient_only`,
`conflict`) and never echo the ambient or configured value.

Structured status reports the resolved binding so operators can verify routing
without reading configuration: each required selector appears with its resolved
value and the source that supplied it, alongside every context name injected
into adapter subprocesses. Required names are validated as namespace selectors,
which is what makes their values reportable; any other declared context name is
redacted. This block is exempt from registry-context value redaction in
aggregate and per-project status output, because redacting it would erase the
routing fact in exactly the registry-driven multi-project case that needs it.

Status must not itself cross projects. When a declared binding is unresolved,
status reports the diagnostics as blockers and a queue `source_error` without
invoking the task source or lock adapter, so an unbound repository never
displays another project's queue or locks under its own path. Unresolved
bindings appear as project blockers in the same payload.

Selector names are compared verbatim, matching environment-variable semantics:
two names differing only in case are two distinct selectors, and each must be
supplied on its own.

Binding must survive process launch. Detached start and registry-driven launches
transport registry context to the child out of band and drop the ambient copy of
every bound name from the child environment, so a stale shell export cannot
reach an adapter if the child's own resolution changes.

The table is optional. A command backend that already scopes its project
explicitly in the command string has no ambiguity to close and keeps working
unchanged with no `[project_binding]` declared.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-06`.

## PRD-AUT-008 TUI And WebUI Readiness

> **Superseded (removed from vibe-loop).** The in-tree `autopilot tui` (Textual)
> and `autopilot webui` surfaces were removed; interactive dashboards now live in
> the [loopyard](https://github.com/ei-grad/loopyard) web UI. The still-active
> requirement here is the machine-readable `autopilot status --json` boundary that
> loopyard consumes; the TUI/WebUI acceptance items below are historical.

TUI and WebUI implementations are future work, but autopilot must expose a
machine-readable status boundary that makes them straightforward follow-ups.

Acceptance must cover `autopilot status --json`, path-addressable logs,
append-only durable records as the source of truth, no raw command or secret
leakage in UI-ready payloads, no text scraping requirement, and no TUI/WebUI
runtime dependencies in the first autopilot slice.

Related implementation IDs: `AUTO-01`, `AUTO-02`, `AUTO-07`, `AUTO-08`.

## PRD-AUT-009 Read-Only Analysis Agent

Autopilot must be able to run a *read-only* analysis agent to make judgement
calls that the supervisor cannot decide mechanically, while keeping product-code
authorship limited to the existing read-write worker agent. The analysis agent
inspects evidence and returns a structured decision; it must never author or
mutate tracked project files.

The architectural contract is: the agent decides, `vibe-loop` executes
deterministically within safety guardrails, and every action is recorded to the
append-only journal so the system stays monitorable and recoverable. Analysis
and decision steps run a new read-only agent invocation distinct from the
read-write worker.

Acceptance must cover a per-agent-kind `analysis_command` default that is
read-only by construction (Claude disallows `Edit`/`Write`/`NotebookEdit` while
retaining `Read`/`Grep`/`Glob` so it can inspect work-in-progress to judge
salvageability; Codex uses a read-only sandbox), an `AgentConfig.analysis_command`
field with a `require_analysis_command()` accessor, parsing of the new config key,
reuse of the generic key-based command resolution, inclusion of `analysis_command`
in the generated-task-profile forbidden keys, and a runner entry point that
launches the analysis agent and parses its strict structured (JSON) decision
using the same prompt-delivery and shell-preparation validation as the existing
selection path. The analysis agent must not be granted write/edit tools and must
not be required for routine read-only status commands. For native low-ready
planning, it receives bounded queue and worker evidence and returns only a
strict `should_plan`, reason, and objective decision. It never authors task
content; a separate invocation of the configured read-write worker command
performs any requested queue replenishment.

Related implementation IDs: `AUTO-12`, `AUTO-18`.

### Account Limit Walls At The Agent Subprocess Boundary

An agent refusal caused by an account usage/session/weekly limit is a *known
duration* wall, not a short transport transient. Its text also matches the
transient patterns (it mentions "limit"/"quota"), so without separate
classification the retry layer spends its whole jittered budget against a wall
that cannot clear for hours or days.

The subprocess boundary must therefore classify a limit wall before transient
classification and return immediately without consuming retry attempts, while
ordinary 429, 5xx, capacity, network, and overload failures keep their bounded
transient retries. Detection reads both stdout and stderr, because agent CLIs
disagree about which stream carries the refusal, and only applies to a nonzero
exit so a successful run that merely quotes a limit phrase stays on the normal
path.

Detection is opt-in per call site. It is a behaviour change to the retry
contract, so paths that never asked for it — task selection, generated-profile
refresh — keep their existing retry semantics rather than inheriting wall
classification from a default.

The no-retry short circuit requires evidence that the wall is genuinely
long-lived: either a parseable reset time, or the absence of any independent
transient marker. Provider rate-limit bodies routinely carry a wall phrase while
advertising recovery in seconds ("usage limit exceeded, retry after 30s"); those
keep their bounded retries, because trading three short retries for a
half-hour pause the provider never asked for is strictly worse. That
recoverability check reads the same combined stdout+stderr the wall scan reads;
a throttling body arriving only on stdout is no less recoverable. Once those
retries are spent and the failure persists, the named wall is the best remaining
explanation, so it is surfaced exactly once and the caller applies its
configured backoff rather than redispatching into the same refusal.

Advertised resets may be wall-clock ("resets 1am (UTC)") or an absolute calendar
instant ("try again at Jul 25th, 2026 3:24 AM"). Calendar forms are parsed in
preference to clock-only forms, since the two share their digits and a clock
reading of a multi-day wall understates the wait. The two forms carry different
misparse bounds: a bare clock cannot meaningfully mean more than a day, while a
full date is self-validating and a multi-day account limit is a legitimate
reading that must not be truncated into an untruthful wake. A year-less calendar
date that reads as past rolls forward a year only when the rolled instant lands
inside that same near-future bound, which distinguishes a genuine year-boundary
crossing ("Jan 2nd" seen on Dec 31) from a reset that is merely hours old. An
already-elapsed reset reports no usable reset at all rather than a zero wait,
so the caller falls back to its configured backoff instead of spinning.

When native planning hits a wall, the decision is journaled with its own
`limit_wall` status and pause rather than the generic `analysis_error` status,
the cycle records a `native_planning_limit_wall:<seconds>s` action, and the
supervisor pauses dispatch until the reset. The journaled `next_wake` must be
the reset the supervisor actually sleeps to, not the interval stamped before the
cycle ran, and status output must name the wall so a paused cycle stays
distinguishable from a planning failure that shares the same `idle` status. A
non-positive pause is not a pause: a configured zero backoff leaves the cycle on
its ordinary interval, and the reported wake must describe that interval rather
than the wall. A
stop request must interrupt the pause: signal-driven stops unwind through the
sleeper, and a cooperative stop is polled between bounded slices.

## PRD-AUT-009a Planning Outcome Budget Backoff

Every native planning launch spends provider budget on a read-only analysis pass
and, when it decides to plan, an authoring worker. Repeating that launch on the
ordinary supervisor interval while it produces nothing is the dominant failure
mode: an observed 48-hour window spent $28.94 across twelve launches, ending in
an `invalid_plan` decision that created no tasks.

Each launch must therefore be classified into exactly one outcome:

| Outcome | Meaning | Charged to the streak |
| --- | --- | --- |
| `productive` | the launch created at least one task | resets it |
| `invalid_plan` | the analysis agent returned an unusable decision | yes |
| `no_tasks` | the analysis agent decided no plan was needed | yes |
| `zero_created` | the authoring worker finished without creating a task | yes |
| `limit_wall` | a provider wall stopped the launch | no |
| `analysis_error` | the analysis stage failed on infrastructure | no |
| `worker_error` | the authoring worker never started or failed | no |
| `task_source_error` | the task source could not be read after planning | no |

The four inconclusive outcomes say nothing about whether planning *can* produce
work, so they neither extend nor reset the streak; classifying them as
unproductive would let provider walls consume the planning budget, and
classifying them as productive would reopen the gate on no evidence.

`invalid_plan` is reserved for a genuine plan/schema fault. Failing to resolve
the agent executable, an `OSError`, a subprocess failure, and an unreadable task
source are infrastructure faults with distinct causes and no bearing on planning
quality; folding them into `invalid_plan` would back off planning for six hours
because of a misconfigured path. They are recorded as `analysis_error`,
`worker_error`, or `task_source_error` accordingly.

Productivity is decided from authoritative created-task evidence - identities
new to the complete normalized task-source listing across the launch - not from
the runnable-count delta. A task claimed before the post-worker listing remains
present in that complete source and therefore remains credited to the planning
launch. Concurrent claims and completions move the runnable count independently
of planning, so the delta can call a productive launch unproductive or credit
planning for work it never authored when a completion unblocks dependents.

Two independent gates then withhold planning, and the later deadline governs:

- `unproductive_outcomes` - once `planning_unproductive_threshold` consecutive
  unproductive launches are recorded (default 2), planning is withheld for
  `planning_backoff_seconds` (default six hours) measured from the last one. A
  fingerprint change starts a new evidence epoch, so earlier outcomes from the
  previous source state do not count toward the new state's threshold.
- `daily_launch_cap` - at most `planning_max_launches_per_day` launches (default
  4) in a rolling 24 hours, expiring only as the oldest counted launch ages out.

A withheld cycle must not invoke the analysis agent at all; attempting the launch
and discarding it would spend exactly the budget the gate exists to protect.

The daily cap counts a durable pre-launch record appended before the analysis
agent runs, not only terminal outcome records. A launch that is interrupted or
crashes after the analysis or worker started but before it can classify itself
has already spent budget, and must still consume one of the day's launches;
counting terminal records alone would let a crash loop plan without limit. The
only launch exempted is one whose outcome proves no provider was reached, such
as an unresolvable agent executable. Cap accounting scans the complete planning
journal for the rolling window; intervening exempt outcomes cannot evict charged
launches from consideration.

The outcome gate is evidence-based and releases when the evidence changes: a
launch is compared against a fingerprint of the task source it acted on, and a
materially changed board (or a productive launch) clears the streak. That
fingerprint is built from the complete normalized task-source identity and
content, excluding lifecycle status. A same-cardinality task replacement or
content/source edit is therefore detected even though every count is unchanged,
while `active`/`done` transitions and counter churn from unrelated workers are
not mistaken for fresh planning evidence. The daily cap is a spend ceiling, not
an evidence gate, so a changed fingerprint does not lift it.

During an outcome backoff, the idle waiter compares each complete task-source
snapshot with the pre-wait fingerprint in addition to checking `min_ready`. A
same-cardinality replacement or content/source edit therefore wakes the next
cycle even when runnable depth remains below the dispatch threshold. Status-only
and counter-only churn retains the same fingerprint and does not wake early.

The backoff extends the idle wait budget rather than adding a blocking sleep, so
it can never shorten an operator's configured interval, only lengthen it, and so
the idle waiter keeps its existing guarantees throughout: a task source that
reaches `min_ready` still wakes the next cycle early and dispatches at most one
implementer, stop requests are still honoured per slice, exponential poll backoff
still prevents a busy loop, and the journaled `next_wake` is the deadline the
supervisor actually honours. Status output must name the recorded outcome, the
attempt count, and the backoff reason, using outcome names and counts only - no
prompt text, objectives, or credentials.

## PRD-AUT-010 Native Worktree Disposition Health Step

Autopilot cycles must include a native worktree-disposition health step so that
orphaned worktrees are visible without per-project configuration. The safe
default is `report-only`: it identifies and journals otherwise reapable
worktrees without invoking the analysis agent or mutating git. Automatic reaping
requires an explicit, bounded operator opt-in through configuration or the CLI;
starting autopilot alone is not approval.

Under the explicit `reap` policy, the step follows the agent-decides /
code-executes / guardrails contract:
`vibe-loop` gathers per-worktree evidence mechanically (path, branch,
merged-into-main predicate, dirty state, the claiming run and whether its process
is alive); passes that evidence to the read-only analysis agent
(`PRD-AUT-009`); receives a per-worktree keep-or-reap decision *with a reason*;
and executes `git worktree remove` plus `git branch -d` only within safety
guardrails. There must be no blanket reap: salvageable unmerged or dirty
work-in-progress must be kept.

Acceptance must cover mechanical evidence gathering that reuses existing
workers.py helpers (worktree enumeration, merged-branch predicate, dirty-state
inspection, worker-view claim and liveness); report-only behavior that records
eligible candidates and performs no deletion; validation that rejects unknown
policy modes; an explicit-reap agent decision per worktree with a reason; and
code-side execution that never force-removes dirty or unmerged worktrees, never
removes a worktree claimed by a live run, and records the configured policy,
candidate, reasons, and result in the append-only journal. Side effects (git
removal, branch deletion) must be dependency-injected so tests do not run real
git. The step runs unconditionally, but starting autopilot must not authorize
the bounded destructive exception.

Related implementation IDs: `AUTO-13`, `AUTO-14`.

## PRD-AUT-011 Full Cycle Action Logging

Every autopilot cycle action, including native worktree disposition, must be
recorded to the append-only journal so the loop is monitorable and recoverable.
Each native maintenance behavior added to the cycle must register a typed record
in the run store's known record-type set so existing readers do not silently drop
it, and must append a concise action tag to the cycle's `actions` list while
emitting any detailed payload as a separate dedicated record.

Acceptance must cover a new `autopilot_worktree_reap` record type that mirrors
the existing maintenance-command-result record shape (schema version, record
type, occurrence time, repo, cycle id, configured policy, candidates, evidence,
reasoned outcomes, and reaped/kept/errors/status payload),
registration of every new native record type in the run store's autopilot and
known record-type sets, tolerance of unknown record types on read, and a cycle
action tag for the policy, candidate count, and reap count appended for the
disposition step.

Related implementation IDs: `AUTO-13`, `AUTO-14`, `AUTO-15`, `AUTO-16`,
`AUTO-17`.

## PRD-AUT-012 Configuration-Free Generic Cycle

`vibe-loop autopilot run` must behave like the generic `autopilot` skill cycle
without per-project configuration. Today the cycle is a supervisor plus
stale-lock cleaner plus four empty maintenance-command slots (health, summary,
troubleshoot, planning) that are all unset by default, so with no project config
the loop never inspects orphaned worktrees, checks disk, summarizes what landed,
detects recurring trouble, or plans new work.

The native generic cycle must provide repository-agnostic defaults for these
behaviors while preserving the non-destructive recovery boundary
(`PRD-AUT-006`): native report-only worktree disposition with an explicit reaping
opt-in (`PRD-AUT-010`), native disk-health
checks, a native "what landed" git-log summary that derives the commit span
from the previous cycle's recorded `main` ref (read from the prior
`autopilot_cycle` record, since status carries only the current ref) to the
current `main` ref and journals a bounded, read-only `autopilot_cycle_summary`
record, native troubleshoot detection
derived from `runs.jsonl`, and native planning that invokes the configured agent
rather than requiring a separate `planning_command` script. Native planning is
split into two stages: the read-only analysis runner decides whether and what to
plan from bounded runtime evidence plus repository-local planning sources; only
a separate read-write worker launched through `agent.command` may author task
content. The supervisor validates and journals the decision, launches the
worker when requested, records its started and terminal lifecycle (including
PID, log, configured worker timeout, and before/after runnable depth), and
re-reads rather than mutating the task source. Post-worker task-source failures
remain explicit instead of becoming a zero count. Malformed decisions fail
closed without launching a write-capable worker. The two stages use the
registered `autopilot_planning_decision` and `autopilot_planning_worker` record
types.
Project-authored `[autopilot]` maintenance commands (`PRD-AUT-005`) continue to
override or augment the native behaviors; native behavior is the default, not a
replacement for explicit configuration.

The native disk-health floors are configuration-free by default but
project-tunable. A repository may raise or lower any of the four floors
(absolute free bytes, proportional free fraction, absolute free inodes,
proportional free-inode fraction) through an `[autopilot.disk_reserve]` table so
a heavy repository can demand a larger reserve without changing the global
default, which would create false positives for small or light repositories.
An unset override keeps the reviewed default, so a configuration-free project's
behavior is unchanged. Configuration validation rejects invalid, negative,
non-finite, and out-of-range values, and rejects a positive reserve paired with
an explicit zero reserve on the same axis as contradictory because the paired
floors can then never both be exhausted. The effective thresholds are journaled
in every `autopilot_disk_health` record and surfaced in project status.

Acceptance must cover each native behavior landing as an independently
reviewable slice, every native action appearing in the append-only journal
(`PRD-AUT-011`), and no native behavior performing destructive recovery outside
its declared evidence-gated guardrails. Configurable disk reserves must preserve
the reviewed AUTO-15 defaults when unset, block an injected 3.4 GiB/242 GiB
sample under an 8 GiB reserve, fail validation on invalid or contradictory
values, and expose the effective thresholds in cycle records and status without
introducing any cleanup or repository mutation.

Related implementation IDs: `AUTO-12`, `AUTO-14`, `AUTO-15`, `AUTO-16`,
`AUTO-17`, `AUTO-18`, `AUTO-19`, `autopilot-configurable-disk-reserve`.

## PRD-AUT-013 Observed Agent Session Id And Transcript Linkage

A run record must let an operator find the agent's real session transcript.
Today the worker is launched with `claude -p {prompt}` in default text mode,
which never emits its session id, so `runs.jsonl` records
`session_id_source: fallback:run_id` and only the wrapper-log path. The real
agent session transcript (for Claude,
`~/.claude/projects/.../<uuid>.jsonl`) is referenced nowhere, so what the agent
actually did cannot be recovered from a run record.

The worker invocation must surface the agent's real session id when the agent
can provide one, and the run records must persist both that id and the resolved
transcript path. For Claude the supervisor injects a known `--session-id <uuid>`
into the worker command (the alternative to `claude -p --output-format json`,
which would change stdout from streamed text to a JSON envelope and force the
selection/analysis text-scraping paths to adapt). Injection leaves stdout
unchanged, so streaming/progress output and the read-only selection/analysis
paths are untouched; the transcript path is resolved by globbing
`$CLAUDE_HOME/projects/*/<uuid>.jsonl` after the run, independent of Claude's
cwd encoding, and is skipped when the command already pins `--session-id`. For
Codex the CLI was inspected: `codex exec` has no flag to force or print a
session id without `--json` (which would replace the streamed human-readable
output the wrapper log and selection/analysis parsing rely on), so Codex worker
runs retain `fallback:run_id` and this limitation is documented in the README
rather than silently faked.

Acceptance must cover: a real `session_id` recorded with
`session_id_source: observed` (distinct from `fallback:run_id`) whenever the
agent surfaces one; the resolved transcript file path recorded on the
`run_started` and `run_result`/run context records; `fallback:run_id` retained
only when the agent genuinely surfaces no session id; preserved
streaming/progress behavior (the chosen `--session-id` injection leaves stdout
unchanged); and a documented path from a run record to the agent's transcript
file. This contract is independent of recovery and may land on its own.

Related implementation IDs: `AUTO-20`.

## PRD-AUT-014 Unknown-Run Recovery And Continuation

When a worker run ends without a clear terminal report — classified `unknown`,
or committed on its claimed branch but neither merged nor reported — the work is
orphaned: the supervisor stops or re-attempts from scratch and the
committed-but-unmerged-unreported work is left behind. The observed failure mode
is a worker that did real work, committed to its branch, then *parked* on a
billable external/authorization gate ("I'll continue when the monitor fires")
and exited; because `claude -p` is one-shot, the process exit left the run
`unknown`, never merged and never reported.

`run-until-done` must deterministically launch a bounded continuation worker for
such runs. The supervisor's deterministic control flow decides *when* to
recover; the agent does the *work*. Recovery must launch a continuation
read-write worker agent (the existing worker command path, not the read-only
analysis agent of `PRD-AUT-009`) with a recovery prompt that conveys the task
id, the prior `run_id`, the claimed branch and worktree (from the
`workspace_claim` record), the prior agent transcript path (from
`PRD-AUT-013`) and the wrapper log, and the instruction: the previous session
ended `unknown`; investigate what went wrong, finish the work and/or emit a
proper status (`completed`/`blocked`/`failed`); if blocked on an
external/authorization gate, report `blocked` with the precise reason — do not
park.

Recovery must be bounded: cap recovery attempts per task by reusing or extending
the existing `task_restart` counter/record so an `unknown → recover → unknown`
cycle cannot loop forever; after the cap, leave a clear `blocked`/`failed`
terminal record. Every recovery launch and outcome must be journaled
append-only.

This recovery stays inside the `PRD-AUT-006` non-destructive boundary: it
launches a *new* worker against the existing claimed branch/worktree and never
deletes, resets, steals, merges, or otherwise mutates another worker's
committed work; it neither reclassifies live work nor force-removes workspaces.
It depends on `PRD-AUT-013` because the recovery prompt must point the
continuation worker at the prior agent transcript.

Acceptance must cover: deterministic detection of `unknown` (and
committed-but-unmerged-unreported) runs in `run-until-done`; launch of a
read-write continuation worker with the recovery prompt context above; a
per-task recovery attempt cap built on the `task_restart` counter; a clear
terminal `blocked`/`failed` record once the cap is reached; append-only
journaling of each recovery launch and outcome; and no destructive action on
the claimed workspace.

Related implementation IDs: `AUTO-21`.

## PRD-AUT-015 Direct User Message Wake

`wait-helper` may poll a trusted external message adapter in addition to its
default process and wall-clock signals. The integration is explicit through
`--message-command`; vibe-loop does not import, detect, or infer a task backend.
The recipient is `--session-ref`, falling back only to `VIBE_LOOP_RUN_ID`, and
is passed to the adapter as `VIBE_LOOP_WAIT_SESSION_REF` without shell
interpolation.

The adapter prints one JSON document with `received` and `message`. A received
message must include a stable `id` and non-blank `content`; optional sender and
timestamp fields are preserved as structured data. A message wakes regardless
of PID `--mode`. Already-satisfied PID/deadline conditions retain precedence
and do not invoke the adapter. Nonzero exits, timeouts, or invalid JSON/schema
produce a safe `adapter_error` result and never silently degrade to clock-only
waiting or echo adapter stdout/stderr.

Acceptance must cover immediate and delayed messages, PID precedence, session
resolution, literal environment delivery, schema validation, bounded command
execution, safe errors, and unchanged behavior when no adapter is configured.

Related implementation IDs: `AUTO-22`.

Autopilot may also use an explicit trusted `[autopilot] idle_wake_command`
between adaptive task-source fallback listings. The command receives the
current wait budget, cycle id, and outer deadline through literal environment
values rather than shell interpolation. Validated registry runtime selectors
use the same literal adapter-environment boundary. The command returns
`{"woke":false}` or a validated `task_change`/`operator_message` wake reason
with optional bounded event metadata. Each invocation is time-bounded; invalid,
failed, or timed-out commands are journaled by safe category and fall back to
the same adaptive wait budget. Adapter output, permitted event fields, and total
journaled event size are byte-bounded. Message content and adapter stdout/stderr
are not journaled. Generated task-source profiles cannot introduce this command.

Autopilot acceptance must cover prompt change/message wakes, literal environment
delivery, schema validation, deadline preservation, adaptive fallback polling,
bounded error provenance, and unchanged clock/task-source behavior when no wake
adapter is configured.

## PRD-AUT-016 Provider Usage Run Telemetry

Worker and native-planning results record provider-reported usage without
estimating tokens from output or transcript size. Recognized Codex and Claude
commands use their structured result streams; a persisted Claude transcript is
a numeric-only fallback. Common input/output/cache/total token, duration, turn,
and reported-cost values live in versioned `stats`, alongside recognized raw
provider numeric fields and an explicit source/version. Missing and malformed
usage have a typed reason.

Codex quota windows are emitted in the local rollout `token_count` events, not
the ordinary `codex exec --json` stdout stream. Once stdout supplies the native
session id, collection resolves that rollout locally, prefers cumulative
`total_token_usage` over last-call usage, and retains a bounded history of
quota observations. Neither the rollout path nor raw rollout records enter
telemetry.

Telemetry records reject prompts, credentials, fencing values, command
payloads, and transcript content at persistence. Normalized numeric fields stay
compatible with Loopyard's existing `runs sync` ingestion. Rolling summaries
group durable provenance by project, provider, model, and phase and report
launch/productivity ratios plus typed budget diagnostics; diagnostics never
switch providers.

Provider group labels are limited to `openai`, `anthropic`, and `unknown`.
Model labels must be bounded, versioned native Codex/OpenAI (`gpt-*` or `oN-*`)
or Claude (`claude-*`) families with supported suffixes. Selector aliases,
key-name values, command fragments, paths, whitespace/control-bearing strings,
and arbitrary JSON values normalize to `unknown`. Runtime ingestion emits a
bounded attribution diagnostic containing only the rejected dimension. Summary
projection applies the same rules to legacy records without rewriting
append-only history and retains their numeric usage under the safe group.

Rolling summaries preserve those raw groups and add a separate provider quota
and account-wall view. Provider dimensions remain distinct: fresh input, cache
read, cache creation, output/reasoning output, reported cost, launches,
attempts, productive completions, and worker-minutes are not converted to a
universal token price. Gross and fresh-input usage per landed task are reported
separately.

Native quota evidence is limited to a bounded scope, window label, used
percentage, window duration, reset time, and observation time. Plan, credit,
account, command, prompt, credential, fencing, and transcript fields are not
persisted. Evidence availability is explicit. Forecasting requires two
increasing observations for the same provider, scope, window duration, and
reset timestamp; providers and reset windows are never combined. Missing or
malformed evidence does not produce an inferred quota from transcript bytes or
token totals.

Worker usage defaults to implementation. An allowlisted `phase` and optional
`review` or `discovery` `work_kind` from the terminal worker report can refine
that provenance. Native planning records model/provider provenance from the
formatted analysis and authoring commands, falls back to the configured model,
and measures the full planning wall time. Retry ordinals are normalized to
restart events before aggregation.

Quota activity distinguishes implementation, review, resumed review, planning,
validation, remediation, integration, failed attempts, and restarted attempts.
Repeated unchanged-candidate review in new sessions and repeated failed
attempts are avoidable-burn diagnostics. Same-session review continuation is a
separate informational diagnostic. Telemetry does not switch providers or
reset quota in response.

Acceptance covers Claude and Codex present/missing/malformed/limit-wall
fixtures, cache-present/cache-absent and reported-cost cases, bounded quota
snapshots, malformed snapshots, reset-window changes, unavailable account-wall
evidence, run-record and Loopyard-compatible stats round trips, phase-aware
rolling summaries, forecast arithmetic, low-change/high-token detection,
same-session continuation versus new-session re-review, redaction, and existing
run-result consumers.
