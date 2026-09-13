"""Publish independently bound recovery records after failed-session capture."""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from pathlib import Path

from opencollab_eval.engine.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    metric_submission_integrity,
    read_jsonl,
)
from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid
from opencollab_eval.generation.gen_prediction_config import validate_instance_id
from opencollab_eval.generation.gen_prediction_safe_output import (
    append_output_records,
    build_output_records,
)

KIND = "failed_quiesced_capture"
BINDINGS = (
    "recovery_kind",
    "origin_record_id",
    "origin_predictions_path",
    "origin_metrics_path",
    "origin_patch_sha256",
    "origin_base_commit",
    "origin_runtime_tree_sha256",
    "source_error",
    "model_calls",
    "recovery_container_cleanup_succeeded",
    "recovery_container_id",
)


def _saved_row(path, record_id):
    rows = [row for row in read_jsonl(Path(path)) if row.get("record_id") == record_id]
    if len(rows) != 1:
        raise ValueError("recovery source record is not unique")
    return rows[0]


def _source_pair(prediction, metric, base, runtime):
    patch = prediction.get("model_patch")
    task = prediction.get("instance_id")
    rid = prediction.get("record_id")
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("recovery source patch is empty")
    validate_instance_id(task)
    if not rid or metric.get("record_id") != rid or metric.get("instance_id") != task:
        raise ValueError("recovery source identity mismatch")
    sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    if prediction.get("patch_sha256") != sha or metric.get("patch_sha256") != sha:
        raise ValueError("recovery source patch mismatch")
    if (metric.get("workflow_status") != "error" and metric.get("agent_status") != "stopped") or metric.get(
        "submission_eligible"
    ) is not False:
        raise ValueError("source is not a failed ineligible record")
    if (
        metric.get("session_quiesced") is not True
        or metric.get("execution_quiesced") is not True
        or metric.get("container_execution_quiesced") is not True
        or metric.get("patch_extraction_succeeded") is not True
    ):
        raise ValueError("recovery source capture is incomplete")
    if not current_generation_proof_valid(metric, patch):
        raise ValueError("recovery source capture proof is invalid")
    if metric.get("solver_git_snapshot", {}).get("expected_base_commit") != base:
        raise ValueError("recovery source base mismatch")
    if (
        not isinstance(runtime, str)
        or not re.fullmatch(r"[0-9a-f]{64}", runtime)
        or metric.get("runtime_tree_sha256") != runtime
    ):
        raise ValueError("recovery source runtime mismatch")
    return task, rid, patch, sha


