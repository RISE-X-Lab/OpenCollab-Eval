from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from evaluator_test_support import EvalTask, FakeEnv, run, run_eval_task
from opencollab import RunResult

from opencollab_eval.engine import evaluator_sessions


def _install_client(monkeypatch, client_type):
    client_arguments: list[dict] = []
    clients: list[object] = []

    def build(**kwargs):
        client_arguments.append(kwargs)
        client = client_type()
        clients.append(client)
        return client

    monkeypatch.setattr(evaluator_sessions, "_client", build)
    return client_arguments, clients


def test_current_session_evidence_precedes_legacy_aggregate() -> None:
    record = evaluator_sessions._EvalRunRecord(
        RunResult(
            output=None,
            status="stopped",
            reason="timeout",
            metrics={
                "session_quiesced": False,
                "execution_quiesced": True,
            },
        )
    )

    assert record.execution_quiesced is False
    assert record.workflow_error == "OpenCollab session did not quiesce"


def test_agent_delegates_to_public_runtime_with_bound_configuration(
    monkeypatch,
    tmp_path,
):
    class Client:
        calls: list[tuple[str, dict]] = []

        async def agent(self, prompt, **kwargs):
            Client.calls.append((prompt, kwargs))
            artifacts = Path(kwargs["artifacts"])
            (artifacts / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
            return RunResult(
                output="done",
                status="completed",
                tokens=17,
                metrics={
                    "steps": 3,
                    "markup_recovered": 1,
                    "session_quiesced": True,
                    "execution_quiesced": None,
                },
            )

    client_arguments, _clients = _install_client(monkeypatch, Client)
    env = FakeEnv()
    sentinel_tool = object()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(
                task_id="public-agent",
                description="repair",
                max_tokens=4321,
            ),
            model="model-name",
            provider="provider-name",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            output_dir=str(tmp_path),
            tools_factory=lambda: [sentinel_tool],
            env_factory=env_factory,
            max_steps=9,
            temperature=0.7,
            top_p=0.8,
            max_output_tokens=4096,
            context_window=35_500,
            thinking=True,
            thinking_params={"mode": "enabled"},
        )
    )

    assert client_arguments == [
        {
            "env": env,
            "model": "model-name",
            "provider": "provider-name",
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "temperature": 0.7,
            "top_p": 0.8,
            "max_output_tokens": 4096,
            "context_window": 35_500,
            "thinking": True,
            "thinking_params": {"mode": "enabled"},
            "wire_protocol": "chat_completions",
            "reasoning_effort": None,
            "llm_connect_timeout": 30.0,
            "llm_first_event_timeout": 180.0,
            "llm_stream_idle_timeout": 180.0,
        }
    ]
    prompt, call = Client.calls[0]
    assert prompt == "repair"
    assert call["budget"] == 4321
    assert call["max_steps"] == 9
    assert call["tools"] == [sentinel_tool]
    assert 0 < call["timeout"] <= 600
    assert call["trace"] is True
    assert Path(call["artifacts"]).parent == tmp_path / "trajectories" / "public-agent"
    assert Path(result.trajectory_path).read_text(encoding="utf-8") == "{}\n"
    assert result.tokens_used == 17
    assert result.steps == 3
    assert result.markup_recovered == 1
    assert result.patch == env.diff
    assert result.submission_eligible is True
    assert env.cleaned_up is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [("failed", "provider failure"), ("stopped", "cancelled")],
)
def test_failed_or_uncontrolled_agent_result_keeps_patch_but_blocks_submission(
    monkeypatch,
    tmp_path,
    status,
    reason,
):
    class Client:
        async def agent(self, _prompt, **_kwargs):
            return RunResult(
                output=None,
                status=status,
                reason=reason,
                tokens=5,
                metrics={"steps": 2, "execution_quiesced": True},
            )

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id=f"agent-{status}", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error == reason
    assert result.runtime_status == status
    assert result.runtime_reason == reason
    assert result.tokens_used == 5
    assert result.steps == 2
    assert result.submission_eligible is False


