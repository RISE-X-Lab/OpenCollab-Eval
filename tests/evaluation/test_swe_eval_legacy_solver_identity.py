from __future__ import annotations

import copy

import pytest

from opencollab_eval.engine import swe_eval_discovery as discovery
from opencollab_eval.engine import swe_eval_records as records
from opencollab_eval.engine.swe_eval_record_identity import direct_payload_task_id
from opencollab_eval.engine.swe_v1_remote_records import historical_generation_identity_status


def _pair():
    patch = "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-old\n+new\n"
    prediction = {
        "instance_id": "instance_owner__repo-example", "record_id": "record-1",
        "model_patch": patch, "patch_sha256": records.patch_sha(patch),
    }
    metric = {
        "instance_id": prediction["instance_id"], "task_id": "solver-" + "a" * 32,
        "record_id": prediction["record_id"], "patch_sha256": prediction["patch_sha256"],
        "workflow_status": "done", "runner_returncode": 0,
    }
    return prediction, metric


@pytest.mark.parametrize("embedded", [False, True])
def test_legacy_solver_namespace_pairs_without_rewriting_records(embedded):
    prediction, metric = _pair()
    if embedded:
        prediction["workflow_metric"] = copy.deepcopy(metric)
    before = copy.deepcopy((prediction, metric))
    paired = records.latest_paired_rows([prediction], [] if embedded else [metric], prediction["instance_id"])
    assert paired.status == ("embedded_metric" if embedded else "record_id")
    assert paired.metric == metric
    assert records.task_ids([], [metric]) == [prediction["instance_id"]]
    assert historical_generation_identity_status(prediction, metric, prediction["instance_id"]) == "legacy_verified"
    assert (prediction, metric) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": "instance_owner__repo-other"},
        {"task_id": "solver-" + "A" * 32},
        {"task_id": "solver-" + "a" * 31},
        {"task_id": "solver-" + "a" * 33},
        {"task_id": 42},
        {"id": "instance_owner__repo-other"},
    ],
)
def test_other_namespace_conflicts_still_reject_pairing(overrides):
    prediction, metric = _pair()
    metric.update(overrides)
    assert records.row_task_id(metric) == ""
    assert records.latest_paired_rows([prediction], [metric], prediction["instance_id"]).metric is None
    assert historical_generation_identity_status(prediction, metric, prediction["instance_id"]) == "invalid"


def test_direct_attempt_and_report_use_the_shared_legacy_parser(tmp_path):
    prediction, metric = _pair()
    attempt = {
        **metric, "schema": "opencollab.swe_eval_attempt.v1", "started_at_ns": 1,
    }
    parsed = discovery._attempt_from_payload(tmp_path / "attempt.json", attempt)
    assert parsed is not None and parsed.task_id == prediction["instance_id"]
    report = {
        **metric, "schema": "opencollab.prolite_direct_eval.v2",
        "status": "technical_eval_failed", "technical_reasons": ["missing_evidence"],
    }
    parsed_reports = discovery._reports_from_payload(tmp_path / "summary.json", report)
    assert len(parsed_reports) == 1 and parsed_reports[0].task_id == prediction["instance_id"]
    assert direct_payload_task_id({**metric, "task": "different-benchmark"}) is None
    assert direct_payload_task_id({"task_id": metric["task_id"]}) == metric["task_id"]


@pytest.mark.parametrize("field,value", [("record_id", "other"), ("patch_sha256", "b" * 64)])
def test_legacy_solver_compatibility_keeps_candidate_identity_checks(field, value):
    prediction, metric = _pair()
    metric[field] = value
    paired = records.latest_paired_rows([prediction], [metric], prediction["instance_id"])
    assert paired.metric is None
    assert historical_generation_identity_status(prediction, metric, prediction["instance_id"]) == "invalid"
