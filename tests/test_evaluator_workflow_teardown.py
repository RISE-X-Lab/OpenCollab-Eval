from __future__ import annotations

from evaluator_workflow_test_support import (
    EvalTask,
    FakeEnv,
    asyncio,
    evaluator,
    patch_evaluator_llm,
    run,
    run_eval_task,
)


def test_late_test_injection_paths_are_cleaned_and_never_submitted(
    monkeypatch,
    tmp_path,
):
    env = FakeEnv(diff="diff --git a/tests/leak.py b/tests/leak.py\n+secret test\n")

    async def env_factory(task):
        return env

    async def late_apply_test_patch(_env, _patch):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            return ["tests/leak.py"]

    monkeypatch.setattr(evaluator, "apply_test_patch", late_apply_test_patch)

    result = run(
        run_eval_task(
            EvalTask(
                task_id="late-test-injection",
                description="x",
                timeout=0.02,
                extras={"test_patch": "diff --git a/tests/leak.py b/tests/leak.py"},
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.05,
        )
    )

    assert result.patch == ""
    assert result.test_patch_isolation_failed is True
    assert result.injected_path_cleanup_proven is True
    assert result.task_stage_integrity_proven is False
    assert result.submission_eligible is False
    assert any(command == "git --literal-pathspecs checkout -- tests/leak.py" for command in env.cmds)
    assert any(command == "git --literal-pathspecs clean -fq -- tests/leak.py" for command in env.cmds)


def test_workflow_none_path_unchanged(monkeypatch, tmp_path):
    from opencollab_eval.usage import LLMResponse, Usage

    class FakeLLMClient:
        def __init__(self, *a, **k):
            pass

        async def complete(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="done",
                tool_calls=[],
                usage=Usage(input_tokens=3, output_tokens=2),
                finish_reason="stop",
            )

    patch_evaluator_llm(monkeypatch, FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="t3", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None
