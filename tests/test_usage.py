from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencollab_eval.usage import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMResponse,
    Usage,
    model_context_window,
    pricing_for_model,
    usage_cost_usd,
)


def test_usage_retains_completion_accounting_data() -> None:
    raw_usage = {"provider": "test"}
    usage = Usage(
        input_tokens=1_000,
        output_tokens=50,
        cache_read_tokens=600,
        cache_creation_tokens=200,
        estimated=True,
        raw_usage=raw_usage,
        markup_recovered=1,
    )

    assert usage.total_tokens == 1_050
    assert usage.cache_read_tokens == 600
    assert usage.cache_creation_tokens == 200
    assert usage.estimated is True
    assert usage.raw_usage is raw_usage
    assert usage.markup_recovered == 1


def test_llm_response_uses_independent_default_values() -> None:
    first = LLMResponse()
    second = LLMResponse()

    first.tool_calls.append({"name": "read"})
    first.usage.input_tokens = 3

    assert second.tool_calls == []
    assert second.usage == Usage()
    assert DEFAULT_MAX_OUTPUT_TOKENS == 8_192


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-8-2026", 200_000),
        ("gpt-4o-mini", 128_000),
        ("gpt-4-turbo-preview", 128_000),
        ("gpt-4-0613", 8_192),
        ("o1-preview", 200_000),
        ("o3-mini", 200_000),
        ("deepseek-chat", 64_000),
        ("qwen2.5-coder", 131_072),
        ("glm-5.2", 400_000),
        ("gemini-2.5-pro", 1_000_000),
        ("k3", 1_048_576),
        ("kimi-for-coding", 262_144),
    ],
)
def test_model_context_window_preserves_known_models(
    model: str,
    expected: int,
) -> None:
    assert model_context_window(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "some-unknown-model",
        "kimi-k2.6",
        "kimi-k2.70",
        "k3-preview",
        "kimi-for-coding-preview",
    ],
)
def test_model_context_window_rejects_unknown_and_kimi_near_misses(
    model: str | None,
) -> None:
    assert model_context_window(model) is None


def test_glm_pricing_uses_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GLM_INPUT_USD_PER_MTOK",
        "GLM_CACHED_INPUT_USD_PER_MTOK",
        "GLM_CACHE_CREATION_USD_PER_MTOK",
        "GLM_OUTPUT_USD_PER_MTOK",
    ):
        monkeypatch.delenv(name, raising=False)

    assert pricing_for_model("glm-5.2") == {
        "mode": "glm-5.2-default",
        "input_usd_per_mtok": 1.4,
        "cached_input_usd_per_mtok": 0.26,
        "cache_creation_usd_per_mtok": 1.4,
        "output_usd_per_mtok": 4.4,
    }


def test_pricing_preserves_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLM_INPUT_USD_PER_MTOK", "2.5")
    monkeypatch.setenv("GLM_CACHED_INPUT_USD_PER_MTOK", "invalid")
    monkeypatch.setenv("GLM_CACHE_CREATION_USD_PER_MTOK", "3.5")
    monkeypatch.setenv("GLM_OUTPUT_USD_PER_MTOK", "7.5")

    assert pricing_for_model("custom-glm") == {
        "mode": "glm-5.2-default",
        "input_usd_per_mtok": 2.5,
        "cached_input_usd_per_mtok": 0.26,
        "cache_creation_usd_per_mtok": 3.5,
        "output_usd_per_mtok": 7.5,
    }


def test_unknown_model_pricing_preserves_generic_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_INPUT_USD_PER_MTOK", "2")
    monkeypatch.setenv("OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK", "0.5")
    monkeypatch.delenv("OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK", raising=False)
    monkeypatch.setenv("OPENCOLLAB_OUTPUT_USD_PER_MTOK", "8")

    assert pricing_for_model("unknown") == {
        "mode": "unset",
        "input_usd_per_mtok": 2.0,
        "cached_input_usd_per_mtok": 0.5,
        "cache_creation_usd_per_mtok": 2.0,
        "output_usd_per_mtok": 8.0,
    }


def test_usage_cost_accounts_for_cache_discount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GLM_INPUT_USD_PER_MTOK",
        "GLM_CACHED_INPUT_USD_PER_MTOK",
        "GLM_CACHE_CREATION_USD_PER_MTOK",
        "GLM_OUTPUT_USD_PER_MTOK",
    ):
        monkeypatch.delenv(name, raising=False)
    usage = Usage(
        input_tokens=1_000,
        output_tokens=50,
        cache_read_tokens=600,
        cache_creation_tokens=200,
    )

    cost = usage_cost_usd(usage, "glm-5.2")

    expected = (200 * 1.4 + 600 * 0.26 + 200 * 1.4 + 50 * 4.4) / 1_000_000
    assert cost == pytest.approx(expected)


def test_usage_cost_clamps_cached_input_to_total_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_INPUT_USD_PER_MTOK", "2")
    monkeypatch.setenv("OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK", "1")
    monkeypatch.setenv("OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK", "3")
    monkeypatch.setenv("OPENCOLLAB_OUTPUT_USD_PER_MTOK", "4")
    usage = Usage(
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=80,
        cache_creation_tokens=40,
    )

    cost = usage_cost_usd(usage, "unknown")

    assert cost == pytest.approx((80 * 1 + 40 * 3 + 10 * 4) / 1_000_000)


def test_usage_cost_accepts_legacy_usage_without_cache_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_INPUT_USD_PER_MTOK", "2")
    monkeypatch.setenv("OPENCOLLAB_OUTPUT_USD_PER_MTOK", "4")
    usage = SimpleNamespace(input_tokens=100, output_tokens=10)

    cost = usage_cost_usd(usage, "unknown")

    assert cost == pytest.approx((100 * 2 + 10 * 4) / 1_000_000)
