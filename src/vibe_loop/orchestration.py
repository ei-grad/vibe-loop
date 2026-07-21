from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from vibe_loop.config import (
    AgentConfig,
    AgentSelection,
    OrchestrationConfig,
    VibeConfig,
    agent_command_provider,
    parse_orchestration,
)


RUN_CONTRACT_VERSION = 1
RUN_CONTRACT_SOURCE_KINDS = ("config", "profile", "skill-proposal")
WORKSPACE_BRANCH_PREFIX = "vibe-loop/"
WORKSPACE_NAME_MAX_LENGTH = 64


class RunStage(enum.StrEnum):
    ACTIVATION = "activation"
    WORKSPACE = "workspace"
    IMPLEMENTING = "implementing"
    CANDIDATE = "candidate"
    GATES = "gates"
    REVIEW = "review"
    REMEDIATION = "remediation"
    CLOSURE = "closure"
    INTEGRATION = "integration"
    PROVENANCE = "provenance"
    CLASSIFICATION = "classification"
    FINALIZATION = "finalization"


class StageFailure(enum.StrEnum):
    LIMIT_WALL = "limit_wall"
    TIMED_OUT = "timed_out"
    STAGE_FAILED = "stage_failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


RUN_STAGES = tuple(stage.value for stage in RunStage)
STAGE_FAILURES = tuple(failure.value for failure in StageFailure)

# The worker-owned compatibility edge from implementing directly to
# classification is intentional: shadow mode cannot infer candidate, gate, or
# review boundaries that remain inside the worker process. Runtime-owned mode
# follows the longer path as those owners land in later ORC slices.
LEGAL_STAGE_TRANSITIONS: Mapping[RunStage, frozenset[RunStage]] = {
    RunStage.ACTIVATION: frozenset({RunStage.WORKSPACE}),
    RunStage.WORKSPACE: frozenset({RunStage.IMPLEMENTING}),
    RunStage.IMPLEMENTING: frozenset({RunStage.CANDIDATE, RunStage.CLASSIFICATION}),
    RunStage.CANDIDATE: frozenset({RunStage.GATES}),
    RunStage.GATES: frozenset(
        {RunStage.REVIEW, RunStage.REMEDIATION, RunStage.CLOSURE}
    ),
    RunStage.REVIEW: frozenset({RunStage.REMEDIATION, RunStage.INTEGRATION}),
    RunStage.REMEDIATION: frozenset({RunStage.CANDIDATE}),
    RunStage.CLOSURE: frozenset({RunStage.REMEDIATION, RunStage.INTEGRATION}),
    RunStage.INTEGRATION: frozenset({RunStage.PROVENANCE}),
    RunStage.PROVENANCE: frozenset({RunStage.CLASSIFICATION}),
    RunStage.CLASSIFICATION: frozenset({RunStage.FINALIZATION}),
    RunStage.FINALIZATION: frozenset(),
}


class StageTransitionError(RuntimeError):
    pass


class IllegalStageTransitionError(StageTransitionError):
    def __init__(
        self,
        from_stage: RunStage | None,
        to_stage: RunStage,
    ) -> None:
        self.from_stage = from_stage
        self.to_stage = to_stage
        source = from_stage.value if from_stage is not None else "<initial>"
        super().__init__(f"illegal run stage transition: {source} -> {to_stage.value}")


@dataclasses.dataclass(frozen=True)
class StageTransition:
    from_stage: RunStage | None
    to_stage: RunStage
    reason: str
    ordinal: int
    accepted: bool
    failure: StageFailure | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "from_stage": self.from_stage.value if self.from_stage is not None else "",
            "to_stage": self.to_stage.value,
            "reason": self.reason,
            "ordinal": self.ordinal,
            "accepted": self.accepted,
        }
        if self.failure is not None:
            payload["failure"] = self.failure.value
        return payload


StageJournal = Callable[[StageTransition], None]


@dataclasses.dataclass(frozen=True)
class DerivedStageProgress:
    stage: RunStage
    ordinal: int
    occurred_at: str = ""


