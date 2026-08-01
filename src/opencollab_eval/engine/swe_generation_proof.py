"""Strict validation for current SWE generation integrity evidence."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

TRUSTED_PATCH_EXTRACTION_SCHEMA = "opencollab.trusted_patch_extraction.v1"
MAX_WORKSPACE_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_WORKSPACE_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_ARCHIVE_ENTRIES = 1_000_000
MAX_TRUSTED_PATCH_BYTES = 8 * 1024 * 1024

_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PREPARATION_INPUT_KEYS = {
    "schema",
    "expected_base_commit",
    "base_tree",
    "workspace_sha256",
    "gitlinks",
    "materialized_gitlinks",
    "worktree_matches_base",
    "solver_started",
    "object_access_scope",
    "workspace_integrity",
}
_SNAPSHOT_KEYS = {
    "enabled",
    "anonymous_head",
    "base_tree",
    "workspace_sha256",
    "commit_count",
    "remote_count",
    "extra_git_metadata",
    "removed_git_metadata",
    "removed_gitlinks",
    "materialized_gitlinks",
    "expected_base_commit",
    "workspace_integrity",
}
_EXTRACTION_KEYS = {
    "schema",
    "host_trusted",
    "fixed_anonymous_base",
    "base_tree",
    "archive_bounded",
    "baseline_archive_sha256",
    "baseline_archive_bytes",
    "baseline_archive_entries",
    "baseline_extracted_bytes",
    "workspace_archive_sha256",
    "workspace_archive_bytes",
    "workspace_archive_entries",
    "workspace_extracted_bytes",
    "archive_byte_limit",
    "extracted_byte_limit",
    "file_byte_limit",
    "entry_limit",
    "patch_byte_limit",
    "container_quiesced_before",
    "container_quiesced_after",
    "workspace_frozen_during_copy",
    "patch_sha256",
    "patch_bytes",
    "candidate_tree",
    "changed_paths",
    "path_modes",
    "workspace_integrity",
}
_EXTRACTION_SANITIZATION_KEYS = {
    "pre_sanitization_patch_sha256",
    "pre_sanitization_candidate_tree",
}


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _candidate_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def solver_git_snapshot_valid(value: Any) -> bool:
    """Return whether *value* is an exact current solver snapshot proof."""
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        return False
    anonymous_head = value.get("anonymous_head")
    base_tree = value.get("base_tree")
    workspace_sha256 = value.get("workspace_sha256")
    if not isinstance(anonymous_head, str) or not _OBJECT_ID_RE.fullmatch(anonymous_head):
        return False
    if not isinstance(base_tree, str) or not _OBJECT_ID_RE.fullmatch(base_tree):
        return False
    if len(anonymous_head) != len(base_tree):
        return False
    if not isinstance(workspace_sha256, str) or not _SHA256_RE.fullmatch(workspace_sha256):
        return False
    expected_base_commit = value.get("expected_base_commit")
    integrity = value.get("workspace_integrity")
    if (
        not isinstance(expected_base_commit, str)
        or not _OBJECT_ID_RE.fullmatch(expected_base_commit)
        or len(expected_base_commit) != len(anonymous_head)
        or not isinstance(integrity, dict)
        or integrity.get("schema") != "opencollab.workspace_integrity.v1"
        or integrity.get("failure_scope") != "none"
        or integrity.get("outcome") not in {"allow", "sanitize_then_continue"}
        or not isinstance(integrity.get("findings"), list)
        or len(integrity["findings"]) > 2048
    ):
        return False
    removed_gitlinks = value.get("removed_gitlinks")
    if (
        not isinstance(removed_gitlinks, list)
        or len(removed_gitlinks) > 1024
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "old_oid"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or "\x00" in item["path"]
            or not isinstance(item.get("old_oid"), str)
            or not _OBJECT_ID_RE.fullmatch(item["old_oid"])
            for item in removed_gitlinks
        )
        or len({item["path"] for item in removed_gitlinks}) != len(removed_gitlinks)
        or sum(len(item["path"].encode("utf-8")) for item in removed_gitlinks)
        > 128 * 1024
    ):
        return False
    materialized_gitlinks = value.get("materialized_gitlinks")
    if (
        not isinstance(materialized_gitlinks, list)
        or len(materialized_gitlinks) > 1024
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "oid", "content_sha256"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or "\x00" in item["path"]
            or not isinstance(item.get("oid"), str)
            or not _OBJECT_ID_RE.fullmatch(item["oid"])
            or len(item["oid"]) != len(anonymous_head)
            or not isinstance(item.get("content_sha256"), str)
            or not _SHA256_RE.fullmatch(item["content_sha256"])
            for item in materialized_gitlinks
        )
        or len({item["path"] for item in materialized_gitlinks}) != len(materialized_gitlinks)
    ):
        return False
    return bool(
        value.get("enabled") is True
        and value.get("commit_count") == 1
        and not isinstance(value.get("commit_count"), bool)
        and value.get("remote_count") == 0
        and not isinstance(value.get("remote_count"), bool)
        and value.get("extra_git_metadata") == 0
        and not isinstance(value.get("extra_git_metadata"), bool)
        and _nonnegative_int(value.get("removed_git_metadata"))
    )


def preparation_input_valid(value: Any) -> bool:
    """Validate the pre-public-setup evidence bound into an eval snapshot."""
    if not isinstance(value, dict) or set(value) != _PREPARATION_INPUT_KEYS:
        return False
    expected = value.get("expected_base_commit")
    base_tree = value.get("base_tree")
    workspace_sha256 = value.get("workspace_sha256")
    integrity = value.get("workspace_integrity")
    gitlinks = value.get("gitlinks")
    materialized = value.get("materialized_gitlinks")
    if (
        not isinstance(expected, str)
        or not _OBJECT_ID_RE.fullmatch(expected)
        or not isinstance(base_tree, str)
        or not _OBJECT_ID_RE.fullmatch(base_tree)
        or len(expected) != len(base_tree)
        or not isinstance(workspace_sha256, str)
        or not _SHA256_RE.fullmatch(workspace_sha256)
        or value.get("worktree_matches_base") is not True
        or value.get("solver_started") is not False
        or value.get("object_access_scope") != "trusted_public_preparation_only"
        or not isinstance(integrity, dict)
        or integrity.get("schema") != "opencollab.workspace_integrity.v1"
        or integrity.get("failure_scope") != "none"
        or integrity.get("outcome") not in {"allow", "sanitize_then_continue"}
        or not isinstance(integrity.get("findings"), list)
        or len(integrity["findings"]) > 2048
    ):
        return False
    if (
        not isinstance(gitlinks, list)
        or len(gitlinks) > 1024
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "oid"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or "\x00" in item["path"]
            or not isinstance(item.get("oid"), str)
            or not _OBJECT_ID_RE.fullmatch(item["oid"])
            or len(item["oid"]) != len(expected)
            for item in gitlinks
        )
        or len({item["path"] for item in gitlinks}) != len(gitlinks)
    ):
        return False
    gitlink_map = {item["path"]: item["oid"] for item in gitlinks}
    return bool(
        isinstance(materialized, list)
        and len(materialized) <= 1024
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "oid", "content_sha256"}
            and gitlink_map.get(item.get("path")) == item.get("oid")
            and isinstance(item.get("content_sha256"), str)
            and _SHA256_RE.fullmatch(item["content_sha256"])
            for item in materialized
        )
        and len({item["path"] for item in materialized}) == len(materialized)
    )


def _trusted_patch_extraction_shape_valid(
    value: Any,
    *,
    snapshot: Any,
) -> bool:
    if not solver_git_snapshot_valid(snapshot):
        return False
    if not isinstance(value, dict):
        return False
    keys = set(value)
    sanitized = keys == _EXTRACTION_KEYS | _EXTRACTION_SANITIZATION_KEYS
    if keys != _EXTRACTION_KEYS and not sanitized:
        return False
    if value.get("schema") != TRUSTED_PATCH_EXTRACTION_SCHEMA:
        return False
    if value.get("host_trusted") is not True or value.get("archive_bounded") is not True:
        return False
    if value.get("container_quiesced_before") is not True:
        return False
    if value.get("container_quiesced_after") is not True:
        return False
    if value.get("workspace_frozen_during_copy") is not True:
        return False
    if value.get("fixed_anonymous_base") != snapshot["anonymous_head"]:
        return False
    if value.get("base_tree") != snapshot["base_tree"]:
        return False
    integrity = value.get("workspace_integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("schema") != "opencollab.workspace_integrity.v1"
        or integrity.get("failure_scope") != "none"
        or integrity.get("outcome") not in {"allow", "sanitize_then_continue"}
        or not isinstance(integrity.get("findings"), list)
        or len(integrity["findings"]) > 2048
    ):
        return False
    if sanitized and (
        integrity.get("outcome") != "sanitize_then_continue"
        or not isinstance(value.get("pre_sanitization_patch_sha256"), str)
        or not _SHA256_RE.fullmatch(value["pre_sanitization_patch_sha256"])
        or value["pre_sanitization_patch_sha256"] == value.get("patch_sha256")
        or not isinstance(value.get("pre_sanitization_candidate_tree"), str)
        or not _OBJECT_ID_RE.fullmatch(value["pre_sanitization_candidate_tree"])
        or len(value["pre_sanitization_candidate_tree"]) != len(snapshot["anonymous_head"])
        or value["pre_sanitization_candidate_tree"] == value["candidate_tree"]
    ):
        return False
    candidate_tree = value.get("candidate_tree")
    changed_paths = value.get("changed_paths")
    path_modes = value.get("path_modes")
    valid_modes = {"000000", "100644", "100755", "120000", "160000"}
    if (
        not isinstance(candidate_tree, str)
        or not _OBJECT_ID_RE.fullmatch(candidate_tree)
        or len(candidate_tree) != len(snapshot["anonymous_head"])
        or not isinstance(changed_paths, list)
        or len(changed_paths) > MAX_WORKSPACE_ARCHIVE_ENTRIES
        or any(not _candidate_path(path) for path in changed_paths)
        or len(set(changed_paths)) != len(changed_paths)
        or not isinstance(path_modes, list)
        or len(path_modes) != len(changed_paths)
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "old_mode", "new_mode"}
            or item.get("path") not in changed_paths
            or item.get("old_mode") not in valid_modes
            or item.get("new_mode") not in valid_modes
            or item.get("old_mode") == item.get("new_mode") == "000000"
            for item in path_modes
        )
        or {item["path"] for item in path_modes} != set(changed_paths)
    ):
        return False
    for key in ("baseline_archive_sha256", "workspace_archive_sha256"):
        if not isinstance(value.get(key), str) or not _SHA256_RE.fullmatch(value[key]):
            return False
    for key in (
        "baseline_archive_bytes",
        "baseline_archive_entries",
        "baseline_extracted_bytes",
        "workspace_archive_bytes",
        "workspace_archive_entries",
        "workspace_extracted_bytes",
        "patch_bytes",
    ):
        if not _nonnegative_int(value.get(key)):
            return False
    if any(
        value[key] <= 0
        for key in (
            "baseline_archive_bytes",
            "baseline_archive_entries",
            "workspace_archive_bytes",
            "workspace_archive_entries",
        )
    ):
        return False
    expected_limits = {
        "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
        "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
        "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
        "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
        "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
    }
    if any(value.get(key) != expected for key, expected in expected_limits.items()):
        return False
    if value["baseline_archive_bytes"] > MAX_WORKSPACE_ARCHIVE_BYTES:
        return False
    if value["workspace_archive_bytes"] > MAX_WORKSPACE_ARCHIVE_BYTES:
        return False
    if value["baseline_archive_entries"] > MAX_WORKSPACE_ARCHIVE_ENTRIES:
        return False
    if value["workspace_archive_entries"] > MAX_WORKSPACE_ARCHIVE_ENTRIES:
        return False
    if value["baseline_extracted_bytes"] > MAX_WORKSPACE_EXTRACTED_BYTES:
        return False
    if value["workspace_extracted_bytes"] > MAX_WORKSPACE_EXTRACTED_BYTES:
        return False
    if value["patch_bytes"] > MAX_TRUSTED_PATCH_BYTES:
        return False
    return isinstance(value.get("patch_sha256"), str) and bool(
        _SHA256_RE.fullmatch(value["patch_sha256"])
    )


def trusted_patch_extraction_valid(
    value: Any,
    *,
    patch: str,
    snapshot: Any,
) -> bool:
    """Bind a current host extraction proof to its snapshot and exact patch."""
    if not _trusted_patch_extraction_shape_valid(value, snapshot=snapshot):
        return False
    encoded = patch.encode("utf-8", errors="surrogatepass")
    return bool(
        value.get("patch_bytes") == len(encoded)
        and value.get("patch_sha256") == hashlib.sha256(encoded).hexdigest()
    )


def current_generation_proof_valid(metric: Any, patch: str) -> bool:
    """Validate the two proofs required by every current generation record."""
    if not isinstance(metric, dict):
        return False
    if not isinstance(metric.get("generation_image_id"), str) or not _IMAGE_ID_RE.fullmatch(
        metric["generation_image_id"]
    ):
        return False
    snapshot = metric.get("solver_git_snapshot")
    return trusted_patch_extraction_valid(
        metric.get("trusted_patch_extraction"),
        patch=patch,
        snapshot=snapshot,
    )


def generation_llm_calls_proven(metric: Any) -> bool:
    """Require one identity-bound trajectory with the configured model."""
    if not isinstance(metric, dict):
        return False
    model = metric.get("llm_model")
    return bool(
        isinstance(model, str)
        and model
        and metric.get("trajectory_models") == [model]
        and metric.get("provider_models") == [model]
        and isinstance(metric.get("trajectory_llm_call_count"), int)
        and not isinstance(metric.get("trajectory_llm_call_count"), bool)
        and metric["trajectory_llm_call_count"] > 0
        and isinstance(metric.get("trajectory_sha256"), str)
        and _SHA256_RE.fullmatch(metric["trajectory_sha256"])
        and metric.get("wire_protocol") in {"chat_completions", "responses"}
    )


def current_generation_summary_proof_valid(metric: Any) -> bool:
    """Validate proof shape and patch identity in a report row without patch text."""
    if not isinstance(metric, dict):
        return False
    if not isinstance(metric.get("generation_image_id"), str) or not _IMAGE_ID_RE.fullmatch(
        metric["generation_image_id"]
    ):
        return False
    snapshot = metric.get("solver_git_snapshot")
    extraction = metric.get("trusted_patch_extraction")
    if not _trusted_patch_extraction_shape_valid(extraction, snapshot=snapshot):
        return False
    patch_sha = metric.get("patch_sha256")
    return bool(
        isinstance(patch_sha, str)
        and _SHA256_RE.fullmatch(patch_sha)
        and extraction["patch_sha256"] == patch_sha
    )


__all__ = [
    "TRUSTED_PATCH_EXTRACTION_SCHEMA",
    "MAX_TRUSTED_PATCH_BYTES",
    "MAX_WORKSPACE_ARCHIVE_BYTES",
    "MAX_WORKSPACE_ARCHIVE_ENTRIES",
    "MAX_WORKSPACE_EXTRACTED_BYTES",
    "MAX_WORKSPACE_FILE_BYTES",
    "current_generation_proof_valid",
    "current_generation_summary_proof_valid",
    "generation_llm_calls_proven",
    "preparation_input_valid",
    "solver_git_snapshot_valid",
    "trusted_patch_extraction_valid",
]
