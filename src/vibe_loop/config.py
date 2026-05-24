from __future__ import annotations

import dataclasses
import math
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def shell_quote(s: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([s])
    return shlex.quote(s)


def prepare_shell_command(
    command: str,
) -> tuple[str | list[str], bool]:
    if sys.platform != "win32":
        return command, True
    parts = _split_windows_command(command)
    resolved = shutil.which(parts[0])
    if resolved is None:
        return command, True
    if resolved.lower().endswith((".cmd", ".bat")):
        script = _resolve_cmd_wrapper_target(resolved)
        if script is not None:
            return [sys.executable, script, *parts[1:]], False
        return [resolved, *parts[1:]], True
    if resolved.lower().endswith(".py"):
        return [sys.executable, resolved, *parts[1:]], False
    return [resolved, *parts[1:]], False


def _split_windows_command(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    argc = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        return [command]
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _resolve_cmd_wrapper_target(cmd_path: str) -> str | None:
    try:
        content = Path(cmd_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in content.splitlines():
        line = line.lstrip("@").strip()
        if line.startswith('"') and "%~dp0" in line:
            after = line.split("%~dp0", 1)[1]
            script_name = after.split('"')[0]
            return str(Path(cmd_path).parent / script_name)
    return None


DEFAULT_PLAN_PATHS = (
    "PLAN.md",
    "docs/PLAN.md",
    "plan.md",
    "docs/plan.md",
    "docs/plans.md",
    "docs/ROADMAP.md",
    "ROADMAP.md",
    "TODO.md",
)
CONFIG_FILE_NAME = ".vibe-loop.toml"
DEFAULT_RUNNABLE_STATUSES = ("Active", "Next", "Planned")
GENERATED_TASK_PROFILE_CACHE_FILE = "generated-task-source.json"
GENERATED_TASK_PROFILE_SCHEMA_VERSION = 1
GENERATED_TASK_PROFILE_PROMPT_VERSION = 1
PLANNING_ANALYTICS_ARTIFACT_DIR = "planning-analytics"
PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY = "current-runner-parity"
PLANNING_ANALYTICS_SCHEDULE_POLICIES = (
    "current-runner-parity",
    "lightmetrics-parity",
)
PLANNING_ANALYTICS_DEFAULT_OUTPUTS = {
    "timeline_json": "timeline.json",
    "gantt_html": "gantt.html",
    "benchmark_json": "duration-benchmark.json",
    "benchmark_markdown": "duration-benchmark.md",
}
PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES = ("diagnostic", "disabled")
PLANNING_ANALYTICS_DURATION_MODEL_NAMES = ("robust-duration-baseline-v1",)
SUPERVISION_DEFAULT_MAX_RESTARTS = 3
SUPERVISION_DEFAULT_COOLDOWN_SECONDS = 30.0
SUPERVISION_CONFIG_KEYS = frozenset({"max_restarts", "cooldown_seconds"})
LOCK_BACKEND_TYPES = ("directory", "command")
LOCKS_COMMAND_KEYS = frozenset(
    {"acquire_command", "release_command", "status_command", "list_command"}
)
LOCKS_CONFIG_KEYS = frozenset({"type", "lease_seconds"}) | LOCKS_COMMAND_KEYS
SPEC_DIAGNOSTICS_DEFAULT_APPROVED_STATES = ("approved",)
SPEC_DIAGNOSTICS_CONFIG_KEYS = frozenset(
    {
        "require_approved",
        "require_current_fingerprints",
        "require_requirement_coverage",
        "require_completion_evidence",
        "approved_states",
        "override_commands",
    }
)
PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL = {
    "name": "robust-duration-baseline-v1",
    "group_min_sample_count": 2,
    "similarity_min_score": 0.35,
    "similarity_max_examples": 3,
    "similarity_blend_weight": 0.25,
    "fallback_minutes": 60,
}
TASK_SOURCE_SOURCE_KEYS = frozenset(
    {
        "type",
        "plan_path",
        "plan_paths",
        "list",
        "next",
        "probe",
        "profile",
    }
)
GENERATED_TASK_PROFILE_FORBIDDEN_KEYS = frozenset(
    {
        "command",
        "commands",
        "list",
        "next",
        "probe",
        "selection_command",
        "locks",
        "lock_backend",
        "acquire_command",
        "release_command",
        "status_command",
        "list_command",
    }
)

AGENT_KIND_VALUES = ("auto", "codex", "claude", "custom")
AGENT_PROMPT_DIALECTS = ("codex", "claude")
AGENT_SKILL_REF_PREFIX = {
    "codex": "$",
    "claude": "/",
}
AGENT_SKILL_REF_DIALECT = {
    prefix: dialect for dialect, prefix in AGENT_SKILL_REF_PREFIX.items()
}
AGENT_COMMAND_DEFAULTS = {
    "codex": {
        "command": "codex exec {prompt}",
        "selection_command": "codex exec {prompt}",
    },
    "claude": {
        "command": "claude -p {prompt}",
        "selection_command": "claude -p {prompt}",
    },
}
SUPPORTED_AGENT_CLIS = tuple(AGENT_COMMAND_DEFAULTS)
AGENT_PREFERRED_CLI = "codex"
AGENT_DEFAULT_POLICY_SOURCE = "codex-first"
AGENT_DEFAULT_POLICY = (
    "Explicit .vibe-loop.toml agent commands win. agent.kind controls built-in "
    "prompt dialects; kind=auto keeps Codex-first defaults for omitted commands. "
    "Custom agents must configure prompt_dialect or skill_ref_prefix for worker "
    "prompts. Legacy unkinded explicit commands may use compatibility inference, "
    "reported through diagnostics."
)


class AgentResolutionError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentDetection:
    codex: str | None = None
    claude: str | None = None

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(name for name in SUPPORTED_AGENT_CLIS if self.path_for(name))

    def path_for(self, name: str) -> str | None:
        return getattr(self, name)

    def summary(self) -> str:
        if not self.available:
            return "none"
        return ", ".join(f"{name}={self.path_for(name)}" for name in self.available)

    def to_json(self) -> dict[str, object]:
        return {
            "available": list(self.available),
            "codex": {
                "available": self.codex is not None,
                "path": self.codex,
            },
            "claude": {
                "available": self.claude is not None,
                "path": self.claude,
            },
        }


@dataclasses.dataclass(frozen=True)
class AgentPromptDialectResolution:
    prompt_dialect: str | None
    prompt_dialect_source: str
    skill_ref_prefix: str | None
    skill_ref_prefix_source: str
    diagnostics: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    command: str | None = None
    selection_command: str | None = None
    command_source: str = "unresolved:no-supported-cli"
    selection_command_source: str = "unresolved:no-supported-cli"
    detected: AgentDetection = dataclasses.field(default_factory=AgentDetection)
    forward_stderr: bool = False
    agent_kind: str = "auto"
    agent_kind_source: str = "default:auto"
    executable_kind: str | None = None
    prompt_dialect: str | None = "codex"
    prompt_dialect_source: str = "legacy-default:codex"
    skill_ref_prefix: str | None = "$"
    skill_ref_prefix_source: str = "legacy-default:codex"
    compatibility_diagnostics: tuple[str, ...] = ()

    def require_command(self) -> str:
        if self.command:
            return self.command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.command",
                self.command_source,
                self.detected,
            )
        )

    def require_selection_command(self) -> str:
        if self.selection_command:
            return self.selection_command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.selection_command",
                self.selection_command_source,
                self.detected,
            )
        )

    def require_skill_ref_prefix(self) -> str:
        if self.skill_ref_prefix:
            return self.skill_ref_prefix
        raise AgentResolutionError(
            unresolved_prompt_dialect_message(
                self.agent_kind,
                self.prompt_dialect_source,
            )
        )

    def diagnostics(self) -> list[str]:
        messages: list[str] = list(self.compatibility_diagnostics)
        if not self.command:
            messages.append(
                unresolved_agent_command_message(
                    "agent.command",
                    self.command_source,
                    self.detected,
                )
            )
        if not self.selection_command:
            messages.append(
                unresolved_agent_command_message(
                    "agent.selection_command",
                    self.selection_command_source,
                    self.detected,
                )
            )
        if self.command and not self.skill_ref_prefix:
            messages.append(
                unresolved_prompt_dialect_message(
                    self.agent_kind,
                    self.prompt_dialect_source,
                )
            )
        return messages

    def to_json(self) -> dict[str, object]:
        return {
            "command_configured": self.command is not None,
            "command_source": self.command_source,
            "selection_command_configured": self.selection_command is not None,
            "selection_command_source": self.selection_command_source,
            "forward_stderr": self.forward_stderr,
            "agent_kind": self.agent_kind,
            "agent_kind_source": self.agent_kind_source,
            "executable_kind": self.executable_kind,
            "prompt_dialect": self.prompt_dialect,
            "prompt_dialect_source": self.prompt_dialect_source,
            "skill_ref_prefix": self.skill_ref_prefix,
            "skill_ref_prefix_source": self.skill_ref_prefix_source,
            "detected": self.detected.to_json(),
            "default_policy_source": AGENT_DEFAULT_POLICY_SOURCE,
            "default_policy": AGENT_DEFAULT_POLICY,
            "diagnostics": self.diagnostics(),
        }


