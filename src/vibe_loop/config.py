from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import math
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
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
        # Python 3.12+ shutil.which on Windows matches only PATHEXT
        # extensions, so an explicit path to a .py script resolves to None;
        # route it through the interpreter instead of the cmd.exe fallback
        # (whose .py association runs detached from the captured pipes).
        if parts[0].lower().endswith(".py") and Path(parts[0]).is_file():
            return [sys.executable, *parts], False
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
RUNTIME_CONTEXT_REDACTION = "<runtime-context-redacted>"
REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES = 16
REGISTRY_RUNTIME_CONTEXT_MAX_VALUE_BYTES = 4096
REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES = 16 * 1024
REGISTRY_RUNTIME_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REGISTRY_RUNTIME_CONTEXT_FORBIDDEN_NAMES = frozenset(
    {
        "BASH_ENV",
        "CDPATH",
        "CLASSPATH",
        "ENV",
        "GCONV_PATH",
        "GEM_HOME",
        "GEM_PATH",
        "GLOBIGNORE",
        "IFS",
        "JAVA_TOOL_OPTIONS",
        "NODE_OPTIONS",
        "PATH",
        "PERL5LIB",
        "PERL5OPT",
        "PROMPT_COMMAND",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SHELLOPTS",
        "ZDOTDIR",
        "_JAVA_OPTIONS",
    }
)
REGISTRY_RUNTIME_CONTEXT_FORBIDDEN_PREFIXES = ("DYLD_", "LD_", "VIBE_LOOP_")
REGISTRY_RUNTIME_CONTEXT_SELECTOR_SUFFIXES = frozenset(
    {
        "BOARD",
        "CONTEXT",
        "INSTANCE",
        "NAMESPACE",
        "ORG",
        "ORGANIZATION",
        "PROJECT",
        "PROJECT_ID",
        "PROJECT_KEY",
        "REPO",
        "REPOSITORY",
        "SELECTOR",
        "SITE",
        "TEAM",
        "TENANT",
        "WORKSPACE",
    }
)
REGISTRY_RUNTIME_CONTEXT_SECRET_NAME_TOKENS = frozenset(
    {
        "APIKEY",
        "AUTH",
        "BEARER",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "DSN",
        "PASSWD",
        "PASSWORD",
        "PRIVATE",
        "SECRET",
        "TOKEN",
    }
)
REGISTRY_RUNTIME_CONTEXT_SECRET_VALUE_PREFIXES = (
    "ghp_",
    "github_pat_",
    "sk-",
    "xoxb-",
    "xoxp-",
)
SUPERVISION_DEFAULT_MAX_RESTARTS = 3
SUPERVISION_DEFAULT_COOLDOWN_SECONDS = 30.0
SUPERVISION_DEFAULT_RECOVER_UNKNOWN_RUNS = True
SUPERVISION_DEFAULT_RESUME_UNKNOWN_RUNS = True
SUPERVISION_DEFAULT_LIMIT_WALL_DETECTION = True
SUPERVISION_DEFAULT_LIMIT_WALL_BACKOFF_SECONDS = 1800.0
# Wall-clock bound on a single worker's agent run. When the key is absent it
# defaults to this 3-hour cap; a hung worker is force-killed at the deadline and
# its task returns to runnable, so one stuck worker cannot freeze the whole
# batch/cycle. Only an explicit `worker_timeout_seconds = 0` restores the
# historical unbounded behavior.
SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS = 10800.0
SUPERVISION_DEFAULT_SLICE_TOKEN_THRESHOLD = 100000
SUPERVISION_DEFAULT_CROSS_RUN_ATTEMPT_THRESHOLD = 3
SUPERVISION_CONFIG_KEYS = frozenset(
    {
        "max_restarts",
        "cooldown_seconds",
        "recover_unknown_runs",
        "resume_unknown_runs",
        "limit_wall_detection",
        "limit_wall_backoff_seconds",
        "limit_wall_patterns",
        "worker_timeout_seconds",
        "slice_token_threshold",
        "cross_run_attempt_threshold",
    }
)
LOCK_BACKEND_TYPES = ("directory", "command")
LOCKS_COMMAND_KEYS = frozenset(
    {"acquire_command", "release_command", "status_command", "list_command"}
)
LOCKS_CONFIG_KEYS = frozenset({"type", "lease_seconds"}) | LOCKS_COMMAND_KEYS
PROJECT_BINDING_CONFIG_KEYS = frozenset({"require", "context"})
PROJECT_BINDING_SOURCE_CONFIG = "config"
PROJECT_BINDING_SOURCE_RUNTIME_CONTEXT = "runtime_context"
PROJECT_BINDING_REASON_UNSET = "unset"
PROJECT_BINDING_REASON_AMBIENT_ONLY = "ambient_only"
PROJECT_BINDING_REASON_CONFLICT = "conflict"
AUTOPILOT_COMMAND_KEYS = frozenset(
    {
        "health_command",
        "summary_command",
        "troubleshoot_command",
        "planning_command",
        "idle_wake_command",
    }
)
AUTOPILOT_WORKTREE_DISPOSITION_POLICIES = ("report-only", "reap")
AUTOPILOT_CONFIG_KEYS = (
    frozenset(
        {
            "jobs",
            "interval_seconds",
            "min_ready",
            "require_clean_repo",
            "planning_recheck_seconds",
            "idle_poll_max_seconds",
            "planning_backoff_seconds",
            "planning_max_launches_per_day",
            "planning_unproductive_threshold",
            "worktree_disposition",
            "disk_reserve",
        }
    )
    | AUTOPILOT_COMMAND_KEYS
)
DISK_RESERVE_CONFIG_KEYS = frozenset(
    {
        "min_free_bytes",
        "min_free_fraction",
        "min_free_inodes",
        "min_free_inode_fraction",
    }
)
# Native disk-health floors (the reviewed AUTO-15 defaults). A target is a
# genuine capacity blocker only when BOTH the absolute and the proportional
# floor of an axis are exhausted. These are the single source of truth for the
# defaults; autopilot.DiskHealthThresholds aliases them.
DISK_RESERVE_DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024
DISK_RESERVE_DEFAULT_MIN_FREE_FRACTION = 0.02
DISK_RESERVE_DEFAULT_MIN_FREE_INODES = 10_000
DISK_RESERVE_DEFAULT_MIN_FREE_INODE_FRACTION = 0.02
# Six hours between planning attempts once planning stops producing actionable
# work, capped at four launches a rolling day: an analysis plus authoring pass
# costs real provider spend, and repeating it on the ordinary supervisor
# interval burns that budget without moving the board.
AUTOPILOT_DEFAULT_PLANNING_BACKOFF_SECONDS = 21600.0
AUTOPILOT_DEFAULT_PLANNING_MAX_LAUNCHES_PER_DAY = 4
AUTOPILOT_DEFAULT_PLANNING_UNPRODUCTIVE_THRESHOLD = 2
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
TASK_SOURCE_SOURCE_KEYS = frozenset(
    {
        "type",
        "plan_path",
        "plan_paths",
        "list",
        "next",
        "probe",
        "activate",
        "complete",
        "reset",
        "park",
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
        "activate",
        "complete",
        "reset",
        "park",
        "selection_command",
        "locks",
        "lock_backend",
        "acquire_command",
        "release_command",
        "status_command",
        "list_command",
        "autopilot",
        "health_command",
        "summary_command",
        "troubleshoot_command",
        "planning_command",
        "idle_wake_command",
        "analysis_command",
        "orchestration",
        "reviewer_profile",
        "gates",
        "verify_on_main",
        "max_initial_review_passes",
        "max_closure_review_passes",
        "reviewer_concurrency_budget",
        "max_remediation_rounds",
        "integration_enabled",
        "task_provenance_mode",
    }
)

