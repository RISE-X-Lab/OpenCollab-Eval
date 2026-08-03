from __future__ import annotations

import asyncio

import pytest
from test_gen_prediction_single_agent import (
    RecordingRuntime,
    _agent_config,
    _reserve_empty_artifact_dir,
    _runtime_result,
    gp,
)


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        (
            RecordingRuntime(_runtime_result(), write_trajectory=False),
            "trajectory cannot be read",
        ),
        (
            RecordingRuntime(_runtime_result(), trajectory_type="tool_exec"),
            "contains no verified LLM call",
        ),
        (
            RecordingRuntime(
                _runtime_result(),
                trajectory_payload={
                    "model": "model",
                    "provider_model": "other-model",
                    "wire_protocol": "chat_completions",
                    "reasoning_effort": None,
                },
            ),
            "provider model mismatch",
        ),
    ],
    ids=["missing-trajectory", "zero-llm-call", "provider-model-mismatch"],
)
def test_single_agent_rejects_unverified_trajectory(
    monkeypatch,
    tmp_path,
    runtime,
    message,
):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            4,
            100,
            1,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    assert metrics["workflow_status"] == "error"
    assert metrics["candidate_probe_eligible"] is False
    assert metrics["submission_eligible"] is False
    assert metrics["trajectory_llm_call_count"] == 0
    assert message in metrics["error"]


def test_single_agent_terminal_provider_failure_blocks_candidate(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            agent_failures=(
                {
                    "label": "solver",
                    "exception_type": "PermissionDeniedError",
                    "status_code": 403,
                    "provider_error_type": "access_terminated_error",
                },
            )
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            4,
            100,
            1,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    assert metrics["workflow_status"] == "provider_request_rejected"
    assert metrics["candidate_probe_eligible"] is False
    assert metrics["submission_eligible"] is False
    assert metrics["provider_failure"]["http_statuses"] == [403]
