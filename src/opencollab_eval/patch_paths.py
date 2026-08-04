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


def is_generated_python_bytecode_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    return bool(
        parts
        and parts[-1].endswith((".pyc", ".pyo"))
    )


def is_generated_python_test_artifact_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    return any(part in {".hypothesis", ".pytest_cache", ".ruff_cache"} for part in parts)


def is_generated_runtime_artifact_path(path: str) -> bool:
    return (
        is_generated_dependency_artifact_path(path)
        or is_generated_python_bytecode_path(path)
        or is_generated_python_test_artifact_path(path)
    )
