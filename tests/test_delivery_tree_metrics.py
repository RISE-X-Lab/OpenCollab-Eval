"""The seat boundaries a team run recorded have to reach the metrics row.

The team arm is graded on agent 0's tree; each teammate works in a worktree cut
somewhere else. OpenCollab records that graded tree at every seat boundary, and
the value of doing so is entirely in whether the record survives the trip into
``metrics.jsonl`` -- a run whose boundaries are recorded and then dropped is
indistinguishable from one that never recorded any.

The key is written only when the arm recorded boundaries. A null written for
every other arm would say the same thing as a recorder that produced nothing,
and the self-collaboration workflow already carries its own equivalent nested
inside ``workflow_result``; a top-level null there would shadow it.
"""

from __future__ import annotations

from types import SimpleNamespace

from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.engine.evaluator_task_resources import build_eval_result
from opencollab_eval.generation import gen_prediction_workflow as gpw

BOUNDARIES = [
    {"at": "turn_start", "aid": 0, "role": "analyst", "diff": "", "sha256": "a" * 64},
    {"at": "message_sent", "aid": 0, "to_aid": 1, "to_role": "coder", "diff": "+one"},
    {"at": "turn_start", "aid": 1, "role": "coder", "unchanged_since": 1},
]


def _result(**overrides) -> EvalResult:
    fields = {
        "task_id": "django__django-11292",
        "patch": "diff --git a/a b/a",
        "patch_produced": True,
        "tokens_used": 498_112,
        "steps": 57,
        "duration": 61.25,
        "runtime_status": "completed",
    }
    fields.update(overrides)
    return EvalResult(**fields)


def test_a_recorded_run_writes_its_seat_boundaries_into_the_metrics_row():
    metrics = gpw._result_metrics(_result(tree_snapshots=BOUNDARIES))

    assert metrics["tree_snapshots"] == BOUNDARIES


def test_an_arm_that_records_no_boundaries_writes_no_key_at_all():
    """Absent, not null: null would shadow the workflow arm's nested copy."""
    metrics = gpw._result_metrics(_result())

    assert "tree_snapshots" not in metrics


def test_the_run_that_produced_the_boundaries_is_the_one_they_are_read_from():
    """The team's record arrives as ``session``, not as a workflow context.

    A team run leaves ``workflow_ctx`` unset -- it is not a workflow -- so a
    read that only looked at the workflow context would drop every team row
    while still writing a well-formed record for every other arm.
    """
    session = SimpleNamespace(
        used_tokens=498_112,
        step_count=57,
        markup_recovered=0,
        tree_snapshots=BOUNDARIES,
        runtime_status="completed",
        runtime_reason=None,
    )
    state = SimpleNamespace(
        patch="diff --git a/a b/a",
        error=None,
        checkpoint_result=None,
        test_patch_isolation_failed=False,
        execution_quiesced=True,
        patch_extraction_succeeded=True,
        injected_path_cleanup_proven=True,
        harness_artifact_exclusion_proven=True,
        checkpoint_restore_integrity_proven=True,
        task_stage_integrity_proven=True,
        persistence_succeeded=True,
        agent_failures=(),
    )
    facade = SimpleNamespace(EvalResult=EvalResult)

    result = build_eval_result(
        facade,
        state,
        task=SimpleNamespace(task_id="django__django-11292"),
        workflow_ctx=None,
        session=session,
        tracer=SimpleNamespace(path="/tmp/trajectory.jsonl"),
        duration=61.25,
    )

    assert result.tree_snapshots == BOUNDARIES
    assert gpw._result_metrics(result)["tree_snapshots"] == BOUNDARIES
