"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

from pathlib import Path

from opencollab.sdk.environments import DockerEnvironment
from opencollab.sdk.errors import AgentRunLifecycleError
from opencollab.sdk.models import AgentRunBudget, AgentRunRequest, RuntimeConfig
from opencollab.sdk.persistence import reserve_run_directory
from opencollab.sdk.runtime import OpenCollabRuntime
from opencollab.sdk.tools import (
    BashTool,
    FileReadTool,
    FileWriteTool,
    GrepTool,
)
from opencollab.sdk.usage import DEFAULT_MAX_OUTPUT_TOKENS

from opencollab_eval.engine.swe_eval_records import read_bounded_json

from .gen_prediction_config import validate_instance_id
from .gen_prediction_constants import (
    _ACTIVATE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    DOCKER_WORKDIR,
    MAX_INSTANCE_BYTES,
)


def build_task(instance: dict) -> str:
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{instance['problem_statement']}\n\n"
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


_FAILED_AGENT_PHASES = frozenset(
    {
        "cancelled",
        "budget_exceeded",
        "step_limit_exceeded",
        "context_overflow",
        "error",
    }
)


def _runtime_failure_metrics(exc: Exception) -> dict:
    return {
        "workflow_status": "error",
        "session_phase": "error",
        "step_count": 0,
        "used_tokens": 0,
        "wall_clock_timeout": False,
        "execution_quiesced": False,
        "submission_eligible": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


async def run_agent(
    task: str,
    cid: str,
    cfg: dict,
    max_steps: int,
    budget: int,
    timeout: float,
    *,
    artifact_root: str | Path,
    runtime: OpenCollabRuntime | None = None,
) -> dict:
    env = DockerEnvironment(
        container_id=cid,
        workspace=DOCKER_WORKDIR,
        exec_workdir=DOCKER_WORKDIR,
        command_prefix=_ACTIVATE,
        timeout_returncode=124,
    )
    artifact_dir = Path(reserve_run_directory(str(artifact_root)))
    request = AgentRunRequest(
        prompt=task,
        name="swe_agent",
        system_prompt=AGENT_PROMPT.strip(),
        tools=(BashTool(), FileReadTool(), FileWriteTool(), GrepTool()),
        config=RuntimeConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg.get("api_key"),
            base_url=cfg.get("base_url"),
            llm_timeout_seconds=cfg.get("llm_timeout", 600.0),
            temperature=cfg.get("temperature", 0.0),
            top_p=cfg.get("top_p"),
            max_output_tokens=cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            thinking=cfg.get("thinking", False),
            thinking_params=cfg.get("thinking_params") or {},
        ),
        budget=AgentRunBudget(
            max_tokens=budget,
            max_steps=max_steps,
            timeout_seconds=timeout,
            cleanup_timeout_seconds=AGENT_CANCELLATION_GRACE_SECONDS,
        ),
        environment=env,
        environment_workdir=DOCKER_WORKDIR,
        source_root=DOCKER_WORKDIR,
        artifact_dir=artifact_dir,
        trace=True,
        failure_mode="return",
    )
    print(f"  agent artifacts: {artifact_dir}")
    try:
        result = await (runtime or OpenCollabRuntime()).run_agent(request)
    except AgentRunLifecycleError as exc:
        print(f"  agent: lifecycle failed with {type(exc).__name__}: {exc}")
        return _runtime_failure_metrics(exc)
    except Exception as exc:
        print(f"  agent: runtime call failed with {type(exc).__name__}: {exc}")
        return _runtime_failure_metrics(exc)

    step_count = int(result.step_count)
    used_tokens = int(result.tokens_spent)
    print(f"  agent: steps={step_count} tokens={used_tokens}")
    phase_value = result.phase or "error"
    execution_quiesced = bool(result.cleanup_quiesced)
    timed_out = result.outcome == "timed_out"
    if timed_out and execution_quiesced:
        workflow_status = "done_with_timeout_patch"
    elif not execution_quiesced:
        workflow_status = "error"
    elif result.outcome == "failed":
        workflow_status = phase_value if phase_value in _FAILED_AGENT_PHASES else "error"
    elif result.outcome == "completed" and phase_value == "done":
        workflow_status = "done"
    else:
        workflow_status = phase_value
    metrics = {
        "workflow_status": workflow_status,
        "session_phase": phase_value,
        "step_count": step_count,
        "used_tokens": used_tokens,
        "wall_clock_timeout": timed_out,
        "execution_quiesced": execution_quiesced,
        "submission_eligible": (
            execution_quiesced
            and result.outcome in {"completed", "timed_out"}
            and workflow_status in {"done", "done_with_timeout_patch"}
        ),
    }
    if not execution_quiesced:
        metrics["error_type"] = "ExecutionNotQuiesced"
        metrics["error"] = "agent execution remained active after bounded cancellation cleanup"
    if result.outcome == "failed":
        metrics["error_type"] = result.error_type or "AgentRunError"
        metrics["error"] = result.error_message or result.terminal_reason or "agent execution failed"
    return metrics
