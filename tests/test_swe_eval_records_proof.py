from __future__ import annotations

import hashlib
import json

from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
)

from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof
from opencollab_eval.engine.swe_v1_remote_target_proof import jest_test_command
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


def _add_candidate_projection(payload: dict) -> None:
    task = payload.setdefault("task", "task-1")
    record_id = payload.setdefault("record_id", "a" * 32)
    eval_patch_sha256 = payload["eval_patch_sha256"]
    expectation, projection = candidate_eval_proof_fields(
        task,
        record_id,
        eval_patch_sha256,
        source_candidate_tree="",
        expected_candidate_tree="",
        base_commit="c" * 40,
        base_tree="d" * 40,
    )
    payload.update(
        candidate_expectation=expectation,
        candidate_projection=projection,
        source_candidate_projection=candidate_source_projection_fields(expectation),
    )


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
        "eval_patch_sha256": "a" * 64,
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
                "runtime_dependencies": [],
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
                "runtime_dependencies": [],
            },
            "fail_to_pass_evidence": [failure_evidence],
            "pass_to_pass_evidence": [],
        },
    }
    _add_candidate_projection(payload)

    assert direct_eval_done_has_execution_proof(payload) is True

    for section in ("candidate_projection", "source_candidate_projection"):
        for field in tuple(payload[section]):
            damaged = json.loads(json.dumps(payload))
            damaged[section].pop(field)
            assert direct_eval_done_has_execution_proof(damaged) is False
        damaged = json.loads(json.dumps(payload))
        damaged[section]["unexpected"] = True
        assert direct_eval_done_has_execution_proof(damaged) is False

    payload.pop("eval_patch_sha256")
    assert direct_eval_done_has_execution_proof(payload) is False
    payload["eval_patch_sha256"] = "a" * 64

    failure_evidence["target_failure_proof_matches_plan"] = False
    assert direct_eval_done_has_execution_proof(payload) is False


def test_direct_eval_accepts_structured_pass_with_nonzero_suite_exit() -> None:
    evidence = {
        "status": 1,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": True,
        "target_failure_proof_matches_plan": False,
        "artifact_safe": True,
    }
    plan = prolite_test_plan(
        {"repo_language": "go"},
        ["pkg/widget_test.go::TestWidget"],
    )
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": True,
        "eval_spec_sha256": "e" * 64,
        "eval_patch_sha256": "a" * 64,
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
            "fail_to_pass_plan": plan,
            "pass_to_pass_plan": prolite_test_plan({"repo_language": "go"}, []),
            "fail_to_pass_evidence": [evidence],
            "pass_to_pass_evidence": [],
        },
    }
    _add_candidate_projection(payload)

    assert direct_eval_done_has_execution_proof(payload) is True
    payload["resolved"] = False
    assert direct_eval_done_has_execution_proof(payload) is False


def test_direct_eval_rejects_metadata_stripped_pytest_green() -> None:
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": True,
        "eval_spec_sha256": "e" * 64,
        "eval_patch_sha256": "a" * 64,
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
                "runtime_dependencies": [],
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
    _add_candidate_projection(payload)

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
        "eval_patch_sha256": "a" * 64,
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


def test_direct_eval_reuse_requires_controller_bound_runtime_file_identity() -> None:
    row = {
        "repo": "NodeBB/NodeBB",
        "repo_language": "js",
        "selected_test_files_to_run": ["test/a.js"],
        "test_patch": "diff --git a/test/a.js b/test/a.js\n",
    }
    f2p_plan = prolite_test_plan(
        row,
        ["test/a.js | works"],
        target_file="/eval_input/f2p.targets.json",
    )
    p2p_plan = prolite_test_plan(row, [], target_file="/eval_input/p2p.targets.json")
    evidence = {
        "status": 0,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": True,
        "target_failure_proof_matches_plan": False,
        "artifact_safe": True,
    }
    payload = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": True,
        "eval_spec_sha256": "e" * 64,
        "eval_patch_sha256": "a" * 64,
        "eval_image_id": "sha256:" + "1" * 64,
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
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": [evidence],
            "pass_to_pass_evidence": [],
        },
    }
    _add_candidate_projection(payload)

    assert direct_eval_done_has_execution_proof(payload) is False

    specs = f2p_plan["runtime_dependencies"]
    content_sha256 = "b" * 64
    payload["runtime_dependency_identities"] = {
        "schema": "opencollab.runtime_dependency_identities.v1",
        "image_id": payload["eval_image_id"],
        "entries": [
            {"root": "package.json", "content_sha256": content_sha256},
            {"root": "config.json", "content_sha256": "c" * 64},
        ],
    }
    payload["runtime_dependencies"] = {
        "schema": "opencollab.eval_runtime_dependencies.v1",
        "phase": "restored",
        "source": "pinned_image_runtime_with_trusted_public_preparation",
        "solver_visible": False,
        "spec_sha256": hashlib.sha256(
            json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "entries": [
            {
                "root": "package.json",
                "required_paths": ["package.json"],
                "kind": "file",
                "candidate_protected": False,
                "content_sha256": content_sha256,
            },
            {
                "root": "config.json",
                "required_paths": ["config.json"],
                "kind": "file",
                "candidate_protected": False,
                "content_sha256": "c" * 64,
            },
        ],
    }

    assert direct_eval_done_has_execution_proof(payload) is True

    payload["runtime_dependencies"]["entries"][0]["content_sha256"] = "d" * 64
    assert direct_eval_done_has_execution_proof(payload) is False
