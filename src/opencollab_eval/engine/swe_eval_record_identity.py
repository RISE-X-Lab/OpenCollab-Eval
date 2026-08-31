"""Canonical identity and numeric parsing for persisted evaluation records."""

from __future__ import annotations

import re
from typing import Any

_DIRECT_TASK_ID_FIELDS = ("task", "instance_id", "task_id")
_DIRECT_PATCH_SHA_FIELDS = ("patch_sha256", "patch_sha", "model_patch_sha256")


def direct_payload_alias_value(
    payload: dict[str, Any] | None,
    fields: tuple[str, ...],
) -> str | None:
    """Return a shared compatibility-alias value, or ``None`` on conflict."""
    if not isinstance(payload, dict):
        return None
    values: list[str] = []
    for field in fields:
        value = payload.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return None
        values.append(value)
    if not values:
        return ""
    first = values[0]
    return first if all(value == first for value in values[1:]) else None


def direct_payload_task_id(payload: dict[str, Any] | None) -> str | None:
    """Canonicalize ``task``/``instance_id``/``task_id`` aliases."""
    return direct_payload_alias_value(payload, _DIRECT_TASK_ID_FIELDS)


def direct_payload_patch_sha(payload: dict[str, Any] | None) -> str | None:
    """Canonicalize public patch-SHA aliases in a direct-eval payload."""
    return direct_payload_alias_value(payload, _DIRECT_PATCH_SHA_FIELDS)


def strict_integer(value: Any, *, nonnegative: bool = False) -> int | None:
    """Parse an integer without booleans, floats, truncation, or overflow."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        try:
            parsed = int(value.strip())
        except (ValueError, OverflowError):
            return None
    else:
        return None
    return parsed if not nonnegative or parsed >= 0 else None


__all__ = [
    "direct_payload_alias_value",
    "direct_payload_patch_sha",
    "direct_payload_task_id",
    "strict_integer",
]
