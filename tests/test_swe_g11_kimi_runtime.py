from __future__ import annotations

import json

import pytest
from test_swe_g11_parallel_runner import _args, _load_module

from opencollab_eval.engine.swe_v1_remote_state import (
    bind_remote_api_network_environment,
    read_remote_api_environment,
)
from opencollab_eval.usage import model_context_window


def test_kimi_for_coding_defaults_to_k27_thinking_runtime():
    module = _load_module()
    config = module.resolve_config(
        _args(
            llm_model="kimi-for-coding",
            llm_provider="openai",
            context_window=None,
            temperature=None,
            top_p=None,
            max_output_tokens=None,
        )
    )

    assert config.context_window == 262_144
    assert model_context_window("kimi-for-coding") == config.context_window
    assert config.temperature == 1.0
    assert config.top_p == 0.95
    assert config.max_output_tokens == 32_768
    workflow_env = dict(item.split("=", 1) for item in config.workflow_env)
    assert workflow_env["OPENCOLLAB_THINKING"] == "true"
    assert json.loads(workflow_env["OPENCOLLAB_THINKING_PARAMS"]) == {
        "thinking": {"type": "enabled", "keep": "all"}
    }


def test_k3_defaults_to_one_million_context_and_high_reasoning():
    module = _load_module()
    config = module.resolve_config(
        _args(
            llm_model="k3",
            llm_provider="openai",
            context_window=None,
            temperature=None,
            top_p=None,
            max_output_tokens=None,
        )
    )

    assert config.context_window == 1_048_576
    assert model_context_window("k3") == config.context_window
    assert config.temperature == 1.0
    assert config.top_p == 0.95
    assert config.max_output_tokens == 32_768
    workflow_env = dict(item.split("=", 1) for item in config.workflow_env)
    assert workflow_env["OPENCOLLAB_THINKING"] == "true"
    assert json.loads(workflow_env["OPENCOLLAB_THINKING_PARAMS"]) == {
        "reasoning_effort": "high"
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"context_window": 262_144}, "context-window 1048576"),
        ({"workflow_env": ["OPENCOLLAB_THINKING=false"]}, "OPENCOLLAB_THINKING=true"),
        (
            {"workflow_env": ['OPENCOLLAB_THINKING_PARAMS={"reasoning_effort":"low"}']},
            "reasoning_effort=high",
        ),
        ({"llm_provider": "anthropic"}, "llm-provider openai"),
    ],
)
def test_k3_rejects_runtime_identity_drift(overrides, message):
    module = _load_module()
    values = {
        "llm_model": "k3",
        "llm_provider": "openai",
        "context_window": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        module.resolve_config(_args(**values))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"temperature": 0.0}, "temperature 1"),
        ({"top_p": 1.0}, "top-p 0.95"),
        ({"context_window": 1024}, "context-window 262144"),
        ({"max_output_tokens": 1}, "max-output-tokens 32768"),
        ({"llm_provider": "anthropic", "top_p": None}, "llm-provider openai"),
        ({"workflow_env": ["OPENCOLLAB_THINKING=false"]}, "OPENCOLLAB_THINKING=true"),
        (
            {"workflow_env": ['OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"disabled"}}']},
            "thinking.type=enabled",
        ),
        (
            {"workflow_env": ['OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"off"}}']},
            "thinking.keep=all",
        ),
        (
            {
                "workflow_env": [
                    'OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"all"},"top_p":1}'
                ]
            },
            "contain only thinking",
        ),
        (
            {
                "workflow_env": [
                    'OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"all","budget":1}}'
                ]
            },
            "contain only type and keep",
        ),
    ],
)
def test_kimi_for_coding_rejects_configuration_that_would_route_away_from_k27(overrides, message):
    module = _load_module()
    values = {
        "llm_model": "kimi-for-coding",
        "llm_provider": "openai",
        "context_window": None,
        "top_p": None,
        "max_output_tokens": None,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        module.resolve_config(_args(**values))


def test_remote_model_probe_uses_selected_provider_and_stdin_token(monkeypatch, tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="kimi-for-coding",
            context_window=None,
            top_p=None,
            max_output_tokens=None,
            remote_proxy_base_url="http://127.0.0.1:18789",
            remote_python="/remote/runtime with space/bin/python",
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"status":"ok","thinking_proven":true,'
                '"actual_model":"kimi-for-coding","model_matches":true}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_remote_model_probe(config)

    assert result["status"] == "ok"
    assert result["provider"] == "openai"
    assert result["model"] == "kimi-for-coding"
    assert result["response_model"] == "kimi-for-coding"
    assert result["model_matches"] is True
    assert result["thinking_enabled"] is True
    assert result["thinking_proven"] is True
    assert captured["kwargs"]["input"] == "client-token\n"
    joined = " ".join(captured["command"])
    assert "kimi-for-coding" in joined
    assert '"type":"enabled"' in joined
    assert '"top_p":0.95' in joined
    assert '"max_tokens":32768' in joined
    assert "client-token" not in joined


def test_remote_model_probe_uses_claude_cli_identity_for_claude_code(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            model_name="claude-code-2.1.175-glm-5.2",
            llm_provider="anthropic",
            llm_model="glm-5.2",
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","thinking_proven":true,"actual_model":"glm-5.2"}\n',
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_remote_model_probe(config)["model_matches"] is True
    assert "claude-cli/2.1.175" in " ".join(captured["command"])


def test_remote_model_probe_uses_anthropic_sdk_identity_for_workflows(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            model_name="glm-5.2-g11",
            llm_provider="anthropic",
            llm_model="glm-5.2",
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","thinking_proven":true,"actual_model":"glm-5.2"}\n',
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_remote_model_probe(config)["model_matches"] is True
    assert "Anthropic/Python opencollab-eval" in " ".join(captured["command"])


def test_remote_model_probe_does_not_send_claude_identity_to_openai(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            model_name="claude-code-2.1.175-glm-5.2",
            llm_provider="openai",
            llm_model="glm-5.2",
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","thinking_proven":true,"actual_model":"glm-5.2"}\n',
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_remote_model_probe(config)["model_matches"] is True
    joined = " ".join(captured["command"])
    assert "claude-cli/" not in joined
    assert "Anthropic/Python" not in joined


def test_remote_model_probe_requires_exact_non_kimi_model_identity(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="gpt-5.6-sol",
            workflow_env=[
                "OPENCOLLAB_THINKING=true",
                'OPENCOLLAB_THINKING_PARAMS={"reasoning_effort":"xhigh"}',
            ],
        )
    )
    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "thinking_proven": False,
                    "thinking_request_bound": True,
                    "thinking_evidence": "requested_reasoning_effort",
                    "actual_model": "gpt-5.6",
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="remote model probe failed"):
        module.run_remote_model_probe(config)


def test_remote_model_probe_records_hidden_reasoning_effort_request(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="gpt-5.6-sol",
            workflow_env=[
                "OPENCOLLAB_THINKING=true",
                'OPENCOLLAB_THINKING_PARAMS={"reasoning_effort":"xhigh"}',
            ],
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "thinking_proven": False,
                    "thinking_request_bound": True,
                    "thinking_evidence": "requested_reasoning_effort",
                    "actual_model": "gpt-5.6-sol",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_remote_model_probe(config)

    assert result["model_matches"] is True
    assert result["thinking_proven"] is False
    assert result["thinking_request_bound"] is True
    assert result["thinking_evidence"] == "requested_reasoning_effort"
    assert '"reasoning_effort":"xhigh"' in " ".join(captured["command"])


def test_remote_model_probe_reads_kimi_key_only_on_remote(monkeypatch, tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="kimi-for-coding",
            context_window=None,
            top_p=None,
            max_output_tokens=None,
            proxy_env_file=None,
            local_proxy_base_url="",
            remote_proxy_base_url="https://api.kimi.com/coding/v1",
            remote_api_env_file="/srv/opencollab/secrets/kimi.env",
        )
    )
    captured = {}
    monkeypatch.setattr(
        module,
        "get_proxy_token",
        lambda *_args: pytest.fail("direct mode must not read a local token"),
    )

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","thinking_proven":true,"actual_model":"kimi-for-coding"}\n',
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_remote_model_probe(config)["status"] == "ok"
    joined = " ".join(captured["command"])
    assert "PYTHONPATH=" in joined
    assert "/srv/opencollab/secrets/kimi.env" in joined
    assert "read_remote_api_environment" in joined
    assert "bind_remote_api_network_environment" in joined
    assert captured["kwargs"]["input"] == ""


def test_remote_api_environment_separates_token_from_outbound_proxy(tmp_path):
    env_file = tmp_path / "kimi.env"
    env_file.write_text(
        "KIMI_API_KEY=secret\nHTTPS_PROXY=http://proxy.example.invalid:8888\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    parsed = read_remote_api_environment(str(env_file))

    assert parsed["token"] == "secret"
    assert parsed["network_env"] == {
        "HTTPS_PROXY": "http://proxy.example.invalid:8888"
    }

    inherited = {"https_proxy": "http://stale.example:8080"}
    bind_remote_api_network_environment(inherited, parsed["network_env"])
    assert inherited == {"HTTPS_PROXY": "http://proxy.example.invalid:8888"}


def test_kimi_direct_mode_uses_remote_credential_without_proxy_arguments():
    module = _load_module()
    config = module.resolve_config(
        _args(
            llm_model="kimi-for-coding", llm_provider="openai",
            context_window=None, temperature=None, top_p=None, max_output_tokens=None,
            local_proxy_base_url="", proxy_env_file=None,
            remote_proxy_base_url="https://api.kimi.com/coding/v1",
            remote_api_env_file="/srv/opencollab/secrets/kimi.env",
        )
    )
    command = module.task_command(config, 51)

    assert config.no_ensure_remote_proxy is True
    assert command[command.index("--remote-api-env-file") + 1] == "/srv/opencollab/secrets/kimi.env"
    assert "--proxy-env-file" not in command
    assert "--local-proxy-base-url" not in command


def test_k3_direct_mode_uses_remote_credential_and_bound_identity():
    module = _load_module()
    config = module.resolve_config(
        _args(
            llm_model="k3", llm_provider="openai",
            context_window=None, temperature=None, top_p=None, max_output_tokens=None,
            local_proxy_base_url="", proxy_env_file=None,
            remote_proxy_base_url="https://api.kimi.com/coding/v1",
            remote_api_env_file="/srv/opencollab/secrets/kimi.env",
        )
    )
    command = module.task_command(config, 7)

    assert config.context_window == 1_048_576
    assert command[command.index("--llm-model") + 1] == "k3"
    assert command[command.index("--context-window") + 1] == "1048576"
    assert command[command.index("--remote-api-env-file") + 1] == (
        "/srv/opencollab/secrets/kimi.env"
    )
    assert "--proxy-env-file" not in command
    assert "--local-proxy-base-url" not in command


def test_k3_model_id_is_case_sensitive():
    module = _load_module()
    with pytest.raises(ValueError, match="only for direct Kimi models"):
        module.resolve_config(
            _args(
                llm_model="K3",
                llm_provider="openai",
                remote_api_env_file="/srv/opencollab/secrets/kimi.env",
            )
        )


def test_kimi_remote_api_file_rejects_glm_or_loopback():
    module = _load_module()
    path = "/srv/opencollab/secrets/kimi.env"
    with pytest.raises(ValueError, match="only for direct Kimi models"):
        module.resolve_config(_args(remote_api_env_file=path))
    with pytest.raises(ValueError, match="api.kimi.com/coding/v1"):
        module.resolve_config(
            _args(
                llm_model="kimi-for-coding", llm_provider="openai",
                context_window=None, temperature=None, top_p=None, max_output_tokens=None,
                remote_api_env_file=path,
                remote_proxy_base_url="http://127.0.0.1:18789",
            )
        )


def test_remote_model_probe_skips_for_dry_run(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, dry_run=True))
    assert module.run_remote_model_probe(config) == {"status": "skipped", "reason": "dry_run"}


@pytest.mark.parametrize(
    ("actual_model", "accepted"),
    [
        ("kimi-k2.7-code", True),
        ("kimi-k2.7-thinking", True),
        ("kimi-k2.6", False),
        ("kimi-k2.70", False),
    ],
)
def test_remote_model_probe_accepts_only_alias_or_k27_backend(
    monkeypatch, tmp_path, actual_model, accepted
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="kimi-for-coding",
            context_window=None,
            top_p=None,
            max_output_tokens=None,
            remote_proxy_base_url="http://127.0.0.1:18789",
            remote_python="/remote/runtime with space/bin/python",
        )
    )
    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "thinking_proven": True,
                    "actual_model": actual_model,
                }
            ),
            stderr="",
        ),
    )

    if accepted:
        assert module.run_remote_model_probe(config)["model_matches"] is True
    else:
        with pytest.raises(RuntimeError, match="remote model probe failed"):
            module.run_remote_model_probe(config)


