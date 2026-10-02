"""Native Duo adoption fallback retains completed candidate scoring."""

from __future__ import annotations

import copy
import errno
import json

import pytest

from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.engine.swe_v1_generation_outcomes import (
    generation_execution_invalid,
    generation_outcome_evidence,
    handled_workflow_role_failures,
)
from opencollab_eval.engine.swe_v1_remote_execution import task_outcome
from opencollab_eval.generation import gen_prediction_workflow_state as observations

PATCH = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+fixed\n"


def fallback_result(winner="A", *, patch=PATCH):
    adopted = "B" if winner == "A" else "A"
    result = EvalResult(
        task_id="task", patch=patch, patch_produced=bool(patch), tokens_used=1, steps=1, duration=0.1,
        runtime_status="completed", runtime_reason="completed",
        runtime_state={"failure_attribution": {"origin": "none", "technical_failure": False}},
        workflow_result={"status": "done", "winner": winner, "adopted": adopted,
                         "adoption_attempts": [winner, adopted]},
        agent_failures=({"label": "dual-coder-contract-" + winner.lower() + ":cleanup",
                         "exception_type": "RuntimeError", "status_code": None, "provider_error_type": None},),
    )
    states = [
        {"artifact": f"00{index}_dual-coder-contract-{role.lower()}.json", "phase": "done",
         "terminal_reason": "completed", "pending_events": 0, "pending_external_user_turn": False,
         "active_turn_start_message_index": None}
        for index, role in enumerate(("A", "B"))
    ]
    return result, states


def project(monkeypatch, result, states, errors=None):
    monkeypatch.setattr(observations, "_role_states", lambda *_: (states, errors or []))
    return observations.workflow_stop_metrics(result)


@pytest.mark.parametrize("winner", ["A", "B"])
@pytest.mark.parametrize("resolved", [True, False])
def test_native_fallback_keeps_both_official_outcomes_after_projection(monkeypatch, winner, resolved):
    result, states = fallback_result(winner)
    original = copy.deepcopy(result.workflow_result)
    metrics = project(monkeypatch, result, states)
    assert metrics["failure_origin"] == "none"
    assert metrics["technical_failure"] is False
    assert metrics["workflow_role_failures_tolerated"] is True
    assert metrics["workflow_role_selection"] == original
    assert result.workflow_result == original
    evidence = json.loads(json.dumps(generation_outcome_evidence(metrics, result.patch)))
    assert evidence["workflow_role_selection"] == original
    assert generation_execution_invalid(evidence) is False
    outcome = task_outcome({"status": "generation_done", **evidence},
                           {"status": "eval_done", "summary": {"resolved": resolved}})
    assert outcome["status"] == ("resolved" if resolved else "unresolved")
    assert outcome["resolved"] is resolved
    assert outcome["candidate_official_resolved"] is resolved
    assert outcome["technical_failure"] is False


@pytest.mark.parametrize("winner", ["A", "B"])
def test_legacy_full_workflow_result_survives_compact_outcome_evidence(winner):
    result, states = fallback_result(winner)
    metric = {"agent_status": "completed", "runtime_status": "completed", "failure_origin": "none",
              "execution_quiesced": True, "agent_failures": list(result.agent_failures),
              "workflow_result": result.workflow_result, "workflow_role_states": states}
    assert handled_workflow_role_failures(metric) is True
    assert generation_execution_invalid(metric) is False
    evidence = generation_outcome_evidence(metric, result.patch)
    assert evidence["workflow_role_selection"] == result.workflow_result
    assert generation_execution_invalid(json.loads(json.dumps(evidence))) is False


@pytest.mark.parametrize("attempts", [None, [], ["B"], ["B", "A"], ["A", "A"], ["A", "B", "A"], "AB"])
def test_missing_or_contradictory_adoption_attempts_remain_technical(monkeypatch, attempts):
    result, states = fallback_result()
    if attempts is None:
        del result.workflow_result["adoption_attempts"]
    else:
        result.workflow_result["adoption_attempts"] = attempts
    metrics = project(monkeypatch, result, states)
    assert metrics["workflow_role_failures_tolerated"] is False
    assert metrics["technical_failure"] is True
    legacy = dict(metrics, technical_failure=False)
    assert generation_execution_invalid(legacy) is True