def failed_capture_recovery_valid(prediction, metric):
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        return False
    if metric.get("recovery_kind") != KIND:
        return False
    try:
        if any(prediction.get(key) != metric.get(key) for key in BINDINGS):
            return False
        if (
            metric.get("workflow_status") != "incomplete"
            or type(metric.get("model_calls")) is not int
            or metric["model_calls"] != 0
        ):
            return False
        if metric.get("recovery_container_cleanup_succeeded") is not True or not re.fullmatch(
            r"[0-9a-f]{64}", str(metric.get("recovery_container_id", ""))
        ):
            return False
        origin = metric.get("origin_record_id")
        source_prediction = _saved_row(metric["origin_predictions_path"], origin)
        source_metric = _saved_row(metric["origin_metrics_path"], origin)
        task, rid, patch, sha = _source_pair(
            source_prediction,
            source_metric,
            metric["origin_base_commit"],
            metric["origin_runtime_tree_sha256"],
        )
        if metric.get("record_id") == rid or prediction.get("record_id") != metric.get("record_id"):
            return False
        if prediction.get("instance_id") != task or metric.get("instance_id") != task:
            return False
        if prediction.get("model_patch") != patch or metric.get("origin_patch_sha256") != sha:
            return False
        if prediction.get("patch_sha256") != sha or metric.get("patch_sha256") != sha:
            return False
        if metric.get("source_error") != {
            "type": source_metric.get("error_type"),
            "message": source_metric.get("error"),
        }:
            return False
        for key in (
            "error_type",
            "error",
            "session_quiesced",
            "execution_quiesced",
            "container_execution_quiesced",
            "patch_extraction_succeeded",
            "runtime_outcome",
            "configured_agent_timeout_seconds",
            "usage_complete",
            "used_tokens_lower_bound",
            "usage_status",
            "failure_exception_chain",
            "failure_origin",
            "failure_phase",
            "technical_failure",
            "oc_failure",
            "agent_status",
            "agent_reason",
            "technical_interruption_recoverable",
            "runtime_tree_sha256",
            "run_id",
            "invocation_id",
            "llm_model",
            "llm_provider",
            "wire_protocol",
            "context_window",
            "generation_image_id",
            "solver_git_snapshot",
            "trusted_patch_extraction",
        ):
            if metric.get(key) != source_metric.get(key):
                return False
        if (
            metric.get("submission_eligible") is not True
            or metric_submission_integrity(metric) != SUBMISSION_INTEGRITY_PROVEN
        ):
            return False
        return current_generation_proof_valid(metric, patch)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def publish_failed_capture_recovery(
    *,
    run_dir,
    source_predictions_path,
    source_metrics_path,
    prediction,
    metric,
    capture_metrics,
    expected_base_commit,
    expected_runtime_tree_sha256,
    cid,
):
    patch = prediction.get("model_patch")
    if not isinstance(patch, str) or not patch.strip():
        return None
    task, rid, patch, sha = _source_pair(prediction, metric, expected_base_commit, expected_runtime_tree_sha256)
    if capture_metrics.get("container_cleanup_succeeded") is not True or not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("recovery container cleanup is unproven")
    if (
        capture_metrics.get("session_quiesced") is not True
        or capture_metrics.get("execution_quiesced") is not True
        or capture_metrics.get("patch_extraction_succeeded") is not True
        or not current_generation_proof_valid(capture_metrics, patch)
    ):
        raise ValueError("fresh recovery capture is incomplete")
    if (
        capture_metrics.get("runtime_tree_sha256") != expected_runtime_tree_sha256
        or capture_metrics.get("solver_git_snapshot", {}).get("expected_base_commit") != expected_base_commit
    ):
        raise ValueError("fresh recovery identity differs")
    pp = Path(source_predictions_path).resolve()
    mp = Path(source_metrics_path).resolve()
    if _saved_row(pp, rid) != prediction or _saved_row(mp, rid) != metric:
        raise ValueError("saved recovery source differs")
    values = copy.deepcopy(metric)
    values.update(
        workflow_status="incomplete",
        submission_eligible=True,
        used_tokens=0,
        model_calls=0,
    )
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tokens_used",
        "cost_usd",
        "api_cost_usd",
    ):
        if key in values:
            values[key] = 0
    bindings = {
        "recovery_kind": KIND,
        "origin_record_id": rid,
        "origin_predictions_path": str(pp),
        "origin_metrics_path": str(mp),
        "origin_patch_sha256": sha,
        "origin_base_commit": expected_base_commit,
        "origin_runtime_tree_sha256": expected_runtime_tree_sha256,
        "source_error": {
            "type": metric.get("error_type"),
            "message": metric.get("error"),
        },
        "model_calls": 0,
        "recovery_container_cleanup_succeeded": True,
        "recovery_container_id": cid,
    }
    values.update(bindings)
    new_id = uuid.uuid4().hex
    recovered_prediction, recovered_metric = build_output_records(
        instance_id=task,
        model_name=prediction["model_name_or_path"],
        patch=patch,
        metrics=values,
        record_id=new_id,
        workflow_name=metric.get("workflow"),
    )
    recovered_prediction.update(bindings)
    recovered_metric.update(bindings)
    if not failed_capture_recovery_valid(recovered_prediction, recovered_metric):
        raise ValueError("independent recovery binding failed")
    base = Path(run_dir) / "recovered_candidates" / new_id
    destination = base / task
    destination.mkdir(parents=True, exist_ok=False)
    append_output_records(
        destination / "predictions.jsonl",
        destination / "metrics.jsonl",
        recovered_prediction,
        recovered_metric,
    )
    return {
        "base_run_dir": str(base),
        "record_id": new_id,
        "origin_record_id": rid,
        "model_calls": 0,
    }


def evaluation_candidate_pair(run_dir, prediction, metric):
    """Select the already saved candidate for terminal-session evaluation."""
    from .gen_prediction_safe_output import metrics_have_completed_identity

    if metrics_have_completed_identity(metric, prediction.get("model_patch") or ""):
        return prediction, metric
    matches = []
    for path in (Path(run_dir) / "recovered_candidates").glob("*/*/predictions.jsonl"):
        for recovered_prediction in read_jsonl(path):
            recovered_metric = _saved_row(path.with_name("metrics.jsonl"), recovered_prediction.get("record_id"))
            if recovered_metric.get("origin_record_id") != metric.get("record_id"):
                continue
            if failed_capture_recovery_valid(recovered_prediction, recovered_metric):
                matches.append((recovered_prediction, recovered_metric))
    if len(matches) > 1:
        raise ValueError("multiple saved captures for the same terminal session")
    return matches[0] if matches else None