class RunLifecycleStateMachine:
    def __init__(self, journal: StageJournal) -> None:
        self._journal = journal
        self._stage: RunStage | None = None
        self._ordinals: dict[RunStage, int] = {}

    @property
    def stage(self) -> RunStage | None:
        return self._stage

    @property
    def ordinal(self) -> int:
        if self._stage is None:
            return 0
        return self._ordinals.get(self._stage, 0)

    def transition(
        self,
        to_stage: RunStage,
        *,
        reason: str,
    ) -> StageTransition:
        accepted = (
            to_stage is RunStage.ACTIVATION
            if self._stage is None
            else to_stage in LEGAL_STAGE_TRANSITIONS[self._stage]
        )
        transition = StageTransition(
            from_stage=self._stage,
            to_stage=to_stage,
            reason=reason,
            ordinal=self._ordinals.get(to_stage, 0) + 1,
            accepted=accepted,
        )
        self._journal(transition)
        if not accepted:
            raise IllegalStageTransitionError(self._stage, to_stage)
        self._accept(transition)
        return transition

    def fail(
        self,
        failure: StageFailure,
        *,
        reason: str,
    ) -> StageTransition:
        if self._stage is None:
            raise StageTransitionError("cannot record a failure before activation")
        if self._stage is RunStage.FINALIZATION:
            destination = RunStage.FINALIZATION
        elif self._stage is RunStage.CLASSIFICATION:
            destination = RunStage.FINALIZATION
        else:
            destination = RunStage.CLASSIFICATION
        transition = StageTransition(
            from_stage=self._stage,
            to_stage=destination,
            reason=reason,
            ordinal=self._ordinals.get(destination, 0) + 1,
            accepted=True,
            failure=failure,
        )
        self._journal(transition)
        self._accept(transition)
        return transition

    def _accept(self, transition: StageTransition) -> None:
        self._stage = transition.to_stage
        self._ordinals[transition.to_stage] = transition.ordinal

    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, Any]],
        journal: StageJournal,
    ) -> RunLifecycleStateMachine:
        machine = cls(journal)
        for record in records:
            transition = accepted_stage_transition(record)
            if transition is None:
                continue
            machine._accept(transition)
        return machine


def accepted_stage_transition(
    record: Mapping[str, Any],
) -> StageTransition | None:
    if record.get("record_type") != "stage_transition":
        return None
    if record.get("accepted") is not True:
        return None
    try:
        to_stage = RunStage(record.get("to_stage"))
    except (TypeError, ValueError):
        return None
    raw_from = record.get("from_stage")
    try:
        from_stage = RunStage(raw_from) if raw_from else None
    except (TypeError, ValueError):
        return None
    raw_ordinal = record.get("ordinal")
    if isinstance(raw_ordinal, bool) or not isinstance(raw_ordinal, int):
        return None
    if raw_ordinal < 1:
        return None
    raw_failure = record.get("failure")
    try:
        failure = StageFailure(raw_failure) if raw_failure else None
    except (TypeError, ValueError):
        return None
    reason = record.get("reason")
    return StageTransition(
        from_stage=from_stage,
        to_stage=to_stage,
        reason=reason if isinstance(reason, str) else "",
        ordinal=raw_ordinal,
        accepted=True,
        failure=failure,
    )


def derive_stage_progress(
    records: Sequence[Mapping[str, Any]],
) -> DerivedStageProgress | None:
    latest: DerivedStageProgress | None = None
    for record in records:
        transition = accepted_stage_transition(record)
        if transition is None:
            continue
        occurred_at = record.get("occurred_at")
        latest = DerivedStageProgress(
            stage=transition.to_stage,
            ordinal=transition.ordinal,
            occurred_at=occurred_at if isinstance(occurred_at, str) else "",
        )
    return latest


@dataclasses.dataclass(frozen=True)
class RunContractProposal:
    kind: str
    source_id: str
    values: Mapping[str, object]
    digest: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RUN_CONTRACT_SOURCE_KINDS[1:]:
            raise ValueError(
                "run contract proposal kind must be profile or skill-proposal"
            )
        if not self.source_id:
            raise ValueError("run contract proposal source_id is required")
        if self.digest and not is_sha256_digest(self.digest):
            raise ValueError("run contract proposal digest must be a sha256 digest")

    @property
    def source_digest(self) -> str:
        if self.digest:
            return self.digest
        return sha256_digest(
            {"kind": self.kind, "id": self.source_id, "values": dict(self.values)}
        )


