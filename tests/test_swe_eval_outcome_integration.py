from __future__ import annotations

from pathlib import Path

from generation_proof_test_support import candidate_source_projection_fields
from swe_v1_prolite_runner_test_support import eval_snapshot_proof_fields
from test_swe_rejudge_direct_eval import (
    _DEFAULT_CANDIDATE_EXPECTATION,
    _plans,
    _seed_output,
    _write_json,
    read_eval_output_artifacts,
)

from opencollab_eval.engine.eval_candidate_projection import source_projection_sha256
from opencollab_eval.engine.swe_eval_discovery import _reports_from_payload
from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof
from opencollab_eval.engine.swe_v1_remote_artifacts import derive_eval_verdict


def _projection_failure(expectation: dict, snapshot: dict) -> dict:
    return {
        "schema": "opencollab.eval_candidate_projection_failure.v1",
        "status": "failed",
        "error_kind": "patch_not_applicable",
        "phase": "source",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "verified_base_commit": snapshot["expected_base_commit"],
        "verified_base_tree": snapshot["base_tree"],
        "source_projection_sha256": "",
    }


def _summary_from_verdict(
    *,
    artifacts: dict,
    verdict: dict,
    f2p_plan: dict,
    p2p_plan: dict,
    container_cleanup: dict,
    candidate_expectation: dict | None = None,
) -> dict:
    return {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": verdict["summary_status"],
        "task": "task-1",
        "record_id": "record-1",
        "resolved": verdict["resolved"],
        "outcome": verdict["outcome"],
        "outcome_basis": verdict["outcome_basis"],
        "error": verdict["technical_error"],
        "eval_spec_sha256": "e" * 64,
        "eval_patch_sha256": "c" * 64,
        "patch_sha256": "a" * 64,
        "candidate_expectation": candidate_expectation or _DEFAULT_CANDIDATE_EXPECTATION,
        "candidate_projection_failure": artifacts["candidate_projection_failure"],
        "candidate_projection": artifacts["candidate_projection"],
        "source_candidate_projection": artifacts["source_candidate_projection"],
        "base_snapshot_integrity": artifacts["base_snapshot"],
        "runtime_dependencies": artifacts["runtime_dependencies"],
        "technical_reasons": verdict["technical_reasons"],
        "operational_warnings": verdict["operational_warnings"],
        "output_artifact_errors": verdict["output_artifact_errors"],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": container_cleanup,
        "tests_status": {
            "base_commit_status": artifacts["base_commit_status"],
            "service_bootstrap_status": artifacts["service_status"],
            "before_repo_status": artifacts["before_status"],
            "post_before_base_status": artifacts["post_before_base_status"],
            "model_patch_status": artifacts["model_status"],
            "test_patch_status": artifacts["test_status"],
            "fail_to_pass_status": artifacts["f2p_status"],
            "pass_to_pass_status": artifacts["p2p_status"],
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": artifacts["f2p_evidence"],
            "pass_to_pass_evidence": artifacts["p2p_evidence"],
        },
    }


def test_bound_patch_rejection_is_unresolved_across_report_consumers(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    snapshot = eval_snapshot_proof_fields()
    expectation = dict(_DEFAULT_CANDIDATE_EXPECTATION)
    expectation["expected_candidate_tree"] = ""
    for name in (
        "candidate_projection.json",
        "runtime_dependencies.json",
        "f2p.batch_001.command",
        "f2p.batch_001.exit",
        "f2p.batch_001.log",
    ):
        (report_dir / name).unlink()
    _write_json(
        report_dir / "candidate_expectation.json",
        expectation,
    )
    _write_json(
        report_dir / "candidate_projection_failure.json",
        _projection_failure(expectation, snapshot),
    )
    for name, status in (
        ("model_patch.exit", 1),
        ("service_bootstrap.exit", 99),
        ("test_patch.exit", 99),
        ("f2p.exit", 99),
    ):
        (report_dir / name).write_text(f"{status}\n", encoding="ascii")

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        candidate_expectation=expectation,
    )
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )
    summary = _summary_from_verdict(
        artifacts=artifacts,
        verdict=verdict,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
        container_cleanup={"ok": True},
        candidate_expectation=expectation,
    )

    assert verdict["outcome"] == "unresolved"
    assert verdict["technical_reasons"] == []
    assert verdict["output_artifact_errors"] == []
    assert direct_eval_done_has_execution_proof(summary) is True
    discovered = _reports_from_payload(tmp_path / "summary.json", summary)
    assert len(discovered) == 1
    assert discovered[0].resolved_count == 0
    assert discovered[0].unresolved_count == 1


