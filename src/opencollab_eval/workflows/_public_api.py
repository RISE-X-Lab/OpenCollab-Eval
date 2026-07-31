"""Small adapters over OpenCollab's public workflow-authoring API."""

from __future__ import annotations

import os
from typing import Any

from opencollab.tools import BuiltinToolName, Tool, builtin_tools

_LIMIT_FIELDS = {
    "bash": ("max_output_chars",),
    "file_read": ("max_read_chars",),
    "git_diff": ("max_diff_chars", "max_status_chars"),
    "grep": ("max_grep_chars",),
    "run_tests": ("max_traceback_chars",),
}


def _tool_output_limits(names: tuple[BuiltinToolName, ...]) -> dict[str, dict[str, int]]:
    raw = os.environ.get("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", "").strip()
    if not raw:
        return {}
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS must be an integer") from exc
    if not 256 <= limit <= 1_000_000:
        raise ValueError("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS must be in 256..1000000")
    return {
        name: {field: limit for field in _LIMIT_FIELDS[name]}
        for name in names
        if name in _LIMIT_FIELDS
    }


def toolset(
    *names: BuiltinToolName,
    allow_file_creation: bool = True,
) -> list[Tool]:
    """Build a fresh, headless-safe tool list for one workflow role."""
    return list(
        builtin_tools(
            *names,
            headless=True,
            allow_file_creation=allow_file_creation,
            limits=_tool_output_limits(names),
        )
    )


def format_findings_report(payload: dict[str, Any]) -> str:
    """Render structured scout findings for the next workflow stage."""
    lines: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary}")
    for finding in payload.get("findings") or ():
        aspect = str(finding.get("aspect") or "").strip()
        claim = str(finding.get("claim") or "").strip()
        anchor = str(finding.get("evidence_anchor") or "").strip()
        confidence = str(finding.get("confidence") or "").strip()
        verification = "verified" if finding.get("verified") else "unverified"
        prefix = f"({aspect}) " if aspect else ""
        evidence = f" [{anchor}]" if anchor else ""
        suffix = f" — {verification}"
        if confidence:
            suffix += f", confidence={confidence}"
        lines.append(f"- {prefix}{claim}{evidence}{suffix}")
    if payload.get("insufficient_evidence"):
        lines.append(
            "(insufficient_evidence: the scout could not gather enough evidence "
            "to fully answer this dimension)"
        )
    return "\n".join(lines)
