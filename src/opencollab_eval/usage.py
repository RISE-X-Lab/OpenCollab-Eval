"""Completion usage values and pricing used by OpenCollab-Eval."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_OUTPUT_TOKENS = 8_192

DEFAULT_GLM52_INPUT_USD_PER_MTOK = 1.4
DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK = 0.26
DEFAULT_GLM52_OUTPUT_USD_PER_MTOK = 4.4

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o3": 200_000,
    "deepseek": 64_000,
    "qwen": 131_072,
    "glm-5.2": 400_000,
    "gemini": 1_000_000,
}

#: Exact windows, one row per model whose family fallback above would be wrong.
#:
#: Every value here is a second copy of a number OpenCollab already holds in
#: ``opencollab.adapters.llm.types``, and the copy is what the recorder writes:
#: ``gen_prediction_workflow`` puts ``model_context_window(cfg["model"])`` into
#: ``metrics.jsonl`` as a run's ``context_window``, and the audit table reads
#: that field as the context the run actually had. The runtime meanwhile
#: compacts its history against *OpenCollab's* number. Two tables therefore
#: mean the recorded window and the enforced window can disagree, which is a
#: recorder that is wrong about its own instrument rather than a harmless
#: duplicate.
#:
#: This layer may not import the other table -- ``opencollab.adapters`` is not
#: in the public surface Eval is allowed to reach into, which
#: ``tests/test_boundaries.py`` enforces -- so the copy stays, and
#: ``tests/test_usage.py::test_recorded_context_window_is_the_one_the_runtime_ran_under``
#: reads OpenCollab's table out of its source and asserts the two agree for
#: every model a batch runs under. A drift then turns a test red instead of
#: quietly changing a recorded number.
EXACT_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_048_576,
    "k3": 1_048_576,
    "kimi-for-coding": 262_144,
    # 983,616 is this model's largest *input* in thinking mode, the mode the
    # batches run in (``OPENCOLLAB_REASONING_EFFORT=max``). Without this row the
    # substring rule below answered 131,072 -- the model's maximum *output* --
    # and wrote it into ``metrics.jsonl`` as the window three batches ran under.
    "qwen3.8-flash": 983_616,
}


@dataclass
class Usage:
    """Token accounting for one completion.

    Cached input is already included in ``input_tokens`` and is retained
    separately for observability and pricing.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated: bool = False
    raw_usage: dict[str, Any] = field(default_factory=dict)
    markup_recovered: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Completion result used by evaluator-compatible clients."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    reasoning: str | None = None


def model_context_window(model: str | None) -> int | None:
    """Return the known context window for a model identifier."""
    if not model:
        return None
    lowered = model.strip().lower().rsplit("/", 1)[-1]
    exact = EXACT_MODEL_CONTEXT_WINDOWS.get(lowered)
    if exact is not None:
        return exact
    for key, window in EXACT_MODEL_CONTEXT_WINDOWS.items():
        if re.fullmatch(rf"{re.escape(key)}-\d{{4}}(?:-\d{{2}}){{0,2}}", lowered):
            return window
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in lowered:
            return window
    return None


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def pricing_for_model(model: str | None) -> dict[str, float | str]:
    """Return per-million-token prices for a model."""
    if "glm" in (model or "").lower():
        input_price = _float_env(
            "GLM_INPUT_USD_PER_MTOK",
            DEFAULT_GLM52_INPUT_USD_PER_MTOK,
        )
        return {
            "mode": "glm-5.2-default",
            "input_usd_per_mtok": input_price,
            "cached_input_usd_per_mtok": _float_env(
                "GLM_CACHED_INPUT_USD_PER_MTOK",
                DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK,
            ),
            "cache_creation_usd_per_mtok": _float_env(
                "GLM_CACHE_CREATION_USD_PER_MTOK",
                input_price,
            ),
            "output_usd_per_mtok": _float_env(
                "GLM_OUTPUT_USD_PER_MTOK",
                DEFAULT_GLM52_OUTPUT_USD_PER_MTOK,
            ),
        }
    input_price = _float_env("OPENCOLLAB_INPUT_USD_PER_MTOK", 0.0)
    return {
        "mode": "unset",
        "input_usd_per_mtok": input_price,
        "cached_input_usd_per_mtok": _float_env(
            "OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK",
            0.0,
        ),
        "cache_creation_usd_per_mtok": _float_env(
            "OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK",
            input_price,
        ),
        "output_usd_per_mtok": _float_env(
            "OPENCOLLAB_OUTPUT_USD_PER_MTOK",
            0.0,
        ),
    }


def usage_cost_usd(usage: Usage, model: str | None) -> float:
    """Calculate completion cost from uncached, cached, and output tokens."""
    pricing = pricing_for_model(model)
    cached = max(int(getattr(usage, "cache_read_tokens", 0) or 0), 0)
    cache_creation = max(
        int(getattr(usage, "cache_creation_tokens", 0) or 0),
        0,
    )
    uncached_input = max(
        int(usage.input_tokens or 0) - cached - cache_creation,
        0,
    )
    return (
        uncached_input / 1_000_000 * float(pricing["input_usd_per_mtok"])
        + cached / 1_000_000 * float(pricing["cached_input_usd_per_mtok"])
        + cache_creation / 1_000_000 * float(pricing["cache_creation_usd_per_mtok"])
        + int(usage.output_tokens or 0) / 1_000_000 * float(pricing["output_usd_per_mtok"])
    )


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "LLMResponse",
    "Usage",
    "model_context_window",
    "pricing_for_model",
    "usage_cost_usd",
]