ORCHESTRATION_MODES = ("worker-owned", "runtime-owned")
DEFAULT_ORCHESTRATION_MODE = "runtime-owned"
ORCHESTRATION_TASK_PROVENANCE_MODES = ("external-confirmed", "adapter")
ORCHESTRATION_CONFIG_KEYS = frozenset(
    {
        "mode",
        "reviewer_profile",
        "gates",
        "verify_on_main",
        "max_initial_review_passes",
        "max_closure_review_passes",
        "reviewer_concurrency_budget",
        "max_remediation_rounds",
        "integration_enabled",
        "task_provenance_mode",
    }
)
ORCHESTRATION_COMMAND_REF_RE = re.compile(r"^completion\.commands\[(\d+)]$")

AGENT_KIND_VALUES = ("auto", "codex", "claude", "custom")
AGENT_PROMPT_DIALECTS = ("codex", "claude")
AGENT_EFFORT_VALUES = frozenset({"minimal", "low", "medium", "high", "xhigh"})
AGENT_PROVIDER_EFFORT_VALUES = {
    "codex": AGENT_EFFORT_VALUES,
    "claude": frozenset({"low", "medium", "high"}),
}
AGENT_ROUTING_PREDICATE_KEYS = frozenset(
    {
        "match_hazards_any",
        "match_paths_glob",
        "match_task_id_regex",
        "match_title_regex",
        "match_priority",
    }
)
AGENT_ROUTING_RULE_KEYS = frozenset({"profile"}) | AGENT_ROUTING_PREDICATE_KEYS
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
        "analysis_command": "codex exec --sandbox read-only {prompt}",
    },
    "claude": {
        "command": "claude -p {prompt}",
        "selection_command": "claude -p {prompt}",
        "analysis_command": (
            "claude -p {prompt} --disallowedTools Edit Write NotebookEdit"
        ),
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
    analysis_command: str | None = None
    model: str | None = None
    effort: str | None = None
    command_source: str = "unresolved:no-supported-cli"
    selection_command_source: str = "unresolved:no-supported-cli"
    analysis_command_source: str = "unresolved:no-supported-cli"
    model_source: str = "default:none"
    effort_source: str = "default:none"
    detected: AgentDetection = dataclasses.field(default_factory=AgentDetection)
    forward_stderr: bool = False
    agent_kind: str = "auto"
    agent_kind_source: str = "default:auto"
    executable_kind: str | None = None
    profile_name: str = ""
    prompt_dialect: str | None = "codex"
    prompt_dialect_source: str = "legacy-default:codex"
    skill_ref_prefix: str | None = "$"
    skill_ref_prefix_source: str = "legacy-default:codex"
    compatibility_diagnostics: tuple[str, ...] = ()

    def require_command(self) -> str:
        self.require_effort_delivery("command")
        if self.command:
            return self.command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.command",
                self.command_source,
                self.detected,
            )
        )

    def require_effort_delivery(self, key: str) -> None:
        diagnostic = self.effort_delivery_diagnostic(key)
        if diagnostic:
            raise AgentResolutionError(diagnostic)

    def effort_delivery_diagnostic(self, key: str) -> str:
        if self.effort is None:
            return ""
        command = getattr(self, key)
        if command is None:
            return ""
        command_source = getattr(self, f"{key}_source")
        provider = agent_command_provider(
            command,
            self.executable_kind or self.agent_kind,
        )
        if provider in AGENT_PROVIDER_EFFORT_VALUES:
            allowed = AGENT_PROVIDER_EFFORT_VALUES[provider]
            if self.effort not in allowed:
                return (
                    f"agent.effort {self.effort!r} is not supported by {provider}; "
                    f"allowed values: {', '.join(sorted(allowed))}"
                )
        if command_source != "explicit":
            return ""
        setting = (
            f"agent.profiles.{self.profile_name}." if self.profile_name else "agent."
        )
        if command_embeds_native_effort(command):
            return (
                f"{setting}{key} already embeds provider-specific effort while "
                f"{setting}effort is set; remove the embedded flag and use "
                "{effort}, or unset the first-class setting."
            )
        if not command_template_uses_field(command, "effort"):
            return (
                f"{setting}{key} is explicit and cannot receive {setting}effort; "
                "add a validated {effort} placeholder or unset agent.effort."
            )
        return ""

    def require_selection_command(self) -> str:
        self.require_effort_delivery("selection_command")
        if self.selection_command:
            return self.selection_command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.selection_command",
                self.selection_command_source,
                self.detected,
            )
        )

    def require_analysis_command(self) -> str:
        self.require_effort_delivery("analysis_command")
        if self.analysis_command:
            return self.analysis_command
        raise AgentResolutionError(
            unresolved_agent_command_message(
                "agent.analysis_command",
                self.analysis_command_source,
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
        for key in ("command", "selection_command", "analysis_command"):
            diagnostic = self.effort_delivery_diagnostic(key)
            if diagnostic:
                messages.append(diagnostic)
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
            "analysis_command_configured": self.analysis_command is not None,
            "analysis_command_source": self.analysis_command_source,
            "model": self.model,
            "model_source": self.model_source,
            "effort": self.effort,
            "effort_source": self.effort_source,
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
class AgentRoutingRule:
    """One ordered `[[agent.routing]]` rule mapping matching tasks to a profile.

    A rule matches a task when every predicate it *specifies* matches (AND
    within a rule); ordering across rules provides OR. Predicates left at their
    empty/None default are simply not evaluated, so a rule with only `profile`
    is an unconditional catch-all. Matching reads task attributes by name so it
    stays independent of the tasks module (no import cycle).
    """

    profile: str
    match_hazards_any: tuple[str, ...] = ()
    match_paths_glob: tuple[str, ...] = ()
    match_task_id_regex: str | None = None
    match_title_regex: str | None = None
    match_priority: str | None = None

    def matches(self, task: Any) -> bool:
        if self.match_hazards_any:
            hazards = set(getattr(task, "hazards", ()) or ())
            if hazards.isdisjoint(self.match_hazards_any):
                return False
        if self.match_paths_glob:
            paths = tuple(getattr(task, "paths", ()) or ())
            if not any(
                fnmatch.fnmatch(path, pattern)
                for pattern in self.match_paths_glob
                for path in paths
            ):
                return False
        if self.match_task_id_regex is not None:
            if not re.search(self.match_task_id_regex, getattr(task, "task_id", "")):
                return False
        if self.match_title_regex is not None:
            if not re.search(self.match_title_regex, getattr(task, "title", "")):
                return False
        if self.match_priority is not None:
            priority = getattr(task, "priority", "") or ""
            if priority.casefold() != self.match_priority.casefold():
                return False
        return True

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"profile": self.profile}
        if self.match_hazards_any:
            payload["match_hazards_any"] = list(self.match_hazards_any)
        if self.match_paths_glob:
            payload["match_paths_glob"] = list(self.match_paths_glob)
        if self.match_task_id_regex is not None:
            payload["match_task_id_regex"] = self.match_task_id_regex
        if self.match_title_regex is not None:
            payload["match_title_regex"] = self.match_title_regex
        if self.match_priority is not None:
            payload["match_priority"] = self.match_priority
        return payload


@dataclasses.dataclass(frozen=True)
class AgentSelection:
    """The agent profile resolved for one task at dispatch time.

    `profile` is the empty string for the default `[agent]`, otherwise the named
    `[agent.profiles.<name>]` chosen by an explicit task field or a routing rule.
    `source` records how the profile was selected for provenance.
    """

    config: AgentConfig
    profile: str
    source: str


@dataclasses.dataclass(frozen=True)
class TaskSourceConfig:
    type: str = "markdown-plan"
    plan_path: str | None = None
    plan_paths: tuple[str, ...] = DEFAULT_PLAN_PATHS
    profile: dict[str, Any] | None = None
    list_command: str | None = None
    next_command: str | None = None
    probe_command: str | None = None
    # Required for command-backed worker execution. The adapter transitions the
    # selected task from a runnable state to a project-owned in-progress state
    # and returns the normalized post-transition task JSON for confirmation.
    activate_command: str | None = None
    # Optional runtime-owned completion adapter. The command performs the
    # project-owned terminal transition and returns the normalized task JSON
    # that confirms it.
    complete_command: str | None = None
    # Optional operator wiring: a command that asks a command-backed task
    # backend to return a claimed task to its runnable state, templated with
    # {task_id}. The supervisor invokes it when a run hits a provider limit
    # wall, because activation moved the task to an in-progress status before
    # worker launch and the worker died before any terminal transition. Absent
    # hook leaves project-owned task status unchanged.
    reset_command: str | None = None
    # Optional runtime-owned terminal-failure adapter. It moves an activated
    # task into the source's held state and returns normalized task JSON for
    # confirmation. When absent, settlement falls back to reset/requeue.
    park_command: str | None = None
    # Wall-clock ceiling applied to every task-source subprocess invocation
    # (list at cycle start, activate before launch, probe during
    # classification/recovery, and the reset hook). A hung backend command — a
    # stalled loopyard CLI, a blocked Postgres query — would otherwise freeze
    # the supervisor synchronously, because these calls are made inline on the
    # dispatch/status path. Expiry raises subprocess.TimeoutExpired, a
    # SubprocessError that behaves like any other command failure at each call
    # site. See tasks.run_json_command.
    command_timeout_seconds: float = 120.0
    runnable_statuses: tuple[str, ...] = DEFAULT_RUNNABLE_STATUSES
    # Opt-in: when true, the task source's emitted order is authoritative and
    # the priority band is dropped from the dispatch sort key (see
    # tasks.task_sort_key). Default false keeps the historical
    # (status, priority, order) ordering for every deployment that does not set
    # it — markdown/spec sources are untouched.
    respect_source_order: bool = False
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
            "activate_command": self.activate_command,
            "complete_command": self.complete_command,
            "reset_command": self.reset_command,
            "park_command": self.park_command,
            "command_timeout_seconds": self.command_timeout_seconds,
            "runnable_statuses": list(self.runnable_statuses),
            "respect_source_order": self.respect_source_order,
            "explicit_keys": sorted(self.explicit_keys),
            "explicit_source_keys": list(self.explicit_source_keys),
            "allows_generated_cache": self.allows_generated_cache,
        }


