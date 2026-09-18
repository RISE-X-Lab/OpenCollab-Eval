"""Explicit candidate delivery retains independent official scoring."""

from __future__ import annotations

import copy
import json

import pytest
from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
    trusted_patch_proof_fields,
)
from swe_v1_prolite_runner_test_support import _remote_namespace

from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.engine.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    metric_submission_integrity,
)
from opencollab_eval.engine.swe_v1_generation_outcomes import (
    adopted_candidate_metric_view,
    adopted_candidate_report_view,
    handled_revoked_role_failures,
)
from opencollab_eval.generation.gen_prediction_workflow import (
    _result_metrics,
    _workflow_status_for_result,
    build_output_records,
)
from opencollab_eval.generation.gen_prediction_workflow_state import (
    workflow_candidate_delivered,
    workflow_stop_metrics,
)

PATCH = "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-old\n+fixed\n"
TASK = "instance_example"


def delivered_result():
    return EvalResult(
        task_id="solver-" + "a" * 32, patch=PATCH, patch_produced=True,
        tokens_used=123, steps=2, duration=1.0, runtime_status="completed",
        runtime_state={"status": "completed", "external_error": False,
                       "failure_attribution": {"origin": "none", "basis": "no_error"}, "usage_complete": True},
        workflow_result={"status": "incomplete", "candidate_status": "incomplete",
                         "candidate_adopted": True, "candidate_diff_bytes": len(PATCH),
                         "attempts": [{"final_verdict": {"verdict": "FAIL", "findings": "missing verifier probe"}}]},
    )


def old_delivery_pair():
    result = delivered_result()
    metric = {
        **_result_metrics(result), **workflow_stop_metrics(result), **trusted_patch_proof_fields(PATCH),
        "workflow_status": "incomplete", "failure_origin": "oc", "oc_failure": True,
        "failure_phase": "workflow_result", "runner_returncode": 1, "submission_eligible": False,
        "candidate_probe_eligible": True, "worktree_integrity_proven": True,
    }
    return build_output_records(instance_id=TASK, model_name="model", patch=PATCH,
                                metrics=metric, workflow_name="validation-council-solve", record_id="b" * 32)


def test_delivered_candidate_keeps_internal_verdict_and_needs_official_score():
    result = delivered_result()
    original = copy.deepcopy(result.workflow_result)
    projected = workflow_stop_metrics(result)
    assert projected["failure_origin"] == "none" and projected["oc_failure"] is False
    assert projected["original_workflow_status"] == "incomplete"
    assert _workflow_status_for_result(result, PATCH) == "done"
    assert result.workflow_result == original
    assert _workflow_status_for_result(result, "") == "incomplete"


def test_workflow_status_preserves_structured_advisory_gap():
    result = EvalResult(
        task_id="task-1", patch=PATCH, patch_produced=True, tokens_used=1,
        steps=1, duration=1.0,
        workflow_result={"status": "advisory_gap", "done_with_advisory_gap": True},
    )
    assert _workflow_status_for_result(result, result.patch) == "advisory_gap"


def revoked_role_evidence():
    return {
        "agent_failures": [{"label": "candidate/verifier:r1", "exception_type": "RuntimeError",
                            "status_code": None, "provider_error_type": None}],
        "workflow_role_states": [{"artifact": "001_verifier-r1.json", "phase": "error",
                                  "terminal_reason": "RuntimeError: Execution environment has been revoked. "
                                                     "Session cannot continue."}],
    }


@pytest.mark.parametrize("case", ["generic", "provider", "malformed_status", "unknown", "wrong_role", "missing_state",
                                  "duplicate", "mixed_generic", "other_exception", "nonerror"])
