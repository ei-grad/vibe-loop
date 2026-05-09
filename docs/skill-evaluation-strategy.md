# Skill Evaluation Strategy

Last researched: 2026-05-09.

Bundled skills should be evaluated as first-class runtime artifacts, not only
reviewed as prompt text. The release-quality signal for `vibe-loop` is the
controlled delta caused by a skill under the same agent harness, model, task,
workspace, budget, and grader. Public coding-agent benchmarks can provide
context later, but they should not be the first gate for bundled skill changes
because they measure broad agent capability more than this repository's workflow
contract.

The recommended path is:

1. Build a local paired skill-eval suite around small deterministic demo
   repositories.
2. Run every task in at least a no-skill baseline and a bundled-skill condition.
3. Grade task outcomes with deterministic checks wherever possible.
4. Add trajectory graders only for workflow-contract behavior that final
   outcomes miss.
5. Preserve enough artifacts to reproduce failures, compare deltas, audit
   graders, and diagnose regressions.

No public benchmark run is required before the local methodology, artifact
schema, and demo-project specifications exist. The initial bundled-skill run
record and artifact contract is specified in
[`docs/skill-eval-schema.md`](skill-eval-schema.md).
External benchmark fit and sampling recommendations are specified in
[`docs/external-benchmark-fit.md`](external-benchmark-fit.md).

## Source Comparison

The sources agree on the core shape: isolate trials, compare with and without
the skill, grade concrete outcomes, record traces, repeat stochastic runs, and
inspect failures. They differ mostly in packaging and emphasis.

