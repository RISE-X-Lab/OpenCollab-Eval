import json

from opencollab_eval.engine.swe_v1_go_failure_proof import go_failure_proof_matches


def fixture(dependency="example.com/project/models", path="models/utils.go", link=True):
    target = "example.com/project/detector"
    events = [
        {"Action": "build-output", "ImportPath": dependency,
         "Output": f"{path}:130:63: undefined: upstream.Fortinet\n"},
        {"Action": "build-fail", "ImportPath": dependency},
        {"Action": "start", "Package": target},
        {"Action": "output", "Package": target, "Output": f"FAIL\t{target} [build failed]\n"},
        {"Action": "fail", "Package": target},
    ]
    if link:
        events[-1]["FailedBuild"] = dependency
    discovery = {"package": "./detector", "test_files": ["detector/detector_test.go"], "tests": ["TestFeature"]}
    text = "OPENCOLLAB_GO_TARGET_DISCOVERY " + json.dumps(discovery) + "\n"
    text += "\n".join(json.dumps(event) for event in events)
    proof = {"kind": "go_json_test_pass", "tests": ["TestFeature"], "dynamic_discovery": True,
             "candidate_source_paths": ["models/utils.go", "detector/detector.go"]}
    return proof, text


def test_changed_dependency_build_is_a_candidate_failure():
    proof, text = fixture()
    assert go_failure_proof_matches(proof, text, expected_command="go test", observed_command="go test")


def test_unmodified_dependency_source_is_not_attributed_to_candidate():
    proof, text = fixture(path="models/unchanged.go")
    assert not go_failure_proof_matches(proof, text, expected_command="go test", observed_command="go test")


def test_external_dependency_and_missing_link_remain_unproven():
    for arguments in ({"dependency": "example.com/external/models"}, {"link": False}):
        proof, text = fixture(**arguments)
        assert not go_failure_proof_matches(proof, text, expected_command="go test", observed_command="go test")


def test_dependency_failure_requires_the_recorded_command():
    proof, text = fixture()
    assert not go_failure_proof_matches(proof, text, expected_command="go test", observed_command="other")
