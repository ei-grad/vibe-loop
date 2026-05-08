from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import stat
from pathlib import Path


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
    "private-key",
    "privatekey",
    "oauth",
    "secret",
    "secrets",
    "service-account",
    "service-account-key",
    "service-account-key",
    "serviceaccount",
    "serviceaccountkey",
    "token",
    "tokens",
    "xox",
    "xoxb",
)
SECRET_KEY_TERM_PATTERN = (
    r"access[_-]?key[_-]?id|api[_-]?key|auth[_-]?token|authorization|"
    r"(?<![A-Za-z0-9])auth(?![A-Za-z0-9])|client[_-]?secret|"
    r"client[_-]?key[_-]?data|credential|credentials|creds|github[_-]?pat|githubpat|"
    r"password|passwd|personal[_-]?access[_-]?token|private[_-]?key|secret|"
    r"service[_-]?account[_-]?key|(?<![A-Za-z0-9])tokens?(?![A-Za-z0-9])"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?im)^(\s*(?:-\s*)?(?=[A-Za-z0-9_.-]*(?:"
    rf"{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Za-z0-9_.-]+\s*[:=]\s*).*$"
)
SPACED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(\s*(?:-\s*)?[\"']?[A-Za-z0-9_. -]*(?:access[\s.-]*key[\s.-]*id|"
    r"api[\s.-]*key|bearer[\s.-]*token|client[\s.-]*secret|"
    r"credentials?|"
    r"(?:access[\s.-]*)?tokens?(?![A-Za-z0-9])|[A-Za-z0-9_. -]*password|"
    r"secret[\s.-]*(?:access[\s.-]*)?key|private[\s.-]*key|"
    r"service[\s.-]*account[\s.-]*key)"
    r"[A-Za-z0-9_. -]*[\"']?\s*[:=]\s*).*$"
)
SPACED_MULTILINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(\s*(?:-\s*)?[\"']?[A-Za-z0-9_. -]*(?:access[\s.-]*key[\s.-]*id|"
    r"api[\s.-]*key|bearer[\s.-]*token|client[\s.-]*secret|"
    r"credentials?|"
    r"(?:access[\s.-]*)?tokens?(?![A-Za-z0-9])|[A-Za-z0-9_. -]*password|"
    r"secret[\s.-]*(?:access[\s.-]*)?key|private[\s.-]*key|"
    r"service[\s.-]*account[\s.-]*key)"
    r"[A-Za-z0-9_. -]*[\"']?\s*[:=]\s*(?:[!&][^\s]+\s+)*)"
    r"(?P<marker>[|>][+-]?\d*|\"\"\"|'''|[{\[])"
)
KNOWN_TOKEN_LITERAL_PATTERN = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]+|gh[oprsu]_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|sk-(?:proj-)?[A-Za-z0-9_-]{8,}|"
    r"xox[a-z]?-[A-Za-z0-9-]+|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
SECRET_PATH_VALUE_PATTERN = re.compile(
    r"(?i)((?:access[-_.]?key[-_.]?id|api[-_.]?key|service[-_.]?account[-_.]?key|"
    r"id[-_.]?(?:dsa|ecdsa|ed25519|rsa)|credential|creds|password|passwd|"
    r"private[-_.]?key|secret|token)"
    r"[-_.])(?!(?:json|ya?ml|md|txt|toml)\b)[A-Za-z0-9][A-Za-z0-9_-]+"
)
SECRET_MANIFEST_COMPACT_PREFIXES = (
    "accesskeyid",
    "apikey",
    "auth",
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
    }
)
SECRET_NAME_VALUE_TERM_PATTERN = (
    r"ACCESS_KEY_ID|API_KEY|AUTH|AUTHORIZATION|AUTH_TOKEN|CLIENT_KEY_DATA|CLIENT_SECRET|"
    r"CREDENTIAL|CREDENTIALS|CREDS|GITHUB_PAT|PASSWD|PASSWORD|PERSONAL_ACCESS_TOKEN|"
    r"PRIVATE_KEY|SECRET|SERVICE_ACCOUNT_KEY|TOKEN"
)
SECRET_NAME_VALUE_PATTERN = re.compile(
    rf"(?i)^\s*(?:-\s*)?[\"']?name[\"']?\s*[:=]\s*[\"']?[A-Z0-9_]*(?:"
    rf"{SECRET_NAME_VALUE_TERM_PATTERN})[A-Z0-9_]*[\"']?\s*,?\s*(?:#.*)?$"
)
FLOW_SECRET_NAME_VALUE_PATTERN = re.compile(
    rf"(?i)[\"']?name[\"']?\s*[:=]\s*[\"']?[A-Z0-9_]*(?:"
    rf"{SECRET_NAME_VALUE_TERM_PATTERN})[A-Z0-9_]*[\"']?"
)
FLOW_VALUE_PATTERN = re.compile(r"(?i)[\"']?value[\"']?\s*[:=]")
FLOW_START_VALUE_PATTERN = re.compile(r"(?i)^\s*-\s*{[^}\n]*[\"']?value[\"']?\s*[:=]")
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
    rf"(?i)(\b(?=[A-Z0-9_-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Z_][A-Z0-9_-]*\s*=\s*\\([\"']))(.*?)(\\\2)"
)
INLINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(\b(?=[A-Z0-9_-]*(?:{SECRET_KEY_TERM_PATTERN}))"
    r"[A-Z_][A-Z0-9_-]*\s*=\s*)(?:([\"'])(.*?)(\2)|([^\s\"'\\]+))"
)
SECRET_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:api-key|auth|authorization|auth-token|client-secret|password|passwd|"
    r"private-key|secret|token)(?:=|\s+))(?:([\"'])(.*?)(\2)|(?!--)([^\s\"'\\]+))"
)
ESCAPED_SECRET_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:api-key|auth|authorization|auth-token|client-secret|password|passwd|"
    r"private-key|secret|token)(?:=|\s+)\\([\"']))(.*?)(\\\2)"
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
    r"(?i)([a-z][a-z0-9+.-]*:\\/\\/)([^\\/\s:@]*)(:)([^@\\\s/]+)(@)"
)
URL_TOKEN_USERINFO_VALUE_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|github_pat_|gh[oprsu]_|glpat|secret|token|xox[a-z]?)"
)
URL_TOKEN_USERINFO_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)"
    r"([^/\s:@]*(?:api[_-]?key|github_pat_|gh[oprsu]_|glpat|secret|token|xox[a-z]?)"
    r"[^/\s:@]*)(@)"
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
            "mtime_ns": self.mtime_ns,
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
    if name in IGNORED_EVIDENCE_DIR_NAMES:
        return "ignored_directory"
    if is_secret_like_directory_name(name):
        return "secret_directory"
    relative = relative_evidence_path(repo, path)
    if relative == ".":
        return None
    parts = tuple(part.lower() for part in Path(relative).parts)
    if any(is_secret_like_directory_name(part) for part in parts):
        return "secret_directory"
    return None


