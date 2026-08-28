"""The third execution regime: order of work decided by the model, not by code.

A workflow and a team differ in exactly one respect -- who sequences the work --
and a comparison between them is only about that if everything else is held
equal. These tests hold the evaluator to that: the team regime is selected the
same way a workflow is, it is refused when both are asked for, and the three
run settings that make a team run an experimental arm are fixed here rather
than left to a caller to remember.

That last point is the one worth stating. A run missing any of the three still
finishes and still looks ordinary in its output, so the failure would be
invisible until the data was analysed and found to answer a different question
than the one asked.
"""

from __future__ import annotations

import pytest
from opencollab import RunResult

from opencollab_eval.engine import evaluator_sessions
from opencollab_eval.engine.evaluator import EvalTask, run_eval_task


class _RecordingClient:
    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    async def team(self, prompt, **kwargs):
        self._calls.append({"prompt": prompt, **kwargs})
        return RunResult(
            output="done",
            status="completed",
            tokens=1234,
            metrics={"steps": 7, "sessions": 3},
        )

    async def workflow(self, *_args, **_kwargs):  # pragma: no cover - not used here
        raise AssertionError("the team regime must not fall through to a workflow")


class _Tracer:
    def __init__(self) -> None:
        self.bound: list[tuple] = []

    def bind_artifacts(self, artifacts, *, filename) -> None:
        self.bound.append((artifacts, filename))


@pytest.fixture
def team_calls(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        evaluator_sessions, "_client", lambda **_kwargs: _RecordingClient(calls)
    )
    return calls


async def _run_team(team_calls, tmp_path, *, timeout=60.0, budget=1_000_000):
    tracer = _Tracer()
    record = await evaluator_sessions._run_team_mode(
        task=EvalTask(
            task_id="t-1",
            description="Fix the failing test.",
            timeout=timeout,
            max_tokens=budget,
        ),
        env=object(),
        tracer=tracer,
        team_config=tmp_path / "team.yaml",
        model="gpt-4o",
        provider="openai",
        api_key="test-key",  # pragma: allowlist secret
        base_url=None,
        max_steps=1000,
        save_dir=str(tmp_path / "runs"),
    )
    return record, tracer


async def test_a_team_run_seats_its_roster_before_the_first_model_call(
    team_calls, tmp_path
):
    """Without this the roster is an outcome of the run, not an input to it.

    A run that lets the model spawn its own teammates has no declared topology
    to be judged against, so nothing can be said about whether the collaboration
    that was asked for is the one that happened.
    """
    await _run_team(team_calls, tmp_path)

    assert team_calls[0]["prebuild_team"] is True


async def test_a_team_run_gives_every_teammate_its_own_worktree(team_calls, tmp_path):
    """Shared files are an unrecorded channel between agents.

    With isolation on, a result reaches a teammate only through something the
    run writes down, which is what makes "did this agent adopt that one's work"
    answerable at all.
    """
    await _run_team(team_calls, tmp_path)

    assert team_calls[0]["use_worktrees"] is True


async def test_a_team_run_takes_one_turn_at_a_time(team_calls, tmp_path):
    """Two agents reading one shared budget at once can each be granted all of it."""
    await _run_team(team_calls, tmp_path)

    assert team_calls[0]["serialize_turns"] is True


async def test_a_team_run_carries_the_task_budget_and_deadline(team_calls, tmp_path):
    record, tracer = await _run_team(team_calls, tmp_path, timeout=90.0, budget=500_000)

    call = team_calls[0]
    assert call["prompt"] == "Fix the failing test."
    assert call["budget"] == 500_000
    assert call["timeout"] == 90.0
    assert call["config"] == tmp_path / "team.yaml"
    assert record.used_tokens == 1234
    assert record.step_count == 7
    assert record.session_count == 3
    # A team drives several sessions, so its evidence is bound the way a
    # workflow's is rather than a single session's.
    assert tracer.bound and tracer.bound[0][1] == "trajectory.jsonl"