@dataclasses.dataclass(frozen=True)
class TaskSourceConfig:
    type: str = "markdown-plan"
    plan_path: str | None = None
    plan_paths: tuple[str, ...] = DEFAULT_PLAN_PATHS
    profile: dict[str, Any] | None = None
    list_command: str | None = None
    next_command: str | None = None
    probe_command: str | None = None
    runnable_statuses: tuple[str, ...] = DEFAULT_RUNNABLE_STATUSES
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def explicit_source_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.explicit_keys & TASK_SOURCE_SOURCE_KEYS))

    @property
    def allows_generated_cache(self) -> bool:
        return not self.explicit_source_keys

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "type": self.type,
            "plan_path": self.plan_path,
            "plan_paths": list(self.plan_paths),
            "profile": self.profile,
            "list_command": self.list_command,
            "next_command": self.next_command,
            "probe_command": self.probe_command,
            "runnable_statuses": list(self.runnable_statuses),
            "explicit_keys": sorted(self.explicit_keys),
            "explicit_source_keys": list(self.explicit_source_keys),
            "allows_generated_cache": self.allows_generated_cache,
        }


@dataclasses.dataclass(frozen=True)
class CompletionConfig:
    commands: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SupervisionConfig:
    max_restarts: int = SUPERVISION_DEFAULT_MAX_RESTARTS
    cooldown_seconds: float = SUPERVISION_DEFAULT_COOLDOWN_SECONDS
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "max_restarts": self.max_restarts,
            "cooldown_seconds": self.cooldown_seconds,
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class LockConfig:
    type: str = "directory"
    acquire_command: str | None = None
    release_command: str | None = None
    status_command: str | None = None
    list_command: str | None = None
    lease_seconds: int | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def command_backend(self) -> bool:
        return self.type == "command"

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "type": self.type,
            "command_backend": self.command_backend,
            "acquire_command": self.acquire_command,
            "release_command": self.release_command,
            "status_command": self.status_command,
            "list_command": self.list_command,
            "lease_seconds": self.lease_seconds,
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsOutputs:
    timeline_json: str | None = None
    gantt_html: str | None = None
    benchmark_json: str | None = None
    benchmark_markdown: str | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def has_explicit_paths(self) -> bool:
        return any(
            getattr(self, key) is not None for key in PLANNING_ANALYTICS_DEFAULT_OUTPUTS
        )


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsDurationModelConfig:
    name: str = str(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["name"])
    group_min_sample_count: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["group_min_sample_count"]
    )
    similarity_min_score: float = float(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_min_score"]
    )
    similarity_max_examples: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_max_examples"]
    )
    similarity_blend_weight: float = float(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_blend_weight"]
    )
    fallback_minutes: int = int(
        PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["fallback_minutes"]
    )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "parameters": {
                "group_min_sample_count": self.group_min_sample_count,
                "similarity_min_score": self.similarity_min_score,
                "similarity_max_examples": self.similarity_max_examples,
                "similarity_blend_weight": self.similarity_blend_weight,
                "fallback_minutes": self.fallback_minutes,
            },
        }


