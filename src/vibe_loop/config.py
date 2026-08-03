"""Configuration loading and project-binding resolution.

The ``resolve_project_binding`` and ``require_project_binding`` entry points
implement docs/prd/autopilot.md#prd-aut-020.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
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

from vibe_loop.telemetry import PHASES as USAGE_PHASES


def shell_quote(s: str) -> str:
    if sys.platform == "win32":
        return quote_windows_argv(s)
    return shlex.quote(s)


def quote_windows_argv(value: str) -> str:
    """Quote one value for CommandLineToArgvW, including trailing backslashes."""

    result = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
        else:
            result.append("\\" * backslashes)
            result.append(character)
        backslashes = 0
    result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


WINDOWS_CMD_UNSAFE_VALUE_CHARS = frozenset({'"', "%", "!", "\r", "\n", "\x00"})


def format_shell_command_template(
    command_template: str,
    values: Mapping[str, str],
    *,
    windows_shell_fields: Sequence[str] = (),
) -> str:
    """Substitute exact shell-quoted fields into an operator-authored template."""

    try:
        parsed = tuple(string.Formatter().parse(command_template))
    except ValueError as exc:
        raise ValueError("malformed shell command template") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in values:
            raise ValueError(f"unsupported shell command template field: {field_name}")
        if format_spec or conversion:
            raise ValueError(
                f"shell command template field {field_name!r} may not use "
                "conversion or formatting"
            )
    if sys.platform == "win32":
        for field_name in windows_shell_fields:
            value = values[field_name]
            if any(character in WINDOWS_CMD_UNSAFE_VALUE_CHARS for character in value):
                raise ValueError(
                    f"shell command template field {field_name!r} contains a "
                    "character that cmd.exe cannot safely interpolate"
                )
    quoted_values = {name: shell_quote(value) for name, value in values.items()}
    return command_template.format(**quoted_values)


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
SUPERVISION_DEFAULT_PROVIDER_LIMIT_DETECTION = True
SUPERVISION_DEFAULT_PROVIDER_LIMIT_BACKOFF_SECONDS = 1800.0
# Wall-clock bound on a single worker's agent run. When the key is absent it
# defaults to this 3-hour cap; a hung worker is force-killed at the deadline and
# its task returns to runnable, so one stuck worker cannot freeze the whole
# batch/cycle. Only an explicit `worker_timeout_seconds = 0` restores the
# historical unbounded behavior.
SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS = 10800.0
SUPERVISION_DEFAULT_SLICE_TOKEN_THRESHOLD = 100000
SUPERVISION_DEFAULT_CROSS_RUN_ATTEMPT_THRESHOLD = 3
SUPERVISION_PROVIDER_LIMIT_ALIASES = {
    "provider_limit_detection": "limit_wall_detection",
    "provider_limit_backoff_seconds": "limit_wall_backoff_seconds",
    "provider_limit_patterns": "limit_wall_patterns",
}
SUPERVISION_CONFIG_KEYS = frozenset(
    {
        "max_restarts",
        "cooldown_seconds",
        "recover_unknown_runs",
        "resume_unknown_runs",
        "provider_limit_detection",
        "provider_limit_backoff_seconds",
        "provider_limit_patterns",
        *SUPERVISION_PROVIDER_LIMIT_ALIASES.values(),
        "worker_timeout_seconds",
        "slice_token_threshold",
        "cross_run_attempt_threshold",
    }
)
TOP_LEVEL_CONFIG_KEYS = frozenset(
    {
        "main_branch",
        "state_dir",
        "agent",
        "task_source",
        "completion",
        "orchestration",
        "supervision",
        "locks",
        "project_binding",
        "autopilot",
        "specs",
        "budget",
    }
)
AGENT_PROFILE_CONFIG_KEYS = frozenset(
    {
        "command",
        "selection_command",
        "analysis_command",
        "model",
        "effort",
        "forward_stderr",
        "kind",
        "prompt_dialect",
        "skill_ref_prefix",
    }
)
AGENT_CONFIG_KEYS = AGENT_PROFILE_CONFIG_KEYS | frozenset(
    {"profiles", "routing", "worker_prompt_extra"}
)
TASK_SOURCE_CONFIG_KEYS = frozenset(
    {
        "type",
        "plan_path",
        "plan_paths",
        "list",
        "next",
        "probe",
        "activate",
        "health",
        "capabilities",
        "complete",
        "reset",
        "park",
        "profile",
        "command_timeout_seconds",
        "runnable_statuses",
        "respect_source_order",
    }
)
COMPLETION_CONFIG_KEYS = frozenset({"commands"})
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
PROJECT_BINDING_REASON_AMBIENT_CONFLICT = "ambient_conflict"
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
            "dispatch_min_ready",
            "require_clean_repo",
            "require_upstream_sync",
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
        "warn_free_bytes",
        "hard_stop_free_bytes",
        "min_free_bytes",
        "min_free_inodes",
        "min_free_inode_fraction",
    }
)
# Native disk-headroom thresholds. Byte thresholds are absolute because build
# capacity depends on bytes available, not the size of the backing filesystem.
DISK_RESERVE_DEFAULT_WARN_FREE_BYTES = 50 * 1024 * 1024 * 1024
DISK_RESERVE_DEFAULT_HARD_STOP_FREE_BYTES = 10 * 1024 * 1024 * 1024
DISK_RESERVE_DEFAULT_MIN_FREE_BYTES = DISK_RESERVE_DEFAULT_HARD_STOP_FREE_BYTES
DISK_RESERVE_DEFAULT_MIN_FREE_INODES = 10_000
DISK_RESERVE_DEFAULT_MIN_FREE_INODE_FRACTION = 0.02
# Six hours between planning attempts once planning stops producing actionable
# work, capped at four launches a rolling day: an analysis plus authoring pass
# costs real provider spend, and repeating it on the ordinary supervisor
# interval burns that budget without moving the board.
AUTOPILOT_DEFAULT_PLANNING_BACKOFF_SECONDS = 21600.0
AUTOPILOT_DEFAULT_PLANNING_MAX_LAUNCHES_PER_DAY = 4
AUTOPILOT_DEFAULT_PLANNING_UNPRODUCTIVE_THRESHOLD = 2
AUTOPILOT_MIN_INTERVAL_SECONDS = 60.0
BUDGET_METRICS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "non_cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cost_usd",
)
BUDGET_FAIL_SAFE_POLICIES = ("reserved", "fixed")
BUDGET_ON_INSUFFICIENT = ("block", "defer")
BUDGET_SELECTOR_KEYS = ("project", "provider", "phase", "model", "effort")
BUDGET_LIMIT_KEYS = frozenset(
    {*BUDGET_SELECTOR_KEYS, "limit", "warn_at", "window_hours"}
)
BUDGET_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "metric",
        "fail_safe",
        "fail_safe_amount",
        "default_declared",
        "on_insufficient",
        "declared",
        "limits",
    }
)
# Provider labels a budget selector may pin. Mirrors the usage-group providers
# in telemetry; a selector never sums tokens across two of them.
BUDGET_PROVIDERS = frozenset({"anthropic", "openai", "unknown"})
# Route/limit cardinality is bounded so replay, admission, and inspect work stay
# bounded; a configuration exceeding this is rejected rather than silently
# truncated.
BUDGET_MAX_LIMITS = 256
# Worker phases a reservation may be attributed to. Imported from telemetry so
# the budget vocabulary cannot drift from the usage-attribution vocabulary.
BUDGET_PHASES = USAGE_PHASES
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
        "capabilities",
        "capabilities_command",
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
        "reviewer_routing",
        "gates",
        "verify_on_main",
        "integration_lock_timeout_seconds",
        "main_push_timeout_seconds",
        "max_initial_review_passes",
        "max_closure_review_passes",
        "reviewer_concurrency_budget",
        "max_remediation_rounds",
        "max_candidate_reanchors",
        "integration_enabled",
        "push_main_to_upstream",
        "task_provenance_mode",
        "external_completion_actor",
    }
)

ORCHESTRATION_MODES = ("worker-owned", "runtime-owned")
DEFAULT_ORCHESTRATION_MODE = "runtime-owned"
ORCHESTRATION_TASK_PROVENANCE_MODES = ("external-confirmed", "adapter")
ORCHESTRATION_EXTERNAL_COMPLETION_ACTORS = (
    "worker",
    "operator",
    "external-system",
)
ORCHESTRATION_DEFAULT_INTEGRATION_LOCK_TIMEOUT_SECONDS = 900.0
ORCHESTRATION_DEFAULT_MAIN_PUSH_TIMEOUT_SECONDS = 300.0
ORCHESTRATION_CONFIG_KEYS = frozenset(
    {
        "mode",
        "reviewer_profile",
        "reviewer_routing",
        "gates",
        "verify_on_main",
        "integration_lock_timeout_seconds",
        "main_push_timeout_seconds",
        "max_initial_review_passes",
        "max_closure_review_passes",
        "reviewer_concurrency_budget",
        "max_remediation_rounds",
        "max_candidate_reanchors",
        "integration_enabled",
        "push_main_to_upstream",
        "task_provenance_mode",
        "external_completion_actor",
    }
)
REVIEWER_ROUTING_RULE_KEYS = frozenset(
    {
        "profile",
        "match_implementer_profile",
        "match_implementer_provider",
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


class TaskAgentResolutionError(AgentResolutionError):
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
class UsageObservationCapability:
    possible: bool
    provider: str
    output_format: str
    source: str
    diagnostic: str

    @property
    def unavailable_reason(self) -> str:
        return (
            "provider_usage_not_reported"
            if self.possible
            else "configured_command_cannot_report_usage"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "possible": self.possible,
            "provider": self.provider,
            "output_format": self.output_format,
            "source": self.source,
            "diagnostic": self.diagnostic,
        }


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

    def require_reviewer_command(self) -> str:
        """Resolve a reviewer command without silently dropping route settings."""
        if self.command and codex_review_exact_route_requested(self):
            setting = (
                f"agent.profiles.{self.profile_name}" if self.profile_name else "agent"
            )
            raise AgentResolutionError(
                f"{setting}.command is the exact 'codex review {{prompt}}' form, "
                f"which cannot safely receive {setting}.model or {setting}.effort: "
                "Codex project config cannot bind model_provider and the exact "
                "command exposes no supported effective-route metadata. Use a "
                "repository-permitted command with explicit {model}/{effort} "
                "delivery, or unset the first-class route settings."
            )
        if (
            self.command
            and self.command_source == "explicit"
            and self.model is not None
        ):
            setting = (
                f"agent.profiles.{self.profile_name}" if self.profile_name else "agent"
            )
            if command_embeds_native_model(self.command):
                raise AgentResolutionError(
                    f"{setting}.command already embeds a provider-specific model "
                    f"while {setting}.model is set; remove the embedded flag and "
                    "use {model}, or unset the first-class setting."
                )
            if not command_template_uses_field(self.command, "model"):
                raise AgentResolutionError(
                    f"{setting}.command cannot receive {setting}.model; add a "
                    "validated {model} placeholder or unset the first-class setting."
                )
        return self.require_command()

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
            "usage_observation": structured_usage_observation(
                self.command,
                self.agent_kind,
            ).to_json(),
            "diagnostics": self.diagnostics(),
        }


def codex_review_exact_route_requested(agent: AgentConfig) -> bool:
    if agent.command_source != "explicit" or not agent.command:
        return False
    try:
        argv = shlex.split(agent.command)
    except ValueError:
        return False
    return argv == ["codex", "review", "{prompt}"] and bool(agent.model or agent.effort)


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
class ReviewerRoutingRule:
    """Select a reviewer profile from the route that implemented a candidate."""

    profile: str
    match_implementer_profile: str | None = None
    match_implementer_provider: str | None = None

    def matches(self, *, implementer_profile: str, implementer_provider: str) -> bool:
        if (
            self.match_implementer_profile is not None
            and implementer_profile != self.match_implementer_profile
        ):
            return False
        if (
            self.match_implementer_provider is not None
            and implementer_provider != self.match_implementer_provider
        ):
            return False
        return True

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"profile": self.profile}
        if self.match_implementer_profile is not None:
            payload["match_implementer_profile"] = self.match_implementer_profile
        if self.match_implementer_provider is not None:
            payload["match_implementer_provider"] = self.match_implementer_provider
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
    # Optional backend health check. Unlike source-selection commands, this
    # does not select or replace the active source; it lets every repository
    # depending on an external backend verify that dependency independently.
    health_command: str | None = None
    # Optional deployment diagnostic for the configured task-source adapter.
    # It is intentionally not a source-selection key and is invoked only by
    # doctor when explicitly configured.
    capabilities_command: str | None = None
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
            "health_command": self.health_command,
            "capabilities_command": self.capabilities_command,
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
    reviewer_routing: tuple[ReviewerRoutingRule, ...] = ()
    gates: tuple[str, ...] = ()
    verify_on_main: tuple[str, ...] = ()
    integration_lock_timeout_seconds: float = (
        ORCHESTRATION_DEFAULT_INTEGRATION_LOCK_TIMEOUT_SECONDS
    )
    main_push_timeout_seconds: float = ORCHESTRATION_DEFAULT_MAIN_PUSH_TIMEOUT_SECONDS
    max_initial_review_passes: int = 1
    max_closure_review_passes: int = 2
    reviewer_concurrency_budget: int = 1
    max_remediation_rounds: int = 2
    max_candidate_reanchors: int = 2
    integration_enabled: bool = True
    push_main_to_upstream: bool = False
    task_provenance_mode: str = "external-confirmed"
    external_completion_actor: str | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def to_json(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reviewer_profile": self.reviewer_profile,
            "reviewer_routing": [rule.to_json() for rule in self.reviewer_routing],
            "gates": list(self.gates),
            "verify_on_main": list(self.verify_on_main),
            "integration_lock_timeout_seconds": (self.integration_lock_timeout_seconds),
            "main_push_timeout_seconds": self.main_push_timeout_seconds,
            "max_initial_review_passes": self.max_initial_review_passes,
            "max_closure_review_passes": self.max_closure_review_passes,
            "reviewer_concurrency_budget": self.reviewer_concurrency_budget,
            "max_remediation_rounds": self.max_remediation_rounds,
            "max_candidate_reanchors": self.max_candidate_reanchors,
            "integration_enabled": self.integration_enabled,
            "push_main_to_upstream": self.push_main_to_upstream,
            "task_provenance_mode": self.task_provenance_mode,
            "external_completion_actor": self.external_completion_actor,
            "explicit_keys": sorted(self.explicit_keys),
        }


@dataclasses.dataclass(frozen=True)
class SupervisionConfig:
    max_restarts: int = SUPERVISION_DEFAULT_MAX_RESTARTS
    cooldown_seconds: float = SUPERVISION_DEFAULT_COOLDOWN_SECONDS
    recover_unknown_runs: bool = SUPERVISION_DEFAULT_RECOVER_UNKNOWN_RUNS
    resume_unknown_runs: bool = SUPERVISION_DEFAULT_RESUME_UNKNOWN_RUNS
    provider_limit_detection: bool = SUPERVISION_DEFAULT_PROVIDER_LIMIT_DETECTION
    provider_limit_backoff_seconds: float = (
        SUPERVISION_DEFAULT_PROVIDER_LIMIT_BACKOFF_SECONDS
    )
    # Empty means "use the runner's built-in DEFAULT_PROVIDER_LIMIT_PATTERNS"; a
    # non-empty tuple fully overrides that default list.
    provider_limit_patterns: tuple[str, ...] = ()
    # 0.0 means unbounded (historical behavior); a positive value caps a single
    # worker's wall-clock runtime before its process group is force-killed.
    worker_timeout_seconds: float = SUPERVISION_DEFAULT_WORKER_TIMEOUT_SECONDS
    slice_token_threshold: int = SUPERVISION_DEFAULT_SLICE_TOKEN_THRESHOLD
    cross_run_attempt_threshold: int = SUPERVISION_DEFAULT_CROSS_RUN_ATTEMPT_THRESHOLD
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)
    compatibility_diagnostics: tuple[str, ...] = ()

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def diagnostics(self) -> list[str]:
        return list(self.compatibility_diagnostics)

    def to_json(self) -> dict[str, object]:
        return {
            "max_restarts": self.max_restarts,
            "cooldown_seconds": self.cooldown_seconds,
            "recover_unknown_runs": self.recover_unknown_runs,
            "resume_unknown_runs": self.resume_unknown_runs,
            "provider_limit_detection": self.provider_limit_detection,
            "provider_limit_backoff_seconds": self.provider_limit_backoff_seconds,
            "provider_limit_patterns": list(self.provider_limit_patterns),
            "worker_timeout_seconds": self.worker_timeout_seconds,
            "slice_token_threshold": self.slice_token_threshold,
            "cross_run_attempt_threshold": self.cross_run_attempt_threshold,
            "explicit_keys": sorted(self.explicit_keys),
            "diagnostics": self.diagnostics(),
        }


@dataclasses.dataclass(frozen=True)
class DiskReserveConfig:
    """Per-project overrides for the native disk-headroom thresholds.

    ``min_free_bytes`` remains a compatibility alias for
    ``hard_stop_free_bytes``. Inode pressure retains its absolute-and-
    proportional evaluation because inode capacity is not a build-size budget.
    """

    warn_free_bytes: int | None = None
    hard_stop_free_bytes: int | None = None
    min_free_bytes: int | None = None
    min_free_inodes: int | None = None
    min_free_inode_fraction: float | None = None
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    @property
    def effective_warn_free_bytes(self) -> int:
        if self.warn_free_bytes is not None:
            return self.warn_free_bytes
        return max(
            DISK_RESERVE_DEFAULT_WARN_FREE_BYTES,
            self.effective_hard_stop_free_bytes,
        )

    @property
    def effective_hard_stop_free_bytes(self) -> int:
        if self.hard_stop_free_bytes is not None:
            return self.hard_stop_free_bytes
        if self.min_free_bytes is not None:
            return self.min_free_bytes
        return DISK_RESERVE_DEFAULT_HARD_STOP_FREE_BYTES

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
            "warn_free_bytes": self.warn_free_bytes,
            "hard_stop_free_bytes": self.hard_stop_free_bytes,
            "min_free_bytes": self.min_free_bytes,
            "min_free_inodes": self.min_free_inodes,
            "min_free_inode_fraction": self.min_free_inode_fraction,
            # Effective floors actually enforced by the cycle: the configured
            # override, or the native default when unset. Doctor/status show
            # these so an operator sees the values in force, not just overrides.
            "effective": {
                "warn_free_bytes": self.effective_warn_free_bytes,
                "hard_stop_free_bytes": self.effective_hard_stop_free_bytes,
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
    dispatch_min_ready: int | None = None
    require_clean_repo: bool = True
    require_upstream_sync: bool = False
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
            "dispatch_min_ready": self.dispatch_min_ready,
            "require_clean_repo": self.require_clean_repo,
            "require_upstream_sync": self.require_upstream_sync,
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


def project_binding_guidance(binding: ResolvedProjectBinding) -> str:
    """Remedy text for a binding failure, or ``""`` when there is nothing to add.

    The remedy for an ambient conflict is not the remedy for the other reasons:
    the binding itself is fine, and what has to change is the caller's
    environment. Every surface that reports the diagnostic renders this, so the
    operator is not told a bare code on whichever path they happened to hit.
    """

    names = sorted(
        {
            item.name
            for item in binding.diagnostics
            if item.reason == PROJECT_BINDING_REASON_AMBIENT_CONFLICT
        }
    )
    if not names:
        return ""
    return (
        f"{', '.join(names)} names a different project than this repository is "
        "bound to; unset it, or point --repo at the repository that variable "
        "selects. The binding is pinned in this repository's "
        f"{CONFIG_FILE_NAME} or in its project-registry entry."
    )


class ProjectBindingError(ValueError):
    def __init__(self, binding: ResolvedProjectBinding) -> None:
        message = "command backend project binding is unresolved: " + ", ".join(
            item.code for item in binding.diagnostics
        )
        guidance = project_binding_guidance(binding)
        if guidance:
            message += f"; {guidance}"
        super().__init__(message)
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
class BudgetLimit:
    """One independent usage cap, optionally narrowed by selector fields.

    A ``None`` selector matches any value on that axis; a launch must satisfy
    *every* limit whose selectors it matches. ``window_hours`` of ``0`` means the
    cap is cumulative (all-time consumed), otherwise consumed usage is counted
    only inside the trailing window. Live (not-yet-reconciled) reservations
    always count regardless of the window, so a cap cannot be oversubscribed by
    in-flight launches.
    """

    limit: float
    project: str | None = None
    provider: str | None = None
    phase: str | None = None
    model: str | None = None
    effort: str | None = None
    warn_at: float | None = None
    window_hours: float = 0.0

    def selector(self) -> dict[str, str]:
        return {
            key: value
            for key in BUDGET_SELECTOR_KEYS
            if (value := getattr(self, key)) is not None
        }

    def matches(
        self,
        *,
        project: str,
        provider: str,
        phase: str,
        model: str,
        effort: str,
    ) -> bool:
        candidate = {
            "project": project,
            "provider": provider,
            "phase": phase,
            "model": model,
            "effort": effort,
        }
        return all(value == candidate[key] for key, value in self.selector().items())

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "limit": self.limit,
            "selector": self.selector(),
            "window_hours": self.window_hours,
        }
        if self.warn_at is not None:
            payload["warn_at"] = self.warn_at
        return payload


@dataclasses.dataclass(frozen=True)
class BudgetConfig:
    """Per-project usage-budget policy.

    Unconfigured (the default) leaves ``enabled`` false and every collection
    empty, so admission is a no-op and behavior is unchanged. ``metric`` is the
    single dimension every cap, declared allowance, and fail-safe charge is
    denominated in; the reconciliation ledger still records the full dimension
    breakdown for evidence. ``fail_safe`` governs how usage that a provider never
    reported (or reported malformed) is charged — never as zero.
    """

    enabled: bool = False
    metric: str = "total_tokens"
    fail_safe: str = "reserved"
    fail_safe_amount: float | None = None
    default_declared: float = 0.0
    on_insufficient: str = "block"
    declared: tuple[tuple[str, float], ...] = ()
    limits: tuple[BudgetLimit, ...] = ()
    explicit_keys: frozenset[str] = dataclasses.field(default_factory=frozenset)

    def is_explicit(self, key: str) -> bool:
        return key in self.explicit_keys

    def declared_for(self, phase: str) -> float:
        for name, amount in self.declared:
            if name == phase:
                return amount
        return self.default_declared

    def fail_safe_charge(self, declared: float) -> float:
        """Metric units charged when terminal usage is not authoritative.

        Never zero: the reserved (declared) allowance is retained by default, or
        an explicit fixed floor is used. Both are validated positive at load.
        """

        if self.fail_safe == "fixed" and self.fail_safe_amount is not None:
            return self.fail_safe_amount
        return declared

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "metric": self.metric,
            "fail_safe": self.fail_safe,
            "fail_safe_amount": self.fail_safe_amount,
            "default_declared": self.default_declared,
            "on_insufficient": self.on_insufficient,
            "declared": {name: amount for name, amount in self.declared},
            "limits": [limit.to_json() for limit in self.limits],
            "explicit_keys": sorted(self.explicit_keys),
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
    budget: BudgetConfig = dataclasses.field(default_factory=BudgetConfig)
    config_path: Path | None = None
    config_source: str = "default"
    config_digest: str = ""
    config_key_fingerprints: tuple[tuple[str, str], ...] = ()
    worker_prompt_extra: str | None = None
    runtime_context: tuple[tuple[str, str], ...] = ()
    # Whether the caller's ambient environment is claiming to name *this*
    # target. True when the target is the repository the caller pointed at with
    # --repo or cwd: an ambient selector naming a different project is then a
    # second, contradictory statement about what the command is asking, and
    # answering from the binding alone reports one project's state under the
    # other's name. False for a target the command enumerated from the project
    # registry, where the entry supplies its own context, no single ambient
    # value can be a claim about any one of several entries, and refusing would
    # blank the answer the command exists to give. It rides on the config
    # because every downstream gate -- task source, lock manager, dispatch --
    # re-resolves the binding from it.
    ambient_selects_target: bool = True

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
    reject_unknown_config_keys(data, TOP_LEVEL_CONFIG_KEYS, "configuration")
    config_key_fingerprints = fingerprint_config_keys(data)
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
    budget = parse_budget(data.get("budget", {}))
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
        config_key_fingerprints=config_key_fingerprints,
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
        budget=budget,
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


def fingerprint_config_keys(
    data: Mapping[str, Any],
    *,
    prefix: str = "",
) -> tuple[tuple[str, str], ...]:
    fingerprints: list[tuple[str, str]] = []
    for name, value in sorted(data.items()):
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(value, Mapping):
            nested = fingerprint_config_keys(value, prefix=key)
            if nested:
                fingerprints.extend(nested)
            else:
                fingerprints.append((key, fingerprint_config_value(value)))
            continue
        fingerprints.append((key, fingerprint_config_value(value)))
    return tuple(fingerprints)


def fingerprint_config_value(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def parse_agent(data: object) -> AgentConfig:
    table = expect_table(data, "agent")
    reject_unknown_config_keys(table, AGENT_CONFIG_KEYS, "agent")
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
        reject_unknown_config_keys(
            profile_table,
            AGENT_PROFILE_CONFIG_KEYS,
            label,
        )
        try:
            # Each profile uses the agent execution fields, so it resolves
            # through the same command/kind/prompt-dialect machinery as the
            # default without accepting top-level routing or prompt policy.
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
        reject_unknown_config_keys(entry, AGENT_ROUTING_RULE_KEYS, label)
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
            error_type = (
                TaskAgentResolutionError
                if source == "task.agent"
                else AgentResolutionError
            )
            raise error_type(
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


def command_executable_name(argv: list[str]) -> str:
    for token in argv:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        return Path(token).name
    return ""


def command_option_value(argv: list[str], name: str) -> str:
    for index, token in enumerate(argv):
        if token.startswith(f"{name}="):
            return token.partition("=")[2]
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def inject_executable_options(
    command: str,
    executable: str,
    options: str,
) -> str:
    pattern = re.compile(rf"(?<!\S)((?:[^\s]*/)?{re.escape(executable)})(?=\s|$)")
    return pattern.sub(rf"\1 {options}", command, count=1)


def inject_structured_usage_output(command: str, agent_kind: str) -> str:
    """Request native usage events only for recognized first-party CLIs."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    executable = command_executable_name(argv)
    if executable == "codex" and agent_kind in {"auto", "codex"}:
        if "exec" not in argv or "--json" in argv:
            return command
        return re.sub(r"(?<!\S)exec(?=\s|$)", "exec --json", command, count=1)
    if executable == "claude" and agent_kind in {"auto", "claude"}:
        output_format = command_option_value(argv, "--output-format")
        if output_format:
            if output_format == "stream-json" and "--verbose" not in argv:
                return inject_executable_options(command, "claude", "--verbose")
            return command
        return inject_executable_options(
            command,
            "claude",
            "--output-format stream-json --verbose",
        )
    return command


