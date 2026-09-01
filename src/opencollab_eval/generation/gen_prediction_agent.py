"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from opencollab import OpenCollab, RunResult
from opencollab.environments import attach_container
from opencollab.tools import builtin_tools

from opencollab_eval.benchmarks.task_specification import (
    compose_task_specification,
)
from opencollab_eval.engine.swe_eval_records import read_bounded_json
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

from .gen_prediction_config import validate_instance_id
from .gen_prediction_constants import (
    _ACTIVATE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    DOCKER_WORKDIR,
    MAX_INSTANCE_BYTES,
)

_CONTROLLED_STOP_REASON_PREFIXES = (
    "budget exceeded:",
    "budget exceeded after model call:",
    "budget exhausted before model call:",
    "team budget exceeded:",
    "step limit reached:",
    "context overflow:",
)
_CONTROLLED_STOP_REASON_NAMES = frozenset(
    {"budget_exceeded", "context_overflow", "step_limit_exceeded", "timeout"}
)


def build_task(instance: dict) -> str:
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{compose_task_specification(instance)}\n\n"
        "Locate the root cause in the source, apply a minimal fix, and ensure the "
        "publicly described behavior is satisfied."
    )


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


def _runtime_failure_metrics(exc: Exception) -> dict[str, Any]:
    return {
        "workflow_status": "error",
        "session_phase": "error",
        "step_count": 0,
        "used_tokens": 0,
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
    if normalized in _CONTROLLED_STOP_REASON_NAMES or normalized.startswith(
        _CONTROLLED_STOP_REASON_PREFIXES
    ):
        return "done_with_timeout_patch"
    return None


def _result_metrics(result: RunResult[str]) -> dict[str, Any]:
    values = result.metrics
    if "session_quiesced" in values:
        session_quiesced = values.get("session_quiesced") is True
    else:
        session_quiesced = values.get("execution_quiesced") is True
    phase = str(values.get("phase") or result.status)
    timed_out = result.status == "stopped" and result.reason == "timeout"
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
    # A controlled stop can happen after the agent has already edited the
    # workspace.  Once the session is quiescent, the workspace probe and
    # trusted extraction below are safe regardless of whether the stop was
    # caused by the token budget, step ceiling, context overflow, or timeout.
    # Unhandled failures remain excluded because they provide no workspace
    # integrity guarantee.
    candidate_probe_eligible = session_quiesced and result.status in {
        "completed",
        "stopped",
    }
    metrics = {
        "workflow_status": workflow_status,
        "session_phase": phase,
        "step_count": int(values.get("steps") or 0),
        "used_tokens": int(result.tokens or 0),
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
    environment = attach_container(
        container_id=cid,
        workspace=DOCKER_WORKDIR,
        command_prefix=_ACTIVATE,
        timeout_returncode=124,
    )
    artifact_dir = Path(reserve_run_directory(artifact_root))
    client = runtime or OpenCollab(
        Path.cwd(),
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
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
            "llm_connect_timeout": cfg.get("llm_connect_timeout", 30.0),
            "llm_first_event_timeout": cfg.get("llm_first_event_timeout", 180.0),
            "llm_stream_idle_timeout": cfg.get("llm_stream_idle_timeout", 180.0),
        },
        environment=environment,
    )
    print(f"  agent artifacts: {artifact_dir}")
    try:
        result = await client.agent(
            task,
            name="swe_agent",
            system_prompt=AGENT_PROMPT.strip(),
            tools=builtin_tools(
                "bash",
                "file_read",
                "file_write",
                "grep",
                headless=True,
            ),
            budget=budget,
            max_steps=max_steps,
            timeout=timeout,
            cleanup_timeout=AGENT_CANCELLATION_GRACE_SECONDS,
            artifacts=artifact_dir,
            trace=True,
        )
    except Exception as exc:
        print(f"  agent: runtime failed with {type(exc).__name__}: {exc}")
        return _runtime_failure_metrics(exc)

    metrics = _result_metrics(result)
    print(
        f"  agent: steps={metrics['step_count']} "
        f"tokens={metrics['used_tokens']}"
    )
    return metrics