@dataclasses.dataclass(frozen=True)
class CompletionConfig:
    commands: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class OrchestrationConfig:
    mode: str = DEFAULT_ORCHESTRATION_MODE
    reviewer_profile: str | None = None
    gates: tuple[str, ...] = ()
    verify_on_main: tuple[str, ...] = ()
    max_initial_review_passes: int = 1
    max_closure_review_passes: int = 2
    reviewer_concurrency_budget: int = 1
    max_remediation_rounds: int = 2
    integration_enabled: bool = True
    task_provenance_mode: str = "external-confirmed"
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reviewer_profile": self.reviewer_profile,
            "gates": list(self.gates),
            "verify_on_main": list(self.verify_on_main),
            "max_initial_review_passes": self.max_initial_review_passes,
            "max_closure_review_passes": self.max_closure_review_passes,
            "reviewer_concurrency_budget": self.reviewer_concurrency_budget,
            "max_remediation_rounds": self.max_remediation_rounds,
            "integration_enabled": self.integration_enabled,
            "task_provenance_mode": self.task_provenance_mode,
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class SupervisionConfig:
    max_restarts: int = SUPERVISION_DEFAULT_MAX_RESTARTS
    cooldown_seconds: float = SUPERVISION_DEFAULT_COOLDOWN_SECONDS
    recover_unknown_runs: bool = SUPERVISION_DEFAULT_RECOVER_UNKNOWN_RUNS
    resume_unknown_runs: bool = SUPERVISION_DEFAULT_RESUME_UNKNOWN_RUNS
    limit_wall_detection: bool = SUPERVISION_DEFAULT_LIMIT_WALL_DETECTION
    limit_wall_backoff_seconds: float = SUPERVISION_DEFAULT_LIMIT_WALL_BACKOFF_SECONDS
    # Empty means "use the runner's built-in DEFAULT_LIMIT_WALL_PATTERNS"; a
    # non-empty tuple fully overrides that default list.
    limit_wall_patterns: tuple[str, ...] = ()
    # 0.0 means unbounded (historical behavior); a positive value caps a single
    # worker's wall-clock runtime before its process group is force-killed.
    worker_timeout_seconds: float = SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS
    slice_token_threshold: int = SUPERVISION_DEFAULT_SLICE_TOKEN_THRESHOLD
    cross_run_attempt_threshold: int = SUPERVISION_DEFAULT_CROSS_RUN_ATTEMPT_THRESHOLD
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "max_restarts": self.max_restarts,
            "cooldown_seconds": self.cooldown_seconds,
            "recover_unknown_runs": self.recover_unknown_runs,
            "resume_unknown_runs": self.resume_unknown_runs,
            "limit_wall_detection": self.limit_wall_detection,
            "limit_wall_backoff_seconds": self.limit_wall_backoff_seconds,
            "limit_wall_patterns": list(self.limit_wall_patterns),
            "worker_timeout_seconds": self.worker_timeout_seconds,
            "slice_token_threshold": self.slice_token_threshold,
            "cross_run_attempt_threshold": self.cross_run_attempt_threshold,
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class DiskReserveConfig:
    """Per-project overrides for the native disk-health capacity floors.

    Each field is ``None`` when unset, so the native AUTO-15 default applies and
    a configuration-free project keeps its reviewed behavior. The disk-health
    check blocks a target only when *both* the absolute and the proportional
    floor of an axis are exhausted, so pairing a positive reserve on one axis
    with a zero reserve on the other can never block; that combination is
    rejected as contradictory during validation.
    """

    min_free_bytes: int | None = None
    min_free_fraction: float | None = None
    min_free_inodes: int | None = None
    min_free_inode_fraction: float | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    @property
    def effective_min_free_bytes(self) -> int:
        if self.min_free_bytes is None:
            return DISK_RESERVE_DEFAULT_MIN_FREE_BYTES
        return self.min_free_bytes

    @property
    def effective_min_free_fraction(self) -> float:
        if self.min_free_fraction is None:
            return DISK_RESERVE_DEFAULT_MIN_FREE_FRACTION
        return self.min_free_fraction

    @property
    def effective_min_free_inodes(self) -> int:
        if self.min_free_inodes is None:
            return DISK_RESERVE_DEFAULT_MIN_FREE_INODES
        return self.min_free_inodes

    @property
    def effective_min_free_inode_fraction(self) -> float:
        if self.min_free_inode_fraction is None:
            return DISK_RESERVE_DEFAULT_MIN_FREE_INODE_FRACTION
        return self.min_free_inode_fraction

    def to_json(self) -> dict[str, object]:
        return {
            "min_free_bytes": self.min_free_bytes,
            "min_free_fraction": self.min_free_fraction,
            "min_free_inodes": self.min_free_inodes,
            "min_free_inode_fraction": self.min_free_inode_fraction,
            # Effective floors actually enforced by the cycle: the configured
            # override, or the native default when unset. Doctor/status show
            # these so an operator sees the values in force, not just overrides.
            "effective": {
                "min_free_bytes": self.effective_min_free_bytes,
                "min_free_fraction": self.effective_min_free_fraction,
                "min_free_inodes": self.effective_min_free_inodes,
                "min_free_inode_fraction": self.effective_min_free_inode_fraction,
            },
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class AutopilotConfig:
    jobs: int | None = None
    interval_seconds: float | None = None
    min_ready: int | None = None
    require_clean_repo: bool = True
    planning_recheck_seconds: float = 60.0
    idle_poll_max_seconds: float = 600.0
    planning_backoff_seconds: float = AUTOPILOT_DEFAULT_PLANNING_BACKOFF_SECONDS
    planning_max_launches_per_day: int = AUTOPILOT_DEFAULT_PLANNING_MAX_LAUNCHES_PER_DAY
    planning_unproductive_threshold: int = (
        AUTOPILOT_DEFAULT_PLANNING_UNPRODUCTIVE_THRESHOLD
    )
    worktree_disposition: str = "report-only"
    health_command: str | None = None
    summary_command: str | None = None
    troubleshoot_command: str | None = None
    planning_command: str | None = None
    idle_wake_command: str | None = None
    disk_reserve: DiskReserveConfig = dataclasses.field(
        default_factory=DiskReserveConfig
    )
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def maintenance_command(self, kind: str) -> str | None:
        return {
            "health": self.health_command,
            "summary": self.summary_command,
            "troubleshoot": self.troubleshoot_command,
            "planning": self.planning_command,
        }.get(kind)

    def to_json(self) -> dict[str, object]:
        return {
            "jobs": self.jobs,
            "interval_seconds": self.interval_seconds,
            "min_ready": self.min_ready,
            "require_clean_repo": self.require_clean_repo,
            "planning_recheck_seconds": self.planning_recheck_seconds,
            "idle_poll_max_seconds": self.idle_poll_max_seconds,
            "planning_backoff_seconds": self.planning_backoff_seconds,
            "planning_max_launches_per_day": self.planning_max_launches_per_day,
            "planning_unproductive_threshold": self.planning_unproductive_threshold,
            "worktree_disposition": self.worktree_disposition,
            "health_command": self.health_command,
            "summary_command": self.summary_command,
            "troubleshoot_command": self.troubleshoot_command,
            "planning_command": self.planning_command,
            "idle_wake_command": self.idle_wake_command,
            "disk_reserve": self.disk_reserve.to_json(),
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
class ProjectBindingConfig:
    """Declared per-repository namespace binding for command adapters.

    ``require`` names the selector variables that command-backed task sources
    and locks must receive from an explicit source. ``context`` pins their
    values in repository configuration.
    """

    require: tuple[str, ...] = ()
    context: tuple[tuple[str, str], ...] = ()
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    @property
    def declared(self) -> bool:
        return bool(self.require or self.context)

    def to_json(self) -> dict[str, object]:
        return {
            "declared": self.declared,
            "require": list(self.require),
            "context_names": [name for name, _value in self.context],
        }


@dataclasses.dataclass(frozen=True)
class ResolvedBindingEntry:
    name: str
    value: str
    source: str

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "value": (
                self.value
                if registry_runtime_context_name_is_selector(self.name.upper())
                else RUNTIME_CONTEXT_REDACTION
            ),
        }


@dataclasses.dataclass(frozen=True)
class ProjectBindingDiagnostic:
    name: str
    reason: str

    @property
    def code(self) -> str:
        return f"project_binding_{self.reason}:{self.name}"

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "reason": self.reason, "code": self.code}


