"""Prepare a retained Terminal candidate and run its official verifier once."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any

from opencollab_eval.generation.terminal_verifier_preparation import (
    VerifierPreparationError,
    prepare_terminal_verifier,
)


async def run_terminal_verifier(
    task_name: str,
    environment: Any,
    artifacts: Path,
    verifier: Callable[[float], Awaitable[dict[str, Any]]],
    *,
    timeout_seconds: float,
    login_probe: str | None = None,
    preparation_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run preparation and scoring within the task's existing total time limit.

    ``environment`` is a dedicated scoring copy of the retained candidate. The
    callback runs the unchanged official verifier with the supplied remaining
    seconds and returns its raw reward, exit_code, timed_out, and error fields.
    The caller retains that raw evidence and owns the scoring container. Pass
    a fresh preparation_receipt dictionary to retain recovery paths even when
    the scoring task is externally cancelled.
    """
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    started = monotonic()
    preparation = preparation_receipt if preparation_receipt is not None else {}
    preparation.clear()
    stage = "preparation"
    raw_verifier = None

    async def execute():
        nonlocal stage, raw_verifier
        await environment.ensure_quiescent()
        # The caller owns progress independently of asyncio exception identity.
        prepared = await prepare_terminal_verifier(
            task_name, environment, Path(artifacts), login_probe=login_probe,
            timeout=max(0.001, timeout_seconds - (monotonic() - started)),
            receipt=preparation,
        )
        preparation.update(prepared)
        stage = "verifier"
        remaining = timeout_seconds - (monotonic() - started)
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await verifier(remaining)
        raw_verifier = raw
        await environment.ensure_quiescent()
        if not isinstance(raw, dict):
            return _failure("verifier_result_invalid", "verifier", raw)
        reward = raw.get("reward")
        usable = (
            not isinstance(reward, bool)
            and reward in (0, 1)
            and raw.get("timed_out") is False
            and raw.get("error") is None
            and isinstance(raw.get("exit_code"), int)
            and not isinstance(raw["exit_code"], bool)
            and raw.get("status") != "facility_error"
            and (reward == 0 or raw["exit_code"] == 0)
        )
        if not usable:
            return _failure("verifier_result_requires_review", "verifier", raw)
        return {"status": "normal", "reward": reward, "reason": "verified_result", "verifier": raw}

    try:
        result = await asyncio.wait_for(execute(), timeout_seconds)
    except VerifierPreparationError as exc:
        preparation.update(exc.receipt)
        result = _failure(exc.reason, "preparation", None)
        await _abort(environment, result)
    except asyncio.TimeoutError:
        result = _failure("verifier_budget_exhausted", stage, raw_verifier)
        result["timed_out"] = True
        await _abort(environment, result)
    except asyncio.CancelledError:
        # The command backend owns process-group cancellation. A failed abort
        # must remain visible to the caller instead of claiming safe completion.
        await environment.abort()
        raise
    except Exception as exc:
        result = _failure("verifier_execution_error", stage, raw_verifier)
        result["error_type"] = type(exc).__name__
        await _abort(environment, result)
    if result["status"] == "normal" and monotonic() - started >= timeout_seconds:
        result = _failure("verifier_budget_exhausted", "verifier", result["verifier"])
        result["timed_out"] = True
        await _abort(environment, result)
    result.update(
        preparation=preparation,
        elapsed_seconds=monotonic() - started,
        timeout_seconds=timeout_seconds,
    )
    return result


def _failure(reason: str, phase: str, raw: Any) -> dict[str, Any]:
    return {
        "status": "facility_error", "reward": None, "reason": reason,
        "failure_phase": phase, "retry_kind": "score_only", "verifier": raw,
    }


async def _abort(environment: Any, result: dict[str, Any]) -> None:
    try:
        await environment.abort()
    except Exception as exc:
        result["cleanup_error_type"] = type(exc).__name__
        result["retry_kind"] = None
