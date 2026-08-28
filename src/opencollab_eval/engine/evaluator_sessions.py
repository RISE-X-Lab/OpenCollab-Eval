"""Evaluation runs implemented through OpenCollab's public Python facade."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencollab import OpenCollab, RunResult
from opencollab.tools import Tool

from opencollab_eval.engine.environment import ExecutionEnvironment
from opencollab_eval.engine.evidence_trace import (
    ORCHESTRATION_FILENAME,
    TRAJECTORY_FILENAME,
)
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

if TYPE_CHECKING:
    from opencollab_eval.engine.evaluator import EvalTask
    from opencollab_eval.engine.evidence_trace import EvidenceTrace

DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P: float | None = None
DEFAULT_THINKING = False
DEFAULT_THINKING_PARAMS = {"enable_thinking": True}
_CONTROLLED_STOP_REASONS = frozenset(
    {"budget_exceeded", "context_overflow", "step_limit_exceeded", "timeout"}
)


def _workflow_concurrency() -> int:
    raw = os.environ.get("OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY", "4")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("workflow concurrency must be an integer from 1 to 32") from exc
    if not 1 <= value <= 32:
        raise RuntimeError("workflow concurrency must be an integer from 1 to 32")
    return value


def _runtime_session_quiesced(result: RunResult[Any]) -> bool:
    metrics = result.metrics
    if "session_quiesced" in metrics:
        return metrics.get("session_quiesced") is True
    return metrics.get("execution_quiesced") is True


def _reserve_artifacts(run_dir: str | None) -> Path | None:
    if run_dir is None:
        return None
    parent = Path(run_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(32):
        candidate = parent / f"runtime-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not reserve evaluation runtime artifacts")


@dataclass(slots=True)
class _EvalRunRecord:
    """Session-shaped view consumed by the evaluator result builder."""

    result: RunResult[Any]
    workflow: bool = False

    @property
    def used_tokens(self) -> int:
        return int(self.result.tokens or 0)

    @property
    def step_count(self) -> int:
        return int(self.result.metrics.get("steps") or 0)

    @property
    def session_count(self) -> int:
        return int(self.result.metrics.get("sessions") or 1)

    @property
    def markup_recovered(self) -> int:
        return int(self.result.metrics.get("markup_recovered") or 0)

    @property
    def pending_cleanup_tasks(self) -> tuple[()]:
        return ()

    @property
    def persistence_errors(self) -> tuple[()]:
        return ()

    @property
    def sessions(self) -> tuple[_EvalRunRecord, ...]:
        return (self,) if self.workflow else ()

    @property
    def workflow_result(self) -> Any:
        return self.result.output

    @property
    def runtime_status(self) -> str:
        return self.result.status

    @property
    def runtime_reason(self) -> str | None:
        return self.result.reason

    @property
    def execution_quiesced(self) -> bool:
        return _runtime_session_quiesced(self.result)

    @property
    def workflow_error(self) -> str | None:
        if not self.execution_quiesced:
            return "OpenCollab session did not quiesce"
        if self.result.status == "completed":
            return None
        if (
            self.result.status == "stopped"
            and self.result.reason in _CONTROLLED_STOP_REASONS
        ):
            return None
        return self.result.reason or self.result.status

    @property
    def agent_failures(self) -> tuple[dict[str, Any], ...]:
        return self.result.agent_failures


def _client(
    *,
    env: ExecutionEnvironment,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    top_p: float | None,
    max_output_tokens: int,
    thinking: bool,
    thinking_params: dict | None,
    wire_protocol: str,
    reasoning_effort: str | None,
    llm_connect_timeout: float,
    llm_first_event_timeout: float,
    llm_stream_idle_timeout: float,
) -> OpenCollab:
    return OpenCollab(
        Path.cwd(),
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        config={
            "temperature": temperature,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "thinking": thinking,
            "thinking_params": (
                dict(DEFAULT_THINKING_PARAMS)
                if thinking_params is None
                else dict(thinking_params)
            ),
            "wire_protocol": wire_protocol,
            "reasoning_effort": reasoning_effort,
            "llm_connect_timeout": llm_connect_timeout,
            "llm_first_event_timeout": llm_first_event_timeout,
            "llm_stream_idle_timeout": llm_stream_idle_timeout,
        },
        environment=env,
    )


async def _run_single_session(
    *,
    task: EvalTask,
    env: ExecutionEnvironment,
    tracer: EvidenceTrace,
    prompt: str,
    tools: Sequence[Tool],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    wire_protocol: str = "chat_completions",
    reasoning_effort: str | None = None,
    llm_connect_timeout: float = 30.0,
    llm_first_event_timeout: float = 180.0,
    llm_stream_idle_timeout: float = 180.0,
    save_dir: str | None = None,
    **_unused: Any,
) -> _EvalRunRecord:
    """Run one task-bound public OpenCollab agent."""
    artifacts = _reserve_artifacts(save_dir)
    result = await _client(
        env=env,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params,
        wire_protocol=wire_protocol,
        reasoning_effort=reasoning_effort,
        llm_connect_timeout=llm_connect_timeout,
        llm_first_event_timeout=llm_first_event_timeout,
        llm_stream_idle_timeout=llm_stream_idle_timeout,
    ).agent(
        task.description,
        name="eval_agent",
        system_prompt=prompt,
        tools=tools,
        budget=task.max_tokens,
        max_steps=max_steps,
        timeout=task.timeout,
        artifacts=artifacts,
        trace=True,
    )
    if artifacts is not None:
        tracer.bind_artifacts(artifacts, filename=TRAJECTORY_FILENAME)
    return _EvalRunRecord(result)


async def _run_workflow_mode(
    *,
    task: EvalTask,
    env: ExecutionEnvironment,
    tracer: EvidenceTrace,
    prompt: str,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    workflow: Any,
    injected_paths: Sequence[str] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    wire_protocol: str = "chat_completions",
    reasoning_effort: str | None = None,
    llm_connect_timeout: float = 30.0,
    llm_first_event_timeout: float = 180.0,
    llm_stream_idle_timeout: float = 180.0,
    save_dir: str | None = None,
    **_unused: Any,
) -> _EvalRunRecord:
    """Run one task-bound workflow through the public OpenCollab facade."""
    args = dict(task.extras or {})
    args.pop("injected_test_paths", None)
    args.update({"task_id": task.task_id, "description": task.description})
    if injected_paths:
        args["injected_test_paths"] = list(injected_paths)
    artifacts = _reserve_artifacts(save_dir)
    result = await _client(
        env=env,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params,
        wire_protocol=wire_protocol,
        reasoning_effort=reasoning_effort,
        llm_connect_timeout=llm_connect_timeout,
        llm_first_event_timeout=llm_first_event_timeout,
        llm_stream_idle_timeout=llm_stream_idle_timeout,
    ).workflow(
        workflow,
        args,
        budget=task.max_tokens,
        concurrency=_workflow_concurrency(),
        timeout=task.timeout,
        max_steps=max_steps,
        system_prompt=prompt,
        artifacts=artifacts,
        trace=True,
    )
    if artifacts is not None:
        tracer.bind_artifacts(artifacts, filename=ORCHESTRATION_FILENAME)
    return _EvalRunRecord(result, workflow=True)


async def _run_team_mode(
    *,
    task: EvalTask,
    env: ExecutionEnvironment,
    tracer: EvidenceTrace,
    team_config: str | os.PathLike[str],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    wire_protocol: str = "chat_completions",
    reasoning_effort: str | None = None,
    llm_connect_timeout: float = 30.0,
    llm_first_event_timeout: float = 180.0,
    llm_stream_idle_timeout: float = 180.0,
    save_dir: str | None = None,
    **_unused: Any,
) -> _EvalRunRecord:
    """Run one task-bound team, with the order of work left to the model.

    The third regime beside a single session and a workflow, and the only one
    whose sequence of work is not decided by code. It is here so that a
    comparison against the workflow regime differs in that one respect and not
    in where the agents work, what tools they hold, or how they hand results
    over -- all of which are held equal by running both against the same task
    container through the same environment.

    Three settings are fixed rather than exposed. The roster is prebuilt, so
    which roles exist is an input to the run instead of something the model
    decides midway and the run has a declared topology to be judged against.
    Each teammate gets its own worktree, so a result reaches a teammate only
    through a channel the run records. Turns are serialized, so one shared
    budget is not granted twice over by two agents reading it at once. A run
    missing any of them still finishes and still looks ordinary, which is
    exactly why they are not left to a caller to remember.
    """
    artifacts = _reserve_artifacts(save_dir)
    result = await _client(
        env=env,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params,
        wire_protocol=wire_protocol,
        reasoning_effort=reasoning_effort,
        llm_connect_timeout=llm_connect_timeout,
        llm_first_event_timeout=llm_first_event_timeout,
        llm_stream_idle_timeout=llm_stream_idle_timeout,
    ).team(
        task.description,
        config=team_config,
        budget=task.max_tokens,
        timeout=task.timeout,
        artifacts=artifacts,
        trace=True,
        use_worktrees=True,
        prebuild_team=True,
        max_steps=max_steps,
        serialize_turns=True,
    )
    if artifacts is not None:
        tracer.bind_artifacts(artifacts, filename=TRAJECTORY_FILENAME)
    return _EvalRunRecord(result, workflow=True)


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "used_tokens", 0)) for session in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "step_count", 0)) for session in sessions)


def _aggregate_markup_recovery(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "markup_recovered", 0)) for session in sessions)