def structured_usage_observation(
    command: str | None,
    agent_kind: str,
) -> UsageObservationCapability:
    if not command:
        return UsageObservationCapability(
            possible=False,
            provider="unknown",
            output_format="",
            source="unavailable",
            diagnostic="no resolved agent command can emit provider usage",
        )
    effective = inject_structured_usage_output(command, agent_kind)
    source = "runtime-injected" if effective != command else "configured"
    try:
        argv = shlex.split(effective)
    except ValueError:
        return UsageObservationCapability(
            possible=False,
            provider="unknown",
            output_format="",
            source=source,
            diagnostic=(
                "the configured command cannot report usage because it cannot be parsed"
            ),
        )
    executable = command_executable_name(argv)
    if executable == "codex":
        possible = "exec" in argv and "--json" in argv
        return UsageObservationCapability(
            possible=possible,
            provider="openai",
            output_format="jsonl" if possible else "",
            source=source,
            diagnostic=(
                "the resolved Codex command emits JSONL usage events"
                if possible
                else "the configured Codex command cannot report usage without "
                "`codex exec --json`"
            ),
        )
    if executable == "claude":
        output_format = command_option_value(argv, "--output-format")
        print_mode = "-p" in argv or "--print" in argv
        stream_ready = output_format == "stream-json" and "--verbose" in argv
        possible = print_mode and (output_format == "json" or stream_ready)
        return UsageObservationCapability(
            possible=possible,
            provider="anthropic",
            output_format=output_format if possible else "",
            source=source,
            diagnostic=(
                "the resolved Claude command emits structured usage events"
                if possible
                else "the configured Claude command cannot report usage without "
                "print mode and JSON or verbose stream-JSON output"
            ),
        )
    return UsageObservationCapability(
        possible=False,
        provider="unknown",
        output_format="",
        source=source,
        diagnostic=(
            "usage observation is unavailable because the configured command is "
            "not a recognized Codex or Claude CLI invocation"
        ),
    )


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
    values = {
        "prompt": prompt,
        "model": model or "",
        "effort": effort or "",
        **format_fields,
    }
    return format_shell_command_template(
        command_template,
        values,
        windows_shell_fields=("model", "effort", *format_fields),
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


def command_embeds_native_model(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(argv):
        if token in {"--model", "-m"}:
            if index + 1 < len(argv) and "{model}" not in argv[index + 1]:
                return True
            continue
        if token.startswith("--model="):
            if "{model}" not in token.split("=", 1)[1]:
                return True
            continue
        if token in {"-c", "--config"} and index + 1 < len(argv):
            token = argv[index + 1]
        elif token.startswith(("-c=", "--config=")):
            token = token.split("=", 1)[1]
        else:
            continue
        key, separator, value = token.partition("=")
        if (
            separator
            and "{model}" not in value
            and key.replace("-", "_") in {"model", "model_id"}
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


def validate_worker_prompt_delivery(command_template: str, task: Any) -> None:
    if not getattr(task, "has_traceability", False):
        return
    if command_template_uses_field(command_template, "prompt"):
        return
    raise AgentResolutionError(
        "agent.command must include {prompt} for tasks with traceability "
        "metadata; otherwise the worker prompt addendum and spec context cannot "
        "be delivered. Set agent.command to a prompt-mode template such as "
        "`codex exec {prompt}` or `claude -p {prompt}`."
    )


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
    reject_unknown_config_keys(table, TASK_SOURCE_CONFIG_KEYS, "task_source")
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
        health_command=optional_string(table.get("health")),
        capabilities_command=optional_string(table.get("capabilities")),
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
    reject_unknown_config_keys(table, COMPLETION_CONFIG_KEYS, "completion")
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
    reject_unknown_config_keys(table, ORCHESTRATION_CONFIG_KEYS, "orchestration")

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
    reviewer_routing = parse_reviewer_routing(table, agent_profiles)

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
    external_completion_actor = optional_orchestration_enum_value(
        table,
        "external_completion_actor",
        allowed=ORCHESTRATION_EXTERNAL_COMPLETION_ACTORS,
    )

    return OrchestrationConfig(
        mode=mode,
        reviewer_profile=reviewer_profile,
        reviewer_routing=reviewer_routing,
        gates=gates,
        verify_on_main=verify_on_main,
        integration_lock_timeout_seconds=positive_float(
            table.get("integration_lock_timeout_seconds"),
            ORCHESTRATION_DEFAULT_INTEGRATION_LOCK_TIMEOUT_SECONDS,
            "orchestration.integration_lock_timeout_seconds",
        ),
        main_push_timeout_seconds=positive_float(
            table.get("main_push_timeout_seconds"),
            ORCHESTRATION_DEFAULT_MAIN_PUSH_TIMEOUT_SECONDS,
            "orchestration.main_push_timeout_seconds",
        ),
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
        max_candidate_reanchors=nonnegative_int(
            table.get("max_candidate_reanchors"),
            2,
            "orchestration.max_candidate_reanchors",
        ),
        integration_enabled=optional_bool(
            table.get("integration_enabled"),
            True,
            "orchestration.integration_enabled",
        ),
        push_main_to_upstream=optional_bool(
            table.get("push_main_to_upstream"),
            False,
            "orchestration.push_main_to_upstream",
        ),
        task_provenance_mode=task_provenance_mode,
        external_completion_actor=external_completion_actor,
        explicit_keys=explicit_keys,
    )


def parse_reviewer_routing(
    table: Mapping[str, object],
    agent_profiles: Mapping[str, AgentConfig],
) -> tuple[ReviewerRoutingRule, ...]:
    raw = table.get("reviewer_routing")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            "orchestration.reviewer_routing must be an array of routing tables"
        )
    rules: list[ReviewerRoutingRule] = []
    for index, entry in enumerate(raw):
        label = f"orchestration.reviewer_routing[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a table")
        reject_unknown_config_keys(entry, REVIEWER_ROUTING_RULE_KEYS, label)
        profile = optional_nonempty_string(entry.get("profile"))
        if profile is None:
            raise ValueError(f"{label}.profile is required")
        if profile not in agent_profiles:
            available = ", ".join(sorted(agent_profiles)) or "none"
            raise ValueError(
                f"{label}.profile {profile!r} is not defined in [agent.profiles] "
                f"(defined: {available})"
            )
        match_profile = optional_nonempty_string(entry.get("match_implementer_profile"))
        match_provider = optional_nonempty_string(
            entry.get("match_implementer_provider")
        )
        if match_profile is None and match_provider is None:
            raise ValueError(
                f"{label} must define match_implementer_profile or "
                "match_implementer_provider"
            )
        if match_profile is not None and match_profile not in agent_profiles:
            available = ", ".join(sorted(agent_profiles)) or "none"
            raise ValueError(
                f"{label}.match_implementer_profile {match_profile!r} is not "
                f"defined in [agent.profiles] (defined: {available})"
            )
        if match_provider is not None and match_provider not in SUPPORTED_AGENT_CLIS:
            allowed = ", ".join(SUPPORTED_AGENT_CLIS)
            raise ValueError(
                f"{label}.match_implementer_provider must be one of: {allowed}"
            )
        rules.append(
            ReviewerRoutingRule(
                profile=profile,
                match_implementer_profile=match_profile,
                match_implementer_provider=match_provider,
            )
        )
    return tuple(rules)


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


def optional_orchestration_enum_value(
    table: Mapping[str, object],
    key: str,
    *,
    allowed: Sequence[str],
) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or value not in allowed:
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
    reject_unknown_config_keys(table, SUPERVISION_CONFIG_KEYS, "supervision")
    for current, legacy in SUPERVISION_PROVIDER_LIMIT_ALIASES.items():
        if current in table and legacy in table:
            raise ValueError(
                f"supervision.{current} and deprecated supervision.{legacy} "
                "cannot both be set"
            )
    legacy_to_current = {
        legacy: current
        for current, legacy in SUPERVISION_PROVIDER_LIMIT_ALIASES.items()
    }
    explicit_keys = frozenset(
        legacy_to_current.get(str(key), str(key)) for key in table
    )
    compatibility_diagnostics = tuple(
        f"supervision.{legacy} is deprecated; use supervision.{current}"
        for current, legacy in SUPERVISION_PROVIDER_LIMIT_ALIASES.items()
        if legacy in table
    )

    def provider_limit_value(key: str) -> object:
        legacy = SUPERVISION_PROVIDER_LIMIT_ALIASES[key]
        return table.get(key) if key in table else table.get(legacy)

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
        provider_limit_detection=optional_bool(
            provider_limit_value("provider_limit_detection"),
            SUPERVISION_DEFAULT_PROVIDER_LIMIT_DETECTION,
            "supervision.provider_limit_detection",
        ),
        provider_limit_backoff_seconds=nonnegative_float(
            provider_limit_value("provider_limit_backoff_seconds"),
            SUPERVISION_DEFAULT_PROVIDER_LIMIT_BACKOFF_SECONDS,
            "supervision.provider_limit_backoff_seconds",
        ),
        provider_limit_patterns=parse_provider_limit_patterns(
            provider_limit_value("provider_limit_patterns")
        ),
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
        compatibility_diagnostics=compatibility_diagnostics,
    )


