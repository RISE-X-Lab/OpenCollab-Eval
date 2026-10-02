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


def handled_revoked_role_failures(metric):
    """Require concrete, matched native revocation observations for every role failure."""
    import re

    from opencollab_eval.engine.native_failure_attribution import classify_failure

    failures = metric.get("agent_failures") or []
    if not failures:
        return True
    if not isinstance(failures, (list, tuple)):
        return False
    counts = {}
    for failure in failures:
        if (not isinstance(failure, dict) or failure.get("exception_type") != "RuntimeError"
                or failure.get("status_code") is not None
                or failure.get("provider_error_type") is not None
                or classify_failure(record=failure)["origin"] != "oc"):
            return False
        label = failure.get("label")
        if not isinstance(label, str) or not label:
            return False
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label.rsplit("/", 1)[-1]).strip("-._")[:40]
        if not slug:
            return False
        counts[slug] = counts.get(slug, 0) + 1
    states = metric.get("workflow_role_states") or []
    if not isinstance(states, list) or any(not isinstance(state, dict) for state in states):
        return False
    reason = "RuntimeError: Execution environment has been revoked. Session cannot continue."
    for slug, count in counts.items():
        matches = [state for state in states
                   if re.fullmatch(r"\d+_" + re.escape(slug) + r"\.json", str(state.get("artifact") or ""))]
        if (len(matches) != count or len({state["artifact"] for state in matches}) != count or any(
            state.get("phase") != "error" or state.get("terminal_reason") != reason for state in matches
        )):
            return False
    return True


def _completed_duo_role(states, label):
    import re

    matches = [state for state in states if isinstance(state, dict)
               and re.fullmatch(r"\d+_" + re.escape(label) + r"\.json", str(state.get("artifact") or ""))]
    if len(matches) != 1:
        return False
    state = matches[0]
    return bool(
        state.get("phase") == "done" and state.get("terminal_reason") == "completed"
        and type(state.get("pending_events")) is int and state["pending_events"] == 0
        and state.get("pending_external_user_turn") is False
        and "active_turn_start_message_index" in state and state["active_turn_start_message_index"] is None
    )


def handled_workflow_role_failures(metric):
    """Recognize proven revocation and completed, discarded-candidate cleanup."""
    output = metric.get("workflow_result") or metric.get("workflow_role_selection") or {}
    selected = output.get("adopted") if isinstance(output, dict) else None
    winner = output.get("winner") if isinstance(output, dict) else None
    fallback = selected in ("A", "B") and winner in ("A", "B") and winner != selected
    if not metric.get("agent_failures") or not fallback and handled_revoked_role_failures(metric):
        return True
    failures = metric.get("agent_failures")
    states = metric.get("workflow_role_states") or []
    if not isinstance(failures, (list, tuple)) or not isinstance(output, dict) or not isinstance(states, list):
        return False
    if (selected not in ("A", "B") or winner not in ("A", "B") or output.get("status") != "done"
            or metric.get("runtime_status", metric.get("agent_status")) != "completed"
            or metric.get("execution_quiesced", metric.get("session_quiesced")) is not True):
        return False
    discarded = "dual-coder-contract-" + ("b" if selected == "A" else "a")
    if fallback:
        from opencollab_eval.engine.native_failure_attribution import classify_failure

        if (output.get("adoption_attempts") != [winner, selected]
                or not _completed_duo_role(states, "dual-coder-contract-" + selected.lower())
                or not _completed_duo_role(states, discarded)):
            return False
    remaining = []
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        if failure.get("label") != discarded + ":cleanup":
            if fallback:
                return False
            remaining.append(failure)
            continue
        matches = [state for state in states if isinstance(state, dict)
                   and str(state.get("artifact", "")).partition("_")[2] == discarded + ".json"]
        if (failure.get("exception_type") != "RuntimeError" or failure.get("status_code") is not None
                or failure.get("provider_error_type") is not None or len(matches) != 1):
            return False
        if fallback and classify_failure(record=failure)["origin"] != "oc":
            return False
        state = matches[0]
        if (state.get("phase") != "done" or state.get("terminal_reason") != "completed"
                or state.get("pending_events") != 0 or state.get("pending_external_user_turn") is not False
                or state.get("active_turn_start_message_index") is not None):
            return False
    return handled_revoked_role_failures({**metric, "agent_failures": remaining})


