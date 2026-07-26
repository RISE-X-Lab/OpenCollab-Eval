"""Shared interpretation of parser-backed target evidence."""

from __future__ import annotations

from typing import Any


def target_evidence_passed(item: dict[str, Any]) -> bool | None:
    """Return the declared-target outcome, or ``None`` for invalid evidence."""
    status = item.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    passed = item.get("target_proof_matches_plan") is True
    failed = item.get("target_failure_proof_matches_plan") is True
    if passed == failed or failed and status == 0:
        return None
    return passed


__all__ = ["target_evidence_passed"]
