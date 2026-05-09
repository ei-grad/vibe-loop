from __future__ import annotations

import json
from pathlib import Path


source = Path("src/review_rules/codes.py").read_text(encoding="utf-8")
test_source = Path("tests/test_codes.py").read_text(encoding="utf-8")
findings = []
if "value = value.strip()" not in source:
    findings.append("Normalize code input with strip() before format checks.")
if "test_rejects_whitespace_only_code" not in test_source:
    findings.append("Add a regression test for whitespace-only input.")
print(json.dumps({"material_findings": findings}, indent=2))
