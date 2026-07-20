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
    parse_limit_wall_reset_delay,
    parse_quota_reset_delay,
    retry_subprocess_run,
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
        text = "You've hit your session limit" + ("x" * 300) + " reset at 3pm"
        signal = detect_limit_wall(text)
        assert signal is not None
        self.assertEqual(signal.reset_text, "")
        self.assertIsNone(signal.reset_delay)

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
