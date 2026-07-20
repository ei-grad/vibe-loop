from __future__ import annotations

import datetime
import errno
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from vibe_loop.retry import (
    DEFAULT_BASE_DELAY,
    DEFAULT_JITTER,
    DEFAULT_MAX_DELAY,
    LIMIT_WALL_DEFAULT_BACKOFF_SECONDS,
    QUOTA_RESET_MARGIN_SECONDS,
    QUOTA_RESET_MAX_DELAY_SECONDS,
    LimitWallSignal,
    backoff_delay,
    detect_limit_wall,
    is_transient_oserror,
    is_transient_stderr,
    is_transient_subprocess_result,
    limit_wall_backoff_seconds,
    limit_wall_from_result,
    parse_limit_wall_reset_delay,
    parse_quota_reset_delay,
    retry_subprocess_run,
)

# The exact wall the detached autopilot planner hit on 2026-07-20: a
# multi-day account limit whose reset is an absolute calendar instant.
OBSERVED_USAGE_WALL = (
    "You've hit your usage limit. Your limit will reset and you can "
    "try again at Jul 25th, 2026 3:24 AM."
)


class QuotaResetDelayTests(unittest.TestCase):
    def test_parses_am_pm_reset_time(self) -> None:
        import datetime

        now = datetime.datetime(2026, 6, 10, 0, 50, tzinfo=datetime.timezone.utc)
        delay = parse_quota_reset_delay(
            "You've hit your session limit · resets 2:40am (UTC)", now=now
        )
        self.assertIsNotNone(delay)
        self.assertAlmostEqual(delay, 110 * 60 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0)

    def test_parses_hour_only_pm_time(self) -> None:
        import datetime

        now = datetime.datetime(2026, 6, 10, 20, 0, tzinfo=datetime.timezone.utc)
        delay = parse_quota_reset_delay("limit resets 11pm (UTC)", now=now)
        self.assertIsNotNone(delay)
        self.assertAlmostEqual(delay, 3 * 3600 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0)

    def test_reset_time_already_passed_rolls_to_next_day_capped(self) -> None:
        import datetime

        now = datetime.datetime(2026, 6, 10, 23, 0, tzinfo=datetime.timezone.utc)
        delay = parse_quota_reset_delay("resets 1:00am (UTC)", now=now)
        self.assertIsNotNone(delay)
        self.assertLessEqual(delay, QUOTA_RESET_MAX_DELAY_SECONDS)
        self.assertAlmostEqual(delay, 2 * 3600 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0)

    def test_far_future_reset_is_capped(self) -> None:
        import datetime

        now = datetime.datetime(2026, 6, 10, 1, 0, tzinfo=datetime.timezone.utc)
        delay = parse_quota_reset_delay("resets 11:59pm (UTC)", now=now)
        self.assertEqual(delay, QUOTA_RESET_MAX_DELAY_SECONDS)

    def test_no_reset_time_returns_none(self) -> None:
        self.assertIsNone(parse_quota_reset_delay("rate limit exceeded"))
        self.assertIsNone(parse_quota_reset_delay("plain failure output"))

    def test_invalid_times_return_none(self) -> None:
        self.assertIsNone(parse_quota_reset_delay("resets 13:00pm (UTC)"))
        self.assertIsNone(parse_quota_reset_delay("resets 25:00 (UTC)"))


UTC = datetime.timezone.utc


