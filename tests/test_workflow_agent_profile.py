"""Independent workflow/profile selection through the public OC facade."""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys

import pytest
from evaluator_test_support import EvalTask, FakeEnv, run, run_eval_task
from openai import DefaultAsyncHttpxClient
from opencollab import OpenCollab, RunResult
from opencollab.environments import local_environment
from opencollab.tools import profile_tool_limits
from swe_v1_prolite_runner_test_support import _remote_namespace, _seed_remote_completed_generation
from test_swe_g11_parallel_runner import _args, _load_module

from opencollab_eval.commands.swe_g11_task_execution import task_command
from opencollab_eval.engine import evaluator_sessions
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.runtime_config import SINGLE2_AUTHORIZED_BUDGET, SINGLE2_AUTHORIZED_MAX_STEPS
from opencollab_eval.verification import BashEvidence, evaluation_tools
from opencollab_eval.workflows import validation_council_dual_coder_contract as g21

WORKFLOW = "validation-council-dual-coder-contract-v1"


def test_workflow_generator_cli_exposes_independent_profile_selection():
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.generation.gen_prediction_workflow", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--workflow WORKFLOW" in result.stdout
    assert "--agent-profile {single,single2}" in result.stdout
    assert gpw._BUNDLED_WORKFLOWS[WORKFLOW] is g21.validation_council_dual_coder_contract_v1


@pytest.mark.parametrize("profile", [None, "single", "single2"])
def test_evaluator_selects_profile_without_replacing_workflow(monkeypatch, tmp_path, profile):
    calls = []

    class Client:
        async def workflow(self, workflow, inputs, **kwargs):
            calls.append((workflow, inputs, kwargs))
            return RunResult(
                output={"status": "done"},
                status="completed",
                metrics={"execution_quiesced": True, "sessions": 3},
            )

    monkeypatch.setattr(evaluator_sessions, "_client", lambda **kwargs: Client())
    env = FakeEnv()

    async def factory(task):
        return env

    result = run(run_eval_task(
        EvalTask(task_id="profile-combination", description="repair public behavior"),
        workflow=g21.validation_council_dual_coder_contract_v1,
        agent_profile=profile,
        env_factory=factory,
        tools_factory=list,
        output_dir=str(tmp_path),
        prompt="existing workflow prompt",
    ))
    workflow, inputs, options = calls[0]
    assert workflow is g21.validation_council_dual_coder_contract_v1
    assert inputs["description"] == "repair public behavior"
    manifest = json.loads((tmp_path / "trajectories/profile-combination/workflow.json").read_text())
    if profile == "single2":
        assert options["agent_profile"] == "single2"
        assert "system_prompt" not in options
        assert manifest["agent_profile"] == "single2"
    else:
        assert "agent_profile" not in options
        assert options["system_prompt"].startswith("existing workflow prompt")
        assert "agent_profile" not in manifest
    assert result.workflow_result == {"status": "done"}


def test_parallel_and_remote_configuration_carry_the_profile(tmp_path):
    runner = _load_module()
    config = runner.resolve_config(_args(workflow=WORKFLOW, agent_profile="single2"))
    command = task_command(config, config.indices[0])
    assert config.workflow == WORKFLOW
    assert config.agent_profile == "single2"
    assert command[command.index("--workflow") + 1] == WORKFLOW
    assert command[command.index("--agent-profile") + 1] == "single2"
    default = runner.resolve_config(_args(workflow=WORKFLOW, agent_profile="single"))
    assert default.agent_profile is None
    assert "--agent-profile" not in task_command(default, default.indices[0])
    namespace = _remote_namespace(tmp_path, workflow=WORKFLOW, agent_profile="single2")
    assert namespace["workflow"] == WORKFLOW
    assert namespace["agent_profile"] == "single2"
    assert namespace["generation_runtime_identity"]()["agent_profile"] == "single2"


