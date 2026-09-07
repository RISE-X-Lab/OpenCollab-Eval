from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import opencollab
import pytest

from opencollab_eval.usage import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    EXACT_MODEL_CONTEXT_WINDOWS,
    MODEL_CONTEXT_WINDOWS,
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
        ("deepseek-v4-flash", 1_048_576),
        ("deepseek-v4-flash-0731", 1_048_576),
        ("vendor/deepseek-v4-flash-2026-07-31", 1_048_576),
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


# --- Cross-repository agreement on the recorded context window ---------------
#
# `gen_prediction_workflow` writes `model_context_window(cfg["model"])` into
# `metrics.jsonl`, and the paper's audit table reads that field as the context
# a run actually had. OpenCollab derives its history-compaction trigger from a
# table of its own. Two tables mean the recorded window and the enforced window
# can disagree, and on 2026-09-06 they did: OpenCollab had `qwen3.8-flash` at
# 983,616 while this repository had no row for it, so the family fallback
# answered 131,072 -- that model's maximum *output* -- and three batches were
# recorded under a window no run ever ran at.
#
# The table is read out of OpenCollab's source rather than imported.
# `opencollab.adapters` is not part of the surface this layer may import (see
# `tests/test_boundaries.py`, which scans this file too), and the value under
# test is a literal in that file, so parsing it is not a weaker check than
# importing it would be.

_OPENCOLLAB_TYPES_SOURCE = (
    Path(opencollab.__file__).resolve().parent / "adapters" / "llm" / "types.py"
)

#: Every model a batch on record was, or is about to be, run under.
#:
#: `gpt-5.6-luna` is on the list with `None` on purpose: its runs were produced
#: under the fallback an unlisted model gets on both sides, and that agreement
#: is as much a fact about the instrument as a number would be.
MODELS_BATCHES_RUN_UNDER: tuple[str, ...] = (
    "deepseek-v4-flash",
    "qwen3.8-flash",
    "gpt-5.6-luna",
    "glm-5.2",
    "k3",
    "kimi-for-coding",
)


def _opencollab_module_ast() -> ast.Module:
    assert _OPENCOLLAB_TYPES_SOURCE.is_file(), (
        f"OpenCollab's capability table is not where this test expects it: "
        f"{_OPENCOLLAB_TYPES_SOURCE}"
    )
    return ast.parse(_OPENCOLLAB_TYPES_SOURCE.read_text(encoding="utf-8"))


def _assigned_dict(tree: ast.Module, name: str) -> ast.Dict:
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Dict):
            return node.value
    raise AssertionError(f"{name} is not a module-level dict literal in {_OPENCOLLAB_TYPES_SOURCE}")


def _dict_items(node: ast.Dict) -> list[tuple[str, ast.expr]]:
    items: list[tuple[str, ast.expr]] = []
    for key, value in zip(node.keys, node.values, strict=True):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), "non-literal key"
        items.append((key.value, value))
    return items


def opencollab_exact_context_windows() -> dict[str, int]:
    """`_EXACT_MODEL_CAPABILITIES` reduced to ``{model: context_window}``."""
    table = _assigned_dict(_opencollab_module_ast(), "_EXACT_MODEL_CAPABILITIES")
    windows: dict[str, int] = {}
    for model, value in _dict_items(table):
        assert isinstance(value, ast.Call), f"{model}: expected a ModelCapabilities(...) call"
        for keyword in value.keywords:
            if keyword.arg == "context_window" and isinstance(keyword.value, ast.Constant):
                windows[model] = keyword.value.value
    return windows


def opencollab_family_context_windows() -> dict[str, int]:
    table = _assigned_dict(_opencollab_module_ast(), "MODEL_CONTEXT_WINDOWS")
    return {
        family: value.value
        for family, value in _dict_items(table)
        if isinstance(value, ast.Constant)
    }


def _opencollab_context_window(model: str) -> int | None:
    """OpenCollab's answer for ``model``, by its own two rules in order."""
    exact = opencollab_exact_context_windows()
    leaf = model.strip().lower().rsplit("/", 1)[-1]
    if leaf in exact:
        return exact[leaf]
    for family, window in opencollab_family_context_windows().items():
        if leaf == family or leaf.startswith(f"{family}-"):
            return window
    return None


@pytest.mark.parametrize("model", MODELS_BATCHES_RUN_UNDER)
def test_recorded_context_window_is_the_one_the_runtime_ran_under(model: str) -> None:
    assert model_context_window(model) == _opencollab_context_window(model)


def test_exact_context_windows_never_contradict_opencollabs() -> None:
    """A row here may be absent from OpenCollab's table, but never disagree."""
    theirs = opencollab_exact_context_windows()
    assert theirs, "read no rows out of OpenCollab's table: the reader, not the table, is broken"
    disagreements = {
        model: (window, theirs[model])
        for model, window in EXACT_MODEL_CONTEXT_WINDOWS.items()
        if model in theirs and theirs[model] != window
    }
    assert disagreements == {}


def test_qwen38_flash_records_its_measured_input_ceiling() -> None:
    """983,616 is the input ceiling in thinking mode, the mode batches run in.

    131,072 is this model's maximum *output*. Recorded in the input field it put
    history compaction at 98,072 tokens -- a tenth of the usable window -- and
    the wrong number in the audit table's realized-context column.
    """
    assert model_context_window("qwen3.8-flash") == 983_616
    assert model_context_window("qwen3.8-flash") != 131_072
    assert model_context_window("qwen3.8-flash") != MODEL_CONTEXT_WINDOWS["qwen"]


def test_deepseek_v4_flash_keeps_the_window_its_batches_were_recorded_under() -> None:
    """The ladder's runs are on record under 1,048,576; nothing may move it.

    Both reachable wrong answers are named: the family fallback would give
    64,000, and dropping the exact row is the one edit that reaches it.
    """
    assert model_context_window("deepseek-v4-flash") == 1_048_576
    assert EXACT_MODEL_CONTEXT_WINDOWS["deepseek-v4-flash"] == 1_048_576
    assert model_context_window("deepseek-v4-flash") != MODEL_CONTEXT_WINDOWS["deepseek"]
    assert opencollab_exact_context_windows()["deepseek-v4-flash"] == 1_048_576
    for suffixed in ("deepseek-v4-flash-0731", "vendor/deepseek-v4-flash-2026-07-31"):
        assert model_context_window(suffixed) == 1_048_576