@dataclasses.dataclass(frozen=True)
class ResolvedProjectBinding:
    declared: bool = False
    entries: tuple[ResolvedBindingEntry, ...] = ()
    diagnostics: tuple[ProjectBindingDiagnostic, ...] = ()
    injected_names: tuple[str, ...] = ()

    @property
    def blocker(self) -> str | None:
        return self.diagnostics[0].code if self.diagnostics else None

    def to_json(self) -> dict[str, object]:
        return {
            "declared": self.declared,
            "resolved": [entry.to_json() for entry in self.entries],
            "diagnostics": [item.to_json() for item in self.diagnostics],
            # Every name handed to adapter subprocesses, not just the required
            # ones: an unrequired selector still influences routing, so the
            # report would overstate its authority by hiding it.
            "injected_names": list(self.injected_names),
        }


class ProjectBindingError(ValueError):
    def __init__(self, binding: ResolvedProjectBinding) -> None:
        super().__init__(
            "command backend project binding is unresolved: "
            + ", ".join(item.code for item in binding.diagnostics)
        )
        self.binding = binding


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
    agent_profiles: dict[str, AgentConfig] = dataclasses.field(default_factory=dict)
    agent_routing: tuple[AgentRoutingRule, ...] = ()
    task_source: TaskSourceConfig = dataclasses.field(default_factory=TaskSourceConfig)
    completion: CompletionConfig = dataclasses.field(default_factory=CompletionConfig)
    orchestration: OrchestrationConfig = dataclasses.field(
        default_factory=OrchestrationConfig
    )
    supervision: SupervisionConfig = dataclasses.field(
        default_factory=SupervisionConfig
    )
    locks: LockConfig = dataclasses.field(default_factory=LockConfig)
    project_binding: ProjectBindingConfig = dataclasses.field(
        default_factory=ProjectBindingConfig
    )
    autopilot: AutopilotConfig = dataclasses.field(default_factory=AutopilotConfig)
    specs: SpecDiagnosticsConfig = dataclasses.field(
        default_factory=SpecDiagnosticsConfig
    )
    config_path: Path | None = None
    config_source: str = "default"
    config_digest: str = ""
    worker_prompt_extra: str | None = None
    runtime_context: tuple[tuple[str, str], ...] = ()

    @property
    def state_path(self) -> Path:
        return self.repo / self.state_dir

    @property
    def generated_task_profile_path(self) -> Path:
        return self.state_path / GENERATED_TASK_PROFILE_CACHE_FILE

    @property
    def runtime_environment(self) -> dict[str, str]:
        # Registry-supplied context wins over repository pins; a disagreement
        # between the two is refused separately by resolve_project_binding.
        environment = dict(self.project_binding.context)
        environment.update(self.runtime_context)
        return environment

    def config_report(self) -> dict[str, object]:
        return {
            "source": self.config_source,
            "path": str(self.config_path) if self.config_path else None,
        }


def load_config(
    repo: Path,
    *,
    runtime_context: object = None,
) -> VibeConfig:
    repo = repo.resolve()
    config_path, config_source = resolve_config_file(repo)
    if config_path is not None:
        data, config_digest = read_config_file_snapshot(config_path)
    else:
        data = {}
        config_digest = ""
    task_source = parse_task_source(data.get("task_source", {}))
    completion = parse_completion(data.get("completion", {}), repo)
    agent_table = expect_table(data.get("agent", {}), "agent")
    agent = parse_agent(agent_table)
    agent_profiles = parse_agent_profiles(agent_table)
    agent_routing = parse_agent_routing(agent_table, agent_profiles)
    orchestration = parse_orchestration(
        data.get("orchestration", {}),
        completion=completion,
        agent_profiles=agent_profiles,
    )
    supervision = parse_supervision(data.get("supervision", {}))
    locks = parse_locks(data.get("locks", {}))
    project_binding = parse_project_binding(data.get("project_binding", {}))
    autopilot = parse_autopilot(data.get("autopilot", {}))
    specs = parse_specs(data.get("specs", {}))
    normalized_runtime_context = normalize_registry_runtime_context(runtime_context)
    validate_required_project_binding_values(
        project_binding.require,
        normalized_runtime_context,
        source="registry entry context",
    )
    return VibeConfig(
        repo=repo,
        config_path=config_path,
        config_source=config_source,
        config_digest=config_digest,
        main_branch=str(data.get("main_branch") or "main"),
        state_dir=str(data.get("state_dir") or ".vibe-loop"),
        worker_prompt_extra=optional_text(
            agent_table.get("worker_prompt_extra"),
            "agent.worker_prompt_extra",
        ),
        agent=agent,
        agent_profiles=agent_profiles,
        agent_routing=agent_routing,
        task_source=task_source,
        completion=completion,
        orchestration=orchestration,
        supervision=supervision,
        locks=locks,
        project_binding=project_binding,
        autopilot=autopilot,
        specs=specs,
        runtime_context=normalized_runtime_context,
    )


def normalize_registry_runtime_context(
    value: object,
) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("registry entry context must be an object")
    return normalize_registry_runtime_context_assignments(value.items())


