"""Interpret the structured terminal state emitted by OpenHands."""

from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.engine.swe_v1_remote_records import read_tail_text


def _latest_event(stdout_log: Path) -> dict[str, object] | None:
    latest = None
    for line in read_tail_text(stdout_log, 1024 * 1024).splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("kind"), str):
            latest = event
    return latest


def terminal_error_evidence(stdout_log: Path) -> dict[str, object]:
    """Return bounded evidence when the final OpenHands event is an error."""
    event = _latest_event(stdout_log)
    if not event or event.get("kind") != "ConversationErrorEvent":
        return {}
    code = str(event.get("code") or "ConversationErrorEvent")
    if code == "MaxIterationsReached":
        return {}
    return {
        "openhands_terminal_error": {
            "kind": "ConversationErrorEvent",
            "code": code,
            "detail": str(event.get("detail") or "")[:4096],
        }
    }


def apply_empty_patch_failure(metrics: dict, patch: str) -> RuntimeError | None:
    """Turn a zero-exit model error without a candidate into a technical failure."""
    event = metrics.get("openhands_terminal_error")
    if not isinstance(event, dict) or patch.strip():
        return None
    code = str(event.get("code") or "ConversationErrorEvent")
    metrics.update(status="openhands_failed", workflow_status="error")
    return RuntimeError(f"OpenHands ended with {code} before producing a candidate patch")
