"""Shared fakes for the single-agent generation tests.

Lifted out of ``test_gen_prediction_single_agent`` when a second file needed
them; that file is at the repository's per-file line ceiling.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from opencollab import RunResult as AgentRunResult

from opencollab_eval.generation import gen_prediction as gp


class RecordingRuntime:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.requests = []

    async def agent(self, prompt, **kwargs):
        request = SimpleNamespace(prompt=prompt, **kwargs)
        self.requests.append(request)
        assert request.artifacts is not None
        assert list(request.artifacts.iterdir()) == []
        if self.error is not None:
            raise self.error
        return self.result


def _runtime_result(
    *,
    outcome="completed",
    phase="done",
    error_type=None,
    error_message=None,
    tokens_spent=10,
    step_count=1,
    cleanup_quiesced=True,
):
    status = {
        "completed": "completed",
        "timed_out": "stopped",
        "failed": "failed",
    }[outcome]
    if phase in {"budget_exceeded", "step_limit_exceeded", "context_overflow"}:
        status = "stopped"
    error = None
    if error_message:
        error = (
            TimeoutError(error_message)
            if error_type == "TimeoutError"
            else RuntimeError(error_message)
        )
    return AgentRunResult(
        output="result" if outcome == "completed" else None,
        status=status,
        reason=("timeout" if outcome == "timed_out" else phase if status == "stopped" else None),
        tokens=tokens_spent,
        error=error,
        metrics={
            "phase": phase,
            "steps": step_count,
            "session_quiesced": cleanup_quiesced,
            "execution_quiesced": None,
        },
    )


def _reserve_empty_artifact_dir(monkeypatch, tmp_path):
    artifact_dir = tmp_path / "agent-artifacts"

    def reserve(_root):
        assert Path(_root) == tmp_path
        artifact_dir.mkdir()
        return str(artifact_dir)

    monkeypatch.setattr(gp.gen_prediction_agent, "reserve_run_directory", reserve)
    return artifact_dir


def _agent_config():
    return {
        "model": "model",
        "provider": "provider",
        "api_key": "key",
        "base_url": "http://local",
    }
