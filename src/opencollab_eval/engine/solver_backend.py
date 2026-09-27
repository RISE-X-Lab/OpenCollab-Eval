"""Generic solver configuration values and model-client helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any


def default_openai_user_agent() -> str:
    """Return the identity used by OpenCollab's AsyncOpenAI client."""
    return f"AsyncOpenAI/Python {version('openai')}"


def normalize_llm_user_agent(value: str) -> str:
    """Validate the model client identity before starting a task."""
    value = value.strip()
    if len(value.encode("utf-8")) > 256:
        raise ValueError("OPENCOLLAB_LLM_USER_AGENT must be at most 256 bytes")
    if any(not 32 <= ord(char) <= 126 for char in value):
        raise ValueError("OPENCOLLAB_LLM_USER_AGENT must contain printable ASCII only")
    return value


@dataclass(frozen=True, slots=True)
class WorkflowSolverSpec:
    """Configuration for a solver implemented by an OpenCollab workflow."""

    name: str
    workflow_name: str
    description: str
    max_attempts: int = 1
    default_budget_tokens: int | None = None
    required_runtime_options: tuple[tuple[str, str], ...] = ()
    config_overrides: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)


__all__ = ["WorkflowSolverSpec", "default_openai_user_agent", "normalize_llm_user_agent"]