class LimitWallDetectionTests(unittest.TestCase):
    def test_detects_usage_cap_message(self) -> None:
        signal = detect_limit_wall(
            "output...\nYou've reached your Fable 5 limit · switch models"
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.marker, "You've reached your Fable 5 limit")

    def test_detects_session_limit_message(self) -> None:
        signal = detect_limit_wall("You've hit your session limit · resets 1am (UTC)")
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.marker, "You've hit your session limit")

    def test_ignores_non_limit_output(self) -> None:
        self.assertIsNone(detect_limit_wall("worker completed the slice; all green"))
        # A generic "rate limit" 429 is a transient failure, not a limit wall.
        self.assertIsNone(detect_limit_wall("Error: 429 rate limit exceeded"))

    def test_reset_time_with_utc_is_parsed(self) -> None:
        now = datetime.datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        signal = detect_limit_wall(
            "You've hit your session limit · resets 1am (UTC)", now=now
        )
        assert signal is not None
        self.assertEqual(signal.reset_text, "resets 1am (UTC)")
        self.assertAlmostEqual(
            signal.reset_delay, 3600 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0
        )

    def test_reset_time_without_utc_is_parsed(self) -> None:
        now = datetime.datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
        signal = detect_limit_wall(
            "You've reached your limit. Your limit will reset at 3pm", now=now
        )
        assert signal is not None
        self.assertIsNotNone(signal.reset_delay)
        self.assertAlmostEqual(
            signal.reset_delay, 2 * 3600 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0
        )

    def test_unparseable_reset_leaves_delay_none(self) -> None:
        signal = detect_limit_wall("You've hit your session limit")
        assert signal is not None
        self.assertEqual(signal.reset_text, "")
        self.assertIsNone(signal.reset_delay)

    def test_distant_reset_phrase_is_not_attached(self) -> None:
        # An unrelated "reset at N" far from the limit phrase must not inflate
        # the pause: the reset search is scoped to a window after the marker.
        text = "You've hit your session limit " + ("x" * 300) + " reset at 3pm"
        signal = detect_limit_wall(text)
        assert signal is not None
        self.assertEqual(signal.reset_text, "")
        self.assertIsNone(signal.reset_delay)

    def test_detects_observed_usage_wall_with_absolute_reset(self) -> None:
        # The wall that motivated this work: "usage limit" (not "session"), and
        # a reset given as a calendar instant days out rather than a wall clock.
        now = datetime.datetime(2026, 7, 20, 10, 19, tzinfo=datetime.timezone.utc)
        signal = detect_limit_wall(OBSERVED_USAGE_WALL, now=now)
        assert signal is not None
        self.assertIn("usage limit", signal.marker.lower())
        self.assertIn("Jul 25th", signal.reset_text)
        assert signal.reset_delay is not None
        # The full ~4.7-day wait must survive: a calendar reset carries a
        # self-validating date, so truncating it to the clock-only misparse cap
        # would report a wake four days before the limit actually clears.
        self.assertAlmostEqual(
            signal.reset_delay,
            (
                datetime.datetime(2026, 7, 25, 3, 24, tzinfo=datetime.timezone.utc)
                - now
            ).total_seconds()
            + QUOTA_RESET_MARGIN_SECONDS,
            delta=1.0,
        )
        self.assertGreater(signal.reset_delay, QUOTA_RESET_MAX_DELAY_SECONDS)

    def test_absolute_reset_within_the_cap_is_preserved(self) -> None:
        # A near-term calendar reset must survive intact: it is under the cap,
        # so the supervisor sleeps to the real advertised instant.
        now = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=datetime.timezone.utc)
        signal = detect_limit_wall(
            "You've hit your usage limit, try again at Jul 20th, 2026 1:30 PM",
            now=now,
        )
        assert signal is not None
        self.assertAlmostEqual(
            signal.reset_delay,
            3.5 * 3600 + QUOTA_RESET_MARGIN_SECONDS,
            delta=1.0,
        )

    def test_elapsed_absolute_reset_falls_back_to_configured_backoff(self) -> None:
        # Recorded wall messages are re-parsed long after the fact. An elapsed
        # reset must not read as "wait zero seconds", which would let the
        # supervisor re-dispatch in a tight loop against a wall that may still
        # be standing; it reports no usable reset so the caller backs off.
        now = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=datetime.timezone.utc)
        self.assertIsNone(
            parse_limit_wall_reset_delay("try again at Jul 20th, 2026 3:24 AM", now=now)
        )
        signal = detect_limit_wall(
            "You've hit your usage limit, try again at Jul 20th, 2026 3:24 AM",
            now=now,
        )
        assert signal is not None
        self.assertIsNone(signal.reset_delay)
        self.assertEqual(limit_wall_backoff_seconds(signal, 1800.0), 1800.0)

    def test_iso_reset_timestamp_is_parsed(self) -> None:
        now = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=datetime.timezone.utc)
        delay = parse_limit_wall_reset_delay(
            "try again at 2026-07-20T12:00:00Z", now=now
        )
        assert delay is not None
        self.assertAlmostEqual(delay, 2 * 3600 + QUOTA_RESET_MARGIN_SECONDS, delta=1.0)

    def test_custom_patterns_override_defaults(self) -> None:
        # A custom list fully replaces the defaults: the default phrases no
        # longer match, and the custom phrase does.
        self.assertIsNone(
            detect_limit_wall(
                "You've hit your session limit", ["provider wall reached"]
            )
        )
        signal = detect_limit_wall(
            "the provider wall reached for now", ["provider wall reached"]
        )
        self.assertIsNotNone(signal)

    def test_far_future_reset_is_capped(self) -> None:
        now = datetime.datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        delay = parse_limit_wall_reset_delay("reset at 11:59pm", now=now)
        self.assertEqual(delay, QUOTA_RESET_MAX_DELAY_SECONDS)


