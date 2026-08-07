from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from pathlib import Path

from vibe_loop.skill_deployment import (
    SkillDeploymentError,
    deploy_skill_bundle,
    deployment_drift_advisories,
    stale_deployment_entries,
    verify_worker_skill_deployments,
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


def bundled_skill_source_root() -> Path:
    return Path(str(importlib.resources.files("vibe_loop") / "skills")).resolve()


def refresh_stale_skill_deployments(
    home: Path,
    *,
    source_repo: Path,
) -> tuple[Path, ...]:
    """Reinstall recorded deployments whose source moved ahead of the install.

    Only a repository that supplies the running bundle can refresh it, and only
    an existing recorded deployment is refreshed: a host that never installed
    the skills must not acquire one as a side effect of integrating a slice.
    """
    source_root = bundled_skill_source_root()
    repo = source_repo.resolve()
    if repo != source_root and repo not in source_root.parents:
        return ()
    stale = stale_deployment_entries(
        verify_worker_skill_deployments(home),
        skill_names=SKILL_NAMES,
    )
    if not stale:
        return ()
    return tuple(install_skills(False, False, home))


def installed_skill_drift_advisories(home: Path) -> tuple[dict[str, object], ...]:
    try:
        source_root = Path(str(importlib.resources.files("vibe_loop") / "skills"))
        return deployment_drift_advisories(
            home,
            source_root=source_root,
            skill_names=SKILL_NAMES,
        )
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