@dataclasses.dataclass(frozen=True)
class PlanningAnalyticsConfig:
    schedule_policy: str = PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY
    subject_matching: str = "diagnostic"
    worklog_command: str | None = None
    outputs: PlanningAnalyticsOutputs = dataclasses.field(
        default_factory=PlanningAnalyticsOutputs
    )
    duration_model: PlanningAnalyticsDurationModelConfig = dataclasses.field(
        default_factory=PlanningAnalyticsDurationModelConfig
    )
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys


@dataclasses.dataclass(frozen=True)
class SpecDiagnosticsConfig:
    require_approved: bool = False
    require_current_fingerprints: bool = False
    require_requirement_coverage: bool = False
    require_completion_evidence: bool = False
    approved_states: tuple[str, ...] = SPEC_DIAGNOSTICS_DEFAULT_APPROVED_STATES
    override_commands: tuple[str, ...] = ()
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def enforces_execution(self) -> bool:
        return (
            self.require_approved
            or self.require_current_fingerprints
            or self.require_requirement_coverage
            or self.require_completion_evidence
        )

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "require_approved": self.require_approved,
            "require_current_fingerprints": self.require_current_fingerprints,
            "require_requirement_coverage": self.require_requirement_coverage,
            "require_completion_evidence": self.require_completion_evidence,
            "approved_states": list(self.approved_states),
            "override_commands": list(self.override_commands),
            "explicit_keys": sorted(self.explicit_keys),
            "enforces_execution": self.enforces_execution,
        }


@dataclasses.dataclass(frozen=True)
class VibeConfig:
    repo: Path
    main_branch: str = "main"
    state_dir: str = ".vibe-loop"
    agent: AgentConfig = dataclasses.field(default_factory=AgentConfig)
    task_source: TaskSourceConfig = dataclasses.field(default_factory=TaskSourceConfig)
    completion: CompletionConfig = dataclasses.field(default_factory=CompletionConfig)
    supervision: SupervisionConfig = dataclasses.field(
        default_factory=SupervisionConfig
    )
    locks: LockConfig = dataclasses.field(default_factory=LockConfig)
    planning_analytics: PlanningAnalyticsConfig = dataclasses.field(
        default_factory=PlanningAnalyticsConfig
    )
    specs: SpecDiagnosticsConfig = dataclasses.field(
        default_factory=SpecDiagnosticsConfig
    )
    config_path: Path | None = None
    config_source: str = "default"

    @property
    def state_path(self) -> Path:
        return self.repo / self.state_dir

    @property
    def generated_task_profile_path(self) -> Path:
        return self.state_path / GENERATED_TASK_PROFILE_CACHE_FILE

    @property
    def planning_analytics_state_path(self) -> Path:
        return self.state_path / PLANNING_ANALYTICS_ARTIFACT_DIR

    def config_report(self) -> dict[str, object]:
        return {
            "source": self.config_source,
            "path": str(self.config_path) if self.config_path else None,
        }


def load_config(repo: Path) -> VibeConfig:
    repo = repo.resolve()
    config_path, config_source = resolve_config_file(repo)
    data = read_config_file(config_path) if config_path is not None else {}
    task_source = parse_task_source(data.get("task_source", {}))
    completion = parse_completion(data.get("completion", {}), repo)
    agent = parse_agent(data.get("agent", {}))
    supervision = parse_supervision(data.get("supervision", {}))
    locks = parse_locks(data.get("locks", {}))
    planning_analytics = parse_planning_analytics(data.get("planning_analytics", {}))
    specs = parse_specs(data.get("specs", {}))
    return VibeConfig(
        repo=repo,
        config_path=config_path,
        config_source=config_source,
        main_branch=str(data.get("main_branch") or "main"),
        state_dir=str(data.get("state_dir") or ".vibe-loop"),
        agent=agent,
        task_source=task_source,
        completion=completion,
        supervision=supervision,
        locks=locks,
        planning_analytics=planning_analytics,
        specs=specs,
    )


def resolve_config_file(repo: Path) -> tuple[Path | None, str]:
    local = repo / CONFIG_FILE_NAME
    if local.is_file():
        return local.resolve(), "repo"
    fallback = main_worktree_config_path(repo)
    if fallback is not None:
        return fallback.resolve(), "main_worktree"
    return None, "default"


