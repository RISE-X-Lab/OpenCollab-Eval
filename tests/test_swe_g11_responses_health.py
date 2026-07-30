from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_swe_g11_parallel_runner import _args, _load_module


@pytest.mark.parametrize(
    "workflow_env",
    [
        [
            "OPENCOLLAB_WIRE_PROTOCOL=responses",
            "OPENCOLLAB_REASONING_EFFORT=xhigh",
        ],
        [
            "OPENCOLLAB_WIRE_PROTOCOL=responses",
            "OPENCOLLAB_THINKING=true",
            'OPENCOLLAB_THINKING_PARAMS={"reasoning_effort":"xhigh"}',
        ],
        [
            "OPENCOLLAB_WIRE_PROTOCOL=responses",
            "OPENCOLLAB_REASONING_EFFORT=xhigh",
            "OPENCOLLAB_THINKING=true",
            'OPENCOLLAB_THINKING_PARAMS={"reasoning_effort":"low"}',
        ],
    ],
    ids=["independent-effort", "legacy-fallback", "independent-overrides-legacy"],
)
@pytest.mark.parametrize("user_agent", [None, "compatible-client/1.0", "   "])
def test_remote_model_probe_uses_responses_wire_and_nested_reasoning(
    monkeypatch, tmp_path, workflow_env, user_agent
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="gpt-5.6-sol",
            workflow_env=workflow_env
            + (
                [f"OPENCOLLAB_LLM_USER_AGENT={user_agent}"]
                if user_agent is not None
                else []
            ),
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
                    "wire_protocol": "responses",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(module, "get_proxy_token", lambda _path: "client-token")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_remote_model_probe(config)

    assert result["wire_protocol"] == "responses"
    assert result["wire_protocol_matches"] is True
    assert result["thinking_enabled"] is True
    assert result["thinking_request_bound"] is True
    remote_command = captured["command"][-1]
    assert "openai gpt-5.6-sol responses true" in remote_command
    assert 'path="/responses"' in remote_command
    assert 'payload["reasoning"]={"effort":reasoning_effort}' in remote_command
    assert 'payload["max_output_tokens"]=options["max_tokens"]' in remote_command
    assert 'retry_after_seconds' in remote_command
    assert 'probe_status="empty_output"' in remote_command
    assert '"reasoning_effort":"xhigh"' in remote_command
    assert '"reasoning_effort":"low"' not in remote_command
    expected_user_agent = (
        user_agent.strip()
        if user_agent and user_agent.strip()
        else module._shared_health.default_openai_user_agent()
    )
    assert expected_user_agent in remote_command
    assert f"OPENCOLLAB_LLM_USER_AGENT={expected_user_agent}" in config.workflow_env
    assert "OPENCOLLAB_LLM_MAX_RETRIES=10000" in config.workflow_env


def test_remote_model_probe_rejects_response_from_another_wire(
    monkeypatch, tmp_path
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="gpt-5.6-sol",
            workflow_env=["OPENCOLLAB_WIRE_PROTOCOL=responses"],
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
                    "thinking_request_bound": True,
                    "actual_model": "gpt-5.6-sol",
                    "wire_protocol": "chat_completions",
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="remote model probe failed"):
        module.run_remote_model_probe(config)


@pytest.mark.parametrize(
    "failure_result",
    [
        {"status": "http_error", "http_status": code, "direct": True}
        for code in (408, 429, 500, 502, 503, 504)
    ]
    + [
        {
            "status": "empty_output",
            "model_matches": True,
            "wire_protocol_matches": True,
            "thinking_enabled": True,
            "thinking_request_bound": True,
            "direct": True,
        },
        {"status": "transport_error", "direct": True},
        {"status": "failed", "failure_kind": "timeout", "direct": True},
    ],
    ids=["408", "429", "500", "502", "503", "504", "empty", "transport", "timeout"],
)
def test_remote_model_probe_waits_through_transient_failures(
    monkeypatch, tmp_path, failure_result
):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, retry_delay_seconds=0)
    )
    calls = 0

    def probe(_config):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module._shared_health.SharedProbeFailure(
                "overloaded",
                failure_result,
            )
        return {"status": "ok", "direct": True}

    monkeypatch.setattr(module, "run_remote_model_probe", probe)

    assert module.wait_for_remote_model_probe(config)["status"] == "ok"
    assert calls == 2
    ledger = json.loads((tmp_path / "model_probe_attempts.json").read_text())
    assert ledger["status"] == "ok"
    assert len(ledger["attempts"]) == 2


@pytest.mark.parametrize(
    "failure_result",
    [
        {"status": "http_error", "http_status": 401, "direct": True},
        {"status": "http_error", "http_status": 403, "direct": True},
        {"status": "ok", "model_matches": False, "direct": True},
        {"status": "ok", "wire_protocol_matches": False, "direct": True},
        {
            "status": "empty_output",
            "model_matches": False,
            "wire_protocol_matches": True,
            "direct": True,
        },
        {
            "status": "empty_output",
            "model_matches": True,
            "wire_protocol_matches": False,
            "direct": True,
        },
        {
            "status": "empty_output",
            "model_matches": True,
            "wire_protocol_matches": True,
            "thinking_enabled": True,
            "thinking_request_bound": False,
            "direct": True,
        },
    ],
    ids=[
        "401",
        "403",
        "model-mismatch",
        "wire-mismatch",
        "empty-model-mismatch",
        "empty-wire-mismatch",
        "empty-thinking-unbound",
    ],
)
def test_remote_model_probe_does_not_retry_permanent_failure(
    monkeypatch, tmp_path, failure_result
):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, retry_delay_seconds=0)
    )
    calls = 0

    def probe(_config):
        nonlocal calls
        calls += 1
        raise module._shared_health.SharedProbeFailure(
            "permanent failure",
            failure_result,
        )

    monkeypatch.setattr(module, "run_remote_model_probe", probe)

    with pytest.raises(RuntimeError, match="permanent failure"):
        module.wait_for_remote_model_probe(config)
    assert calls == 1


def test_remote_model_probe_retry_obeys_deadline(monkeypatch, tmp_path):
    module = _load_module()
    config = replace(
        module.resolve_config(_args(output_dir=tmp_path, retry_delay_seconds=0)),
        total_timeout=0,
    )
    monkeypatch.setattr(
        module,
        "run_remote_model_probe",
        lambda _config: (_ for _ in ()).throw(
            module._shared_health.SharedProbeFailure(
                "overloaded",
                {"status": "http_error", "http_status": 503, "direct": True},
            )
        ),
    )

    with pytest.raises(RuntimeError, match="retry deadline exhausted"):
        module.wait_for_remote_model_probe(config)


def test_remote_model_probe_wait_is_cancelable(monkeypatch, tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    monkeypatch.setattr(module._parallel_process, "interrupted", lambda: True)

    with pytest.raises(InterruptedError, match="interrupted"):
        module.wait_for_remote_model_probe(config)
