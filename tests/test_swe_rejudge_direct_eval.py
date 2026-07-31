from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
)
from swe_v1_prolite_runner_test_support import controller_proof_text, eval_snapshot_proof_fields

from opencollab_eval.commands.swe_rejudge_direct_eval import _validate_execution_plan, rejudge
from opencollab_eval.engine.swe_v1_remote_artifacts import (
    derive_eval_verdict,
)
from opencollab_eval.engine.swe_v1_remote_artifacts import (
    read_eval_output_artifacts as _read_eval_output_artifacts,
)
from opencollab_eval.engine.swe_v1_remote_commands import (
    _plan_log_failure_proof_matches,
)
from opencollab_eval.engine.swe_v1_remote_eval_script import direct_eval_script


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _plans() -> tuple[dict, dict]:
    f2p = {
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
    }
    p2p = {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "unsupported",
        "coverage": "none",
        "coverage_verified": False,
        "declared_targets": [],
        "target_batches": [],
        "commands": [],
        "proofs": [],
        "runtime_dependencies": [],
    }
    return f2p, p2p
_DEFAULT_CANDIDATE_EXPECTATION = candidate_eval_proof_fields("task-1", "record-1", "a" * 64, "c" * 64)[0]
def read_eval_output_artifacts(*args, **kwargs):
    kwargs.setdefault("candidate_expectation", _DEFAULT_CANDIDATE_EXPECTATION)
    return _read_eval_output_artifacts(*args, **kwargs)


def _assert_resolved_verdict(verdict: dict) -> None:
    assert verdict == {
        "outcome": "resolved",
        "outcome_basis": ["all_declared_targets_passed"],
        "technical_reasons": [], "technical_error": False,
        "resolved": True, "summary_status": "done",
        "output_artifact_errors": [], "operational_warnings": [],
    }


def _seed_output(
    report_dir: Path,
    f2p_plan: dict,
    *,
    candidate_identity: tuple[str, str, str, str] = ("task-1", "record-1", "a" * 64, "c" * 64),
    f2p_status: int = 0,
    f2p_log: str = (
        '{"Action":"run","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
        '{"Action":"pass","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
    ),
) -> None:
    report_dir.mkdir(parents=True)
    for name in (
        "base_commit",
        "service_bootstrap",
        "before_repo",
        "post_before_base",
        "model_patch",
        "test_patch",
        "f2p",
        "p2p",
    ):
        status = f2p_status if name == "f2p" else 0
        (report_dir / f"{name}.exit").write_text(f"{status}\n", encoding="ascii")
    snapshot = eval_snapshot_proof_fields()
    _write_json(report_dir / "base_snapshot.json", snapshot)
    expectation, projection = candidate_eval_proof_fields(
        *candidate_identity,
        base_commit=snapshot["anonymous_head"],
        base_tree=snapshot["base_tree"],
        source_base_commit=snapshot["expected_base_commit"],
    )
    _write_json(report_dir / "candidate_projection.json", projection)
    _write_json(
        report_dir / "source_candidate_projection.json",
        candidate_source_projection_fields(expectation),
    )
    _write_json(
        report_dir / "runtime_dependencies.json",
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(b"[]").hexdigest(),
            "entries": [],
        },
    )
    (report_dir / "f2p.batch_001.exit").write_text(f"{f2p_status}\n", encoding="ascii")
    (report_dir / "f2p.batch_001.command").write_text(
        f2p_plan["commands"][0] + "\n",
        encoding="utf-8",
    )
    (report_dir / "f2p.batch_001.log").write_text(f2p_log, encoding="utf-8")
    for name in ("f2p.command", "p2p.command"):
        path = report_dir / name
        path.write_text("diagnostic aggregate\n", encoding="utf-8")
        path.chmod(0)


