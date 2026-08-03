from __future__ import annotations

import hashlib
import json

from opencollab_eval.engine.swe_generation_proof import (
    MAX_TRUSTED_PATCH_BYTES,
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_EXTRACTED_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
    TRUSTED_PATCH_EXTRACTION_SCHEMA,
)
from opencollab_eval.generation.gen_prediction_snapshot_support import anonymous_commit_oid


def current_inline_generation_schema_fields() -> dict:
    return {
        "generation_proof_schema": "opencollab.generation_proof.v2",
        "solver_task_specification": {
            "schema": "opencollab.solver_task_specification.v1",
            "delivery": "inline",
            "source_bytes": 1,
            "source_sha256": "9" * 64,
            "interfaces_required": False,
        },
    }


def trusted_summary_proof_fields(
    patch_sha256: str,
    *,
    patch_bytes: int = 1,
) -> dict:
    snapshot = {
        "enabled": True,
        "anonymous_head": "a" * 40,
        "base_tree": "b" * 40,
        "workspace_sha256": "0" * 64,
        "commit_count": 1,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": 0,
        "removed_gitlinks": [],
        "materialized_gitlinks": [],
        "expected_base_commit": "e" * 40,
        "workspace_integrity": {
            "schema": "opencollab.workspace_integrity.v1",
            "findings": [],
            "outcome": "allow",
            "failure_scope": "none",
        },
    }
    extraction = {
        "schema": TRUSTED_PATCH_EXTRACTION_SCHEMA,
        "host_trusted": True,
        "fixed_anonymous_base": snapshot["anonymous_head"],
        "base_tree": snapshot["base_tree"],
        "archive_bounded": True,
        "baseline_archive_sha256": "c" * 64,
        "baseline_archive_bytes": 10,
        "baseline_archive_entries": 1,
        "baseline_extracted_bytes": 1,
        "workspace_archive_sha256": "d" * 64,
        "workspace_archive_bytes": 10,
        "workspace_archive_entries": 1,
        "workspace_extracted_bytes": 1,
        "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
        "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
        "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
        "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
        "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
        "container_quiesced_before": True,
        "container_quiesced_after": True,
        "workspace_frozen_during_copy": True,
        "patch_sha256": patch_sha256,
        "patch_bytes": patch_bytes,
        "candidate_tree": "f" * 40,
        "changed_paths": [],
        "path_modes": [],
        "workspace_integrity": {
            "schema": "opencollab.workspace_integrity.v1",
            "findings": [],
            "outcome": "allow",
            "failure_scope": "none",
        },
    }
    return {
        **current_inline_generation_schema_fields(),
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": snapshot,
        "trusted_patch_extraction": extraction,
    }


def trusted_patch_proof_fields(patch: str) -> dict:
    encoded = patch.encode("utf-8", errors="surrogatepass")
    return trusted_summary_proof_fields(
        hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    )


def eval_snapshot_proof_fields(expected_base_commit: str = "e" * 40) -> dict:
    integrity = {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [],
        "outcome": "allow",
        "failure_scope": "none",
    }
    pre = {
        "schema": "opencollab.preparation_input.v1",
        "expected_base_commit": expected_base_commit,
        "base_tree": "b" * 40,
        "workspace_sha256": "1" * 64,
        "gitlinks": [],
        "materialized_gitlinks": [],
        "worktree_matches_base": True,
        "solver_started": False,
        "object_access_scope": "trusted_public_preparation_only",
        "workspace_integrity": integrity,
    }
    post = trusted_summary_proof_fields("f" * 64)["solver_git_snapshot"]
    post["expected_base_commit"] = expected_base_commit
    return {**post, "preparation_input_snapshot": pre}


def candidate_eval_proof_fields(
    task: str,
    record_id: str,
    source_patch_sha256: str,
    eval_patch_sha256: str | None = None,
    *,
    source_candidate_tree: str = "f" * 40,
    expected_candidate_tree: str | None = None,
    base_commit: str = "a" * 40,
    base_tree: str = "b" * 40,
    source_base_commit: str = "e" * 40,
) -> tuple[dict, dict]:
    eval_sha = eval_patch_sha256 or source_patch_sha256
    expected_tree = source_candidate_tree if expected_candidate_tree is None else expected_candidate_tree
    expectation = {
        "schema": "opencollab.eval_candidate_expectation.v1",
        "instance_id": task,
        "record_id": record_id,
        "run_identity_sha256": "c" * 64,
        "source_patch_sha256": source_patch_sha256,
        "eval_patch_sha256": eval_sha,
        "source_base_commit": source_base_commit,
        "source_anonymous_base": anonymous_commit_oid(base_tree),
        "source_base_tree": base_tree,
        "source_candidate_tree": source_candidate_tree,
        "expected_candidate_tree": expected_tree,
    }
    source_projection = candidate_source_projection_fields(expectation)
    projection = {
        "schema": "opencollab.eval_candidate_projection.v2",
        "status": "verified",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "source_projection_sha256": hashlib.sha256(
            (json.dumps(source_projection, sort_keys=True) + "\n").encode()
        ).hexdigest(),
        "verified_source_base_commit": source_base_commit,
        "verified_source_anonymous_base": expectation["source_anonymous_base"],
        "verified_source_base_tree": base_tree,
        "verified_source_candidate_tree": expected_tree or "e" * len(base_tree),
        "prepared_base_commit": base_commit,
        "prepared_base_tree": base_tree,
        "prepared_candidate_tree": expected_tree or "e" * len(base_tree),
        "worktree_candidate_tree": expected_tree or "e" * len(base_tree),
        "generation_tree_matches": True if expected_tree else None,
        "official_worktree_matches": True,
    }
    return expectation, projection


def candidate_source_projection_fields(expectation: dict) -> dict:
    expected_tree = expectation["expected_candidate_tree"]
    return {
        "schema": "opencollab.eval_candidate_source_projection.v1",
        "status": "verified",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "verified_source_base_commit": expectation["source_base_commit"],
        "verified_source_anonymous_base": expectation["source_anonymous_base"],
        "verified_source_base_tree": expectation["source_base_tree"],
        "verified_source_candidate_tree": expected_tree or "e" * len(
            expectation["source_base_tree"]
        ),
        "generation_tree_matches": True if expected_tree else None,
    }
