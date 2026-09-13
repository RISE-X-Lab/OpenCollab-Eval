"""Pure projection of native generation outcomes into evaluation metadata."""

GENERATION_INTEGRITY_FIELDS = (
    "generation_image_id",
    "submission_eligible",
    "execution_quiesced",
    "patch_extraction_succeeded",
    "injected_path_cleanup_proven",
    "harness_artifact_exclusion_proven",
    "checkpoint_restore_integrity_proven",
    "task_stage_integrity_proven",
    "test_patch_isolation_failed",
    "worktree_integrity_proven",
    "patch_produced",
    "checkpoint_result",
    "solver_git_snapshot",
    "trusted_patch_extraction",
)



def generation_integrity_evidence(metric):
    if not isinstance(metric, dict):
        return {}
    return {field: metric[field] for field in GENERATION_INTEGRITY_FIELDS if field in metric}


def generation_outcome_evidence(metric, patch):
    """Keep OC's terminal outcome separate from candidate test performance."""
    if not isinstance(metric, dict):
        return {}
    agent_status = metric.get("agent_status")
    origin = metric.get("failure_origin")
    explicit = agent_status in {"completed", "stopped", "failed"} and origin is not None
    if not explicit:
        return {}
    technical = metric.get("technical_failure") is True or origin not in {"none", "oc"}
    intrinsic = metric.get("oc_failure") is True or (not technical and not patch.strip())
    return {
        "usage_complete": metric.get("usage_complete"),
        "used_tokens_lower_bound": metric.get("used_tokens_lower_bound"),
        "usage_status": metric.get("usage_status"),
        "runtime_outcome": metric.get("runtime_outcome"),
        "configured_agent_timeout_seconds": metric.get("configured_agent_timeout_seconds"),
        "agent_status": agent_status,
        "agent_reason": metric.get("agent_reason"),
        "failure_origin": "oc" if intrinsic and origin == "none" else origin,
        "failure_phase": "empty_candidate" if intrinsic and origin == "none" else metric.get("failure_phase"),
        "failure_exception_chain": metric.get("failure_exception_chain", []),
        "oc_failure": intrinsic,
        "technical_failure": technical,
        "technical_interruption_recoverable": metric.get("technical_interruption_recoverable", False),
        "origin_record_id": metric.get("origin_record_id", metric.get("record_id")),
    }
