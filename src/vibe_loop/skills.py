from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from pathlib import Path

from vibe_loop.skill_deployment import (
    SkillDeploymentError,
    deploy_skill_bundle,
    deployment_drift_advisories,
)


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
        package_name="vibe-loop",
        source_repository="https://github.com/ei-grad/vibe-loop",
        report_diagnostic=report_diagnostic,
    )


def installed_skill_drift_advisories(home: Path) -> tuple[dict[str, object], ...]:
    try:
        return deployment_drift_advisories(home, skill_names=SKILL_NAMES)
    except (OSError, SkillDeploymentError) as exc:
        return (
            {
                "code": "skill_deployment_check_failed",
                "severity": "warning",
                "affected_skills": [],
                "deployments": [],
                "message": (
                    "could not verify bundled skill deployments: "
                    f"{type(exc).__name__}: {exc}; run `vibe-loop verify-skills`"
                ),
            },
        )