def normalize_registry_runtime_context_assignments(
    value: object,
) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("registry entry context assignments must be pairs")
    try:
        raw_entries = iter(value)
    except TypeError as exc:
        raise ValueError("registry entry context assignments must be pairs") from exc

    entries: list[tuple[str, str]] = []
    normalized_names: set[str] = set()
    total_bytes = 0
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, (tuple, list)) or len(raw_entry) != 2:
            raise ValueError("registry entry context assignments must be pairs")
        name, context_value = raw_entry
        if len(entries) >= REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES:
            raise ValueError(
                "registry entry context has too many entries "
                f"(maximum {REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES})"
            )
        if not isinstance(name, str):
            raise ValueError("registry entry context names must be strings")
        if not REGISTRY_RUNTIME_CONTEXT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"registry entry context name {name!r} is not a valid "
                "environment variable name"
            )
        normalized_name = name.upper()
        if normalized_name in normalized_names:
            raise ValueError(
                f"registry entry context name {name!r} is duplicated case-insensitively"
            )
        if registry_runtime_context_name_is_dangerous(normalized_name):
            raise ValueError(f"registry entry context name {name!r} is prohibited")
        if not registry_runtime_context_name_is_selector(normalized_name):
            suffixes = ", ".join(
                f"_{suffix}"
                for suffix in sorted(REGISTRY_RUNTIME_CONTEXT_SELECTOR_SUFFIXES)
            )
            raise ValueError(
                f"registry entry context name {name!r} is not selector-shaped; "
                f"use a selector suffix such as {suffixes}"
            )
        if not isinstance(context_value, str):
            raise ValueError(
                f"registry entry context value for {name!r} must be a string"
            )
        if "\0" in context_value:
            raise ValueError(
                f"registry entry context value for {name!r} contains a null byte"
            )
        value_bytes = len(context_value.encode("utf-8"))
        if value_bytes > REGISTRY_RUNTIME_CONTEXT_MAX_VALUE_BYTES:
            raise ValueError(
                f"registry entry context value for {name!r} is too large "
                f"(maximum {REGISTRY_RUNTIME_CONTEXT_MAX_VALUE_BYTES} bytes)"
            )
        if (
            context_value.strip()
            .lower()
            .startswith(REGISTRY_RUNTIME_CONTEXT_SECRET_VALUE_PREFIXES)
        ):
            raise ValueError(
                f"registry entry context value for {name!r} looks secret-like"
            )
        total_bytes += len(name.encode("utf-8")) + value_bytes
        if total_bytes > REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES:
            raise ValueError(
                "registry entry context is too large "
                f"(maximum {REGISTRY_RUNTIME_CONTEXT_MAX_TOTAL_BYTES} bytes)"
            )
        normalized_names.add(normalized_name)
        entries.append((name, context_value))
    return tuple(sorted(entries))


def registry_runtime_context_name_is_dangerous(normalized_name: str) -> bool:
    if normalized_name in REGISTRY_RUNTIME_CONTEXT_FORBIDDEN_NAMES:
        return True
    if normalized_name.startswith(REGISTRY_RUNTIME_CONTEXT_FORBIDDEN_PREFIXES):
        return True
    tokens = frozenset(part for part in normalized_name.split("_") if part)
    if tokens & REGISTRY_RUNTIME_CONTEXT_SECRET_NAME_TOKENS:
        return True
    if "API_KEY" in normalized_name or "PRIVATE_KEY" in normalized_name:
        return True
    return False


def registry_runtime_context_name_is_selector(normalized_name: str) -> bool:
    return any(
        normalized_name == suffix or normalized_name.endswith(f"_{suffix}")
        for suffix in REGISTRY_RUNTIME_CONTEXT_SELECTOR_SUFFIXES
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
    return read_config_file_snapshot(path)[0]


def read_config_file_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    content = path.read_bytes()
    payload = tomllib.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected TOML table")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return payload, digest


def parse_agent(data: object) -> AgentConfig:
    table = expect_table(data, "agent")
    detected = detect_agent_clis()
    model = optional_nonempty_string(table.get("model"))
    model_source = "explicit" if model is not None else "default:none"
    effort = parse_agent_effort(table.get("effort"), "agent.effort")
    effort_source = "explicit" if effort is not None else "default:none"
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
    configured_analysis = optional_nonempty_string(table.get("analysis_command"))
    command, command_source, executable_kind = resolve_agent_command(
        "command",
        configured_command,
        agent_kind,
        detected,
        model,
        effort,
    )
    selection_command, selection_command_source, _ = resolve_agent_command(
        "selection_command",
        configured_selection,
        agent_kind,
        detected,
        model,
        effort,
    )
    analysis_command, analysis_command_source, _ = resolve_agent_command(
        "analysis_command",
        configured_analysis,
        agent_kind,
        detected,
        model,
        effort,
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
        analysis_command=analysis_command,
        model=model,
        effort=effort,
        command_source=command_source,
        selection_command_source=selection_command_source,
        analysis_command_source=analysis_command_source,
        model_source=model_source,
        effort_source=effort_source,
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


def parse_agent_profiles(table: dict[str, Any]) -> dict[str, AgentConfig]:
    raw = table.get("profiles")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("agent.profiles must be a table of named profile tables")
    profiles: dict[str, AgentConfig] = {}
    for raw_name, profile_table in raw.items():
        name = str(raw_name)
        label = f"agent.profiles.{name}"
        if not isinstance(profile_table, dict):
            raise ValueError(f"{label} must be a table")
        try:
            # Each profile is a full [agent]-shaped table, so it resolves through
            # the same command/kind/prompt-dialect machinery as the default.
            profiles[name] = dataclasses.replace(
                parse_agent(profile_table), profile_name=name
            )
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc
    return profiles


def parse_agent_routing(
    table: dict[str, Any],
    profiles: dict[str, AgentConfig],
) -> tuple[AgentRoutingRule, ...]:
    raw = table.get("routing")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("agent.routing must be an array of routing tables")
    rules: list[AgentRoutingRule] = []
    for index, entry in enumerate(raw):
        label = f"agent.routing[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a table")
        keys = frozenset(str(key) for key in entry)
        unknown = sorted(keys - AGENT_ROUTING_RULE_KEYS)
        if unknown:
            raise ValueError(f"{label} contains unsupported keys: {', '.join(unknown)}")
        profile = optional_nonempty_string(entry.get("profile"))
        if profile is None:
            raise ValueError(f"{label}.profile is required")
        if profile not in profiles:
            available = ", ".join(sorted(profiles)) or "none"
            raise ValueError(
                f"{label}.profile {profile!r} is not defined in [agent.profiles] "
                f"(defined: {available})"
            )
        rules.append(
            AgentRoutingRule(
                profile=profile,
                match_hazards_any=nonempty_string_tuple(
                    entry.get("match_hazards_any"),
                    (),
                    f"{label}.match_hazards_any",
                    allow_empty=True,
                ),
                match_paths_glob=nonempty_string_tuple(
                    entry.get("match_paths_glob"),
                    (),
                    f"{label}.match_paths_glob",
                    allow_empty=True,
                ),
                match_task_id_regex=routing_regex(
                    entry.get("match_task_id_regex"),
                    f"{label}.match_task_id_regex",
                ),
                match_title_regex=routing_regex(
                    entry.get("match_title_regex"),
                    f"{label}.match_title_regex",
                ),
                match_priority=optional_nonempty_string(entry.get("match_priority")),
            )
        )
    return tuple(rules)


def routing_regex(value: object, name: str) -> str | None:
    text = optional_nonempty_string(value)
    if text is None:
        return None
    try:
        re.compile(text)
    except re.error as exc:
        raise ValueError(f"{name} is not a valid regex ({text!r}): {exc}") from exc
    return text


def resolve_task_agent_profile(
    task: Any,
    routing: tuple[AgentRoutingRule, ...],
) -> tuple[str, str]:
    """Select a profile name for a task from routing rules (pure).

    Returns `(profile_name, source)` where an empty name means the default
    `[agent]`. An explicit task `agent` field wins over all routing rules; among
    routing rules the first match wins.
    """
    explicit = (getattr(task, "agent", "") or "").strip()
    if explicit:
        return explicit, "task.agent"
    for index, rule in enumerate(routing):
        if rule.matches(task):
            return rule.profile, f"agent.routing[{index}]"
    return "", "default"


def resolve_task_agent(config: VibeConfig, task: Any) -> AgentSelection:
    """Resolve the AgentConfig a task should run under.

    Unknown profile names fail closed with AgentResolutionError rather than
    falling back to the default: routing a security task to a refusing agent is
    exactly the failure this feature prevents, so a typo must stop the run.
    """
    name, source = resolve_task_agent_profile(task, config.agent_routing)
    if not name:
        profile = config.agent
    else:
        profile = config.agent_profiles.get(name)
        if profile is None:
            available = ", ".join(sorted(config.agent_profiles)) or "none"
            task_id = getattr(task, "task_id", "") or ""
            raise AgentResolutionError(
                f"task {task_id!r} routes to agent profile {name!r} ({source}), "
                f"which is not defined in [agent.profiles] (defined: {available})."
            )
    task_model = (getattr(task, "model", "") or "").strip()
    if task_model:
        profile = dataclasses.replace(
            profile,
            model=task_model,
            model_source="task.model",
        )
        profile = apply_model_to_inferred_commands(profile, task_model)
    return AgentSelection(profile, name, source)


def apply_model_to_inferred_commands(
    config: AgentConfig,
    model: str,
) -> AgentConfig:
    agent_kind = config.executable_kind
    if agent_kind not in SUPPORTED_AGENT_CLIS:
        return config
    replacements: dict[str, str] = {}
    for key in ("command", "selection_command", "analysis_command"):
        source = getattr(config, f"{key}_source")
        if source != "explicit" and getattr(config, key) is not None:
            replacements[key] = default_agent_command(
                agent_kind, key, model, config.effort
            )
    if not replacements:
        return config
    return dataclasses.replace(config, **replacements)


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
    model: str | None,
    effort: str | None,
) -> tuple[str | None, str, str | None]:
    if configured is not None:
        return configured, "explicit", None
    if agent_kind == "custom":
        return None, f"unresolved:custom-{key}-required", "custom"
    if agent_kind in SUPPORTED_AGENT_CLIS:
        if detected.path_for(agent_kind):
            return (
                default_agent_command(agent_kind, key, model, effort),
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
            default_agent_command(AGENT_PREFERRED_CLI, key, model, effort),
            source,
            AGENT_PREFERRED_CLI,
        )
    if len(available) == 1:
        agent_name = available[0]
        return (
            default_agent_command(agent_name, key, model, effort),
            f"auto:{agent_name}",
            agent_name,
        )
    if not available:
        return None, "unresolved:no-supported-cli", None
    return None, "unresolved:multiple-supported-clis", None


