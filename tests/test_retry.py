from __future__ import annotations

import errno
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from vibe_loop.retry import (
    DEFAULT_BASE_DELAY,
    DEFAULT_JITTER,
    DEFAULT_MAX_DELAY,
    backoff_delay,
    is_transient_oserror,
    is_transient_stderr,
    is_transient_subprocess_result,
    retry_subprocess_run,
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
