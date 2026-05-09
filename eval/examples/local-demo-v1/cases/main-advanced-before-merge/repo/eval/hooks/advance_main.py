from __future__ import annotations

import subprocess
from pathlib import Path


Path("docs/harness-note.md").write_text(
    "# Harness Note\n\nMain advanced before the worker merged.\n",
    encoding="utf-8",
)
subprocess.run(["git", "add", "docs/harness-note.md"], check=True)
subprocess.run(["git", "commit", "-m", "docs: add harness note"], check=True)
