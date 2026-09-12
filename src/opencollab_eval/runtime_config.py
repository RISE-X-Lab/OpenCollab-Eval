"""Resolve OpenCollab runtime settings through its public configuration view."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def resolve_runtime_config(
    workspace: str | Path,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-secret effective settings for evaluation evidence and calls."""
    from opencollab import OpenCollab

    values = dict(overrides or {})
    client = OpenCollab(
        workspace,
        model=values.pop("model", None),
        provider=values.pop("provider", None),
        base_url=values.pop("base_url", None),
        config=values,
    )
    return dict(client.configuration)


__all__ = ["resolve_runtime_config"]


def resolve_generation_environment(configured, *, environment=None):
    """Inherit the host limit toggle and let explicit workflow settings win."""
    inherited = os.environ if environment is None else environment
    effective = {}
    if "OPENCOLLAB_UNBOUNDED_LIMITS" in inherited:
        effective["OPENCOLLAB_UNBOUNDED_LIMITS"] = str(inherited["OPENCOLLAB_UNBOUNDED_LIMITS"])
    effective.update({str(key): str(value) for key, value in configured.items()})
    return effective


def effective_generation_limits(*, budget, max_steps, environment=None):
    """Resolve the same effective limits for generation and persisted identity."""
    values = resolve_generation_environment({} if environment is None else environment)
    unbounded = str(values.get("OPENCOLLAB_UNBOUNDED_LIMITS", "")).strip().lower()
    if unbounded not in {"", "0", "false", "1", "true"}:
        raise ValueError("OPENCOLLAB_UNBOUNDED_LIMITS must be true or false")
    return (None, None) if unbounded in {"1", "true"} else (budget, max_steps)