class LimitWallBackoffTests(unittest.TestCase):
    def test_uses_reset_delay_when_present(self) -> None:
        signal = LimitWallSignal(
            marker="wall", reset_text="resets 1am", reset_delay=900.0
        )
        self.assertEqual(limit_wall_backoff_seconds(signal, 1800.0), 900.0)

    def test_falls_back_to_default_backoff(self) -> None:
        signal = LimitWallSignal(marker="wall")
        self.assertEqual(limit_wall_backoff_seconds(signal, 1800.0), 1800.0)

    def test_default_backoff_constant(self) -> None:
        signal = LimitWallSignal(marker="wall")
        self.assertEqual(
            limit_wall_backoff_seconds(signal), LIMIT_WALL_DEFAULT_BACKOFF_SECONDS
        )


class TransientStderrDetectionTests(unittest.TestCase):
    def test_detects_rate_limit_patterns(self) -> None:
        self.assertTrue(is_transient_stderr("Error: 429 Too Many Requests"))
        self.assertTrue(is_transient_stderr("rate limit exceeded"))
        self.assertTrue(is_transient_stderr("Rate-limit hit, try later"))
        self.assertTrue(is_transient_stderr("rate_limit_error"))
        self.assertTrue(is_transient_stderr("too many requests"))

    def test_detects_server_error_patterns(self) -> None:
        self.assertTrue(is_transient_stderr("HTTP 500 Internal Server Error"))
        self.assertTrue(is_transient_stderr("502 Bad Gateway"))
        self.assertTrue(is_transient_stderr("503 Service Unavailable"))
        self.assertTrue(is_transient_stderr("529 overloaded"))
        self.assertTrue(is_transient_stderr("server error occurred"))
        self.assertTrue(is_transient_stderr("internal server error"))
        self.assertTrue(is_transient_stderr("bad gateway"))

    def test_detects_quota_and_capacity_patterns(self) -> None:
        self.assertTrue(is_transient_stderr("quota exceeded for this model"))
        self.assertTrue(is_transient_stderr("capacity limit reached"))
        self.assertTrue(is_transient_stderr("overloaded, please wait"))
        self.assertTrue(is_transient_stderr("API throttled"))
        self.assertTrue(is_transient_stderr("resource_exhausted"))

    def test_detects_session_and_usage_limit_patterns(self) -> None:
        self.assertTrue(
            is_transient_stderr("You've hit your session limit · resets 2:40am (UTC)")
        )
        self.assertTrue(is_transient_stderr("usage limit reached"))
        self.assertTrue(is_transient_stderr("weekly limit will reset"))
        self.assertTrue(is_transient_stderr("You've hit your 5-hour limit"))

    def test_detects_connection_errors(self) -> None:
        self.assertTrue(is_transient_stderr("ECONNRESET"))
        self.assertTrue(is_transient_stderr("ETIMEDOUT"))
        self.assertTrue(is_transient_stderr("connection reset by peer"))
        self.assertTrue(is_transient_stderr("connection refused"))
        self.assertTrue(is_transient_stderr("connection timed out"))
        self.assertTrue(is_transient_stderr("temporarily unavailable"))
        self.assertTrue(is_transient_stderr("try again later"))

    def test_does_not_match_non_transient_errors(self) -> None:
        self.assertFalse(is_transient_stderr("syntax error in line 42"))
        self.assertFalse(is_transient_stderr("file not found: config.yaml"))
        self.assertFalse(is_transient_stderr("permission denied"))
        self.assertFalse(is_transient_stderr("invalid argument"))
        self.assertFalse(is_transient_stderr(""))

    def test_does_not_match_bare_numbers_in_context(self) -> None:
        self.assertFalse(is_transient_stderr("processed 500 items successfully"))
        self.assertFalse(is_transient_stderr("502 items processed"))
        self.assertFalse(is_transient_stderr("429 connections"))

    def test_detects_http_version_status_format(self) -> None:
        self.assertTrue(is_transient_stderr("HTTP/1.1 503 Service Unavailable"))
        self.assertTrue(is_transient_stderr("HTTP/2 429"))


