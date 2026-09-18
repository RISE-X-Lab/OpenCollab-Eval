"""Check the public G22 configuration entry against the official runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval import cli
from opencollab_eval.commands import g22
from opencollab_eval.engine.native_progress_watch import controller_wall_timeout, generation_wall_timeout
from opencollab_eval.runtime_config import effective_generation_limits


def write_config(tmp_path, **changes):
    values = {
        "remote_root": str(tmp_path / "worker"),
        "image_repository": "registry.example/swe",
        "proxy_env_file": str(tmp_path / "provider.env"),
        "remote_proxy_base_url": "http://127.0.0.1:18000/v1",
        "llm_model": "example-reasoning-model",
        "context_window": 1000000,
        "max_output_tokens": 32768,
        **changes,
    }
    path = tmp_path / "g22.json"
    path.write_text(json.dumps(values))
    return path


def test_default_config_reaches_single2_and_unbounded_progress_supervision(tmp_path):
    config = g22.resolve_config(write_config(tmp_path), {"run_id": "smoke"})
    env = dict(item.split("=", 1) for item in config.workflow_env)
    assert config.workflow == "validation-council-dual-coder-selection-v2"
    assert config.agent_profile == "single2"
    assert config.indices == (1,)
    assert config.runner_transport == "local"
    assert config.model_name == config.llm_model == "example-reasoning-model"
    assert config.output_dir == tmp_path / "results/smoke"
    assert config.remote_base == str(tmp_path / "worker/runs/smoke")
    assert config.remote_runtime_repo == str(tmp_path / "worker/runs/smoke/_runtime/repo")
    assert config.local_proxy_base_url == config.remote_proxy_base_url
    assert config.max_task_starts == 1 and config.max_empty_patch_retries == 0
    assert env["OPENCOLLAB_WIRE_PROTOCOL"] == "responses"
    assert env["OPENCOLLAB_REASONING_EFFORT"] == "max"
    limits = effective_generation_limits(budget=config.budget, max_steps=config.max_steps, environment=env)
    assert limits == (None, None)
    assert generation_wall_timeout(config.swe_timeout, env) is None
    assert controller_wall_timeout(config.total_timeout, env) is None
    assert env["OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT"] == "43200"


@pytest.mark.parametrize("environment", [
    {"OPENCOLLAB_UNBOUNDED_LIMITS": False, "OPENCOLLAB_REASONING_EFFORT": "high"},
    ["OPENCOLLAB_UNBOUNDED_LIMITS=false", "OPENCOLLAB_REASONING_EFFORT=high"],
])
def test_explicit_limits_and_profiles_are_passed_to_existing_resolver(tmp_path, environment):
    path = write_config(tmp_path, agent_profile="single", budget=500, max_steps=3, workflow_env=environment)
    config = g22.resolve_config(path, {"indices": "2-4,7", "max_workers": 2, "run_id": "batch"})
    env = dict(item.split("=", 1) for item in config.workflow_env)
    assert config.agent_profile is None
    assert config.indices == (2, 3, 4, 7)
    assert config.max_workers == 2
    assert env["OPENCOLLAB_REASONING_EFFORT"] == "high"
    assert effective_generation_limits(budget=config.budget, max_steps=config.max_steps, environment=env) == (500, 3)


@pytest.mark.parametrize("environment", [{}, []])
def test_host_limit_toggle_cannot_replace_default_g22_budget(tmp_path, monkeypatch, environment):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "false")
    config = g22.resolve_config(write_config(tmp_path, workflow_env=environment))
    env = dict(item.split("=", 1) for item in config.workflow_env)
    assert env["OPENCOLLAB_UNBOUNDED_LIMITS"] == "true"


def test_cli_dry_run_never_starts_runner_or_reads_provider_credentials(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    (tmp_path / "provider.env").write_text("OPENCOLLAB_UPSTREAM_API_KEY=test-only-key\n")

    def unexpected_run(config):
        pytest.fail("configuration-only invocation started the evaluator")

    monkeypatch.setattr(g22.parallel, "run_parallel", unexpected_run)
    assert cli.main(["g22", "--config", str(path), "--run-id", "check", "--dry-run"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "configuration_validated"
    assert result["model_calls"] == 0
    assert result["configuration"]["agent_profile"] == "single2"
    assert "test-only-key" not in output
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(("status", "expected_exit"), [("done", 0), ("done_with_technical_failures", 1)])
def test_execution_delegates_to_official_runner_and_keeps_terminal_exit(tmp_path, monkeypatch, status, expected_exit):
    seen = []

    def run(config):
        seen.append(config)
        return {"status": status}

    monkeypatch.setattr(g22.parallel, "run_parallel", run)
    monkeypatch.setattr(g22.parallel, "compact_progress", lambda value: value)
    result = cli.main(["g22", "--config", str(write_config(tmp_path)), "--indices", "2-50",
                       "--workers", "5", "--run-id", "remaining"])
    assert result == expected_exit
    assert len(seen) == 1
    assert seen[0].indices == tuple(range(2, 51))
    assert seen[0].max_workers == 5
    assert seen[0].workflow == g22.WORKFLOW


@pytest.mark.parametrize("changes", [
    {"workfow": "typo"},
    {"workflow": "base-team"},
    {"workflow_env": "invalid"},
    {"workflow_env": {"UNKNOWN_WORKFLOW_SETTING": "1"}},
    {"no_ensure_remote_proxy": "false"},
])
def test_invalid_config_stops_before_execution(tmp_path, monkeypatch, changes):
    monkeypatch.setattr(g22.parallel, "run_parallel", lambda config: pytest.fail("invalid config reached execution"))
    assert g22.main(["--config", str(write_config(tmp_path, **changes))]) == 2


def test_config_path_variables_are_expanded_without_changing_recorded_run(tmp_path, monkeypatch):
    monkeypatch.setenv("G22_TEST_ROOT", str(tmp_path / "worker with spaces"))
    config = g22.resolve_config(write_config(tmp_path, remote_root="${G22_TEST_ROOT}"), {"run_id": "variable"})
    assert config.remote_root == str(tmp_path / "worker with spaces")
    assert Path(config.remote_base) == tmp_path / "worker with spaces/runs/variable"
