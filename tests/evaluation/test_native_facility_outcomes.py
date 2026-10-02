"""Facility interruption evidence takes precedence over empty candidates."""

from __future__ import annotations

import errno

import pytest
from opencollab import RunError, RunResult

from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.engine.evaluator_sessions import _EvalRunRecord
from opencollab_eval.engine.native_failure_attribution import classify_failure, exception_chain
from opencollab_eval.engine.swe_v1_generation_outcomes import (
    generation_execution_invalid,
    generation_outcome_evidence,
)
from opencollab_eval.engine.swe_v1_remote_execution import task_outcome
from opencollab_eval.generation.gen_prediction_agent import _result_metrics, _runtime_failure_metrics
from opencollab_eval.generation.gen_prediction_workflow_state import workflow_stop_metrics


def storage_error(kind, code=errno.ENOSPC):
    error = OSError(code, "storage exhausted")
    if kind == "direct":
        return error
    wrapper = RunError("execution failed")
    if kind == "cause":
        wrapper.__cause__ = error
    elif kind == "context":
        wrapper.__context__ = error
    else:
        group = getattr(pytest.importorskip("builtins"), "ExceptionGroup", None)
        if group is None:
            pytest.skip("exception groups require Python 3.11")
        wrapper.__cause__ = group("multiple failures", [ValueError("bad state"), error])
    return wrapper


