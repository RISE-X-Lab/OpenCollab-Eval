"""Regression tests for Go target failures emitted before a test can run."""

from __future__ import annotations

import json

from opencollab_eval.engine.swe_v1_go_failure_proof import (
    GO_TARGET_DISCOVERY_PREFIX,
    go_failure_proof_matches,
    go_pass_proof_matches,
)


def _discovery(package: str, test: str, test_file: str) -> str:
    return GO_TARGET_DISCOVERY_PREFIX + json.dumps(
        {"package": package, "tests": [test], "test_files": [test_file]},
        sort_keys=True,
    )


def _dynamic_proof(
    test: str,
    *,
    candidate_source_paths: list[str] | None = None,
) -> dict[str, object]:
    proof: dict[str, object] = {
        "kind": "go_json_test_pass",
        "tests": [test],
        "dynamic_discovery": True,
    }
    if candidate_source_paths is not None:
        proof["candidate_source_paths"] = candidate_source_paths
    return proof


def _legacy_command(test: str) -> str:
    return rf'''python3 -c 'import json
import pathlib
import re
import subprocess
names = json.loads('["{test}"]')
for path in pathlib.Path(".").rglob("*_test.go"):
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?m)^func\s+" + re.escape(name) + r"\s*\(", text):
        pass
print("unable to map Go tests to packages: " + "")
subprocess.run(["go", "test", "-count=1", "-json", package, "-run", pattern])
' '''


def test_module_download_progress_does_not_hide_a_successful_target() -> None:
    """Go may write dependency-download progress outside its JSON stream."""
    proof = {
        "kind": "go_json_test_pass",
        "test": "TestEvaluate",
        "package": "./internal/server",
        "test_file": "internal/server/evaluator_test.go",
    }
    package = "example.invalid/project/internal/server"
    log = "\n".join(
        (
            "go: downloading golang.org/x/sync v0.7.0",
            json.dumps({"Action": "start", "Package": package}),
            json.dumps(
                {"Action": "run", "Package": package, "Test": "TestEvaluate"}
            ),
            json.dumps(
                {
                    "Action": "output",
                    "Package": package,
                    "Test": "TestEvaluate",
                    "Output": "ok\n",
                }
            ),
            json.dumps(
                {"Action": "pass", "Package": package, "Test": "TestEvaluate"}
            ),
            json.dumps({"Action": "pass", "Package": package}),
        )
    )

    assert go_pass_proof_matches(proof, log)


def test_toolchain_download_progress_does_not_hide_a_successful_target() -> None:
    """Go's automatic toolchain selection can precede the JSON event stream."""
    proof = {
        "kind": "go_json_test_pass",
        "test": "TestEvaluate",
        "package": "./internal/server",
        "test_file": "internal/server/evaluator_test.go",
    }
    package = "example.invalid/project/internal/server"
    events = (
        {"Action": "start", "Package": package},
        {"Action": "run", "Package": package, "Test": "TestEvaluate"},
        {
            "Action": "output",
            "Package": package,
            "Test": "TestEvaluate",
            "Output": "ok\n",
        },
        {"Action": "pass", "Package": package, "Test": "TestEvaluate"},
        {"Action": "pass", "Package": package},
    )
    event_log = "\n".join(json.dumps(event) for event in events)

    assert go_pass_proof_matches(
        proof,
        "go: downloading go1.21.0 (linux/amd64)\n" + event_log,
    )
    assert go_pass_proof_matches(
        proof,
        "go: downloading go1.21rc3\n" + event_log,
    )
    # Do not turn an arbitrary diagnostic that resembles the prefix into a
    # successful proof.
    assert not go_pass_proof_matches(
        proof,
        "go: downloading go1.21.0 (linux/amd64) unexpected\n" + event_log,
    )


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


