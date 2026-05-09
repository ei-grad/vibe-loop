from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote


EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
DEFAULT_MAX_EVIDENCE_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_EVIDENCE_TOTAL_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_EVIDENCE_FILES = 1000
DEFAULT_MAX_SKIPPED_EVIDENCE_ENTRIES = 1000
ALLOWED_EVIDENCE_EXTENSIONS = frozenset(
    {
        ".json",
        ".markdown",
        ".md",
        ".rst",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
ALLOWED_EXTENSIONLESS_EVIDENCE_NAMES = frozenset(
    {
        "AGENTS",
        "BACKLOG",
        "CHANGELOG",
        "CLAUDE",
        "PLAN",
        "README",
        "ROADMAP",
        "TASKS",
        "TODO",
        "WORKLOG",
    }
)
IGNORED_EVIDENCE_DIR_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".direnv",
        ".vibe-loop",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
SECRET_EVIDENCE_DIR_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".docker",
        ".gnupg",
        ".gcloud",
        ".kube",
        ".ssh",
        "credentials",
        "private",
        "secrets",
    }
)
SECRET_EVIDENCE_DIR_TERMS = (
    "access-key-id",
    "apikey",
    "api-key",
    "aws",
    "azure",
    "auth",
    "client-key-data",
    "credential",
    "creds",
    "docker",
    "gcloud",
    "githubpat",
    "gho",
    "ghp",
    "ghr",
    "ghs",
    "ghu",
    "glpat",
    "gnupg",
    "id-dsa",
    "id-ecdsa",
    "id-ed25519",
    "id-rsa",
    "keys",
    "kube",
    "oauth",
    "passwd",
    "password",
    "private",
    "secret",
    "service-account",
    "service-account-key",
    "ssh",
    "token",
    "webhook-url",
    "xox",
    "xoxb",
)
SECRET_EVIDENCE_FILE_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
    }
)
SECRET_EVIDENCE_FILE_SUFFIXES = (
    ".env",
    ".env.local",
    ".envrc",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
)
SECRET_EVIDENCE_NAME_TERMS = (
    "access-key-id",
    "accesskeyid",
    "api-key",
    "apikey",
    "auth",
    "auth-token",
    "client-key-data",
    "client-secret",
    "credential",
    "credentials",
    "creds",
    "github-pat",
    "githubpat",
    "ghp",
    "gho",
    "ghu",
    "ghs",
    "ghr",
    "glpat",
    "id-dsa",
    "id-ecdsa",
    "id-ed25519",
    "id-rsa",
    "iddsa",
    "idecdsa",
    "ided25519",
    "idrsa",
    "password",
    "passwords",
    "passwd",
    "pass",
    "private-key",
    "privatekey",
    "oauth",
    "secret",
    "secrets",
    "service-account",
    "service-account-key",
    "serviceaccount",
    "serviceaccountkey",
    "token",
    "tokens",
    "webhook-url",
    "xox",
    "xoxb",
)
SECRET_IDENTIFIER_VALUE_TERMS = SECRET_EVIDENCE_NAME_TERMS + (
    "authorization",
    "authz",
    "webhook-url",
)
TASK_ACTION_SECRET_NAME_SUFFIXES = ("-cleanup", "-migration", "-reset", "-rotation")
TASK_ACTION_SECRET_NAME_PREFIXES = ("api-key", "auth", "password", "token")
TASK_ACTION_TITLE_PATTERN = re.compile(
    r"(?i)(?:api key|auth|password|token) (?:cleanup|migration|reset|rotation)"
)
SECRET_KEY_TERM_PATTERN = (
    r"access[_-]?key[_-]?id|api[_-]?key|auth[_-]?token|authorization|"
    r"(?<![A-Za-z0-9])auth(?![A-Za-z0-9])|client[_-]?secret|"
    r"client[_-]?key[_-]?data|credential|credentials|creds|github[_-]?pat|githubpat|"
    r"password|passwd|(?<![A-Za-z0-9])pass(?![A-Za-z0-9])|"
    r"personal[_-]?access[_-]?token|private[_-]?key|secret|"
    r"service[_-]?account[_-]?key|(?<![A-Za-z0-9])tokens?(?![A-Za-z0-9])|"
    r"(?:slack|discord)[_-]?webhook(?:[_-]?url)?(?![A-Za-z0-9_-])|"
    r"webhook[_-]?url(?![A-Za-z0-9_-])"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?im)^(\s*(?:-\s*)?(?=[A-Za-z0-9_.-]*(?:"
    rf"{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+\s*[:=]\s*).*$"
)
SPACED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(\s*(?:-\s*)?[\"']?[A-Za-z0-9_. -]*(?:access[\s.-]*key[\s.-]*id|"
    r"(?:api[\s.-]*key|auth|password|token)[\s.-]*(?:cleanup|migration|reset|rotation)|"
    r"api[\s.-]*key|bearer[\s.-]*token|client[\s.-]*secret|"
    r"auth[\s.-]*migration|client[\s.-]*key[\s.-]*data|credentials?|github[\s.-]*pat|"
    r"(?:access[\s.-]*)?tokens?(?![A-Za-z0-9])|"
    r"[A-Za-z0-9_. -]*password(?:[\s.-]*reset)?(?=\s*[\"']?\s*[:=])|"
    r"secret[\s.-]*(?:access[\s.-]*)?key|private[\s.-]*key|token[\s.-]*cleanup|"
    r"service[\s.-]*account[\s.-]*key|(?:slack|discord)[\s.-]*webhook(?=\s*[\"']?\s*[:=])|"
    r"(?:slack[\s.-]*)?webhook[\s.-]*url)"
    r"[A-Za-z0-9_. -]*[\"']?\s*[:=]\s*).*$"
)
SPACED_MULTILINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(\s*(?:-\s*)?[\"']?[A-Za-z0-9_. -]*(?:access[\s.-]*key[\s.-]*id|"
    r"(?:api[\s.-]*key|auth|password|token)[\s.-]*(?:cleanup|migration|reset|rotation)|"
    r"api[\s.-]*key|bearer[\s.-]*token|client[\s.-]*secret|"
    r"auth[\s.-]*migration|client[\s.-]*key[\s.-]*data|credentials?|github[\s.-]*pat|"
    r"(?:access[\s.-]*)?tokens?(?![A-Za-z0-9])|"
    r"[A-Za-z0-9_. -]*password(?:[\s.-]*reset)?(?=\s*[\"']?\s*[:=])|"
    r"secret[\s.-]*(?:access[\s.-]*)?key|private[\s.-]*key|token[\s.-]*cleanup|"
    r"service[\s.-]*account[\s.-]*key|(?:slack|discord)[\s.-]*webhook(?=\s*[\"']?\s*[:=])|"
    r"(?:slack[\s.-]*)?webhook[\s.-]*url)"
    r"[A-Za-z0-9_. -]*[\"']?\s*[:=]\s*(?:[!&][^\s]+\s+)*)"
    r"(?P<marker>[|>][+-]?\d*|\"\"\"|'''|[{\[])"
)
KNOWN_TOKEN_LITERAL_PATTERN = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]+|gh[oprsu]_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|sk-(?:proj-)?[A-Za-z0-9_-]{8,}|"
    r"xox[a-z]?-[A-Za-z0-9-]+|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
SLACK_WEBHOOK_PATH_PATTERN = re.compile(
    r"(?i)(^|[/\\])hooks\.slack\.com[/\\]services"
    r"(?:[/\\][^/\\\s]+){1,}"
)
SLACK_WEBHOOK_ENCODED_PATH_PATTERN = re.compile(
    r"(?i)hooks(?:[._-]|%2e|%252e)slack(?:[._-]|%2e|%252e)com[._-]services"
    r"(?:[._-][A-Za-z0-9]+){1,}"
)
SLACK_WEBHOOK_ESCAPED_PATH_PATTERN = re.compile(
    r"(?i)hooks(?:\.|%2e|%252e)slack(?:\.|%2e|%252e)com\\\/services"
    r"(?:\\\/[^\\/\s.]+){1,}"
)
SLACK_WEBHOOK_PERCENT_ENCODED_PATH_PATTERN = re.compile(
    r"(?i)hooks(?:\.|%2e)slack(?:\.|%2e)com%2fservices(?:%2f[^/\\\s.]+){1,}"
)
SLACK_WEBHOOK_MIXED_ENCODED_PATH_PATTERN = re.compile(
    r"(?i)hooks(?:\.|%2e|%252e)slack(?:\.|%2e|%252e)com"
    r"(?:[/\\]|%2f|%252f)services"
    r"(?:(?:[/\\]|%2f|%252f)[^/\\\s.]+){1,}"
)
DISCORD_WEBHOOK_PATH_PATTERN = re.compile(
    r"(?i)discord(?:app)?(?:\.|[._-]|%2e|%252e)com"
    r"(?:[/\\._-]|%2f|%252f)api(?:[/\\._-]|%2f|%252f)webhooks"
    r"(?:(?:[/\\._-]|%2f|%252f)[^/\\\s.]+){1,}"
)
SECRET_PATH_VALUE_PATTERN = re.compile(
    r"(?i)((?:access[-_.]?key[-_.]?id|api[-_.]?key|service[-_.]?account[-_.]?key|"
    r"id[-_.]?(?:dsa|ecdsa|ed25519|rsa)|credential|creds|password|passwd|"
    r"private[-_.]?key|secret|token|webhook[-_.]?url)"
    r"[-_.])(?!(?:json|ya?ml|md|txt|toml)\b)[A-Za-z0-9][A-Za-z0-9_-]+"
)
SECRET_MANIFEST_COMPACT_PREFIXES = (
    "accesskeyid",
    "apikey",
    "auth",
    "clientkeydata",
    "clientsecret",
    "credentials",
    "credential",
    "creds",
    "gho",
    "ghp",
    "ghr",
    "ghs",
    "ghu",
    "glpat",
    "githubpat",
    "oauth",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "serviceaccountkey",
    "token",
    "webhookurl",
    "xox",
    "xoxb",
)
SECRET_MANIFEST_COMPACT_START_PREFIXES = (
    "aws",
    "azure",
    "docker",
    "gcloud",
    "gnupg",
    "iddsa",
    "idecdsa",
    "ided25519",
    "idrsa",
    "kube",
    "ssh",
)
SECRET_MANIFEST_GENERIC_COMPONENTS = {
    "authtoken",
    "awsaccesskeyid",
    "clientkeydata",
    "clientsecret",
    "credentials",
    "creds",
    "oauth",
    "passwords",
    "projectsecrets",
    "secrets",
    "tokens",
}
MANIFEST_SAFE_SUFFIXES = {"json", "md", "toml", "txt", "yaml", "yml"}
SECRET_MANIFEST_SEPARATOR_PREFIXES = (
    "aws",
    "azure",
    "docker",
    "env",
    "gcloud",
    "gnupg",
    "kube",
    "ssh",
)
SECRET_SECTION_COMPONENTS = frozenset(
    {
        "access-key-id",
        "apikey",
        "api-key",
        "auth",
        "aws",
        "azure",
        "client-key-data",
        "client-secret",
        "credential",
        "credentials",
        "creds",
        "docker",
        "gcloud",
        "github-pat",
        "githubpat",
        "gnupg",
        "kube",
        "oauth",
        "password",
        "passwords",
        "passwd",
        "personal-access-token",
        "private",
        "private-key",
        "secret",
        "secrets",
        "service-account",
        "service-account-key",
        "ssh",
        "token",
        "tokens",
        "webhook-url",
    }
)
SECRET_NAME_VALUE_TERM_PATTERN = (
    r"ACCESS_KEY_ID|API_KEY|AUTH|AUTHORIZATION|AUTH_TOKEN|CLIENT_KEY_DATA|CLIENT_SECRET|"
    r"CREDENTIAL|CREDENTIALS|CREDS|GITHUB_PAT|PASSWD|PASSWORD|PERSONAL_ACCESS_TOKEN|"
    r"PRIVATE_KEY|SECRET|SERVICE_ACCOUNT_KEY|TOKEN"
)
SECRET_NAME_VALUE_PATTERN = re.compile(
    rf"(?i)^\s*(?:-\s*)?[\"']?(?:name|key)[\"']?\s*[:=]\s*[\"']?[A-Z0-9_]*(?:"
    rf"{SECRET_NAME_VALUE_TERM_PATTERN})[A-Z0-9_]*[\"']?\s*,?\s*(?:#.*)?$"
)
FLOW_SECRET_NAME_VALUE_PATTERN = re.compile(
    rf"(?i)(?:^|[{{,\s])[\"']?(?:name|key)[\"']?\s*[:=]\s*[\"']?[A-Z0-9_]*(?:"
    rf"{SECRET_NAME_VALUE_TERM_PATTERN})[A-Z0-9_]*[\"']?"
)
TASK_ID_IDENTIFIER_VALUE_PATTERN = re.compile(r"(?i)^[A-Z]+-\d+[A-Z0-9_-]*$")
IDENTIFIER_FIELD_VALUE_PATTERN = re.compile(
    r"(?i)(?:^\s*(?:-\s*)?|[{,\[]\s*)[\"']?(?:name|key)[\"']?\s*[:=]\s*"
    r"(?:[!&][^\s]+\s+)*"
    r"(?:(?P<quote>[\"'])(?P<quoted_value>[^\"'\n]+)(?P=quote)|"
    r"(?P<value>[A-Z0-9_.-][^,}\]#\n]*?)(?=\s*(?:[,}\]#]|$)))"
)
FLOW_VALUE_PATTERN = re.compile(r"(?i)[\"']?value[\"']?\s*[:=]")
FLOW_START_VALUE_PATTERN = re.compile(r"(?i)^\s*-\s*{[^}\n]*[\"']?value[\"']?\s*[:=]")
FLOW_CONTAINER_START_PATTERN = re.compile(r"(?i)(?:[:=,\[])\s*(?:\[\s*)?{")
LIST_VALUE_PATTERN = re.compile(
    r"(?i)^\s*-\s*[\"']?value[\"']?\s*:\s*(?:[!&][^\s]+\s+)*(?P<marker>[|>][+-]?\d*)?"
)
SPLIT_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)^(?P<prefix>[\"']?value[\"']?\s*[:=]\s*)(?:[!&][^\s]+\s+)*(?P<marker>[|>][+-]?\d*)?"
)
ENV_CONTAINER_START_PATTERN = re.compile(
    r"(?i)^\s*[\"']?env[\"']?\s*[:=]\s*(?:\[\s*{?|\{)"
)
QUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?im)^(\s*(?:-\s*)?[\"'](?=[A-Za-z0-9_.-]*(?:"
    rf"{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']\s*=\s*|"
    rf"\s*-\s*[\"'](?=[A-Za-z0-9_.-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']\s*:\s*|"
    rf"\s*[\"'](?=[A-Za-z0-9_.-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']\s*:(?!\s*[\"'])\s*).*$"
)
ESCAPED_INLINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?<!-)(\b(?=[A-Z0-9_-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Z_][A-Z0-9_-]*\s*=\s*\\([\"']))(.*?)(\\\2)"
)
INLINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?<!-)(\b(?=[A-Z0-9_-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Z_][A-Z0-9_-]*\s*=\s*)(?:([\"'])(.*?)(\2)|([^\s\"'\\]+))"
)
SECRET_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:access-token|api-key|auth|authorization|auth-token|client-secret|"
    r"oauth-token|password|passwd|private-key|secret|token)(?:=|\s+))"
    r"(?:([\"'])(.*?)(\2)|(?!--)(?!Bearer(?:\s|$))([^\s\"'\\]+))"
)
ESCAPED_SECRET_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:access-token|api-key|auth|authorization|auth-token|client-secret|"
    r"oauth-token|password|passwd|private-key|secret|token)(?:=|\s+)\\([\"']))"
    r"(.*?)(\\\2)"
)
SECRET_BEARER_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:access-token|auth|authorization|auth-token|oauth-token|token)"
    r"(?:=|\s+)Bearer\s+)(?:([\"'])(.*?)(\2)|(?!--)([^\s\"'\\]+))"
)
ESCAPED_SECRET_BEARER_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:access-token|auth|authorization|auth-token|oauth-token|token)"
    r"(?:=|\s+)Bearer\s+\\([\"']))(.*?)(\\\2)"
)
JSON_SECRET_PATTERN = re.compile(
    rf"(?i)([\"'](?=[A-Za-z0-9_.-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']\s*:\s*)"
    r"((?:\"(?:\\.|[^\"\\])*\")|(?:'(?:\\.|[^'\\])*'))"
)
UNREDACTED_SECRET_LINE_PATTERN = re.compile(
    rf"(?im)^.*[\"']?(?=[A-Za-z0-9_.-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']?\s*[:=](?!\s*[\"']?<redacted>[\"']?)\s*.*$"
)
MULTILINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)^(\s*(?:-\s*)?[\"']?(?=[A-Za-z0-9_.-]*(?:"
    rf"{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']?\s*[:=]\s*(?:[!&][^\s]+\s+)*)"
    r"(?:(?P<marker>[|>][+-]?\d*|\"\"\"|'''|[{\[])|\s*(?:#.*)?$)"
)
INLINE_SECRET_CONTAINER_PATTERN = re.compile(
    rf"(?i)[\"']?(?=[A-Za-z0-9_.-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+[\"']?\s*[:=]\s*[{\[]"
)
SECTION_HEADER_PATTERN = re.compile(
    r"^\s*\[{1,2}\s*(?P<section>[^\]\n]+?)\s*\]{1,2}\s*(?:[#;].*)?$"
)
URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]*)(:)([^@\s/]+)(@)"
)
ESCAPED_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*:\\+/\\+/)([^\\/\s:@]*)(:)([^@\\\s/]+)(@)"
)
URL_TOKEN_USERINFO_VALUE_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|github_pat_|gh[oprsu]_|glpat|secret|token|xox[a-z]?)"
)
URL_TOKEN_USERINFO_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)"
    r"([^/\s:@]*(?:api[_-]?key|github_pat_|gh[oprsu]_|glpat|secret|token|xox[a-z]?)"
    r"[^/\s:@]*)(@)"
)
URL_USERINFO_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+)(@)")
ESCAPED_URL_TOKEN_USERINFO_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*:\\+/\\+/)"
    r"([^\\/\s:@]*(?:api[_-]?key|github_pat_|gh[oprsu]_|glpat|secret|token|xox[a-z]?)"
    r"[^\\/\s:@]*)(@)"
)
ESCAPED_URL_USERINFO_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*:\\+/\\+/)([^\\/\s:@]+)(@)"
)
WEBHOOK_URL_LITERAL_PATTERN = re.compile(
    r"(?i)https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)/[^\s\"'<>]+"
)
SLACK_WEBHOOK_SERVICE_PATH_LITERAL_PATTERN = re.compile(
    r"(?i)hooks\.slack\.com/services/[^\s\"'<>]+"
)
DISCORD_WEBHOOK_SERVICE_PATH_LITERAL_PATTERN = re.compile(
    r"(?i)discord(?:app)?\.com/api/webhooks/[^\s\"'<>]+"
)
ESCAPED_WEBHOOK_URL_LITERAL_PATTERN = re.compile(
    r"(?i)https:\\/\\/(?:hooks\.slack\.com\\/services|discord(?:app)?\.com\\/api\\/webhooks)(?:\\/[^\\\s\"'<>]+)+"
)
PERCENT_ENCODED_WEBHOOK_URL_LITERAL_PATTERN = re.compile(
    r"(?i)https%3a%2f%2fhooks(?:\.|%2e)slack(?:\.|%2e)com"
    r"%2fservices(?:%2f[^%\s\"'<>]+)+"
)
WEBHOOK_URL_CANDIDATE_PATTERN = re.compile(
    r"(?i)(?:https[^\s\"'<>]*|hooks[^\s\"'<>]*|discord(?:app)?[^\s\"'<>]*|"
    r"(?:[A-Za-z0-9._/\\%?#&+;,@~-]*"
    r"(?:%[0-9a-f]{2}|%25[0-9a-f]{2}|%5cu00[0-7][0-9a-f]|"
    r"%255cu00[0-7][0-9a-f]|\\+u00[0-7][0-9a-f])"
    r"[^\s\"'<>]*)|"
    r"(?:%[0-9a-f]{2}|%25[0-9a-f]{2}|%5cu00[0-7][0-9a-f]|%255cu00[0-7][0-9a-f])"
    r"[^\s\"'<>]*|"
    r"(?:%25[0-9a-f]{2}){5,}[^\s\"'<>]*|(?:%[0-9a-f]{2}){5,}[^\s\"'<>]*|"
    r"(?:%255cu00[0-7][0-9a-f]){5,}[^\s\"'<>]*|"
    r"(?:%5cu00[0-7][0-9a-f]){5,}[^\s\"'<>]*|"
    r"(?:\\+u00[0-7][0-9a-f]){5,}[^\s\"'<>]*)"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bBearer\s+(?!--)[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END [^-]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)


@dataclasses.dataclass(frozen=True)
class EvidenceLimits:
    max_file_bytes: int = DEFAULT_MAX_EVIDENCE_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_EVIDENCE_TOTAL_BYTES
    max_files: int = DEFAULT_MAX_EVIDENCE_FILES
    max_skipped_entries: int = DEFAULT_MAX_SKIPPED_EVIDENCE_ENTRIES

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.max_total_bytes < 1:
            raise ValueError("max_total_bytes must be positive")
        if self.max_files < 1:
            raise ValueError("max_files must be positive")
        if self.max_skipped_entries < 1:
            raise ValueError("max_skipped_entries must be positive")

    def to_json(self) -> dict[str, int]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_files": self.max_files,
            "max_skipped_entries": self.max_skipped_entries,
        }


