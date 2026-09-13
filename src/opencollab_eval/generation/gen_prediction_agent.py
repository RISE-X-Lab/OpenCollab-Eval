"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from opencollab import OpenCollab, RunError, RunResult
from opencollab.environments import attach_container

from opencollab_eval.benchmarks.task_specification import (
    compose_task_specification,
)
from opencollab_eval.engine.native_failure_attribution import classify_failure
from opencollab_eval.engine.swe_eval_records import read_bounded_json
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

from .candidate_environment import image_activation_prefix
from .configured_model import ConfiguredModel
from .gen_prediction_config import validate_instance_id
from .gen_prediction_constants import (
    _ACTIVATE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    DOCKER_WORKDIR,
    MAX_INSTANCE_BYTES,
)

_CONTROLLED_STOP_REASON_PREFIXES = (
    "budget exceeded:",
    "budget exceeded after model call:",
    "budget exhausted before model call:",
    "team budget exceeded:",
    "budget reserve exhausted:",
    "step limit reached:",
    "context overflow:",
    "output truncated:",
)
_CONTROLLED_STOP_REASON_NAMES = frozenset({"budget_exceeded", "context_overflow", "step_limit_exceeded", "timeout"})


def build_task(instance: dict) -> str:
    return f"# Issue to fix in `{instance['repo']}`\n\n{compose_task_specification(instance)}\n"


def load_instance(path: str | Path) -> dict:
    document = read_bounded_json(Path(path), max_bytes=MAX_INSTANCE_BYTES)
    if document is None or not isinstance(document[0], dict):
        raise ValueError(f"instance input is not a bounded regular JSON object: {path}")
    instance = document[0]
    instance["instance_id"] = validate_instance_id(instance.get("instance_id"))
    return instance


def reserve_run_directory(root: str | Path) -> str:
    """Reserve one new artifact directory without consulting shared state."""
    parent = Path(root).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(32):
        candidate = parent / f"agent-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return str(candidate)
    raise FileExistsError("could not reserve a unique agent artifact directory")


def _technical_interruption_recoverable(error: BaseException | None) -> bool:
    if not isinstance(error, Exception):
        return False
    provider_or_transport = (
        isinstance(error, OSError)
        or type(error).__module__.startswith(("openai", "httpx", "httpcore", "aiohttp"))
        or type(error).__name__ in {"TransientProviderError", "APIError", "APIConnectionError", "APITimeoutError"}
    )
    return provider_or_transport and classify_failure(error=error).get("retryable") is True


