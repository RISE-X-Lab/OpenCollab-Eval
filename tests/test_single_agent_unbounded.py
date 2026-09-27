"""Single-agent limits stay unbounded through the public execution API."""

import asyncio

import pytest
from opencollab import OpenCollab
from single_agent_test_support import TwoStepLLM
from test_gen_prediction_single_agent import _agent_config

from opencollab_eval.generation import gen_prediction_agent
from opencollab_eval.generation.gen_prediction_config import validate_generation_limits


@pytest.mark.parametrize("flag", ["true", "1"])
def test_single_agent_unbounded_env_normalizes_cli_limits(monkeypatch, flag):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", flag)
    assert validate_generation_limits(max_steps=84, budget=2_400_000, timeout=900) == (None, None, 900.0)


@pytest.mark.parametrize("flag", ["false", "0", ""])
def test_single_agent_bounded_configuration_preserves_limits(monkeypatch, flag):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", flag)
    assert validate_generation_limits(max_steps=84, budget=2_400_000, timeout=900) == (84, 2_400_000, 900.0)


def test_single_agent_unbounded_keeps_wall_timeout_validation(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    with pytest.raises(ValueError, match="--timeout"):
        validate_generation_limits(max_steps=84, budget=2_400_000, timeout=0)


class SingleClient(OpenCollab):
    def __init__(self, *args, llm, **kwargs):
        super().__init__(*args, **kwargs)
        self._test_llm = llm

    async def agent(self, prompt, **kwargs):
        return await super().agent(prompt, llm=self._test_llm, **kwargs)


def test_public_single_agent_completes_after_bounded_step_and_token_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    max_steps, budget, timeout = validate_generation_limits(max_steps=1, budget=1, timeout=30)
    (tmp_path / "marker.txt").write_text("evidence\n", encoding="utf-8")
    llm = TwoStepLLM()
    client = SingleClient(tmp_path, model="unit-model", provider="openai", llm=llm,
                        config={"wire_protocol": "responses", "context_window": 4_000_000})
    metrics = asyncio.run(gen_prediction_agent.run_agent(
        "inspect", "unused-container", _agent_config(), max_steps, budget, timeout,
        artifact_root=tmp_path, runtime=client,
    ))
    assert llm.calls == 2
    assert metrics["workflow_status"] == "done"
    assert metrics["step_count"] == 2
    assert metrics["used_tokens"] == 1_100_032
