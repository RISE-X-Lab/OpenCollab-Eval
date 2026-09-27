"""Regression tests for composed and abrupt Go target failures."""

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


def _event(**values: object) -> str:
    return json.dumps(values, sort_keys=True)


def _proof(*tests: str) -> dict[str, object]:
    return {
        "kind": "go_json_test_pass",
        "tests": list(tests),
        "dynamic_discovery": True,
    }


def _mixed_failure_log(*, diagnostic_path: str = "lib/ai/model/tokencount_test.go") -> str:
    runtime_package = "example.invalid/project/lib/ai"
    build_package = runtime_package + "/model"
    return "\n".join(
        (
            _discovery("./lib/ai", "TestChatPromptTokens", "lib/ai/chat_test.go"),
            _discovery(
                "./lib/ai/model",
                "TestTokenCount",
                "lib/ai/model/tokencount_test.go",
            ),
            _event(Action="run", Package=runtime_package, Test="TestChatPromptTokens"),
            _event(Action="fail", Package=runtime_package, Test="TestChatPromptTokens"),
            _event(Action="fail", Package=runtime_package),
            f"# {build_package}",
            f"{diagnostic_path}:70:22: undefined: defaultTokenizer",
            f"FAIL\t{build_package} [build failed]",
        )
    )


def test_exact_failure_and_target_build_failure_compose_across_packages():
    command = "exact discovery command"
    proof = _proof("TestChatPromptTokens", "TestTokenCount")

    assert go_failure_proof_matches(
        proof,
        _mixed_failure_log(),
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        proof,
        _mixed_failure_log(),
        expected_command=command,
        observed_command=command + " changed",
    )
    assert not go_failure_proof_matches(
        proof,
        _mixed_failure_log(diagnostic_path="lib/other/unrelated_test.go"),
        expected_command=command,
        observed_command=command,
    )


def _panic_log(
    *,
    target_output: bool = True,
    terminal_action: str = "",
    failed_package: str = "example.invalid/project/lib/kube/proxy",
) -> str:
    package = "example.invalid/project/lib/kube/proxy"
    target = "TestMTLSClientCAs/1000_CAs"
    panic_event: dict[str, object] = {
        "Action": "output",
        "Package": package,
        "Output": "panic: runtime error: invalid memory address or nil pointer dereference\n",
    }
    if target_output:
        panic_event["Test"] = target
    lines = [
        _discovery("./lib/kube/proxy", target, "lib/kube/proxy/server_test.go"),
        _event(Action="run", Package=package, Test=target),
        json.dumps(panic_event, sort_keys=True),
        _event(
            Action="output",
            Package=package,
            Test=target,
            Output="example.invalid/project/lib/kube/proxy.(*forwarder).ServeHTTP()\n",
        ),
    ]
    if terminal_action:
        lines.append(_event(Action=terminal_action, Package=package, Test=target))
    lines.append(_event(Action="fail", Package=failed_package))
    return "\n".join(lines)


def test_unterminated_target_attributed_panic_proves_package_failure():
    command = "exact discovery command"
    proof = _proof("TestMTLSClientCAs/1000_CAs")

    assert go_failure_proof_matches(
        proof,
        _panic_log(),
        expected_command=command,
        observed_command=command,
    )
    assert not go_failure_proof_matches(
        proof,
        _panic_log(),
        expected_command=command,
        observed_command=command + " changed",
    )


def test_panic_requires_exact_target_attribution_and_unterminated_target():
    command = "exact discovery command"
    proof = _proof("TestMTLSClientCAs/1000_CAs")

    for log in (
        _panic_log(target_output=False),
        _panic_log(terminal_action="pass"),
        _panic_log(failed_package="example.invalid/project/lib/other"),
        _panic_log().replace(
            _event(Action="fail", Package="example.invalid/project/lib/kube/proxy"),
            _event(
                Action="fail",
                Package="example.invalid/project/lib/kube/proxy",
                Test="TestUnrelated",
            ),
        ),
    ):
        assert not go_failure_proof_matches(
            proof,
            log,
            expected_command=command,
            observed_command=command,
        )


def test_timeout_panic_is_excluded_from_abrupt_panic_proof():
    command = "exact discovery command"
    proof = _proof("TestMTLSClientCAs/1000_CAs")
    log = _panic_log().replace(
        "panic: runtime error: invalid memory address or nil pointer dereference",
        "panic: test timed out after 10m0s",
    )

    assert not go_failure_proof_matches(
        proof,
        log,
        expected_command=command,
        observed_command=command,
    )


def test_target_timeout_does_not_hide_an_unproven_failed_package():
    command = "exact discovery command"
    timeout_package = "example.invalid/project/internal/server"
    other_package = "example.invalid/project/internal/other"
    timeout_target = "TestEvaluate"
    log = "\n".join(
        (
            _discovery(
                "./internal/server",
                timeout_target,
                "internal/server/evaluator_test.go",
            ),
            _discovery(
                "./internal/other",
                "TestOther",
                "internal/other/other_test.go",
            ),
            _event(Action="run", Package=timeout_package, Test=timeout_target),
            _event(
                Action="output",
                Package=timeout_package,
                Output="panic: test timed out after 10m0s\n",
            ),
            _event(
                Action="output",
                Package=timeout_package,
                Test=timeout_target,
                Output="example.invalid/project/internal/server.TestEvaluate()\n",
            ),
            _event(Action="fail", Package=timeout_package),
            _event(Action="fail", Package=other_package),
        )
    )

    assert not go_failure_proof_matches(
        _proof(timeout_target, "TestOther"),
        log,
        expected_command=command,
        observed_command=command,
    )
