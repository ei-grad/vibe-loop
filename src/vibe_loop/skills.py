from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from pathlib import Path

from vibe_loop.skill_deployment import deploy_skill_bundle


SKILL_NAMES = (
    "vibe-loop",
    "infinite-vibe-loop",
    "orchestrated-vibe-loop",
    "autopilot",
)


def install_skills(
    codex: bool,
    claude: bool,
    home: Path,
    *,
    force: bool = False,
    allow_unmerged: bool = False,
    report_diagnostic: Callable[[str], None] | None = None,
) -> list[Path]:
    source_root = Path(str(importlib.resources.files("vibe_loop") / "skills"))
    return deploy_skill_bundle(
        source_root=source_root,
        skill_names=SKILL_NAMES,
        home=home,
        codex=codex,
        claude=claude,
        force=force,
        allow_unmerged=allow_unmerged,
        report_diagnostic=report_diagnostic,
    )
