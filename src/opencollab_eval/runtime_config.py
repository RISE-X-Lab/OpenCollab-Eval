"""Resolve OpenCollab runtime settings through its public configuration view."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opencollab import OpenCollab


def resolve_runtime_config(
    workspace: str | Path,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-secret effective settings for evaluation evidence and calls."""
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
