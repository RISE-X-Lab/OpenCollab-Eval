"""Structured provider-failure evidence shared by generation and scheduling."""

from __future__ import annotations

from typing import Any

TERMINAL_PROVIDER_ERROR_TYPES = frozenset({"access_terminated_error"})


def summarize_terminal_provider_failures(value: Any) -> dict[str, Any] | None:
    """Summarize terminal account-level failures without retaining messages."""
    if not isinstance(value, list | tuple):
        return None
    matched = [
        item
        for item in value
        if isinstance(item, dict)
        and item.get("provider_error_type") in TERMINAL_PROVIDER_ERROR_TYPES
    ]
    if not matched:
        return None
    statuses = sorted(
        {
            status
            for item in matched
            if isinstance((status := item.get("status_code")), int)
            and not isinstance(status, bool)
        }
    )
    return {
        "status": "provider_request_rejected",
        "error_types": sorted({item["provider_error_type"] for item in matched}),
        "http_statuses": statuses,
        "occurrences": len(matched),
        "direct_shared_probe_required": True,
    }


def terminal_provider_failure_result(
    failures: Any,
    *,
    identity_valid: bool,
    task: str,
    pairing: Any,
    patch_len: int,
    workflow_status: str,
    record_id: Any,
    patch_sha256: Any,
) -> dict[str, Any] | None:
    """Build a task-local generation failure after identity validation."""
    evidence = summarize_terminal_provider_failures(failures)
    if not evidence:
        return None
    if not identity_valid:
        return {
            "status": "technical_generation_provider_evidence_invalid",
            "task": task,
            "pairing": pairing,
            "failure_scope": "task",
        }
    return {
        "status": "technical_generation_provider_failed",
        "task": task,
        "pairing": pairing,
        "patch_len": patch_len,
        "workflow_status": workflow_status,
        "record_id": record_id,
        "patch_sha256": patch_sha256,
        "provider_failure": evidence,
        "failure_scope": "task",
    }


__all__ = [
    "TERMINAL_PROVIDER_ERROR_TYPES",
    "summarize_terminal_provider_failures",
    "terminal_provider_failure_result",
]