@pytest.mark.parametrize("kind", ["direct", "cause", "context", "group"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_storage_evidence_survives_wrapping_and_serialization(kind, code):
    error = storage_error(kind, code)
    attribution = classify_failure(error)
    restored = classify_failure(record={"exception_type": type(error).__name__,
                                        "exception_chain": exception_chain(error)})
    for observed in (attribution, restored):
        assert observed["origin"] == "evaluation_storage"
        assert observed["basis"] == "storage_exhausted"
        assert observed["errno"] == code
        assert observed["technical_failure"] is True
        assert observed["retryable"] is True
    metric = _runtime_failure_metrics(error)
    assert metric["failure_phase"] == "storage_exhausted"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is True
    outcome = generation_outcome_evidence(metric, "")
    assert outcome["technical_failure"] is True
    assert outcome["oc_failure"] is False
    assert task_outcome(outcome, {"status": "skipped_intrinsic_unresolved"})["status"] == "technical_failure"


def test_exception_graph_uses_explicit_cause_and_handles_cycles():
    wrapper = RuntimeError("outer")
    wrapper.__cause__ = ValueError("explicit cause")
    wrapper.__context__ = OSError(errno.ENOSPC, "handled context")
    wrapper.__cause__.__context__ = wrapper
    chain = exception_chain(wrapper)
    assert [node["type"] for node in chain] == ["RuntimeError", "ValueError"]
    assert classify_failure(wrapper)["technical_failure"] is False


def test_suppressed_context_cannot_supply_storage_failure():
    wrapper = RuntimeError("outer")
    wrapper.__context__ = OSError(errno.ENOSPC, "handled context")
    wrapper.__suppress_context__ = True
    assert [node["type"] for node in exception_chain(wrapper)] == ["RuntimeError"]
    assert classify_failure(wrapper)["technical_failure"] is False


@pytest.mark.parametrize("kind", ["cause", "context", "group"])
def test_single_agent_failed_result_keeps_storage_as_technical(kind):
    result = RunResult(status="failed", reason="execution failed", error=storage_error(kind),
                       metrics={"session_quiesced": True}, output=None)
    metric = _result_metrics(result)
    assert metric["failure_origin"] == "evaluation_storage"
    assert metric["failure_phase"] == "storage_exhausted"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is True


def workflow_result(*, patch="", status="failed", attribution=None, role_failure=None):
    return EvalResult(
        task_id="task", patch=patch, patch_produced=bool(patch), tokens_used=1, steps=1, duration=0.1,
        runtime_status=status, runtime_reason="execution failed" if status == "failed" else None,
        workflow_result={"status": "done"} if status == "completed" else {"status": "error"},
        runtime_state={"failure_attribution": attribution or {"origin": "none", "technical_failure": False}},
        agent_failures=() if role_failure is None else (role_failure,),
    )


@pytest.mark.parametrize("role", [False, True])
@pytest.mark.parametrize("status", ["failed", "completed"])
def test_workflow_storage_interruption_with_empty_patch_is_technical(role, status):
    error = storage_error("cause")
    failure = {"label": "coder", "exception_type": "RunError", "exception_chain": exception_chain(error)}
    result = workflow_result(status=status, attribution=None if role else classify_failure(error),
                             role_failure=failure if role else None)
    metric = workflow_stop_metrics(result)
    assert metric["failure_origin"] == "evaluation_storage"
    assert metric["failure_phase"] == "storage_exhausted"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is True


def test_trusted_delivery_keeps_its_score_after_storage_maintenance_error():
    result = workflow_result(patch="diff --git a/a b/a\n", status="completed",
                             attribution=classify_failure(storage_error("cause")))
    metric = workflow_stop_metrics(result)
    assert metric["failure_origin"] == "none"
    assert metric["technical_failure"] is False
    assert generation_outcome_evidence(metric, result.patch)["oc_failure"] is False


@pytest.mark.parametrize("error", [ValueError("bad state"), AssertionError("runtime invariant mismatch")])
def test_native_runtime_failure_without_delivery_is_technical_with_oc_responsibility(error):
    wrapper = RunError("execution failed")
    wrapper.__cause__ = error
    assert classify_failure(wrapper)["origin"] == "oc"
    metric = _runtime_failure_metrics(wrapper)
    assert metric["failure_origin"] == "oc"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is False
    assert task_outcome(generation_outcome_evidence(metric, ""),
                        {"status": "skipped_intrinsic_unresolved"})["status"] == "technical_failure"


def test_normal_completed_empty_patch_remains_capability_failure():
    metric = workflow_stop_metrics(workflow_result(status="completed"))
    outcome = generation_outcome_evidence(metric, "")
    assert outcome["technical_failure"] is False
    assert outcome["oc_failure"] is True


@pytest.mark.parametrize("evaluation", ["skipped_intrinsic_unresolved", "eval_done"])
def test_technical_generation_cannot_be_counted_as_capability_failure(evaluation):
    metric = {"agent_status": "failed", "failure_origin": "oc", "technical_failure": True, "oc_failure": True}
    assert generation_outcome_evidence(metric, "")["oc_failure"] is False
    outcome = task_outcome(metric, {"status": evaluation, "summary": {"resolved": True}})
    assert outcome["status"] == "technical_failure"
    assert outcome["oc_failure"] is False
    assert outcome["technical_failure"] is True


def test_real_candidate_assertion_failure_keeps_official_failure():
    generation = {"status": "generation_done", "technical_failure": False, "oc_failure": False}
    outcome = task_outcome(generation, {"status": "eval_done", "summary": {"resolved": False}})
    assert outcome["status"] == "unresolved"
    assert outcome["technical_failure"] is False


def test_boolean_errno_and_message_are_insufficient_storage_evidence():
    record = {"exception_type": "RuntimeError", "errno": True,
              "message": "No space left on device", "exception_chain": [{"type": "RuntimeError", "errno": True}]}
    assert classify_failure(record=record)["technical_failure"] is False


def test_evaluator_public_runtime_view_preserves_structured_roots():
    error = storage_error("group")
    record = _EvalRunRecord(RunResult(status="failed", reason="execution failed", error=error, output=None))
    state = record.runtime_state
    assert any(node.get("errno") == errno.ENOSPC for node in state["error_chain"])
    assert state["failure_attribution"]["basis"] == "storage_exhausted"
    assert state["external_error_retryable"] is True


def test_internal_exception_group_remains_capability_failure_after_serialization():
    group = getattr(pytest.importorskip("builtins"), "ExceptionGroup", None)
    if group is None:
        pytest.skip("exception groups require Python 3.11")
    error = group("internal failures", [AssertionError("mismatch"), ValueError("bad state")])
    record = {"exception_type": "ExceptionGroup", "exception_chain": exception_chain(error)}
    assert classify_failure(error)["technical_failure"] is False
    assert classify_failure(record=record)["technical_failure"] is False


@pytest.mark.parametrize("metrics", [
    {"tracer_write_error": "writer failed"}, {"trace_write_error": "writer failed"},
    {"tracer_dropped_steps": 2}, {"evidence_complete": False}, {"persistence_errors": ["save failed"]},
])
def test_required_persistence_failure_prevents_single_and_workflow_scores(metrics):
    result = RunResult(status="completed", output="candidate delivered",
                       metrics={"session_quiesced": True, **metrics})
    projection = _result_metrics(result)
    assert projection["technical_failure"] is True
    assert projection["oc_failure"] is False
    assert projection["failure_origin"] == "evaluation_persistence"
    record = _EvalRunRecord(result)
    workflow = workflow_result(status="completed", patch="diff --git a/a b/a\n")
    workflow.runtime_state = record.runtime_state
    projection = workflow_stop_metrics(workflow)
    assert projection["technical_failure"] is True
    assert projection["failure_origin"] == "evaluation_persistence"
    assert projection["failure_phase"] == "required_evidence_persistence"


def test_rebuildable_progress_and_post_delivery_maintenance_errors_keep_score():
    result = RunResult(status="completed", output="candidate delivered", metrics={
        "session_quiesced": True, "observation_write_errors": ["snapshot write failed"],
        "archive_error": "archive unavailable", "notification_error": "notification unavailable",
    })
    assert _result_metrics(result)["technical_failure"] is False
    assert _EvalRunRecord(result).runtime_state["failure_attribution"]["technical_failure"] is False


def test_lifecycle_wrapper_without_root_evidence_is_technical():
    error = RunError("execution failed")
    assert classify_failure(error)["origin"] == "oc"
    assert classify_failure(error)["technical_failure"] is True
    assert classify_failure(record={"exception_type": "RunError"})["technical_failure"] is True



def test_native_trajectory_failure_without_original_errno_stays_technical():
    error = RunError("workflow failed")
    error.__cause__ = OSError("trajectory persistence failed")
    attribution = classify_failure(error)
    assert attribution["origin"] == "evaluation_environment"
    assert attribution["technical_failure"] is True
    assert attribution["basis"] == "system_operation_failed"
    assert _runtime_failure_metrics(error)["oc_failure"] is False


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (408, True), (429, True), (503, True)])
def test_wrapped_http_evidence_keeps_technical_classification(status, retryable):
    error = RuntimeError("response failed")
    error.status_code = status
    wrapper = RunError("execution failed")
    wrapper.__cause__ = error
    record = {"exception_type": "RunError", "exception_chain": exception_chain(wrapper)}
    for attribution in (classify_failure(wrapper), classify_failure(record=record)):
        assert attribution["origin"] == "provider_transport"
        assert attribution["technical_failure"] is True
        assert attribution["retryable"] is retryable



