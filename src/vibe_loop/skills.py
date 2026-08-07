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
SOURCE_REPOSITORY = "https://github.com/ei-grad/vibe-loop"


def bundled_skill_source_root() -> Path:
    return Path(str(importlib.resources.files("vibe_loop") / "skills"))


def repository_supplies_bundle(source_repo: Path) -> bool:
    """Whether this repository is the source of the running skill bundle.

    Only an editable or in-tree runtime resolves its bundled skills inside a
    repository; a wheel or tool install resolves them under its own site
    directory, where a repository merge can never make them stale.
    """
    source_root = bundled_skill_source_root().resolve()
    repo = source_repo.resolve()
    return repo == source_root or repo in source_root.parents


def install_skills(
    codex: bool,
    claude: bool,
    home: Path,
    *,
    force: bool = False,
    allow_unmerged: bool = False,
    report_diagnostic: Callable[[str], None] | None = None,
) -> list[Path]:
    return deploy_skill_bundle(
        source_root=bundled_skill_source_root(),
        skill_names=SKILL_NAMES,
        home=home,
        codex=codex,
        claude=claude,
        force=force,
        allow_unmerged=allow_unmerged,
        package_name="vibe-loop",
        source_repository=SOURCE_REPOSITORY,
        report_diagnostic=report_diagnostic,
    )


def refresh_stale_skill_deployments(
    home: Path,
    *,
    source_repo: Path,
) -> tuple[Path, ...]:
    """Reinstall recorded deployments whose source moved ahead of the install.

    Only a repository that supplies the running bundle can refresh it, and the
    write is confined to the runtime roots that already record this bundle: a
    runtime the operator never installed into must not acquire a deployment as
    a side effect of integrating a slice.
    """
    if not repository_supplies_bundle(source_repo):
        return ()
    stale = stale_deployment_entries(
        verify_worker_skill_deployments(home),
        skill_names=SKILL_NAMES,
    )
    if not stale:
        return ()
    return tuple(
        deploy_skill_bundle(
            source_root=bundled_skill_source_root(),
            skill_names=SKILL_NAMES,
            home=home,
            roots=tuple(dict.fromkeys(entry.target_root for entry in stale)),
            package_name="vibe-loop",
            source_repository=SOURCE_REPOSITORY,
        )
    )


def installed_skill_drift_advisories(home: Path) -> tuple[dict[str, object], ...]:
    try:
        return deployment_drift_advisories(
            home,
            source_root=bundled_skill_source_root(),
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
