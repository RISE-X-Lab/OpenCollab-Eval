from __future__ import annotations

from evaluator_workflow_test_support import (
    EvalTask,
    ExecResult,
    FakeEnv,
    ScriptedCtx,
    asyncio,
    evaluator,
    generate_review_fix,
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


def test_generate_review_fix_skips_apply_when_ok(tmp_path):
    env = FakeEnv()
    # Stage 1 implement -> text; stage 2 review verdict -> needs_changes False.
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": False, "feedback": "looks good"},
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Only two agent calls — the apply stage was skipped.
    assert len(ctx.agent_calls) == 2
    # The review call used a schema (structured verdict).
    assert ctx.agent_calls[1]["schema"] is not None
    assert result["needs_changes"] is False


def test_generate_review_fix_runs_apply_when_changes_requested(tmp_path):
    env = FakeEnv()
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": True, "feedback": "rename foo to bar"},
            "applied the feedback",
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Three agent calls — implement, review, apply.
    assert len(ctx.agent_calls) == 3
    assert result["needs_changes"] is True
    # The apply-stage prompt carried the review feedback.
    assert "rename foo to bar" in ctx.agent_calls[2]["prompt"]


def test_generate_review_fix_marks_truncated_diff_unavailable(tmp_path):
    class TruncatedReviewEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return ExecResult(
                returncode=0,
                stdout="diff --git a/x b/x\n+partial secret tail\n",
                stderr="",
                stdout_truncated=True,
                stdout_dropped_bytes=7000,
            )

    ctx = ScriptedCtx(
        TruncatedReviewEnv(),
        replies=[
            "implemented the fix",
            {"needs_changes": False, "feedback": "unavailable"},
        ],
    )

    run(generate_review_fix(ctx, {"description": "fix the bug"}))

    review_prompt = ctx.agent_calls[1]["prompt"]
    assert "diff unavailable" in review_prompt
    assert "stdout dropped 7000 bytes" in review_prompt
    assert "partial secret tail" not in review_prompt