def test_multi_package_build_failure_is_not_hidden_by_other_target_passes():
    command = "exact discovery command"
    failed_package = "github.com/navidrome/navidrome/core/agents/lastfm"
    passing_packages = {
        "github.com/navidrome/navidrome/core/agents/listenbrainz": "TestListenBrainz",
        "github.com/navidrome/navidrome/core/agents/spotify": "TestSpotify",
    }
    discoveries = [
        _discovery("./core/agents/lastfm", "TestLastFM", "core/agents/lastfm/client_test.go"),
        *(
            _discovery("./" + package.split("github.com/navidrome/navidrome/", 1)[1], test, "unused_test.go")
            for package, test in passing_packages.items()
        ),
    ]
    events = [
        {
            "ImportPath": failed_package + " [" + failed_package + ".test]",
            "Action": "build-output",
            "Output": "core/agents/lastfm/client_test.go:131:18: client.GetToken undefined\n",
        },
        {"ImportPath": failed_package + " [" + failed_package + ".test]", "Action": "build-fail"},
        {
            "Action": "output",
            "Package": failed_package,
            "Output": f"FAIL\t{failed_package} [build failed]\n",
        },
        {"Action": "fail", "Package": failed_package},
        *(
            {"Action": "pass", "Package": package, "Test": test}
            for package, test in passing_packages.items()
        ),
    ]
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestLastFM", *passing_packages.values()],
        "dynamic_discovery": True,
    }
    log = "\n".join((*discoveries, *(json.dumps(event) for event in events)))

    assert go_failure_proof_matches(
        proof,
        log,
        expected_command=command,
        observed_command=command,
    )

    events.append({"Action": "pass", "Package": failed_package, "Test": "TestLastFM"})
    contradictory_log = "\n".join((*discoveries, *(json.dumps(event) for event in events)))
    assert not go_failure_proof_matches(
        proof,
        contradictory_log,
        expected_command=command,
        observed_command=command,
    )


def test_candidate_production_source_build_failure_proves_target_failure():
    command = "exact discovery command"
    package = "example.invalid/project/internal/server"
    target = "TestEvaluate"
    events = (
        {
            "ImportPath": package + " [" + package + ".test]",
            "Action": "build-output",
            "Output": "internal/server/evaluator.go:42:9: undefined: rollout\n",
        },
        {
            "ImportPath": package + " [" + package + ".test]",
            "Action": "build-fail",
        },
        {
            "Action": "output",
            "Package": package,
            "Output": f"FAIL\t{package} [build failed]\n",
        },
        {"Action": "fail", "Package": package},
    )
    log = "\n".join(
        (
            _discovery("./internal/server", target, "internal/server/evaluator_test.go"),
            *(json.dumps(event) for event in events),
        )
    )
    proof = _dynamic_proof(
        target,
        candidate_source_paths=["internal/server/evaluator.go"],
    )

    assert go_failure_proof_matches(
        proof,
        log,
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        _dynamic_proof(target),
        log,
        expected_command=command,
        observed_command=command,
    )
    proof["candidate_source_paths"] = ["internal/server/unrelated.go"]
    assert not go_failure_proof_matches(
        proof,
        log,
        expected_command=command,
        observed_command=command,
    )


def test_legacy_dynamic_production_failure_requires_candidate_path():
    target = "TestScanner"
    command = _legacy_command(target)
    package = "github.com/navidrome/navidrome/scanner"
    log = "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "ImportPath": f"{package} [{package}.test]",
                "Action": "build-output",
                "Output": "scanner/walk_dir_tree.go:21:20: undefined: walkResults\n",
            },
            {"ImportPath": f"{package} [{package}.test]", "Action": "build-fail"},
            {
                "Action": "output",
                "Package": package,
                "Output": f"FAIL\t{package} [build failed]\n",
            },
            {"Action": "fail", "Package": package},
        )
    )
    proof = {"kind": "go_json_test_pass", "tests": [target]}

    assert not go_failure_proof_matches(
        proof,
        log,
        expected_command=command,
        observed_command=command,
    )
    proof["candidate_source_paths"] = ["scanner/walk_dir_tree.go"]
    assert go_failure_proof_matches(
        proof,
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
