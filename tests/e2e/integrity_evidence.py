"""Shared assertions for deterministic workspace-integrity evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def require_sanitized_snapshot(snapshot: Any, required_kinds: Iterable[str]) -> set[str]:
    if not isinstance(snapshot, dict):
        raise RuntimeError("candidate lacks a solver snapshot")
    integrity = snapshot.get("workspace_integrity")
    findings = integrity.get("findings") if isinstance(integrity, dict) else None
    if not isinstance(findings, list):
        raise RuntimeError("candidate lacks workspace integrity findings")
    kinds = {
        finding.get("observed_state", {}).get("kind")
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("observed_state"), dict)
    }
    missing = set(required_kinds) - kinds
    if missing:
        raise RuntimeError(f"solver snapshot missed baseline classifications: {sorted(missing)!r}")
    if snapshot.get("commit_count") != 1 or snapshot.get("remote_count") != 0:
        raise RuntimeError("solver snapshot retained repository history or remotes")
    return kinds
