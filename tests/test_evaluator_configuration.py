from __future__ import annotations

from evaluator_test_support import (
    EvalTask,
    FakeEnv,
    FakeLLMClient,
    evaluator,
    is_worktree_diff_cmd,
    patch_evaluator_llm,
    pytest,
    run,
    run_eval_task,
)


@pytest.mark.parametrize(
    "cleanup_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_cleanup_timeout_without_side_effects(tmp_path, cleanup_timeout):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="cancellation_cleanup_timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-cleanup-timeout", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=cleanup_timeout,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "task_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_task_timeout_without_side_effects(tmp_path, task_timeout):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="task timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(
                    task_id="invalid-task-timeout",
                    description="fix",
                    timeout=task_timeout,
                ),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "checkpoint_interval",
    [float("nan"), float("inf"), float("-inf"), -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_checkpoint_interval_without_side_effects(tmp_path, checkpoint_interval):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="checkpoint_interval_seconds must be finite and non-negative",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-checkpoint-interval", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                checkpoint_interval_seconds=checkpoint_interval,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


def test_run_eval_task_staged_extraction_includes_new_files(monkeypatch, tmp_path):
    patch_evaluator_llm(monkeypatch, FakeLLMClient)
    env = FakeEnv(
        diff=(
            "diff --git a/new_module.py b/new_module.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_module.py\n"
            "@@ -0,0 +1 @@\n"
            "+value = 1\n"
        )
    )

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="new-file", description="add file"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
    assert "new file mode" in result.patch
    assert result.patch_produced is True


def test_default_tools_match_curated_team_surface():
    # The headless eval agent must exercise the same curated toolset as team
    # roles — in particular run_tests/git_diff/apply_patch, which the bash
    # description deflects to. Guards against the two paths drifting apart.
    names = [t.name for t in evaluator.default_tools()]
    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "git_diff",
        "grep",
    ]
    by_name = {tool.name: tool for tool in evaluator.default_tools()}
    assert by_name["bash"].require_process_isolation is True
    assert by_name["run_tests"].require_process_isolation is True
    assert by_name["run_tests"].allow_runner_override is False
    assert by_name["run_tests"].allow_extra_args is False


def test_eval_task_round_trips_extras():
    # extras defaults to None and carries an arbitrary benchmark dict unchanged.
    assert EvalTask(task_id="t", description="d").extras is None
    extras = {"test_patch": "diff...", "fail_to_pass": ["pkg::test_a"]}
    task = EvalTask(task_id="t", description="d", extras=extras)
    assert task.extras == extras
