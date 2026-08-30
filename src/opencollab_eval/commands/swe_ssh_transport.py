"""Bounded retry policy for SSH failures before remote command execution."""

from __future__ import annotations

import math
import operator
import subprocess
import time
from pathlib import Path

from opencollab_eval.commands.swe_v1_prolite_common import _redacted


class CheckedCommandError(RuntimeError):
    """A failed command with enough structured state for transport policy."""

    def __init__(
        self,
        command: list[str],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.command = tuple(command)
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.ssh_attempts = 1
        self.ssh_failure_kind = ""
        message = _redacted(
            result.stderr
            or result.stdout
            or f"{command[0]} exited {result.returncode}"
        )
        super().__init__(message)

    def __str__(self) -> str:
        message = super().__str__()
        if self.ssh_attempts > 1:
            return (
                f"{message} "
                f"(ssh_attempts={self.ssh_attempts}, "
                f"failure_kind={self.ssh_failure_kind or 'non_retryable'})"
            )
        return message


_RETRYABLE_SSH_TRANSPORT_MARKERS = (
    ("banner_timeout", "connection timed out during banner exchange"),
    ("key_exchange_reset", "kex_exchange_identification"),
    ("key_exchange_reset", "ssh_exchange_identification"),
    ("connection_refused", "connection refused"),
    ("route_unavailable", "no route to host"),
    ("dns_unavailable", "could not resolve hostname"),
)
_IDEMPOTENT_DISCONNECT_MARKERS = (
    "connection closed by",
    "connection reset by peer",
    "broken pipe",
)


def run_checked(
    command: list[str],
    *,
    timeout: int = 120,
    input_text: str | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise CheckedCommandError(command, result)
    return result


def retryable_ssh_transport_failure(error: CheckedCommandError) -> str:
    """Classify failures proven to occur before a remote command can run."""
    if error.returncode != 255:
        return ""
    detail = f"{error.stderr}\n{error.stdout}".lower()
    for failure_kind, marker in _RETRYABLE_SSH_TRANSPORT_MARKERS:
        if marker in detail:
            return failure_kind
    return ""


def idempotent_ssh_disconnect_failure(error: CheckedCommandError) -> str:
    """Classify an ambiguous disconnect that is safe only for read-only commands."""
    if error.returncode != 255:
        return ""
    detail = f"{error.stderr}\n{error.stdout}".lower()
    return (
        "idempotent_disconnect"
        if any(marker in detail for marker in _IDEMPOTENT_DISCONNECT_MARKERS)
        else ""
    )


def run_ssh_checked(
    command: list[str],
    *,
    timeout: float = 120,
    input_text: str | None = None,
    cwd: str | Path | None = None,
    attempts: int = 3,
    idempotent: bool = False,
    retry_log: list[dict[str, object]] | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Retry only SSH failures proven to precede remote command execution."""
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("SSH timeout must be finite and positive") from exc
    if isinstance(timeout, bool) or not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("SSH timeout must be finite and positive")
    timeout = timeout_value
    if deadline is not None:
        try:
            deadline_value = float(deadline)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("SSH deadline must be finite") from exc
        if isinstance(deadline, bool) or not math.isfinite(deadline_value):
            raise ValueError("SSH deadline must be finite")
        deadline = deadline_value
    if isinstance(attempts, bool):
        raise ValueError("attempts must be a positive integer")
    try:
        attempts_value = operator.index(attempts)
    except TypeError as exc:
        raise ValueError("attempts must be a positive integer") from exc
    if attempts_value < 1:
        raise ValueError("attempts must be a positive integer")
    for attempt in range(1, attempts_value + 1):
        effective_timeout = timeout
        if deadline is not None:
            effective_timeout = min(timeout, deadline - time.monotonic())
            if effective_timeout <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
        try:
            if cwd is None:
                result = run_checked(
                    command,
                    timeout=effective_timeout,
                    input_text=input_text,
                )
            else:
                result = run_checked(
                    command,
                    timeout=effective_timeout,
                    input_text=input_text,
                    cwd=cwd,
                )
        except subprocess.TimeoutExpired:
            retryable = idempotent and attempt < attempts_value
            if retry_log is not None:
                retry_log.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "returncode": None,
                        "failure_kind": "command_timeout",
                        "retried": retryable,
                    }
                )
            if not retryable:
                raise
            sleep_for = min(float(attempt), 10.0)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                raise
            time.sleep(sleep_for)
            continue
        except CheckedCommandError as exc:
            failure_kind = retryable_ssh_transport_failure(exc)
            if not failure_kind and idempotent:
                failure_kind = idempotent_ssh_disconnect_failure(exc)
            retryable = bool(failure_kind) and attempt < attempts_value
            if retry_log is not None:
                retry_log.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "returncode": exc.returncode,
                        "failure_kind": failure_kind or "non_retryable",
                        "retried": retryable,
                    }
                )
            exc.ssh_attempts = attempt
            exc.ssh_failure_kind = failure_kind
            if not retryable:
                raise
            sleep_for = min(float(attempt), 10.0)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                raise
            time.sleep(sleep_for)
            continue
        if retry_log is not None:
            retry_log.append(
                {
                    "attempt": attempt,
                    "status": "ok",
                    "returncode": 0,
                    "failure_kind": "",
                    "retried": False,
                }
            )
        return result
    raise AssertionError("unreachable SSH retry state")


__all__ = [
    "CheckedCommandError",
    "idempotent_ssh_disconnect_failure",
    "retryable_ssh_transport_failure",
    "run_checked",
    "run_ssh_checked",
]