class TransientOSErrorTests(unittest.TestCase):
    def test_transient_errnos(self) -> None:
        self.assertTrue(is_transient_oserror(OSError(errno.EAGAIN, "try again")))
        self.assertTrue(is_transient_oserror(OSError(errno.ENOMEM, "out of memory")))
        self.assertTrue(is_transient_oserror(OSError(errno.EMFILE, "too many files")))
        self.assertTrue(is_transient_oserror(OSError(errno.ENFILE, "file table full")))

    def test_non_transient_errnos(self) -> None:
        self.assertFalse(is_transient_oserror(OSError(errno.ENOENT, "not found")))
        self.assertFalse(is_transient_oserror(OSError(errno.EACCES, "denied")))
        self.assertFalse(is_transient_oserror(OSError(errno.EPERM, "not permitted")))


class TransientSubprocessResultTests(unittest.TestCase):
    def test_success_is_not_transient(self) -> None:
        result = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout="", stderr="rate limit"
        )
        self.assertFalse(is_transient_subprocess_result(result))

    def test_failure_with_transient_stderr_is_transient(self) -> None:
        result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="Error: 429 rate limit"
        )
        self.assertTrue(is_transient_subprocess_result(result))

    def test_failure_without_transient_stderr_is_not_transient(self) -> None:
        result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="syntax error"
        )
        self.assertFalse(is_transient_subprocess_result(result))

    def test_failure_with_no_stderr_is_not_transient(self) -> None:
        result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr=""
        )
        self.assertFalse(is_transient_subprocess_result(result))


class BackoffDelayTests(unittest.TestCase):
    def test_exponential_growth(self) -> None:
        delays = [backoff_delay(i, jitter=0.0) for i in range(5)]
        self.assertAlmostEqual(delays[0], 10.0)
        self.assertAlmostEqual(delays[1], 20.0)
        self.assertAlmostEqual(delays[2], 40.0)
        self.assertAlmostEqual(delays[3], 80.0)
        self.assertAlmostEqual(delays[4], 120.0)

    def test_max_delay_cap(self) -> None:
        delay = backoff_delay(10, max_delay=60.0, jitter=0.0)
        self.assertAlmostEqual(delay, 60.0)

    def test_jitter_bounds(self) -> None:
        for attempt in range(4):
            base = min(DEFAULT_BASE_DELAY * (2**attempt), DEFAULT_MAX_DELAY)
            for _ in range(50):
                delay = backoff_delay(attempt)
                jitter_range = base * DEFAULT_JITTER
                self.assertGreaterEqual(delay, base - jitter_range)
                self.assertLessEqual(delay, base + jitter_range)

    def test_never_negative(self) -> None:
        delay = backoff_delay(0, base_delay=0.01, jitter=1.0)
        self.assertGreaterEqual(delay, 0.0)

    def test_custom_parameters(self) -> None:
        delay = backoff_delay(0, base_delay=5.0, max_delay=30.0, jitter=0.0)
        self.assertAlmostEqual(delay, 5.0)


