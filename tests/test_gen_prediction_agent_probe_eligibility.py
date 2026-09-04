"""Controlled agent stops must retain a chance to extract an existing patch."""

from __future__ import annotations

import pytest
from opencollab import RunResult

from opencollab_eval.generation.gen_prediction_agent import (
    _controlled_stop_status,
    _result_metrics,
)


@pytest.mark.parametrize(
    "phase",
    ["budget_exceeded", "step_limit_exceeded", "context_overflow"],
)
def test_quiesced_controlled_stop_keeps_candidate_probe_eligible(phase: str) -> None:
    result = RunResult(
        output=None,
        status="stopped",
        reason=phase,
        tokens=100,
        error=None,
        metrics={
            "phase": phase,
            "steps": 4,
            "session_quiesced": True,
        },
    )

    metrics = _result_metrics(result)

    assert metrics["workflow_status"] == phase
    assert metrics["session_quiesced"] is True
    assert metrics["candidate_probe_eligible"] is True


def test_failed_result_remains_ineligible_even_when_session_is_quiescent() -> None:
    result = RunResult(
        output=None,
        status="failed",
        reason="done",
        tokens=100,
        error=RuntimeError("agent failed after editing"),
        metrics={
            "phase": "done",
            "steps": 4,
            "session_quiesced": True,
        },
    )

    assert _result_metrics(result)["candidate_probe_eligible"] is False


@pytest.mark.parametrize(
    "reason",
    [
        "budget exceeded: 100 tokens used",
        "budget exceeded after model call: 100 tokens used",
        "budget exhausted before model call: conservative input reservation",
        "team budget exceeded: aggregate spend reached the global cap",
        "step limit reached: 4 steps",
        "context overflow: prompt exceeds the model context window",
        "output truncated: provider reached its generation limit",
    ],
)
def test_verbose_controlled_stop_reason_stays_diagnostic_until_proof(
    reason: str,
) -> None:
    result = RunResult(
        output=None,
        status="stopped",
        reason=reason,
        tokens=100,
        metrics={"phase": "stopped", "steps": 4, "session_quiesced": True},
    )

    metrics = _result_metrics(result)

    assert metrics["workflow_status"] == reason
    assert metrics["session_phase"] == "stopped"
    assert metrics["candidate_probe_eligible"] is True
    assert _controlled_stop_status(reason) == "done_with_timeout_patch"


@pytest.mark.parametrize("reason", ["cancelled", "loop block limit reached: 3"])
def test_unclassified_stop_reason_is_not_timeout_patch(reason: str) -> None:
    assert _controlled_stop_status(reason) is None