@pytest.mark.parametrize("role", ["A", "B"])
@pytest.mark.parametrize("field,value", [
    ("phase", "stopped"), ("terminal_reason", "timeout"), ("pending_events", 1),
    ("pending_events", False), ("pending_external_user_turn", True), ("active_turn_start_message_index", 1),
])
def test_fallback_requires_both_roles_completed_and_quiescent(monkeypatch, role, field, value):
    result, states = fallback_result()
    state = states[0 if role == "A" else 1]
    state[field] = value
    metrics = project(monkeypatch, result, states)
    assert metrics["technical_failure"] is True
    assert metrics["workflow_role_failures_tolerated"] is False
    assert generation_execution_invalid(dict(metrics, technical_failure=False)) is True


@pytest.mark.parametrize("role", ["A", "B"])
@pytest.mark.parametrize("field", ["phase", "terminal_reason", "pending_events", "pending_external_user_turn",
                                   "active_turn_start_message_index"])
def test_fallback_requires_explicit_terminal_state_fields(monkeypatch, role, field):
    result, states = fallback_result()
    del states[0 if role == "A" else 1][field]
    assert project(monkeypatch, result, states)["technical_failure"] is True


@pytest.mark.parametrize("case", ["missing_selected", "missing_discarded", "duplicate_selected", "duplicate_discarded",
                                  "malformed_artifact", "unreadable"])
def test_missing_or_ambiguous_role_snapshots_remain_technical(monkeypatch, case):
    result, states = fallback_result()
    errors = None
    if case == "missing_selected":
        states.pop(1)
    elif case == "missing_discarded":
        states.pop(0)
    elif case == "duplicate_selected":
        states.append(dict(states[1], artifact="002_dual-coder-contract-b.json"))
    elif case == "duplicate_discarded":
        states.append(dict(states[0], artifact="002_dual-coder-contract-a.json"))
    elif case == "malformed_artifact":
        states[1]["artifact"] = "unknown_dual-coder-contract-b.json"
    else:
        errors = [{"artifact": "002_extra.json", "error_type": "JSONDecodeError"}]
    metrics = project(monkeypatch, result, states, errors)
    assert metrics["technical_failure"] is True
    assert generation_execution_invalid(metrics) is True


@pytest.mark.parametrize("failure", [
    {"label": "dual-coder-contract-b", "exception_type": "ResponsesProtocolError"},
    {"label": "dual-coder-contract-b:cleanup", "exception_type": "RuntimeError"},
    {"label": "dual-coder-contract-adjudicator", "exception_type": "RuntimeError"},
    {"label": "dual-coder-contract-a", "exception_type": "RuntimeError"},
])
def test_fallback_accepts_only_discarded_candidate_cleanup_failures(monkeypatch, failure):
    result, states = fallback_result()
    result.agent_failures = (*result.agent_failures, failure)
    metrics = project(monkeypatch, result, states)
    assert metrics["technical_failure"] is True
    for resolved in (True, False):
        outcome = task_outcome(metrics, {"status": "eval_done", "summary": {"resolved": resolved}})
        assert outcome["status"] == "technical_failure"
        assert outcome["candidate_official_resolved"] is resolved
    assert generation_execution_invalid(dict(metrics, technical_failure=False)) is True


def test_additional_revoked_role_is_an_extra_failure_during_fallback(monkeypatch):
    result, states = fallback_result()
    result.agent_failures = (*result.agent_failures, {"label": "extra-role", "exception_type": "RuntimeError"})
    states.append({"artifact": "002_extra-role.json", "phase": "error",
                   "terminal_reason": "RuntimeError: Execution environment has been revoked. Session cannot continue."})
    assert project(monkeypatch, result, states)["technical_failure"] is True


