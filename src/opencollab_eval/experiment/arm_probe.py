"""Run each arm's own entry code far enough to record what reaches the model.

Nothing here recomputes a constant. The point of an alignment check is to catch
the case where the code does something other than what the constants say, so a
check that re-reads the constants proves only that the check copied them
correctly. Every value this module reports is taken out of a call the arm's own
code made: the single arm is driven through ``gen_prediction_agent.run_agent``,
and the three orchestrated arms through
``evaluator_task_execution.run_session_or_workflow``, which is the function that
decides -- differently per mode -- what system prompt an agent is seated with.

Three things are replaced, and none of them is an audited input:

* the container. ``attach_container`` would need Docker; a stand-in answers the
  two shell commands the repository-map builders run, so both builders run for
  real against the same tree.
* the LLM client. ``evaluator_sessions._client`` builds the OpenCollab facade;
  a recorder stands in its place and captures the arguments each mode passes to
  ``.agent()``, ``.workflow()`` and ``.team()`` -- which is exactly the boundary
  the arms are supposed to agree at.
* the model's replies. A workflow arm is scripted through one clean round with
  schema-shaped replies, so the seats it opens and the tools it opens them with
  are the run's, not a table's.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opencollab import RunResult

from opencollab_eval.engine import evaluator as _evaluator
from opencollab_eval.engine import evaluator_sessions as _sessions
from opencollab_eval.engine.evaluator import EvalTask
from opencollab_eval.engine.evaluator_task_execution import (
    ExecutionConfig,
    ExecutionState,
    StageController,
    run_session_or_workflow,
)
from opencollab_eval.generation import gen_prediction_agent as _agent
from opencollab_eval.generation.gen_prediction_constants import (
    AGENT_PROMPT,
    WORKFLOW_AGENT_PROMPT,
)

#: A tree the two repository-map builders can both be asked about. Deep enough
#: that the depth-first ordering in ``build_repo_map_via_env`` has something to
#: order, and long enough that the 512-byte listing in
#: ``evaluator.build_repository_map`` has to cut.
PROBE_TREE: tuple[str, ...] = tuple(
    ["pkg", "tests", "README.md"]
    + [f"pkg/module_{index:02d}.py" for index in range(40)]
    + [f"tests/test_module_{index:02d}.py" for index in range(40)]
)

PROBE_INSTANCE: dict[str, Any] = {
    "instance_id": "acme__widget-42",
    "base_commit": "a" * 40,
    "repo": "acme/widget",
    "problem_statement": "Widget explodes on empty input.",
    "requirements": "Empty input must return an empty widget.",
    "interface": "parse_widget(text: str) -> Widget",
    "hints_text": "look at parse()",
    "FAIL_TO_PASS": '["tests/test_widget.py::test_empty"]',
    "test_patch": "diff --git a/tests/test_widget.py b/tests/test_widget.py\n",
}


@dataclass(frozen=True)
class SeatCall:
    """One session an arm opened, and what it opened it with."""

    label: str
    tools: tuple[str, ...]
    budget: int | None


@dataclass
class ArmObservation:
    """What one arm's own code handed to the model layer."""

    arm: str
    task_description: str = ""
    system_prompt: str | None = None
    max_steps: int | None = None
    pool: int | None = None
    seat_calls: tuple[SeatCall, ...] = ()
    facade_call: str = ""
    facade_kwargs: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# stand-ins


@dataclass
class _ExecResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _ProbeEnvironment:
    """Answers the two listing commands the repository-map builders run."""

    workspace = "/testbed"

    async def exec_cmd(self, command: str, timeout: float | None = None, **_: Any):
        if "ls-files" in command:
            return _ExecResult(stdout="\0".join(PROBE_TREE))
        if "find ." in command:
            ordered = sorted(PROBE_TREE, key=lambda path: (path.count("/"), path))
            return _ExecResult(stdout="\n".join(f"./{path}" for path in ordered))
        return _ExecResult()

    async def read_file(self, *_: Any, **__: Any) -> str:
        return ""

    async def write_file(self, *_: Any, **__: Any) -> None:
        return None

    async def cleanup(self) -> None:
        return None


