from __future__ import annotations

from evaluator_test_support import (
    EvalTask,
    FakeEnv,
    FakeLLMClient,
    asyncio,
    evaluator,
    is_worktree_diff_cmd,
    patch_evaluator_llm,
    pytest,
    run,
    run_eval_task,
)


def test_caller_cancellation_cleans_environment_before_propagating(monkeypatch, tmp_path):
    started = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(evaluator, "_run_single_session", wait_forever)
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancelled", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert env.cleaned_up is True
    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)


def test_same_tick_caller_and_teardown_self_cancel_still_cleans_environment(
    monkeypatch,
    tmp_path,
):
    teardown_started = asyncio.Event()
    release_teardown = asyncio.Event()
    original_wait = evaluator._wait_for_owned_execution
    calls = 0

    async def quick_session(**kwargs):
        return None

    async def self_cancel_once(tasks, workflow_ctx, *, cleanup_timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            teardown_started.set()
            await release_teardown.wait()
            raise asyncio.CancelledError("inner teardown cancellation")
        return await original_wait(
            tasks,
            workflow_ctx,
            cleanup_timeout=cleanup_timeout,
        )

    monkeypatch.setattr(evaluator, "_run_single_session", quick_session)
    monkeypatch.setattr(
        evaluator,
        "_wait_for_owned_execution",
        self_cancel_once,
    )
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        owner = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="dual-cancel", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        await teardown_started.wait()
        owner.cancel("caller cancellation")
        release_teardown.set()
        with pytest.raises(asyncio.CancelledError):
            await owner

    run(scenario())
    assert env.cleaned_up is True


def test_caller_cancel_bounds_stubborn_environment_cleanup(monkeypatch, tmp_path):
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class StubbornCleanupEnv(FakeEnv):
        async def cleanup(self) -> None:
            cleanup_started.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_cancelled.set()
            self.cleaned_up = True
            cleanup_finished.set()

    env = StubbornCleanupEnv()
    patch_evaluator_llm(monkeypatch, FakeLLMClient)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-env-cleanup", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        eval_task.cancel()
        eval_task.cancel()
        await asyncio.wait_for(cleanup_cancelled.wait(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        assert env.revoked is True
        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)

    run(scenario())
