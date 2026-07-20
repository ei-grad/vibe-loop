from __future__ import annotations

import dataclasses
import datetime
import errno
import os
import random
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
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

_RESET_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_RESET_CLOCK_NAMED = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?"
# Transient-path parser: requires an explicit UTC marker to avoid reading a
# bare number as a time.
QUOTA_RESET_PATTERN = re.compile(
    rf"resets?\s+(?:at\s+)?{_RESET_CLOCK}\s*\(?UTC\)?",
    re.IGNORECASE,
)
# Limit-wall parser: the UTC marker is optional. Only run against text already
# confirmed to carry a limit-wall phrase, so a bare "reset at 3pm" is safe to
# read as a wall-clock UTC reset.
LIMIT_WALL_RESET_PATTERN = re.compile(
    rf"resets?\s+(?:at\s+)?{_RESET_CLOCK}\s*(?:\(?\s*UTC\s*\)?)?",
    re.IGNORECASE,
)
QUOTA_RESET_MARGIN_SECONDS = 120.0
QUOTA_RESET_MAX_DELAY_SECONDS = 8 * 3600.0


def _reset_delay_from_match(
    match: re.Match[str],
    now: datetime.datetime | None,
) -> float | None:
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


_MONTH_NUMBERS: dict[str, int] = {
    name: index
    for index, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        start=1,
    )
}

# Absolute reset walls advertise a calendar instant rather than a wall clock
# time, e.g. "try again at Jul 25th, 2026 3:24 AM" or "resets 2026-07-25T03:24Z".
# These are only meaningful for multi-day account limits, where a clock-only
# parse would land on the wrong day.
LIMIT_WALL_RESET_DATE_PATTERN = re.compile(
    r"(?:try\s+again|resets?|available\s+again)\s+(?:at\s+|on\s+)?"
    r"(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*"
    r"(?P<year>\d{4})?,?\s*"
    rf"(?:at\s+)?{_RESET_CLOCK_NAMED}"
    r"\s*(?:\(?\s*UTC\s*\)?)?",
    re.IGNORECASE,
)
LIMIT_WALL_RESET_ISO_PATTERN = re.compile(
    r"(?:try\s+again|resets?|available\s+again)\s+(?:at\s+|on\s+)?"
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[T\s](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::\d{2})?"
    r"\s*(?:Z|UTC|\+00:?00)?",
    re.IGNORECASE,
)


def _delay_until(
    reset: datetime.datetime,
    now: datetime.datetime | None,
) -> float:
    current = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    delay = (reset - current).total_seconds() + QUOTA_RESET_MARGIN_SECONDS
    return min(max(delay, 0.0), QUOTA_RESET_MAX_DELAY_SECONDS)


def _reset_delay_from_date_match(
    match: re.Match[str],
    now: datetime.datetime | None,
) -> float | None:
    groups = match.groupdict()
    month_text = groups["month"].lower()
    month = (
        int(month_text) if month_text.isdigit() else _MONTH_NUMBERS.get(month_text[:3])
    )
    if month is None or not 1 <= month <= 12:
        return None
    day = int(groups["day"])
    current = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    year = int(groups["year"]) if groups.get("year") else current.year
    hour = int(groups["hour"])
    minute = int(groups.get("minute") or 0)
    meridiem = (groups.get("meridiem") or "").lower()
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    try:
        reset = datetime.datetime(
            year, month, day, hour, minute, tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None
    return _delay_until(reset, now)


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
    return _reset_delay_from_match(match, now)


# Provider "limit wall" deaths: the worker process exits nonzero after the
# provider refuses further work for a usage/session cap (distinct from the
# transient 429/overloaded errors above, which retry quickly). Re-dispatching
# straight into the same wall burns restart budget and stalls the loop, so
# these are classified separately and paused until the advertised reset.
DEFAULT_LIMIT_WALL_PATTERNS: tuple[str, ...] = (
    r"you'?ve (?:reached|hit) your [^.\n]{0,60}?\blimit\b",
    r"\b(?:usage|session|weekly|5-hour)\s+limit\s+(?:reached|exceeded)\b",
)
LIMIT_WALL_DEFAULT_BACKOFF_SECONDS = 1800.0
# The advertised reset ("· resets 1am (UTC)") follows the limit phrase in the
# same message. Scope the reset search to a window right after the matched
# marker so an unrelated "reset at N" elsewhere in the tail cannot inflate the
# pause.
LIMIT_WALL_RESET_WINDOW_CHARS = 200


@dataclasses.dataclass(frozen=True)
class LimitWallSignal:
    """A detected provider limit wall in captured agent output.

    ``marker`` is the matched limit phrase. ``reset_text`` is the advertised
    reset phrase (e.g. "resets 1am (UTC)") when present, and ``reset_delay`` is
    the parsed seconds until that reset, or None when the wall carries no
    parseable reset time.
    """

    marker: str
    reset_text: str = ""
    reset_delay: float | None = None


def compile_limit_wall_patterns(
    patterns: Iterable[str] | None = None,
) -> tuple[re.Pattern[str], ...]:
    source = tuple(patterns) if patterns is not None else DEFAULT_LIMIT_WALL_PATTERNS
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in source)


