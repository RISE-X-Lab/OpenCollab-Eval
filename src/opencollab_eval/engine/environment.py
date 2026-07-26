"""Evaluation-side structural contracts for command environments."""

from __future__ import annotations

from dataclasses import dataclass

from opencollab.environments import Environment

PROCESS_OUTPUT_CAPTURE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Structural command result used by evaluation fakes and adapters."""

    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


ExecutionEnvironment = Environment

__all__ = [
    "ExecResult",
    "ExecutionEnvironment",
    "PROCESS_OUTPUT_CAPTURE_BYTES",
]