def _runtime_failure_metrics(exc: Exception, *, phase: str = "public_api_execution") -> dict[str, Any]:
    chain = []
    current: BaseException | None = exc
    seen: set[int] = set()
    oc_lifecycle = False
    provider_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kind, module = type(current).__name__, type(current).__module__
        chain.append({"type": kind, "module": module})
        oc_lifecycle = (
            oc_lifecycle
            or isinstance(current, RunError)
            or (
                module.startswith("opencollab.")
                and kind in {"ProgrammaticLifecycleError", "AgentRuntimeLifecycleError"}
            )
        )
        provider_failure = provider_failure or module.startswith(("openai", "httpx", "httpcore", "aiohttp"))
        current = current.__cause__ or current.__context__
    origin = (
        "evaluation_adapter"
        if phase == "adapter_setup"
        else "oc"
        if oc_lifecycle
        else "provider_transport"
        if provider_failure
        else "public_api_exception_unclassified"
    )
    return {
        "workflow_status": "error",
        "failure_origin": origin,
        "failure_phase": phase,
        "failure_exception_chain": chain,
        "technical_failure": origin != "oc",
        "oc_failure": origin == "oc",
        "agent_status": "failed",
        "agent_reason": str(exc),
        "technical_interruption_recoverable": _technical_interruption_recoverable(exc),
        "session_phase": "error",
        "step_count": 0,
        "used_tokens": 0,
        "usage_complete": False,
        "used_tokens_lower_bound": True,
        "usage_status": "unavailable_after_public_api_exception",
        "wall_clock_timeout": False,
        "session_quiesced": False,
        "execution_quiesced": False,
        "candidate_probe_eligible": False,
        "submission_eligible": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _controlled_stop_status(reason: object) -> str | None:
    """Return the stable timeout disposition for a bounded stop reason.

    OpenCollab keeps useful counters in these reasons (for example, ``"step
    limit reached: 4 steps"``), while generation records need one stable
    status for the existing return-code contract.  The original reason stays
    in ``session_phase`` and the runtime trace; this helper only classifies
    known controlled-stop prefixes and never treats cancellation or failures as
    a successful stop.
    """
    if not isinstance(reason, str):
        return None
    normalized = reason.strip().lower()
    if normalized in _CONTROLLED_STOP_REASON_NAMES or normalized.startswith(_CONTROLLED_STOP_REASON_PREFIXES):
        return "done_with_timeout_patch"
    return None


def _result_metrics(result: RunResult[str]) -> dict[str, Any]:
    values = result.metrics
    if "session_quiesced" in values:
        session_quiesced = values.get("session_quiesced") is True
    else:
        session_quiesced = values.get("execution_quiesced") is True
    phase = str(values.get("phase") or result.status)
    timed_out = result.status == "stopped" and result.reason == "timeout" and values.get("outcome") != "completed"
    if not session_quiesced:
        workflow_status = "error"
    elif timed_out:
        workflow_status = "done_with_timeout_patch"
    elif result.status == "completed":
        workflow_status = "done"
    elif result.status == "stopped":
        # Keep the runtime's detailed terminal reason in the diagnostic status
        # until trusted extraction has succeeded.  The output layer applies
        # ``_controlled_stop_status`` only after its proof gate, so a stopped
        # run without a valid patch cannot look like a completed candidate.
        workflow_status = str(result.reason or phase)
    else:
        workflow_status = "error"
    candidate_probe_eligible = session_quiesced and result.status in {"completed", "stopped", "failed"}
    provider_failure = isinstance(result.error, Exception) and (
        isinstance(result.error, OSError)
        or type(result.error).__module__.startswith(("openai", "httpx", "httpcore", "aiohttp"))
        or type(result.error).__name__
        in {"TransientProviderError", "APIError", "APIConnectionError", "APITimeoutError"}
    )
    failure_origin = (
        "oc"
        if not session_quiesced
        else ("evaluation_deadline" if values.get("outcome") == "timed_out" else "timeout_origin_unresolved")
        if timed_out
        else "provider_transport"
        if provider_failure
        else "oc"
        if result.status in {"stopped", "failed"}
        else "none"
    )
    metrics = {
        "workflow_status": workflow_status,
        "failure_origin": failure_origin,
        "failure_phase": phase if session_quiesced else "public_result_not_quiesced",
        "technical_failure": failure_origin not in {"none", "oc"},
        "oc_failure": not session_quiesced
        or (
            result.status in {"stopped", "failed"}
            and not provider_failure
            and (not timed_out or values.get("outcome") == "completed")
        ),
        "runtime_outcome": values.get("outcome"),
        "agent_status": result.status,
        "agent_reason": result.reason,
        "technical_interruption_recoverable": (_technical_interruption_recoverable(result.error)),
        "session_phase": phase,
        "step_count": int(values.get("steps") or 0),
        "used_tokens": int(result.tokens or 0),
        "usage_complete": result.tokens is not None,
        "used_tokens_lower_bound": result.tokens is None,
        "usage_status": "reported" if result.tokens is not None else "result_tokens_unavailable",
        "wall_clock_timeout": timed_out,
        "session_quiesced": session_quiesced,
        "execution_quiesced": False,
        "candidate_probe_eligible": candidate_probe_eligible,
        "submission_eligible": False,
    }
    error = result.error
    if not session_quiesced:
        metrics["error_type"] = "SessionNotQuiesced"
        metrics["error"] = "agent session remained active after bounded cleanup"
    elif result.status == "failed":
        metrics["error_type"] = type(error).__name__ if error else "AgentRunError"
        metrics["error"] = str(error or result.reason or "agent execution failed")
    return metrics


async def run_agent(
    task: str,
    cid: str,
    cfg: dict,
    max_steps: int,
    budget: int,
    timeout: float,
    *,
    artifact_root: str | Path,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Run one agent through the public OpenCollab facade."""
    owned_model = None
    try:
        model_api_key = cfg.get("api_key") or os.environ.get("OPENCOLLAB_API_KEY")
        model_base_url = cfg.get("base_url") or os.environ.get("OPENCOLLAB_BASE_URL")
        # The public configuration view omits these effective model settings.
        # Keep non-secret values visible to the caller's persisted metrics.
        for field, variable, convert, default in (
            ("context_window", "OPENCOLLAB_CONTEXT_WINDOW", int, None),
            ("llm_max_retries", "OPENCOLLAB_LLM_MAX_RETRIES", int, 3),
            ("provider_error_time_budget", "OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET", float, 0.0),
        ):
            if cfg.get(field) is None:
                raw = os.environ.get(variable)
                cfg[field] = convert(raw) if raw is not None else default
        environment = attach_container(
            container_id=cid,
            workspace=DOCKER_WORKDIR,
            command_prefix=image_activation_prefix(cid, _ACTIVATE) if runtime is None else _ACTIVATE,
            timeout_returncode=124,
        )
        artifact_dir = Path(reserve_run_directory(artifact_root))
        client = runtime or OpenCollab(
            Path.cwd(),
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=model_api_key,
            base_url=model_base_url,
            config={
                "llm_timeout": cfg.get("llm_timeout", 600.0),
                "temperature": cfg.get("temperature", 0.0),
                "top_p": cfg.get("top_p"),
                "max_output_tokens": cfg.get(
                    "max_output_tokens",
                    DEFAULT_MAX_OUTPUT_TOKENS,
                ),
                "thinking": cfg.get("thinking", False),
                "thinking_params": cfg.get("thinking_params") or {},
                "wire_protocol": cfg.get("wire_protocol", "chat_completions"),
                "reasoning_effort": cfg.get("reasoning_effort"),
                "context_window": cfg.get("context_window"),
                "llm_connect_timeout": cfg.get("llm_connect_timeout", 30.0),
                "llm_first_event_timeout": cfg.get("llm_first_event_timeout", 180.0),
                "llm_stream_idle_timeout": cfg.get("llm_stream_idle_timeout", 180.0),
                "llm_max_retries": cfg.get("llm_max_retries", 3),
                "provider_error_time_budget": cfg.get("provider_error_time_budget", 0.0),
            },
            environment=environment,
        )
        create_model = getattr(client, "create_model_client", None)
        if runtime is None and callable(create_model):
            native_model = create_model()
            owned_model = ConfiguredModel(
                native_model,
                reasoning_effort=cfg.get("reasoning_effort"),
                observations=artifact_dir / "model-observations.jsonl",
            )
        print(f"  agent artifacts: {artifact_dir}")
    except Exception as exc:
        return _runtime_failure_metrics(exc, phase="adapter_setup")

    model_close_error = None
    try:
        result = await client.agent(
            task,
            name="swe_agent",
            tools="coding",
            budget=budget,
            max_steps=max_steps,
            timeout=timeout,
            cleanup_timeout=AGENT_CANCELLATION_GRACE_SECONDS,
            artifacts=artifact_dir,
            trace=True,
            **({"llm": owned_model} if owned_model is not None else {}),
        )
        metrics = _result_metrics(result)
    except Exception as exc:
        print(f"  agent: runtime failed with {type(exc).__name__}: {exc}")
        metrics = _runtime_failure_metrics(exc)
    finally:
        if owned_model is not None:
            try:
                await owned_model.close()
            except Exception as exc:
                model_close_error = {"type": type(exc).__name__, "message": str(exc)}

    if owned_model is not None:
        metrics["model_observations"] = str(owned_model.observations)
        metrics["model_observation_errors"] = list(owned_model.observation_errors)
    if model_close_error is not None:
        metrics["model_transport_cleanup_error"] = model_close_error
    metrics["evaluation_model_configuration"] = {
        "public_interface": "OpenCollab.create_model_client + OpenCollab.agent(llm=...)",
        "client": "native LLMClient with configured reasoning default",
        "model": cfg["model"],
        "wire_protocol": cfg.get("wire_protocol"),
        "reasoning_effort": cfg.get("reasoning_effort"),
        "context_window": cfg.get("context_window"),
        "max_output_tokens": cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
    }
    print(f"  agent: steps={metrics['step_count']} tokens={metrics['used_tokens']}")
    return metrics