def default_agent_command(
    agent_kind: str,
    key: str,
    model: str | None,
    effort: str | None = None,
) -> str:
    command = AGENT_COMMAND_DEFAULTS[agent_kind][key]
    if model is None:
        configured = command
    elif agent_kind == "codex":
        configured = command.replace("codex exec", "codex exec -m {model}", 1)
    else:
        configured = command.replace("claude -p", "claude -p --model {model}", 1)
    if effort is None:
        return configured
    if agent_kind == "codex":
        return configured.replace(
            "codex exec", "codex exec -c model_reasoning_effort={effort}", 1
        )
    return configured.replace("claude -p", "claude -p --effort {effort}", 1)


def format_agent_command(
    command_template: str,
    *,
    prompt: str,
    model: str | None,
    effort: str | None = None,
    task: Any | None = None,
    profile: str = "",
    **format_fields: str,
) -> str:
    if not model and command_template_uses_field(command_template, "model"):
        task_context = ""
        if task is not None:
            task_id = getattr(task, "task_id", "") or ""
            task_context = f"task {task_id!r} "
        profile_name = profile or "default"
        model_setting = f"agent.profiles.{profile}.model" if profile else "agent.model"
        raise AgentResolutionError(
            f"{task_context}agent profile {profile_name!r} command template "
            f"references {{model}}, but no model is resolved; set task.model "
            f"or {model_setting}."
        )
    if not effort and command_template_uses_field(command_template, "effort"):
        task_context = ""
        if task is not None:
            task_id = getattr(task, "task_id", "") or ""
            task_context = f"task {task_id!r} "
        profile_name = profile or "default"
        effort_setting = (
            f"agent.profiles.{profile}.effort" if profile else "agent.effort"
        )
        raise AgentResolutionError(
            f"{task_context}agent profile {profile_name!r} command template "
            f"references {{effort}}, but no effort is resolved; set {effort_setting}."
        )
    return command_template.format(
        prompt=shell_quote(prompt),
        model=shell_quote(model or ""),
        effort=shell_quote(effort or ""),
        **format_fields,
    )


def parse_agent_effort(value: object, setting: str) -> str | None:
    effort = optional_nonempty_string(value)
    if effort is None:
        return None
    normalized = effort.lower()
    if normalized not in AGENT_EFFORT_VALUES:
        allowed = ", ".join(sorted(AGENT_EFFORT_VALUES))
        raise ValueError(f"{setting} must be one of: {allowed}")
    return normalized


def agent_command_provider(command: str, fallback: str | None) -> str:
    # A recognizable explicit executable is authoritative: it outranks the
    # declared kind, so a Codex kind pointing at a Claude command is validated
    # against Claude. An identifiable-but-unknown executable fails closed to ""
    # rather than inventing the kind's provider identity. The kind fallback is
    # used only when the command carries no executable token to inspect.
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    for token in argv:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        executable = Path(token).name
        return executable if executable in AGENT_PROVIDER_EFFORT_VALUES else ""
    return fallback if fallback in AGENT_PROVIDER_EFFORT_VALUES else ""