def adopted_workflow_candidate(metric):
    """Recognize explicit delivery separately from the council's own verdict."""
    if not isinstance(metric, dict):
        return False
    output = metric.get("workflow_result")
    native = metric.get("runtime_state") or {}
    if not isinstance(output, dict) or not isinstance(native, dict):
        return False
    origins = metric.get("workflow_role_failure_origins")
    if origins is None:
        origins = []
    if not isinstance(origins, list) or any(
        not isinstance(role, dict) or role.get("origin") != "oc" for role in origins
    ):
        return False
    diff_bytes = output.get("candidate_diff_bytes")
    return bool(
        metric.get("runtime_status") == "completed"
        and metric.get("agent_status") in (None, "completed")
        and metric.get("execution_quiesced") is True
        and not metric.get("error")
        and not metric.get("original_workflow_error")
        and handled_revoked_role_failures(metric)
        and not metric.get("failure_exception_chain")
        and not metric.get("technical_failure")
        and not metric.get("provider_failure")
        and not metric.get("workflow_role_provider_failure")
        and not metric.get("workflow_role_observation_errors")
        and not metric.get("trajectory_verification_error")
        and not metric.get("wall_clock_timeout")
        and not metric.get("technical_interruption_recoverable")
        and metric.get("runtime_reason") in (None, "", "completed")
        and metric.get("agent_reason") in (None, "", "completed")
        and native.get("status") in (None, "completed")
        and not native.get("external_error")
        and not native.get("error_chain")
        and (native.get("failure_attribution") or {}).get("origin") == "none"
        and (metric.get("failure_attribution") or {}).get("origin") in (None, "none")
        and output.get("status") in {"done", "incomplete", "blocked"}
        and output.get("candidate_status") in {"done", "incomplete", "blocked"}
        and output.get("candidate_adopted") is True
        and isinstance(diff_bytes, int)
        and not isinstance(diff_bytes, bool)
        and diff_bytes > 0
    )


def adopted_candidate_metric_view(metric, patch):
    """Correct an older delivery projection in memory, retaining source diagnostics."""
    from opencollab_eval.engine.swe_eval_records import (
        SUBMISSION_INTEGRITY_PROVEN,
        metric_submission_integrity,
    )
    from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid

    if not adopted_workflow_candidate(metric) or not patch.strip():
        return metric
    if metric.get("workflow_status") not in {"incomplete", "blocked"}:
        return metric
    if (metric.get("failure_origin") != "oc" or metric.get("oc_failure") is not True
            or metric.get("runner_returncode") != 1 or metric.get("candidate_probe_eligible") is not True
            or not current_generation_proof_valid(metric, patch)):
        return metric
    corrected = dict(metric, submission_eligible=True)
    if metric_submission_integrity(corrected) != SUBMISSION_INTEGRITY_PROVEN:
        return metric
    corrected["original_generation_projection"] = {
        field: metric.get(field) for field in (
            "workflow_status", "runner_returncode", "failure_origin", "oc_failure", "submission_eligible"
        )
    }
    corrected.update(workflow_status="done", runner_returncode=0, failure_origin="none", oc_failure=False)
    if metric.get("agent_failures"):
        corrected["workflow_role_failures_tolerated"] = True
    return corrected