class RetrySubprocessRunTests(unittest.TestCase):
    def test_interruptible_run_terminates_process_group_before_propagating(
        self,
    ) -> None:
        process = MagicMock(pid=5151)
        process.__enter__.return_value = process
        process.communicate.side_effect = KeyboardInterrupt
        with (
            patch("vibe_loop.retry.subprocess.Popen", return_value=process),
            patch("vibe_loop.retry.terminate_interruptible_process_group") as terminate,
            self.assertRaises(KeyboardInterrupt),
        ):
            retry_subprocess_run(
                ["analysis-agent"],
                interrupt_process_group=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        terminate.assert_called_once_with(process)

    def test_success_on_first_attempt(self) -> None:
        fake_sleep = MagicMock()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["test"], returncode=0, stdout="ok", stderr=""
            )
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(mock_run.call_count, 1)
        fake_sleep.assert_not_called()

    def test_retries_on_transient_failure_then_succeeds(self) -> None:
        fake_sleep = MagicMock()
        on_retry = MagicMock()
        transient_result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="429 rate limit"
        )
        success_result = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", side_effect=[transient_result, success_result]):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=3,
                sleep=fake_sleep,
                on_retry=on_retry,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(fake_sleep.call_count, 1)
        on_retry.assert_called_once()
        self.assertEqual(on_retry.call_args[0][0], 1)

    def test_limit_wall_is_not_retried_as_a_transient(self) -> None:
        # Regression: the observed wall matches the transient patterns
        # ("usage limit"), so before limit-wall classification it burned all
        # three jittered retries against a wall that could not clear for days.
        fake_sleep = MagicMock()
        on_retry = MagicMock()
        walls: list[LimitWallSignal] = []
        wall_result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr=OBSERVED_USAGE_WALL
        )
        with patch("subprocess.run", return_value=wall_result) as mock_run:
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=3,
                sleep=fake_sleep,
                on_retry=on_retry,
                detect_limit_walls=True,
                on_limit_wall=walls.append,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mock_run.call_count, 1)
        fake_sleep.assert_not_called()
        on_retry.assert_not_called()
        self.assertEqual(len(walls), 1)
        self.assertGreater(walls[0].reset_delay, QUOTA_RESET_MAX_DELAY_SECONDS)

    def test_limit_wall_on_stdout_is_detected(self) -> None:
        # Agent CLIs differ on which stream carries the refusal notice.
        fake_sleep = MagicMock()
        walls: list[LimitWallSignal] = []
        wall_result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout=OBSERVED_USAGE_WALL, stderr=""
        )
        with patch("subprocess.run", return_value=wall_result) as mock_run:
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=3,
                sleep=fake_sleep,
                detect_limit_walls=True,
                on_limit_wall=walls.append,
            )
        self.assertEqual(mock_run.call_count, 1)
        fake_sleep.assert_not_called()
        self.assertEqual(len(walls), 1)

    def test_ordinary_transients_still_retry_with_wall_detection_on(self) -> None:
        # Acceptance guard: limit-wall classification must not swallow the
        # short-transient retries that 429/5xx/capacity failures depend on.
        for stderr, carries_wall_phrase in (
            ("429 Too Many Requests", False),
            ("503 Service Unavailable", False),
            ("Overloaded, please retry", False),
            ("ECONNRESET", False),
            ("at capacity", False),
            # Collision cases: these carry a wall phrase but are independently
            # transient and advertise no reset, so they must keep their
            # retries rather than buying a half-hour pause for a 30s outage.
            (
                "Error 429: Too Many Requests. usage limit exceeded, retry after 30s",
                True,
            ),
            ("503: weekly limit reached, service temporarily unavailable", True),
        ):
            with self.subTest(stderr=stderr):
                fake_sleep = MagicMock()
                walls: list[LimitWallSignal] = []
                transient = subprocess.CompletedProcess(
                    args=["test"], returncode=1, stdout="", stderr=stderr
                )
                with patch("subprocess.run", return_value=transient) as mock_run:
                    retry_subprocess_run(
                        ["test"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        max_retries=2,
                        sleep=fake_sleep,
                        detect_limit_walls=True,
                        on_limit_wall=walls.append,
                    )
                self.assertEqual(mock_run.call_count, 3)
                self.assertEqual(fake_sleep.call_count, 2)
                # The retries come first either way. A body that never named a
                # limit stays a plain transient failure; one that did is
                # surfaced once, after its retries are spent, so the caller
                # backs off instead of redispatching into the same refusal.
                self.assertEqual(len(walls), 1 if carries_wall_phrase else 0)

    def test_stdout_only_throttling_body_keeps_its_retries(self) -> None:
        # Wall detection reads stdout and stderr, so the recoverability check
        # must read the same combined output. A throttling body that arrives
        # only on stdout is no less recoverable than one on stderr, and
        # classifying it as a terminal wall would cost it every retry.
        fake_sleep = MagicMock()
        walls: list[LimitWallSignal] = []
        stdout_only = subprocess.CompletedProcess(
            args=["test"],
            returncode=1,
            stdout="Error 429: Too Many Requests. usage limit exceeded, retry after 30s",
            stderr="",
        )
        with patch("subprocess.run", return_value=stdout_only) as mock_run:
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
                detect_limit_walls=True,
                on_limit_wall=walls.append,
            )
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(fake_sleep.call_count, 2)
        self.assertEqual(len(walls), 1)

    def test_reset_less_wall_that_is_not_transient_still_short_circuits(self) -> None:
        # A wall phrase with no reset and no independent transient marker has
        # nothing suggesting recoverability, so it stays terminal and backs off
        # on the configured default rather than retrying into the wall. Note
        # the phrasing: most reset-less wall texts also match a transient
        # pattern and therefore keep their retries (see the collision cases in
        # test_ordinary_transients_still_retry_with_wall_detection_on); this
        # one does not, so it is the branch that still short-circuits.
        fake_sleep = MagicMock()
        walls: list[LimitWallSignal] = []
        no_reset = subprocess.CompletedProcess(
            args=["test"],
            returncode=1,
            stdout="",
            stderr="You've reached your Fable 5 limit · switch models",
        )
        with patch("subprocess.run", return_value=no_reset) as mock_run:
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
                detect_limit_walls=True,
                on_limit_wall=walls.append,
            )
        self.assertEqual(mock_run.call_count, 1)
        fake_sleep.assert_not_called()
        self.assertEqual(len(walls), 1)
        self.assertIsNone(walls[0].reset_delay)
        self.assertEqual(
            limit_wall_backoff_seconds(walls[0], 1800.0),
            1800.0,
        )

    def test_year_less_calendar_reset_rolls_across_the_year_boundary(self) -> None:
        # "Jan 2nd" read on Dec 31 must not parse as ~363 days in the past,
        # which previously collapsed to a zero pause and spun the supervisor.
        now = datetime.datetime(2026, 12, 31, 23, 0, tzinfo=datetime.timezone.utc)
        signal = detect_limit_wall(
            "You've hit your usage limit. try again at Jan 2nd 3:00 AM.", now=now
        )
        assert signal is not None
        assert signal.reset_delay is not None
        self.assertAlmostEqual(
            signal.reset_delay,
            28 * 3600 + QUOTA_RESET_MARGIN_SECONDS,
            delta=1.0,
        )

    def test_year_less_reset_hours_in_the_past_is_elapsed_not_next_year(self) -> None:
        # A same-day reset already behind the clock is simply elapsed. Rolling
        # every past year-less date into the next year turned it into a
        # ~365-day wait, capped to a false seven-day pause, instead of falling
        # back to the configured backoff.
        now = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=datetime.timezone.utc)
        signal = detect_limit_wall(
            "You've hit your usage limit. try again at Jul 20th 3:24 AM.", now=now
        )
        assert signal is not None
        self.assertIsNone(signal.reset_delay)
        self.assertEqual(limit_wall_backoff_seconds(signal, 1800.0), 1800.0)

    def test_year_less_reset_months_in_the_past_is_elapsed(self) -> None:
        # Well short of the year boundary: rolling "Mar 1" seen in July into
        # next March advertises a wait no limit wall ever means.
        now = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=datetime.timezone.utc)
        signal = detect_limit_wall(
            "You've hit your usage limit. try again at Mar 1 3:00 AM.", now=now
        )
        assert signal is not None
        self.assertIsNone(signal.reset_delay)

    def test_wall_detection_is_opt_in(self) -> None:
        # Detection must not silently change unrelated call sites (task
        # selection, generated profiles) that never asked for it.
        fake_sleep = MagicMock()
        wall_result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr=OBSERVED_USAGE_WALL
        )
        with patch("subprocess.run", return_value=wall_result) as mock_run:
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
            )
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(fake_sleep.call_count, 2)

    def test_limit_wall_detection_can_be_disabled(self) -> None:
        fake_sleep = MagicMock()
        walls: list[LimitWallSignal] = []
        wall_result = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr=OBSERVED_USAGE_WALL
        )
        with patch("subprocess.run", return_value=wall_result) as mock_run:
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
                detect_limit_walls=False,
                on_limit_wall=walls.append,
            )
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(walls, [])

    def test_zero_exit_output_quoting_a_limit_phrase_is_not_a_wall(self) -> None:
        # A worker that succeeds while implementing limit handling must not be
        # classified as having hit a wall.
        success = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout=OBSERVED_USAGE_WALL, stderr=""
        )
        self.assertIsNone(limit_wall_from_result(success))

    def test_returns_last_result_after_max_retries(self) -> None:
        fake_sleep = MagicMock()
        transient = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="503 Service Unavailable"
        )
        with patch("subprocess.run", return_value=transient):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fake_sleep.call_count, 2)

    def test_no_retry_on_non_transient_failure(self) -> None:
        fake_sleep = MagicMock()
        non_transient = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="syntax error at line 5"
        )
        with patch("subprocess.run", return_value=non_transient):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=3,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 1)
        fake_sleep.assert_not_called()

    def test_retries_on_timeout_then_succeeds(self) -> None:
        fake_sleep = MagicMock()
        success_result = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout="ok", stderr=""
        )
        with patch(
            "subprocess.run",
            side_effect=[
                subprocess.TimeoutExpired(cmd="test", timeout=900),
                success_result,
            ],
        ):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
                max_retries=2,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(fake_sleep.call_count, 1)

    def test_raises_timeout_after_max_retries(self) -> None:
        fake_sleep = MagicMock()
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="test", timeout=900),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                retry_subprocess_run(
                    ["test"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=900,
                    max_retries=2,
                    sleep=fake_sleep,
                )
        self.assertEqual(fake_sleep.call_count, 2)

    def test_retries_transient_oserror(self) -> None:
        fake_sleep = MagicMock()
        success_result = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout="ok", stderr=""
        )
        with patch(
            "subprocess.run",
            side_effect=[
                OSError(errno.EAGAIN, "try again"),
                success_result,
            ],
        ):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=2,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 0)

    def test_raises_non_transient_oserror_immediately(self) -> None:
        fake_sleep = MagicMock()
        with patch(
            "subprocess.run",
            side_effect=OSError(errno.ENOENT, "not found"),
        ):
            with self.assertRaises(OSError):
                retry_subprocess_run(
                    ["test"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    max_retries=3,
                    sleep=fake_sleep,
                )
        fake_sleep.assert_not_called()

    def test_on_retry_receives_correct_arguments(self) -> None:
        fake_sleep = MagicMock()
        on_retry = MagicMock()
        transient = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="quota exceeded"
        )
        success = subprocess.CompletedProcess(
            args=["test"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", side_effect=[transient, transient, success]):
            retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=3,
                sleep=fake_sleep,
                on_retry=on_retry,
            )
        self.assertEqual(on_retry.call_count, 2)
        self.assertEqual(on_retry.call_args_list[0][0][0], 1)
        self.assertEqual(on_retry.call_args_list[1][0][0], 2)
        self.assertGreater(on_retry.call_args_list[0][0][1], 0)
        self.assertIn("quota", on_retry.call_args_list[0][0][2])

    def test_zero_max_retries_no_retry(self) -> None:
        fake_sleep = MagicMock()
        transient = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="error 429 rate limit"
        )
        with patch("subprocess.run", return_value=transient):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=0,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 1)
        fake_sleep.assert_not_called()

    def test_negative_max_retries_treated_as_zero(self) -> None:
        fake_sleep = MagicMock()
        transient = subprocess.CompletedProcess(
            args=["test"], returncode=1, stdout="", stderr="error 429 rate limit"
        )
        with patch("subprocess.run", return_value=transient):
            result = retry_subprocess_run(
                ["test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                max_retries=-1,
                sleep=fake_sleep,
            )
        self.assertEqual(result.returncode, 1)
        fake_sleep.assert_not_called()
