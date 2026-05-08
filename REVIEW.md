# Review Gates

These gates apply to every change that touches user-visible behavior, persisted
metadata, configuration defaults, command orchestration, generated artifacts, or
selection logic. A change is not reviewable until its author has made the
relevant gate evidence explicit in the diff, tests, or review notes.

## Semantic Regression Gate

Preserve the meaning of existing behavior, not just the shape of existing
fields, commands, or tests.

Blocking criteria:

- Identify the existing contract being touched from code, tests, docs, artifacts,
  or recent commits before changing it.
- Tests must assert meaning, provenance, and observable consequences, not just
  field presence, output shape, or successful execution.
- Do not replace a distinct external, upstream, or user-provided value with a
  local alias, placeholder, or fallback unless that fallback is the documented
  contract and tests cover the fallback path separately.
- Merge and conflict-resolution changes must preserve all previously accepted
  behavior that remains in scope, with tests proving the combined contract.
- Any intentional breaking change must be named as breaking in docs or review
  notes, with the old behavior and migration impact stated plainly.

## Default UX Gate

Defaults must keep common valid environments usable. Failing fast is acceptable
only when continuing would be unsafe, destructive, or genuinely ambiguous in a
way the program cannot resolve deterministically.

Blocking criteria:

- Explicit user configuration remains authoritative.
- When multiple valid choices are available, choose a documented deterministic
  default or a persisted/project-local preference. Do not make routine commands
  fail solely because more than one valid choice is available.
- When no valid choice is available, fail with an actionable diagnostic that
  names the missing configuration or dependency.
- The selected default and its source must be visible in diagnostics, logs, and
  structured output where users would debug behavior.
- Tests must cover single-option, multiple-option, explicit-override, and
  no-option paths for any default-resolution behavior.
