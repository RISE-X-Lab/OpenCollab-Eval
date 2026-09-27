"""Exercise explicit context metadata and the real FIFO launcher."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.runtime_config import resolve_runtime_config


def test_single_agent_metrics_keep_explicit_context(monkeypatch, tmp_path):
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"instance_id": "task-1", "repo": "fixture/repo", "problem_statement": "done"}))
    output = tmp_path / "out/predictions.jsonl"
    monkeypatch.setattr(sys, "argv", ["gen_prediction", "--instance-file", str(instance), "--output", str(output)])
    config = resolve_runtime_config(tmp_path, overrides={
        "model": "unknown-model", "provider": "openai", "context_window": 1_048_576,
    })
    monkeypatch.setattr(gp, "get_config", lambda *_: config)
    monkeypatch.setattr(gp, "start_container_with_marker", lambda *a, **kw: "unused-container")
    monkeypatch.setattr(gp, "prepare_testbed_environment", lambda *_: None)
    monkeypatch.setattr(gp, "stash_solver_runtime_dependencies", lambda *_: None)
    monkeypatch.setattr(gp, "restore_solver_runtime_dependencies", lambda *_: None)
    monkeypatch.setattr(gp, "container_image_id", lambda *_: "sha256:" + "8" * 64)
    monkeypatch.setattr(gp, "prepare_solver_git_snapshot", lambda *a, **kw: SimpleNamespace(as_dict=lambda: {}))
    monkeypatch.setattr(gp, "prepare_trusted_patch_baseline", lambda *a, **kw: None)
    monkeypatch.setattr(gp, "_cleanup_generation_attempt", lambda **kwargs: [], raising=False)
    monkeypatch.setattr(gp, "finalize_container_ownership", lambda **kwargs: None)

    async def finish_without_candidate(*args, **kwargs):
        return {"workflow_status": "error", "candidate_probe_eligible": False}

    monkeypatch.setattr(gp, "run_agent", finish_without_candidate)
    with pytest.raises(SystemExit) as stopped:
        gp.main()
    assert stopped.value.code == 1
    metric = json.loads(output.with_name("metrics.jsonl").read_text())
    assert metric["context_window"] == 1_048_576