def adopted_candidate_report_view(row, prediction, metric):
    """Expose a misclassified delivery to scoring discovery without rewriting reports."""
    from opencollab_eval.engine.swe_eval_record_identity import direct_payload_task_id
    from opencollab_eval.engine.swe_eval_records import (
        prediction_patch,
        row_explicit_patch_sha,
        row_record_id,
        row_task_id,
    )
    from opencollab_eval.engine.swe_v1_remote_records import generation_done_result

    generation = row.get("generation") or {}
    evaluation = row.get("eval") or {}
    if (generation.get("status") != "intrinsic_unresolved"
            or evaluation.get("status") != "skipped_intrinsic_unresolved"):
        return row
    corrected = adopted_candidate_metric_view(metric, prediction_patch(prediction))
    if not isinstance(corrected, dict) or "original_generation_projection" not in corrected:
        return row
    task = direct_payload_task_id(row)
    if (not task or row_task_id(prediction) != task or row_task_id(corrected) != task
            or not generation.get("record_id")
            or generation["record_id"] != row_record_id(prediction)
            or generation["record_id"] != row_record_id(corrected)
            or generation.get("patch_sha256") != row_explicit_patch_sha(prediction)
            or generation.get("patch_sha256") != row_explicit_patch_sha(corrected)):
        return row
    delivered = generation_done_result(task, prediction, corrected, generation.get("pairing") or "record_id")
    result = dict(row.get("task_result") or {}, status="official_eval_pending", oc_failure=False,
                  resolved=False, technical_failure=False, failure_origin="none")
    return dict(row, generation=delivered, task_result=result, oc_failure=False)


def adopted_candidate_source_report_view(row, source_base_run_dir):
    """Read a bound source pair for a legacy report's scoring view."""
    from pathlib import Path

    from opencollab_eval.engine.swe_eval_record_identity import direct_payload_task_id
    from opencollab_eval.engine.swe_v1_remote_records import latest_pair

    if (not isinstance(source_base_run_dir, (str, Path)) or not Path(source_base_run_dir).is_absolute()
            or (row.get("generation") or {}).get("status") != "intrinsic_unresolved"):
        return row
    task = direct_payload_task_id(row)
    if not task:
        return row
    try:
        prediction, metric, _pairing = latest_pair(Path(source_base_run_dir) / task, task)
    except (OSError, ValueError, RuntimeError):
        return row
    return adopted_candidate_report_view(row, prediction, metric)



def generation_integrity_evidence(metric):
    if not isinstance(metric, dict):
        return {}
    return {field: metric[field] for field in GENERATION_INTEGRITY_FIELDS if field in metric}



def generation_execution_invalid(metric):
    """Recognize a failed or unfinished execution before interpreting its candidate."""
    if not isinstance(metric, dict):
        return False
    return bool(
        metric.get("technical_failure") is True
        or metric.get("agent_status") == "failed"
        or metric.get("runtime_status") == "failed"
        or metric.get("session_quiesced") is False
        or metric.get("execution_quiesced") is False
        or bool(metric.get("workflow_role_observation_errors"))
        or not handled_workflow_role_failures(metric)
    )

def generation_outcome_evidence(metric, patch):
    """Keep OC's terminal outcome separate from candidate test performance."""
    if not isinstance(metric, dict):
        return {}
    agent_status = metric.get("agent_status")
    origin = metric.get("failure_origin")
    explicit = agent_status in {"completed", "stopped", "failed"} and origin is not None
    if not explicit:
        return {}
    technical = generation_execution_invalid(metric) or origin not in {"none", "oc"}
    intrinsic = not technical and (metric.get("oc_failure") is True or not patch.strip())
    selection = metric.get("workflow_result") or metric.get("workflow_role_selection") or {}
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
        **({field: metric[field] for field in (
            "agent_failures", "workflow_role_failure_origins", "workflow_role_states",
            "workflow_role_failures_tolerated", "session_quiesced", "execution_quiesced"
        ) if field in metric} if metric.get("agent_failures") else {}),
        **({"workflow_role_selection": {key: selection.get(key) for key in (
            "status", "winner", "adopted", "adoption_attempts"
        )} if isinstance(selection, dict) else selection} if metric.get("agent_failures") else {}),
        **({"original_generation_projection": metric["original_generation_projection"]}
           if "original_generation_projection" in metric else {}),
    }
