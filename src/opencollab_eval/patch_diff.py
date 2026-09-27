"""OpenCollab patch primitives and evaluator-owned candidate filtering."""

from __future__ import annotations

from opencollab.patches import (
    decode_git_c_path,
    diff_target_path,
    git_diff_endpoint,
    git_header_tokens,
    normalize_patch_path,
    patch_block_target_path,
    patch_entries,
    patch_paths,
    split_patch_blocks,
)

from .patch_paths import is_generated_runtime_artifact_path

EVAL_TEST_DIRECTORY_NAMES = frozenset(
    {"test", "tests", "testing", "__tests__", "spec", "specs"}
)


def is_eval_test_path(path: str) -> bool:
    normalized = normalize_patch_path(path)
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if any(part in EVAL_TEST_DIRECTORY_NAMES for part in parts):
        return True
    return (
        name == "conftest.py"
        or name.endswith("_test.go")
        or name == "test.py"
        or name.startswith("test_")
        and name.endswith(".py")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def is_eval_control_path(path: str) -> bool:
    return normalize_patch_path(path).rsplit("/", 1)[-1] == "conftest.py"


def filter_patch_paths_with_evidence(
    patch: str,
    excluded_paths: set[str],
) -> tuple[str, list[str]]:
    excluded = {normalize_patch_path(path) for path in excluded_paths if path}
    if not excluded:
        return patch, []
    kept: list[str] = []
    removed: dict[str, None] = {}
    for block in split_patch_blocks(patch):
        text = "".join(block)
        entries = patch_entries(text)
        if not text.strip():
            kept.extend(block)
            continue
        if len(entries) != 1:
            raise RuntimeError("candidate patch block could not be classified safely")
        endpoints = [normalize_patch_path(path) for path in entries[0] if path]
        if any(path in excluded for path in endpoints):
            for path in endpoints:
                removed.setdefault(path, None)
        else:
            kept.extend(block)
    return "".join(kept), list(removed)


def filter_model_patch_with_evidence(patch: str) -> tuple[str, list[str]]:
    excluded = {
        path
        for path in patch_paths(patch)
        if is_eval_test_path(path) and not is_eval_control_path(path)
    }
    return filter_patch_paths_with_evidence(patch, excluded)


def filter_model_patch_for_eval(patch: str) -> str:
    filtered, _evidence = filter_model_patch_with_evidence(patch)
    return filtered


def remove_generated_artifact_blocks(
    patch: str,
    paths: set[str],
) -> tuple[str, list[str]]:
    normalized_paths = {normalize_patch_path(path) for path in paths}
    if not normalized_paths:
        return patch, []

    kept: list[str] = []
    removed: dict[str, None] = {}
    for lines in split_patch_blocks(patch):
        entries = patch_entries("".join(lines))
        if len(entries) != 1:
            raise RuntimeError("trusted patch block could not be classified safely")
        endpoints = [path for path in entries[0] if path]
        intersects = any(path in normalized_paths for path in endpoints)
        if not intersects:
            kept.extend(lines)
            continue
        if any(
            path not in normalized_paths
            or not is_generated_runtime_artifact_path(path)
            for path in endpoints
        ):
            raise RuntimeError(
                "trusted patch artifact filtering encountered a mixed-path entry"
            )
        for path in endpoints:
            removed.setdefault(path, None)
    if set(removed) != normalized_paths:
        raise RuntimeError("trusted patch artifact filtering was incomplete")
    return "".join(kept), list(removed)


__all__ = [
    "decode_git_c_path",
    "diff_target_path",
    "git_diff_endpoint",
    "git_header_tokens",
    "filter_model_patch_for_eval",
    "filter_model_patch_with_evidence",
    "filter_patch_paths_with_evidence",
    "is_eval_control_path",
    "is_eval_test_path",
    "normalize_patch_path",
    "patch_entries",
    "patch_block_target_path",
    "patch_paths",
    "remove_generated_artifact_blocks",
    "split_patch_blocks",
]
