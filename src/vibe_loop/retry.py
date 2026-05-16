from __future__ import annotations

import errno
import random
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

TRANSIENT_STDERR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|(?:error|status|HTTP|code)[:\s]*)\b429\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|(?:error|status|HTTP|code)[:\s]*)\b500\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|(?:error|status|HTTP|code)[:\s]*)\b502\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|(?:error|status|HTTP|code)[:\s]*)\b503\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|(?:error|status|HTTP|code)[:\s]*)\b529\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"rate[_\s-]?limit", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"throttl", re.IGNORECASE),
    re.compile(r"capacity", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"server\s+error", re.IGNORECASE),
    re.compile(r"service\s+unavailable", re.IGNORECASE),
    re.compile(r"bad\s+gateway", re.IGNORECASE),
    re.compile(r"internal\s+server", re.IGNORECASE),
    re.compile(r"resource[_\s]exhausted", re.IGNORECASE),
    re.compile(r"temporarily\s+unavailable", re.IGNORECASE),
    re.compile(r"try\s+again\s+later", re.IGNORECASE),
    re.compile(r"ECONNRESET", re.IGNORECASE),
    re.compile(r"ETIMEDOUT", re.IGNORECASE),
    re.compile(r"connection\s+(reset|refused|timed\s*out)", re.IGNORECASE),
)

TRANSIENT_OSERROR_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EAGAIN,
        errno.ENOMEM,
        errno.EMFILE,
        errno.ENFILE,
    }
)

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 10.0
DEFAULT_MAX_DELAY = 120.0
DEFAULT_JITTER = 0.25

RetryCallback = Callable[[int, float, str], None]


def is_transient_stderr(stderr: str) -> bool:
    return any(pattern.search(stderr) for pattern in TRANSIENT_STDERR_PATTERNS)


def is_transient_oserror(exc: OSError) -> bool:
    return exc.errno in TRANSIENT_OSERROR_ERRNOS


def is_transient_subprocess_result(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    stderr = result.stderr or ""
    return is_transient_stderr(stderr)


def backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
) -> float:
    delay = min(base_delay * (2**attempt), max_delay)
    jitter_range = delay * jitter
    delay += random.uniform(-jitter_range, jitter_range)
    return max(0.0, delay)


def retry_subprocess_run(
    cmd: str | list[str],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    on_retry: RetryCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **subprocess_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    last_exc: OSError | subprocess.TimeoutExpired | None = None
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(cmd, **subprocess_kwargs)
        except subprocess.TimeoutExpired:
            if attempt >= max_retries:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay, jitter)
            if on_retry:
                on_retry(attempt + 1, delay, "timeout")
            sleep(delay)
            continue
        except OSError as exc:
            if not is_transient_oserror(exc) or attempt >= max_retries:
                raise
            last_exc = exc
            delay = backoff_delay(attempt, base_delay, max_delay, jitter)
            if on_retry:
                on_retry(attempt + 1, delay, f"OSError: {exc}")
            sleep(delay)
            continue

        if result.returncode == 0 or not is_transient_subprocess_result(result):
            return result

        if attempt >= max_retries:
            return result

        delay = backoff_delay(attempt, base_delay, max_delay, jitter)
        reason = "transient error"
        stderr_snippet = (result.stderr or "").strip()[:200]
        if stderr_snippet:
            reason = f"transient error: {stderr_snippet}"
        if on_retry:
            on_retry(attempt + 1, delay, reason)
        sleep(delay)

    assert last_exc is not None
    raise last_exc
