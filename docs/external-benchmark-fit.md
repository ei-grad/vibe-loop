# External Benchmark Fit

Last researched: 2026-05-09.

This note evaluates external benchmarks as optional context for bundled
`vibe-loop` skill evals. It is not a leaderboard plan. Public benchmarks are
useful only after the local paired suite is stable, because they mostly measure
agent or model capability while `vibe-loop` needs evidence about workflow
discipline: task selection, isolation, review, remediation, commit, integration,
reporting, and safe git behavior.

No public benchmark run is required for this slice. The resource estimates below
are planning estimates inferred from each benchmark's harness shape, not measured
capacity numbers from this repository.

## Recommendation

Integrate external adapters in this order, if a concrete product question needs
them:

1. Add a small SWE-style smoke adapter first: SWE-bench Pro public tasks, or a
   SWE-rebench V2 sample if Pro access, terms, or scaffold constraints block
   useful local use.
2. Add Terminal-Bench 2.0 as a terminal-workflow stress test only after coding
   outcome adapters can preserve task IDs, image identifiers, logs, diffs, and
   workflow-contract grades.
3. Defer SWE-bench-Live until the adapter can pin a dataset month, image
   identifiers, gold-patch validation results, and the actual denominator used
   for a run.
4. Do not use SWE-bench Verified as a release gate. Treat it as historical
   context or a harness-compatibility smoke test at most.
5. Keep GAIA, AgentBench, OSWorld, and tau-style suites out of the first coding
   adapter set. They are useful comparators for browsing, GUI, or tool-policy
   questions, but they do not directly measure this repository's finite coding
   loop.

## Fit Matrix