@pytest.mark.parametrize("reason", ["budget_exceeded", "timeout"])
def test_controlled_stop_keeps_quiescent_patch_and_metrics_eligible(
    monkeypatch,
    tmp_path,
    reason,
):
    class Client:
        async def agent(self, _prompt, **_kwargs):
            return RunResult(
                output=None,
                status="stopped",
                reason=reason,
                tokens=13,
                metrics={"steps": 6, "execution_quiesced": True},
            )

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id=f"agent-{reason}", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error is None
    assert result.runtime_status == "stopped"
    assert result.runtime_reason == reason
    assert result.tokens_used == 13
    assert result.steps == 6
    assert result.execution_quiesced is True
    assert result.submission_eligible is True


def test_nonquiescent_controlled_stop_blocks_submission(monkeypatch, tmp_path):
    class Client:
        async def agent(self, _prompt, **_kwargs):
            return RunResult(
                output=None,
                status="stopped",
                reason="timeout",
                tokens=13,
                metrics={"steps": 6, "execution_quiesced": False},
            )

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="agent-nonquiescent", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch == ""
    assert result.patch_produced is False
    assert result.runtime_status == "stopped"
    assert result.runtime_reason == "timeout"
    assert result.tokens_used == 13
    assert result.steps == 6
    assert result.execution_quiesced is False
    assert "OpenCollab session did not quiesce" in (result.error or "")
    assert result.submission_eligible is False


@pytest.mark.parametrize("reason", ["budget_exceeded", "timeout"])
def test_controlled_workflow_stop_keeps_patch_eligible(
    monkeypatch,
    tmp_path,
    reason,
):
    class Client:
        async def workflow(self, _workflow, _args, **_kwargs):
            return RunResult(
                output=None,
                status="stopped",
                reason=reason,
                tokens=21,
                metrics={
                    "steps": 4,
                    "sessions": 4,
                    "execution_quiesced": True,
                },
            )

    async def workflow(_ctx, _args):
        return None

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id=f"workflow-{reason}", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
    )

    assert result.patch == env.diff
    assert result.error is None
    assert result.runtime_status == "stopped"
    assert result.runtime_reason == reason
    assert result.tokens_used == 21
    assert result.steps == 4
    assert result.execution_quiesced is True
    assert result.submission_eligible is True


def test_workflow_result_and_public_artifacts_are_bound_to_eval_result(
    monkeypatch,
    tmp_path,
):
    failure = {"agent": "reviewer", "reason": "probe failed"}

    class Client:
        async def workflow(self, workflow, args, **kwargs):
            assert workflow is sample_workflow
            assert args["task_id"] == "public-workflow"
            artifacts = Path(kwargs["artifacts"])
            (artifacts / "orchestration.jsonl").write_text(
                '{"event":"done"}\n',
                encoding="utf-8",
            )
            return RunResult(
                output={"verdict": "complete"},
                status="completed",
                tokens=22,
                metrics={
                    "steps": 4,
                    "sessions": 4,
                    "execution_quiesced": True,
                },
                agent_failures=(failure,),
            )

    async def sample_workflow(_ctx, _args):
        return None

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="public-workflow", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=sample_workflow,
        )
    )

    run_dir = tmp_path / "trajectories" / "public-workflow"
    manifest = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
    assert manifest == {
        "budget_total": 1_000_000,
        "sessions": 4,
        "task_id": "public-workflow",
        "tokens_spent": 22,
        "workflow": "sample_workflow",
    }
    assert Path(result.trajectory_path).read_text(encoding="utf-8") == (
        '{"event":"done"}\n'
    )
    assert result.workflow_result == {"verdict": "complete"}
    assert result.agent_failures == (failure,)
    assert result.tokens_used == 22
    assert result.steps == 4
    assert result.submission_eligible is True


