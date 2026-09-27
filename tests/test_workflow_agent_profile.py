"""Independent workflow/profile selection through the public OC facade."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from evaluator_test_support import EvalTask, FakeEnv, run, run_eval_task
from opencollab import RunResult
from opencollab.builtin_workflows import duo
from swe_v1_prolite_runner_test_support import _remote_namespace, _seed_remote_completed_generation
from test_swe_g11_parallel_runner import _args, _load_module

from opencollab_eval.commands.swe_g11_task_execution import task_command
from opencollab_eval.engine import evaluator_sessions
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.runtime_config import SINGLE2_AUTHORIZED_BUDGET, SINGLE2_AUTHORIZED_MAX_STEPS

WORKFLOW = "duo"


def test_workflow_generator_cli_exposes_independent_profile_selection():
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.generation.gen_prediction_workflow", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--workflow WORKFLOW" in result.stdout
    assert "--agent-profile PROFILE" in result.stdout
    assert gpw._BUNDLED_WORKFLOWS[WORKFLOW] is duo


@pytest.mark.parametrize("profile", [None, "base", "default", "single", "single2"])
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
        workflow=duo,
        agent_profile=profile,
        env_factory=factory,
        tools_factory=list,
        output_dir=str(tmp_path),
        prompt="existing workflow prompt",
    ))
    workflow, inputs, options = calls[0]
    assert workflow is duo
    assert inputs["description"] == "repair public behavior"
    manifest = json.loads((tmp_path / "trajectories/profile-combination/workflow.json").read_text())
    if profile is not None:
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
    assert default.agent_profile == "single2"
    assert task_command(default, default.indices[0]).count("single2") == 1
    namespace = _remote_namespace(tmp_path, workflow=WORKFLOW, agent_profile="single2")
    assert namespace["workflow"] == WORKFLOW
    assert namespace["agent_profile"] == "single2"
    assert namespace["generation_runtime_identity"]()["agent_profile"] == "single2"


def test_evaluator_single_session_uses_native_single2_arguments(monkeypatch, tmp_path):
    calls = []

    class Client:
        async def agent(self, prompt, **kwargs):
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
    assert kwargs["profile"] == "single2"
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
    for workflow, profile in (("single2", None), ("single-agent", None), ("single-agent", "single2")):
        namespace = _remote_namespace(
            tmp_path / workflow, workflow=workflow, agent_profile=profile,
            workflow_env={"OPENCOLLAB_UNBOUNDED_LIMITS": "true"},
        )
        identity = namespace["generation_runtime_identity"]()
        assert identity["agent_profile"] == "single2"
        assert identity["budget"] == SINGLE2_AUTHORIZED_BUDGET
        assert identity["max_steps"] == SINGLE2_AUTHORIZED_MAX_STEPS
