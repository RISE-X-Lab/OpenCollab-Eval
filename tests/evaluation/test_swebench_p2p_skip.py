"""Compare executed P2P skips with the installed SWE-bench grading rule."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys

import pytest

from opencollab_eval.engine.swe_eval_outcome import classify_evaluation, derive_eval_verdict
from opencollab_eval.engine.swe_eval_records import _direct_eval_plan_status
from opencollab_eval.engine.swe_v1_remote_artifacts import _read_plan_evidence
from opencollab_eval.engine.swe_v1_remote_pytest_proof import prolite_pytest_proof_plugin_source
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


def _execute_target(root, prefix, targets):
    plan = prolite_test_plan({"repo_language": "python", "repo": "normal/project"}, targets)
    command = plan["commands"][0]
    stem = root / f"{prefix}.batch_001"
    events_file = root / f"{prefix}.worker-events.jsonl"
    fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    env = dict(os.environ, PYTHONPATH=str(root), OPENCOLLAB_PYTEST_EVENT_FD=str(fd))
    try:
        proc = subprocess.run([sys.executable, "-m", *shlex.split(command)], cwd=root, env=env,
                              pass_fds=(fd,), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        os.close(fd)
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    raw = b"".join((json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode() for event in events)
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1", "worker_pid": 1234,
        "worker_uid": 65534, "controller_uid": 0, "command_sha256": plan["proofs"][0]["command_sha256"],
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof", "worker_returncode": proc.returncode,
        "event_stream_sha256": hashlib.sha256(raw).hexdigest(),
    }
    for suffix, value in {
        ".proof.fixture.jsonl": "".join(json.dumps(event) + "\n" for event in events),
        ".command": command + "\n", ".exit": str(proc.returncode) + "\n", ".log": proc.stdout,
    }.items():
        root.joinpath(stem.name + suffix).write_text(value)
    errors = []
    evidence = _read_plan_evidence(root, errors, prefix, plan, "fixture")
    assert not errors
    return plan, evidence


@pytest.mark.parametrize("mixed", [False, True])
def test_executed_p2p_skip_matches_official_resolution_and_report_consumer(tmp_path, mixed):
    pytest.importorskip("swebench")
    from swebench.harness.grading import get_eval_tests_report, get_resolution_status

    (tmp_path / "opencollab_pytest_proof.py").write_text(prolite_pytest_proof_plugin_source())
    (tmp_path / "test_normal.py").write_text(
        "import pytest\n"
        "def test_fixed():\n    assert 2 + 3 == 5\n"
        "def test_maintained():\n    assert True\n"
        "@pytest.mark.skipif(True, reason='platform-specific behavior')\n"
        "def test_platform():\n    assert True\n"
    )
    f2p = ["test_normal.py::test_fixed"]
    p2p = ["test_normal.py::test_platform"]
    if mixed:
        p2p.append("test_normal.py::test_maintained")
    f2p_plan, f2p_evidence = _execute_target(tmp_path, "f2p", f2p)
    p2p_plan, p2p_evidence = _execute_target(tmp_path, "p2p", p2p)
    official_statuses = {f2p[0]: "PASSED", p2p[0]: "SKIPPED"}
    if mixed:
        official_statuses[p2p[1]] = "PASSED"
    official = get_eval_tests_report(official_statuses, {"FAIL_TO_PASS": f2p, "PASS_TO_PASS": p2p})
    assert get_resolution_status(official) == "RESOLVED_FULL"
    artifacts = {
        "f2p_evidence": f2p_evidence, "p2p_evidence": p2p_evidence,
        "f2p_status": 0, "p2p_status": 0, "f2p_execution_evidence_complete": True,
        "p2p_execution_evidence_complete": True, "output_artifact_errors": [],
        "base_commit_status": 0, "base_snapshot": {"verified": True}, "candidate_projection": {"verified": True},
        "service_status": 0, "before_status": 0, "post_before_base_status": 0, "model_status": 0, "test_status": 0,
    }
    verdict = derive_eval_verdict(artifacts, docker_exit=0, cleanup_quiesced=True, container_cleanup={"ok": True})
    assert verdict["resolved"] is True
    assert verdict["outcome"] == "resolved"
    tests_status = {
        "pass_to_pass_skips_allowed": True,
        "fail_to_pass_plan": f2p_plan, "fail_to_pass_evidence": f2p_evidence, "fail_to_pass_status": 0,
        "pass_to_pass_plan": p2p_plan, "pass_to_pass_evidence": p2p_evidence, "pass_to_pass_status": 0,
    }
    assert _direct_eval_plan_status(tests_status, "pass_to_pass", p2p_plan, require_commands=True) == 0
    strict = derive_eval_verdict(
        artifacts, docker_exit=0, cleanup_quiesced=True, container_cleanup={"ok": True},
        pass_to_pass_skips_allowed=False,
    )
    assert strict["outcome"] == "unresolved"
    tests_status["pass_to_pass_skips_allowed"] = False
    assert _direct_eval_plan_status(tests_status, "pass_to_pass", p2p_plan, require_commands=True) == 1
    assert classify_evaluation(evidence=p2p_evidence).outcome.value == "unresolved"
    unknown = [{**item, "target_skip_proof_matches_plan": False} for item in p2p_evidence]
    unknown_verdict = classify_evaluation(evidence=f2p_evidence, pass_to_pass_evidence=unknown)
    assert unknown_verdict.outcome.value == "technical_failure"
    assert classify_evaluation(evidence=[], pass_to_pass_evidence=p2p_evidence).outcome.value == "technical_failure"
    invalid_skip = [{**item, "status": 5} for item in p2p_evidence]
    invalid_verdict = classify_evaluation(evidence=f2p_evidence, pass_to_pass_evidence=invalid_skip)
    assert invalid_verdict.outcome.value == "technical_failure"
    tests_status.pop("pass_to_pass_skips_allowed")
    assert _direct_eval_plan_status(tests_status, "pass_to_pass", p2p_plan, require_commands=True) == 1
    empty_plan = {**f2p_plan, "commands": [], "declared_targets": [], "target_batches": [], "proofs": []}
    empty_status = {"fail_to_pass_plan": empty_plan, "fail_to_pass_evidence": [], "fail_to_pass_status": 0}
    assert _direct_eval_plan_status(empty_status, "fail_to_pass", empty_plan, require_commands=True) is None
