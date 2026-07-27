# Documentation

Start with the [configuration reference](configuration.md) for
`.vibe-loop.toml` settings and the [PRD index](prd/README.md) for authoritative
component contracts.

## Operator references

- [CLI reference](cli-reference.md)
- [Configuration reference](configuration.md)
- [Generated task discovery](generated-task-discovery.md)
- [Parallel worker orchestration](parallel-worker-orchestration.md)
- [Deterministic run orchestration](deterministic-run-orchestration.md)
- [Skill work modes](skill-work-modes.md)
- [Recorded skill deployment](skill-deployment.md)
- [Release checklist](release-checklist.md)
- [Planning analytics migration](planning-analytics.md)

## Evaluation and benchmarks

- [External benchmark fit](external-benchmark-fit.md)
- [Skill evaluation strategy](skill-evaluation-strategy.md)
- [Skill evaluation schema](skill-eval-schema.md)
- [Skill evaluation demo projects](skill-eval-demo-projects.md)

## Examples

- [Ralphex Markdown plan](examples/ralphex-markdown-plan.md)
- [Release-readiness dry run](examples/release-readiness-dry-run.json)
- [SWE-bench Pro smoke summary](examples/swe-bench-pro-smoke-summary.json)

## Product requirements

The [PRD index](prd/README.md) links every product-requirement document and
defines their authority and density rules.

## Documentation checks

`make doc-budget` checks the `README.md` and repository-root Markdown size
budgets, structural prose runs across all Markdown files, links, and
reachability. CI, release builds, and the installed pre-commit hook run this
gate.

Before each budget's deadline, content above its target but no larger than its
baseline passes with a warning. After the deadline, the target is the hard
limit, so CI, `make check`, and release builds remain blocked until the target
is met. The pre-commit hook likewise rejects any staged change that evaluates
an over-target budget. Extending or renegotiating a deadline requires a
reviewed edit to `doc-budgets.toml`.

`make doc-budget-refresh` explicitly lowers baselines after documentation
shrinks; normal checks never rewrite `doc-budgets.toml`. Structural exceptions
are intentionally grandfathered per file, so removing the recorded README
exception remains higher priority than adding another long prose run there.