def test_direct_eval_selects_testbed_when_app_is_not_a_repository(tmp_path: Path) -> None:
    app = tmp_path / "app"
    testbed = tmp_path / "testbed"
    app.mkdir()
    (testbed / ".git").mkdir(parents=True)
    prefix = direct_eval_script().split('expected_base_commit="', 1)[0]
    replacements = {
        "/app": str(app),
        "/testbed": str(testbed),
        "/workspace": str(tmp_path / "workspace"),
        "/repo": str(tmp_path / "repo"),
        "/src": str(tmp_path / "src"),
    }
    for original, replacement in replacements.items():
        prefix = prefix.replace(original, replacement)

    result = subprocess.run(
        ["bash", "-c", prefix + '\nprintf "%s\\n%s\\n" "$repo_root" "$GIT_CONFIG_VALUE_0"'],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.splitlines() == [str(testbed), str(testbed)]


def test_aggregate_command_permission_errors_are_diagnostic_only(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["output_artifact_errors"] == []
    assert set(artifacts["diagnostic_artifact_errors"]) == {
        "unsafe:f2p.command:UnsafeRecordInputError",
        "unsafe:p2p.command:UnsafeRecordInputError",
    }
    _assert_resolved_verdict(verdict)


def test_eval_artifacts_require_two_complete_base_snapshots(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    snapshot_path = report_dir / "base_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot.pop("preparation_input_snapshot")
    _write_json(snapshot_path, snapshot)

    missing_pre = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
    )
    assert "unsafe:base_snapshot.json:invalid_integrity" in missing_pre["output_artifact_errors"]

    _write_json(snapshot_path, eval_snapshot_proof_fields())
    wrong_base = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        "f" * 40,
    )
    assert "unsafe:base_snapshot.json:invalid_integrity" in wrong_base["output_artifact_errors"]


def test_eval_artifacts_require_runtime_dependency_provenance(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    (report_dir / "runtime_dependencies.json").unlink()

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")

    assert "missing:runtime_dependencies.json" in artifacts["output_artifact_errors"]


def test_eval_artifacts_bind_runtime_dependencies_to_the_test_plan(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    report_path = report_dir / "runtime_dependencies.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["spec_sha256"] = "f" * 64
    _write_json(report_path, report)

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")

    assert "unsafe:runtime_dependencies.json:invalid_integrity" in artifacts["output_artifact_errors"]


@pytest.mark.parametrize(
    "entry_update",
    [
        {"kind": "directory", "content_sha256": ""},
        {"content_sha256": "b" * 64},
        {"candidate_protected": True},
    ],
)
def test_eval_artifacts_bind_runtime_dependency_type_content_and_protection(
    tmp_path: Path,
    entry_update: dict,
) -> None:
    f2p_plan, p2p_plan = _plans()
    spec = {
        "root": "package.json",
        "required_paths": ["package.json"],
        "kind": "file",
        "candidate_protected": False,
    }
    f2p_plan["runtime_dependencies"] = [spec]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    content_sha256 = "a" * 64
    entry = {**spec, "content_sha256": content_sha256, **entry_update}
    _write_json(
        report_dir / "runtime_dependencies.json",
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(
                json.dumps([spec], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "entries": [entry],
        },
    )

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        runtime_dependency_identities={
            "schema": "opencollab.runtime_dependency_identities.v1",
            "image_id": "sha256:" + "1" * 64,
            "entries": [{"root": "package.json", "content_sha256": content_sha256}],
        },
        expected_eval_image_id="sha256:" + "1" * 64,
    )

    assert "unsafe:runtime_dependencies.json:invalid_integrity" in artifacts["output_artifact_errors"]


def test_eval_artifacts_accept_controller_bound_runtime_file_identity(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    spec = {
        "root": "package.json",
        "required_paths": ["package.json"],
        "kind": "file",
        "candidate_protected": False,
    }
    f2p_plan["runtime_dependencies"] = [spec]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    content_sha256 = "a" * 64
    _write_json(
        report_dir / "runtime_dependencies.json",
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(
                json.dumps([spec], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "entries": [{**spec, "content_sha256": content_sha256}],
        },
    )

    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        runtime_dependency_identities={
            "schema": "opencollab.runtime_dependency_identities.v1",
            "image_id": "sha256:" + "1" * 64,
            "entries": [{"root": "package.json", "content_sha256": content_sha256}],
        },
        expected_eval_image_id="sha256:" + "1" * 64,
    )

    assert "unsafe:runtime_dependencies.json:invalid_integrity" not in artifacts[
        "output_artifact_errors"
    ]

    report_path = report_dir / "runtime_dependencies.json"
    duplicate = json.loads(report_path.read_text(encoding="utf-8"))
    duplicate["entries"].append(dict(duplicate["entries"][0]))
    _write_json(report_path, duplicate)
    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        "nonce",
        runtime_dependency_identities={
            "schema": "opencollab.runtime_dependency_identities.v1",
            "image_id": "sha256:" + "1" * 64,
            "entries": [{"root": "package.json", "content_sha256": content_sha256}],
        },
        expected_eval_image_id="sha256:" + "1" * 64,
    )
    assert "unsafe:runtime_dependencies.json:invalid_integrity" in artifacts[
        "output_artifact_errors"
    ]


def test_eval_artifacts_accept_the_original_v2_javascript_dependency_evidence(
    tmp_path: Path,
) -> None:
    f2p_plan, p2p_plan = _plans()
    legacy_spec = {
        "root": "node_modules",
        "required_paths": ["node_modules/.bin/mocha"],
    }
    f2p_plan["runtime_dependencies"] = [legacy_spec]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    _write_json(
        report_dir / "runtime_dependencies.json",
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(
                json.dumps([legacy_spec], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "entries": [legacy_spec],
        },
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")

    assert "unsafe:runtime_dependencies.json:invalid_integrity" not in artifacts[
        "output_artifact_errors"
    ]


@pytest.mark.parametrize(
    "malformed_report",
    [
        [],
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(b"[]").hexdigest(),
            "entries": [{"root": "node_modules", "required_paths": [{"path": "jest"}]}],
        },
    ],
)
def test_eval_artifacts_reject_malformed_runtime_dependency_json_without_crashing(
    tmp_path: Path, malformed_report
) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    _write_json(report_dir / "runtime_dependencies.json", malformed_report)

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")

    assert "unsafe:runtime_dependencies.json:invalid_integrity" in artifacts["output_artifact_errors"]


def test_eval_artifacts_reject_numeric_object_ids_without_crashing(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(report_dir, f2p_plan)
    snapshot_path = report_dir / "base_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["preparation_input_snapshot"]["expected_base_commit"] = int("1" * 40)
    _write_json(snapshot_path, snapshot)

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")

    assert "unsafe:base_snapshot.json:invalid_integrity" in artifacts["output_artifact_errors"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.pop("coverage_verified"),
        lambda plan: plan.__setitem__("proofs", [None]),
        lambda plan: plan.__setitem__("proofs", []),
    ],
)
def test_rejudge_rejects_unproven_fail_to_pass_plans(mutate) -> None:
    f2p_plan, _p2p_plan = _plans()
    mutate(f2p_plan)

    with pytest.raises(RuntimeError, match="executable plan contract"):
        _validate_execution_plan(f2p_plan, label="fail-to-pass", require_commands=True)


def test_rejudge_rejects_metadata_stripped_pytest_plan() -> None:
    plan = {"commands": ["pytest target"], "coverage_verified": True}

    with pytest.raises(RuntimeError, match="executable plan contract"):
        _validate_execution_plan(plan, label="fail-to-pass", require_commands=True)


def test_negative_target_proofs_require_an_exact_failed_target() -> None:
    js_target = "test/messaging.js | Messaging edit should reject invalid data"
    js_proof = {
        "kind": "js_parser_backed_targets",
        "targets": [js_target],
        "repo_language": "js",
        "repo": "nodebb/nodebb",
    }
    assert _plan_log_failure_proof_matches(
        js_proof,
        json.dumps(["fail", {"fullTitle": "Messaging edit should reject invalid data"}]),
    )
    assert not _plan_log_failure_proof_matches(
        js_proof,
        json.dumps(["fail", {"fullTitle": "an unrelated test"}]),
    )
    assert not _plan_log_failure_proof_matches(
        js_proof,
        json.dumps(
            {
                "testResults": [
                    {
                        "name": "test/messaging.js",
                        "assertionResults": [
                            {
                                "title": "Messaging edit should reject invalid data",
                                "status": "pending",
                            }
                        ],
                    }
                ]
            }
        ),
    )

    go_proof = {"kind": "go_json_test_pass", "tests": ["TestColumn"]}
    assert _plan_log_failure_proof_matches(
        go_proof,
        json.dumps({"Action": "fail", "Test": "TestColumn"}),
    )
    assert not _plan_log_failure_proof_matches(
        go_proof,
        json.dumps({"Action": "fail", "Test": "TestOther"}),
    )

    pytest_target = "tests/test_widget.py::test_widget"
    pytest_proof = {
        "kind": "pytest_structured_reports",
        "targets": [pytest_target],
        "command_sha256": "a" * 64,
    }
    structured = controller_proof_text(
        [
            {"event": "session_start"},
            {"event": "collection_finish", "nodeids": [pytest_target]},
            {
                "event": "runtest_logreport",
                "nodeid": pytest_target,
                "when": "call",
                "outcome": "failed",
            },
            {"event": "session_finish", "exitstatus": 1},
        ],
        returncode=1,
    )
    assert _plan_log_failure_proof_matches(pytest_proof, "", structured)
    assert not _plan_log_failure_proof_matches(
        pytest_proof,
        "",
        structured.replace(pytest_target, "tests/test_widget.py::test_other"),
    )


def test_nonzero_exit_without_exact_failed_target_remains_technical(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    f2p_plan["proofs"] = [
        {
            "kind": "js_parser_backed_targets",
            "targets": ["test/messaging.js | declared target"],
            "repo_language": "js",
            "repo": "nodebb/nodebb",
        }
    ]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(
        report_dir,
        f2p_plan,
        f2p_status=1,
        f2p_log=json.dumps(["fail", {"fullTitle": "unrelated target"}]),
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["f2p_evidence"][0]["target_failure_proof_matches_plan"] is False
    assert "fail_to_pass_evidence" in verdict["technical_reasons"]


def test_exact_mocha_failure_is_a_trusted_unresolved_result(tmp_path: Path) -> None:
    failed_title = "Messaging Library edit should reject invalid data"
    f2p_plan, p2p_plan = _plans()
    f2p_plan["proofs"] = [
        {
            "kind": "js_parser_backed_targets",
            "targets": [f"test/messaging.js | {failed_title}"],
            "repo_language": "js",
            "repo": "nodebb/nodebb",
        }
    ]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(
        report_dir,
        f2p_plan,
        f2p_status=1,
        f2p_log=json.dumps(["fail", {"fullTitle": failed_title}]),
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["f2p_evidence"][0]["target_failure_proof_matches_plan"] is True
    assert "fail_to_pass_evidence" not in verdict["technical_reasons"]
    assert verdict["technical_error"] is False
    assert verdict["resolved"] is False
    assert verdict["summary_status"] == "done"


def test_exact_target_pass_overrides_an_unrelated_suite_failure(tmp_path: Path) -> None:
    target = "test/messaging.js | declared target"
    f2p_plan, p2p_plan = _plans()
    f2p_plan["proofs"] = [
        {
            "kind": "js_parser_backed_targets",
            "targets": [target],
            "repo_language": "js",
            "repo": "nodebb/nodebb",
        }
    ]
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(
        report_dir,
        f2p_plan,
        f2p_status=1,
        f2p_log="\n".join(
            [
                json.dumps(["pass", {"fullTitle": "declared target"}]),
                json.dumps(["fail", {"fullTitle": "unrelated target"}]),
            ]
        ),
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert artifacts["f2p_evidence"][0]["status"] == 1
    assert artifacts["f2p_evidence"][0]["target_proof_matches_plan"] is True
    assert artifacts["f2p_evidence"][0]["target_failure_proof_matches_plan"] is False
    _assert_resolved_verdict(verdict)


def test_log_text_alone_cannot_assign_infrastructure_failure(tmp_path: Path) -> None:
    f2p_plan, p2p_plan = _plans()
    report_dir = tmp_path / "reports" / "task-1"
    _seed_output(
        report_dir,
        f2p_plan,
        f2p_status=1,
        f2p_log="redis.exceptions.ConnectionError: Connection refused\n",
    )

    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, "nonce")
    artifacts["f2p_log_tail"] = "redis.exceptions.ConnectionError: Connection refused\n"
    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=0,
        cleanup_quiesced=True,
        container_cleanup={"ok": True},
    )

    assert "fail_to_pass_evidence" in verdict["technical_reasons"]
    assert "fail_to_pass_infra" not in verdict["technical_reasons"]
    assert verdict["outcome"] == "technical_failure"
    assert verdict["technical_error"] is True
    assert verdict["resolved"] is False


@pytest.mark.parametrize(
    "case",
    [
        "permission",
        "evidence",
        "attempt_eval_patch_mismatch",
        "attempt_image_mismatch",
        "source_identity_missing",
        "source_tests_missing",
        "source_tests_wrong_type",
        "source_plan_mismatch",
        "malformed_proof_nonce",
    ],
)
def test_rejudge_writes_derived_summary_without_mutating_evidence(
    tmp_path: Path, case: str
) -> None:
    task = "task-1"
    record_id = "record-1"
    patch_sha = "a" * 64
    eval_patch_sha = "c" * 64
    eval_spec_sha = "b" * 64
    eval_image_id = "sha256:" + "d" * 64
    permission_reclassification = case == "permission"
    eval_dir = tmp_path / "run" / "official_eval_v5"
    output_dir = tmp_path / "run" / "official_eval_v5_rejudged"
    f2p_plan, p2p_plan = _plans()
    _write_json(eval_dir / "input" / "f2p.plan.json", f2p_plan)
    _write_json(eval_dir / "input" / "p2p.plan.json", p2p_plan)
    (eval_dir / "input" / "proof.nonce").write_text("f" * 32 + "\n", encoding="ascii")
    _seed_output(
        eval_dir / "reports" / task,
        f2p_plan,
        candidate_identity=(task, record_id, patch_sha, eval_patch_sha),
        f2p_status=1,
        f2p_log=(
            '{"Action":"run","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
            '{"Action":"fail","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
        ),
    )
    if not permission_reclassification:
        for name in ("f2p.command", "p2p.command"):
            (eval_dir / "reports" / task / name).chmod(0o644)
    technical_reasons = (
        ["unsafe_or_missing_output_artifact", "fail_to_pass_evidence"]
        if permission_reclassification
        else ["fail_to_pass_evidence"]
    )
    output_artifact_errors = (
        [
            "unsafe:f2p.command:UnsafeRecordInputError",
            "unsafe:p2p.command:UnsafeRecordInputError",
        ]
        if permission_reclassification
        else []
    )
    source = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "technical_eval_failed",
        "task": task,
        "resolved": False, "operational_warnings": [],
        "outcome": "technical_failure", "outcome_basis": ["evaluation_prerequisite_failed"],
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "eval_patch_sha256": eval_patch_sha,
        "eval_spec_sha256": eval_spec_sha,
        "eval_image_id": eval_image_id,
        "candidate_expectation": candidate_eval_proof_fields(task, record_id, patch_sha, eval_patch_sha)[0],
        "technical_reasons": technical_reasons,
        "output_artifact_errors": output_artifact_errors,
        "diagnostic_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "tests_status": {
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
        },
    }
    attempt = {
        "phase": "eval_attempt_started",
        "task": task,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "eval_patch_sha256": eval_patch_sha,
        "eval_spec_sha256": eval_spec_sha,
        "eval_image_id": eval_image_id,
    }
    if case == "attempt_eval_patch_mismatch":
        attempt["eval_patch_sha256"] = "e" * 64
    elif case == "attempt_image_mismatch":
        attempt["eval_image_id"] = "sha256:" + "e" * 64
    elif case == "source_identity_missing":
        source.pop("record_id")
    elif case == "source_tests_missing":
        source.pop("tests_status")
    elif case == "source_tests_wrong_type":
        source["tests_status"] = []
    elif case == "source_plan_mismatch":
        source["tests_status"]["pass_to_pass_plan"] = {"schema": "substituted"}
    elif case == "malformed_proof_nonce":
        (eval_dir / "input" / "proof.nonce").write_text("../proof\n", encoding="ascii")
    _write_json(eval_dir / "summary.json", source)
    attempts_path = eval_dir.parent / "eval_attempts.jsonl"
    attempts_path.write_text(json.dumps(attempt) + "\n", encoding="utf-8")
    source_before = (eval_dir / "summary.json").read_bytes()
    attempts_before = attempts_path.read_bytes()
    source_stat = os.stat(eval_dir / "summary.json")
    attempts_stat = os.stat(attempts_path)

    expected_error = {
        "attempt_eval_patch_mismatch": "no persisted eval attempt",
        "attempt_image_mismatch": "no persisted eval attempt",
        "source_identity_missing": "invalid or missing record_id",
        "source_tests_missing": "lacks tests_status",
        "source_tests_wrong_type": "lacks tests_status",
        "source_plan_mismatch": "disagree on pass_to_pass_plan",
        "malformed_proof_nonce": "invalid proof nonce",
    }.get(case)
    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            rejudge(eval_dir, output_dir)
        assert (eval_dir / "summary.json").read_bytes() == source_before
        assert attempts_path.read_bytes() == attempts_before
        assert not output_dir.exists()
        return
    derived = rejudge(eval_dir, output_dir)

    assert derived["status"] == "done"
    assert derived["resolved"] is False
    assert derived["technical_reasons"] == []
    assert derived["output_artifact_errors"] == []
    assert derived["rejudgement"]["added_eval_attempts"] == 0
    assert derived["rejudgement"]["matching_eval_attempts"] == 1
    assert derived["rejudgement"]["reason"] == (
        "aggregate_command_permission_only"
        if permission_reclassification
        else "structured_test_evidence_only"
    )
    assert (output_dir / "source_summary.json").read_bytes() == source_before
    assert (eval_dir / "summary.json").read_bytes() == source_before
    assert attempts_path.read_bytes() == attempts_before
    assert os.stat(eval_dir / "summary.json").st_mtime_ns == source_stat.st_mtime_ns
    assert os.stat(attempts_path).st_mtime_ns == attempts_stat.st_mtime_ns
    assert json.loads((output_dir / "summary.json").read_text())["resolved"] is False