def main_worktree_config_path(repo: Path) -> Path | None:
    main_worktree = git_main_worktree_path(repo)
    if main_worktree is None:
        return None
    main_worktree = main_worktree.resolve()
    if main_worktree == repo:
        return None
    candidate = main_worktree / CONFIG_FILE_NAME
    if candidate.is_file():
        return candidate
    return None


def git_main_worktree_path(repo: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repo), "worktree", "list", "--porcelain"),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_main_worktree_path(completed.stdout)


def parse_main_worktree_path(porcelain: str) -> Path | None:
    for line in porcelain.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line.removeprefix("worktree ").strip()
        if path:
            return Path(path)
        return None
    return None


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected TOML table")
    return payload


def parse_agent(data: object) -> AgentConfig:
    table = expect_table(data, "agent")
    detected = detect_agent_clis()
    agent_kind = optional_nonempty_string(table.get("kind")) or "auto"
    if agent_kind not in AGENT_KIND_VALUES:
        allowed = ", ".join(AGENT_KIND_VALUES)
        raise ValueError(f"agent.kind must be one of: {allowed}")
    agent_kind_source = "explicit" if "kind" in table else "default:auto"
    prompt_dialect_setting = optional_nonempty_string(table.get("prompt_dialect"))
    if (
        prompt_dialect_setting is not None
        and prompt_dialect_setting not in AGENT_PROMPT_DIALECTS
    ):
        allowed = ", ".join(AGENT_PROMPT_DIALECTS)
        raise ValueError(f"agent.prompt_dialect must be one of: {allowed}")
    skill_ref_prefix_setting = optional_nonempty_string(table.get("skill_ref_prefix"))
    if (
        skill_ref_prefix_setting is not None
        and skill_ref_prefix_setting not in AGENT_SKILL_REF_DIALECT
    ):
        allowed = ", ".join(sorted(AGENT_SKILL_REF_DIALECT))
        raise ValueError(f"agent.skill_ref_prefix must be one of: {allowed}")
    if (
        prompt_dialect_setting is not None
        and skill_ref_prefix_setting is not None
        and AGENT_SKILL_REF_PREFIX[prompt_dialect_setting] != skill_ref_prefix_setting
    ):
        raise ValueError("agent.prompt_dialect and agent.skill_ref_prefix disagree")
    if agent_kind in AGENT_PROMPT_DIALECTS:
        expected_prefix = AGENT_SKILL_REF_PREFIX[agent_kind]
        if prompt_dialect_setting is not None and prompt_dialect_setting != agent_kind:
            raise ValueError("agent.kind and agent.prompt_dialect disagree")
        if (
            skill_ref_prefix_setting is not None
            and skill_ref_prefix_setting != expected_prefix
        ):
            raise ValueError("agent.kind and agent.skill_ref_prefix disagree")
    configured_command = optional_nonempty_string(table.get("command"))
    configured_selection = optional_nonempty_string(table.get("selection_command"))
    command, command_source, executable_kind = resolve_agent_command(
        "command",
        configured_command,
        agent_kind,
        detected,
    )
    selection_command, selection_command_source, _ = resolve_agent_command(
        "selection_command",
        configured_selection,
        agent_kind,
        detected,
    )
    prompt_resolution = resolve_agent_prompt_dialect(
        agent_kind,
        command,
        command_source,
        prompt_dialect_setting,
        skill_ref_prefix_setting,
    )
    return AgentConfig(
        command=command,
        selection_command=selection_command,
        command_source=command_source,
        selection_command_source=selection_command_source,
        detected=detected,
        forward_stderr=optional_bool(
            table.get("forward_stderr"), False, "agent.forward_stderr"
        ),
        agent_kind=agent_kind,
        agent_kind_source=agent_kind_source,
        executable_kind=executable_kind,
        prompt_dialect=prompt_resolution.prompt_dialect,
        prompt_dialect_source=prompt_resolution.prompt_dialect_source,
        skill_ref_prefix=prompt_resolution.skill_ref_prefix,
        skill_ref_prefix_source=prompt_resolution.skill_ref_prefix_source,
        compatibility_diagnostics=prompt_resolution.diagnostics,
    )


def detect_agent_clis(path: str | None = None) -> AgentDetection:
    return AgentDetection(
        codex=shutil.which("codex", path=path),
        claude=shutil.which("claude", path=path),
    )


def resolve_agent_command(
    key: str,
    configured: str | None,
    agent_kind: str,
    detected: AgentDetection,
) -> tuple[str | None, str, str | None]:
    if configured is not None:
        return configured, "explicit", None
    if agent_kind == "custom":
        return None, f"unresolved:custom-{key}-required", "custom"
    if agent_kind in SUPPORTED_AGENT_CLIS:
        if detected.path_for(agent_kind):
            return (
                AGENT_COMMAND_DEFAULTS[agent_kind][key],
                f"agent.kind:{agent_kind}",
                agent_kind,
            )
        return None, f"unresolved:{agent_kind}-not-found", agent_kind
    available = detected.available
    if AGENT_PREFERRED_CLI in available:
        source = "auto:codex"
        if len(available) > 1:
            source = f"auto:codex:{AGENT_DEFAULT_POLICY_SOURCE}"
        return (
            AGENT_COMMAND_DEFAULTS[AGENT_PREFERRED_CLI][key],
            source,
            AGENT_PREFERRED_CLI,
        )
    if len(available) == 1:
        agent_name = available[0]
        return AGENT_COMMAND_DEFAULTS[agent_name][key], f"auto:{agent_name}", agent_name
    if not available:
        return None, "unresolved:no-supported-cli", None
    return None, "unresolved:multiple-supported-clis", None


