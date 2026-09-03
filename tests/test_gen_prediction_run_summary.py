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


def _degenerate_workflow_metrics() -> dict:
    """The dw-subset50 shape: the workflow returned an error, the runtime did not.

    ``self_collaboration`` returns ``{"status": "error", ...}`` when the analyst
    commits no brief. The workflow function itself returned normally, so the
    runtime around it reports "completed" with no reason -- while the very same
    stop on the single-agent arm is recorded as "stopped" with a reason string.
    """
    result = EvalResult(
        task_id="django__django-15128",
        patch="",
        patch_produced=False,
        tokens_used=1_874_331,
        steps=88,
        duration=1_910.0,
        runtime_status="completed",
        runtime_reason=None,
        workflow_result={
            "status": "error",
            "error": "analyst produced no structured brief",
            "seat_cap": 666_666,
            "seat_spend": {"analyst": 620_140},
            "edges_walked": [],
        },
    )
    return gpw._result_metrics(result)


def test_a_workflow_that_returned_an_error_is_recorded_as_stopped_with_a_reason():
    summary = _degenerate_workflow_metrics()[RUN_SUMMARY_KEY]

    assert set(summary) == set(RUN_SUMMARY_FIELDS)
    assert summary["status"] == "stopped"
    assert summary["reason"] == "analyst produced no structured brief"
    # The run totals still come from the run, not from the override.
    assert summary["tokens"] == 1_874_331
    assert summary["steps"] == 88


def test_a_completed_workflow_is_still_recorded_as_the_runtime_reported_it():
    """Positive control: the override fires on the error status and nowhere else."""
    result = EvalResult(
        task_id="django__django-15128",
        patch="diff --git a/a b/a",
        patch_produced=True,
        tokens_used=900_000,
        steps=40,
        duration=800.0,
        runtime_status="completed",
        runtime_reason=None,
        workflow_result={"status": "done", "edges_walked": ["analyst->coder"]},
    )

    summary = gpw._result_metrics(result)[RUN_SUMMARY_KEY]

    assert summary["status"] == "completed"
    assert summary["reason"] is None


def test_a_workflow_error_is_not_labelled_as_a_run_that_finished_empty():
    """``workflow_status`` must name the stop, not the empty patch it left.

    ``result.error`` is the runtime's crash string and stays None here, so the
    empty patch used to fall through to ``empty_patch_after_done`` -- "the run
    finished and wrote nothing", a different fact from "the analyst never
    handed anything over".
    """
    result = EvalResult(
        task_id="django__django-15128",
        patch="",
        patch_produced=False,
        tokens_used=1_874_331,
        steps=88,
        duration=1_910.0,
        runtime_status="completed",
        workflow_result={
            "status": "error",
            "error": "analyst produced no structured brief",
        },
    )

    assert gpw._workflow_status_for_result(result, "") == "error"
    # A workflow that really did finish empty keeps its old label.
    done = EvalResult(
        task_id="django__django-15128",
        patch="",
        patch_produced=False,
        tokens_used=10,
        steps=1,
        duration=1.0,
        runtime_status="completed",
        workflow_result={"status": "done"},
    )
    assert gpw._workflow_status_for_result(done, "") == "empty_patch_after_done"
