"""CLI provider overrides resolve endpoint diagnostics from the final configuration."""

import importlib
import json
import os
import sys

import pytest
from opencollab import OpenCollab

from opencollab_eval.generation.gen_prediction_config import llm_transport_metrics
from opencollab_eval.runtime_config import resolve_runtime_config

MODULES = (
    "opencollab_eval.generation.gen_prediction",
    "opencollab_eval.generation.gen_prediction_workflow",
    "opencollab_eval.generation.gen_prediction_best_of_n",
)


class ReachedExecutionBoundary(BaseException):
    pass


@pytest.mark.parametrize("module_name", MODULES)
def test_cli_provider_override_records_actual_endpoint(module_name, tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("OPENCOLLAB_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENCOLLAB_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:18001/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:18002")
    monkeypatch.chdir(tmp_path)
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"instance_id": "example__calculator-1", "repo": "example/calculator",
                                    "problem_statement": "Fix add for an empty input"}))
    module = importlib.import_module(module_name)
    captured = []

    def configuration(*args, **kwargs):
        cfg = resolve_runtime_config(*args, **kwargs)
        captured.append(cfg)
        return cfg

    def stop(*args, **kwargs):
        raise ReachedExecutionBoundary

    async def stop_async(*args, **kwargs):
        raise ReachedExecutionBoundary

    monkeypatch.setattr(module, "get_config", configuration)
    if module_name.endswith(".gen_prediction"):
        monkeypatch.setattr(module, "_CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(module, "start_container_with_marker", stop)
    elif module_name.endswith("_workflow"):
        monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(module, "generate", stop_async)
    else:
        monkeypatch.setattr(module, "solve_candidate", stop)
    monkeypatch.setattr(sys, "argv", [module_name, "--instance-file", str(instance), "--output",
                                     str(tmp_path / "predictions.jsonl"), "--provider", "anthropic",
                                     "--model", "example-model"])
    with pytest.raises(ReachedExecutionBoundary):
        module.main()
    assert len(captured) == 1
    cfg = captured[0]
    expected = resolve_runtime_config(tmp_path, overrides={"provider": "anthropic", "model": "example-model"})
    route = OpenCollab(tmp_path, provider=cfg["provider"], model=cfg["model"]).configuration
    assert cfg["provider"] == route["provider"] == "anthropic"
    assert cfg["model"] == route["model"] == "example-model"
    assert llm_transport_metrics(cfg)["llm_base_url_sha256"] == route["base_url_sha256"]
    assert cfg["base_url_sha256"] == expected["base_url_sha256"]
