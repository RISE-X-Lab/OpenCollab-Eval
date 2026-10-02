"""Project native workflow outcomes into evaluation observations."""

from __future__ import annotations

import re
from pathlib import Path

from opencollab import OpenCollab

from opencollab_eval.engine.native_failure_attribution import classify_failure, persistence_failure
from opencollab_eval.engine.native_progress_watch import is_progress_stop
from opencollab_eval.engine.swe_v1_generation_outcomes import (
    adopted_workflow_candidate,
    handled_workflow_role_failures,
)


def workflow_candidate_delivered(result, *, role_states=None, observation_errors=None) -> bool:
    if role_states is None and result.agent_failures:
        role_states, observation_errors = _role_states(result.trajectory_path)
    return adopted_workflow_candidate({
        "workflow_result": result.workflow_result,
        "runtime_status": result.runtime_status,
        "runtime_reason": result.runtime_reason,
        "runtime_state": result.runtime_state,
        "execution_quiesced": result.execution_quiesced,
        "agent_failures": result.agent_failures,
        "workflow_role_states": role_states,
        "workflow_role_observation_errors": observation_errors,
        "error": result.error,
    })


def workflow_model_settings(overrides=None) -> dict:
    """Read the native resolver fields omitted by the public metadata view."""
    values = dict(overrides or {})
    fields = ("context_window", "llm_max_retries", "provider_error_time_budget")
    selected = {key: values[key] for key in fields if values.get(key) is not None}
    config = OpenCollab(
        Path.cwd(), model=values.get("model"), provider=values.get("provider"), config=selected,
    ).configuration
    return {
        field: config.get(field) for field in ("context_window", "llm_max_retries", "provider_error_time_budget")
    }


def _role_states(trajectory_path: str | None) -> tuple[list[dict], list[dict]]:
    if not trajectory_path:
        return [], []
    directory = Path(trajectory_path).parent
    roles, errors = [], []
    paths = set(directory.glob("*.json"))
    paths.update(path.with_suffix("") for path in directory.glob("*.json.journal"))
    for path in sorted(paths):
        if re.fullmatch(r"\d+_.+\.json", path.name) is None:
            continue
        try:
            snapshot = OpenCollab.read_session_snapshot(path)
            state = snapshot.get("session_state") or {}
            if not isinstance(state, dict):
                raise ValueError("role session state is malformed")
            roles.append(
                {
                    "artifact": path.name,
                    "role": snapshot.get("role"),
                    "phase": state.get("phase"),
                    "terminal_reason": state.get("terminal_reason"),
                    "steps": state.get("step_count"),
                    "used_tokens": state.get("used_tokens"),
                    "pending_events": len(state.get("pending_events") or []),
                    "pending_external_user_turn": bool(state.get("pending_external_user_turn")),
                    "active_turn_start_message_index": state.get("active_turn_start_message_index"),
                }
            )
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"artifact": path.name, "error_type": type(exc).__name__})
    return roles, errors


