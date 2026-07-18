# User stories

- [x] LIST-01 Establish story vocabulary
  - Dependencies: none
  - Scope: Define stable IDs for list-shaped tasks.
  - Acceptance: The vocabulary is documented.
  - Evidence: Existing parser test.
  - Requirements: PRD-TSK-002
- [ ] LIST-02 Preserve selected story metadata
  - Dependencies: LIST-01
  - Scope: Record the selected list story without losing its description.
  - Acceptance: The result names LIST-02 and its requirement.
  - Evidence: `python -m unittest discover`
  - Requirements: PRD-TSK-001, PRD-TSK-002