def command_embeds_native_effort(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(argv):
        # A placeholder flag such as `--effort {effort}` does not embed a fixed
        # effort; keep scanning so a later fixed flag (e.g. `--effort low`) is
        # still detected instead of short-circuiting on the placeholder.
        if token in {"--effort", "--reasoning-effort"}:
            if index + 1 < len(argv) and "{effort}" not in argv[index + 1]:
                return True
            continue
        if token.startswith(("--effort=", "--reasoning-effort=")):
            if "{effort}" not in token.split("=", 1)[1]:
                return True
            continue
        if token in {"-c", "--config"} and index + 1 < len(argv):
            token = argv[index + 1]
        elif token.startswith(("-c=", "--config=")):
            token = token.split("=", 1)[1]
        else:
            continue
        key, separator, _value = token.partition("=")
        if (
            separator
            and "{effort}" not in _value
            and key.replace("-", "_")
            in {
                "model_reasoning_effort",
                "reasoning_effort",
            }
        ):
            return True
    return False


def command_template_uses_field(command_template: str, field: str) -> bool:
    for (
        _literal_text,
        field_name,
        _format_spec,
        _conversion,
    ) in string.Formatter().parse(command_template):
        if field_name == field:
            return True
    return False


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
    respect_source_order = table.get("respect_source_order", False)
    if not isinstance(respect_source_order, bool):
        raise ValueError("task_source.respect_source_order must be a boolean")
    return TaskSourceConfig(
        type=str(table.get("type") or "markdown-plan"),
        plan_path=optional_string(table.get("plan_path")),
        plan_paths=candidate_paths,
        profile=profile,
        list_command=optional_string(table.get("list")),
        next_command=optional_string(table.get("next")),
        probe_command=optional_string(table.get("probe")),
        activate_command=optional_string(table.get("activate")),
        complete_command=optional_string(table.get("complete")),
        reset_command=optional_string(table.get("reset")),
        park_command=optional_string(table.get("park")),
        command_timeout_seconds=positive_float(
            table.get("command_timeout_seconds"),
            120.0,
            "task_source.command_timeout_seconds",
            minimum=1.0,
        ),
        runnable_statuses=runnable,
        respect_source_order=respect_source_order,
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


def parse_orchestration(
    data: object,
    *,
    completion: CompletionConfig,
    agent_profiles: Mapping[str, AgentConfig],
) -> OrchestrationConfig:
    table = expect_table(data, "orchestration")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - ORCHESTRATION_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            "orchestration contains unsupported keys: " + ", ".join(unknown_keys)
        )

    mode = orchestration_enum_value(
        table,
        "mode",
        default=DEFAULT_ORCHESTRATION_MODE,
        allowed=ORCHESTRATION_MODES,
    )

    reviewer_profile = optional_nonempty_string(table.get("reviewer_profile"))
    if reviewer_profile is not None and reviewer_profile not in agent_profiles:
        raise ValueError(
            "orchestration.reviewer_profile must reference a configured "
            f"agent.profiles entry: {reviewer_profile}"
        )

    default_command_refs = tuple(
        f"completion.commands[{index}]" for index, _ in enumerate(completion.commands)
    )
    gates = nonempty_string_tuple(
        table.get("gates"),
        default_command_refs,
        "orchestration.gates",
        allow_empty=True,
    )
    verify_on_main = nonempty_string_tuple(
        table.get("verify_on_main"),
        default_command_refs,
        "orchestration.verify_on_main",
        allow_empty=True,
    )
    validate_orchestration_command_refs(
        gates,
        completion=completion,
        setting="orchestration.gates",
    )
    validate_orchestration_command_refs(
        verify_on_main,
        completion=completion,
        setting="orchestration.verify_on_main",
    )

    task_provenance_mode = orchestration_enum_value(
        table,
        "task_provenance_mode",
        default="external-confirmed",
        allowed=ORCHESTRATION_TASK_PROVENANCE_MODES,
    )

    return OrchestrationConfig(
        mode=mode,
        reviewer_profile=reviewer_profile,
        gates=gates,
        verify_on_main=verify_on_main,
        max_initial_review_passes=positive_int(
            table.get("max_initial_review_passes"),
            1,
            "orchestration.max_initial_review_passes",
        ),
        max_closure_review_passes=nonnegative_int(
            table.get("max_closure_review_passes"),
            2,
            "orchestration.max_closure_review_passes",
        ),
        reviewer_concurrency_budget=positive_int(
            table.get("reviewer_concurrency_budget"),
            1,
            "orchestration.reviewer_concurrency_budget",
        ),
        max_remediation_rounds=nonnegative_int(
            table.get("max_remediation_rounds"),
            2,
            "orchestration.max_remediation_rounds",
        ),
        integration_enabled=optional_bool(
            table.get("integration_enabled"),
            True,
            "orchestration.integration_enabled",
        ),
        task_provenance_mode=task_provenance_mode,
        explicit_keys=explicit_keys,
    )


def validate_orchestration_command_refs(
    refs: Sequence[str],
    *,
    completion: CompletionConfig,
    setting: str,
) -> None:
    for ref in refs:
        match = ORCHESTRATION_COMMAND_REF_RE.fullmatch(ref)
        if match is None:
            raise ValueError(
                f"{setting} entries must be allowlisted completion.commands[N] "
                f"references, not executable values: {ref!r}"
            )
        index = int(match.group(1))
        if index >= len(completion.commands):
            raise ValueError(
                f"{setting} references unconfigured command key {ref}; "
                f"completion.commands has {len(completion.commands)} entries"
            )


def orchestration_enum_value(
    table: Mapping[str, object],
    key: str,
    *,
    default: str,
    allowed: Sequence[str],
) -> str:
    value = table.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise ValueError(f"orchestration.{key} must be one of: " + ", ".join(allowed))
    if value not in allowed:
        raise ValueError(f"orchestration.{key} must be one of: " + ", ".join(allowed))
    return value


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
        recover_unknown_runs=optional_bool(
            table.get("recover_unknown_runs"),
            SUPERVISION_DEFAULT_RECOVER_UNKNOWN_RUNS,
            "supervision.recover_unknown_runs",
        ),
        resume_unknown_runs=optional_bool(
            table.get("resume_unknown_runs"),
            SUPERVISION_DEFAULT_RESUME_UNKNOWN_RUNS,
            "supervision.resume_unknown_runs",
        ),
        limit_wall_detection=optional_bool(
            table.get("limit_wall_detection"),
            SUPERVISION_DEFAULT_LIMIT_WALL_DETECTION,
            "supervision.limit_wall_detection",
        ),
        limit_wall_backoff_seconds=nonnegative_float(
            table.get("limit_wall_backoff_seconds"),
            SUPERVISION_DEFAULT_LIMIT_WALL_BACKOFF_SECONDS,
            "supervision.limit_wall_backoff_seconds",
        ),
        limit_wall_patterns=parse_limit_wall_patterns(table.get("limit_wall_patterns")),
        worker_timeout_seconds=nonnegative_float(
            table.get("worker_timeout_seconds"),
            SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS,
            "supervision.worker_timeout_seconds",
        ),
        slice_token_threshold=nonnegative_int(
            table.get("slice_token_threshold"),
            SUPERVISION_DEFAULT_SLICE_TOKEN_THRESHOLD,
            "supervision.slice_token_threshold",
        ),
        cross_run_attempt_threshold=positive_int(
            table.get("cross_run_attempt_threshold"),
            SUPERVISION_DEFAULT_CROSS_RUN_ATTEMPT_THRESHOLD,
            "supervision.cross_run_attempt_threshold",
        ),
        explicit_keys=explicit_keys,
    )


def parse_limit_wall_patterns(value: object) -> tuple[str, ...]:
    patterns = nonempty_string_tuple(
        value, (), "supervision.limit_wall_patterns", allow_empty=True
    )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                "supervision.limit_wall_patterns entry is not a valid regex "
                f"({pattern!r}): {exc}"
            ) from exc
    return patterns


def parse_autopilot(data: object) -> AutopilotConfig:
    table = expect_table(data, "autopilot")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - AUTOPILOT_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"autopilot contains unsupported keys: {', '.join(unknown_keys)}"
        )
    worktree_disposition = table.get("worktree_disposition", "report-only")
    if (
        not isinstance(worktree_disposition, str)
        or worktree_disposition not in AUTOPILOT_WORKTREE_DISPOSITION_POLICIES
    ):
        allowed = ", ".join(AUTOPILOT_WORKTREE_DISPOSITION_POLICIES)
        raise ValueError("autopilot.worktree_disposition must be one of: " + allowed)
    return AutopilotConfig(
        jobs=optional_positive_int(table.get("jobs"), "autopilot.jobs"),
        interval_seconds=optional_nonnegative_float(
            table.get("interval_seconds"),
            "autopilot.interval_seconds",
        ),
        min_ready=optional_positive_int(table.get("min_ready"), "autopilot.min_ready"),
        planning_recheck_seconds=positive_float(
            table.get("planning_recheck_seconds"),
            60.0,
            "autopilot.planning_recheck_seconds",
            minimum=5.0,
        ),
        idle_poll_max_seconds=positive_float(
            table.get("idle_poll_max_seconds"),
            600.0,
            "autopilot.idle_poll_max_seconds",
            minimum=5.0,
        ),
        planning_backoff_seconds=nonnegative_float(
            table.get("planning_backoff_seconds"),
            AUTOPILOT_DEFAULT_PLANNING_BACKOFF_SECONDS,
            "autopilot.planning_backoff_seconds",
        ),
        planning_max_launches_per_day=nonnegative_int(
            table.get("planning_max_launches_per_day"),
            AUTOPILOT_DEFAULT_PLANNING_MAX_LAUNCHES_PER_DAY,
            "autopilot.planning_max_launches_per_day",
        ),
        planning_unproductive_threshold=positive_int(
            table.get("planning_unproductive_threshold"),
            AUTOPILOT_DEFAULT_PLANNING_UNPRODUCTIVE_THRESHOLD,
            "autopilot.planning_unproductive_threshold",
        ),
        require_clean_repo=optional_bool(
            table.get("require_clean_repo"),
            True,
            "autopilot.require_clean_repo",
        ),
        worktree_disposition=worktree_disposition,
        health_command=optional_nonempty_string(table.get("health_command")),
        summary_command=optional_nonempty_string(table.get("summary_command")),
        troubleshoot_command=optional_nonempty_string(
            table.get("troubleshoot_command")
        ),
        planning_command=optional_nonempty_string(table.get("planning_command")),
        idle_wake_command=optional_nonempty_string(table.get("idle_wake_command")),
        disk_reserve=parse_disk_reserve(table.get("disk_reserve", {})),
        explicit_keys=explicit_keys,
    )