def parse_limit_wall_reset_delay(
    text: str,
    *,
    now: datetime.datetime | None = None,
) -> float | None:
    """Seconds until a limit-wall reset (e.g. "resets 1am (UTC)", "reset at 3pm").

    Unlike parse_quota_reset_delay, the UTC marker is optional here because the
    caller has already confirmed a limit-wall phrase. Returns None when no reset
    time is present, so the caller falls back to the configured backoff.
    """
    found = search_limit_wall_reset(text, now=now)
    return None if found is None else found[1]


def search_limit_wall_reset(
    text: str,
    *,
    now: datetime.datetime | None = None,
) -> tuple[str, float] | None:
    """Find an advertised reset instant in ``text``.

    Absolute calendar resets are tried before wall-clock resets: a multi-day
    account limit ("try again at Jul 25th, 2026 3:24 AM") shares its clock
    digits with the clock-only form, and reading it as a same-or-next-day time
    would understate the wait by days. Returns the matched phrase and the
    seconds until it, or None when no reset time is present.
    """
    for pattern, extract in (
        (LIMIT_WALL_RESET_ISO_PATTERN, _reset_delay_from_date_match),
        (LIMIT_WALL_RESET_DATE_PATTERN, _reset_delay_from_date_match),
        (LIMIT_WALL_RESET_PATTERN, _reset_delay_from_match),
    ):
        match = pattern.search(text)
        if match is None:
            continue
        delay = extract(match, now)
        if delay is None:
            continue
        return match.group(0).strip(), delay
    return None


def detect_limit_wall(
    text: str,
    patterns: Iterable[str] | None = None,
    *,
    now: datetime.datetime | None = None,
) -> LimitWallSignal | None:
    """Detect a provider limit wall in ``text``.

    ``patterns`` overrides the default limit-phrase patterns; None uses
    DEFAULT_LIMIT_WALL_PATTERNS. Returns a LimitWallSignal with any advertised
    reset time attached, or None when no limit phrase matches.
    """
    for pattern in compile_limit_wall_patterns(patterns):
        match = pattern.search(text)
        if match is None:
            continue
        window = text[match.start() : match.end() + LIMIT_WALL_RESET_WINDOW_CHARS]
        found = search_limit_wall_reset(window, now=now)
        return LimitWallSignal(
            marker=match.group(0).strip(),
            reset_text=found[0] if found is not None else "",
            reset_delay=found[1] if found is not None else None,
        )
    return None


def limit_wall_backoff_seconds(
    signal: LimitWallSignal,
    default_backoff: float = LIMIT_WALL_DEFAULT_BACKOFF_SECONDS,
) -> float:
    """Seconds to pause dispatch after a limit wall.

    Uses the advertised reset delay when the wall carried one, otherwise the
    configured default backoff. The reset delay already includes a safety
    margin and is capped by parse_quota_reset_delay.
    """
    if signal.reset_delay is not None:
        return signal.reset_delay
    return max(0.0, default_backoff)


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


def subprocess_result_output(result: subprocess.CompletedProcess[str]) -> str:
    """Combined stdout+stderr of a finished subprocess.

    Agent CLIs disagree about which stream carries a refusal notice, so
    limit-wall detection must read both.
    """
    parts = [text for text in (result.stdout, result.stderr) if text]
    return "\n".join(parts)


def limit_wall_from_result(
    result: subprocess.CompletedProcess[str],
    patterns: Iterable[str] | None = None,
    *,
    now: datetime.datetime | None = None,
) -> LimitWallSignal | None:
    """Detect a provider limit wall in a nonzero subprocess result.

    A limit wall is terminal for the advertised window: its text also matches
    the transient patterns (it mentions "limit"/"quota"), so it must be checked
    before transient classification or the caller burns its whole retry budget
    against a wall that cannot clear for hours. A zero exit is never a wall,
    which keeps a successful run that merely quotes a limit phrase off this
    path.
    """
    if result.returncode == 0:
        return None
    output = subprocess_result_output(result)
    if not output:
        return None
    return detect_limit_wall(output, patterns, now=now)


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
    detect_limit_walls: bool = True,
    limit_wall_patterns: Iterable[str] | None = None,
    on_limit_wall: Callable[[LimitWallSignal], None] | None = None,
    **subprocess_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    interrupt_process_group = bool(
        subprocess_kwargs.pop("interrupt_process_group", False)
    )
    max_retries = max(max_retries, 0)
    for attempt in range(max_retries + 1):
        try:
            if interrupt_process_group:
                result = run_interruptible_subprocess(cmd, **subprocess_kwargs)
            else:
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

        if detect_limit_walls:
            wall = limit_wall_from_result(result, limit_wall_patterns)
            if wall is not None:
                if on_limit_wall is not None:
                    on_limit_wall(wall)
                return result

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


def run_interruptible_subprocess(
    cmd: str | list[str],
    **subprocess_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    timeout = subprocess_kwargs.pop("timeout", None)
    if os.name != "nt":
        subprocess_kwargs["start_new_session"] = True
    with subprocess.Popen(cmd, **subprocess_kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_interruptible_process_group(process)
            process.communicate()
            raise
        except KeyboardInterrupt:
            terminate_interruptible_process_group(process)
            raise
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )


def terminate_interruptible_process_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = 5.0,
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        kill_interruptible_process_group(process)
        process.wait()


def kill_interruptible_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
