"""JavaScript adapter execution-proof tests."""

from __future__ import annotations

import json

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_prolite_js_file_target_uses_positive_event_parser(
    tmp_path,
    language,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": language},
        ["test/widget.test.js"],
    )

    assert plan["coverage_verified"] is True
    assert plan["adapter"] == "jest-json-verbose"
    assert "--json" in plan["commands"][0]
    assert plan["proofs"] == [
        {
            "kind": "js_parser_backed_targets",
            "targets": ["test/widget.test.js"],
            "repo_language": language,
            "repo": "",
        }
    ]


def test_task9_jest_suite_load_failure_binds_official_mock_and_exact_command(tmp_path):
    namespace = _remote_namespace(tmp_path)
    suite = (
        "packages/components/components/drawer/views/SecurityCenter/PassAliases/"
        "PassAliases.test.tsx"
    )
    declared_suite = suite.removeprefix("packages/components/")
    missing_module = (
        "@proton/components/components/drawer/views/SecurityCenter/PassAliases/"
        "usePassAliasesProviderSetup"
    )
    target = declared_suite + " | PassAliases renders the aliases list"
    test_patch = (
        f"diff --git a/{suite} b/{suite}\n"
        f"--- a/{suite}\n"
        f"+++ b/{suite}\n"
        "@@ -1 +1,2 @@\n"
        "+jest.mock('"
        + missing_module
        + "', () => ({ usePassAliasesSetup: () => {} }));\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "js",
            "repo": "protonmail/webclients",
            "selected_test_files_to_run": json.dumps([suite]),
            "test_patch": test_patch,
        },
        [target],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    result = {
        "numFailedTestSuites": 1,
        "numRuntimeErrorTestSuites": 1,
        "numTotalTestSuites": 1,
        "numTotalTests": 0,
        "success": False,
        "testResults": [
            {
                "assertionResults": [],
                "name": "/app/" + suite,
                "status": "failed",
                "message": (
                    "  ● Test suite failed to run\n\n"
                    f"    Cannot find module '{missing_module}' from '{declared_suite}'\n"
                ),
            }
        ],
    }
    log = f"FAIL {suite}\n" + json.dumps(result) + "\n"

    assert proof["suite_module_mocks"] == [
        {"suite": suite, "modules": [missing_module]}
    ]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
    rejected = (
        log.replace(missing_module, "@proton/components/unbound", 1),
        log.replace(declared_suite, "components/other.test.tsx", 1),
        log.replace("numRuntimeErrorTestSuites\": 1", "numRuntimeErrorTestSuites\": 0"),
        log.replace("Cannot find module", "Validation Error"),
        log.replace("FAIL " + suite, "FAIL packages/components/other.test.tsx"),
    )
    for bad_log in rejected:
        assert namespace["_plan_log_failure_proof_matches"](
            proof, bad_log, "", command, command
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command + " changed"
    ) is False