def test_only_concrete_matched_role_revocation_can_be_tolerated(case):
    evidence = revoked_role_evidence()
    failure = evidence["agent_failures"][0]
    state = evidence["workflow_role_states"][0]
    if case == "generic":
        state["terminal_reason"] = "RuntimeError: unexpected failure"
    elif case == "provider":
        failure["status_code"] = 524
    elif case == "malformed_status":
        failure["status_code"] = True
    elif case == "unknown":
        failure["provider_error_type"] = "unknown_error"
    elif case == "wrong_role":
        state["artifact"] = "001_other-role.json"
    elif case == "missing_state":
        evidence["workflow_role_states"] = []
    elif case == "duplicate":
        evidence["agent_failures"].append(dict(failure))
        evidence["workflow_role_states"].append(dict(state))
    elif case == "mixed_generic":
        evidence["agent_failures"].append(dict(failure))
        evidence["workflow_role_states"].append({**state, "artifact": "002_verifier-r1.json",
                                                "terminal_reason": "RuntimeError: unknown cause"})
    elif case == "other_exception":
        failure["exception_type"] = "ValueError"
    else:
        state["phase"] = "stopped"
    assert handled_revoked_role_failures(evidence) is False


def test_adopted_candidate_can_deliver_after_concrete_internal_revocation(monkeypatch):
    from opencollab_eval.generation import gen_prediction_workflow_state as observations

    result = delivered_result()
    evidence = revoked_role_evidence()
    result.agent_failures = tuple(evidence["agent_failures"])
    monkeypatch.setattr(observations, "_role_states", lambda *args: (evidence["workflow_role_states"], []))
    metrics = workflow_stop_metrics(result)
    assert metrics["failure_origin"] == "none" and metrics["oc_failure"] is False
    assert metrics["workflow_role_failures_tolerated"] is True
    assert metrics["workflow_role_failure_origins"][0]["origin"] == "oc"
    assert metrics["workflow_role_states"] == evidence["workflow_role_states"]
    assert _workflow_status_for_result(result, PATCH) == "done"
    _, original = old_delivery_pair()
    original.update(evidence)
    view = adopted_candidate_metric_view(original, PATCH)
    assert view["workflow_status"] == "done" and view["workflow_role_failures_tolerated"] is True
    assert view["agent_failures"] == original["agent_failures"]
    assert original["workflow_status"] == "incomplete"


@pytest.mark.parametrize("origins", [["bad"], {}, {"origin": "oc"}])
def test_corrupted_role_origin_records_remain_ineligible(origins):
    _, metric = old_delivery_pair()
    metric["workflow_role_failure_origins"] = origins
    assert adopted_candidate_metric_view(metric, PATCH) is metric


@pytest.mark.parametrize("case", ["unadopted", "empty", "boolean_bytes", "unknown_status", "caught_exception",
                                  "provider", "unknown_origin", "missing_attribution", "role_error", "error",
                                  "unquiesced", "stopped", "failed"])
def test_adoption_does_not_hide_unknown_or_interrupted_execution(case):
    result = delivered_result()
    if case == "unadopted":
        result.workflow_result["candidate_adopted"] = False
    elif case == "empty":
        result.workflow_result["candidate_diff_bytes"] = 0
    elif case == "boolean_bytes":
        result.workflow_result["candidate_diff_bytes"] = True
    elif case == "unknown_status":
        result.workflow_result["status"] = "unknown"
    elif case == "caught_exception":
        result.workflow_result["candidate_status"] = "exception"
    elif case in {"provider", "unknown_origin"}:
        result.runtime_state["failure_attribution"]["origin"] = (
            "provider_transport" if case == "provider" else "unclassified"
        )
    elif case == "missing_attribution":
        del result.runtime_state["failure_attribution"]
    elif case == "role_error":
        result.agent_failures = ({"label": "verifier", "exception_type": "RuntimeError"},)
    elif case == "error":
        result.error = "unknown lifecycle failure"
    elif case == "unquiesced":
        result.execution_quiesced = False
    else:
        result.runtime_status = case
        result.runtime_reason = "timeout" if case == "stopped" else "unknown failure"
    assert workflow_candidate_delivered(result) is False
    assert _workflow_status_for_result(result, PATCH) != "done"


