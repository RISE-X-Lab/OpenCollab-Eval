from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from types import SimpleNamespace

from opencollab import OpenCollab
from test_gen_prediction_single_agent import (
    RecordingRuntime,
    _agent_config,
    _reserve_empty_artifact_dir,
    _runtime_result,
)
from test_swe_g11_parallel_runner import _args, _load_module
from test_swe_v1_prolite_runner import _complete_remote_config

from opencollab_eval.engine import swe_v1_remote_execution
from opencollab_eval.engine import swe_v1_remote_generation as remote_generation
from opencollab_eval.engine import swe_v1_remote_state as remote_state
from opencollab_eval.generation import gen_prediction as gp


class RecordingSingle2Runtime(RecordingRuntime):
    async def agent2(self, prompt, **kwargs):
        request = SimpleNamespace(prompt=prompt, **kwargs)
        self.requests.append(request)
        assert request.artifacts is not None
        assert list(request.artifacts.iterdir()) == []
        if self.error is not None:
            raise self.error
        return self.result


class Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.markup_recovered = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TwoStepLLM:
    def __init__(self) -> None:
        self.calls = 0

    def context_window(self) -> int:
        return 4_000_000

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content=None,
                tool_calls=[
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": '{"path":"marker.txt"}',
                        },
                    }
                ],
                usage=Usage(1_100_000, 10),
                finish_reason="tool_calls",
                reasoning=None,
                provider_items=[],
                provider_state=None,
            )
        return SimpleNamespace(
            content="finished",
            tool_calls=[],
            usage=Usage(20, 2),
            finish_reason="stop",
            reasoning=None,
            provider_items=[],
            provider_state=None,
        )


class Single2Client(OpenCollab):
    def __init__(self, *args, llm, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._test_llm = llm

    async def agent2(self, prompt: str, **kwargs):
        return await super().agent2(prompt, llm=self._test_llm, **kwargs)


def test_single2_profile_is_available_on_the_single_agent_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.generation.gen_prediction", "--help"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "--agent-profile {single,single2}" in result.stdout


def test_single2_uses_profile_owned_prompt_tools_and_authorized_limits(
    monkeypatch,
    tmp_path,
) -> None:
    artifact_dir = _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingSingle2Runtime(_runtime_result())

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            None,
            None,
            12.5,
            artifact_root=tmp_path,
            runtime=runtime,
            profile="single2",
        )
    )

    request = runtime.requests[0]
    assert request.prompt == "task"
    assert request.artifacts == artifact_dir
    assert request.max_steps == gp.SINGLE2_AUTHORIZED_MAX_STEPS
    assert request.budget == gp.SINGLE2_AUTHORIZED_BUDGET
    assert request.timeout == 12.5
    assert request.cleanup_timeout == gp.AGENT_CANCELLATION_GRACE_SECONDS
    for profile_owned in ("name", "tools", "system_prompt", "llm"):
        assert not hasattr(request, profile_owned)
    assert metrics["evaluation_model_configuration"]["public_interface"] == (
        "OpenCollab.agent2"
    )


def test_unbounded_parsing_is_restored_only_for_single2(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    max_steps, budget, timeout = gp.validate_generation_limits(
        max_steps=17,
        budget=23,
        timeout=5,
    )

    assert (max_steps, budget, timeout) == (None, None, 5.0)
    assert gp.resolve_agent_generation_limits("single", max_steps, budget) == (
        None,
        None,
    )
    assert gp.resolve_agent_generation_limits("single2", max_steps, budget) == (
        gp.SINGLE2_AUTHORIZED_MAX_STEPS,
        gp.SINGLE2_AUTHORIZED_BUDGET,
    )


def test_public_single2_continues_after_more_than_the_legacy_budget(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    max_steps, budget, _timeout = gp.validate_generation_limits(
        max_steps=1_000_000_000_000,
        budget=1_000_000_000_000,
        timeout=30,
    )
    (tmp_path / "marker.txt").write_text("evidence\n", encoding="utf-8")
    llm = TwoStepLLM()
    client = Single2Client(
        tmp_path,
        model="unit-model",
        provider="openai",
        config={"wire_protocol": "responses", "context_window": 4_000_000},
        llm=llm,
    )
    monkeypatch.setattr(gp.gen_prediction_agent, "attach_container", lambda **kwargs: None)

    metrics = asyncio.run(
        gp.run_agent(
            "inspect",
            "container-id",
            _agent_config(),
            max_steps,
            budget,
            30,
            artifact_root=tmp_path,
            runtime=client,
            profile="single2",
        )
    )

    assert llm.calls == 2
    assert metrics["workflow_status"] == "done"
    assert metrics["used_tokens"] == 1_100_032
    assert metrics["agent_profile"] == "single2"
    configuration = metrics["evaluation_model_configuration"]
    assert configuration["effective_budget"] == 1_000_000_000_000
    assert configuration["effective_max_steps"] == 1_000_000_000_000


def test_single2_workflow_selects_the_native_agent_profile() -> None:
    shell = gp.Path(gp.__file__).parent.parent / "resources/run_swe_v2_one_from_fifo.sh"
    source = inspect.getsource(remote_generation.generation_for_task_once)
    shell_text = shell.read_text(encoding="utf-8")

    assert 'workflow in {"single-agent", "single2"}' in source
    assert 'agent_profile_args+=(--agent-profile single2)' in shell_text
    assert '${agent_profile_args[@]+"${agent_profile_args[@]}"}' in shell_text


def test_single2_enables_only_its_terminal_stream_compatibility() -> None:
    module = _load_module()

    single2 = module.resolve_config(_args(workflow="single2"))
    default = module.resolve_config(_args(workflow="single-agent"))

    assert (
        "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=1"
        in single2.workflow_env
    )
    assert all(
        not item.startswith("OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=")
        for item in default.workflow_env
    )


def test_remote_runner_accepts_single2_terminal_stream_compatibility() -> None:
    config = _complete_remote_config(
        {
            "token": "x",
            "remote_root": "/tmp/remote",
            "remote_repo": "/tmp/repo",
            "base_run_dir": "/tmp/run",
            "workflow": "single2",
            "workflow_env": {
                "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH": "1"
            },
            "model_name": "model",
            "session_prefix": "session",
            "remote_proxy_base_url": "http://127.0.0.1:1",
            "start_index": 1,
            "limit": 1,
            "budget": 1,
            "max_steps": 1,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 0,
            "max_task_starts": 1,
            "max_eval_attempts": 1,
            "dry_run": True,
        }
    )

    remote_state.configure(config)

    assert remote_state.workflow_env == {
        "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH": "1"
    }


def test_remote_summary_records_eval_container_bind_timeout_at_source() -> None:
    source = inspect.getsource(swe_v1_remote_execution.main)
    assert '"eval_container_bind_timeout": eval_container_bind_timeout' in source
