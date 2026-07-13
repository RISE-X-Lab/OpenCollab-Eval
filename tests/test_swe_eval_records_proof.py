from __future__ import annotations

from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof
from opencollab_eval.engine.swe_v1_remote_target_proof import jest_test_command
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


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
            "fail_to_pass_plan": {
                "schema": "opencollab.prolite_test_plan.v2",
                "adapter": "go-test-json",
                "coverage": "exact_test_events",
                "coverage_verified": True,
                "declared_targets": ["pkg/widget_test.go::TestWidget"],
                "target_batches": [["pkg/widget_test.go::TestWidget"]],
                "commands": ["go test -count=1 -json ./pkg -run '^TestWidget$'"],
                "proofs": [
                    {
                        "kind": "go_json_test_pass",
                        "test": "TestWidget",
                        "package": "./pkg",
                        "test_file": "pkg/widget_test.go",
                    }
                ],
            },
            "pass_to_pass_plan": {
                "schema": "opencollab.prolite_test_plan.v2",
                "adapter": "unsupported",
                "coverage": "none",
                "coverage_verified": False,
                "declared_targets": [],
                "target_batches": [],
                "commands": [],
                "proofs": [],
            },
            "fail_to_pass_evidence": [failure_evidence],
            "pass_to_pass_evidence": [],
        },
    }

    assert direct_eval_done_has_execution_proof(payload) is True

    failure_evidence["target_failure_proof_matches_plan"] = False
    assert direct_eval_done_has_execution_proof(payload) is False


def test_direct_eval_rejects_metadata_stripped_pytest_green() -> None:
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": True,
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
            "fail_to_pass_status": 0,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": {
                "commands": ["pytest target"],
                "coverage_verified": True,
            },
            "pass_to_pass_plan": {
                "schema": "opencollab.prolite_test_plan.v2",
                "adapter": "unsupported",
                "coverage": "none",
                "coverage_verified": False,
                "declared_targets": [],
                "target_batches": [],
                "commands": [],
                "proofs": [],
            },
            "fail_to_pass_evidence": [
                {
                    "status": 0,
                    "command_matches_plan": True,
                    "log_artifact_safe": True,
                    "target_proof_matches_plan": True,
                    "target_failure_proof_matches_plan": False,
                    "artifact_safe": True,
                }
            ],
            "pass_to_pass_evidence": [],
        },
    }

    assert direct_eval_done_has_execution_proof(payload) is False


def test_direct_eval_rejects_jest_evidence_for_a_different_test_file() -> None:
    f2p_plan = prolite_test_plan(
        {"repo_language": "javascript"},
        ["test/a.test.js"],
    )
    f2p_plan["commands"] = [jest_test_command(["test/b.test.js"])]
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": True,
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
            "fail_to_pass_status": 0,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": prolite_test_plan(
                {"repo_language": "javascript"},
                [],
            ),
            "fail_to_pass_evidence": [
                {
                    "status": 0,
                    "command_matches_plan": True,
                    "log_artifact_safe": True,
                    "target_proof_matches_plan": True,
                    "target_failure_proof_matches_plan": False,
                    "artifact_safe": True,
                }
            ],
            "pass_to_pass_evidence": [],
        },
    }

    assert direct_eval_done_has_execution_proof(payload) is False
