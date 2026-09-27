from __future__ import annotations

import json
from types import SimpleNamespace

from opencollab_eval.engine.evidence_recovery import completed_workflow_usage, record_exception
from opencollab_eval.engine.swe_v1_remote_execution import task_outcome


def test_usage_recovery_counts_completed_responses_without_double_counting(tmp_path):
    path = tmp_path / "orchestration.jsonl"
    events = [
        {"type": "llm_call_started", "seq": 1},
        {"type": "llm_call", "seq": 2, "metrics": {"tokens": 19}},
        {"type": "llm_call", "seq": 2, "metrics": {"tokens": 19}},
        {"type": "llm_call_started", "seq": 3},
    ]
    path.write_text("\n".join(json.dumps(row) for row in events) + "\n{unfinished")
    result = completed_workflow_usage(str(path))
    assert result["tokens_known"] == 19
    assert result["completed_model_calls"] == 1
    assert result["started_model_calls"] == 2


def test_exception_evidence_preserves_error_prefix_and_redacts_credentials(tmp_path, monkeypatch):
    key = "fake-provider-credential-for-test"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    tracer = SimpleNamespace(path=tmp_path / "orchestration.jsonl")
    error = ValueError("connection error with " + key)
    summary = record_exception(tracer, error)
    evidence = json.loads((tmp_path / "evaluation-error.json").read_text())
    assert summary.startswith("ValueError:")
    assert key not in summary
    assert key not in evidence["available_traceback"]
    assert evidence["exception_type"] == "ValueError"


def test_saved_candidate_official_pass_counts_even_after_intrinsic_stop():
    result = task_outcome(
        {"status": "intrinsic_unresolved", "oc_failure": True, "failure_origin": "oc"},
        {"status": "eval_done", "summary": {"resolved": True}},
    )
    assert result["status"] == "resolved"
    assert result["candidate_official_resolved"] is True
    assert result["oc_failure"] is True


def test_external_interruption_with_failing_candidate_stays_technical():
    result = task_outcome(
        {"status": "technical_generation_adapter_or_api_failure", "technical_failure": True},
        {"status": "eval_done", "summary": {"resolved": False}},
    )
    assert result["status"] == "technical_failure"
    assert result["resolved"] is False
