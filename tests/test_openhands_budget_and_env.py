from __future__ import annotations

from pathlib import Path

import pytest
from gen_prediction_openhands_support import (
    install_fake_openhands_process as _install_fake_openhands_process,
)

from opencollab_eval.generation import gen_prediction_openhands as gpo
from opencollab_eval.generation import openhands_runtime


def test_run_openhands_passes_effective_runtime_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    leaked_id = "owner__repo-deadbeef"
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "900")
    monkeypatch.setenv("OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT", "1800")
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT", "600")
    monkeypatch.setenv(
        "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR",
        f"/trusted/runs/{leaked_id}/workflow_logs",
    )
    monkeypatch.setenv("SWE_TASK_ID", leaked_id)
    _install_fake_openhands_process(
        monkeypatch,
        stdout="done",
        captured=captured,
    )
    gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
        context_window=400000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32768,
        token_budget=16000000,
        max_steps=60,
    )

    env = captured["env"]
    assert env["OPENHANDS_CONTEXT_WINDOW"] == "400000"
    assert env["OPENHANDS_TEMPERATURE"] == "1.0"
    assert env["OPENHANDS_TOP_P"] == "1.0"
    assert env["OPENHANDS_MAX_OUTPUT_TOKENS"] == "32768"
    assert env["OPENHANDS_TOKEN_BUDGET"] == "16000000"
    assert env["OPENHANDS_MAX_STEPS"] == "60"
    assert env["OPENHANDS_EMPTY_PATCH_REJECTIONS"] == "0"
    assert env["OPENCOLLAB_LLM_TIMEOUT"] == "900"
    assert env["OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT"] == "1800"
    assert env["OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT"] == "600"
    assert env["OPENHANDS_CONTAINER_PYTHON"] == "/usr/bin/python3"
    assert env["OPENHANDS_CONTAINER_GUARD_ROOT"] == gpo._CONTAINER_GUARD_ROOT
    assert "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR" not in env
    assert "SWE_TASK_ID" not in env
    assert all(leaked_id not in value for value in env.values())
    assert captured["start_new_session"] is True
    assert captured["shell"] is False
    assert captured["command"][1:3] == [
        "-m",
        "opencollab_eval.generation.openhands_process_supervisor",
    ]


def test_openhands_token_budget_uses_observed_usage_not_context_window() -> None:
    class Usage:
        prompt_tokens = 0
        completion_tokens = 0

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        max_input_tokens = 1_048_576
        max_output_tokens = 32_768
        metrics = Metrics()

    llm = FakeLLM()
    guard = openhands_runtime.TokenBudgetGuard(16_000_000)

    # A context window is a ceiling, not the size of a short request.
    assert guard.request_upper_bound(llm) == 32_768
    reservation = guard.reserve(guard.request_upper_bound(llm))
    llm.metrics.accumulated_token_usage.prompt_tokens = 100
    llm.metrics.accumulated_token_usage.completion_tokens = 20
    guard.record(llm, reservation=reservation)

    assert guard.request_upper_bound(llm) == 120
    for _ in range(200):
        reservation = guard.reserve(guard.request_upper_bound(llm))
        llm.metrics.accumulated_token_usage.prompt_tokens += 100
        llm.metrics.accumulated_token_usage.completion_tokens += 20
        guard.record(llm, reservation=reservation)
    assert guard.spent == 24_120
    assert guard.reserved == 0


@pytest.mark.parametrize(
    "prompt, completion, expected",
    [
        (1.9, 0.1, (2, 1)),
        (9_007_199_254_740_993, "9007199254740993", (9_007_199_254_740_993, 9_007_199_254_740_993)),
        (True, float("nan"), (0, 0)),
    ],
)
def test_openhands_usage_rounds_fractional_counts_up_without_float_loss(
    prompt, completion, expected
) -> None:
    class Usage:
        prompt_tokens = prompt
        completion_tokens = completion

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        metrics = Metrics()

    assert openhands_runtime._usage_parts(FakeLLM()) == expected


def test_openhands_fractional_usage_cannot_be_truncated_under_budget() -> None:
    class Usage:
        prompt_tokens = 1.9
        completion_tokens = 0.0

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        metrics = Metrics()

    guard = openhands_runtime.TokenBudgetGuard(1)
    reservation = guard.reserve(1)
    with pytest.raises(RuntimeError, match="token budget exceeded"):
        guard.record(FakeLLM(), reservation=reservation)


def test_openhands_token_budget_still_enforces_actual_usage_after_small_reservation() -> None:
    class Usage:
        prompt_tokens = 0
        completion_tokens = 0

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        max_input_tokens = 1_048_576
        max_output_tokens = 32_768
        metrics = Metrics()

    llm = FakeLLM()
    guard = openhands_runtime.TokenBudgetGuard(100)
    reservation = guard.reserve(guard.request_upper_bound(llm))
    llm.metrics.accumulated_token_usage.prompt_tokens = 150
    with pytest.raises(RuntimeError, match="token budget exceeded"):
        guard.record(llm, reservation=reservation)