def resolve_agent_prompt_dialect(
    agent_kind: str,
    command: str | None,
    command_source: str,
    prompt_dialect_setting: str | None,
    skill_ref_prefix_setting: str | None,
) -> AgentPromptDialectResolution:
    if agent_kind in AGENT_PROMPT_DIALECTS:
        return AgentPromptDialectResolution(
            prompt_dialect=agent_kind,
            prompt_dialect_source=f"agent.kind:{agent_kind}",
            skill_ref_prefix=AGENT_SKILL_REF_PREFIX[agent_kind],
            skill_ref_prefix_source=f"agent.kind:{agent_kind}",
        )

    explicit_prompt = explicit_prompt_dialect_resolution(
        prompt_dialect_setting,
        skill_ref_prefix_setting,
    )
    if explicit_prompt is not None:
        return explicit_prompt

    if agent_kind == "custom":
        return AgentPromptDialectResolution(
            prompt_dialect=None,
            prompt_dialect_source="unresolved:custom-missing-prompt-dialect",
            skill_ref_prefix=None,
            skill_ref_prefix_source="unresolved:custom-missing-skill-ref-prefix",
        )

    auto_kind = auto_prompt_dialect_from_command_source(command_source)
    if auto_kind is not None:
        return AgentPromptDialectResolution(
            prompt_dialect=auto_kind,
            prompt_dialect_source=command_source,
            skill_ref_prefix=AGENT_SKILL_REF_PREFIX[auto_kind],
            skill_ref_prefix_source=command_source,
        )

    if command is None:
        return AgentPromptDialectResolution(
            prompt_dialect=None,
            prompt_dialect_source="unresolved:no-worker-command",
            skill_ref_prefix=None,
            skill_ref_prefix_source="unresolved:no-worker-command",
        )

    inferred = infer_legacy_prompt_dialect(command)
    if inferred is not None:
        diagnostic = (
            "agent.kind is auto and agent.command is explicit; inferred "
            f"prompt dialect {inferred!r} from legacy command parsing. Set "
            "agent.kind or agent.prompt_dialect to make this explicit."
        )
        source = f"legacy-command-inference:{inferred}"
        return AgentPromptDialectResolution(
            prompt_dialect=inferred,
            prompt_dialect_source=source,
            skill_ref_prefix=AGENT_SKILL_REF_PREFIX[inferred],
            skill_ref_prefix_source=source,
            diagnostics=(diagnostic,),
        )

    diagnostic = (
        "agent.kind is auto and agent.command is explicit, but the prompt "
        "dialect could not be inferred; using the legacy Codex-style "
        "skill_ref_prefix '$'. Set agent.kind = 'custom' with "
        "agent.prompt_dialect or agent.skill_ref_prefix to make this explicit."
    )
    return AgentPromptDialectResolution(
        prompt_dialect="codex",
        prompt_dialect_source="legacy-default:codex",
        skill_ref_prefix="$",
        skill_ref_prefix_source="legacy-default:codex",
        diagnostics=(diagnostic,),
    )


def explicit_prompt_dialect_resolution(
    prompt_dialect_setting: str | None,
    skill_ref_prefix_setting: str | None,
) -> AgentPromptDialectResolution | None:
    if prompt_dialect_setting is not None:
        source = "explicit:agent.prompt_dialect"
        return AgentPromptDialectResolution(
            prompt_dialect=prompt_dialect_setting,
            prompt_dialect_source=source,
            skill_ref_prefix=AGENT_SKILL_REF_PREFIX[prompt_dialect_setting],
            skill_ref_prefix_source=source,
        )
    if skill_ref_prefix_setting is not None:
        source = "explicit:agent.skill_ref_prefix"
        return AgentPromptDialectResolution(
            prompt_dialect=AGENT_SKILL_REF_DIALECT[skill_ref_prefix_setting],
            prompt_dialect_source=source,
            skill_ref_prefix=skill_ref_prefix_setting,
            skill_ref_prefix_source=source,
        )
    return None


def auto_prompt_dialect_from_command_source(source: str) -> str | None:
    for agent_name in AGENT_PROMPT_DIALECTS:
        if source == f"auto:{agent_name}" or source.startswith(f"auto:{agent_name}:"):
            return agent_name
    return None


def infer_legacy_prompt_dialect(command: str) -> str | None:
    executable = legacy_command_executable(command)
    if executable is None:
        return None
    executable_name = Path(executable).name
    if executable_name in AGENT_PROMPT_DIALECTS:
        return executable_name
    return None


