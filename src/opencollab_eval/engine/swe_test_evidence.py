"""Shared interpretation of parser-backed target evidence."""

from __future__ import annotations

from typing import Any

from opencollab_eval.engine.swe_eval_outcome import (
    TargetOutcome,
    target_evidence_outcome,
)


def target_evidence_passed(item: dict[str, Any]) -> bool | None:
    """Return the declared-target outcome, or ``None`` for invalid evidence."""
    outcome = target_evidence_outcome(item)
    if outcome is TargetOutcome.PASSED:
        return True
    if outcome in {TargetOutcome.CANDIDATE_FAILED, TargetOutcome.SKIPPED}:
        return False
    return None


__all__ = ["target_evidence_passed"]
