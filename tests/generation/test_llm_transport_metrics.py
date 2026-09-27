from __future__ import annotations

import json

import pytest

from opencollab_eval.generation.gen_prediction_config import bind_llm_transport, llm_transport_metrics


def test_transport_record_retains_defaults_empty_switches_and_separates_secrets(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "")
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "private-key")
    monkeypatch.delenv("OPENCOLLAB_LLM_USER_AGENT", raising=False)

    metrics = llm_transport_metrics({})

    assert metrics["wire_protocol"] == "chat_completions"
    assert metrics["reasoning_effort"] is None
    assert metrics["llm_base_url_sha256"] is None
    assert metrics["workflow_env"]["OPENCOLLAB_LLM_STREAM_CHAT"] == ""
    assert "OPENCOLLAB_LLM_USER_AGENT" not in metrics["workflow_env"]
    assert "OPENCOLLAB_API_KEY" not in metrics["workflow_env"]


def test_remote_transport_binding_keeps_its_existing_precedence(monkeypatch):
    environment = {"OPENCOLLAB_WIRE_PROTOCOL": "responses", "OPENCOLLAB_REASONING_EFFORT": "high"}
    monkeypatch.setenv("OPENCOLLAB_EVAL_WORKFLOW_ENV", json.dumps(environment))
    monkeypatch.setenv("OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256", "a" * 64)
    monkeypatch.setenv("OPENCOLLAB_EVAL_INVOCATION_ID", "invocation-1")
    metrics = llm_transport_metrics({"wire_protocol": "chat_completions", "reasoning_effort": "low"})

    bind_llm_transport(metrics)

    assert metrics["wire_protocol"] == "responses"
    assert metrics["reasoning_effort"] == "high"
    assert metrics["llm_base_url_sha256"] == "a" * 64
    assert metrics["workflow_env"] == environment
    assert metrics["invocation_id"] == "invocation-1"


def test_observed_switches_are_separate_from_supplied_wire_settings(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "responses")
    monkeypatch.setenv("OPENCOLLAB_REASONING_EFFORT", "high")
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "false")

    metrics = llm_transport_metrics({"wire_protocol": "chat_completions", "reasoning_effort": "low"})

    assert metrics["wire_protocol"] == "chat_completions"
    assert metrics["reasoning_effort"] == "low"
    assert metrics["workflow_env"]["OPENCOLLAB_WIRE_PROTOCOL"] == "responses"
    assert metrics["workflow_env"]["OPENCOLLAB_REASONING_EFFORT"] == "high"
    assert metrics["workflow_env"]["OPENCOLLAB_LLM_STREAM_CHAT"] == "false"


@pytest.mark.parametrize("value", ["not-a-digest", "A" * 64])
def test_remote_transport_binding_retains_digest_validation(monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256", value)

    with pytest.raises(ValueError, match="SHA-256 digest"):
        bind_llm_transport(llm_transport_metrics({}))