def legacy_command_executable(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    index = 0
    while index < len(parts) and shell_env_assignment(parts[index]):
        index += 1
    if index >= len(parts):
        return None
    if parts[index] != "env":
        return parts[index]
    index += 1
    while index < len(parts):
        token = parts[index]
        if token == "--":
            index += 1
            break
        if token == "-i" or token.startswith("-i") and token != "-":
            index += 1
            continue
        if token == "-u":
            index += 2
            continue
        if token.startswith("-u") and token != "-u":
            index += 1
            continue
        if shell_env_assignment(token):
            index += 1
            continue
        break
    if index >= len(parts):
        return None
    return parts[index]


def shell_env_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    if not separator or not name:
        return False
    return all(char == "_" or char.isalnum() for char in name) and not name[0].isdigit()


def unresolved_agent_command_message(
    setting: str,
    source: str,
    detected: AgentDetection,
) -> str:
    if source == "unresolved:multiple-supported-clis":
        available = ", ".join(detected.available)
        return (
            f"{setting} is not configured and multiple supported agent CLIs are "
            f"available on PATH ({available}); set {setting} in .vibe-loop.toml "
            "to choose the command explicitly."
        )
    if source.startswith("unresolved:custom-"):
        return (
            f"{setting} is not configured and agent.kind is custom; set "
            f"{setting} in .vibe-loop.toml."
        )
    for agent_name in SUPPORTED_AGENT_CLIS:
        if source == f"unresolved:{agent_name}-not-found":
            return (
                f"{setting} is not configured and agent.kind is {agent_name}, "
                f"but {agent_name} was not found on PATH; install {agent_name} "
                f"or set {setting} explicitly in .vibe-loop.toml."
            )
    return (
        f"{setting} is not configured and no supported agent CLI was found on "
        "PATH; install codex or claude, or set the command explicitly in "
        ".vibe-loop.toml."
    )


def unresolved_prompt_dialect_message(agent_kind: str, source: str) -> str:
    if source.startswith("unresolved:custom-"):
        return (
            "agent.kind is custom, so worker prompt construction requires "
            "agent.prompt_dialect or agent.skill_ref_prefix in .vibe-loop.toml."
        )
    return (
        "worker prompt dialect could not be resolved from agent configuration "
        f"(agent.kind={agent_kind}, source={source}); set agent.kind, "
        "agent.prompt_dialect, or agent.skill_ref_prefix."
    )


def parse_task_source(data: object) -> TaskSourceConfig:
    table = expect_table(data, "task_source")
    explicit_keys = frozenset(str(key) for key in table)
    profile = optional_profile(table.get("profile"))
    statuses = table.get("runnable_statuses")
    if statuses is None:
        runnable = profile_runnable_statuses(profile) or DEFAULT_RUNNABLE_STATUSES
    elif isinstance(statuses, list) and all(isinstance(item, str) for item in statuses):
        runnable = tuple(statuses)
    else:
        raise ValueError("task_source.runnable_statuses must be an array of strings")
    plan_paths = table.get("plan_paths")
    if plan_paths is None:
        candidate_paths = DEFAULT_PLAN_PATHS
    elif isinstance(plan_paths, list) and all(
        isinstance(item, str) for item in plan_paths
    ):
        candidate_paths = tuple(plan_paths)
    else:
        raise ValueError("task_source.plan_paths must be an array of strings")
    return TaskSourceConfig(
        type=str(table.get("type") or "markdown-plan"),
        plan_path=optional_string(table.get("plan_path")),
        plan_paths=candidate_paths,
        profile=profile,
        list_command=optional_string(table.get("list")),
        next_command=optional_string(table.get("next")),
        probe_command=optional_string(table.get("probe")),
        runnable_statuses=runnable,
        explicit_keys=explicit_keys,
    )


def optional_profile(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("task_source.profile must be a TOML table")
    return value


def profile_runnable_statuses(profile: dict[str, Any] | None) -> tuple[str, ...] | None:
    if profile is None:
        return None
    status_map = profile.get("status_map")
    if not isinstance(status_map, dict):
        return None
    runnable = status_map.get("runnable")
    if runnable is None:
        return None
    if (
        isinstance(runnable, list)
        and runnable
        and all(isinstance(item, str) for item in runnable)
    ):
        return tuple(runnable)
    raise ValueError(
        "task_source.profile.status_map.runnable must be an array of strings"
    )


def reject_generated_command_adapters(profile: object) -> None:
    if not isinstance(profile, dict):
        raise ValueError("generated task-source profile must be a JSON object")
    forbidden = sorted(find_forbidden_generated_command_keys(profile))
    if forbidden:
        fields = ", ".join(forbidden)
        raise ValueError(
            "generated task-source profiles cannot define executable command "
            f"adapters or lock backends: {fields}"
        )


def find_forbidden_generated_command_keys(
    value: object,
    path: str = "profile",
) -> set[str]:
    forbidden: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key in GENERATED_TASK_PROFILE_FORBIDDEN_KEYS:
                forbidden.add(child_path)
            if key == "type" and child == "command":
                forbidden.add(f"{child_path}=command")
            forbidden.update(find_forbidden_generated_command_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            forbidden.update(
                find_forbidden_generated_command_keys(child, f"{path}[{index}]")
            )
    return forbidden


def parse_completion(data: object, repo: Path) -> CompletionConfig:
    table = expect_table(data, "completion")
    commands = table.get("commands")
    if commands is None:
        return CompletionConfig(commands=default_completion_commands(repo))
    if isinstance(commands, list) and all(isinstance(item, str) for item in commands):
        return CompletionConfig(commands=tuple(commands))
    raise ValueError("completion.commands must be an array of strings")


def default_completion_commands(repo: Path) -> tuple[str, ...]:
    record = repo / "scripts" / "record_worklog.py"
    gantt = repo / "scripts" / "generate_gantt.py"
    if record.exists() and gantt.exists():
        return (
            "uv run python scripts/record_worklog.py --validate",
            "uv run python scripts/generate_gantt.py --coverage-check",
        )
    return ()


def parse_supervision(data: object) -> SupervisionConfig:
    table = expect_table(data, "supervision")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - SUPERVISION_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"supervision contains unsupported keys: {', '.join(unknown_keys)}"
        )
    return SupervisionConfig(
        max_restarts=nonnegative_int(
            table.get("max_restarts"),
            SUPERVISION_DEFAULT_MAX_RESTARTS,
            "supervision.max_restarts",
        ),
        cooldown_seconds=nonnegative_float(
            table.get("cooldown_seconds"),
            SUPERVISION_DEFAULT_COOLDOWN_SECONDS,
            "supervision.cooldown_seconds",
        ),
        explicit_keys=explicit_keys,
    )


def parse_locks(data: object) -> LockConfig:
    table = expect_table(data, "locks")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - LOCKS_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(f"locks contains unsupported keys: {', '.join(unknown_keys)}")
    lock_type = optional_nonempty_string(table.get("type")) or "directory"
    if lock_type not in LOCK_BACKEND_TYPES:
        allowed = ", ".join(LOCK_BACKEND_TYPES)
        raise ValueError(f"locks.type must be one of: {allowed}")
    commands = {
        key: optional_nonempty_string(table.get(key)) for key in LOCKS_COMMAND_KEYS
    }
    configured_command_keys = {
        key for key, value in commands.items() if value is not None
    }
    if lock_type == "directory" and configured_command_keys:
        keys = ", ".join(sorted(configured_command_keys))
        raise ValueError(
            f'locks command adapter keys require locks.type = "command": {keys}'
        )
    if lock_type == "command":
        missing = sorted(key for key, value in commands.items() if value is None)
        if missing:
            keys = ", ".join(f"locks.{key}" for key in missing)
            raise ValueError(f"locks.type command requires {keys}")
    return LockConfig(
        type=lock_type,
        acquire_command=commands["acquire_command"],
        release_command=commands["release_command"],
        status_command=commands["status_command"],
        list_command=commands["list_command"],
        lease_seconds=optional_positive_int(
            table.get("lease_seconds"),
            "locks.lease_seconds",
        ),
        explicit_keys=explicit_keys,
    )


def parse_planning_analytics(data: object) -> PlanningAnalyticsConfig:
    table = expect_table(data, "planning_analytics")
    explicit_keys = frozenset(str(key) for key in table)
    schedule_policy = (
        optional_nonempty_string(table.get("schedule_policy"))
        or PLANNING_ANALYTICS_DEFAULT_SCHEDULE_POLICY
    )
    if schedule_policy not in PLANNING_ANALYTICS_SCHEDULE_POLICIES:
        allowed = ", ".join(PLANNING_ANALYTICS_SCHEDULE_POLICIES)
        raise ValueError(
            f"planning_analytics.schedule_policy must be one of: {allowed}"
        )
    subject_matching = (
        optional_nonempty_string(table.get("subject_matching")) or "diagnostic"
    )
    if subject_matching not in PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES:
        allowed = ", ".join(PLANNING_ANALYTICS_SUBJECT_MATCHING_MODES)
        raise ValueError(
            f"planning_analytics.subject_matching must be one of: {allowed}"
        )
    return PlanningAnalyticsConfig(
        schedule_policy=schedule_policy,
        subject_matching=subject_matching,
        worklog_command=optional_nonempty_string(table.get("worklog_command")),
        outputs=parse_planning_analytics_outputs(table.get("outputs")),
        duration_model=parse_planning_analytics_duration_model(
            table.get("duration_model")
        ),
        explicit_keys=explicit_keys,
    )


def parse_planning_analytics_duration_model(
    data: object,
) -> PlanningAnalyticsDurationModelConfig:
    table = expect_table(data, "planning_analytics.duration_model")
    supported_keys = set(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL)
    unknown_keys = sorted(set(str(key) for key in table) - supported_keys)
    if unknown_keys:
        raise ValueError(
            "planning_analytics.duration_model contains unsupported keys: "
            f"{', '.join(unknown_keys)}"
        )
    name = (
        optional_nonempty_string(table.get("name"))
        or PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["name"]
    )
    if name not in PLANNING_ANALYTICS_DURATION_MODEL_NAMES:
        allowed = ", ".join(PLANNING_ANALYTICS_DURATION_MODEL_NAMES)
        raise ValueError(
            f"planning_analytics.duration_model.name must be one of: {allowed}"
        )
    return PlanningAnalyticsDurationModelConfig(
        name=str(name),
        group_min_sample_count=positive_int(
            table.get("group_min_sample_count"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["group_min_sample_count"]),
            "planning_analytics.duration_model.group_min_sample_count",
        ),
        similarity_min_score=bounded_float(
            table.get("similarity_min_score"),
            float(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_min_score"]),
            "planning_analytics.duration_model.similarity_min_score",
            minimum=0.0,
            maximum=1.0,
        ),
        similarity_max_examples=nonnegative_int(
            table.get("similarity_max_examples"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_max_examples"]),
            "planning_analytics.duration_model.similarity_max_examples",
        ),
        similarity_blend_weight=bounded_float(
            table.get("similarity_blend_weight"),
            float(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["similarity_blend_weight"]),
            "planning_analytics.duration_model.similarity_blend_weight",
            minimum=0.0,
            maximum=1.0,
        ),
        fallback_minutes=positive_int(
            table.get("fallback_minutes"),
            int(PLANNING_ANALYTICS_DEFAULT_DURATION_MODEL["fallback_minutes"]),
            "planning_analytics.duration_model.fallback_minutes",
        ),
    )


def parse_planning_analytics_outputs(data: object) -> PlanningAnalyticsOutputs:
    table = expect_table(data, "planning_analytics.outputs")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(set(explicit_keys) - set(PLANNING_ANALYTICS_DEFAULT_OUTPUTS))
    if unknown_keys:
        raise ValueError(
            "planning_analytics.outputs contains unsupported keys: "
            f"{', '.join(unknown_keys)}"
        )
    return PlanningAnalyticsOutputs(
        timeline_json=optional_repo_relative_path(
            table.get("timeline_json"),
            "planning_analytics.outputs.timeline_json",
        ),
        gantt_html=optional_repo_relative_path(
            table.get("gantt_html"),
            "planning_analytics.outputs.gantt_html",
        ),
        benchmark_json=optional_repo_relative_path(
            table.get("benchmark_json"),
            "planning_analytics.outputs.benchmark_json",
        ),
        benchmark_markdown=optional_repo_relative_path(
            table.get("benchmark_markdown"),
            "planning_analytics.outputs.benchmark_markdown",
        ),
        explicit_keys=explicit_keys,
    )


def parse_specs(data: object) -> SpecDiagnosticsConfig:
    table = expect_table(data, "specs")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - SPEC_DIAGNOSTICS_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(f"specs contains unsupported keys: {', '.join(unknown_keys)}")
    return SpecDiagnosticsConfig(
        require_approved=optional_bool(
            table.get("require_approved"), False, "specs.require_approved"
        ),
        require_current_fingerprints=optional_bool(
            table.get("require_current_fingerprints"),
            False,
            "specs.require_current_fingerprints",
        ),
        require_requirement_coverage=optional_bool(
            table.get("require_requirement_coverage"),
            False,
            "specs.require_requirement_coverage",
        ),
        require_completion_evidence=optional_bool(
            table.get("require_completion_evidence"),
            False,
            "specs.require_completion_evidence",
        ),
        approved_states=nonempty_string_tuple(
            table.get("approved_states"),
            SPEC_DIAGNOSTICS_DEFAULT_APPROVED_STATES,
            "specs.approved_states",
        ),
        override_commands=nonempty_string_tuple(
            table.get("override_commands"),
            (),
            "specs.override_commands",
            allow_empty=True,
        ),
        explicit_keys=explicit_keys,
    )


def planning_analytics_report(
    config: VibeConfig,
    task_source_runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    diagnostics: list[str] = []
    status = "ready"
    if task_source_runtime is not None and not task_source_runtime.get("usable"):
        status = "task_source_unusable"
        diagnostics.append("planning analytics requires a usable task source")
    if config.planning_analytics.worklog_command is None:
        diagnostics.append(
            "project worklog adapter is not configured; authoritative evidence "
            "will come from task source state, run reports, explicit commit "
            "mapping, and bounded git metadata"
        )
    return {
        "status": status,
        "schedule_policy": config.planning_analytics.schedule_policy,
        "schedule_policy_source": (
            "explicit"
            if config.planning_analytics.is_explicit("schedule_policy")
            else "default"
        ),
        "subject_matching": config.planning_analytics.subject_matching,
        "subject_matching_source": (
            "explicit"
            if config.planning_analytics.is_explicit("subject_matching")
            else "default"
        ),
        "worklog_adapter": {
            "configured": config.planning_analytics.worklog_command is not None,
            "source": (
                "explicit"
                if config.planning_analytics.worklog_command is not None
                else "none"
            ),
        },
        "duration_model": config.planning_analytics.duration_model.to_json(),
        "coverage": {
            "authoritative_evidence": [
                "task source completion state",
                "worker reports with explicit task ids",
                "project worklog adapter records",
                "explicit commit refs, Plan-Item trailers, or Requirement trailers",
                "worker report metadata requirement_ids and plan_items",
            ],
            "diagnostic_only": [
                "subject matching",
                "branch names",
                "raw run log text",
            ],
        },
        "outputs": planning_analytics_output_report(config),
        "repo_artifact_outputs_enabled": (
            config.planning_analytics.outputs.has_explicit_paths
        ),
        "diagnostics": diagnostics,
    }


def planning_analytics_output_report(config: VibeConfig) -> dict[str, object]:
    outputs = config.planning_analytics.outputs
    report: dict[str, object] = {}
    for key, default_name in PLANNING_ANALYTICS_DEFAULT_OUTPUTS.items():
        explicit_path = getattr(outputs, key)
        if explicit_path is None:
            path = config.planning_analytics_state_path / default_name
            source = "default_state_dir"
        else:
            path = config.repo / explicit_path
            source = "explicit"
        report[key] = {
            "path": str(path),
            "source": source,
        }
    return report


def expect_table(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_nonempty_string(value: object) -> str | None:
    text = optional_string(value)
    if not text:
        return None
    return text


def optional_repo_relative_path(value: object, name: str) -> str | None:
    text = optional_nonempty_string(value)
    if text is None:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a repo-relative path")
    return text


def optional_bool(value: object, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def nonempty_string_tuple(
    value: object,
    default: tuple[str, ...],
    name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{name} must be an array of non-empty strings")
    if not value and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    return tuple(item.strip() for item in value)


def positive_int(value: object, default: int, name: str) -> int:
    parsed = optional_int(value, default, name)
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    parsed = optional_int(value, 0, name)
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def nonnegative_int(value: object, default: int, name: str) -> int:
    parsed = optional_int(value, default, name)
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def nonnegative_float(value: object, default: float, name: str) -> float:
    if value is None:
        parsed = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    else:
        parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if parsed < 0.0:
        raise ValueError(f"{name} must be a non-negative number")
    return parsed


def optional_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def bounded_float(
    value: object,
    default: float,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if value is None:
        parsed = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    else:
        parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
