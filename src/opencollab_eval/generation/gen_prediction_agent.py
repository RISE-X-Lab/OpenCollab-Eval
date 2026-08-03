"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from opencollab import OpenCollab, RunResult
from opencollab.environments import attach_container
from opencollab.tools import builtin_tools

from opencollab_eval.benchmarks.task_specification import (
    compose_task_specification,
)
from opencollab_eval.engine.provider_failures import (
    summarize_terminal_provider_failures,
)
from opencollab_eval.engine.swe_eval_records import open_regular_binary, read_bounded_json
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

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
        workflow_status = str(result.reason or phase)
    else:
        workflow_status = "error"
    candidate_probe_eligible = (
        session_quiesced
        and result.status in {"completed", "stopped"}
        and workflow_status in {"done", "done_with_timeout_patch"}
    )
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
        "agent_failures": [dict(item) for item in result.agent_failures],
    }
    error = result.error
    if not session_quiesced:
        metrics["error_type"] = "SessionNotQuiesced"
        metrics["error"] = "agent session remained active after bounded cleanup"
    elif result.status == "failed":
        metrics["error_type"] = type(error).__name__ if error else "AgentRunError"
        metrics["error"] = str(error or result.reason or "agent execution failed")
    return metrics


def verified_llm_calls(
    trajectory_path: str | Path | None,
    *,
    artifact_root: Path,
    expected_model: str,
    expected_reasoning_effort: str | None,
    wire_protocol: str,
) -> tuple[list[str], list[str], str, int]:
    """Bind a generated candidate to calls in its controller-owned trace."""
    if wire_protocol not in {"chat_completions", "responses"}:
        raise RuntimeError(f"Unsupported trajectory wire protocol {wire_protocol!r}")
    if not trajectory_path:
        raise RuntimeError("LLM execution did not produce a trajectory")
    path = Path(trajectory_path)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(artifact_root.resolve(strict=True)):
            raise RuntimeError("LLM trajectory is outside the current artifact root")
        with open_regular_binary(path) as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read(16 * 1024 * 1024 + 1)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("LLM trajectory changed while reading")
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("LLM trajectory exceeds 16 MiB")
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("LLM trajectory cannot be read") from exc
    requested_models: list[str] = []
    provider_models: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM trajectory contains invalid JSON") from exc
        if not isinstance(record, dict) or record.get("type") != "llm_call":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("LLM call is missing its payload")
        if payload.get("wire_protocol") != wire_protocol:
            raise RuntimeError("LLM trajectory contains a mixed wire protocol")
        requested = payload.get("model")
        if requested != expected_model:
            raise RuntimeError(
                f"LLM requested model mismatch expected {expected_model!r} got {requested!r}"
            )
        observed = payload.get("provider_model")
        if observed != expected_model:
            raise RuntimeError(
                f"LLM provider model mismatch expected {expected_model!r} got {observed!r}"
            )
        observed_effort = payload.get("reasoning_effort")
        if wire_protocol == "responses" and observed_effort != expected_reasoning_effort:
            raise RuntimeError(
                "LLM reasoning effort mismatch "
                f"expected {expected_reasoning_effort!r} got {observed_effort!r}"
            )
        requested_models.append(requested)
        provider_models.append(observed)
    if not requested_models:
        raise RuntimeError("LLM trajectory contains no verified LLM call")
    return (
        sorted(set(requested_models)),
        sorted(set(provider_models)),
        hashlib.sha256(raw).hexdigest(),
        len(requested_models),
    )


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
    provider_failure = summarize_terminal_provider_failures(result.agent_failures)
    if provider_failure is not None:
        metrics["provider_failure"] = provider_failure
    wire_protocol = cfg.get("wire_protocol", "chat_completions")
    try:
        (
            trajectory_models,
            provider_models,
            trajectory_sha256,
            trajectory_llm_call_count,
        ) = verified_llm_calls(
            artifact_dir / "trajectory.jsonl",
            artifact_root=artifact_dir,
            expected_model=cfg["model"],
            expected_reasoning_effort=cfg.get("reasoning_effort"),
            wire_protocol=wire_protocol,
        )
    except RuntimeError as exc:
        trajectory_models = []
        provider_models = []
        trajectory_sha256 = None
        trajectory_llm_call_count = 0
        if metrics["candidate_probe_eligible"]:
            metrics.update(
                workflow_status="error",
                candidate_probe_eligible=False,
                error_type="TrajectoryIdentityError",
                error=str(exc),
            )
    metrics.update(
        trajectory_models=trajectory_models,
        provider_models=provider_models,
        trajectory_sha256=trajectory_sha256,
        trajectory_llm_call_count=trajectory_llm_call_count,
        wire_protocol=wire_protocol,
    )
    if provider_failure is not None:
        metrics.update(
            workflow_status="provider_request_rejected",
            candidate_probe_eligible=False,
            submission_eligible=False,
        )
    print(
        f"  agent: steps={metrics['step_count']} "
        f"tokens={metrics['used_tokens']}"
    )
    return metrics