def test_workflow_exception_keeps_observable_patch_and_blocks_submission(
    monkeypatch,
    tmp_path,
):
    class Client:
        async def workflow(self, _workflow, _args, **_kwargs):
            raise RuntimeError("workflow runtime failed")

    async def workflow(_ctx, _args):
        return None

    _install_client(monkeypatch, Client)
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="workflow-error", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
    )

    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error == "RuntimeError: workflow runtime failed"
    assert result.submission_eligible is False


def test_workflow_manifest_failure_keeps_patch_and_blocks_submission(
    monkeypatch,
    tmp_path,
):
    from opencollab_eval.engine import evaluator_resources

    class Client:
        async def workflow(self, _workflow, _args, **_kwargs):
            return RunResult(
                output={"verdict": "complete"},
                status="completed",
                tokens=8,
                metrics={"steps": 2, "execution_quiesced": True},
            )

    async def workflow(_ctx, _args):
        return None

    def fail_manifest(*_args, **_kwargs):
        raise OSError("manifest disk failure")

    _install_client(monkeypatch, Client)
    monkeypatch.setattr(
        evaluator_resources,
        "_write_eval_workflow_manifest",
        fail_manifest,
    )
    env = FakeEnv()

    async def env_factory(_task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="manifest-error", description="repair"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
    )

    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.tokens_used == 8
    assert result.steps == 2
    assert "workflow manifest failed" in (result.error or "")
    assert "manifest disk failure" in (result.error or "")
    assert result.submission_eligible is False
    assert env.cleaned_up is True


def test_workflow_manifest_timeout_marks_execution_nonquiescent(
    monkeypatch,
    tmp_path,
):
    from opencollab_eval.engine import evaluator_resources

    class Client:
        async def workflow(self, _workflow, _args, **_kwargs):
            return RunResult(
                output={"verdict": "complete"},
                status="completed",
                tokens=8,
                metrics={"steps": 2, "execution_quiesced": True},
            )

    async def workflow(_ctx, _args):
        return None

    started = threading.Event()
    release = threading.Event()
    original_write = evaluator_resources._write_eval_workflow_manifest

    def blocking_manifest(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        original_write(*args, **kwargs)

    _install_client(monkeypatch, Client)
    monkeypatch.setattr(
        evaluator_resources,
        "_write_eval_workflow_manifest",
        blocking_manifest,
    )
    env = FakeEnv()

    async def env_factory(_task):
        return env

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="manifest-timeout", description="repair"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=workflow,
                cancellation_cleanup_timeout=0.01,
            )
        )
        assert await asyncio.to_thread(started.wait, 0.5)
        result = await asyncio.wait_for(evaluation, timeout=0.5)
        release.set()
        for _ in range(100):
            if not evaluator_resources._LATE_EVAL_RESOURCE_TASKS:
                break
            await asyncio.sleep(0.01)
        return result

    try:
        result = run(scenario())
    finally:
        release.set()

    assert result.patch == env.diff
    assert result.execution_quiesced is False
    assert "workflow manifest timed out" in (result.error or "")
    assert result.submission_eligible is False
    assert env.cleaned_up is True
    assert not evaluator_resources._LATE_EVAL_RESOURCE_TASKS


def test_runtime_artifact_directories_are_unique_for_repeated_task_runs(
    monkeypatch,
    tmp_path,
):
    artifacts: list[Path] = []

    class Client:
        async def agent(self, _prompt, **kwargs):
            artifacts.append(Path(kwargs["artifacts"]))
            return RunResult(
                output="done",
                status="completed",
                metrics={"execution_quiesced": True},
            )

    _install_client(monkeypatch, Client)

    async def env_factory(_task):
        return FakeEnv()

    for _ in range(2):
        run(
            run_eval_task(
                EvalTask(task_id="repeated", description="repair"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert len(set(artifacts)) == 2
    assert all(path.is_dir() for path in artifacts)
