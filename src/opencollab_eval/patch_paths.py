"""Patch-path classification shared by generation and evaluation."""

from __future__ import annotations

GENERATED_DEPENDENCY_ARTIFACT_PATHS = frozenset(
    {
        ".yarn/install-state.gz",
    }
)


def is_generated_dependency_artifact_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    return normalized in GENERATED_DEPENDENCY_ARTIFACT_PATHS
