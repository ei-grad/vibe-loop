from __future__ import annotations

import datetime
import errno
import random
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

TRANSIENT_STDERR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:error|status|HTTP|code)[:\s/\d.]*\b(429|500|502|503|529)\b",
        re.IGNORECASE,
    ),
    re.compile(r"rate[_\s-]?limit", re.IGNORECASE),
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"throttl", re.IGNORECASE),
    re.compile(r"\bcapacity\b", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"server\s+error", re.IGNORECASE),
    re.compile(r"service\s+unavailable", re.IGNORECASE),
    re.compile(r"bad\s+gateway", re.IGNORECASE),
    re.compile(r"internal\s+server", re.IGNORECASE),
    re.compile(r"resource[_\s]exhausted", re.IGNORECASE),
    re.compile(r"temporarily\s+unavailable", re.IGNORECASE),
    re.compile(r"try\s+again\s+later", re.IGNORECASE),
    re.compile(r"(session|usage|weekly|5-hour)\s+limit", re.IGNORECASE),
    re.compile(r"hit your[^\n]{0,40}\blimit\b", re.IGNORECASE),
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

QUOTA_RESET_PATTERN = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(?UTC\)?",
    re.IGNORECASE,
)
QUOTA_RESET_MARGIN_SECONDS = 120.0
QUOTA_RESET_MAX_DELAY_SECONDS = 8 * 3600.0


def parse_quota_reset_delay(
    text: str,
    *,
    now: datetime.datetime | None = None,
) -> float | None:
    """Seconds until an advertised quota reset (e.g. "resets 2:40am (UTC)").

    Returns None when the text carries no parseable reset time. The delay
    includes a safety margin and is capped so a misparsed time cannot stall
    the loop for more than QUOTA_RESET_MAX_DELAY_SECONDS.
    """
    match = QUOTA_RESET_PATTERN.search(text)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    current = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    reset = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= current:
        reset += datetime.timedelta(days=1)
    delay = (reset - current).total_seconds() + QUOTA_RESET_MARGIN_SECONDS
    return min(delay, QUOTA_RESET_MAX_DELAY_SECONDS)


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
    max_retries = max(max_retries, 0)
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

    raise RuntimeError("retry_subprocess_run: unreachable")
