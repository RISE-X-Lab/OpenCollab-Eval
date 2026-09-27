"""CLI defaults use the same limits before public profile resolution."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.runtime_config import (
    SINGLE2_AUTHORIZED_BUDGET,
    SINGLE2_AUTHORIZED_MAX_STEPS,
    resolve_runtime_config,
)


@pytest.mark.parametrize("generator", ["single-agent", "workflow"])
@pytest.mark.parametrize("explicit", [False, True])
def test_generation_cli_limits_reach_execution(monkeypatch, tmp_path, generator, explicit):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({
        "instance_id": "task-1", "repo": "fixture/repo", "problem_statement": "fix the fixture",
    }))
    output = tmp_path / "out/predictions.jsonl"
    argv = ["generator", "--instance-file", str(instance), "--output", str(output)]
    if explicit:
        argv.extend(["--max-steps", "73", "--budget", "2345678", "--timeout", "3456"])
    monkeypatch.setattr(sys, "argv", argv)
    config = resolve_runtime_config(tmp_path, overrides={"model": "unit-model", "provider": "openai"})
    captured = {}
    validate = gp.validate_generation_limits

    def capture_validation(**kwargs):
        captured["cli"] = (kwargs["max_steps"], kwargs["budget"], kwargs["timeout"])
        return validate(**kwargs)

    if generator == "single-agent":
        monkeypatch.setattr(gp, "validate_generation_limits", capture_validation)
        monkeypatch.setattr(gp, "get_config", lambda *_: config)
        monkeypatch.setattr(gp, "start_container_with_marker", lambda *a, **kw: "fixture-container")
        monkeypatch.setattr(gp, "prepare_testbed_environment", lambda *_: None)
        monkeypatch.setattr(gp, "stash_solver_runtime_dependencies", lambda *_: None)
        monkeypatch.setattr(gp, "restore_solver_runtime_dependencies", lambda *_: None)
        monkeypatch.setattr(gp, "container_image_id", lambda *_: "sha256:" + "8" * 64)
        monkeypatch.setattr(gp, "prepare_solver_git_snapshot", lambda *a, **kw: SimpleNamespace(as_dict=lambda: {}))
        monkeypatch.setattr(gp, "prepare_trusted_patch_baseline", lambda *a, **kw: None)
        monkeypatch.setattr(gp, "_cleanup_generation_attempt", lambda **kwargs: [])

        async def execute(task, cid, cfg, max_steps, budget, timeout, **kwargs):
            captured["execution"] = (max_steps, budget, timeout)
            captured["profile"] = kwargs["profile"]
            return {"workflow_status": "error", "candidate_probe_eligible": False}

        monkeypatch.setattr(gp, "run_agent", execute)
        entry = gp.main
    else:
        monkeypatch.setattr(gp, "validate_generation_limits", capture_validation)
        monkeypatch.setattr(gpw, "get_config", lambda *_: config)

        async def execute(instance, image, cfg, args, workflow, workflow_name):
            captured["execution"] = (args.max_steps, args.budget, args.timeout)
            return "", {"workflow_status": "error"}

        monkeypatch.setattr(gpw, "generate", execute)
        entry = gpw.main

    with pytest.raises(SystemExit) as stopped:
        entry()
    assert stopped.value.code == 1
    if explicit:
        assert captured["cli"] == (73, 2_345_678, 3456.0)
        assert captured["execution"] == (73, 2_345_678, 3456.0)
    else:
        assert captured["cli"] == (None, None, 1800.0)
        if generator == "single-agent":
            assert captured["execution"] == (SINGLE2_AUTHORIZED_MAX_STEPS, SINGLE2_AUTHORIZED_BUDGET, 1800.0)
            assert captured["profile"] == "single2"
        else:
            assert captured["execution"] == (None, None, 1800.0)
