"""The two arms must state the same run-level quantities under the same names.

The single-agent generator and the workflow/team generator build their metric
records from different objects, and those objects name the same quantities
differently. A cross-arm read then silently returns one arm: selecting
``used_tokens`` gets every single-agent row and no team row, with no error
raised and nothing in the output saying rows were dropped.

These tests pin the block that fixes it -- ``run_summary``, written by both
paths -- by asserting the two key sets are equal to each other and to the
declared field list, and that the values come from the run rather than from a
default.
"""

from __future__ import annotations

from opencollab import RunResult

from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.generation import gen_prediction_agent as gpa
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.generation.gen_prediction_run_summary import (
    RUN_SUMMARY_FIELDS,
    RUN_SUMMARY_KEY,
)


def _single_arm_metrics() -> dict:
    result = RunResult(
        output="done",
        status="stopped",
        reason="budget exhausted",
        tokens=173_818,
        metrics={"steps": 41, "session_quiesced": True, "phase": "stopped"},
    )
    return gpa._result_metrics(result, 12.5)


def _team_arm_metrics() -> dict:
    result = EvalResult(
        task_id="django__django-11292",
        patch="diff --git a/a b/a",
        patch_produced=True,
        tokens_used=498_112,
        steps=57,
        duration=61.25,
        runtime_status="stopped",
        runtime_reason="budget exhausted",
    )
    return gpw._result_metrics(result)


def test_both_arms_write_the_same_run_summary_key_set():
    single = _single_arm_metrics()[RUN_SUMMARY_KEY]
    team = _team_arm_metrics()[RUN_SUMMARY_KEY]

    assert set(single) == set(RUN_SUMMARY_FIELDS)
    assert set(team) == set(RUN_SUMMARY_FIELDS)


def test_the_single_agent_block_carries_that_run_s_own_numbers():
    summary = _single_arm_metrics()[RUN_SUMMARY_KEY]

    assert summary == {
        "steps": 41,
        "tokens": 173_818,
        "status": "stopped",
        # The reason is what separates a budget stop from a step-ceiling stop;
        # ``status`` is "stopped" for both.
        "reason": "budget exhausted",
        "duration_s": 12.5,
        "error": None,
    }


def test_the_team_block_carries_that_run_s_own_numbers():
    summary = _team_arm_metrics()[RUN_SUMMARY_KEY]

    assert summary == {
        "steps": 57,
        "tokens": 498_112,
        "status": "stopped",
        "reason": "budget exhausted",
        "duration_s": 61.25,
        "error": None,
    }


def test_a_single_agent_run_that_never_returned_still_writes_the_block(tmp_path):
    """A crashed run must be a readable row, not an absent one."""
    metrics = gpa._runtime_failure_metrics(
        RuntimeError("provider fell over"), 4.0, tmp_path
    )

    summary = metrics[RUN_SUMMARY_KEY]
    assert set(summary) == set(RUN_SUMMARY_FIELDS)
    assert summary["status"] == "failed"
    assert summary["error"] == "provider fell over"
    assert summary["tokens"] == 0
    assert metrics["trajectory_path"] == str(tmp_path)


def test_the_arm_native_keys_are_left_alone():
    """The readers already selecting per-arm names stay correct."""
    single = _single_arm_metrics()
    team = _team_arm_metrics()

    assert single["step_count"] == 41
    assert single["used_tokens"] == 173_818
    assert team["steps"] == 57
    assert team["tokens_used"] == 498_112