@dataclasses.dataclass(frozen=True)
class EvidenceFile:
    path: str
    size: int
    sha256: str
    mtime_ns: int
    content: str
    redacted: bool = False

    def fingerprint_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "redacted": self.redacted,
        }

    def prompt_json(self) -> dict[str, object]:
        return {
            **self.fingerprint_json(),
            "content": self.content,
        }


@dataclasses.dataclass(frozen=True)
class SkippedEvidence:
    path: str
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        payload = {
            "path": self.path,
            "reason": self.reason,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclasses.dataclass(frozen=True)
class EvidenceBundle:
    repo: Path
    limits: EvidenceLimits
    files: tuple[EvidenceFile, ...]
    skipped: tuple[SkippedEvidence, ...]

    @property
    def total_bytes(self) -> int:
        return sum(file.size for file in self.files)

    def manifest_json(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "repo": ".",
            "evidence_limit": self.limits.to_json(),
            "total_bytes": self.total_bytes,
            "files": [file.fingerprint_json() for file in self.files],
            "skipped": [skipped.to_json() for skipped in self.skipped],
        }

    def prompt_input_json(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "manifest": self.manifest_json(),
            "files": [file.prompt_json() for file in self.files],
        }


def collect_generated_discovery_evidence(
    repo: Path,
    *,
    state_dir: str = ".vibe-loop",
    limits: EvidenceLimits | None = None,
) -> EvidenceBundle:
    repo = repo.resolve()
    active_limits = limits or EvidenceLimits()
    state_path = (repo / state_dir).resolve()
    files: list[EvidenceFile] = []
    skipped: list[SkippedEvidence] = []
    skipped_overflow = 0
    total_bytes = 0
    raw_bytes_read = 0

    def record_skipped(path: Path, reason: str, detail: str = "") -> None:
        nonlocal skipped_overflow
        if len(skipped) < active_limits.max_skipped_entries:
            skipped.append(skipped_evidence(repo, path, reason, detail))
        else:
            skipped_overflow += 1

    def record_walk_error(error: OSError) -> None:
        filename = getattr(error, "filename", None)
        path = Path(filename) if filename else repo
        record_skipped(path, "unreadable_directory", str(error))

    for root, dirs, filenames in os.walk(repo, onerror=record_walk_error):
        root_path = Path(root)
        retained_dirs = []
        for dirname in sorted(dirs):
            path = root_path / dirname
            reason = directory_skip_reason(repo, path, state_path)
            if reason:
                record_skipped(path, reason)
            else:
                retained_dirs.append(dirname)
        dirs[:] = retained_dirs

        for filename in sorted(filenames):
            path = root_path / filename
            reason = file_skip_reason(repo, path)
            if reason:
                record_skipped(path, reason)
                continue
            if len(files) >= active_limits.max_files:
                record_skipped(
                    path,
                    "file_count_limit",
                    f"{len(files) + 1} > {active_limits.max_files}",
                )
                continue
            if total_bytes >= active_limits.max_total_bytes:
                record_skipped(
                    path,
                    "total_size_limit",
                    f"{total_bytes} >= {active_limits.max_total_bytes}",
                )
                continue
            try:
                stat_result = path.stat()
            except OSError as exc:
                record_skipped(path, "unreadable", str(exc))
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                record_skipped(path, "non_regular_file")
                continue
            if stat_result.st_size > active_limits.max_file_bytes:
                record_skipped(
                    path,
                    "file_too_large",
                    f"{stat_result.st_size} > {active_limits.max_file_bytes}",
                )
                continue
            remaining_raw_bytes = active_limits.max_total_bytes - raw_bytes_read
            if stat_result.st_size > remaining_raw_bytes:
                record_skipped(
                    path,
                    "total_size_limit",
                    f"{stat_result.st_size} > {remaining_raw_bytes} raw bytes remaining",
                )
                continue
            raw_bytes_read += stat_result.st_size
            try:
                raw = path.read_bytes()
            except OSError as exc:
                record_skipped(path, "unreadable", str(exc))
                continue
            if is_likely_binary(raw):
                record_skipped(path, "binary_file")
                continue
            text = raw.decode("utf-8", errors="replace")
            redacted = redact_evidence_text(text)
            redacted_size = len(redacted.encode("utf-8"))
            if redacted_size > active_limits.max_file_bytes:
                record_skipped(
                    path,
                    "file_too_large",
                    f"{redacted_size} > {active_limits.max_file_bytes} after redaction",
                )
                continue
            if total_bytes + redacted_size > active_limits.max_total_bytes:
                record_skipped(
                    path,
                    "total_size_limit",
                    (
                        f"{total_bytes + redacted_size} > "
                        f"{active_limits.max_total_bytes} after redaction"
                    ),
                )
                continue
            files.append(
                EvidenceFile(
                    path=relative_evidence_path(repo, path),
                    size=redacted_size,
                    sha256=hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
                    mtime_ns=stat_result.st_mtime_ns,
                    content=redacted,
                    redacted=redacted != text,
                )
            )
            total_bytes += redacted_size

    if skipped_overflow:
        summary = SkippedEvidence(
            path=".",
            reason="skipped_manifest_limit",
            detail=(
                f"{skipped_overflow + 1} additional skipped evidence entries omitted"
            ),
        )
        skipped[-1:] = [summary]

    files.sort(key=lambda file: file.path)
    skipped.sort(key=lambda item: (item.path, item.reason, item.detail))
    return EvidenceBundle(
        repo=repo,
        limits=active_limits,
        files=tuple(files),
        skipped=tuple(skipped),
    )


def directory_skip_reason(repo: Path, path: Path, state_path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    name = path.name
    resolved_path = path.resolve()
    if resolved_path == state_path or state_path in resolved_path.parents:
        return "state_directory"
    if is_ignored_evidence_directory_name(name):
        return "ignored_directory"
    if is_secret_like_directory_name(name):
        return "secret_directory"
    relative = relative_evidence_path(repo, path)
    if relative == ".":
        return None
    if is_webhook_like_evidence_directory(relative):
        return "secret_directory"
    parts = tuple(part.lower() for part in Path(relative).parts)
    if any(is_secret_like_directory_name(part) for part in parts):
        return "secret_directory"
    return None


def file_skip_reason(repo: Path, path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    relative = relative_evidence_path(repo, path)
    if is_webhook_like_evidence_path(relative):
        return "secret_path"
    if is_secret_like_path(path):
        return "secret_path"
    if not is_allowed_evidence_file(path):
        return "unsupported_file_type"
    try:
        path.relative_to(repo)
    except ValueError:
        return "outside_repo"
    return None


def is_allowed_evidence_file(path: Path) -> bool:
    if path.suffix.lower() in ALLOWED_EVIDENCE_EXTENSIONS:
        return True
    return path.name in ALLOWED_EXTENSIONLESS_EVIDENCE_NAMES


def is_webhook_like_evidence_directory(relative: str) -> bool:
    parts = tuple(part.lower() for part in Path(relative).parts)
    if any(
        parts[index : index + 2] == ("hooks.slack.com", "services")
        for index in range(max(0, len(parts) - 1))
    ):
        return True
    return any(
        SLACK_WEBHOOK_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_MIXED_ENCODED_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_ENCODED_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_PERCENT_ENCODED_PATH_PATTERN.search(variant)
        or DISCORD_WEBHOOK_PATH_PATTERN.search(variant)
        for variant in evidence_path_variants(relative)
    )


def is_webhook_like_evidence_path(relative: str) -> bool:
    return any(
        SLACK_WEBHOOK_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_ENCODED_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_MIXED_ENCODED_PATH_PATTERN.search(variant)
        or SLACK_WEBHOOK_PERCENT_ENCODED_PATH_PATTERN.search(variant)
        or DISCORD_WEBHOOK_PATH_PATTERN.search(variant)
        for variant in evidence_path_variants(relative)
    )


def evidence_path_variants(relative: str) -> tuple[str, ...]:
    return normalized_text_variants(relative)


def normalized_text_variants(value: str, max_decode_depth: int = 6) -> tuple[str, ...]:
    variants: list[str] = []
    current = value
    for _ in range(max_decode_depth + 1):
        unicode_normalized = normalize_json_unicode_escapes(current)
        dot_normalized = re.sub(r"\\+\.", ".", unicode_normalized)
        normalized = re.sub(
            r"\\+/",
            "/",
            dot_normalized,
        )
        for candidate in (current, normalized):
            if candidate not in variants:
                variants.append(candidate)
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        current = decoded
    return tuple(variants)


def normalize_json_unicode_escapes(value: str) -> str:
    return re.sub(
        r"\\+u00([2-6][0-9a-fA-F]|7[0-9a-eA-E])",
        lambda match: chr(int(match[1], 16)),
        value,
    )


def normalized_secret_name(value: str) -> str:
    with_camel_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    return re.sub(r"[\s_.]+", "-", with_camel_boundaries.lower()).replace(
        "git-hub",
        "github",
    )


def contains_secret_name_term(value: str, terms: tuple[str, ...]) -> bool:
    normalized = normalized_secret_name(value)
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    for term in terms:
        normalized_term = term.replace("_", "-")
        compact_term = normalized_term.replace("-", "")
        if normalized_term in {"auth", "pass", "token"}:
            if (
                normalized == normalized_term
                or normalized.startswith(f"{normalized_term}-")
                or normalized.endswith(f"-{normalized_term}")
                or f"-{normalized_term}-" in normalized
            ):
                return True
            continue
        if normalized_term in normalized or compact_term in compact:
            return True
    return False


def sensitive_dotfile_prefix(value: str) -> str | None:
    lower_value = value.lower()
    for filename in SECRET_EVIDENCE_FILE_NAMES:
        if not filename.startswith("."):
            continue
        if lower_value == filename:
            return filename
        if lower_value.startswith(
            (
                f"{filename}.",
                f"{filename}-",
                f"{filename}_",
                f"{filename}/",
                f"{filename}\\",
            )
        ):
            return filename
    return None


def is_secret_evidence_file_name(name: str) -> bool:
    for variant in normalized_name_parts(name):
        lower_name = variant.lower()
        if lower_name in SECRET_EVIDENCE_FILE_NAMES:
            return True
        if sensitive_dotfile_prefix(lower_name) is not None:
            return True
        if any(lower_name.endswith(suffix) for suffix in SECRET_EVIDENCE_FILE_SUFFIXES):
            return True
    return False


def is_secret_like_path(path: Path) -> bool:
    name = path.name
    if any(
        KNOWN_TOKEN_LITERAL_PATTERN.search(variant)
        for variant in normalized_name_parts(name)
    ):
        return True
    if is_secret_evidence_file_name(name):
        return True
    if path.suffix.lower() in {".markdown", ".md"} and is_exact_task_action_name(
        path.stem
    ):
        return False
    return any(
        contains_secret_name_term(variant, SECRET_EVIDENCE_NAME_TERMS)
        for variant in normalized_text_variants(path.stem)
    )


def is_ignored_evidence_directory_name(name: str) -> bool:
    return any(
        variant.lower() in IGNORED_EVIDENCE_DIR_NAMES
        for variant in normalized_name_parts(name)
    )


def is_secret_like_directory_name(name: str) -> bool:
    for variant in normalized_name_parts(name):
        if is_exact_task_action_name(variant):
            continue
        lower_name = variant.lower()
        if KNOWN_TOKEN_LITERAL_PATTERN.search(variant):
            return True
        if sensitive_dotfile_prefix(lower_name) is not None:
            return True
        if lower_name in SECRET_EVIDENCE_DIR_NAMES:
            return True
        normalized = variant.lstrip(".")
        if any(
            contains_secret_name_term(secret_variant, SECRET_EVIDENCE_DIR_TERMS)
            for secret_variant in normalized_text_variants(normalized)
        ):
            return True
    return False


def normalized_name_parts(value: str) -> tuple[str, ...]:
    variants: list[str] = []
    for variant in normalized_text_variants(value):
        for candidate in (variant, *re.split(r"[/\\]+", variant)):
            if candidate and candidate not in variants:
                variants.append(candidate)
    return tuple(variants)


def is_secret_like_section_name(section: str) -> bool:
    if TASK_ID_IDENTIFIER_VALUE_PATTERN.fullmatch(section.strip()):
        return False
    if is_natural_language_task_action_title(section):
        return False
    if contains_secret_name_term(section, tuple(SECRET_SECTION_COMPONENTS)):
        return True
    compact_terms = {term.replace("-", "") for term in SECRET_SECTION_COMPONENTS}
    for component in re.split(r"[.\s\"']+", section.strip()):
        if not component:
            continue
        if re.fullmatch(r"[A-Za-z]+-\d+[A-Za-z0-9_-]*", component):
            continue
        normalized = normalized_secret_name(component)
        compact = re.sub(r"[^a-z0-9]", "", normalized)
        if normalized in SECRET_SECTION_COMPONENTS or compact in compact_terms:
            return True
        for term in SECRET_SECTION_COMPONENTS:
            normalized_term = term.replace("_", "-")
            if (
                normalized.startswith(f"{normalized_term}-")
                or normalized.endswith(f"-{normalized_term}")
                or f"-{normalized_term}-" in normalized
            ):
                return True
    return False


def is_likely_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if b"\0" in raw:
        return True
    allowed_control = {7, 8, 9, 10, 12, 13, 27}
    suspicious = 0
    sample = raw[:4096]
    for byte in sample:
        if byte < 32 and byte not in allowed_control:
            suspicious += 1
    return suspicious / len(sample) > 0.3


def has_secret_identifier(line: str) -> bool:
    for match in IDENTIFIER_FIELD_VALUE_PATTERN.finditer(line):
        value = (match["quoted_value"] or match["value"]).strip()
        if TASK_ID_IDENTIFIER_VALUE_PATTERN.fullmatch(value):
            continue
        if is_secret_identifier_value(value):
            return True
    return False


def has_only_task_action_secret_identifier(line: str) -> bool:
    secret_values: list[str] = []
    for match in IDENTIFIER_FIELD_VALUE_PATTERN.finditer(line):
        value = (match["quoted_value"] or match["value"]).strip()
        if TASK_ID_IDENTIFIER_VALUE_PATTERN.fullmatch(value):
            continue
        if is_secret_identifier_value(value):
            secret_values.append(value)
    return bool(secret_values) and all(
        is_exact_task_action_name(value) or is_natural_language_task_action_title(value)
        for value in secret_values
    )


def is_secret_identifier_value(value: str) -> bool:
    if not contains_secret_name_term(value, SECRET_IDENTIFIER_VALUE_TERMS):
        return False
    normalized = normalized_secret_name(value).strip("-")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if is_task_action_evidence_name(value):
        return True
    if re.fullmatch(r"[A-Z0-9_]+", value):
        return True
    exact_secret_labels = {
        "access-key-id",
        "access-token",
        "api-key",
        "aws-access-key-id",
        "authorization",
        "auth-token",
        "authz",
        "client-secret",
        "credential",
        "credentials",
        "creds",
        "client-key-data",
        "database-password",
        "github-pat",
        "oauth",
        "password",
        "personal-access-token",
        "secret",
        "service-account-key",
        "token",
        "webhook-url",
    }
    if normalized in exact_secret_labels:
        return True
    compact_secret_labels = {
        re.sub(r"[^a-z0-9]", "", label) for label in exact_secret_labels
    }
    if compact in compact_secret_labels:
        return True
    return normalized.endswith(
        (
            "-credentials",
            "-creds",
            "-key",
            "-pass",
            "-password",
            "-passwd",
            "-secret",
            "-token",
            "-webhook",
            "-webhook-url",
        )
    )


def redact_evidence_text(text: str) -> str:
    redacted = redact_multiline_secret_blocks(text)
    redacted = PRIVATE_KEY_BLOCK_PATTERN.sub("<redacted private key>", redacted)
    redacted = URL_CREDENTIAL_PATTERN.sub(redact_url_credentials, redacted)
    redacted = ESCAPED_URL_CREDENTIAL_PATTERN.sub(redact_url_credentials, redacted)
    redacted = URL_TOKEN_USERINFO_PATTERN.sub(r"\1<redacted>\3", redacted)
    redacted = URL_USERINFO_PATTERN.sub(redact_url_token_userinfo, redacted)
    redacted = ESCAPED_URL_TOKEN_USERINFO_PATTERN.sub(r"\1<redacted>\3", redacted)
    redacted = ESCAPED_URL_USERINFO_PATTERN.sub(
        redact_url_token_userinfo,
        redacted,
    )
    redacted = ESCAPED_WEBHOOK_URL_LITERAL_PATTERN.sub(
        "<redacted webhook>",
        redacted,
    )
    redacted = WEBHOOK_URL_CANDIDATE_PATTERN.sub(
        redact_webhook_url_candidate,
        redacted,
    )
    redacted = PERCENT_ENCODED_WEBHOOK_URL_LITERAL_PATTERN.sub(
        "<redacted webhook>",
        redacted,
    )
    redacted = WEBHOOK_URL_LITERAL_PATTERN.sub("<redacted webhook>", redacted)
    redacted = ESCAPED_SECRET_BEARER_FLAG_PATTERN.sub(
        lambda match: f"{match[1]}<redacted>{match[4]}",
        redacted,
    )
    redacted = SECRET_BEARER_FLAG_PATTERN.sub(
        redact_quoted_or_unquoted_secret,
        redacted,
    )
    redacted = ESCAPED_INLINE_SECRET_ASSIGNMENT_PATTERN.sub(
        redact_escaped_inline_secret_assignment,
        redacted,
    )
    redacted = INLINE_SECRET_ASSIGNMENT_PATTERN.sub(
        redact_inline_secret_assignment,
        redacted,
    )
    redacted = ESCAPED_SECRET_FLAG_PATTERN.sub(
        lambda match: f"{match[1]}<redacted>{match[4]}",
        redacted,
    )
    redacted = SECRET_FLAG_PATTERN.sub(
        redact_quoted_or_unquoted_secret,
        redacted,
    )
    redacted = JSON_SECRET_PATTERN.sub(
        redact_json_secret,
        redacted,
    )
    redacted = SECRET_ASSIGNMENT_PATTERN.sub(
        redact_secret_assignment,
        QUOTED_SECRET_ASSIGNMENT_PATTERN.sub(
            redact_secret_assignment,
            redacted,
        ),
    )
    redacted = SPACED_SECRET_ASSIGNMENT_PATTERN.sub(
        redact_spaced_secret_assignment,
        redacted,
    )
    redacted = UNREDACTED_SECRET_LINE_PATTERN.sub(
        redact_unredacted_secret_line,
        redacted,
    )
    redacted = KNOWN_TOKEN_LITERAL_PATTERN.sub("<redacted token>", redacted)
    return BEARER_TOKEN_PATTERN.sub("Bearer <redacted>", redacted)


def redact_multiline_secret_blocks(text: str) -> str:
    lines = text.splitlines(keepends=True)
    redacted_lines: list[str] = []
    yaml_block_indent: int | None = None
    yaml_block_skip_same_indent_list = False
    triple_quote_marker: str | None = None
    container_depth: int | None = None
    container_quote: str | None = None
    container_triple_quote: str | None = None
    flow_map_depth: int | None = None
    flow_map_indent: int | None = None
    flow_map_quote: str | None = None
    flow_map_triple_quote: str | None = None
    flow_map_secret_seen = False
    pending_value_line: tuple[str, str, str] | None = None
    secret_name_value_indent: int | None = None
    secret_section = False

    for line_number, line in enumerate(lines):
        if pending_value_line is not None:
            pending_line, pending_prefix, pending_ending = pending_value_line
            if has_secret_identifier(line):
                indent = leading_space_count(pending_line)
                redacted_lines.append(
                    f"{pending_line[:indent]}{pending_prefix}<redacted>{pending_ending}"
                )
                redacted_lines.append(f"<redacted>{line_ending(line)}")
                pending_value_line = None
                continue
            redacted_lines.append(pending_line)
            pending_value_line = None

        if secret_name_value_indent is not None:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                redacted_lines.append(line)
                continue
            value_match = SPLIT_SECRET_VALUE_PATTERN.match(stripped)
            if value_match:
                indent = leading_space_count(line)
                redacted_lines.append(
                    f"{line[:indent]}{value_match['prefix']}<redacted>{line_ending(line)}"
                )
                if value_match.group("marker"):
                    yaml_block_indent = indent
                    yaml_block_skip_same_indent_list = False
                secret_name_value_indent = None
                continue
            if (
                stripped
                and leading_space_count(line) <= secret_name_value_indent
                and (
                    stripped.startswith("- ")
                    or SECTION_HEADER_PATTERN.match(line)
                    or not re.match(r".+[:=]", stripped)
                )
            ):
                secret_name_value_indent = None

        if triple_quote_marker is not None:
            if triple_quote_marker in line:
                triple_quote_marker = None
            continue

        if container_depth is not None:
            delta, container_quote, container_triple_quote = container_delimiter_delta(
                line,
                quote=container_quote,
                triple_quote=container_triple_quote,
            )
            container_depth += delta
            if (
                container_depth <= 0
                and container_quote is None
                and container_triple_quote is None
            ):
                container_depth = None
            continue

        if yaml_block_indent is not None:
            stripped = line.lstrip()
            if stripped and leading_space_count(line) <= yaml_block_indent:
                if stripped.startswith("-") and yaml_block_skip_same_indent_list:
                    continue
                if stripped.startswith(("{", "[")):
                    yaml_block_indent = None
                    yaml_block_skip_same_indent_list = False
                    depth, container_quote, container_triple_quote = (
                        container_delimiter_delta(line)
                    )
                    if (
                        depth > 0
                        or container_quote is not None
                        or container_triple_quote is not None
                    ):
                        container_depth = depth
                    continue
                yaml_block_indent = None
                yaml_block_skip_same_indent_list = False
            else:
                continue

        section_match = SECTION_HEADER_PATTERN.match(line)
        if section_match:
            secret_section = is_secret_like_section_name(section_match["section"])
            if secret_section:
                redacted_lines.append(redact_manifest_text(line))
            else:
                redacted_lines.append(line)
            continue

        if secret_section:
            if line.strip():
                redacted_lines.append(
                    f"{line[: leading_space_count(line)]}<redacted>{line_ending(line)}"
                )
            else:
                redacted_lines.append(line)
            continue

        if flow_map_depth is not None:
            stripped = line.lstrip()
            if (
                flow_map_indent is not None
                and stripped
                and leading_space_count(line) <= flow_map_indent
                and not stripped.startswith(("}", "]"))
            ):
                flow_map_depth = None
                flow_map_indent = None
                flow_map_secret_seen = False
            else:
                delta, flow_map_quote, flow_map_triple_quote = (
                    container_delimiter_delta(
                        line,
                        quote=flow_map_quote,
                        triple_quote=flow_map_triple_quote,
                    )
                )
                flow_map_depth += delta
                line_has_secret_name = has_secret_identifier(line)
                flow_map_secret_seen = flow_map_secret_seen or line_has_secret_name
                if (
                    FLOW_VALUE_PATTERN.search(line)
                    and (
                        flow_map_secret_seen
                        or has_secret_name_before_value_item_end(lines, line_number)
                    )
                ) or line_has_secret_name:
                    redacted_lines.append(f"<redacted>{line_ending(line)}")
                else:
                    redacted_lines.append(line)
                if (
                    flow_map_depth <= 0
                    and flow_map_quote is None
                    and flow_map_triple_quote is None
                ):
                    flow_map_depth = None
                    flow_map_indent = None
                    flow_map_secret_seen = False
                continue

        match = MULTILINE_SECRET_ASSIGNMENT_PATTERN.match(line)
        if match is None:
            match = SPACED_MULTILINE_SECRET_ASSIGNMENT_PATTERN.match(line)
        if match:
            marker = match.group("marker")
            redacted_lines.append(f"{match[1]}<redacted>{line_ending(line)}")
            if marker in {'"""', "'''"}:
                if line.count(marker) < 2:
                    triple_quote_marker = marker
            elif marker in {"{", "["}:
                depth, container_quote, container_triple_quote = (
                    container_delimiter_delta(line)
                )
                if (
                    depth > 0
                    or container_quote is not None
                    or container_triple_quote is not None
                ):
                    container_depth = depth
            else:
                yaml_block_indent = leading_space_count(line)
                yaml_block_skip_same_indent_list = marker is None
            continue

        if INLINE_SECRET_CONTAINER_PATTERN.search(line):
            redacted_lines.append(f"<redacted>{line_ending(line)}")
            depth, container_quote, container_triple_quote = container_delimiter_delta(
                line
            )
            if (
                depth > 0
                or container_quote is not None
                or container_triple_quote is not None
            ):
                container_depth = depth
            continue

        if ENV_CONTAINER_START_PATTERN.match(line):
            redacted_lines.append(f"<redacted>{line_ending(line)}")
            depth, container_quote, container_triple_quote = container_delimiter_delta(
                line
            )
            if (
                depth > 0
                or container_quote is not None
                or container_triple_quote is not None
            ):
                container_depth = depth
            continue

        if FLOW_START_VALUE_PATTERN.match(line):
            if has_secret_name_before_value_item_end(lines, line_number):
                redacted_lines.append(f"<redacted>{line_ending(line)}")
                depth, container_quote, container_triple_quote = (
                    container_delimiter_delta(line)
                )
                if (
                    depth > 0
                    or container_quote is not None
                    or container_triple_quote is not None
                ):
                    container_depth = depth
            else:
                redacted_lines.append(line)
            continue

        if has_secret_identifier(line) and FLOW_VALUE_PATTERN.search(line):
            redacted_lines.append(f"<redacted>{line_ending(line)}")
            depth, container_quote, container_triple_quote = container_delimiter_delta(
                line
            )
            if (
                depth > 0
                or container_quote is not None
                or container_triple_quote is not None
            ):
                container_depth = depth
            continue

        if line.lstrip().startswith("- {"):
            redacted_lines.append(line)
            depth, flow_map_quote, flow_map_triple_quote = container_delimiter_delta(
                line
            )
            flow_map_secret_seen = has_secret_identifier(line)
            if (
                depth > 0
                or flow_map_quote is not None
                or flow_map_triple_quote is not None
            ):
                flow_map_depth = depth
                flow_map_indent = leading_space_count(line)
            continue

        if FLOW_CONTAINER_START_PATTERN.search(line) or (
            line.lstrip().startswith("{")
            and (FLOW_VALUE_PATTERN.search(line) or has_secret_identifier(line))
        ):
            depth, flow_map_quote, flow_map_triple_quote = container_delimiter_delta(
                line
            )
            if (
                depth > 0
                or flow_map_quote is not None
                or flow_map_triple_quote is not None
            ):
                if FLOW_VALUE_PATTERN.search(
                    line
                ) and has_secret_name_before_value_item_end(
                    lines,
                    line_number,
                ):
                    redacted_lines.append(f"<redacted>{line_ending(line)}")
                else:
                    redacted_lines.append(line)
                flow_map_depth = depth
                flow_map_indent = leading_space_count(line)
                flow_map_secret_seen = has_secret_identifier(line)
                continue

        list_value_match = LIST_VALUE_PATTERN.match(line)
        if list_value_match:
            if has_secret_name_before_value_item_end(lines, line_number):
                indent = leading_space_count(line)
                redacted_lines.append(
                    f"{line[:indent]}- value: <redacted>{line_ending(line)}"
                )
                if list_value_match.group("marker"):
                    yaml_block_indent = indent
                    yaml_block_skip_same_indent_list = False
            else:
                redacted_lines.append(line)
            continue

        if has_secret_identifier(line):
            if (
                not line.lstrip().startswith("-")
                and has_only_task_action_secret_identifier(line)
                and not FLOW_VALUE_PATTERN.search(line)
            ):
                redacted_lines.append(line)
                continue
            if not (
                FLOW_VALUE_PATTERN.search(line)
                or has_value_field_before_item_end(lines, line_number)
            ):
                redacted_lines.append(line)
                continue
            redacted_lines.append(f"<redacted>{line_ending(line)}")
            secret_name_value_indent = leading_space_count(line)
            continue

        split_value_match = SPLIT_SECRET_VALUE_PATTERN.match(line.lstrip())
        if split_value_match:
            if has_non_secret_identifier_before_value(
                lines, line_number
            ) and not has_secret_name_before_value_item_end(lines, line_number):
                redacted_lines.append(line)
                continue
            if not has_secret_name_before_value_item_end(lines, line_number):
                redacted_lines.append(line)
            elif split_value_match.group("marker"):
                indent = leading_space_count(line)
                redacted_lines.append(
                    f"{line[:indent]}{split_value_match['prefix']}<redacted>{line_ending(line)}"
                )
                yaml_block_indent = indent
                yaml_block_skip_same_indent_list = False
            else:
                indent = leading_space_count(line)
                redacted_lines.append(
                    f"{line[:indent]}{split_value_match['prefix']}<redacted>{line_ending(line)}"
                )
            continue

        redacted_lines.append(line)

    if pending_value_line is not None:
        redacted_lines.append(pending_value_line[0])

    return "".join(redacted_lines)


def leading_space_count(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def container_delimiter_delta(
    line: str,
    *,
    quote: str | None = None,
    triple_quote: str | None = None,
) -> tuple[int, str | None, str | None]:
    delta = 0
    escaped = False
    index = 0
    while index < len(line):
        if triple_quote is not None:
            end = line.find(triple_quote, index)
            if end == -1:
                return delta, quote, triple_quote
            index = end + len(triple_quote)
            triple_quote = None
            continue

        if line.startswith('"""', index) or line.startswith("'''", index):
            triple_quote = line[index : index + 3]
            index += 3
            continue

        character = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            break
        elif character in {"{", "["}:
            delta += 1
        elif character in {"}", "]"}:
            delta -= 1
        index += 1
    return delta, quote, triple_quote


def line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def has_secret_name_before_value_item_end(lines: list[str], start_index: int) -> bool:
    start_line = lines[start_index]
    start_indent = leading_space_count(start_line)
    starts_list_item = start_line.lstrip().startswith("-")
    depth = 0
    quote: str | None = None
    triple_quote: str | None = None
    saw_container = False

    for index in range(start_index, len(lines)):
        line = lines[index]
        stripped = line.lstrip()
        if index > start_index and stripped and depth <= 0:
            indent = leading_space_count(line)
            if indent < start_indent:
                break
            if indent == start_indent and stripped.startswith("- "):
                break
            if (
                not starts_list_item
                and start_indent == 0
                and not stripped.startswith(('"', "'"))
                and indent == start_indent
                and re.match(r"[\"']?(?:name|key)[\"']?\s*[:=]", stripped, re.I)
            ):
                if has_secret_identifier(
                    line
                ) and not has_only_task_action_secret_identifier(line):
                    return True
                break
            if (
                starts_list_item
                and indent <= start_indent
                and re.match(r"[\"']?[A-Za-z0-9_.-]+[\"']?\s*[:=]", stripped)
            ):
                break
            if indent <= start_indent and SECTION_HEADER_PATTERN.match(line):
                break

        if has_secret_identifier(line):
            return True

        delta, quote, triple_quote = container_delimiter_delta(
            line,
            quote=quote,
            triple_quote=triple_quote,
        )
        depth += delta
        saw_container = saw_container or "{" in line or "[" in line
        if (
            index > start_index
            and saw_container
            and depth <= 0
            and quote is None
            and triple_quote is None
        ):
            break

    return False


def has_non_secret_identifier_before_value(lines: list[str], start_index: int) -> bool:
    start_indent = leading_space_count(lines[start_index])
    for index in range(start_index - 1, -1, -1):
        line = lines[index]
        if not line.strip():
            continue
        indent = leading_space_count(line)
        stripped = line.lstrip()
        if indent == start_indent and re.match(
            r"[\"']?(?:name|key)[\"']?\s*[:=]",
            stripped,
            re.I,
        ):
            return not has_secret_identifier(line)
        if indent < start_indent and re.match(
            r"-\s*[\"']?(?:name|key)[\"']?\s*[:=]",
            stripped,
            re.I,
        ):
            return not has_secret_identifier(line)
        break
    return False


def has_value_field_before_item_end(lines: list[str], start_index: int) -> bool:
    start_line = lines[start_index]
    start_indent = leading_space_count(start_line)
    starts_list_item = start_line.lstrip().startswith("-")
    depth = 0
    quote: str | None = None
    triple_quote: str | None = None

    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip()
        if stripped and depth <= 0:
            indent = leading_space_count(line)
            if indent < start_indent:
                break
            if indent == start_indent and stripped.startswith("- "):
                break
            if (
                index > start_index
                and not starts_list_item
                and indent == start_indent
                and re.match(r"[\"']?(?:name|key)[\"']?\s*[:=]", stripped, re.I)
            ):
                break
            if (
                starts_list_item
                and indent <= start_indent
                and re.match(r"[\"']?[A-Za-z0-9_.-]+[\"']?\s*[:=]", stripped)
            ):
                break
            if indent <= start_indent and SECTION_HEADER_PATTERN.match(line):
                break

        if SPLIT_SECRET_VALUE_PATTERN.match(stripped) or LIST_VALUE_PATTERN.match(line):
            return True
        if FLOW_VALUE_PATTERN.search(line):
            return True

        delta, quote, triple_quote = container_delimiter_delta(
            line,
            quote=quote,
            triple_quote=triple_quote,
        )
        depth += delta

    return False


def redact_quoted_or_unquoted_secret(match: re.Match[str]) -> str:
    if match[2]:
        return f"{match[1]}{match[2]}<redacted>{match[4]}"
    return f"{match[1]}<redacted>"


def redact_inline_secret_assignment(match: re.Match[str]) -> str:
    return redact_quoted_or_unquoted_secret(match)


def redact_escaped_inline_secret_assignment(match: re.Match[str]) -> str:
    return f"{match[1]}<redacted>{match[4]}"


def redact_webhook_url_candidate(match: re.Match[str]) -> str:
    value = match[0]
    for variant in normalized_text_variants(value):
        if (
            WEBHOOK_URL_LITERAL_PATTERN.search(variant)
            or SLACK_WEBHOOK_SERVICE_PATH_LITERAL_PATTERN.search(variant)
            or DISCORD_WEBHOOK_SERVICE_PATH_LITERAL_PATTERN.search(variant)
            or is_webhook_like_evidence_path(variant)
        ):
            return "<redacted webhook>"
    return value


def is_task_action_evidence_name(value: str) -> bool:
    normalized = normalized_secret_name(value).strip("-")
    return any(
        normalized == f"{prefix}{suffix}"
        for prefix in TASK_ACTION_SECRET_NAME_PREFIXES
        for suffix in TASK_ACTION_SECRET_NAME_SUFFIXES
    )


def is_exact_task_action_name(value: str) -> bool:
    normalized = normalized_secret_name(value)
    if normalized != value:
        return False
    return any(
        normalized == f"{prefix}{suffix}"
        for prefix in TASK_ACTION_SECRET_NAME_PREFIXES
        for suffix in TASK_ACTION_SECRET_NAME_SUFFIXES
    )


def is_natural_language_task_action_title(value: str) -> bool:
    return bool(TASK_ACTION_TITLE_PATTERN.fullmatch(value.strip()))


def redact_secret_assignment(match: re.Match[str]) -> str:
    return f"{match[1]}<redacted>"


def redact_spaced_secret_assignment(match: re.Match[str]) -> str:
    return f"{match[1]}<redacted>"


def redact_json_secret(match: re.Match[str]) -> str:
    quote = match[2][0]
    return f"{match[1]}{quote}<redacted>{quote}"


def redact_unredacted_secret_line(match: re.Match[str]) -> str:
    return "<redacted>"


def redact_url_credentials(match: re.Match[str]) -> str:
    user = match[2]
    if any(
        URL_TOKEN_USERINFO_VALUE_PATTERN.search(variant)
        for variant in normalized_text_variants(user)
    ):
        user = "<redacted>"
    return f"{match[1]}{user}:<redacted>{match[5]}"


def redact_url_token_userinfo(match: re.Match[str]) -> str:
    user = match[2]
    if any(
        URL_TOKEN_USERINFO_VALUE_PATTERN.search(variant)
        for variant in normalized_text_variants(user)
    ):
        return f"{match[1]}<redacted>{match[3]}"
    return match[0]


def skipped_evidence(
    repo: Path,
    path: Path,
    reason: str,
    detail: str = "",
) -> SkippedEvidence:
    detail = detail.replace(str(repo), ".")
    return SkippedEvidence(
        path=redact_manifest_text(relative_evidence_path(repo, path)),
        reason=reason,
        detail=redact_manifest_text(detail),
    )


def relative_evidence_path(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def redact_manifest_text(value: str) -> str:
    if is_webhook_like_evidence_path(value) or is_webhook_like_evidence_directory(
        value
    ):
        return "<redacted>"
    redacted = SLACK_WEBHOOK_PATH_PATTERN.sub(
        r"\1hooks.slack.com/services/<redacted>",
        value,
    )
    redacted = SLACK_WEBHOOK_ENCODED_PATH_PATTERN.sub(
        "hooks.slack.com-services-<redacted>",
        redacted,
    )
    redacted = SLACK_WEBHOOK_ESCAPED_PATH_PATTERN.sub(
        "hooks.slack.com\\/services\\/<redacted>",
        redacted,
    )
    redacted = SLACK_WEBHOOK_MIXED_ENCODED_PATH_PATTERN.sub(
        "hooks.slack.com/services/<redacted>",
        redacted,
    )
    redacted = SLACK_WEBHOOK_PERCENT_ENCODED_PATH_PATTERN.sub(
        "hooks.slack.com%2Fservices%2F<redacted>",
        redacted,
    )
    redacted = KNOWN_TOKEN_LITERAL_PATTERN.sub("<redacted token>", redacted)
    redacted = SECRET_PATH_VALUE_PATTERN.sub(r"\1<redacted>", redacted)
    return redact_manifest_components(redacted)


def redact_manifest_components(value: str) -> str:
    parts = re.split(r"([/\\])", value)
    return "".join(redact_manifest_component(part) for part in parts)


def redact_manifest_component(component: str) -> str:
    if not component or component in {"/", "\\"}:
        return component
    leading_dots = component[: len(component) - len(component.lstrip("."))]
    comparable = component.lstrip(".")
    for variant in normalized_name_parts(comparable):
        sensitive_file = redact_manifest_sensitive_file_component(
            leading_dots,
            variant,
        )
        if sensitive_file is not None:
            return sensitive_file
        prefixed = redact_manifest_prefixed_component(leading_dots, variant)
        if prefixed is not None:
            return prefixed
        redacted = redact_manifest_component_variant(leading_dots, variant)
        if redacted is not None:
            return redacted
    return component


def redact_manifest_sensitive_file_component(
    leading_dots: str,
    comparable: str,
) -> str | None:
    component = f"{leading_dots}{comparable}"
    prefix = sensitive_dotfile_prefix(component)
    if prefix is None or component.lower() == prefix:
        return None
    return f"{prefix}.<redacted>"


def redact_manifest_component_variant(
    leading_dots: str,
    comparable: str,
) -> str | None:
    stem, dot, suffix = comparable.rpartition(".")
    if not dot:
        stem = comparable
        suffix = ""
    compact = re.sub(r"[^a-z0-9]", "", stem.lower())
    secret_stem = compact in SECRET_MANIFEST_GENERIC_COMPONENTS or any(
        compact == prefix for prefix in SECRET_MANIFEST_COMPACT_PREFIXES
    )
    if dot and suffix.lower() not in MANIFEST_SAFE_SUFFIXES and secret_stem:
        return f"{leading_dots}{stem}.<redacted>"
    if compact in SECRET_MANIFEST_GENERIC_COMPONENTS:
        return None
    for prefix in SECRET_MANIFEST_COMPACT_START_PREFIXES:
        if compact.startswith(prefix) and len(compact) > len(prefix):
            return (
                f"{leading_dots}<redacted>{dot}{suffix}"
                if dot
                else f"{leading_dots}<redacted>"
            )
    for prefix in SECRET_MANIFEST_COMPACT_PREFIXES:
        if prefix in compact and len(compact) > len(prefix):
            return (
                f"{leading_dots}<redacted>{dot}{suffix}"
                if dot
                else f"{leading_dots}<redacted>"
            )
    return None


def redact_manifest_prefixed_component(
    leading_dots: str,
    comparable: str,
) -> str | None:
    lower_comparable = comparable.lower()
    for prefix in SECRET_MANIFEST_SEPARATOR_PREFIXES:
        for separator in (".", "-", "_"):
            marker = f"{prefix}{separator}"
            if lower_comparable.startswith(marker) and len(comparable) > len(marker):
                return (
                    f"{leading_dots}{comparable[: len(prefix)]}"
                    f"{comparable[len(prefix)]}<redacted>"
                )
    return None
