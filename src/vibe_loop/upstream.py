from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path


UPSTREAM_FETCH_TIMEOUT_SECONDS = 30.0


@dataclasses.dataclass(frozen=True)
class UpstreamSyncBlocker:
    code: str
    relation: str
    upstream: str
    ahead: int
    behind: int
    reviewed_commit: str
    reviewed_commit_contained: bool | None
    unmet_prerequisite: str
    fresh: bool

    def to_json(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class UpstreamSyncStatus:
    required: bool
    relation: str
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    reviewed_commit: str = ""
    reviewed_commit_contained: bool | None = None
    fresh: bool = False
    blocker: UpstreamSyncBlocker | None = None

    @property
    def satisfied(self) -> bool:
        return not self.required or self.blocker is None

    def to_json(self) -> dict[str, object]:
        return {
            "required": self.required,
            "relation": self.relation,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "reviewed_commit": self.reviewed_commit,
            "reviewed_commit_contained": self.reviewed_commit_contained,
            "fresh": self.fresh,
            "blocker": self.blocker.to_json() if self.blocker else None,
        }


def check_upstream_sync(
    repo: Path,
    main_branch: str,
    *,
    required: bool,
    reviewed_commit: str = "",
    require_reviewed_commit: bool = False,
    refresh: bool,
    unmet_prerequisite: str = "",
) -> UpstreamSyncStatus:
    if not required:
        return UpstreamSyncStatus(required=False, relation="policy_disabled")

    main_ref = f"refs/heads/{main_branch}"
    upstream, error = _git(
        repo,
        "for-each-ref",
        "--format=%(upstream:short)",
        main_ref,
    )
    if error or not upstream:
        return _blocked(
            code="missing_upstream",
            relation="missing_upstream",
            upstream="",
            reviewed_commit=reviewed_commit,
            fresh=False,
            unmet_prerequisite=unmet_prerequisite,
        )

    fresh = False
    if refresh:
        remote, remote_error = _git(
            repo,
            "for-each-ref",
            "--format=%(upstream:remotename)",
            main_ref,
        )
        if remote_error or not remote:
            return _blocked(
                code="missing_upstream",
                relation="missing_upstream",
                upstream=upstream,
                reviewed_commit=reviewed_commit,
                fresh=False,
                unmet_prerequisite=unmet_prerequisite,
            )
        _output, fetch_error = _git(
            repo,
            "fetch",
            "--quiet",
            "--",
            remote,
            timeout=UPSTREAM_FETCH_TIMEOUT_SECONDS,
            suppress_terminal_prompt=True,
        )
        if fetch_error:
            return _blocked(
                code="fetch_failed",
                relation="fetch_failed",
                upstream=upstream,
                reviewed_commit=reviewed_commit,
                fresh=False,
                unmet_prerequisite=unmet_prerequisite,
            )
        fresh = True
    else:
        return _blocked(
            code="stale_ref",
            relation="stale_ref",
            upstream=upstream,
            reviewed_commit=reviewed_commit,
            fresh=False,
            unmet_prerequisite=unmet_prerequisite,
        )

    counts, counts_error = _git(
        repo,
        "rev-list",
        "--left-right",
        "--count",
        f"{main_ref}...{upstream}",
    )
    if counts_error:
        return _blocked(
            code="git_relation_unavailable",
            relation="unavailable",
            upstream=upstream,
            reviewed_commit=reviewed_commit,
            fresh=fresh,
            unmet_prerequisite=unmet_prerequisite,
        )
    ahead_text, separator, behind_text = counts.partition("\t")
    try:
        ahead = int(ahead_text)
        behind = int(behind_text) if separator else 0
    except ValueError:
        return _blocked(
            code="git_relation_unavailable",
            relation="unavailable",
            upstream=upstream,
            reviewed_commit=reviewed_commit,
            fresh=fresh,
            unmet_prerequisite=unmet_prerequisite,
        )
    relation = (
        "equal"
        if ahead == behind == 0
        else "diverged"
        if ahead and behind
        else "ahead"
        if ahead
        else "behind"
    )
    contained: bool | None = None
    if reviewed_commit:
        try:
            contained = (
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", reviewed_commit, upstream],
                    cwd=repo,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )
        except OSError:
            contained = False

    code = ""
    prerequisite = unmet_prerequisite
    if require_reviewed_commit and not reviewed_commit:
        code = "reviewed_commit_missing"
        prerequisite = prerequisite or "reviewed_commit_containment"
    elif relation != "equal":
        code = f"upstream_{relation}"
        prerequisite = prerequisite or "upstream_equality"
    elif reviewed_commit and not contained:
        code = "reviewed_commit_not_upstream"
        prerequisite = prerequisite or "reviewed_commit_containment"
    elif unmet_prerequisite:
        code = "lifecycle_prerequisite_unmet"
    if code:
        return _blocked(
            code=code,
            relation=relation,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            reviewed_commit=reviewed_commit,
            reviewed_commit_contained=contained,
            fresh=fresh,
            unmet_prerequisite=prerequisite,
        )
    return UpstreamSyncStatus(
        required=True,
        relation=relation,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        reviewed_commit=reviewed_commit,
        reviewed_commit_contained=contained,
        fresh=fresh,
    )


def _blocked(
    *,
    code: str,
    relation: str,
    upstream: str,
    reviewed_commit: str,
    fresh: bool,
    unmet_prerequisite: str,
    ahead: int = 0,
    behind: int = 0,
    reviewed_commit_contained: bool | None = None,
) -> UpstreamSyncStatus:
    blocker = UpstreamSyncBlocker(
        code=code,
        relation=relation,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        reviewed_commit=reviewed_commit,
        reviewed_commit_contained=reviewed_commit_contained,
        unmet_prerequisite=unmet_prerequisite,
        fresh=fresh,
    )
    return UpstreamSyncStatus(
        required=True,
        relation=relation,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        reviewed_commit=reviewed_commit,
        reviewed_commit_contained=reviewed_commit_contained,
        fresh=fresh,
        blocker=blocker,
    )


def _git(
    repo: Path,
    *args: str,
    timeout: float | None = None,
    suppress_terminal_prompt: bool = False,
) -> tuple[str, str]:
    env = None
    if suppress_terminal_prompt:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", type(exc).__name__
    if result.returncode != 0:
        return "", f"git_exit_{result.returncode}"
    return result.stdout.strip(), ""
