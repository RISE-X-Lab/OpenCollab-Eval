from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from swe_v1_prolite_runner_test_support import controller_proof_text

from opencollab_eval.commands.swe_rejudge_direct_eval import _validate_execution_plan, rejudge
from opencollab_eval.engine.swe_v1_remote_artifacts import (
    derive_eval_verdict,
    read_eval_output_artifacts,
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
        "proofs": [{"kind": "go_json_test_pass", "test": "TestWidget"}],
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
    }
    return f2p, p2p


def _seed_output(
    report_dir: Path,
    f2p_plan: dict,
    *,
    f2p_status: int = 0,
    f2p_log: str = (
        '{"Action":"run","Test":"TestWidget"}\n'
        '{"Action":"pass","Test":"TestWidget"}\n'
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


def test_direct_eval_makes_aggregate_commands_world_readable() -> None:
    script = direct_eval_script()

    assert "chmod 0644 /eval_output/f2p.command" in script
    assert "chmod 0644 /eval_output/p2p.command" in script


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
    assert verdict == {
        "technical_reasons": [],
        "technical_error": False,
        "resolved": True,
        "summary_status": "done",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan.pop("coverage_verified"), "does not verify target coverage"),
        (lambda plan: plan.__setitem__("proofs", [None]), "contains an unstructured proof"),
        (lambda plan: plan.__setitem__("proofs", []), "does not bind one proof"),
    ],
)
def test_rejudge_rejects_unproven_fail_to_pass_plans(mutate, message: str) -> None:
    f2p_plan, _p2p_plan = _plans()
    mutate(f2p_plan)

    with pytest.raises(RuntimeError, match=message):
        _validate_execution_plan(f2p_plan, label="fail-to-pass", require_commands=True)


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
    pytest_proof = {"kind": "pytest_structured_reports", "targets": [pytest_target]}
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


def test_rejudge_writes_derived_summary_without_mutating_evidence(tmp_path: Path) -> None:
    task = "task-1"
    record_id = "record-1"
    patch_sha = "a" * 64
    eval_spec_sha = "b" * 64
    eval_dir = tmp_path / "run" / "official_eval_v5"
    output_dir = tmp_path / "run" / "official_eval_v5_rejudged"
    f2p_plan, p2p_plan = _plans()
    _write_json(eval_dir / "input" / "f2p.plan.json", f2p_plan)
    _write_json(eval_dir / "input" / "p2p.plan.json", p2p_plan)
    (eval_dir / "input" / "proof.nonce").write_text("nonce\n", encoding="ascii")
    _seed_output(
        eval_dir / "reports" / task,
        f2p_plan,
        f2p_status=1,
        f2p_log=(
            '{"Action":"run","Test":"TestWidget"}\n'
            '{"Action":"fail","Test":"TestWidget"}\n'
        ),
    )
    source = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "technical_eval_failed",
        "task": task,
        "resolved": False,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "eval_spec_sha256": eval_spec_sha,
        "technical_reasons": [
            "unsafe_or_missing_output_artifact",
            "fail_to_pass_evidence",
        ],
        "output_artifact_errors": [
            "unsafe:f2p.command:UnsafeRecordInputError",
            "unsafe:p2p.command:UnsafeRecordInputError",
        ],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "tests_status": {
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
        },
    }
    _write_json(eval_dir / "summary.json", source)
    attempt = {
        "phase": "eval_attempt_started",
        "task": task,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "eval_spec_sha256": eval_spec_sha,
    }
    attempts_path = eval_dir.parent / "eval_attempts.jsonl"
    attempts_path.write_text(json.dumps(attempt) + "\n", encoding="utf-8")
    source_before = (eval_dir / "summary.json").read_bytes()
    attempts_before = attempts_path.read_bytes()
    source_stat = os.stat(eval_dir / "summary.json")
    attempts_stat = os.stat(attempts_path)

    derived = rejudge(eval_dir, output_dir)

    assert derived["status"] == "done"
    assert derived["resolved"] is False
    assert derived["technical_reasons"] == []
    assert derived["output_artifact_errors"] == []
    assert derived["rejudgement"]["added_eval_attempts"] == 0
    assert derived["rejudgement"]["matching_eval_attempts"] == 1
    assert (output_dir / "source_summary.json").read_bytes() == source_before
    assert (eval_dir / "summary.json").read_bytes() == source_before
    assert attempts_path.read_bytes() == attempts_before
    assert os.stat(eval_dir / "summary.json").st_mtime_ns == source_stat.st_mtime_ns
    assert os.stat(attempts_path).st_mtime_ns == attempts_stat.st_mtime_ns
    assert json.loads((output_dir / "summary.json").read_text())["resolved"] is False