| Source | What it contributes | Implication for `vibe-loop` |
| --- | --- | --- |
| [Agent Skills, Evaluating skill output quality](https://agentskills.io/skill-creation/evaluating-skills) | Skill-specific eval loop with realistic prompts, optional input files, paired `with_skill` and `without_skill` runs, timing/token capture, assertion grading, aggregate `benchmark.json`, human review, and iteration. | Use paired conditions as the default. Keep workspaces clean per trial. Store outputs, timing, grading, and aggregate summaries separately for each condition. |
| [Agent Skills, Optimizing skill descriptions](https://agentskills.io/skill-creation/optimizing-descriptions) | Trigger evals should include should-trigger and should-not-trigger queries, realistic prompt variation, multiple runs, and trigger-rate thresholds. | Evaluate activation separately from task success. Negative trigger cases are mandatory because an over-broad skill can harm unrelated coding work. |
| [Skill Bench, Writing Evals](https://skill-bench.dev/docs/writing-evals/) and [Non-determinism](https://skill-bench.dev/docs/non-determinism/) | Colocated YAML cases with prompts, files, criteria or weighted rubrics, `expect_skill`, timeouts, negative triggers, CI reporting, evidence-backed grading, and multi-run aggregation for flaky LLM behavior. | YAML is a reasonable authoring format for future cases, but the durable contract should be schema-driven rather than tied to one hosted action. Criteria must be specific and verifiable, not generic quality claims. |
| [SkillsBench paper](https://arxiv.org/abs/2602.12670) and [project](https://github.com/benchflow-ai/skillsbench) | Treats skills as the experimental variable across no-skill, curated-skill, and self-generated-skill conditions; uses deterministic verifiers, oracle solutions, isolation, full trajectory logging, leakage audits, five trials per task, pass-rate deltas, normalized gain, cost analysis, and failure taxonomy. | Adopt the paired design, deterministic verifiers, leakage checks, and normalized-gain reporting. Do not copy the scale; `vibe-loop` needs representative workflow tasks before broad domain coverage. |
| [Anthropic, Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Defines tasks, trials, graders, transcripts, outcomes, harnesses, repeated trials, pass@k versus pass^k, transcript review, human calibration, and the need to audit ambiguous tasks and brittle graders. | Outcome graders should lead, but transcripts are required for review discipline, unsafe operations, and unnecessary user prompts. Report both average success and consistency where reliability matters. |
| [OpenAI grader guidance](https://developers.openai.com/api/docs/guides/graders) and [openai/evals custom eval docs](https://github.com/openai/evals/blob/main/docs/custom-eval.md) | Separates data items from sampled outputs, supports string, similarity, model, Python, and combined graders, and warns that model graders need their own calibration set to avoid reward hacking. | Represent grader inputs and outputs explicitly. Prefer code graders for file and repository state. If LLM graders are used, evaluate the grader itself against expert-labeled examples and keep human spot checks. |
| Coding-agent benchmarks: [SWE-bench datasets](https://www.swebench.com/SWE-bench/guides/datasets/), [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/), [OpenAI's 2026 SWE-bench Verified deprecation note](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/), [SWE-bench Live](https://github.com/microsoft/SWE-bench-Live), [SWE-rebench V2](https://arxiv.org/abs/2602.23866), [Terminal-Bench](https://github.com/harbor-framework/terminal-bench), and [tau-bench](https://arxiv.org/abs/2406.12045) | Mature agent benchmarks use real environments, hidden or held-out tests, Docker/sandbox isolation, oracle/reference solutions, multiple trials, live or refreshed datasets, and state-based scoring for tool-user workflows. They also expose recurring pitfalls: underspecified tasks, brittle tests, contamination, infrastructure failures, and plausible-but-wrong patches. | Use public benchmarks as later smoke or stress adapters, not as the local release gate. Treat SWE-bench Verified as historical context, not a current frontier-coding signal; EVAL-04 records the current fit matrix and includes SWE-bench Pro as the preferred SWE-style smoke adapter. Local demo tasks need clear specs, reference solutions, deterministic checks, and workflow-specific invariants that public benchmarks do not measure. |

## Evaluation Questions

`vibe-loop` skill evals should answer three different questions instead of
collapsing them into one pass rate:

- **Activation:** Did the right skill load for relevant prompts, and did it stay
  silent for unrelated prompts?
- **Task outcome:** Did the agent produce the required repository state, tests,
  docs, commit, report, or integration result?
- **Workflow contract:** Did the agent follow the finite-slice discipline:
  inspect first, isolate work, avoid unsafe git operations, run relevant checks,
  use independent review, address findings, integrate only when permitted, and
  report structured status when supervised?

The first question is a trigger eval. The second is mostly deterministic
repository grading. The third needs trajectory grading because a patch can pass
tests while still violating the workflow contract.

## Paired Conditions

Every bundled skill task should run under a controlled matrix:

- `no_skill`: same user prompt, no bundled skill available.
- `vibe_loop`: bundled `vibe-loop` skill available and expected to activate.
- `infinite_vibe_loop`: only for continuation/backlog scenarios where the
  infinite skill is relevant.
- `candidate_skill`: a proposed skill revision or alternative packaging.
- `self_generated_skill`: optional research condition, reported separately from
  curated bundled skills because SkillsBench found self-generated skill gains to
  be unreliable.

Keep the agent harness, model, tool permissions, repository seed, time budget,
network policy, and task prompt constant across paired conditions. Randomize or
alternate run order when practical so cache warmth, transient service state, or
developer attention does not systematically favor one condition. Record the
skill content hash, harness command, model identity, budget, source fingerprint,
and run order for every trial.

Each trial needs a clean worktree or fresh fixture checkout. Do not reuse a
modified repository, `.vibe-loop/` state directory, lock file, transcript, or
skill cache across conditions unless that state is explicitly part of the task.

## Task Suite Design

Start with local demo/example repositories before external benchmarks. They can
target behavior public benchmarks miss:

- finite one-slice execution through implementation, review, commit, and local
  integration;
- generated Markdown task discovery and degraded discovery diagnostics;
- review-loop remediation and re-review;
- branch/worktree discipline with unrelated user changes present;
- worker report protocol and `main-integration` lock behavior;
- scheduler/lock interactions for parallel workers;
- negative trigger prompts that ask ordinary coding questions and should not
  load the skill.

The concrete EVAL-08 fixture plan is specified in
[`docs/skill-eval-demo-projects.md`](skill-eval-demo-projects.md).

Each task case should include:

- a human-written prompt that resembles an actual user or supervisor request;
- a seeded repository state and explicit source fingerprints;
- a reference solution or oracle evidence proving the task is solvable;
- deterministic outcome graders;
- expected workflow-contract evidence;
- permitted tools and budgets;
- a failure taxonomy label set for post-run classification;
- fixture data that avoids secrets and external credentials by default.

For early development, use a small suite of high-signal cases rather than a
large noisy benchmark. A practical starting point is 6-10 task-outcome cases,
8-10 should-trigger prompts, and 8-10 should-not-trigger prompts. Skill Bench's
3-5 case minimum is enough for smoke coverage, but `vibe-loop` needs broader
coverage before treating results as release evidence.

## Grader Strategy

Use deterministic graders first:

- repository tests and command exit status;
- file-state assertions over expected paths, content, and schemas;
- git assertions for branch/worktree creation, commit presence, merge state, and
  absence of forbidden rewrites;
- `.vibe-loop/` lock, run, and report record checks;
- source fingerprint and artifact completeness checks;
- timeout and resource-budget checks.

Use transcript or trajectory graders only for behavior that deterministic
outcomes cannot prove:

- skill activated or failed to activate;
- agent skipped required review or re-review;
- agent used unsafe git operations or touched unrelated files;
- agent asked unnecessary user questions when the task was decidable;
- agent ignored task evidence or selected an unrelated slice;
- agent entered final integration without the required permission or lock;
- agent claimed checks passed without evidence.

LLM graders may be useful for trajectory classification, but they need
guardrails:

- grade against specific rubric items with cited transcript evidence;
- run the grader on expert-labeled examples before trusting aggregate scores;
- keep model-grader output separate from deterministic pass/fail;
- spot-check failures and sampled passes with human review;
- watch for grader hacking, especially when agents can see or infer grader
  wording.

## Trial Counts And Metrics

Agent runs are stochastic even at low temperature, and tool availability can
change outcomes. Single-run smoke results are useful while authoring cases, but
release evidence should use repeated trials.

Recommended trial policy:

- trigger evals: 3 runs per query initially, reporting trigger rate;
- local smoke suite: 1 trial per condition for quick developer feedback;
- release gate for bundled skill changes: 3 trials per task per condition;
- high-risk changes or flaky cases: 5 trials per task per condition;
- public benchmark adapters: sample sizes follow
  [`docs/external-benchmark-fit.md`](external-benchmark-fit.md), with results
  labeled as non-leaderboard unless the official harness, scaffold, sample,
  budget, and reporting rules are followed.

Report these metrics:

- per-condition pass rate and per-task pass rate;
- absolute uplift: `pass_with_skill - pass_no_skill`;
- normalized gain: `(pass_with_skill - pass_no_skill) / (1 - pass_no_skill)`,
  with the denominator caveat that ceiling effects can make this misleading;
- pass@1 for first-attempt coding-style success;
- pass@k only when multiple attempts are an allowed product behavior;
- pass^k when consistency across repeated attempts is the reliability target;
- confidence interval or bootstrap interval for aggregate pass-rate deltas;
- latency, command count, token usage where available, and cost;
- timeout rate and infrastructure-error rate;
- workflow-contract violation rate;
- trigger false-positive and false-negative rates;
- failure taxonomy counts.

Do not collapse infrastructure failures into agent failures. Record them,
exclude them from primary task pass-rate calculations when justified, and report
the exclusion count.

## Artifact Model

Every trial should leave a reproducible artifact bundle. EVAL-01 should turn
this into a versioned schema, but the research recommendation is:

```text
eval-runs/
  <suite-id>/
    manifest.json
    aggregate.json
    aggregate.md
    cases/
      <case-id>/
        <condition>/
          trial-<n>/
            run.json
            prompt.txt
            skill-fingerprint.json
            repo-fingerprint.json
            logs/
            transcript.jsonl
            diff.patch
            final-tree.json
            command-results.json
            deterministic-grades.json
            trajectory-grades.json
            human-review.json
```

Required fields include task id, condition, trial number, agent harness command,
model id, skill id and hash, repository seed hash, budget, start and finish
timestamps, timeout status, exit status, final commit or tree hash, raw grader
outputs, summarized evidence, and failure taxonomy. Logs should be stored as
files, not copied into aggregate JSON. Any path likely to contain credentials
must be rejected or redacted by the harness.

## Known Benchmark Pitfalls

Task ambiguity is a first-order risk. Anthropic's guidance, OpenAI's original
SWE-bench Verified work, and OpenAI's 2026 Verified deprecation analysis all
emphasize that underspecified tasks and brittle graders can punish valid
solutions. Every local case needs a reference solution and a task statement that
contains the information the grader expects.

Test-only grading can overstate correctness. SWE-bench-style benchmarks grade
patches by selected fail-to-pass and pass-to-pass tests, but the
[PatchDiff empirical study](https://arxiv.org/abs/2503.15223) found that some
plausible SWE-bench Verified patches pass benchmark validation while still
failing broader developer-written tests or diverging behaviorally from the
human patch. For `vibe-loop`, pair tests with review and diff/tree inspection
for workflow-sensitive changes.

Contamination and stale public tasks make leaderboard comparisons weak evidence
for skill quality. OpenAI stopped reporting SWE-bench Verified in February 2026
after finding both flawed residual tests and training-data exposure, and
recommended SWE-bench Pro for current reporting. SWE-bench Live and SWE-rebench
V2 address freshness by refreshing or scaling datasets, but that also increases
harness, Docker, storage, and resource cost. Treat external benchmark adapters
as optional context after the local suite is stable. The current adapter
recommendations and caveats are recorded in
[`docs/external-benchmark-fit.md`](external-benchmark-fit.md).

Exact trajectory matching is too brittle. Graders should check required
invariants and outcomes, not force a single action sequence when multiple valid
engineering paths exist. Workflow-contract graders should flag missing gates and
unsafe actions, not harmless differences in exploration order.

Model-graded evals can drift. Calibrate LLM graders against expert examples,
version grader prompts, and keep deterministic graders authoritative whenever
the property can be checked by code.

Self-generated skills are not a substitute for curated skills. SkillsBench's
results make this a separate research condition, not a release replacement for
the bundled skills.

## Remaining Questions

- Which transcript fields can be stored safely without retaining secret-like
  command output?
- What threshold should block bundled skill releases: absolute pass-rate
  regression, workflow-contract regression, trigger regression, or a combined
  gate?
- How should `vibe-loop` normalize costs and token counts across Codex, Claude,
  and other future harnesses when their telemetry differs?
- Which deferred comparators, if any, become worth adapting after the first
  SWE-style and terminal-workflow adapters produce stable evidence?
