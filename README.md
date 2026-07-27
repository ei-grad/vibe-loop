# vibe-loop

`vibe-loop` executes bounded AI coding tasks from a repository task source. It
selects runnable work, locks it, provisions an isolated worktree, launches an
agent, runs configured gates and review, records evidence, and can continue
until no runnable tasks remain.

Runtime-owned orchestration is the default; worker-owned orchestration remains
a compatibility mode. Four bundled [skills](#skills) also work directly in
Codex or Claude without the CLI.

> [!WARNING]
> `vibe-loop` is in early development. It is not yet well tested or broadly
> reviewed, so treat it as experimental automation and run it only where failed
> commands or incorrect agent behavior cannot damage important work.

## Installation

`vibe-loop` requires Python 3.11 or newer.

Install it as a standalone CLI:

```bash
uv tool install vibe-loop
pipx install vibe-loop
```

Install it into an existing Python environment:

```bash
python -m pip install vibe-loop
```

For unreleased changes, install the current repository state from GitHub:

```bash
uv tool install git+https://github.com/ei-grad/vibe-loop
```

## Quick Start

For a small repository, a Markdown task table is enough:

```markdown
| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-01 | P0 | Next | none | Make one scoped change. | Tests pass. | Not run. |
```

For an existing planning format, configure a repo-specific profile:

```bash
vibe-loop tasks configure --repo . --dry-run --json   # review candidate
vibe-loop tasks configure --repo . --json             # activate
```

Inspect the queue, then run a named or selected task:

```bash
vibe-loop tasks list --repo .
vibe-loop next --repo .
vibe-loop run TASK-01 --repo .
vibe-loop run-next --repo .
```

> [!NOTE]
> Routine agent work is smoother with a narrowly scoped allowlist and permission
> prompts disabled.

> [!WARNING]
> Any Codex or Claude session with permission prompts disabled — whether
> launched directly or by a worker command — MUST run in a container or VM with
> only the required repository, tools, network, and credentials.

## Skills

The package ships four skills: `vibe-loop` for one bounded slice,
`infinite-vibe-loop` for unattended finite slices, `orchestrated-vibe-loop` for
role-separated multi-agent work, and `autopilot` for supervising a persistent
worker pool. The [Skills PRD](docs/prd/skills.md) owns finite, infinite, and
shared workflow contracts; the [Autopilot PRD](docs/prd/autopilot.md) owns
supervision. Exact instructions live under `src/vibe_loop/skills/`; the
[work-mode guide](docs/skill-work-modes.md) compares all four.

Install them into Codex and/or Claude:

```bash
vibe-loop install-skills --codex --claude
vibe-loop verify-skills
```

Install from a clean `main` checkout. See
[skill deployment](docs/skill-deployment.md) for provenance and drift rules.

## Commands

### Tasks

```bash
vibe-loop tasks list --repo .
vibe-loop tasks tree --repo .
vibe-loop tasks inspect QUERY-09 --repo .
vibe-loop tasks runnable --repo .
vibe-loop tasks locks --repo .
vibe-loop tasks configure --repo . --dry-run --json
vibe-loop tasks configure --repo . --force-refresh --json
vibe-loop tasks configure --repo . --promotion-toml
vibe-loop next --repo .
```

`vibe-loop tasks` without a subcommand is a compatibility alias for
`vibe-loop tasks runnable`.

### Run and worker commands

Run orchestration, worker lifecycle, session provenance, recovery, and usage
commands are documented in the [CLI reference](docs/cli-reference.md#run-commands).
The linked PRDs there are authoritative for behavior.

### Status and diagnostics

```bash
vibe-loop workers --repo . --json
vibe-loop runs list --repo .
vibe-loop runs inspect <run-id> --repo .
vibe-loop runs summary --repo . --hours 24 --json
vibe-loop doctor --repo . --json
vibe-loop specs check --repo . --json
vibe-loop --version
```

`runs list` groups records by run id and shows the latest status plus log path;
`runs inspect <run-id>` prints the detailed record history. `vibe-loop
--version` prints the package version; editable source-tree and non-tag Git
installs append `(git <short-sha>)`.

### Evaluation commands

Evaluation invocations are documented in the
[CLI reference](docs/cli-reference.md#evaluation-commands); evaluation methodology
and release policy live in the [skill evaluation strategy](docs/skill-evaluation-strategy.md).

## Autopilot

Autopilot supervises one repository or an optional project registry above
`run-until-done`. Its default worktree policy is report-only; starting it does
not authorize branch or worktree deletion.

- The [Autopilot PRD](docs/prd/autopilot.md) is authoritative for supervision,
  status, recovery, project binding, planning, and wake behavior.
- The [CLI reference](docs/cli-reference.md#autopilot-commands) owns command
  syntax, flags, defaults, and output conventions.
- Interactive board, agent, and timeline views live in the
  [loopyard](https://github.com/ei-grad/loopyard) web UI.

```bash
vibe-loop autopilot status --repo . --json
vibe-loop autopilot run --repo . --once
```

## Configuration

A minimal `.vibe-loop.toml`:

```toml
main_branch = "main"
state_dir = ".vibe-loop"

[agent]
kind = "auto"

[task_source]
type = "markdown-plan"

[orchestration]
mode = "runtime-owned"

[autopilot]
require_clean_repo = true
```

See the [configuration reference](docs/configuration.md) for the annotated
configuration, option index, task-source setup, budgets, routing, locks, and
runtime-owned reviewer configuration.

## Where things are documented

- [Documentation index](docs/README.md) — guides and implementation references.
- [PRD index](docs/prd/README.md) — authoritative product contracts.
- [CLI reference](docs/cli-reference.md) — commands, flags, and output.
- [Configuration reference](docs/configuration.md) — setup and option index.

The PRDs own the former README detail for [skills](docs/prd/skills.md) and
[local run orchestration](docs/prd/run-orchestration.md).

## Spec-driven workflow execution

`vibe-loop` executes task artifacts produced by Spec Kit, Kiro, OpenSpec, and
repository-specific planning systems. Those tools own requirements, design,
and approval; `vibe-loop` owns bounded task execution and evidence. A spec or
PRD alone is not proof of implementation.

In this repository, `PROMPT.md` sets philosophy, `docs/prd/` holds stable
contracts, and the configured task source holds runnable slices. The
[Spec-Driven Execution PRD](docs/prd/spec-driven-execution.md) is authoritative.

## Relationship to ralphex

`vibe-loop` is inspired by
[umputun/ralphex](https://github.com/umputun/ralphex): a repeatable autonomous
loop that gives coding agents bounded tasks, validates results, and records
progress instead of relying on one long interactive chat. The main differences:

- `ralphex` is plan-file centered; `vibe-loop` is task-source agnostic
  (generated profiles, Markdown tables, explicit plan paths, or command
  adapters) and fits existing project planning instead of requiring a dedicated
  plan directory.
- `ralphex` runs a dedicated plan through task and review phases; `vibe-loop`
  runs one repository backlog slice at a time and merges reviewed slices back to
  `main` frequently.
- `vibe-loop` treats agent execution as configuration (template commands, not a
  hard dependency on one CLI) and keeps workers finite, leaving
  branch/worktree management to the agent.

## Development

Install the repository tools and run the standard checks:

```bash
uv sync
make check
```

Install repository hooks with:

```bash
make install-hooks
```

The [release checklist](docs/release-checklist.md) owns versioning, installed
hook behavior, skill readiness, TestPyPI staging, and publishing.

## License

`vibe-loop` is licensed under the MIT License. See [`LICENSE`](LICENSE).
