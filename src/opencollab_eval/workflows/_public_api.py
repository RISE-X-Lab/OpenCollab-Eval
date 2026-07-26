"""Small adapters over OpenCollab's public workflow-authoring API."""

from __future__ import annotations

from typing import Any

from opencollab.tools import BuiltinToolName, Tool, builtin_tools


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