@pytest.mark.parametrize(
    ("actual_model", "accepted"),
    [
        ("k3", True),
        ("K3", True),
        ("k3-256k", False),
        ("kimi-k3", False),
    ],
)
def test_remote_model_probe_accepts_only_exact_k3_identity(
    monkeypatch, tmp_path, actual_model, accepted
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="k3",
            context_window=None,
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            remote_proxy_base_url="http://127.0.0.1:18789",
        )
    )
    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "thinking_proven": True,
                    "actual_model": actual_model,
                }
            ),
            stderr="",
        ),
    )

    if accepted:
        result = module.run_remote_model_probe(config)
        assert result["model_matches"] is True
        assert result["thinking_proven"] is True
    else:
        with pytest.raises(RuntimeError, match="remote model probe failed"):
            module.run_remote_model_probe(config)


def test_k3_remote_model_probe_sends_high_reasoning_effort(monkeypatch, tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="k3",
            context_window=None,
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            remote_proxy_base_url="http://127.0.0.1:18789",
            remote_python="/remote/runtime with space/bin/python",
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","thinking_proven":true,"actual_model":"k3"}\n',
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_remote_model_probe(config)["model_matches"] is True
    joined = " ".join(captured["command"])
    assert '"reasoning_effort":"high"' in joined
    assert '"max_tokens":32768' in joined
    assert "/remote/runtime with space/bin/python" in joined


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("access_terminated_error", "access_terminated_error"),
        ("synthetic-kimi-credential", None),
        ("Bearer sk-secret\nleak", None),
        ({"message": "Bearer sk-secret"}, None),
    ],
)
def test_remote_model_probe_preserves_only_safe_upstream_error_type(
    monkeypatch, tmp_path, error_type, expected
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="kimi-for-coding",
            context_window=None,
            top_p=None,
            max_output_tokens=None,
            remote_proxy_base_url="https://api.kimi.com/coding/v1",
            remote_api_env_file="/srv/opencollab/secrets/kimi.env",
        )
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command,
            3,
            stdout=json.dumps(
                {
                    "status": "http_error",
                    "code": 403,
                    "error_type": error_type,
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError) as captured:
        module.run_remote_model_probe(config)

    assert captured.value.result["http_status"] == 403
    assert captured.value.result["remote_error_type"] == expected


def test_systemic_failure_reasons_ignore_honest_unresolved_result():
    module = _load_module()
    assert module.systemic_failure_reasons(
        {"completed": True, "runner_status": "done", "technical_failed": 0, "unresolved": 1}
    ) == []


def test_parallel_runner_continues_after_task_technical_failure(monkeypatch, tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="1-3",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
            max_workers=1,
        )
    )
    started = []

    def fake_run_one(_config, index):
        started.append(index)
        return {
            "index": index,
            "returncode": 1,
            "runner_status": "done_with_technical_failures",
            "completed": True,
            "tasks": 1,
            "technical_failed": 1,
        }

    monkeypatch.setattr(module, "prepare_runtime", lambda _config: None)
    monkeypatch.setattr(module, "run_remote_health_checks", lambda _config: {"status": "ok"})
    monkeypatch.setattr(module, "run_remote_model_probe", lambda _config: {"status": "ok"})
    monkeypatch.setattr(module, "run_one", fake_run_one)
    monkeypatch.setattr(module, "build_token_summary", lambda _config: {})
    monkeypatch.setattr(module, "save_progress", lambda *args, **kwargs: None)

    summary = module.run_parallel(config)

    assert started == [1, 2, 3]
    assert summary["status"] == "done_with_technical_failures"
    assert summary["scheduler"]["halted"] is False
    assert summary["scheduler"]["not_started"] == []