def file_skip_reason(repo: Path, path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
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


def normalized_secret_name(value: str) -> str:
    with_camel_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    return re.sub(r"[\s_.]+", "-", with_camel_boundaries.lower())


def contains_secret_name_term(value: str, terms: tuple[str, ...]) -> bool:
    normalized = normalized_secret_name(value)
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    for term in terms:
        normalized_term = term.replace("_", "-")
        compact_term = normalized_term.replace("-", "")
        if normalized_term in {"auth", "token"}:
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


def is_secret_like_path(path: Path) -> bool:
    name = path.name
    lower_name = name.lower()
    if KNOWN_TOKEN_LITERAL_PATTERN.search(name):
        return True
    if lower_name in SECRET_EVIDENCE_FILE_NAMES:
        return True
    if lower_name.startswith(".env."):
        return True
    if any(lower_name.endswith(suffix) for suffix in SECRET_EVIDENCE_FILE_SUFFIXES):
        return True
    return contains_secret_name_term(path.stem, SECRET_EVIDENCE_NAME_TERMS)


def is_secret_like_directory_name(name: str) -> bool:
    lower_name = name.lower()
    if KNOWN_TOKEN_LITERAL_PATTERN.search(name):
        return True
    if lower_name == ".env" or lower_name.startswith(".env."):
        return True
    normalized = name.lstrip(".")
    if lower_name in SECRET_EVIDENCE_DIR_NAMES:
        return True
    return contains_secret_name_term(normalized, SECRET_EVIDENCE_DIR_TERMS)


def is_secret_like_section_name(section: str) -> bool:
    compact_terms = {term.replace("-", "") for term in SECRET_SECTION_COMPONENTS}
    for component in re.split(r"[.\s\"']+", section.strip()):
        if not component:
            continue
        if re.fullmatch(r"[A-Za-z]+-\d+[A-Za-z0-9_-]*", component):
            continue
        normalized = component.lower().replace("_", "-")
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


def redact_evidence_text(text: str) -> str:
    redacted = redact_multiline_secret_blocks(text)
    redacted = PRIVATE_KEY_BLOCK_PATTERN.sub("<redacted private key>", redacted)
    redacted = URL_CREDENTIAL_PATTERN.sub(redact_url_credentials, redacted)
    redacted = ESCAPED_URL_CREDENTIAL_PATTERN.sub(redact_url_credentials, redacted)
    redacted = URL_TOKEN_USERINFO_PATTERN.sub(r"\1<redacted>\3", redacted)
    redacted = KNOWN_TOKEN_LITERAL_PATTERN.sub("<redacted token>", redacted)
    redacted = ESCAPED_INLINE_SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match[1]}<redacted>{match[4]}",
        redacted,
    )
    redacted = INLINE_SECRET_ASSIGNMENT_PATTERN.sub(
        redact_quoted_or_unquoted_secret,
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
        lambda match: f"{match[1]}<redacted>",
        QUOTED_SECRET_ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match[1]}<redacted>",
            redacted,
        ),
    )
    redacted = SPACED_SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match[1]}<redacted>",
        redacted,
    )
    redacted = UNREDACTED_SECRET_LINE_PATTERN.sub("<redacted>", redacted)
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
    flow_map_quote: str | None = None
    flow_map_triple_quote: str | None = None
    pending_value_line: tuple[str, str, str] | None = None
    secret_name_value_indent: int | None = None
    secret_section = False

    for line in lines:
        if pending_value_line is not None:
            pending_line, pending_prefix, pending_ending = pending_value_line
            if SECRET_NAME_VALUE_PATTERN.match(line):
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
            if stripped and leading_space_count(line) <= secret_name_value_indent:
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
            delta, flow_map_quote, flow_map_triple_quote = container_delimiter_delta(
                line,
                quote=flow_map_quote,
                triple_quote=flow_map_triple_quote,
            )
            flow_map_depth += delta
            if (
                FLOW_VALUE_PATTERN.search(line)
                or SECRET_NAME_VALUE_PATTERN.match(line)
                or FLOW_SECRET_NAME_VALUE_PATTERN.search(line)
            ):
                redacted_lines.append(f"<redacted>{line_ending(line)}")
            else:
                redacted_lines.append(line)
            if (
                flow_map_depth <= 0
                and flow_map_quote is None
                and flow_map_triple_quote is None
            ):
                flow_map_depth = None
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

        if FLOW_SECRET_NAME_VALUE_PATTERN.search(line) and FLOW_VALUE_PATTERN.search(
            line
        ):
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
            if (
                depth > 0
                or flow_map_quote is not None
                or flow_map_triple_quote is not None
            ):
                flow_map_depth = depth
            continue

        list_value_match = LIST_VALUE_PATTERN.match(line)
        if list_value_match:
            indent = leading_space_count(line)
            redacted_lines.append(
                f"{line[:indent]}- value: <redacted>{line_ending(line)}"
            )
            if list_value_match.group("marker"):
                yaml_block_indent = indent
                yaml_block_skip_same_indent_list = False
            continue

        if SECRET_NAME_VALUE_PATTERN.match(line):
            redacted_lines.append(f"<redacted>{line_ending(line)}")
            secret_name_value_indent = leading_space_count(line)
            continue

        split_value_match = SPLIT_SECRET_VALUE_PATTERN.match(line.lstrip())
        if split_value_match:
            if split_value_match.group("marker"):
                indent = leading_space_count(line)
                redacted_lines.append(
                    f"{line[:indent]}{split_value_match['prefix']}<redacted>{line_ending(line)}"
                )
                yaml_block_indent = indent
                yaml_block_skip_same_indent_list = False
            else:
                pending_value_line = (
                    line,
                    split_value_match["prefix"],
                    line_ending(line),
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


def redact_quoted_or_unquoted_secret(match: re.Match[str]) -> str:
    if match[2]:
        return f"{match[1]}{match[2]}<redacted>{match[4]}"
    return f"{match[1]}<redacted>"


def redact_json_secret(match: re.Match[str]) -> str:
    quote = match[2][0]
    return f"{match[1]}{quote}<redacted>{quote}"


def redact_url_credentials(match: re.Match[str]) -> str:
    user = match[2]
    if URL_TOKEN_USERINFO_VALUE_PATTERN.search(user):
        user = "<redacted>"
    return f"{match[1]}{user}:<redacted>{match[5]}"


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
    redacted = KNOWN_TOKEN_LITERAL_PATTERN.sub("<redacted token>", value)
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
    prefixed = redact_manifest_prefixed_component(leading_dots, comparable)
    if prefixed is not None:
        return prefixed
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
        return component
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
    return component


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
