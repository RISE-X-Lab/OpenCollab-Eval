"""Evaluation runs implemented through OpenCollab's public Python facade."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencollab import OpenCollab, RunResult
from opencollab.tools import Tool

from opencollab_eval.engine.environment import ExecutionEnvironment
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
        return self.result.metrics.get("execution_quiesced") is True

    @property
    def workflow_error(self) -> str | None:
        if self.result.metrics.get("execution_quiesced") is not True:
            return "OpenCollab execution did not quiesce"
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
        tracer.bind_artifacts(artifacts, workflow=False)
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
    ).workflow(
        workflow,
        args,
        budget=task.max_tokens,
        timeout=task.timeout,
        max_steps=max_steps,
        system_prompt=prompt,
        artifacts=artifacts,
        trace=True,
    )
    if artifacts is not None:
        tracer.bind_artifacts(artifacts, workflow=True)
    return _EvalRunRecord(result, workflow=True)


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "used_tokens", 0)) for session in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "step_count", 0)) for session in sessions)


def _aggregate_markup_recovery(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(session, "markup_recovered", 0)) for session in sessions)