def test_legacy_source_rejection_must_match_trusted_snapshot(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    snapshot = eval_snapshot_proof_fields()
    expectation = dict(_DEFAULT_CANDIDATE_EXPECTATION)
    expectation.update(
        source_base_commit="",
        source_anonymous_base="",
        source_base_tree="",
    )
    for name in (
        "candidate_projection.json",
        "runtime_dependencies.json",
        "f2p.batch_001.command",
        "f2p.batch_001.exit",
        "f2p.batch_001.log",
    ):
        (report_dir / name).unlink()
    failure = _projection_failure(expectation, snapshot)
    failure["verified_base_commit"] = "f" * 40
    _write_json(report_dir / "candidate_expectation.json", expectation)
    _write_json(report_dir / "candidate_projection_failure.json", failure)
    for name, status in (
        ("model_patch.exit", 1),
        ("service_bootstrap.exit", 99),
        ("test_patch.exit", 99),
        ("f2p.exit", 99),
    ):
        (report_dir / name).write_text(f"{status}\n", encoding="ascii")

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        candidate_expectation=expectation,
    )

    assert (
        "unsafe:candidate_projection_failure.json:invalid_integrity"
        in artifacts["output_artifact_errors"]
    )


def test_source_rejection_after_a_proved_candidate_tree_is_technical(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    for name in (
        "candidate_projection.json",
        "runtime_dependencies.json",
        "f2p.batch_001.command",
        "f2p.batch_001.exit",
        "f2p.batch_001.log",
    ):
        (report_dir / name).unlink()
    _write_json(
        report_dir / "candidate_projection_failure.json",
        _projection_failure(
            _DEFAULT_CANDIDATE_EXPECTATION,
            eval_snapshot_proof_fields(),
        ),
    )
    (report_dir / "model_patch.exit").write_text("1\n", encoding="ascii")

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        candidate_expectation=_DEFAULT_CANDIDATE_EXPECTATION,
    )
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["candidate_projection_failure"]
    assert not any(
        "candidate_projection_failure.json:invalid_integrity" in error
        for error in artifacts["output_artifact_errors"]
    )
    assert verdict["outcome"] == "technical_failure"
    assert "candidate_projection" in verdict["technical_reasons"]


def test_prepared_base_rejection_is_technical(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    snapshot = eval_snapshot_proof_fields()
    source_projection = candidate_source_projection_fields(
        _DEFAULT_CANDIDATE_EXPECTATION
    )
    for name in (
        "candidate_projection.json",
        "runtime_dependencies.json",
        "f2p.batch_001.command",
        "f2p.batch_001.exit",
        "f2p.batch_001.log",
    ):
        (report_dir / name).unlink()
    _write_json(report_dir / "source_candidate_projection.json", source_projection)
    failure = {
        **_projection_failure(_DEFAULT_CANDIDATE_EXPECTATION, snapshot),
        "phase": "prepared",
        "verified_base_commit": snapshot["anonymous_head"],
        "verified_base_tree": snapshot["base_tree"],
        "source_projection_sha256": source_projection_sha256(source_projection),
    }
    _write_json(report_dir / "candidate_projection_failure.json", failure)
    (report_dir / "model_patch.exit").write_text("1\n", encoding="ascii")

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        candidate_expectation=_DEFAULT_CANDIDATE_EXPECTATION,
    )
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["candidate_projection_failure"]
    assert not any(
        "candidate_projection_failure.json:invalid_integrity" in error
        for error in artifacts["output_artifact_errors"]
    )
    assert verdict["outcome"] == "technical_failure"
    assert "candidate_projection" in verdict["technical_reasons"]


def test_cleanup_warning_does_not_erase_a_frozen_semantic_result(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": False},
    )
    summary = _summary_from_verdict(
        artifacts=artifacts,
        verdict=verdict,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
        container_cleanup={"ok": False},
    )

    assert verdict["outcome"] == "resolved"
    assert verdict["operational_warnings"] == [
        "container_removal_failed_after_stop"
    ]
    assert direct_eval_done_has_execution_proof(summary) is True


def test_bound_target_pass_can_resolve_when_process_exit_is_one(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan, f2p_status=1)
    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )
    summary = _summary_from_verdict(
        artifacts=artifacts,
        verdict=verdict,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
        container_cleanup={"ok": True},
    )

    assert verdict["outcome"] == "resolved"
    assert direct_eval_done_has_execution_proof(summary)
    discovered = _reports_from_payload(tmp_path / "summary.json", summary)
    assert len(discovered) == 1
    assert discovered[0].resolved_count == 1
    assert discovered[0].unresolved_count == 0


def test_first_bound_failure_survives_missing_later_batch_artifacts(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    second_target = "pkg/widget_test.go::TestLater"
    f2p_plan["declared_targets"].append(second_target)
    f2p_plan["target_batches"].append([second_target])
    f2p_plan["commands"].append(
        "go test -count=1 -json ./pkg -run '^TestLater$'"
    )
    f2p_plan["proofs"].append(
        {
            "kind": "go_json_test_pass",
            "test": "TestLater",
            "package": "./pkg",
            "test_file": "pkg/widget_test.go",
        }
    )
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(
        report_dir,
        f2p_plan,
        f2p_status=1,
        f2p_log=(
            '{"Action":"run","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
            '{"Action":"fail","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
        ),
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )
    summary = _summary_from_verdict(
        artifacts=artifacts,
        verdict=verdict,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
        container_cleanup={"ok": True},
    )

    assert verdict["outcome"] == "unresolved"
    assert verdict["output_artifact_errors"] == []
    assert any(
        "missing:f2p.batch_002.exit" in warning
        for warning in verdict["operational_warnings"]
    )
    assert direct_eval_done_has_execution_proof(summary) is True
