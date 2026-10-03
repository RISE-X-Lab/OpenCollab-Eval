"""Base defaults reach execution and preserve distinct historical identity."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval import runtime_config
from opencollab_eval.cli import build_parser
from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.runtime_config import resolve_solver_agent_profile, resolve_workflow_agent_profile
from tests.support.gen_prediction_single_agent_support import RecordingRuntime, _agent_config, _runtime_result
from tests.support.swe_g11_parallel_runner_support import _args, _load_module
from tests.support.swe_v1_prolite_runner_test_support import _remote_namespace, _seed_remote_completed_generation


@pytest.mark.parametrize("profile", [None, "base", "default", "single", "single2"])
def test_base_aliases_use_one_public_agent_call(monkeypatch, tmp_path, profile):
    runtime = RecordingRuntime(_runtime_result())
    metrics = asyncio.run(gp.run_agent(
        "repair", "unused-container", _agent_config(), None, None, 10,
        artifact_root=tmp_path, runtime=runtime, profile=profile,
    ))
    request = runtime.requests[0]
    assert request.profile == "single2"
    assert request.budget == gp.SINGLE2_AUTHORIZED_BUDGET
    assert request.max_steps == gp.SINGLE2_AUTHORIZED_MAX_STEPS
    assert metrics["agent_profile"] == "single2"
    assert metrics["evaluation_model_configuration"]["profile"] == "single2"


def test_omitted_workflow_profile_keeps_role_configuration():
    assert resolve_workflow_agent_profile(None) is None
    assert resolve_solver_agent_profile("duo", None) is None
    assert resolve_solver_agent_profile("single-agent", None) == "single2"
    assert resolve_solver_agent_profile(None, None) == "single2"


def test_generator_uses_the_same_public_call_for_a_future_profile(monkeypatch, tmp_path):
    def resolve(name):
        assert name == "future-agent"
        return name

    monkeypatch.setattr(gp.gen_prediction_agent, "resolve_profile_name", resolve)
    monkeypatch.setattr(runtime_config, "resolve_profile_name", resolve)
    runtime = RecordingRuntime(_runtime_result())
    metrics = asyncio.run(gp.run_agent(
        "repair", "unused-container", _agent_config(), None, None, 10,
        artifact_root=tmp_path, runtime=runtime, profile="future-agent",
    ))
    request = runtime.requests[0]
    assert request.profile == "future-agent"
    assert request.budget is None and request.max_steps is None
    assert metrics["agent_profile"] == "future-agent"


@pytest.mark.parametrize("profile", ["base", "default", "single", "single2"])
def test_generic_cli_resolves_explicit_profile_aliases(profile):
    parsed = build_parser().parse_args(["run", "tasks.jsonl", "--agent-profile", profile])
    assert parsed.agent_profile == "single2"


def test_single_agent_history_without_profile_is_distinct_from_new_base(tmp_path):
    namespace = _remote_namespace(tmp_path, workflow="single-agent")
    _seed_remote_completed_generation(namespace, "task-1")
    prediction, metric, _ = namespace["latest_pair"](namespace["base_run_dir"] / "task-1", "task-1")
    assert metric["agent_profile"] == "single2"
    assert namespace["generation_identity_matches"](prediction, metric)
    metric.pop("agent_profile")
    assert namespace["generation_identity_matches"](prediction, metric) is False
    metric["agent_profile"] = None
    assert namespace["generation_identity_matches"](prediction, metric) is False
    metric["agent_profile"] = "single"
    assert namespace["generation_identity_matches"](prediction, metric) is False


def test_standalone_base_inherits_single2_compatibility_and_explicit_override():
    runner = _load_module()
    inherited = runner.resolve_config(_args(workflow="single-agent"))
    explicit = runner.resolve_config(_args(
        workflow="single-agent",
        workflow_env=["OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=0"],
    ))
    workflow = runner.resolve_config(_args(workflow="duo"))
    assert inherited.agent_profile == "single2"
    assert "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=1" in inherited.workflow_env
    assert "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=0" in explicit.workflow_env
    assert "OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=1" not in explicit.workflow_env
    assert workflow.agent_profile is None
    assert not any(value.startswith("OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=")
                   for value in workflow.workflow_env)


@pytest.mark.parametrize("profile", [None, "base", "single", "single2"])
def test_single_agent_cli_records_resolved_profile(monkeypatch, tmp_path, profile):
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"instance_id": "task-1", "repo": "fixture/repo", "problem_statement": "repair"}))
    output = tmp_path / "out/predictions.jsonl"
    arguments = ["gen_prediction", "--instance-file", str(instance), "--output", str(output)]
    if profile is not None:
        arguments.extend(["--agent-profile", profile])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.delenv("OPENCOLLAB_SWE_WORKFLOW", raising=False)
    monkeypatch.setattr(gp, "get_config", lambda *_, **_kwargs: _agent_config())
    monkeypatch.setattr(gp, "start_container_with_marker", lambda *a, **kw: "unused-container")
    monkeypatch.setattr(gp, "prepare_testbed_environment", lambda *_: None)
    monkeypatch.setattr(gp, "stash_solver_runtime_dependencies", lambda *_: None)
    monkeypatch.setattr(gp, "restore_solver_runtime_dependencies", lambda *_: None)
    monkeypatch.setattr(gp, "container_image_id", lambda *_: "sha256:" + "8" * 64)
    monkeypatch.setattr(gp, "prepare_solver_git_snapshot", lambda *a, **kw: SimpleNamespace(as_dict=lambda: {}))
    monkeypatch.setattr(gp, "prepare_trusted_patch_baseline", lambda *a, **kw: None)
    monkeypatch.setattr(gp, "_cleanup_generation_attempt", lambda **kwargs: [], raising=False)
    monkeypatch.setattr(gp, "finalize_container_ownership", lambda **kwargs: None)
    calls = []

    async def finish_without_candidate(*args, **kwargs):
        calls.append(kwargs["profile"])
        return {"workflow_status": "error", "candidate_probe_eligible": False}

    monkeypatch.setattr(gp, "run_agent", finish_without_candidate)
    with pytest.raises(SystemExit) as stopped:
        gp.main()
    assert stopped.value.code == 1
    assert calls == ["single2"]
    metric = json.loads(output.with_name("metrics.jsonl").read_text())
    prediction = json.loads(output.read_text())
    assert metric["agent_profile"] == "single2"
    assert prediction["workflow_metric"]["agent_profile"] == "single2"
    assert metric["workflow"] == prediction["workflow"] == "single-agent"