def test_evaluator_single_session_uses_native_single2_arguments(monkeypatch, tmp_path):
    calls = []

    class Client:
        async def agent2(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return RunResult(output="finished", status="completed", metrics={"execution_quiesced": True})

    monkeypatch.setattr(evaluator_sessions, "_client", lambda **kwargs: Client())
    env = FakeEnv()

    async def factory(task):
        return env

    result = run(run_eval_task(
        EvalTask(task_id="native-single2", description="repair public behavior"),
        agent_profile="single2", env_factory=factory, tools_factory=list, output_dir=str(tmp_path),
    ))
    prompt, kwargs = calls[0]
    assert prompt == "repair public behavior"
    assert {"system_prompt", "tools", "name"}.isdisjoint(kwargs)
    assert kwargs["budget"] == 1_000_000
    assert result.error is None


def test_existing_candidate_reuse_distinguishes_the_selected_agent_profile(tmp_path):
    namespace = _remote_namespace(tmp_path, workflow=WORKFLOW, agent_profile="single2")
    _seed_remote_completed_generation(namespace, "task-1")
    prediction, metric, _ = namespace["latest_pair"](namespace["base_run_dir"] / "task-1", "task-1")
    assert namespace["generation_identity_matches"](prediction, metric)
    metric["agent_profile"] = None
    assert namespace["generation_identity_matches"](prediction, metric) is False
    namespace["agent_profile"] = None
    assert namespace["generation_identity_matches"](prediction, metric)
    metric["agent_profile"] = "single2"
    assert namespace["generation_identity_matches"](prediction, metric) is False


def test_standalone_alias_and_independent_profile_keep_native_effective_limits(tmp_path):
    for workflow, profile in (("single2", None), ("single-agent", "single2")):
        namespace = _remote_namespace(
            tmp_path / workflow, workflow=workflow, agent_profile=profile,
            workflow_env={"OPENCOLLAB_UNBOUNDED_LIMITS": "true"},
        )
        identity = namespace["generation_runtime_identity"]()
        assert identity["agent_profile"] == "single2"
        assert identity["budget"] == SINGLE2_AUTHORIZED_BUDGET
        assert identity["max_steps"] == SINGLE2_AUTHORIZED_MAX_STEPS


def _completion(message, *, tool_name=None, arguments=None):
    calls = [] if tool_name is None else [{
        "index": 0,
        "id": "deterministic-call",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
    }]
    delta = {"role": "assistant", "content": message}
    if calls:
        delta["tool_calls"] = calls
    chunk = {
        "id": "deterministic-response",
        "object": "chat.completion.chunk",
        "model": "unit-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return chunk


@pytest.mark.asyncio
async def test_original_g21_profiles_coders_and_adjudicator_with_real_candidate_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.delenv("OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    (source / "widget.py").write_text('def parse(text):\n    return text.split(",")\n')
    (source / "test_widget.py").write_text(
        "from widget import parse\n\ndef test_empty():\n    assert parse('') == []\n"
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    command = f"{shlex.quote(sys.executable)} -m pytest -rA test_widget.py::test_empty"
    requests = []
    roles = {"A": [], "B": [], "judge": []}
    judge = {
        "winner": "B",
        "requirements_complete": True,
        "requirements": [{
            "requirement": "Accept None as empty input",
            "a_coverage": "not_covered",
            "b_coverage": "covered",
            "a_evidence": ["widget.py calls text.split on None"],
            "b_evidence": ["widget.py handles None through text or empty string"],
        }],
        "rationale": "B covers the complete public input contract",
    }

    async def send(client, request, **kwargs):
        transport = importlib.import_module(type(request).__module__.split(".", 1)[0])
        payload = json.loads(request.content)
        requests.append(payload)
        user = next(item["content"] for item in payload["messages"] if item["role"] == "user")
        role = "A" if "You are autonomous coder A." in user else "B" if "You are autonomous coder B." in user else (
            "judge" if "You are the read-only contract adjudicator" in user else None
        )
        if role is None:
            content = _completion("finished")
        else:
            roles[role].append(payload)
            step = len(roles[role])
            if role == "judge":
                content = _completion(None, tool_name="structured_output", arguments=judge)
            elif step == 1:
                fixed = (
                    'def parse(text):\n    return [] if text == "" else text.split(",")\n'
                    if role == "A" else 'def parse(text):\n    return [] if not text else text.split(",")\n'
                )
                content = _completion(None, tool_name="file_write", arguments={
                    "path": "widget.py", "mode": "create", "content": fixed,
                })
            elif step == 2:
                content = _completion(None, tool_name="bash", arguments={"command": command})
            else:
                content = _completion("finished")
        if payload.get("stream"):
            encoded = f"data: {json.dumps(content)}\n\ndata: [DONE]\n\n".encode()
            return transport.Response(
                200, headers={"content-type": "text/event-stream"}, content=encoded, request=request
            )
        content["object"] = "chat.completion"
        choice = content["choices"][0]
        choice["message"] = choice.pop("delta")
        for tool in choice["message"].get("tool_calls", []):
            tool.pop("index")
        return transport.Response(200, json=content, request=request)

    monkeypatch.setattr(DefaultAsyncHttpxClient, "send", send)
    client = OpenCollab(source, model="unit-model", provider="openai", api_key="test-key",
                        base_url="https://model.example.invalid/v1")
    baseline = await client.agent2("Record the native profile prompt", tools=[], trace=False)
    assert baseline.ok
    system_prompt = requests[0]["messages"][0]["content"]
    coder_tools = []
    observed_instances = []
    original_execute = BashEvidence.execute_with_runtime

    def build_coder_tools():
        tools = list(evaluation_tools("bash", "file_read", "file_write", "apply_patch", "grep", "git_diff",
                                      headless=False))
        coder_tools.append(tools)
        return tools

    async def execute(tool, params, runtime):
        observed_instances.append(tool)
        return await original_execute(tool, params, runtime)

    monkeypatch.setattr(g21, "_coder_tools", build_coder_tools)
    monkeypatch.setattr(BashEvidence, "execute_with_runtime", execute)
    env = local_environment(source)

    async def factory(task):
        return env

    result = await run_eval_task(
        EvalTask(task_id="g21-single2", description="parse must accept empty text and None as empty input",
                 timeout=60, max_tokens=10_000),
        model="unit-model", provider="openai", api_key="test-key", base_url="https://model.example.invalid/v1",
        output_dir=str(tmp_path / "output"), env_factory=factory, tools_factory=list,
        workflow=g21.validation_council_dual_coder_contract_v1, agent_profile="single2", max_steps=6,
        prompt="generic workflow override sentinel",
    )
    assert result.error is None
    assert result.workflow_result["status"] == "done"
    assert result.workflow_result["winner"] == result.workflow_result["adopted"] == "B"
    assert result.workflow_result["selection_reason"] == "contract-adjudicated"
    assert result.workflow_result["shared_public_command"] == command
    assert result.workflow_result["adoption_attempts"] == ["B"]
    assert (source / "widget.py").read_text() == 'def parse(text):\n    return [] if not text else text.split(",")\n'
    assert result.patch_produced
    assert "if not text" in result.patch
    for role in ("A", "B", "judge"):
        assert roles[role]
        for payload in roles[role]:
            prompt = payload["messages"][0]["content"]
            assert prompt.startswith(system_prompt)
            assert "generic workflow override sentinel" not in prompt
    assert command in roles["B"][0]["messages"][1]["content"]
    assert [tool["function"]["name"] for tool in roles["judge"][0]["tools"]] == ["structured_output"]
    assert len(coder_tools) == len(observed_instances) == 2
    for tools, observed in zip(coder_tools, observed_instances, strict=True):
        assert observed is tools[0]
        assert isinstance(observed, BashEvidence)
        assert observed.max_output_chars == profile_tool_limits("single2")["bash"]["max_output_chars"]
        assert observed.verification_records[-1]["command"] == command
        assert observed.verified_targets == frozenset({"test_widget.py::test_empty"})
    assert result.runtime_state["metrics"]["agent_profile"] == "single2"
