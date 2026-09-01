"""Canonical identity and numeric parsing for persisted evaluation records."""

from __future__ import annotations

import re
from typing import Any

_DIRECT_TASK_ID_FIELDS = ("task", "instance_id", "task_id")
_DIRECT_PATCH_SHA_FIELDS = ("patch_sha256", "patch_sha", "model_patch_sha256")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def canonical_sha256(value: Any) -> str | None:
    """Return a strict, lower-case SHA-256 digest or ``None``."""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return None
    return value.lower()


def sha256_equal(left: Any, right: Any) -> bool:
    """Compare two strict SHA-256 digests without treating hex case as identity."""
    left_value = canonical_sha256(left)
    right_value = canonical_sha256(right)
    return left_value is not None and left_value == right_value


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
    if not isinstance(payload, dict):
        return None
    values: list[str] = []
    for field in _DIRECT_PATCH_SHA_FIELDS:
        value = payload.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return None
        # Valid SHA-256 aliases are equivalent regardless of hexadecimal case;
        # preserve malformed text so the proof boundary can reject it.
        values.append(canonical_sha256(value) or value)
    if not values:
        return ""
    first = values[0]
    return first if all(value == first for value in values[1:]) else None


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
    "canonical_sha256",
    "direct_payload_alias_value",
    "direct_payload_patch_sha",
    "direct_payload_task_id",
    "sha256_equal",
    "strict_integer",
]
