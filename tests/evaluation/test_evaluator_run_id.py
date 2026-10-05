"""The run id the batch driver chose reaches the OpenCollab run it starts.

The driver writes the id into its manifest row and hands it to the generator
process as ``OPENCOLLAB_RUN_ID``; the evaluator passes it to whichever SDK call
the arm makes, so the trajectory, the run's own manifest and the metrics row
carry the same id the batch manifest does. Without the variable nothing is
passed and OpenCollab generates its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opencollab import RunResult

from opencollab_eval.engine import evaluator_sessions
from tests.support.evaluator_test_support import EvalTask, FakeEnv, run, run_eval_task

CALLS: list[dict] = []


class Client:
    async def agent(self, _prompt, **kwargs):
        CALLS.append(kwargs)
        (Path(kwargs["artifacts"]) / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
        return RunResult(output="done", status="completed", tokens=1, metrics={"steps": 1})

    async def workflow(self, _workflow, _args, **kwargs):
        CALLS.append(kwargs)
        return RunResult(output={}, status="completed", tokens=1, metrics={"steps": 1})


async def sample_workflow(_ctx, _args):
    return None


@pytest.fixture(autouse=True)
def client(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(evaluator_sessions, "_client", lambda **_kwargs: Client())


def _run(tmp_path, **kwargs):
    async def env_factory(_task):
        return FakeEnv()

    run(
        run_eval_task(
            EvalTask(task_id="t-1", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            **kwargs,
        )
    )
    return CALLS[-1] if CALLS else None


@pytest.mark.parametrize("mode", ["agent", "workflow"])
def test_the_driver_run_id_is_passed_to_the_sdk(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("OPENCOLLAB_RUN_ID", "single-abc")
    call = _run(tmp_path, **({"workflow": sample_workflow} if mode == "workflow" else {}))
    assert call["run_id"] == "single-abc"


@pytest.mark.parametrize("mode", ["agent", "workflow"])
def test_without_a_driver_run_id_the_sdk_chooses(monkeypatch, tmp_path, mode):
    monkeypatch.delenv("OPENCOLLAB_RUN_ID", raising=False)
    call = _run(tmp_path, **({"workflow": sample_workflow} if mode == "workflow" else {}))
    assert "run_id" not in call


async def test_the_driver_run_id_is_passed_to_a_team(monkeypatch, tmp_path):
    calls: list[dict] = []

    class TeamClient:
        async def team(self, _prompt, **kwargs):
            calls.append(kwargs)
            return RunResult(output="done", status="completed", tokens=1, metrics={"steps": 1})

    class Tracer:
        def bind_artifacts(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(evaluator_sessions, "_client", lambda **_kwargs: TeamClient())
    monkeypatch.setenv("OPENCOLLAB_RUN_ID", "team-arm-abc")
    await evaluator_sessions._run_team_mode(
        task=EvalTask(task_id="t-1", description="Fix it.", timeout=60.0, max_tokens=1000),
        env=object(),
        tracer=Tracer(),
        team_config=tmp_path / "team.yaml",
        model="gpt-4o",
        provider="openai",
        api_key="test-key",  # pragma: allowlist secret
        base_url=None,
        max_steps=10,
        save_dir=str(tmp_path / "runs"),
    )
    assert calls[0]["run_id"] == "team-arm-abc"


def test_a_runtime_without_run_id_is_passed_nothing(monkeypatch, tmp_path):
    """OpenCollab 0.9.0 has no ``run_id=``; the run still goes ahead."""
    calls: list[dict] = []

    class OldClient:
        async def agent(self, prompt, *, profile=None, tools=None, system_prompt=None, budget=None,
                        max_steps=None, timeout=None, artifacts=None, trace=True):
            calls.append({"artifacts": artifacts})
            (Path(artifacts) / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
            return RunResult(output="done", status="completed", tokens=1, metrics={"steps": 1})

    monkeypatch.setattr(evaluator_sessions, "_client", lambda **_kwargs: OldClient())
    monkeypatch.setenv("OPENCOLLAB_RUN_ID", "single-abc")
    _run(tmp_path)
    assert calls