@dataclasses.dataclass(frozen=True)
class ResolvedRunContract:
    payload: Mapping[str, object]
    digest: str

    def to_record_payload(self) -> dict[str, object]:
        return {**self.payload, "contract_digest": self.digest}


class RunContractResolver:
    def __init__(self, config: VibeConfig) -> None:
        self.config = config

    def resolve(
        self,
        agent_selection: AgentSelection,
        *,
        profile: RunContractProposal | None = None,
        skill_proposal: RunContractProposal | None = None,
    ) -> ResolvedRunContract:
        if profile is not None and profile.kind != "profile":
            raise ValueError("profile proposal must have kind='profile'")
        if skill_proposal is not None and skill_proposal.kind != "skill-proposal":
            raise ValueError("skill proposal must have kind='skill-proposal'")

        effective = parse_orchestration(
            {},
            completion=self.config.completion,
            agent_profiles=self.config.agent_profiles,
        )
        contributors: list[dict[str, str]] = []
        for proposal in (skill_proposal, profile):
            if proposal is None:
                continue
            parsed = parse_orchestration(
                dict(proposal.values),
                completion=self.config.completion,
                agent_profiles=self.config.agent_profiles,
            )
            effective = overlay_explicit_orchestration(effective, parsed)
            contributors.append(
                {
                    "kind": proposal.kind,
                    "id": proposal.source_id,
                    "digest": proposal.source_digest,
                }
            )
        effective = overlay_explicit_orchestration(
            effective,
            self.config.orchestration,
        )

        config_source = config_source_identity(self.config, effective)
        if self.config.orchestration.explicit_keys or not contributors:
            contributors.append(config_source)
            primary_source = config_source
        else:
            primary_source = contributors[-1]

        implementer = route_payload(agent_selection.config, agent_selection.profile)
        reviewer_profile = effective.reviewer_profile
        if reviewer_profile is None:
            reviewer_agent = agent_selection.config
            reviewer_profile = agent_selection.profile
        else:
            reviewer_agent = self.config.agent_profiles[reviewer_profile]

        payload: dict[str, object] = {
            "contract_version": RUN_CONTRACT_VERSION,
            "mode": effective.mode,
            "source": {
                **primary_source,
                "inputs": contributors,
            },
            "implementer": {
                **implementer,
                "timeout_seconds": self.config.supervision.worker_timeout_seconds,
            },
            "reviewer": {
                **route_payload(reviewer_agent, reviewer_profile),
                "timeout_seconds": 0,
                "max_initial_passes": effective.max_initial_review_passes,
                "max_closure_passes": effective.max_closure_review_passes,
                "concurrency_budget": effective.reviewer_concurrency_budget,
            },
            "gates": [
                {"id": command_ref, "command_key": command_ref}
                for command_ref in effective.gates
            ],
            "integration": {
                "enabled": effective.integration_enabled,
                "verify_on_main": list(effective.verify_on_main),
            },
            "task_provenance": {
                "mode": effective.task_provenance_mode,
                "complete_adapter": None,
                "settlement": {
                    "requeue_adapter": (
                        "task_source.reset"
                        if self.config.task_source.reset_command is not None
                        else None
                    ),
                    "park_adapter": None,
                },
            },
            "remediation": {"max_rounds": effective.max_remediation_rounds},
        }
        return ResolvedRunContract(payload=payload, digest=sha256_digest(payload))


class WorkspaceProvisionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ProvisionedWorkspace:
    mode: str
    branch: str
    worktree: Path
    base_commit: str
    head_commit: str
    owner_run_id: str = ""
    dirty_at_adoption: bool = False

    def to_record_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "mode": self.mode,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "dirty_at_adoption": self.dirty_at_adoption,
        }
        if self.owner_run_id:
            payload["owner_run_id"] = self.owner_run_id
        return payload


