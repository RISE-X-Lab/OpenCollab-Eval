"""Each single-agent attempt retains its own evidence location and usage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_gen_prediction_single_agent import (
    RecordingRuntime,
    _agent_config,
    _reserve_empty_artifact_dir,
    _runtime_result,
    gp,
)


def test_single_agent_runtime_error_retains_its_trajectory_location(monkeypatch, tmp_path):
    artifact_dir = _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(error=RuntimeError("execution interrupted"))

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 4, 100, 12.5,
            artifact_root=tmp_path, runtime=runtime,
        )
    )

    assert metrics["workflow_status"] == "error"
    assert metrics["trajectory_path"] == str(artifact_dir / "trajectory.jsonl")


def test_single_agent_attempts_keep_separate_traces_and_usage(tmp_path):
    class TraceRuntime(RecordingRuntime):
        async def agent(self, prompt, **kwargs):
            result = await super().agent(prompt, **kwargs)
            (kwargs["artifacts"] / "trajectory.jsonl").write_text(prompt, encoding="utf-8")
            return result

    records = []
    for attempt, tokens in (("first attempt", 10), ("second attempt", 20)):
        records.append(asyncio.run(gp.run_agent(
            attempt, "cid", _agent_config(), 4, 100, 12.5, artifact_root=tmp_path,
            runtime=TraceRuntime(_runtime_result(tokens_spent=tokens)),
        )))

    assert records[0]["trajectory_path"] != records[1]["trajectory_path"]
    assert [Path(record["trajectory_path"]).read_text() for record in records] == ["first attempt", "second attempt"]
    assert [record["used_tokens"] for record in records] == [10, 20]


