from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path


SKILL_NAMES = ("vibe-loop", "infinite-vibe-loop", "orchestrated-vibe-loop")


def install_skills(codex: bool, claude: bool, home: Path) -> list[Path]:
    targets: list[Path] = []
    if codex:
        targets.append(home / ".codex" / "skills")
    if claude:
        targets.append(home / ".claude" / "skills")
    if not targets:
        targets.extend([home / ".codex" / "skills", home / ".claude" / "skills"])

    installed: list[Path] = []
    source_root = importlib.resources.files("vibe_loop") / "skills"
    for target_root in targets:
        target_root.mkdir(parents=True, exist_ok=True)
        for skill_name in SKILL_NAMES:
            source = source_root / skill_name
            target = target_root / skill_name
            shutil.copytree(source, target, dirs_exist_ok=True)
            installed.append(target)
    return installed
