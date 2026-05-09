# local-demo-v1

`local-demo-v1` contains small repository fixtures for deterministic bundled
skill evaluations. They are fixture sources, not runnable benchmark results.

Use `vibe_loop.eval_examples.materialize_eval_example` to copy a case into an
isolated workdir before running an agent or a grader. Materialized agent
workdirs omit `eval/reference.patch` by default so the reference solution stays
outside the trial checkout.
