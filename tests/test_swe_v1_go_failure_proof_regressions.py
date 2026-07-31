"""Regression tests for Go target failures emitted before a test can run."""

from __future__ import annotations

import json

from opencollab_eval.engine.swe_v1_go_failure_proof import (
    GO_TARGET_DISCOVERY_PREFIX,
    go_failure_proof_matches,
)


def _discovery(package: str, test: str, test_file: str) -> str:
    return GO_TARGET_DISCOVERY_PREFIX + json.dumps(
        {"package": package, "tests": [test], "test_files": [test_file]},
        sort_keys=True,
    )


def _dynamic_proof(test: str) -> dict[str, object]:
    return {
        "kind": "go_json_test_pass",
        "tests": [test],
        "dynamic_discovery": True,
    }


def test_external_test_package_plain_build_failure_proves_target_failure():
    command = "exact discovery command"
    log = "\n".join(
        (
            _discovery(
                "./lib/auth/touchid",
                "TestRegisterAndLogin",
                "lib/auth/touchid/api_test.go",
            ),
            (
                "# github.com/gravitational/teleport/lib/auth/touchid_test "
                "[github.com/gravitational/teleport/lib/auth/touchid.test]"
            ),
            (
                "lib/auth/touchid/api_test.go:71:22: cannot use &fakeNative{} "
                "as touchid.nativeTID"
            ),
            (
                "\t*fakeNative does not implement touchid.nativeTID "
                "(missing IsAvailable method)"
            ),
            (
                "FAIL\tgithub.com/gravitational/teleport/lib/auth/touchid "
                "[build failed]"
            ),
        )
    )

    assert go_failure_proof_matches(
        _dynamic_proof("TestRegisterAndLogin"),
        log,
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        _dynamic_proof("TestRegisterAndLogin"),
        log,
        expected_command=command,
        observed_command=command + " changed",
    )


def test_external_test_package_json_build_failure_proves_target_failure():
    command = "exact discovery command"
    package = "github.com/navidrome/navidrome/server/subsonic/responses"
    build_package = package + "_test [" + package + ".test]"
    events = (
        {
            "ImportPath": build_package,
            "Action": "build-output",
            "Output": (
                "server/subsonic/responses/responses_test.go:549:6: "
                "unknown field Url\n"
            ),
        },
        {"ImportPath": build_package, "Action": "build-fail"},
        {
            "Action": "output",
            "Package": package,
            "Output": f"FAIL\t{package} [build failed]\n",
        },
        {"Action": "fail", "Package": package, "FailedBuild": build_package},
    )
    log = "\n".join(
        (
            _discovery(
                "./server/subsonic/responses",
                "TestSubsonicApiResponses",
                "server/subsonic/responses/responses_suite_test.go",
            ),
            *(json.dumps(event) for event in events),
        )
    )

    assert go_failure_proof_matches(
        _dynamic_proof("TestSubsonicApiResponses"),
        log,
        expected_command=command,
        observed_command=command,
    )


def test_exact_target_setup_failure_proves_target_failure():
    command = "exact discovery command"
    log = "\n".join(
        (
            _discovery(
                "./lib/auditd",
                "TestSendEvent",
                "lib/auditd/auditd_test.go",
            ),
            "# github.com/gravitational/teleport/lib/auditd",
            (
                "lib/auditd/auditd_test.go:28:2: no required module provides "
                "package github.com/mdlayher/netlink"
            ),
            (
                "FAIL\tgithub.com/gravitational/teleport/lib/auditd "
                "[setup failed]"
            ),
        )
    )

    assert go_failure_proof_matches(
        _dynamic_proof("TestSendEvent"),
        log,
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        _dynamic_proof("TestSendEvent"),
        log.replace(
            "lib/auditd/auditd_test.go:28",
            "lib/other/other_test.go:28",
        ),
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        _dynamic_proof("TestSendEvent"),
        log.replace(
            (
                "lib/auditd/auditd_test.go:28:2: no required module provides "
                "package github.com/mdlayher/netlink"
            ),
            "go: shared module proxy unavailable",
        ),
        expected_command=command,
        observed_command=command,
    )


def test_exact_running_target_timeout_proves_target_failure():
    command = "exact discovery command"
    package = "go.flipt.io/flipt/internal/server"
    target = "TestEvaluate_FirstRolloutRuleIsZero"
    events = (
        {"Action": "run", "Package": package, "Test": target},
        {
            "Action": "output",
            "Package": package,
            "Output": "panic: test timed out after 10m0s\n",
        },
        {
            "Action": "output",
            "Package": package,
            "Test": target,
            "Output": f"{package}.{target}.func1(0xc000321dc0)\n",
        },
        {
            "Action": "output",
            "Package": package,
            "Output": f"FAIL\t{package}\t600.120s\n",
        },
        {"Action": "fail", "Package": package, "Elapsed": 600.12},
    )
    log = "\n".join(
        (
            _discovery("./internal/server", target, "internal/server/evaluator_test.go"),
            *(json.dumps(event) for event in events),
        )
    )

    assert go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command + " changed",
    )


def test_generic_package_timeout_does_not_prove_target_failure():
    command = "exact discovery command"
    package = "go.flipt.io/flipt/internal/server"
    target = "TestEvaluate_FirstRolloutRuleIsZero"
    log = "\n".join(
        (
            _discovery("./internal/server", target, "internal/server/evaluator_test.go"),
            json.dumps({"Action": "run", "Package": package, "Test": target}),
            json.dumps(
                {
                    "Action": "output",
                    "Package": package,
                    "Output": "panic: test timed out after 10m0s\n",
                }
            ),
            json.dumps({"Action": "fail", "Package": package, "Elapsed": 600.12}),
        )
    )

    assert not go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command,
    )


def test_passed_target_is_not_blamed_for_an_unrelated_timeout():
    command = "exact discovery command"
    package = "go.flipt.io/flipt/internal/server"
    target = "TestWanted"
    log = "\n".join(
        (
            _discovery("./internal/server", target, "internal/server/evaluator_test.go"),
            json.dumps({"Action": "run", "Package": package, "Test": target}),
            json.dumps({"Action": "pass", "Package": package, "Test": target}),
            json.dumps({"Action": "run", "Package": package, "Test": "TestUnrelated"}),
            json.dumps(
                {
                    "Action": "output",
                    "Package": package,
                    "Output": "panic: test timed out after 10m0s\nTestWanted\n",
                }
            ),
            json.dumps({"Action": "fail", "Package": package, "Elapsed": 600.12}),
        )
    )

    assert not go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command,
    )


def test_skipped_target_is_not_blamed_for_an_unrelated_timeout():
    command = "exact discovery command"
    package = "go.flipt.io/flipt/internal/server"
    target = "TestWanted"
    log = "\n".join(
        (
            _discovery("./internal/server", target, "internal/server/evaluator_test.go"),
            json.dumps({"Action": "run", "Package": package, "Test": target}),
            json.dumps(
                {
                    "Action": "output",
                    "Package": package,
                    "Test": target,
                    "Output": "TestWanted skipped by platform\n",
                }
            ),
            json.dumps({"Action": "skip", "Package": package, "Test": target}),
            json.dumps({"Action": "run", "Package": package, "Test": "TestUnrelated"}),
            json.dumps(
                {
                    "Action": "output",
                    "Package": package,
                    "Output": "panic: test timed out after 10m0s\n",
                }
            ),
            json.dumps({"Action": "fail", "Package": package, "Elapsed": 600.12}),
        )
    )

    assert not go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command,
    )