def workflow_stop_metrics(result) -> dict:
    native = result.runtime_state or {}
    status = result.runtime_status
    reason = result.runtime_reason
    output = result.workflow_result if isinstance(result.workflow_result, dict) else {}
    output_status = output.get("status")
    roles, observation_errors = _role_states(result.trajectory_path)
    role_origins = []
    for failure in result.agent_failures:
        role_origins.append({"label": failure.get("label"), **classify_failure(record=failure)})
    all_role_failures_external = bool(role_origins) and all(
        role["origin"] == "provider_transport" for role in role_origins
    )
    lifecycle_exception = status is None and str(result.error or "").startswith(
        ("RunError:", "ProgrammaticLifecycleError:", "WorkflowLifecycleError:")
    )
    session_quiesced = result.execution_quiesced is True and not lifecycle_exception
    if lifecycle_exception:
        status = "failed"
        reason = result.error
    completed = status == "completed" and (output_status in {None, "done"} or workflow_candidate_delivered(
        result, role_states=roles, observation_errors=observation_errors
    ))
    # Older test and evaluator callers can omit the public runtime projection.
    if status is None:
        completed = not result.error and output_status in {None, "done"}
    attribution = native.get("failure_attribution") or {}
    storage_failure = attribution.get("basis") == "storage_exhausted" or any(
        role.get("basis") == "storage_exhausted" for role in role_origins
    )
    role_failures_handled = not observation_errors and handled_workflow_role_failures({
        "agent_failures": result.agent_failures, "workflow_role_states": roles,
        "workflow_result": output, "runtime_status": status, "execution_quiesced": session_quiesced,
    })
    delivered = (completed and bool(result.patch.strip()) and result.patch_produced is True
                 and role_failures_handled and result.submission_eligible is True and all(
        getattr(result, field) is True for field in (
            "execution_quiesced", "patch_extraction_succeeded", "injected_path_cleanup_proven",
            "harness_artifact_exclusion_proven", "checkpoint_restore_integrity_proven",
            "task_stage_integrity_proven",
        )
    ))
    persistence_failed = (attribution.get("basis") == "required_evidence_persistence_failed"
                          or persistence_failure(native.get("metrics")))
    if persistence_failed:
        origin = "evaluation_persistence"
    elif storage_failure and not delivered:
        origin = "evaluation_storage"
    elif not delivered and attribution.get("origin") == "evaluation_environment":
        origin = "evaluation_environment"
    elif not delivered and any(role["origin"] == "evaluation_environment" for role in role_origins):
        origin = "evaluation_environment"
    elif role_origins and not role_failures_handled:
        origin = "provider_transport" if all_role_failures_external else "oc" if any(
            role["origin"] == "oc" for role in role_origins
        ) else "unclassified"
    elif not session_quiesced:
        origin = "oc"
    elif completed:
        origin = "evaluation_adapter" if result.error else "none"
    elif status == "stopped" and reason == "timeout":
        origin = "evaluation_deadline"
    elif is_progress_stop(native):
        origin = "evaluation_no_progress"
    elif any(role["origin"] == "oc" for role in role_origins):
        origin = "oc"
    elif (native.get("failure_attribution") or {}).get("origin") in {"provider_transport", "unclassified", "oc"}:
        origin = native["failure_attribution"]["origin"]
    elif native.get("external_error") is True:
        origin = "unclassified"
    elif status == "completed" and output_status == "incomplete" and role_origins:
        origin = (
            "oc"
            if any(role["origin"] == "oc" for role in role_origins)
            else "provider_transport"
            if all_role_failures_external
            else "unclassified"
        )
    elif status in {"failed", "stopped"} or output_status not in {None, "done"}:
        origin = "oc"
    elif result.error:
        origin = "evaluation_adapter"
    else:
        origin = "none"
    invalid_terminal = (lifecycle_exception or not session_quiesced or status == "failed"
                        or output_status == "error" or bool(observation_errors)
                        or bool(role_origins) and not delivered)
    technical = invalid_terminal or origin not in {"none", "oc"} or (
        not delivered and (
            attribution.get("technical_failure") is True
            or any(role.get("technical_failure") is True for role in role_origins)
        )
    )
    usage_complete = native.get("usage_complete", False)
    observed_status, observed_reason = status, reason
    if status == "completed" and output_status == "error":
        observed_status = "stopped"
        observed_reason = reason or str(output.get("error") or "workflow error")
    return {
        "agent_status": observed_status,
        "agent_reason": observed_reason,
        "failure_exception_chain": native.get("error_chain", []),
        "failure_attribution": native.get("failure_attribution", {}),
        "used_tokens": result.tokens_used,
        "step_count": result.steps,
        "failure_origin": origin,
        "failure_phase": (
            "required_evidence_persistence" if persistence_failed
            else "storage_exhausted" if storage_failure and not delivered else "workflow_result"
        ),
        "technical_failure": technical,
        "oc_failure": origin == "oc" and not technical,
        "technical_interruption_recoverable": (
            origin in {"evaluation_storage", "evaluation_persistence"}
            or origin == "evaluation_no_progress"
            or origin == "provider_transport"
            and (
                native.get("external_error_retryable") is True
                or (
                    status == "completed"
                    and all_role_failures_external
                    and all(role["retryable"] for role in role_origins)
                )
            )
        ),
        "session_quiesced": session_quiesced,
        "original_workflow_status": output_status,
        "workflow_role_states": roles,
        "workflow_role_failure_origins": role_origins,
        "workflow_role_observation_errors": observation_errors,
        "agent_failures": list(result.agent_failures),
        **({"workflow_role_selection": {key: output.get(key) for key in (
            "status", "winner", "adopted", "adoption_attempts"
        )}}
           if result.agent_failures else {}),
        "workflow_role_failures_tolerated": completed and bool(result.agent_failures) and role_failures_handled,
        "usage_complete": usage_complete,
        "usage_status": "reported" if usage_complete else "result_tokens_unavailable",
        "used_tokens_lower_bound": usage_complete is False,
        "wall_clock_timeout": status == "stopped" and reason == "timeout",
    }
