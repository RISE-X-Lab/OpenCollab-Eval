"""Exercise explicit context metadata and the real FIFO launcher."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import opencollab_eval
from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.runtime_config import resolve_runtime_config


@pytest.mark.parametrize("generator", ["single-agent", "workflow"])
def test_fifo_launcher_passes_explicit_context_to_generator(tmp_path, generator):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    python = binaries / "python3"
    python.write_text(
        f'#!/bin/sh\nif [ "$1" = "-" ]; then exec {shlex.quote(sys.executable)} "$@"; fi\n'
        'while [ "$#" -gt 0 ]; do\n  if [ "$1" = "--context-window" ]; then\n'
        '    printf "context=%s\\n" "$2"\n  fi\n  shift\ndone\n'
    )
    python.chmod(0o755)
    instance = tmp_path / "instance.json"
    instance.write_text('{}\n')
    token = tmp_path / "test-input"
    token.write_text('offline-fixture\n')
    script = Path(opencollab_eval.__file__).parent / "resources/run_swe_v2_one_from_fifo.sh"
    environment = dict(os.environ)
    environment.pop("OPENCOLLAB_CONTEXT_WINDOW", None)
    environment.update(
        PATH=f"{binaries}:{environment['PATH']}",
        OPENCOLLAB_REMOTE_ROOT=str(tmp_path), OPENCOLLAB_REMOTE_REPO=str(tmp_path),
        OPENCOLLAB_REMOTE_PROXY_BASE_URL="http://127.0.0.1:1",
        OPENCOLLAB_MODEL="unknown-model", OPENCOLLAB_SWE_GENERATOR=generator,
        OPENCOLLAB_INSTANCE_FILE=str(instance),
    )
    completed = subprocess.run(
        ["bash", str(script), "task-1", "unused-image", str(token), str(tmp_path / "run"),
         "unknown-model", "", "", "65536", "1048576"],
        env=environment, capture_output=True, text=True, check=True,
    )
    assert "context=1048576" in completed.stdout


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
