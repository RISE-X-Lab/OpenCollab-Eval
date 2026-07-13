from __future__ import annotations

from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof


def test_direct_eval_unresolved_accepts_structured_failure_proof() -> None:
    failure_evidence = {
        "status": 1,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": False,
        "target_failure_proof_matches_plan": True,
        "artifact_safe": True,
    }
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": False,
        "eval_spec_sha256": "e" * 64,
        "technical_reasons": [],
        "output_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "tests_status": {
            "base_commit_status": 0,
            "service_bootstrap_status": 0,
            "before_repo_status": 0,
            "post_before_base_status": 0,
            "model_patch_status": 0,
            "test_patch_status": 0,
            "fail_to_pass_status": 1,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": {"commands": ["pytest target"], "coverage_verified": True},
            "pass_to_pass_plan": {"commands": [], "coverage_verified": True},
            "fail_to_pass_evidence": [failure_evidence],
            "pass_to_pass_evidence": [],
        },
    }

    assert direct_eval_done_has_execution_proof(payload) is True

    failure_evidence["target_failure_proof_matches_plan"] = False
    assert direct_eval_done_has_execution_proof(payload) is False