@pytest.mark.parametrize("role", ["dual-coder-contract-b", "extra-role"])
def test_revocation_alone_cannot_replace_completed_fallback_cleanup_evidence(monkeypatch, role):
    result, states = fallback_result()
    result.agent_failures = ({"label": role, "exception_type": "RuntimeError"},)
    revoked = {"artifact": "002_" + role + ".json", "phase": "error",
               "terminal_reason": "RuntimeError: Execution environment has been revoked. Session cannot continue."}
    if role == "dual-coder-contract-b":
        states[1] = revoked
    else:
        states.append(revoked)
    metrics = project(monkeypatch, result, states)
    assert metrics["technical_failure"] is True
    assert generation_execution_invalid(dict(metrics, technical_failure=False)) is True


@pytest.mark.parametrize("case", ["exception", "provider_status", "provider_type", "nested_provider", "storage",
                                  "environment"])
def test_discarded_cleanup_keeps_structured_facility_and_transport_errors(monkeypatch, case):
    result, states = fallback_result()
    failure = result.agent_failures[0]
    if case == "exception":
        failure["exception_type"] = "ValueError"
    elif case == "provider_status":
        failure["status_code"] = 503
    elif case == "provider_type":
        failure["provider_error_type"] = "server_error"
    else:
        node = {"type": "OSError", "module": "builtins"}
        if case == "storage":
            node["errno"] = errno.ENOSPC
        elif case == "nested_provider":
            node = {"type": "APIConnectionError", "module": "openai"}
        failure["exception_chain"] = [{"type": "RuntimeError", "module": "builtins"}, node]
    metrics = project(monkeypatch, result, states)
    assert metrics["technical_failure"] is True
    assert generation_execution_invalid(dict(metrics, technical_failure=False)) is True
    if case == "storage":
        assert metrics["failure_origin"] == "evaluation_storage"


@pytest.mark.parametrize("metrics", [{"trace_write_error": "failed"}, {"evidence_complete": False},
                                     {"persistence_errors": ["failed"]}])
def test_required_evidence_persistence_failure_stays_technical_after_fallback(monkeypatch, metrics):
    result, states = fallback_result()
    result.runtime_state["metrics"] = metrics
    projected = project(monkeypatch, result, states)
    assert projected["technical_failure"] is True
    assert projected["failure_origin"] == "evaluation_persistence"
    assert generation_outcome_evidence(projected, result.patch)["technical_failure"] is True


@pytest.mark.parametrize("patch", ["", "   \n"])
def test_empty_fallback_keeps_existing_failed_delivery_rule(monkeypatch, patch):
    result, states = fallback_result(patch=patch)
    metrics = project(monkeypatch, result, states)
    assert metrics["technical_failure"] is True
    assert generation_outcome_evidence(metrics, patch)["oc_failure"] is False
    result.agent_failures = ()
    normal = project(monkeypatch, result, states)
    evidence = generation_outcome_evidence(normal, patch)
    assert evidence["technical_failure"] is False
    assert evidence["oc_failure"] is True


@pytest.mark.parametrize("case", ["runtime_failed", "runtime_stopped", "workflow_incomplete", "unquiesced"])
def test_fallback_requires_completed_outer_execution(monkeypatch, case):
    result, states = fallback_result()
    if case.startswith("runtime_"):
        result.runtime_status = case.removeprefix("runtime_")
    elif case == "workflow_incomplete":
        result.workflow_result["status"] = "incomplete"
    else:
        result.execution_quiesced = False
    assert project(monkeypatch, result, states)["technical_failure"] is True


@pytest.mark.parametrize("output", [{"status": "done"}, {"status": "done", "winner": "A", "adopted": "B"}])
def test_normal_delivery_without_role_failures_keeps_existing_projection(monkeypatch, output):
    result, _ = fallback_result()
    result.agent_failures = ()
    result.workflow_result = output
    metrics = project(monkeypatch, result, [])
    assert metrics["technical_failure"] is False
    assert generation_execution_invalid(metrics) is False