def parse_provider_limit_patterns(value: object) -> tuple[str, ...]:
    patterns = nonempty_string_tuple(
        value, (), "supervision.provider_limit_patterns", allow_empty=True
    )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                "supervision.provider_limit_patterns entry is not a valid regex "
                f"({pattern!r}): {exc}"
            ) from exc
    return patterns


def parse_autopilot(data: object) -> AutopilotConfig:
    table = expect_table(data, "autopilot")
    explicit_keys = frozenset(str(key) for key in table)
    reject_unknown_config_keys(table, AUTOPILOT_CONFIG_KEYS, "autopilot")
    worktree_disposition = table.get("worktree_disposition", "report-only")
    if (
        not isinstance(worktree_disposition, str)
        or worktree_disposition not in AUTOPILOT_WORKTREE_DISPOSITION_POLICIES
    ):
        allowed = ", ".join(AUTOPILOT_WORKTREE_DISPOSITION_POLICIES)
        raise ValueError("autopilot.worktree_disposition must be one of: " + allowed)
    raw_interval = table.get("interval_seconds")
    return AutopilotConfig(
        jobs=optional_positive_int(table.get("jobs"), "autopilot.jobs"),
        interval_seconds=optional_autopilot_interval(
            raw_interval,
            "autopilot.interval_seconds",
        ),
        min_ready=optional_positive_int(table.get("min_ready"), "autopilot.min_ready"),
        dispatch_min_ready=optional_positive_int(
            table.get("dispatch_min_ready"),
            "autopilot.dispatch_min_ready",
        ),
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
        require_upstream_sync=optional_bool(
            table.get("require_upstream_sync"),
            False,
            "autopilot.require_upstream_sync",
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
    reject_unknown_config_keys(
        table,
        DISK_RESERVE_CONFIG_KEYS,
        "autopilot.disk_reserve",
    )
    warn_free_bytes = optional_nonnegative_int(
        table.get("warn_free_bytes"), "autopilot.disk_reserve.warn_free_bytes"
    )
    hard_stop_free_bytes = optional_nonnegative_int(
        table.get("hard_stop_free_bytes"),
        "autopilot.disk_reserve.hard_stop_free_bytes",
    )
    min_free_bytes = optional_nonnegative_int(
        table.get("min_free_bytes"), "autopilot.disk_reserve.min_free_bytes"
    )
    min_free_inodes = optional_nonnegative_int(
        table.get("min_free_inodes"), "autopilot.disk_reserve.min_free_inodes"
    )
    min_free_inode_fraction = optional_fraction(
        table.get("min_free_inode_fraction"),
        "autopilot.disk_reserve.min_free_inode_fraction",
    )
    reserve = DiskReserveConfig(
        warn_free_bytes=warn_free_bytes,
        hard_stop_free_bytes=hard_stop_free_bytes,
        min_free_bytes=min_free_bytes,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
        explicit_keys=explicit_keys,
    )
    if hard_stop_free_bytes is not None and min_free_bytes is not None:
        raise ValueError(
            "autopilot.disk_reserve.hard_stop_free_bytes and .min_free_bytes "
            "cannot both be configured"
        )
    if reserve.effective_warn_free_bytes < reserve.effective_hard_stop_free_bytes:
        raise ValueError(
            "autopilot.disk_reserve.warn_free_bytes must be greater than or "
            "equal to hard_stop_free_bytes"
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
    reject_unknown_config_keys(table, LOCKS_CONFIG_KEYS, "locks")
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
    reject_unknown_config_keys(
        table,
        PROJECT_BINDING_CONFIG_KEYS,
        "project_binding",
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


def ambient_selector_claim(ambient: Mapping[str, str], name: str) -> str | None:
    """The project an ambient variable actually names, or ``None``.

    A value that is empty or whitespace-only names nothing. Explicit sources are
    held to exactly this standard already, and `NAME=` is how a large amount of
    shell and unit-file code unsets a variable -- which is the remedy this
    binding's own diagnostic recommends, so treating it as a competing selector
    makes the remedy fail when followed. Surrounding whitespace is a capture
    artifact (`NAME=$(cmd)` keeps the trailing newline), not a different
    project.
    """

    value = ambient.get(name)
    if value is None:
        return None
    return value.strip() or None


def resolve_project_binding(
    config: VibeConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedProjectBinding:
    """Resolve declared namespace selectors from explicit sources only.

    A value inherited solely from the ambient process environment is refused:
    that is the routing ambiguity this binding exists to close. An ambient value
    that *disagrees* with the resolved one is refused too, but only when this
    config's target was selected by the caller rather than enumerated -- see
    ``VibeConfig.ambient_selects_target``.
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
        resolved_value: str | None = None
        resolved_source = ""
        if supplied_value is not None:
            resolved_value = supplied_value
            resolved_source = PROJECT_BINDING_SOURCE_RUNTIME_CONTEXT
        elif pinned_value is not None:
            resolved_value = pinned_value
            resolved_source = PROJECT_BINDING_SOURCE_CONFIG
        if resolved_value is not None:
            ambient_claim = ambient_selector_claim(ambient, name)
            if (
                config.ambient_selects_target
                and ambient_claim is not None
                and ambient_claim != resolved_value.strip()
            ):
                diagnostics.append(
                    ProjectBindingDiagnostic(
                        name, PROJECT_BINDING_REASON_AMBIENT_CONFLICT
                    )
                )
                continue
            entries.append(ResolvedBindingEntry(name, resolved_value, resolved_source))
            continue
        reason = (
            PROJECT_BINDING_REASON_AMBIENT_ONLY
            if ambient_selector_claim(ambient, name) is not None
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
    reject_unknown_config_keys(table, SPEC_DIAGNOSTICS_CONFIG_KEYS, "specs")
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


def parse_budget(data: object) -> BudgetConfig:
    table = expect_table(data, "budget")
    explicit_keys = frozenset(str(key) for key in table)
    reject_unknown_config_keys(table, BUDGET_CONFIG_KEYS, "budget")
    enabled = optional_bool(table.get("enabled"), False, "budget.enabled")
    metric = optional_nonempty_string(table.get("metric")) or "total_tokens"
    if metric not in BUDGET_METRICS:
        allowed = ", ".join(BUDGET_METRICS)
        raise ValueError(f"budget.metric must be one of: {allowed}")
    fail_safe = optional_nonempty_string(table.get("fail_safe")) or "reserved"
    if fail_safe not in BUDGET_FAIL_SAFE_POLICIES:
        allowed = ", ".join(BUDGET_FAIL_SAFE_POLICIES)
        raise ValueError(f"budget.fail_safe must be one of: {allowed}")
    fail_safe_amount = optional_positive_number(
        table.get("fail_safe_amount"), "budget.fail_safe_amount"
    )
    if fail_safe == "fixed" and fail_safe_amount is None:
        raise ValueError(
            "budget.fail_safe = 'fixed' requires a positive budget.fail_safe_amount"
        )
    on_insufficient = optional_nonempty_string(table.get("on_insufficient")) or "block"
    if on_insufficient not in BUDGET_ON_INSUFFICIENT:
        allowed = ", ".join(BUDGET_ON_INSUFFICIENT)
        raise ValueError(f"budget.on_insufficient must be one of: {allowed}")
    default_declared = (
        optional_positive_number(
            table.get("default_declared"), "budget.default_declared"
        )
        or 0.0
    )
    declared = parse_budget_declared(table.get("declared"))
    limits = parse_budget_limits(table.get("limits"))
    if enabled and default_declared <= 0.0 and not declared:
        raise ValueError(
            "budget.enabled requires a positive budget.default_declared or at "
            "least one budget.declared phase allowance"
        )
    return BudgetConfig(
        enabled=enabled,
        metric=metric,
        fail_safe=fail_safe,
        fail_safe_amount=fail_safe_amount,
        default_declared=default_declared,
        on_insufficient=on_insufficient,
        declared=declared,
        limits=limits,
        explicit_keys=explicit_keys,
    )


def parse_budget_declared(value: object) -> tuple[tuple[str, float], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ValueError("budget.declared must be a table of phase allowances")
    declared: list[tuple[str, float]] = []
    for raw_phase, raw_amount in value.items():
        phase = str(raw_phase)
        if phase not in BUDGET_PHASES:
            allowed = ", ".join(sorted(BUDGET_PHASES))
            raise ValueError(
                f"budget.declared phase {phase!r} must be one of: {allowed}"
            )
        amount = optional_positive_number(raw_amount, f"budget.declared.{phase}")
        if amount is None:
            raise ValueError(f"budget.declared.{phase} must be a positive number")
        declared.append((phase, amount))
    return tuple(sorted(declared))


def parse_budget_limits(value: object) -> tuple[BudgetLimit, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("budget.limits must be an array of limit tables")
    if len(value) > BUDGET_MAX_LIMITS:
        raise ValueError(
            f"budget.limits has too many entries ({len(value)}); "
            f"maximum {BUDGET_MAX_LIMITS}"
        )
    limits: list[BudgetLimit] = []
    for index, entry in enumerate(value):
        label = f"budget.limits[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a table")
        reject_unknown_config_keys(entry, BUDGET_LIMIT_KEYS, label)
        limit_value = optional_positive_number(entry.get("limit"), f"{label}.limit")
        if limit_value is None:
            raise ValueError(f"{label}.limit is required and must be a positive number")
        warn_at = optional_fraction_over_zero(entry.get("warn_at"), f"{label}.warn_at")
        window_hours = nonnegative_float(
            entry.get("window_hours"), 0.0, f"{label}.window_hours"
        )
        provider = optional_nonempty_string(entry.get("provider"))
        if provider is not None and provider not in BUDGET_PROVIDERS:
            allowed = ", ".join(sorted(BUDGET_PROVIDERS))
            raise ValueError(f"{label}.provider must be one of: {allowed}")
        phase = optional_nonempty_string(entry.get("phase"))
        if phase is not None and phase not in BUDGET_PHASES:
            allowed = ", ".join(sorted(BUDGET_PHASES))
            raise ValueError(f"{label}.phase must be one of: {allowed}")
        limits.append(
            BudgetLimit(
                limit=limit_value,
                project=optional_nonempty_string(entry.get("project")),
                provider=provider,
                phase=phase,
                model=optional_nonempty_string(entry.get("model")),
                effort=optional_nonempty_string(entry.get("effort")),
                warn_at=warn_at,
                window_hours=window_hours,
            )
        )
    return tuple(limits)


def optional_positive_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number <= 0.0:
        raise ValueError(f"{name} must be a positive number")
    return number


def optional_fraction_over_zero(value: object, name: str) -> float | None:
    if value is None:
        return None
    number = bounded_float(value, 0.0, name, minimum=0.0, maximum=1.0)
    if number <= 0.0:
        raise ValueError(f"{name} must be greater than 0 and at most 1")
    return number


def expect_table(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def reject_unknown_config_keys(
    table: Mapping[str, object],
    supported: frozenset[str],
    path: str,
) -> None:
    unknown = sorted(str(key) for key in table if str(key) not in supported)
    if not unknown:
        return
    paths = ", ".join(f"{path}.{key}" for key in unknown)
    raise ValueError(f"{path} contains unsupported keys: {paths}")


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


def optional_autopilot_interval(value: object, name: str) -> float | None:
    if value is None:
        return None
    parsed = nonnegative_float(value, 0.0, name)
    if 0 < parsed < AUTOPILOT_MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"{name} must be zero for drain mode or at least "
            f"{AUTOPILOT_MIN_INTERVAL_SECONDS} seconds"
        )
    return parsed


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