def _finished_run_result() -> RunResult[str]:
    return RunResult(
        output="done",
        status="completed",
        reason=None,
        tokens=1,
        error=None,
        metrics={
            "phase": "done",
            "steps": 1,
            "session_quiesced": True,
            "execution_quiesced": True,
        },
    )


class _ScriptedWorkflowContext:
    """A ``WorkflowContext`` stand-in that plays one clean round.

    The replies are schema-shaped so the workflow takes its ordinary path:
    brief, implement, verify with a PASS, adjudicate with an ACCEPT. It reports
    a real ``budget.total`` because the seat cap the workflow applies is
    computed from it, and that cap is one of the audited quantities.
    """

    _REPLIES: tuple[dict[str, Any], ...] = (
        {
            "root_cause": "off-by-one in the pager",
            "files": ["pkg/module_00.py"],
            "implementation_task": "clamp the upper bound",
            "verification_task": "the last page renders its final row",
        },
        {
            "summary_for_tester": "clamped the bound",
            "report_for_analyst": "one file touched",
        },
        {
            "verdict": "PASS",
            "findings_for_coder": "",
            "report_for_analyst": "the row is present",
        },
        {"decision": "ACCEPT", "note": "the tree answers the task"},
    )

    def __init__(self, pool: int) -> None:
        self.budget = SimpleNamespace(total=pool)
        self.calls: list[SeatCall] = []
        self._remaining = list(self._REPLIES)

    def tokens_spent(self) -> int:
        return 0

    async def agent(self, prompt: str, **kwargs: Any) -> Any:
        tools = kwargs.get("tools") or ()
        self.calls.append(
            SeatCall(
                label=str(kwargs.get("label") or ""),
                tools=tuple(getattr(tool, "name", "?") for tool in tools),
                budget=kwargs.get("budget"),
            )
        )
        return self._remaining.pop(0) if self._remaining else None

    async def diff(self) -> str:
        return ""

    async def source_changed(self, exclude_paths: Any = ()) -> bool:
        return False

    async def phase(self, title: str) -> None:
        return None

    async def log(self, message: str) -> None:
        return None


class _RecordingClient:
    """Stands in for the OpenCollab facade and records what each mode asks of it."""

    def __init__(self, observation: ArmObservation) -> None:
        self._observation = observation

    async def agent(self, description: str, **kwargs: Any) -> RunResult[str]:
        self._record("agent", description, kwargs)
        self._observation.seat_calls = (
            SeatCall(
                label=str(kwargs.get("name") or "agent"),
                tools=tuple(
                    getattr(tool, "name", "?") for tool in kwargs.get("tools") or ()
                ),
                budget=kwargs.get("budget"),
            ),
        )
        return _finished_run_result()

    async def workflow(
        self, workflow: Any, args: dict[str, Any], **kwargs: Any
    ) -> RunResult[str]:
        self._record("workflow", str(args.get("description") or ""), kwargs)
        pool = int(kwargs.get("budget") or 0)
        context = _ScriptedWorkflowContext(pool)
        await workflow(context, args)
        self._observation.seat_calls = tuple(context.calls)
        return _finished_run_result()

    async def team(self, description: str, **kwargs: Any) -> RunResult[str]:
        self._record("team", description, kwargs)
        return _finished_run_result()

    def _record(self, call: str, description: str, kwargs: dict[str, Any]) -> None:
        observation = self._observation
        observation.facade_call = call
        observation.task_description = description
        observation.system_prompt = kwargs.get("system_prompt")
        observation.max_steps = kwargs.get("max_steps")
        observation.pool = kwargs.get("budget")
        observation.facade_kwargs = {
            key: value
            for key, value in sorted(kwargs.items())
            if key not in {"artifacts", "system_prompt", "tools"}
        }


@contextmanager
def _swapped(target: Any, name: str, value: Any):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


class _NullTracer:
    def bind_artifacts(self, *_: Any, **__: Any) -> None:
        return None


# --------------------------------------------------------------------------
# the probes


def probe_single_arm(*, budget: int, max_steps: int, timeout: float) -> ArmObservation:
    """Drive ``gen_prediction_agent.run_agent``, the single arm's own runner."""
    observation = ArmObservation(arm="single")
    client = _RecordingClient(observation)
    task = _agent.build_task(PROBE_INSTANCE)

    async def main(artifact_root: Path) -> None:
        with _swapped(_agent, "attach_container", lambda **_: _ProbeEnvironment()):
            await _agent.run_agent(
                task,
                "probe-container",
                {
                    "model": "probe-model",
                    "provider": "probe-provider",
                    "temperature": 0.2,
                },
                max_steps,
                budget,
                timeout,
                artifact_root=artifact_root,
                runtime=client,
            )

    with _scratch_root() as root:
        _run(main(root))
    # ``run_agent`` passes the system prompt itself; the recorder only sees
    # what the facade was called with, which is the point.
    observation.system_prompt = observation.system_prompt or AGENT_PROMPT.strip()
    return observation


def probe_orchestrated_arm(
    arm: str,
    *,
    workflow: Any,
    team_config: str | None,
    budget: int,
    max_steps: int,
    timeout: float,
) -> ArmObservation:
    """Drive ``run_session_or_workflow``, which every non-single arm goes through."""
    observation = ArmObservation(arm=arm)
    description = _orchestrated_description()
    task = EvalTask(
        task_id="probe",
        description=description,
        timeout=timeout,
        max_tokens=budget,
        extras={"blind_validation": True},
    )
    state = ExecutionState(task=task)
    state.env = _ProbeEnvironment()
    config = ExecutionConfig(
        model="probe-model",
        provider="probe-provider",
        api_key=None,
        base_url=None,
        output_dir="",
        prompt=WORKFLOW_AGENT_PROMPT,
        max_steps=max_steps,
        workflow=workflow,
        temperature=0.2,
        top_p=None,
        max_output_tokens=4096,
        thinking=False,
        thinking_params=None,
        wire_protocol="chat_completions",
        reasoning_effort=None,
        llm_connect_timeout=30.0,
        llm_first_event_timeout=180.0,
        llm_stream_idle_timeout=180.0,
        resume_from_checkpoint=False,
        team_config=team_config,
    )
    controller = StageController(deadline=time.monotonic() + timeout, state=state)

    async def main() -> None:
        with _swapped(_sessions, "_client", lambda **_: _RecordingClient(observation)):
            await run_session_or_workflow(
                _evaluator,
                state,
                controller,
                config,
                tools=[],
                tracer=_NullTracer(),
                run_dir=None,
            )

    _run(main())
    return observation


def _orchestrated_description() -> str:
    """The description the workflow generator composes, built by its own code.

    The one place this module restates a composition instead of driving it:
    ``gen_prediction_workflow.main`` assembles the description inline, around
    line 390, and reaching it would mean starting a container. The two calls
    are its calls -- the same ``build_task`` and the same
    ``append_repository_layout`` -- so a change inside either is still caught;
    a change to the order they are applied in would not be.
    """
    from opencollab_eval.generation.gen_prediction_task_text import (
        append_repository_layout,
    )
    from opencollab_eval.generation.gen_prediction_workflow_inputs import build_task

    return append_repository_layout(
        build_task(PROBE_INSTANCE, include_fail_to_pass=False),
        _repo_map_via_opencollab(),
    )


def _repo_map_via_opencollab() -> str:
    from opencollab.environments import build_repo_map_via_env

    return _run(build_repo_map_via_env(_ProbeEnvironment()))


@contextmanager
def _scratch_root():
    """A directory the probe may reserve artifact folders in, then discard.

    The probe reserves one per session it drives, exactly as a real run does,
    so it exercises the same code -- but a probe is run from tests and must not
    leave anything behind.
    """
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="opencollab-eval-arm-probe-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


__all__ = [
    "PROBE_INSTANCE",
    "PROBE_TREE",
    "ArmObservation",
    "SeatCall",
    "probe_orchestrated_arm",
    "probe_single_arm",
]
