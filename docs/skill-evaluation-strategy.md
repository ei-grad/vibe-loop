# Skill Evaluation Strategy

Last researched: 2026-05-08.

Bundled skills should be evaluated as first-class artifacts, not only reviewed
as prompt text. The useful signal is the delta caused by the skill under the
same agent harness, model, task, environment, and budget. The baseline matrix
should include at least no bundled skill, bundled `vibe-loop`, bundled
`infinite-vibe-loop` where continuation is relevant, and any future revised
skill candidate. If self-generated skills are tested, report them separately
instead of treating them as a replacement for curated bundled skills.

Evaluation work should start with research and planning artifacts, not
benchmark execution. The first deliverables are a source-linked methodology
note, a benchmark-fit matrix, demo-project specifications, and a run-artifact
schema. Running public benchmarks is a later optional validation step after the
local suite and adapters are designed.

## Common Practice

The eval harness should follow common agent-eval practice:

- Prefer deterministic outcome graders: repository tests, command exit status,
  file-state checks, structured result records, and merge/report invariants.
- Add transcript or trajectory graders only for behavior that outcomes miss:
  review discipline, unsafe git operations, tool misuse, unnecessary user
  prompts, failure to use task evidence, or skill-trigger failures.
- Run multiple trials when the underlying model or agent harness is stochastic,
  and report pass rate, absolute skill uplift, normalized gain, confidence
  interval, cost, latency, token usage where available, command count, and
  failure taxonomy.
- Preserve full run logs, diffs, final repository state, structured result JSON,
  grader output, and source fingerprints so regressions can be reproduced.
- Treat public benchmark scores as comparative context, not proof that the
  bundled skills work for this workflow. Local representative tasks should be
  the release gate.

## Demo Projects

Bundled demo/example projects are the first evaluation surface because they can
target the workflow contract directly. They should be small real repositories
with seeded plans, tests, git state, and failure modes covering finite slice
execution, generated task discovery, review loops, branch/worktree discipline,
worker reporting, locks, and integration behavior. Each demo task should have a
deterministic oracle and an expected trace envelope, but graders should avoid
requiring an exact action sequence when multiple valid implementations exist.

## External Benchmarks

Relevant external benchmarks are useful as adapters after the local suite is
stable:

- SWE-bench Verified and SWE-bench Live test real issue resolution through
  fail-to-pass and pass-to-pass tests in Dockerized repositories.
- SWE-rebench V2 provides a much larger multilingual executable task corpus with
  prebuilt images and metadata. It is valuable for stratified smoke and stress
  samples, especially non-Python repositories, but its scale and training-data
  orientation make it a poor first release gate.
- Terminal-Bench covers terminal-native engineering workflows that resemble
  agent CLI behavior more closely than patch-only benchmarks.
- GAIA, AgentBench, OSWorld, and tau-bench cover broader research, GUI, web, and
  tool-user interaction skills. Keep them as later comparability targets unless
  `vibe-loop` grows those interaction surfaces.

## Research Basis

- [Anthropic, Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  multi-turn agent tasks need outcome grading, transcript review for behavior
  that outcomes miss, deterministic graders where possible, repeated trials, and
  ongoing eval ownership.
- [Agent Skills, Evaluating skill output quality](https://agentskills.io/skill-creation/evaluating-skills):
  skill evals should compare with-skill and without-skill runs, isolate clean
  workspaces, capture timing and tokens, grade concrete assertions, aggregate
  deltas, inspect patterns, and include human review for qualities assertions
  miss.
- [Skill Bench, Writing Evals](https://skill-bench.dev/docs/writing-evals/):
  colocated YAML eval cases, specific criteria, negative trigger cases, context
  files, realistic prompts, and cost-aware case counts are common skill-eval
  patterns.
- [SkillsBench](https://arxiv.org/abs/2602.12670):
  paired no-skill, curated-skill, and self-generated-skill conditions provide a
  useful methodology; the paper reports pass-rate uplift, normalized gain, cost,
  and trajectory failure analysis.
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) and
  [SWE-bench Live](https://swe-bench-live.github.io/) provide execution-based
  coding-agent benchmark patterns through Dockerized repositories and test
  oracles.
- [SWE-rebench V2](https://arxiv.org/abs/2602.23866) and the
  [SWE-rebench V2 dataset](https://huggingface.co/datasets/nebius/SWE-rebench-V2)
  are useful for multilingual stratified samples with prebuilt images and rich
  metadata, but should be treated as a later adapter target rather than the
  first release gate.
- [Terminal-Bench](https://www.tbench.ai/) is relevant for terminal-native
  engineering workflows and can complement patch-oriented benchmarks once local
  workflow evals are stable.