def parse_disk_reserve(data: object) -> DiskReserveConfig:
    table = expect_table(data, "autopilot.disk_reserve")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - DISK_RESERVE_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            "autopilot.disk_reserve contains unsupported keys: "
            + ", ".join(unknown_keys)
        )
    min_free_bytes = optional_nonnegative_int(
        table.get("min_free_bytes"), "autopilot.disk_reserve.min_free_bytes"
    )
    min_free_fraction = optional_fraction(
        table.get("min_free_fraction"), "autopilot.disk_reserve.min_free_fraction"
    )
    min_free_inodes = optional_nonnegative_int(
        table.get("min_free_inodes"), "autopilot.disk_reserve.min_free_inodes"
    )
    min_free_inode_fraction = optional_fraction(
        table.get("min_free_inode_fraction"),
        "autopilot.disk_reserve.min_free_inode_fraction",
    )
    reserve = DiskReserveConfig(
        min_free_bytes=min_free_bytes,
        min_free_fraction=min_free_fraction,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
        explicit_keys=explicit_keys,
    )
    reject_contradictory_reserve_pair(
        ("min_free_bytes", reserve.effective_min_free_bytes),
        ("min_free_fraction", reserve.effective_min_free_fraction),
    )
    reject_contradictory_reserve_pair(
        ("min_free_inodes", reserve.effective_min_free_inodes),
        ("min_free_inode_fraction", reserve.effective_min_free_inode_fraction),
    )
    return reserve


def reject_contradictory_reserve_pair(
    absolute: tuple[str, int | float],
    proportional: tuple[str, int | float],
) -> None:
    # A blocker fires only when both the absolute and the proportional floor of
    # an axis are exhausted, so a positive reserve paired with a zero reserve on
    # the same axis can never block. Validate the *effective* pair (override or
    # native default), so a lone explicit zero that silently disables an axis is
    # rejected while a fully zeroed (intentionally disabled) axis stays valid.
    (name_a, effective_a) = absolute
    (name_b, effective_b) = proportional
    if (effective_a == 0) != (effective_b == 0):
        raise ValueError(
            f"autopilot.disk_reserve.{name_a} and .{name_b} are contradictory: "
            "a positive reserve paired with a zero reserve can never block launch"
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


def parse_project_binding(data: object) -> ProjectBindingConfig:
    table = expect_table(data, "project_binding")
    explicit_keys = frozenset(str(key) for key in table)
    unknown_keys = sorted(explicit_keys - PROJECT_BINDING_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"project_binding contains unsupported keys: {', '.join(unknown_keys)}"
        )
    require = parse_project_binding_require(table.get("require"))
    try:
        context = normalize_registry_runtime_context(table.get("context"))
        validate_required_project_binding_values(
            require,
            context,
            source="project_binding.context",
        )
    except ValueError as exc:
        raise ValueError(f"project_binding.context is invalid: {exc}") from exc
    return ProjectBindingConfig(
        require=require,
        context=context,
        explicit_keys=explicit_keys,
    )


def validate_required_project_binding_values(
    require: Sequence[str],
    context: Sequence[tuple[str, str]],
    *,
    source: str,
) -> None:
    values = dict(context)
    for name in require:
        value = values.get(name)
        if value is not None and not value.strip():
            raise ValueError(f"{source} value for {name!r} must not be empty")


def parse_project_binding_require(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("project_binding.require must be a list of variable names")
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "project_binding.require entries must be non-empty strings"
            )
        name = item.strip()
        if not REGISTRY_RUNTIME_CONTEXT_NAME_RE.match(name):
            raise ValueError(
                f"project_binding.require name is not a valid variable: {name}"
            )
        normalized = name.upper()
        if registry_runtime_context_name_is_dangerous(normalized):
            raise ValueError(f"project_binding.require name is not allowed: {name}")
        if not registry_runtime_context_name_is_selector(normalized):
            allowed = ", ".join(sorted(REGISTRY_RUNTIME_CONTEXT_SELECTOR_SUFFIXES))
            raise ValueError(
                f"project_binding.require name must be a namespace selector "
                f"ending in one of: {allowed} (got {name})"
            )
        # Deduplicated verbatim, not by normalized case: environment variable
        # names are case-sensitive, so DEMO_PROJECT and Demo_Project are two
        # distinct selectors and each must be supplied on its own.
        if name in seen:
            raise ValueError(f"project_binding.require lists {name} more than once")
        seen.add(name)
        names.append(name)
    if len(names) > REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES:
        raise ValueError(
            "project_binding.require lists too many names "
            f"(maximum {REGISTRY_RUNTIME_CONTEXT_MAX_ENTRIES})"
        )
    return tuple(names)


def resolve_project_binding(
    config: VibeConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedProjectBinding:
    """Resolve declared namespace selectors from explicit sources only.

    A value inherited solely from the ambient process environment is refused:
    that is the routing ambiguity this binding exists to close.
    """

    binding = config.project_binding
    injected_names = tuple(sorted(config.runtime_environment))
    if not binding.require:
        return ResolvedProjectBinding(
            declared=binding.declared,
            injected_names=injected_names,
        )
    ambient = os.environ if environ is None else environ
    pinned = dict(binding.context)
    supplied = dict(config.runtime_context)
    entries: list[ResolvedBindingEntry] = []
    diagnostics: list[ProjectBindingDiagnostic] = []
    for name in binding.require:
        pinned_value = pinned.get(name)
        supplied_value = supplied.get(name)
        if (
            pinned_value is not None
            and supplied_value is not None
            and pinned_value != supplied_value
        ):
            diagnostics.append(
                ProjectBindingDiagnostic(name, PROJECT_BINDING_REASON_CONFLICT)
            )
            continue
        if supplied_value is not None:
            entries.append(
                ResolvedBindingEntry(
                    name,
                    supplied_value,
                    PROJECT_BINDING_SOURCE_RUNTIME_CONTEXT,
                )
            )
            continue
        if pinned_value is not None:
            entries.append(
                ResolvedBindingEntry(
                    name,
                    pinned_value,
                    PROJECT_BINDING_SOURCE_CONFIG,
                )
            )
            continue
        reason = (
            PROJECT_BINDING_REASON_AMBIENT_ONLY
            if ambient.get(name) is not None
            else PROJECT_BINDING_REASON_UNSET
        )
        diagnostics.append(ProjectBindingDiagnostic(name, reason))
    return ResolvedProjectBinding(
        declared=True,
        entries=tuple(entries),
        diagnostics=tuple(diagnostics),
        injected_names=injected_names,
    )


def require_project_binding(
    config: VibeConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedProjectBinding:
    binding = resolve_project_binding(config, environ=environ)
    if binding.diagnostics:
        raise ProjectBindingError(binding)
    return binding


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


def optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value:
        return None
    return value


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


def optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return nonnegative_int(value, 0, name)


def optional_fraction(value: object, name: str) -> float | None:
    if value is None:
        return None
    return bounded_float(value, 0.0, name, minimum=0.0, maximum=1.0)


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


def optional_nonnegative_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    return nonnegative_float(value, 0.0, name)


def positive_float(
    value: object, default: float, name: str, *, minimum: float = 0.0
) -> float:
    parsed = nonnegative_float(value, default, name)
    if minimum > 0.0 and parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum} seconds")
    if parsed <= 0.0:
        raise ValueError(f"{name} must be a positive number")
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
