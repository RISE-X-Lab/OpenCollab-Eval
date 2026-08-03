from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencollab_eval.generation import gen_prediction_openhands as gpo
from opencollab_eval.generation import openhands_runtime


def test_openhands_isolated_tools_keep_only_sdk_terminal_name() -> None:
    agent = SimpleNamespace(
        tools=[
            SimpleNamespace(name="terminal"),
            SimpleNamespace(name="file_editor"),
            SimpleNamespace(name="task_tracker"),
            SimpleNamespace(name="task_tool_set"),
        ]
    )

    tools = openhands_runtime._isolated_agent_tools(agent, "terminal")

    assert [tool.name for tool in tools] == ["terminal"]


def test_openhands_terminal_commands_use_unique_container_guard_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHANDS_CONTAINER_PYTHON", "/usr/bin/python3")
    monkeypatch.setenv(
        "OPENHANDS_CONTAINER_GUARD_ROOT",
        gpo._CONTAINER_GUARD_ROOT,
    )

    first = openhands_runtime._guarded_terminal_invocation(
        "container-123",
        "pytest -q",
    )
    second = openhands_runtime._guarded_terminal_invocation(
        "container-123",
        "git status --short",
    )

    assert first.argv[:8] == (
        "docker",
        "exec",
        "-i",
        "container-123",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-",
    )
    assert first.argv[8] == "run"
    assert first.argv[9] == first.pidfile
    assert first.argv[10] == first.cancelfile
    assert first.argv[-1] == "pytest -q"
    assert first.pidfile != second.pidfile
    assert first.cancelfile == f"{first.pidfile}.cancel"
    assert "def run(pidfile: Path, cancelfile: Path" in first.source


def test_openhands_token_budget_guard_counts_all_llm_instances() -> None:
    class Usage:
        def __init__(self, prompt_tokens, completion_tokens):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens

    class Metrics:
        def __init__(self, usage):
            self.accumulated_token_usage = usage

    class FakeLLM:
        def __init__(self, prompt_tokens, completion_tokens):
            self.metrics = Metrics(Usage(prompt_tokens, completion_tokens))

    guard = openhands_runtime.TokenBudgetGuard(100)
    first = FakeLLM(40, 10)
    second = FakeLLM(30, 10)
    first_reservation = guard.reserve(60)
    guard.record(first, reservation=first_reservation)
    second_reservation = guard.reserve(50)
    guard.record(second, reservation=second_reservation)

    assert guard.spent == 90
    assert guard.reserved == 0
    with pytest.raises(RuntimeError, match="cannot cover the next request"):
        guard.reserve(11)


def test_openhands_token_budget_reserves_request_before_api_call() -> None:
    guard = openhands_runtime.TokenBudgetGuard(100)
    first = guard.reserve(70)

    with pytest.raises(RuntimeError, match="cannot cover the next request"):
        guard.reserve(31)

    class Usage:
        prompt_tokens = 40
        completion_tokens = 10

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        metrics = Metrics()

    guard.record(FakeLLM(), reservation=first)
    assert guard.spent == 50
    assert guard.reserve(50) == 50
