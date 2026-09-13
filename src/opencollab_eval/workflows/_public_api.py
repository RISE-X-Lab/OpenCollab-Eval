"""Small adapters over OpenCollab's public workflow-authoring API."""

from __future__ import annotations

import copy
import json
from typing import Any

from opencollab.tools import Tool

from opencollab_eval.verification import EvaluationToolName, evaluation_tools


def toolset(
    *names: EvaluationToolName,
    allow_file_creation: bool = True,
) -> list[Tool]:
    """Build a fresh, headless-safe tool list for one workflow role."""
    return list(
        evaluation_tools(
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


def validation_execution_evidence(
    decision: dict[str, Any], candidates: dict[str, Any],
) -> dict[str, Any]:
    """Keep the decision and original proposals together for the executing role."""
    proposals = candidates.get("tests", [])
    proposals = proposals if isinstance(proposals, list) else []
    accepted = decision.get("accepted", [])
    accepted = accepted if isinstance(accepted, list) else []
    accepted_ids = {
        item["id"] for item in accepted
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in proposals:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], []).append(item)
    return copy.deepcopy({
        **decision,
        "candidate_proposals": proposals,
        "accepted_candidates": [
            by_id[identifier][0] for identifier in sorted(accepted_ids)
            if len(by_id.get(identifier, [])) == 1
        ],
        "unresolved_accepted_ids": sorted(accepted_ids - by_id.keys()),
        "ambiguous_accepted_ids": sorted(
            identifier for identifier in accepted_ids if len(by_id.get(identifier, [])) > 1
        ),
    })


def role_feedback(*reports: Any) -> str:
    """Keep nested errors, retry feedback, and evidence alongside prose findings."""
    parts = [
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(report, dict) else report
        for report in reports
        if isinstance(report, dict) or isinstance(report, str) and report.strip()
    ]
    return "\n\n".join(parts) or "No structured feedback was returned; re-verify from the evidence package."
