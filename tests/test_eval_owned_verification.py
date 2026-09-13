from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from opencollab.tools import VerificationTool

from opencollab_eval.engine.environment import ExecResult
from opencollab_eval.verification import evaluation_tools

TARGET = "tests/test_example.py::test_case"


def execute(tool, output, *, code=0, truncated=False, target=TARGET, **params):
    class Environment:
        process_isolated = True
        workspace = "/work"

        async def read_text_range(self, *args, **kwargs):
            raise FileNotFoundError

        async def exec_cmd(self, command, timeout=300):
            return ExecResult(code, output, "", truncated)

    runtime = SimpleNamespace(environment=Environment(), safety_policy=None, confirm_fn=lambda: None)
    return asyncio.run(tool.execute_with_runtime({"target": target, **params}, runtime))


def test_evaluation_owned_tool_keeps_latest_exact_target_evidence():
    tool = evaluation_tools("run_tests")[0]
    assert isinstance(tool, VerificationTool)
    passed = f"PASSED {TARGET}\n================ 1 passed in 0.01s ================"
    assert "Verdict: GREEN" in execute(tool, passed)
    assert TARGET in tool.verified_targets
    assert tool.verification_records[-1]["verified"] is True
    assert "Verdict: RED" in execute(tool, "1 failed in 0.01s", code=1)
    assert TARGET not in tool.verified_targets
    assert tool.verification_records[-1]["verified"] is False
    assert len(tool.verification_records) == 2


@pytest.mark.parametrize("output", [
    "", "usage: pytest [options]", "1 test collected in 0.01s", "no tests ran in 0.01s",
    "PASSED tests/test_other.py::test_case\n1 passed in 0.01s",
])
def test_successful_process_without_requested_test_execution_cannot_pass(output):
    tool = evaluation_tools("run_tests")[0]
    assert "Verdict: RED" in execute(tool, output)
    assert not tool.verified_targets


def test_truncated_pass_output_cannot_pass():
    tool = evaluation_tools("run_tests")[0]
    assert "Verdict: RED" in execute(tool, f"PASSED {TARGET}\n1 passed in 0.01s", truncated=True)
    assert not tool.verified_targets


def test_model_roles_retain_runner_and_sandbox_restrictions():
    tool = evaluation_tools("run_tests")[0]
    assert tool.require_process_isolation
    assert "runner override is disabled" in execute(tool, "", runner="echo fake")
    assert "extra_args is disabled" in execute(tool, "", extra_args="--collect-only")
    with pytest.raises(ValueError, match="unique"):
        evaluation_tools("run_tests", "run_tests")


def test_mechanical_verifier_preserves_override_for_authorized_fixed_commands():
    tool = evaluation_tools("run_tests", headless=False)[0]
    assert tool.allow_runner_override
    assert "Verdict: GREEN" in execute(tool, f"PASSED {TARGET}\n1 passed in 0.01s", runner="python -m pytest")
    assert tool.verification_records[-1]["target"] == TARGET
