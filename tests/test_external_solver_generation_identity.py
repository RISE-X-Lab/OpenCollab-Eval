from __future__ import annotations

import copy
import hashlib

from generation_proof_test_support import trusted_patch_proof_fields

from opencollab_eval.engine.swe_generation_proof import generation_identity_proven
from opencollab_eval.engine.swe_v1_remote_records import completed_generation_identity
from opencollab_eval.generation.gen_prediction_safe_output import (
    metrics_have_completed_identity,
)


def _openhands_records() -> tuple[str, dict, dict]:
    task = "acme__widget-1"
    patch = "diff --git a/widget.py b/widget.py\n+fixed = True\n"
    patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
    proof = trusted_patch_proof_fields(patch)
    snapshot = proof["solver_git_snapshot"]
    extraction = proof["trusted_patch_extraction"]
    identity = {
        "schema": "opencollab.external_solver_identity.v1",
        "solver": "claude-code",
        "model": "glm-5.2",
        "stream_sha256": "1" * 64,
        "settings_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "runtime_image_id": "sha256:" + "4" * 64,
        "solver_task_id": "solver-" + "5" * 32,
        "prompt_sha256": "6" * 64,
        "anonymous_head": snapshot["anonymous_head"],
        "base_tree": snapshot["base_tree"],
        "raw_patch_sha256": "7" * 64,
        "raw_candidate_tree": "8" * 40,
        "candidate_tree": extraction["candidate_tree"],
        "task_image_id": proof["generation_image_id"],
        "public_instance_id": task,
        "trusted_final_patch_sha256": patch_sha256,
    }
    metric = {
        **proof,
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha256,
        "model_name_or_path": "openhands-glm-5.2",
        "workflow": "openhands-external",
        "generator": "claude-code",
        "llm_model": "glm-5.2",
        "external_solver_identity": identity,
        "workflow_status": "done",
        "runner_returncode": 0,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": True,
    }
    prediction = {
        "instance_id": task,
        "record_id": metric["record_id"],
        "patch_sha256": patch_sha256,
        "model_name_or_path": metric["model_name_or_path"],
        "workflow": metric["workflow"],
        "model_patch": patch,
    }
    return patch, prediction, metric


def _native_openhands_records() -> tuple[str, dict, dict]:
    patch, prediction, metric = _openhands_records()
    metric["generator"] = "openhands"
    metric.pop("external_solver_identity")
    metric["openhands_execution_identity"] = {
        "schema": "opencollab.openhands_execution_identity.v1",
        "model": metric["llm_model"],
        "state_sha256": "1" * 64,
        "state_file_count": 2,
        "llm_call_count": 3,
        "solver": "openhands",
        "solver_task_id": "solver-" + "2" * 32,
        "prompt_sha256": "3" * 64,
        "anonymous_head": metric["solver_git_snapshot"]["anonymous_head"],
        "base_tree": metric["solver_git_snapshot"]["base_tree"],
        "candidate_tree": metric["trusted_patch_extraction"]["candidate_tree"],
        "task_image_id": metric["generation_image_id"],
        "public_instance_id": metric["instance_id"],
        "trusted_final_patch_sha256": metric["patch_sha256"],
    }
    return patch, prediction, metric


def test_bound_external_solver_candidate_has_current_generation_identity() -> None:
    patch, prediction, metric = _openhands_records()

    assert generation_identity_proven(metric, patch)
    assert metrics_have_completed_identity(metric, patch)
    assert completed_generation_identity(
        prediction,
        metric,
        prediction["instance_id"],
    )


def test_external_solver_candidate_rejects_missing_execution_evidence() -> None:
    patch, prediction, metric = _openhands_records()
    metric.pop("external_solver_identity")

    assert not generation_identity_proven(metric, patch)
    assert not metrics_have_completed_identity(metric, patch)
    assert not completed_generation_identity(
        prediction,
        metric,
        prediction["instance_id"],
    )


def test_external_solver_candidate_rejects_model_or_candidate_drift() -> None:
    patch, _, metric = _openhands_records()
    model_drift = copy.deepcopy(metric)
    model_drift["external_solver_identity"]["model"] = "other-model"
    candidate_drift = copy.deepcopy(metric)
    candidate_drift["external_solver_identity"]["candidate_tree"] = "9" * 40

    assert not generation_identity_proven(model_drift, patch)
    assert not generation_identity_proven(candidate_drift, patch)


def test_native_openhands_candidate_has_current_generation_identity() -> None:
    patch, prediction, metric = _native_openhands_records()

    assert generation_identity_proven(metric, patch)
    assert metrics_have_completed_identity(metric, patch)
    assert completed_generation_identity(
        prediction,
        metric,
        prediction["instance_id"],
    )


def test_native_openhands_candidate_rejects_missing_or_drifted_evidence() -> None:
    patch, _, metric = _native_openhands_records()
    missing = copy.deepcopy(metric)
    missing.pop("openhands_execution_identity")
    model_drift = copy.deepcopy(metric)
    model_drift["openhands_execution_identity"]["model"] = "other-model"

    assert not generation_identity_proven(missing, patch)
    assert not generation_identity_proven(model_drift, patch)