def test_unfinalized_attachment_lifecycle_error_is_technical():
    result = EvalResult(
        task_id="task", patch="", patch_produced=False, tokens_used=0, steps=0, duration=0.1,
        error="RunError: workflow runtime failed without finalized execution evidence",
        runtime_status=None, runtime_state={},
    )
    metric = workflow_stop_metrics(result)
    assert metric["agent_status"] == "failed"
    assert metric["failure_origin"] == "oc"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is False
    assert task_outcome(generation_outcome_evidence(metric, ""),
                        {"status": "skipped_intrinsic_unresolved"})["status"] == "technical_failure"


@pytest.mark.parametrize("error", [ValueError("bad state"), AssertionError("runtime invariant mismatch")])
def test_failed_native_result_has_no_capability_score(error):
    result = RunResult(status="failed", reason="runtime failed", output=None, error=error,
                       metrics={"session_quiesced": True})
    metric = _result_metrics(result)
    assert metric["failure_origin"] == "oc"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is False


@pytest.mark.parametrize("exception_type", ["ValueError", "AssertionError", "RuntimeError"])
def test_caught_role_failure_without_final_delivery_is_technical(exception_type):
    result = workflow_result(status="completed", role_failure={"label": "coder", "exception_type": exception_type})
    result.workflow_result = {"status": "incomplete", "candidate_adopted": False}
    metric = workflow_stop_metrics(result)
    assert metric["failure_origin"] == "oc"
    assert metric["technical_failure"] is True
    assert metric["oc_failure"] is False
    assert metric["technical_interruption_recoverable"] is False


@pytest.mark.parametrize("state", [
    {"agent_status": "failed", "session_quiesced": False},
    {"agent_status": "failed", "session_quiesced": True},
    {"agent_status": "completed", "session_quiesced": False},
    {"agent_status": "completed", "execution_quiesced": False},
])
@pytest.mark.parametrize("resolved", [False, True])
def test_legacy_invalid_generation_gets_technical_outcome_before_official_reward(state, resolved):
    metric = {"status": "intrinsic_unresolved", "failure_origin": "oc",
              "technical_failure": False, "oc_failure": True, **state}
    assert generation_execution_invalid(metric) is True
    outcome = generation_outcome_evidence(metric, "")
    assert outcome["technical_failure"] is True
    assert outcome["oc_failure"] is False
    evaluation = {"status": "eval_done", "summary": {"resolved": resolved}}
    for generation in (metric, outcome):
        result = task_outcome(generation, evaluation)
        assert result["status"] == "technical_failure"
        assert result["resolved"] is False
        assert result["oc_failure"] is False
        assert result["technical_failure"] is True
        assert result["candidate_official_resolved"] is resolved