def test_legacy_read_view_preserves_all_original_documents():
    prediction, metric = old_delivery_pair()
    original = copy.deepcopy((prediction, metric))
    view = adopted_candidate_metric_view(metric, PATCH)
    assert view is not metric
    assert metric_submission_integrity(view) == SUBMISSION_INTEGRITY_PROVEN
    assert view["workflow_status"] == "done" and view["runner_returncode"] == 0
    assert view["failure_origin"] == "none" and view["oc_failure"] is False
    assert view["original_generation_projection"]["runner_returncode"] == 1
    assert view["original_generation_projection"]["submission_eligible"] is False
    assert view["workflow_result"] == metric["workflow_result"]
    assert view["original_workflow_status"] == "incomplete"
    assert (prediction, metric) == original
    assert adopted_candidate_metric_view(view, PATCH) is view


@pytest.mark.parametrize("external_metric", [False, True])
def test_paired_and_embedded_metrics_use_the_same_delivery_view(tmp_path, external_metric):
    from opencollab_eval.engine.swe_v1_remote_records import latest_pair

    prediction, metric = old_delivery_pair()
    path = tmp_path / "predictions.jsonl"
    path.write_text(json.dumps(prediction) + "\n")
    before = path.read_bytes()
    if external_metric:
        (tmp_path / "metrics.jsonl").write_text(json.dumps(metric) + "\n")
    observed, projected, pairing = latest_pair(tmp_path, TASK)
    assert pairing == ("record_id" if external_metric else "embedded_metric")
    assert projected["workflow_status"] == "done" and projected["submission_eligible"] is True
    assert observed == prediction and path.read_bytes() == before


@pytest.mark.parametrize("wrong_record", [False, True])
def test_report_view_exposes_only_the_bound_delivery_to_discovery(wrong_record):
    prediction, metric = old_delivery_pair()
    row = {"index": 41, "task": TASK, "generation": {"status": "intrinsic_unresolved",
           "record_id": metric["record_id"], "patch_sha256": metric["patch_sha256"]},
           "eval": {"status": "skipped_intrinsic_unresolved"},
           "task_result": {"oc_failure": True, "status": "unresolved"}}
    original = copy.deepcopy(row)
    if wrong_record:
        prediction["record_id"] = "different-record"
    view = adopted_candidate_report_view(row, prediction, metric)
    assert row == original
    if wrong_record:
        assert view is row
    else:
        assert view["generation"]["status"] == "generation_done"
        assert view["generation"]["submission_eligible"] is True
        assert view["task_result"]["status"] == "official_eval_pending"
        assert view["task_result"]["oc_failure"] is False


@pytest.mark.parametrize("case", ["missing_proof", "wrong_patch", "unquiesced", "isolation_failed", "wrong_restore",
                                  "not_probe_eligible", "provider", "caught_exception"])
def test_legacy_correction_retains_existing_integrity_failures(case):
    _, metric = old_delivery_pair()
    patch = PATCH
    if case == "missing_proof":
        metric.pop("trusted_patch_extraction")
    elif case == "wrong_patch":
        patch += "changed"
    elif case == "unquiesced":
        metric["execution_quiesced"] = False
    elif case == "isolation_failed":
        metric["test_patch_isolation_failed"] = True
    elif case == "wrong_restore":
        metric["checkpoint_result"] = {"restore": {"worktree_integrity_proven": False}}
    elif case == "not_probe_eligible":
        metric["candidate_probe_eligible"] = False
    elif case == "provider":
        metric["provider_failure"] = {"http_status": 524}
    else:
        metric["workflow_result"]["candidate_status"] = "exception"
    assert adopted_candidate_metric_view(metric, patch) is metric


