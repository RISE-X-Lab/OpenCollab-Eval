from __future__ import annotations

import json

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
def test_remote_model_probe_uses_responses_wire_and_nested_reasoning(
    monkeypatch, tmp_path, workflow_env
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            llm_provider="openai",
            llm_model="gpt-5.6-sol",
            workflow_env=workflow_env,
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
    assert '"reasoning_effort":"xhigh"' in remote_command
    assert '"reasoning_effort":"low"' not in remote_command


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
