"""Positive execution proof for ProLite language adapters."""

# ruff: noqa: F403

from __future__ import annotations

from swe_v1_prolite_runner_adapter_go_tests import *
from swe_v1_prolite_runner_adapter_javascript_tests import *
from swe_v1_prolite_runner_adapter_pytest_tests import *
from swe_v1_prolite_runner_test_support import _remote_namespace


def test_eval_runner_dependency_failures_are_infrastructure(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["eval_log_has_infra_failure"](127, "No supported JS test runner found for jest") is True
    assert namespace["eval_log_has_infra_failure"](124, "test command timed out") is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected error message 'request timed out'",
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1,
        "redis.exceptions.ConnectionError: Connection refused",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "MongoDB server unavailable: failed to connect",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected 'Connection refused' but got 'accepted'",
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1,
        "OSError: [Errno 28] No space left on device",
    ) is True
