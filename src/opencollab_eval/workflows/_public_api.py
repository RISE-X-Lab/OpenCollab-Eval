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

_RESULT_LIMIT_ENV = "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"


def _parse_tool_output_limits(raw: str) -> tuple[int | None, dict[str, int]]:
    if "=" not in raw:
        return _validated_limit(raw), {}
    overrides: dict[str, int] = {}
    for item in raw.split(","):
        name, separator, value = item.strip().partition("=")
        if not separator or name not in {"default", *_LIMIT_FIELDS} or name in overrides:
            raise ValueError(f"{_RESULT_LIMIT_ENV} has an invalid tool limit mapping")
        overrides[name] = _validated_limit(value)
    return overrides.pop("default", None), overrides


def _validated_limit(raw: str) -> int:
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_RESULT_LIMIT_ENV} limits must be integers") from exc
    if not 256 <= limit <= 1_000_000:
        raise ValueError(f"{_RESULT_LIMIT_ENV} limits must be in 256..1000000")
    return limit


def _tool_output_limits(names: tuple[BuiltinToolName, ...]) -> dict[str, dict[str, int]]:
    raw = os.environ.get(_RESULT_LIMIT_ENV, "").strip()
    if not raw:
        return {}
    default, overrides = _parse_tool_output_limits(raw)
    limits = {}
    for name in names:
        limit = overrides.get(name, default)
        if name in _LIMIT_FIELDS and limit is not None:
            limits[name] = {field: limit for field in _LIMIT_FIELDS[name]}
    return limits


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
