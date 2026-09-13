"""Project native workflow outcomes into evaluation observations."""

from __future__ import annotations

import re
from pathlib import Path

from opencollab import OpenCollab

from opencollab_eval.engine.native_failure_attribution import classify_failure
from opencollab_eval.engine.native_progress_watch import is_progress_stop


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
    for path in sorted(directory.glob("*.json")):
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
    completed = status == "completed" and output_status in {None, "done"}
    # Older test and evaluator callers can omit the public runtime projection.
    if status is None:
        completed = not result.error and output_status in {None, "done"}
    if not session_quiesced:
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
    roles, observation_errors = _role_states(result.trajectory_path)
    usage_complete = native.get("usage_complete", False)
    return {
        "agent_status": status,
        "agent_reason": reason,
        "failure_exception_chain": native.get("error_chain", []),
        "failure_attribution": native.get("failure_attribution", {}),
        "used_tokens": result.tokens_used,
        "step_count": result.steps,
        "failure_origin": origin,
        "failure_phase": "workflow_result",
        "technical_failure": origin not in {"none", "oc"},
        "oc_failure": origin == "oc",
        "technical_interruption_recoverable": (
            origin == "evaluation_no_progress"
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
        "workflow_role_failures_tolerated": completed and bool(result.agent_failures),
        "usage_complete": usage_complete,
        "usage_status": "reported" if usage_complete else "result_tokens_unavailable",
        "used_tokens_lower_bound": usage_complete is False,
        "wall_clock_timeout": status == "stopped" and reason == "timeout",
    }