class WorkspaceProvisioner:
    def __init__(
        self,
        *,
        repo: Path,
        main_branch: str,
        lock_manager: object,
        run_store: object,
        ignored_dirty_paths: Sequence[Path] = (),
    ) -> None:
        self.repo = repo.resolve()
        self.main_branch = main_branch
        self.lock_manager = lock_manager
        self.run_store = run_store
        self.ignored_dirty_paths = tuple(ignored_dirty_paths)

    def provision(
        self,
        *,
        task_id: str,
        run_id: str,
        base_commit: str,
        fencing_token: str | None = None,
        recovery_run_id: str = "",
        recovery_branch: str = "",
        recovery_worktree: Path | None = None,
    ) -> ProvisionedWorkspace:
        from vibe_loop.runs import RunLifecycleEvent
        from vibe_loop.workers import claim_worker_workspace

        self._validate_primary(base_commit)
        branch, worktree = self._workspace_identity(
            task_id=task_id,
            recovery_branch=recovery_branch,
            recovery_worktree=recovery_worktree,
        )
        workspace = self._create_or_adopt(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
            base_commit=base_commit,
            recovery_run_id=recovery_run_id,
        )
        try:
            self.run_store.append_lifecycle_event(
                RunLifecycleEvent.workspace_provisioned(
                    run_id=run_id,
                    task_id=task_id,
                    payload=workspace.to_record_payload(),
                )
            )
            claim_worker_workspace(
                self.lock_manager,
                self.run_store,
                task_id=task_id,
                run_id=run_id,
                branch=workspace.branch,
                worktree=workspace.worktree,
                repo=self.repo,
                base_commit=workspace.base_commit,
                fencing_token=fencing_token,
                ignored_dirty_paths=self.ignored_dirty_paths,
            )
        except KeyboardInterrupt:
            if workspace.mode == "created":
                self.compensate_created(workspace)
            raise
        except Exception:
            # Journal and claim backends are extensible local/command adapters;
            # any failure must compensate a workspace created by this run.
            if workspace.mode == "created":
                self.compensate_created(workspace)
            raise
        return workspace

    def compensate_created(self, workspace: ProvisionedWorkspace) -> None:
        if workspace.mode != "created":
            return
        from vibe_loop.workers import build_workspace_git_context, git_status_lines

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        entries = [
            entry
            for entry in context.worktrees
            if entry.path == workspace.worktree.resolve()
        ]
        if len(entries) != 1 or entries[0].branch != workspace.branch:
            raise WorkspaceProvisionError(
                "compensation_identity_mismatch",
                "refusing to compensate workspace whose git identity changed",
                details={
                    "branch": workspace.branch,
                    "worktree": str(workspace.worktree),
                },
            )
        if git_status_lines(
            workspace.worktree,
            ignored_dirty_paths=self.ignored_dirty_paths,
        ):
            raise WorkspaceProvisionError(
                "compensation_dirty_workspace",
                "refusing to remove a created workspace that became dirty",
                details={"worktree": str(workspace.worktree)},
            )
        self._git("worktree", "remove", str(workspace.worktree))
        self._git("branch", "-d", workspace.branch)

    def _validate_primary(self, base_commit: str) -> None:
        from vibe_loop.workers import build_workspace_git_context, git_status_lines

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if context.worktree_list_error:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "workspace provisioning requires readable git worktree state",
                details={"error": context.worktree_list_error},
            )
        if not context.worktrees or context.worktrees[0].path != self.repo:
            raise WorkspaceProvisionError(
                "primary_worktree_required",
                "workspace provisioning must run against the primary git worktree",
                details={"repo": str(self.repo)},
            )
        primary = context.worktrees[0]
        if primary.branch != self.main_branch:
            raise WorkspaceProvisionError(
                "primary_branch_mismatch",
                "primary worktree is not on the configured main branch",
                details={
                    "expected_branch": self.main_branch,
                    "current_branch": primary.branch,
                },
            )
        dirty = git_status_lines(
            self.repo,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if dirty:
            raise WorkspaceProvisionError(
                "dirty_primary_worktree",
                "primary worktree must be clean before worker provisioning",
                details={"dirty_summary": dirty[:20]},
            )
        resolved_base = self._git_text("rev-parse", "--verify", base_commit)
        main_head = self._git_text("rev-parse", "--verify", self.main_branch)
        if resolved_base != main_head:
            raise WorkspaceProvisionError(
                "base_main_mismatch",
                "workspace base no longer matches the configured main branch",
                details={"base_commit": resolved_base, "main_head": main_head},
            )

    def _workspace_identity(
        self,
        *,
        task_id: str,
        recovery_branch: str,
        recovery_worktree: Path | None,
    ) -> tuple[str, Path]:
        if bool(recovery_branch) != (recovery_worktree is not None):
            raise WorkspaceProvisionError(
                "incomplete_recovery_workspace",
                "recovery requires both a recorded branch and worktree",
            )
        if recovery_branch and recovery_worktree is not None:
            return recovery_branch, recovery_worktree.resolve()
        owned = self._existing_owned_identity(task_id)
        if owned is not None:
            return owned
        name = workspace_name(task_id)
        return (
            f"{WORKSPACE_BRANCH_PREFIX}{name}",
            self.repo.parent / f"{self.repo.name}-worktrees" / name,
        )

    def _create_or_adopt(
        self,
        *,
        task_id: str,
        run_id: str,
        branch: str,
        worktree: Path,
        base_commit: str,
        recovery_run_id: str,
    ) -> ProvisionedWorkspace:
        from vibe_loop.workers import build_workspace_git_context, git_status_lines

        if branch == self.main_branch or worktree == self.repo:
            raise WorkspaceProvisionError(
                "primary_workspace_forbidden",
                "a worker cannot use the primary worktree or main branch",
                details={"branch": branch, "worktree": str(worktree)},
            )
        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        branch_entries = [
            entry for entry in context.worktrees if entry.branch == branch
        ]
        path_entries = [
            entry for entry in context.worktrees if entry.path == worktree.resolve()
        ]
        branch_exists = (
            self._git_returncode(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
            )
            == 0
        )
        path_exists = worktree.exists()
        if (
            not branch_entries
            and not path_entries
            and not branch_exists
            and not path_exists
        ):
            worktree.parent.mkdir(parents=True, exist_ok=True)
            result = self._git_result(
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                base_commit,
            )
            if result.returncode != 0:
                self._compensate_partial_creation(
                    branch=branch,
                    worktree=worktree,
                    base_commit=base_commit,
                )
                raise WorkspaceProvisionError(
                    "workspace_create_failed",
                    "git could not create the task workspace",
                    details={"stderr": result.stderr.strip()},
                )
            workspace = ProvisionedWorkspace(
                mode="created",
                branch=branch,
                worktree=worktree.resolve(),
                base_commit=base_commit,
                head_commit=base_commit,
            )
            try:
                head_commit = self._git_text_at(
                    worktree, "rev-parse", "--verify", "HEAD"
                )
            except Exception:
                # Git state inspection can fail for several OS/repository
                # reasons after add succeeds; all share the same compensation.
                self._compensate_partial_creation(
                    branch=branch,
                    worktree=worktree,
                    base_commit=base_commit,
                )
                raise
            return dataclasses.replace(workspace, head_commit=head_commit)
        if (
            len(branch_entries) != 1
            or len(path_entries) != 1
            or branch_entries[0].path != worktree.resolve()
            or path_entries[0].branch != branch
        ):
            raise WorkspaceProvisionError(
                "workspace_collision",
                "existing branch/worktree state is ambiguous or mismatched",
                details={
                    "branch": branch,
                    "worktree": str(worktree),
                    "branch_worktrees": [str(entry.path) for entry in branch_entries],
                    "path_branches": [entry.branch for entry in path_entries],
                    "branch_exists": branch_exists,
                    "path_exists": path_exists,
                },
            )
        owner = self._ownership_record(
            task_id=task_id,
            branch=branch,
            worktree=worktree,
            recovery_run_id=recovery_run_id,
        )
        if owner is None:
            raise WorkspaceProvisionError(
                "workspace_ownership_unverified",
                "existing workspace has no matching task ownership record",
                details={"branch": branch, "worktree": str(worktree)},
            )
        self._reject_live_foreign_claim(
            task_id=task_id,
            run_id=run_id,
            branch=branch,
            worktree=worktree,
        )
        head = self._git_text_at(worktree, "rev-parse", "--verify", "HEAD")
        owner_base = owner.get("base_commit")
        if not isinstance(owner_base, str) or not owner_base:
            raise WorkspaceProvisionError(
                "workspace_base_unverified",
                "existing workspace ownership record has no base commit",
                details={"owner_run_id": str(owner.get("run_id") or "")},
            )
        if (
            self._git_returncode_at(
                worktree,
                "merge-base",
                "--is-ancestor",
                owner_base,
                head,
            )
            != 0
        ):
            raise WorkspaceProvisionError(
                "workspace_base_mismatch",
                "existing workspace does not descend from its recorded base",
                details={"base_commit": owner_base, "head_commit": head},
            )
        if (
            self._git_returncode(
                "merge-base",
                "--is-ancestor",
                owner_base,
                base_commit,
            )
            != 0
        ):
            raise WorkspaceProvisionError(
                "workspace_main_history_mismatch",
                "existing workspace base is not in the selected main history",
                details={
                    "workspace_base": owner_base,
                    "selected_base": base_commit,
                },
            )
        dirty = git_status_lines(
            worktree,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        if dirty and not recovery_run_id:
            raise WorkspaceProvisionError(
                "dirty_existing_workspace",
                "dirty existing workspace is preserved and cannot be adopted",
                details={"dirty_summary": dirty[:20]},
            )
        return ProvisionedWorkspace(
            mode="preserved" if dirty else "adopted",
            branch=branch,
            worktree=worktree.resolve(),
            base_commit=owner_base,
            head_commit=head,
            owner_run_id=str(owner.get("run_id") or ""),
            dirty_at_adoption=bool(dirty),
        )

    def _existing_owned_identity(self, task_id: str) -> tuple[str, Path] | None:
        from vibe_loop.workers import build_workspace_git_context

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        listed = {(entry.branch, entry.path.resolve()) for entry in context.worktrees}
        candidates: set[tuple[str, Path]] = set()
        for record in self.run_store.read_records():
            if record.get("record_type") != "workspace_claim":
                continue
            if record.get("task_id") != task_id:
                continue
            branch = record.get("branch")
            raw_worktree = record.get("worktree")
            if not isinstance(branch, str) or not isinstance(raw_worktree, str):
                continue
            identity = (branch, Path(raw_worktree).resolve())
            if identity in listed:
                candidates.add(identity)
        if len(candidates) > 1:
            raise WorkspaceProvisionError(
                "ambiguous_owned_workspaces",
                "multiple existing workspaces have ownership records for the task",
                details={
                    "workspaces": [
                        {"branch": branch, "worktree": str(worktree)}
                        for branch, worktree in sorted(
                            candidates,
                            key=lambda item: (item[0], str(item[1])),
                        )
                    ]
                },
            )
        return next(iter(candidates), None)

    def _compensate_partial_creation(
        self,
        *,
        branch: str,
        worktree: Path,
        base_commit: str,
    ) -> None:
        from vibe_loop.workers import build_workspace_git_context

        context = build_workspace_git_context(
            self.repo,
            main_branch=self.main_branch,
            ignored_dirty_paths=self.ignored_dirty_paths,
        )
        exact = [
            entry
            for entry in context.worktrees
            if entry.path == worktree.resolve() and entry.branch == branch
        ]
        if exact:
            remove = self._git_result("worktree", "remove", "--force", str(worktree))
            if remove.returncode != 0:
                raise WorkspaceProvisionError(
                    "partial_workspace_compensation_failed",
                    "git could not remove a partially created workspace",
                    details={"stderr": remove.stderr.strip()},
                )
        elif worktree.exists():
            raise WorkspaceProvisionError(
                "workspace_collision",
                "an unverified path appeared while creating the workspace; "
                "it was preserved",
                details={"branch": branch, "worktree": str(worktree)},
            )
        branch_ref = f"refs/heads/{branch}"
        if self._git_returncode("show-ref", "--verify", "--quiet", branch_ref) == 0:
            branch_head = self._git_text("rev-parse", "--verify", branch_ref)
            if branch_head != base_commit:
                raise WorkspaceProvisionError(
                    "partial_branch_changed",
                    "refusing to remove a partially created branch that changed",
                    details={"branch": branch},
                )
            delete = self._git_result("branch", "-D", branch)
            if delete.returncode != 0:
                raise WorkspaceProvisionError(
                    "partial_branch_compensation_failed",
                    "git could not remove a partially created branch",
                    details={"stderr": delete.stderr.strip()},
                )

    def _ownership_record(
        self,
        *,
        task_id: str,
        branch: str,
        worktree: Path,
        recovery_run_id: str,
    ) -> Mapping[str, object] | None:
        for record in reversed(self.run_store.read_records()):
            if record.get("record_type") != "workspace_claim":
                continue
            if record.get("branch") != branch:
                continue
            raw_worktree = record.get("worktree")
            if not isinstance(raw_worktree, str):
                continue
            if Path(raw_worktree).resolve() != worktree.resolve():
                continue
            owner_task_id = str(record.get("task_id") or "")
            owner_run_id = str(record.get("run_id") or "")
            if owner_task_id != task_id or (
                recovery_run_id and owner_run_id != recovery_run_id
            ):
                raise WorkspaceProvisionError(
                    "workspace_foreign_owner",
                    "the latest ownership record belongs to another task or run",
                    details={
                        "owner_task_id": owner_task_id,
                        "owner_run_id": owner_run_id,
                    },
                )
            return record
        return None

    def _reject_live_foreign_claim(
        self,
        *,
        task_id: str,
        run_id: str,
        branch: str,
        worktree: Path,
    ) -> None:
        from vibe_loop.workers import load_active_run_states

        for active in load_active_run_states(self.lock_manager):
            claim = active.workspace
            if claim is None or (active.task_id == task_id and active.run_id == run_id):
                continue
            if claim.branch == branch or claim.worktree.resolve() == worktree.resolve():
                raise WorkspaceProvisionError(
                    "workspace_live_owner",
                    "existing workspace is claimed by another active run",
                    details={
                        "owner_task_id": active.task_id,
                        "owner_run_id": active.run_id,
                    },
                )

    def _git(self, *args: str) -> None:
        result = self._git_result(*args)
        if result.returncode != 0:
            raise WorkspaceProvisionError(
                "git_command_failed",
                "git workspace operation failed",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )

    def _git_text(self, *args: str) -> str:
        return self._git_text_at(self.repo, *args)

    def _git_text_at(self, cwd: Path, *args: str) -> str:
        result = self._git_result_at(cwd, *args)
        if result.returncode != 0:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "git workspace state could not be read",
                details={"git_args": list(args), "stderr": result.stderr.strip()},
            )
        return result.stdout.strip()

    def _git_returncode(self, *args: str) -> int:
        return self._git_returncode_at(self.repo, *args)

    def _git_returncode_at(self, cwd: Path, *args: str) -> int:
        return self._git_result_at(cwd, *args).returncode

    def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._git_result_at(self.repo, *args)

    @staticmethod
    def _git_result_at(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise WorkspaceProvisionError(
                "git_state_unavailable",
                "git could not be executed for workspace provisioning",
                details={"error": str(exc)},
            ) from exc


def workspace_name(task_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-").lower()
    if not normalized:
        normalized = "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    prefix_limit = WORKSPACE_NAME_MAX_LENGTH - len(digest) - 1
    return f"{normalized[:prefix_limit]}-{digest}"


def overlay_explicit_orchestration(
    base: OrchestrationConfig,
    override: OrchestrationConfig,
) -> OrchestrationConfig:
    values = {
        field.name: getattr(override, field.name)
        for field in dataclasses.fields(OrchestrationConfig)
        if field.name in override.explicit_keys
    }
    return dataclasses.replace(
        base,
        **values,
        explicit_keys=base.explicit_keys | override.explicit_keys,
    )


def config_source_identity(
    config: VibeConfig,
    effective: OrchestrationConfig,
) -> dict[str, str]:
    source_id = (
        str(config.config_path) if config.config_path is not None else "defaults"
    )
    if config.config_digest:
        digest = config.config_digest
    else:
        digest_input = {
            "orchestration": effective.to_json(),
            "completion_command_keys": [
                f"completion.commands[{index}]"
                for index, _ in enumerate(config.completion.commands)
            ],
            "agent_profile_keys": sorted(config.agent_profiles),
        }
        digest = sha256_digest(digest_input)
    return {"kind": "config", "id": source_id, "digest": digest}


def route_payload(agent: AgentConfig, profile: str) -> dict[str, object]:
    provider = agent_command_provider(
        agent.command or "",
        agent.executable_kind or agent.agent_kind,
    )
    command_key = f"agent.profiles.{profile}.command" if profile else "agent.command"
    return {
        "profile": profile,
        "provider": provider or "unknown",
        "model": agent.model,
        "effort": agent.effort,
        "command_key": command_key,
    }


def sha256_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def is_sha256_digest(value: str) -> bool:
    prefix, separator, digest = value.partition(":")
    return (
        prefix == "sha256"
        and separator == ":"
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )
