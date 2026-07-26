"""Failure records used by one-task remote generation."""

# ruff: noqa: F403, F405

from __future__ import annotations

import hashlib
import stat

from opencollab_eval.engine import swe_v1_remote_cleanup as remote_cleanup
from opencollab_eval.engine.provider_failures import (
    summarize_terminal_provider_failures,
    terminal_provider_failure_result,
)
from opencollab_eval.engine.swe_generation_proof import solver_git_snapshot_valid
from opencollab_eval.engine.swe_v1_remote_records import *  # noqa: F403

MAX_GENERATION_FAILURE_BYTES = 128 * 1024


def _generation_base_commit_matches(row, metric):
    expected = str(row.get("base_commit") or row.get("commit") or "").strip().lower()
    if not expected:
        return True
    snapshot = metric.get("solver_git_snapshot") if isinstance(metric, dict) else None
    return bool(
        isinstance(snapshot, dict)
        and str(snapshot.get("expected_base_commit") or "").strip().lower() == expected
    )


def _generation_provider_failure_result(row, task, prediction, metric, pairing, *, expected_generation_image_id):
    metric = metric if isinstance(metric, dict) else {}
    if not summarize_terminal_provider_failures(metric.get("agent_failures")):
        return None
    if not generation_identity_matches(prediction, metric, require_patch=False):
        return None
    empty_patch_sha = hashlib.sha256(b"").hexdigest()
    return terminal_provider_failure_result(
        metric.get("agent_failures"),
        identity_valid=(
            row_task_id(prediction) == row_task_id(metric) == task
            and row_record_id(prediction) == row_record_id(metric) != ""
            and prediction_patch(prediction) == ""
            and row_patch_sha(prediction) == row_patch_sha(metric) == empty_patch_sha
            and solver_git_snapshot_valid(metric.get("solver_git_snapshot"))
            and _generation_base_commit_matches(row, metric)
            and metric.get("generation_image_id") == expected_generation_image_id
        ),
        task=task,
        pairing=pairing,
        patch_len=len(prediction_patch(prediction)),
        workflow_status=workflow_status(metric),
        record_id=row_record_id(prediction),
        patch_sha256=row_patch_sha(prediction),
    )


def clear_generation_failure_record(run_dir, task):
    path = run_dir / "generation_failure.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_GENERATION_FAILURE_BYTES:
        return {
            "status": "technical_generation_failure_record_unsafe",
            "task": task,
            "failure_scope": "task",
        }
    path.unlink()
    return None


def generation_failure_evidence(run_dir, task):
    path = run_dir / "generation_failure.json"
    try:
        value = remote_cleanup.read_bounded_json(
            path,
            max_bytes=MAX_GENERATION_FAILURE_BYTES,
        )
    except (FileNotFoundError, OSError, remote_cleanup.CleanupInputError):
        return {}
    integrity = value.get("workspace_integrity")
    scope = str(value.get("failure_scope") or "")
    valid = (
        value.get("schema") == "opencollab.generation_failure.v1"
        and value.get("instance_id") == task
        and isinstance(value.get("phase"), str)
        and bool(value["phase"])
        and scope in {"task", "image"}
        and isinstance(integrity, dict)
        and integrity.get("schema") == "opencollab.workspace_integrity.v1"
        and integrity.get("failure_scope") == scope
        and isinstance(integrity.get("findings"), list)
    )
    if not valid:
        return {
            "failure_scope": "task",
            "generation_failure": {
                "schema": "opencollab.generation_failure.invalid",
                "artifact": str(path),
            },
        }
    return {"failure_scope": scope, "generation_failure": value}


__all__ = [
    "MAX_GENERATION_FAILURE_BYTES",
    "_generation_base_commit_matches",
    "_generation_provider_failure_result",
    "clear_generation_failure_record",
    "generation_failure_evidence",
]
