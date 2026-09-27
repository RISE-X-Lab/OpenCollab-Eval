import json

import pytest

from opencollab_eval.engine.swe_v1_go_failure_proof import go_failure_proof_matches


def log(outcome="fail", diagnostic="telemetry/reporter_test.go"):
    lines = [
        'OPENCOLLAB_GO_TARGET_DISCOVERY ' + json.dumps({
            "package": "./config", "tests": ["TestLoad"], "test_files": ["config/config_test.go"]
        }),
        json.dumps({"Action": "run", "Package": "example.org/app/config", "Test": "TestLoad"}),
        json.dumps({"Action": outcome, "Package": "example.org/app/config", "Test": "TestLoad"}),
        json.dumps({"Action": outcome, "Package": "example.org/app/config"}),
        'OPENCOLLAB_GO_TARGET_DISCOVERY ' + json.dumps({
            "package": "./telemetry", "tests": ["TestReport"], "test_files": ["telemetry/reporter_test.go"]
        }),
        json.dumps({
            "Action": "build-output", "ImportPath": "example.org/client/v3",
            "Output": f"{diagnostic}:18:2: no required module provides package example.org/client/v3; to add it:\n",
        }),
        json.dumps({"Action": "build-fail", "ImportPath": "example.org/client/v3"}),
        json.dumps({
            "Action": "fail", "Package": "example.org/app/telemetry", "FailedBuild": "example.org/client/v3",
        }),
    ]
    return "\n".join(lines)


def proof():
    return {"kind": "go_json_test_pass", "tests": ["TestLoad", "TestReport"], "dynamic_discovery": True}


def test_completed_target_failure_is_decisive_despite_other_test_dependency():
    assert go_failure_proof_matches(proof(), log(), expected_command="official", observed_command="official")


@pytest.mark.parametrize("outcome,diagnostic,command", [
    ("pass", "telemetry/reporter_test.go", "official"),
    ("fail", "unrelated/test.go", "official"),
    ("fail", "telemetry/reporter_test.go", "different"),
])
def test_dependency_errors_alone_and_unbound_evidence_remain_technical(outcome, diagnostic, command):
    assert not go_failure_proof_matches(
        proof(), log(outcome, diagnostic), expected_command="official", observed_command=command
    )