async def test_a_task_is_sequenced_by_a_workflow_or_by_a_team_but_not_both():
    async def workflow(_ctx, _args):  # pragma: no cover - never invoked
        return None

    with pytest.raises(ValueError, match="not by both"):
        await run_eval_task(
            EvalTask(task_id="t-2", description="x", timeout=1.0, max_tokens=1),
            workflow=workflow,
            team_config="team.yaml",
        )


class _FakeFacade:
    """Only the three entry points ``run_session_or_workflow`` can reach."""

    def __init__(self) -> None:
        self.chosen: str | None = None
        self.seen: dict = {}

    async def build_repository_map(self, _env):
        return ""

    async def _run_team_mode(self, **kwargs):
        self.chosen = "team"
        self.seen = kwargs
        return _Record()

    async def _run_workflow_mode(self, **_kwargs):
        self.chosen = "workflow"
        return _Record()

    async def _run_single_session(self, **_kwargs):
        self.chosen = "session"
        return _Record()


class _Record:
    workflow_error = None


class _Controller:
    async def run(self, _name, awaitable):
        return await awaitable

    def remaining_time(self) -> float:
        return 30.0


def _config(**overrides):
    from opencollab_eval.engine.evaluator_task_execution import ExecutionConfig

    base = dict(
        model="gpt-4o",
        provider="openai",
        api_key=None,
        base_url=None,
        output_dir="out",
        prompt="p",
        max_steps=10,
        workflow=None,
        temperature=0.2,
        top_p=None,
        max_output_tokens=1024,
        thinking=False,
        thinking_params=None,
        wire_protocol="chat_completions",
        reasoning_effort=None,
        llm_connect_timeout=1.0,
        llm_first_event_timeout=1.0,
        llm_stream_idle_timeout=1.0,
        resume_from_checkpoint=False,
    )
    base.update(overrides)
    return ExecutionConfig(**base)


async def _dispatch(config):
    from opencollab_eval.engine.evaluator_task_execution import (
        ExecutionState,
        run_session_or_workflow,
    )

    facade = _FakeFacade()
    state = ExecutionState(
        task=EvalTask(task_id="t-3", description="x", timeout=5.0, max_tokens=10)
    )
    await run_session_or_workflow(
        facade, state, _Controller(), config, tools=[], tracer=None, run_dir=None
    )
    return facade


async def test_a_team_config_selects_the_team_regime(tmp_path):
    facade = await _dispatch(_config(team_config=tmp_path / "team.yaml"))

    assert facade.chosen == "team"
    assert facade.seen["team_config"] == tmp_path / "team.yaml"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [({}, "session"), ({"workflow": object()}, "workflow")],
)
async def test_the_other_two_regimes_are_unchanged(overrides, expected):
    """The control: adding a third branch must not have moved the first two."""
    facade = await _dispatch(_config(**overrides))

    assert facade.chosen == expected


async def test_a_team_run_reports_the_trace_file_that_was_actually_written(
    team_calls, tmp_path
):
    """A path that names a file nobody wrote is worse than reporting no path.

    OpenCollab writes a workflow run's trace as ``orchestration.jsonl`` and a
    team run's as ``trajectory.jsonl``. Both are several sessions under one run
    folder, so it is tempting to report them the same way -- and a run that does
    reports a path that does not exist, which fails only later, in whatever
    reads the evidence.
    """
    _record, tracer = await _run_team(team_calls, tmp_path)

    (artifacts, filename) = tracer.bound[-1]
    assert filename == "trajectory.jsonl"
    assert artifacts is not None


def test_setup_and_run_agree_on_where_a_team_run_writes_its_trace(tmp_path):
    """The two halves must not drift: one names the file, the other reports it.

    ``prepare_eval_run`` builds the tracer with a filename and the run binds the
    path once the artifacts directory exists. Nothing forces those two to be the
    same string, so this holds them to it.
    """
    from opencollab_eval.engine import evaluator
    from opencollab_eval.engine.evaluator_task_setup import _create_tracer

    _dir, _run_dir, tracer = _create_tracer(
        evaluator,
        task_id="t-4",
        output_dir=str(tmp_path),
        workflow=None,
        team_config=tmp_path / "team.yaml",
    )

    from pathlib import Path

    assert Path(tracer.path).name == "trajectory.jsonl"
