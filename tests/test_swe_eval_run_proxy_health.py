from __future__ import annotations

import hashlib
import json

import pytest

from opencollab_eval.commands import swe_eval_run


def test_local_relay_health_accepts_v1_base(monkeypatch) -> None:
    captured = {}
    upstream = "https://api.example.invalid/v1"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "kind": "authenticated_model_relay",
                    "aggregate_chat_stream": True,
                    "responses_passthrough": True,
                    "allow_insecure_upstream": False,
                    "direct_upstream": False,
                    "compact_tool_schemas": False,
                    "max_upstream_request_bytes": 0,
                    "upstream_timeout": 900.0,
                    "upstream_base_url_sha256": hashlib.sha256(upstream.encode()).hexdigest(),
                }
            ).encode()

    def fake_urlopen(url, **_kwargs):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(swe_eval_run.urllib.request, "urlopen", fake_urlopen)

    assert swe_eval_run._local_relay_healthy("http://127.0.0.1:8879/v1", upstream) is True
    assert captured["url"] == "http://127.0.0.1:8879/healthz"


def test_relay_timeout_uses_the_single_runner_value() -> None:
    assert swe_eval_run._relay_upstream_timeout(["--llm-timeout", "21600"]) == 240.0


def test_relay_timeout_bounds_abandoned_upstream_requests() -> None:
    arguments = [
        "--llm-timeout",
        "21600",
        "--workflow-env",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=1800",
        "--workflow-env",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=600",
    ]

    assert swe_eval_run._relay_upstream_timeout(arguments) == 1860.0


def test_relay_timeout_uses_provider_defaults_for_omitted_activity_limit() -> None:
    arguments = [
        "--llm-timeout",
        "21600",
        "--workflow-env",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=60",
    ]

    assert swe_eval_run._relay_upstream_timeout(arguments) == 240.0


def test_relay_timeout_accepts_finite_fractional_activity_limits() -> None:
    arguments = [
        "--llm-timeout",
        "21600",
        "--workflow-env",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=60.5",
        "--workflow-env",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180.25",
    ]

    assert swe_eval_run._relay_upstream_timeout(arguments) == 240.25


@pytest.mark.parametrize(
    "arguments",
    [
        ["--llm-timeout", "900", "--llm-timeout", "21600"],
        ["--llm-timeout", "nan"],
        ["--llm-timeout", "inf"],
        ["--llm-timeout", "-1"],
        ["--llm-timeout", "1.5"],
    ],
)
def test_relay_timeout_rejects_ambiguous_or_invalid_values(arguments) -> None:
    with pytest.raises(RuntimeError, match="llm-timeout"):
        swe_eval_run._relay_upstream_timeout(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--workflow-env",
            "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=1800",
            "--workflow-env",
            "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=600",
        ],
        ["--workflow-env", "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=nan"],
        ["--workflow-env", "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=inf"],
        ["--workflow-env", "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=0"],
        ["--workflow-env", "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=-1"],
    ],
)
def test_relay_timeout_rejects_invalid_activity_limits(arguments) -> None:
    with pytest.raises(RuntimeError, match="OPENCOLLAB_LLM_"):
        swe_eval_run._relay_upstream_timeout(arguments)