@pytest.mark.parametrize("resolved", [False, True])
def test_actual_remote_main_scores_legacy_delivered_candidate(tmp_path, capsys, resolved):
    namespace = _remote_namespace(tmp_path, eval_only=True)
    prediction, metric = old_delivery_pair()
    run_dir = namespace["base_run_dir"] / TASK
    run_dir.mkdir(parents=True)
    paths = {"predictions.jsonl": prediction, "metrics.jsonl": metric}
    for name, value in paths.items():
        (run_dir / name).write_text(json.dumps(value) + "\n")
    before = {name: (run_dir / name).read_bytes() for name in paths}
    namespace["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].write_text("{}")
    row = {"instance_id": TASK, "test_patch": "official test fixture", "FAIL_TO_PASS": ["test_fixture"]}
    namespace["load_dataset"] = lambda *args: [row]
    calls = []

    def official_score(scoring_row):
        calls.append(scoring_row)
        return {"status": "eval_done", "task": TASK, "attempt_count": 1,
                "summary": {"resolved": resolved, "technical_reasons": []}}

    namespace["eval_for_task"] = official_score
    namespace["generation_for_task"] = lambda *args: pytest.fail("eval-only must not call a model")
    assert namespace["main"]() == 0
    assert len(calls) == 1 and calls[0]["test_patch"] == row["test_patch"]
    summary = json.loads((namespace["base_run_dir"] / "summary.json").read_text())
    output = summary["rows"][0]
    assert output["generation"]["status"] == "generation_done"
    assert output["generation"]["original_generation_projection"]["workflow_status"] == "incomplete"
    assert output["task_result"]["resolved"] is resolved
    assert output["task_result"]["oc_failure"] is False
    assert {name: (run_dir / name).read_bytes() for name in paths} == before
    capsys.readouterr()


@pytest.mark.parametrize("wrong_raw_record", [False, True])
def test_parent_fact_report_uses_delivery_view_after_raw_identity_checks(tmp_path, wrong_raw_record):
    from test_swe_eval_layer_report import _direct_summary

    from opencollab_eval.commands.swe_eval_layer_report import build_report

    prediction, metric = old_delivery_pair()
    source = tmp_path / "source"
    run_dir = source / TASK
    run_dir.mkdir(parents=True)
    (run_dir / "predictions.jsonl").write_text(json.dumps(prediction) + "\n")
    (run_dir / "metrics.jsonl").write_text(json.dumps(metric) + "\n")
    original = {name: (run_dir / name).read_bytes() for name in ("predictions.jsonl", "metrics.jsonl")}
    raw = {"index": 41, "task": TASK, "generation": {"status": "intrinsic_unresolved", "task": TASK,
           "record_id": metric["record_id"], "patch_sha256": metric["patch_sha256"]},
           "eval": {"status": "skipped_intrinsic_unresolved", "task": TASK}}
    view = adopted_candidate_report_view(raw, prediction, metric)
    if wrong_raw_record:
        raw["generation"]["record_id"] = "different-record"
    first = tmp_path / "parallel_summary.json"
    first.write_text(json.dumps({"base_run_dir": str(source), "rows": [raw]}))
    summary = _direct_summary(TASK, False)
    expectation, projection = candidate_eval_proof_fields(TASK, metric["record_id"], metric["patch_sha256"])
    summary.update(record_id=metric["record_id"], patch_sha256=metric["patch_sha256"],
                   eval_patch_sha256=metric["patch_sha256"], candidate_expectation=expectation,
                   candidate_projection=projection,
                   source_candidate_projection=candidate_source_projection_fields(expectation))
    scored = dict(view, eval={"status": "eval_done", "task": TASK, "executed": True,
                             "attempt_count": 1, "summary": summary})
    second = tmp_path / "task_41_attempt_1.json"
    second.write_text(json.dumps({"rows": [scored]}))
    report = build_report([first, second], expected_indices=[41])
    assert {name: (run_dir / name).read_bytes() for name in original} == original
    if wrong_raw_record:
        assert report["counts"]["technical_failed_final"] == 1
    else:
        assert report["counts"]["unresolved"] == 1 and report["counts"]["technical_failed_final"] == 0
        assert report["tasks"][0]["technical_reasons"] == []
        assert report["tasks"][0]["resolved"] is False