@pytest.mark.parametrize("agent_status", ["completed", "stopped"])
@pytest.mark.parametrize("resolved", [False, True])
def test_completed_or_controlled_stopped_persisted_candidate_keeps_official_score(agent_status, resolved):
    metric = {"status": "generation_done", "agent_status": agent_status,
              "failure_origin": "none" if agent_status == "completed" else "oc",
              "technical_failure": False, "oc_failure": False,
              "session_quiesced": True, "execution_quiesced": True,
              "submission_eligible": True,
              "agent_reason": "completed" if agent_status == "completed" else "budget_exceeded"}
    assert generation_execution_invalid(metric) is False
    outcome = generation_outcome_evidence(metric, "diff --git a/a b/a\n")
    assert outcome["technical_failure"] is False
    result = task_outcome({**metric, **outcome}, {"status": "eval_done", "summary": {"resolved": resolved}})
    assert result["status"] == ("resolved" if resolved else "unresolved")
    assert result["resolved"] is resolved
    assert result["technical_failure"] is False


def test_completed_outer_workflow_cannot_hide_selected_role_protocol_failure(monkeypatch):
    from opencollab_eval.generation import gen_prediction_workflow_state as observations

    failure = {"label": "dual-coder-contract-a", "exception_type": "ResponsesProtocolError",
               "status_code": None, "provider_error_type": None}
    result = workflow_result(status="completed", patch="diff --git a/a b/a\n", role_failure=failure)
    result.workflow_result.update(winner="A", adopted="A", candidate_adopted=True, candidate_diff_bytes=21)
    states = [{"artifact": "000_dual-coder-contract-a.json", "phase": "error",
               "terminal_reason": "ResponsesProtocolError: terminal output disagrees with streamed items"}]
    monkeypatch.setattr(observations, "_role_states", lambda *_: (states, []))
    metrics = workflow_stop_metrics(result)
    assert metrics["technical_failure"] is True
    assert metrics["workflow_role_failures_tolerated"] is False
    assert generation_execution_invalid(metrics) is True
    for resolved in (False, True):
        result = task_outcome(metrics, {"status": "eval_done", "summary": {"resolved": resolved}})
        assert result["status"] == "technical_failure"
    legacy = dict(metrics, technical_failure=False, workflow_role_failures_tolerated=True)
    assert generation_execution_invalid(legacy) is True


def test_completed_workflow_with_unreadable_role_evidence_is_technical(monkeypatch):
    from opencollab_eval.generation import gen_prediction_workflow_state as observations

    result = workflow_result(status="completed", patch="diff --git a/a b/a\n")
    monkeypatch.setattr(observations, "_role_states", lambda *_: ([], [{"error_type": "JSONDecodeError"}]))
    metrics = workflow_stop_metrics(result)
    assert metrics["technical_failure"] is True
    assert generation_execution_invalid(metrics) is True


def test_finished_discarded_candidate_cleanup_keeps_normal_score(monkeypatch):
    from opencollab_eval.generation import gen_prediction_workflow_state as observations

    failure = {"label": "dual-coder-contract-b:cleanup", "exception_type": "RuntimeError",
               "status_code": None, "provider_error_type": None}
    result = workflow_result(status="completed", patch="diff --git a/a b/a\n", role_failure=failure)
    result.workflow_result.update(winner="A", adopted="A")
    states = [{"artifact": "001_dual-coder-contract-b.json", "phase": "done", "terminal_reason": "completed",
               "pending_events": 0, "pending_external_user_turn": False, "active_turn_start_message_index": None}]
    monkeypatch.setattr(observations, "_role_states", lambda *_: (states, []))
    metrics = workflow_stop_metrics(result)
    assert metrics["technical_failure"] is False
    assert metrics["workflow_role_failures_tolerated"] is True
    evidence = generation_outcome_evidence(metrics, result.patch)
    assert generation_execution_invalid(evidence) is False
    for resolved in (True, False):
        observed = task_outcome(evidence, {"status": "eval_done", "summary": {"resolved": resolved}})
        assert observed["resolved"] is resolved
    states[0]["pending_events"] = 1
    assert workflow_stop_metrics(result)["technical_failure"] is True
    states[0]["pending_events"] = 0
    result.workflow_result.update(winner="B", adopted="B")
    assert workflow_stop_metrics(result)["technical_failure"] is True