| Benchmark | Workflow relevance | Harness and resource estimate | Scoring semantics | Contamination and license constraints | Recommendation |
| --- | --- | --- | --- | --- | --- |
| [SWE-bench Verified](https://www.swebench.com/) | Medium for coding-patch outcomes, low for current frontier signal. It does not measure review, branch/worktree isolation, or local integration unless `vibe-loop` adds transcript graders around the run. | Dockerized SWE-bench harness; medium storage and CPU for small samples, high for full 500-instance runs. | Percent resolved over 500 human-filtered instances; official site reports resolved percentage across Full, Verified, Lite, Multilingual, and Multimodal splits. | OpenAI's [2026 analysis](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) says Verified has residual test flaws and training-data exposure, and recommends reporting SWE-bench Pro instead. The SWE-bench harness is MIT-licensed, but generated instances execute tests against upstream repositories with their own licenses and dependency terms. Dataset and generated tasks are public, so contamination risk is high for current models. | Avoid as an effectiveness gate. Use only for SWE-bench harness regression checks or historical comparisons, clearly labeled non-leaderboard. |
| [SWE-bench Pro](https://labs.scale.com/papers/swe_bench_pro) | High for realistic coding-agent outcomes. Long-horizon multi-file changes resemble work `vibe-loop` may supervise, though the benchmark still grades patch correctness more than workflow discipline. | Public subset plus inaccessible held-out and commercial subsets; Docker/SWE-style execution with high compute, storage, and wall-clock cost because tasks can take hours to days for humans. | Pass@1 under a unified scaffold in the paper; leaderboard comparability depends on the official scaffold, split, and reporting rules. | Designed as contamination-resistant relative to SWE-bench; public tasks can still become training data after release. Held-out and commercial tasks are not available locally, so local adapters cannot claim full-benchmark comparability. Check Scale/Hugging Face terms and upstream repository licenses before redistribution. | First-choice SWE-style smoke adapter when access and terms are acceptable. Run 10-20 public tasks, stratified by repository, language, task length, and expected patch spread. |
| [SWE-bench-Live](https://swe-bench-live.github.io/) | High for freshness and real issue resolution, but operationally unstable by design. Monthly updates are valuable for contamination resistance and expensive for reproducibility. | Python evaluation package, patch-file submission, per-instance DockerHub images, Linux and Windows variants. High storage/network cost; Windows support raises environment complexity. | Resolved percentage over selected split/month. The repository recommends gold-patch validation and allows the denominator to be the number of instances that pass gold locally at experiment time. | MIT-licensed harness, but underlying repositories and generated instances carry their own licensing and validity concerns. Tasks are public after release, and some instances may become invalid as dependencies drift. | Defer until adapter can pin month ranges, split, image names/digests, platform, gold-patch pass count, and denominator. Later use one fixed month with 10-20 validated tasks. |
| [SWE-rebench V2](https://arxiv.org/abs/2602.23866) | High for multilingual SWE-agent coverage and useful as a scalable optional adapter. It is closer to training-data generation than a compact release gate. | Released datasets, collection/execution code, pre-built images for 32,000+ executable tasks across 20 languages and 3,600+ repositories, plus 120,000+ additional tasks with installation instructions and tests. Full use is high storage/network cost; sampled use is manageable if images are pinned. | Executable task success with fail-to-pass tests and metadata; paper emphasizes diagnostic studies and confounder flags rather than one canonical public release gate. | Fresher and broader than original SWE-bench, but public release means future contamination. Underlying repository licenses vary. Generated problem statements and LLM-filtered soundness require conservative metadata filtering. | Good first or second SWE-style adapter. Start with 20-30 tasks across 4-6 languages, filter out known confounders, and report dataset version, instance IDs, language, repo, image ID, and grader provenance. |
| [Terminal-Bench 2.0](https://www.tbench.ai/) | Medium. It tests autonomous terminal work, setup recovery, and end-to-end verification, but many tasks are not coding-repository changes and do not require review or integration. | Harbor-native benchmark with Dockerized terminal environments; the [Terminal-Bench 2.0 paper](https://arxiv.org/abs/2601.11868) reports 89 curated tasks with human-written solutions and comprehensive tests. Medium to high Docker storage and wall-clock cost depending on selected tasks. | Task success rate from verifier tests. Official Harbor runs are needed for leaderboard-style comparisons. | Apache-2.0 harness repository, but public task definitions and oracle solutions create contamination risk. Check the exact dataset version and bundled task data license before packaging a subset. Some tasks may be reward-hackable if tests are visible or copied into the wrong place, so adapter isolation matters. | Add after SWE-style adapters as a stress comparator. Sample 8-12 tasks across software, data, and system-administration categories, and grade workflow-contract violations separately from task success. |
| [GAIA](https://arxiv.org/abs/2311.12983) | Low for `vibe-loop` coding workflow. It is a general assistant benchmark for reasoning, browsing, multimodality, and tool use. | Mostly browser/tool/API harness rather than Dockerized coding repos; low local storage but requires network, browsing, and multimodal support. | Exact-answer style leaderboard; paper reports 466 questions with 300 answers withheld for leaderboard use. | Public questions and held-out answers have different access constraints. Web-browsing dependencies can drift, and redistributing answer data requires dataset-card/license review. | Out of scope for first external adapters. Use only if evaluating research/browsing skills around `vibe-loop`, not finite coding-loop quality. |
| [AgentBench](https://github.com/THUDM/AgentBench) | Low to medium. OS and database environments overlap with agent tooling, but the benchmark is a 2023-era general-agent suite rather than a current coding-workflow benchmark. | Eight environments: OS, database, knowledge graph, digital card game, lateral thinking puzzles, house-holding, web shopping, and web browsing. Requires Python 3.9-era setup and Docker for some tasks; medium operational cost and old dependency risk. | Environment-specific scores over dev/test splits; repository notes multi-turn interaction requires thousands of model generations. | Apache-2.0 repository, public and old enough to be contaminated. Some environments are recompiled from other datasets with their own terms. | Defer or avoid. It is a weak signal for `vibe-loop` beyond broad tool-agent sanity checks. |
| [OSWorld](https://arxiv.org/abs/2404.07972) and [OSWorld-Verified](https://xlang.ai/blog/osworld-verified) | Low for coding, high for GUI/computer-use agents. It does not exercise repository task sources, branch isolation, review loops, or commit/integration workflow. | Real desktop environments across Ubuntu, Windows, and macOS through VM, Docker, VMware, VirtualBox, or cloud providers. High storage, KVM/VM, screenshot, and parallelization cost. | Custom execution-based evaluation scripts over 369 open-ended desktop tasks; OSWorld-Verified improves infrastructure and task quality. | Apache-2.0 code, but VM images, web accounts, Google Drive setup, proxies, and live websites introduce credential, licensing, and drift constraints. Public tasks have contamination risk. | Out of scope for `vibe-loop` skill releases unless the project later adds GUI/computer-use skills. Track as a comparator only. |
| [tau-bench](https://arxiv.org/abs/2406.12045), [current tau3-bench repo](https://github.com/sierra-research/tau2-bench) | Medium for policy-constrained tool reliability, low for coding workflow. It is relevant to report consistency and `pass^k`, not repository changes. | Python/uv harness with LLM user simulators, API tools, policies, and domains such as airline, retail, telecom, banking knowledge, and voice. Low storage, medium to high API cost because repeated trials and user simulators are central. | Final database state compared with annotated goal state; paper introduces `pass^k` for reliability over repeated trials. Current tau3-bench adds voice, knowledge, and task fixes. | Original tau-bench repository now warns its tasks are outdated and points users to the tau3-bench codebase. MIT license, but API-provider terms, generated user traces, and domain data must be recorded. | Defer. If adopted, use tau3-bench, not the outdated original repo, with 25-50 text-mode tasks and repeated trials when the product question is policy/tool reliability. |

## Sampling Strategy

External samples should be small, pinned, and reproducible. The goal is to
detect regressions and gather directional evidence, not to approximate public
leaderboards.

| Adapter stage | Minimum sample | Stratification | Trials | Primary signal |
| --- | --- | --- | --- | --- |
| SWE-bench Pro public smoke | 10-20 tasks | Repository, language, task length, expected patch spread, public difficulty metadata if available | 1 trial per condition for smoke; 3 trials for release evidence if budget allows | Patch success plus `vibe-loop` workflow-contract violations |
| SWE-rebench V2 multilingual smoke | 20-30 tasks | 4-6 languages, repository diversity, setup complexity, confounder flags, image availability | 1 trial per condition initially; 3 trials for stable samples | Multilingual pass rate, infrastructure failure rate, setup failure taxonomy |
| Terminal-Bench 2.0 stress | 8-12 tasks | Software, data, system-administration, and long-running terminal tasks; exclude tasks with known harness brittleness | 1 trial for developer feedback; 3 trials for stress evidence | Terminal task success plus unsafe command, review, and reporting trajectory checks |
| SWE-bench-Live freshness probe | 10-20 tasks from one pinned month | Month, language/platform, repository, gold-patch validation status | 1 trial after validating gold patches; repeat only for selected stable month | Fresh task success with denominator and invalid-instance count reported |
| tau3-bench reliability probe | 25-50 text-mode tasks | Domain, mutating tool type, policy complexity | At least 3 repeated trials because `pass^k` is the point | Consistency, policy/tool errors, unnecessary user turns |

Do not sample GAIA, AgentBench, or OSWorld for the first coding adapter. Their
coverage is too far from the finite coding-loop contract to justify the harness
cost before the local suite and SWE-style adapters are stable.

## Adapter Risks

The first EVAL-07 implementation should treat these as adapter requirements, not
only benchmark background:

- SWE-bench Pro: preserve the official scaffold, split, and reporting rules when
  claiming comparability; otherwise label results non-leaderboard. Record public
  versus held-out availability, terms, upstream repository licenses, and the
  exact instance IDs.
- SWE-rebench V2: pin dataset version, image identifiers, language, repository,
  and confounder metadata. Filter known setup, dependency, and test-quality
  confounders before using results as release evidence.
- Terminal-Bench 2.0: keep task tests isolated from agent-editable paths, record
  Harbor/task version, and classify harness setup failures separately from agent
  failures.
- SWE-bench-Live: validate gold patches locally before selecting the sample,
  record the pinned month and platform, and report the denominator after invalid
  or failing-gold instances are excluded.
- tau3-bench: use repeated trials and `pass^k` when testing policy/tool
  reliability, and record user-simulator, model, API provider, and domain data
  versions because they materially affect comparability.

## Reporting Rules

Every external run must record:

- benchmark name, version, split, date or month, instance IDs, repository or
  image identifiers, and platform;
- agent command, model, scaffold, tool permissions, network policy, and time
  budget;
- skill condition, skill fingerprint, prompt text, and run order;
- raw logs, final diff or final state, deterministic grader outputs, trajectory
  grades, and infrastructure-failure classification;
- exact denominator, skipped instances, invalid-instance checks, and any
  gold-patch validation result;
- a statement that results are non-leaderboard unless the official harness,
  scaffold, sample, budget, and submission rules were followed.

External benchmark results must not replace the local release gate. They are
diagnostic context for adapter quality and broad agent capability, while local
demo fixtures remain the authoritative test for bundled skill behavior.
