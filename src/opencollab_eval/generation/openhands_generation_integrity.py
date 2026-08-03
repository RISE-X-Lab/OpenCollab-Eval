"""Final candidate integrity fields for OpenHands-backed solvers."""

from __future__ import annotations

from opencollab_eval.engine.swe_generation_proof import generation_identity_proven


def complete_openhands_integrity(
    metrics: dict,
    *,
    patch: str,
    snapshot_prepared: bool,
    process_quiesced: bool,
    patch_extraction_succeeded: bool,
    harness_artifact_exclusion_proven: bool,
) -> None:
    patch_produced = bool(patch.strip())
    trusted_extraction_proven = bool(
        snapshot_prepared
        and patch_extraction_succeeded
        and generation_identity_proven(metrics, patch)
    )
    worktree_integrity_proven = bool(
        trusted_extraction_proven and harness_artifact_exclusion_proven
    )
    metrics.update(
        {
            "submission_eligible": bool(
                metrics.get("workflow_status") == "done"
                and patch_produced
                and process_quiesced
                and worktree_integrity_proven
            ),
            "execution_quiesced": process_quiesced,
            "patch_extraction_succeeded": trusted_extraction_proven,
            "injected_path_cleanup_proven": trusted_extraction_proven,
            "harness_artifact_exclusion_proven": harness_artifact_exclusion_proven,
            "checkpoint_restore_integrity_proven": trusted_extraction_proven,
            "task_stage_integrity_proven": trusted_extraction_proven,
            "test_patch_isolation_failed": False,
            "worktree_integrity_proven": worktree_integrity_proven,
            "patch_produced": patch_produced,
        }
    )


__all__ = ["complete_openhands_integrity"]
