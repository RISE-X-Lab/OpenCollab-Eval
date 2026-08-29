"""Agent execution, instance loading, and bounded patch extraction."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from opencollab import OpenCollab, RunResult
from opencollab.environments import attach_container, build_repo_map_via_env
from opencollab.tools import builtin_tools

from opencollab_eval.engine.swe_eval_records import read_bounded_json
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

from .gen_prediction_config import validate_instance_id
from .gen_prediction_constants import (
    _ACTIVATE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    DOCKER_WORKDIR,
    MAX_INSTANCE_BYTES,
    WORKING_TOOL_NAMES,
)
from .gen_prediction_run_summary import RUN_SUMMARY_KEY, build_run_summary
from .gen_prediction_task_text import (
    BLIND_VALIDATION_BLOCK,
    append_repository_layout,
    compose_shared_task,
)


def build_task(instance: dict) -> str:
    """The shared task text plus this path's grading disclosure: none.

    This path is blind by construction -- there is no code here that can name a
    sealed field, which ``tests/test_boundaries.py`` pins -- so the block it
    appends is the constant notice, not a decision. The workflow path reaches
    the same text through a run-time check.
    """
    return compose_shared_task(instance) + BLIND_VALIDATION_BLOCK


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


def _runtime_failure_metrics(exc: Exception, duration_s: float) -> dict[str, Any]:
    return {
        RUN_SUMMARY_KEY: build_run_summary(
            steps=0,
            tokens=0,
            status="failed",
            reason=type(exc).__name__,
            duration_s=duration_s,
            error=str(exc),
        ),
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


def _result_metrics(result: RunResult[str], duration_s: float) -> dict[str, Any]:
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
    # Whether the workspace is worth reading, and deliberately not a function of
    # *why* a quiesced session stopped.
    #
    # It used to also require ``workflow_status in {"done",
    # "done_with_timeout_patch"}``. A wall-clock timeout maps to the second of
    # those, so a run that ran out of time kept its patch -- but a run that ran
    # out of tokens maps to the raw stop reason, matched neither, and had its
    # work thrown away with the warning "empty patch (agent made no tracked
    # changes)". Observed on django-11292: the agent's own ``git diff --stat``
    # four steps before the end reported 2 files and 15 insertions still in the
    # tree.
    #
    # The arm this one is compared against never behaved that way. The
    # workflow/team path gates extraction on the container evidence being intact
    # (``gen_prediction_workflow`` around ``outer_extraction_allowed``) and not
    # on the terminal reason, so an identically budget-stopped team run kept its
    # patch. Two arms, one outcome measure, and a run's patch survived on one of
    # them and not the other for a reason that is not what the comparison is
    # about.
    #
    # ``stopped`` is every controlled halt -- budget, step ceiling, loop block,
    # cancel, context overflow -- and in all of them the agent's edits are
    # sitting in /testbed exactly as they are after a timeout. ``failed`` is
    # still excluded: an unhandled fault leaves no promise about the workspace.
    candidate_probe_eligible = session_quiesced and result.status in {
        "completed",
        "stopped",
    }
    metrics = {
        RUN_SUMMARY_KEY: build_run_summary(
            steps=int(values.get("steps") or 0),
            tokens=int(result.tokens or 0),
            status=result.status,
            reason=result.reason,
            duration_s=duration_s,
            error=result.error if result.error is None else str(result.error),
        ),
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
    # Asked of the container, not walked here: the directory this process could
    # walk is the one the run was launched from, and the agent never sees it.
    task = append_repository_layout(task, await build_repo_map_via_env(environment))
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
    started = time.monotonic()
    try:
        result = await client.agent(
            task,
            name="swe_agent",
            system_prompt=AGENT_PROMPT.strip(),
            tools=builtin_tools(*WORKING_TOOL_NAMES, headless=True),
            budget=budget,
            max_steps=max_steps,
            timeout=timeout,
            cleanup_timeout=AGENT_CANCELLATION_GRACE_SECONDS,
            artifacts=artifact_dir,
            trace=True,
        )
    except Exception as exc:
        print(f"  agent: runtime failed with {type(exc).__name__}: {exc}")
        return _runtime_failure_metrics(exc, time.monotonic() - started)

    metrics = _result_metrics(result, time.monotonic() - started)
    print(
        f"  agent: steps={metrics['step_count']} "
        f"tokens={metrics['used_tokens']}"
    )
    return metrics
