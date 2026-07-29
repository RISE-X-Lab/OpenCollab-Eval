from __future__ import annotations

from evaluator_test_support import (
    EvalTask,
    ExecResult,
    FakeEnv,
    FakeLLMClient,
    evaluator,
    is_worktree_diff_cmd,
    patch_evaluator_llm,
    pytest,
    run,
    run_eval_task,
)

from opencollab_eval.engine import evaluator_sessions


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


def test_repository_map_is_utf8_bounded_without_splitting_paths():
    paths = ["src/café.py", *(f"very/long/component/{index:04d}.py" for index in range(500))]

    class RepositoryMapEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            assert cmd == "git -c core.quotepath=false ls-files -z"
            assert timeout == 30.0
            return ExecResult(returncode=0, stdout="\0".join(paths) + "\0", stderr="")

    repository_map = run(evaluator.build_repository_map(RepositoryMapEnv()))

    assert repository_map.startswith("Repository files\nsrc/café.py\n")
    assert repository_map.endswith("\n...")
    assert len(repository_map.encode("utf-8")) <= 512
    assert "\ufffd" not in repository_map


def test_repository_map_can_be_disabled_for_bounded_relays(monkeypatch):
    class RepositoryMapEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            raise AssertionError("disabled repository maps must not inspect the workspace")

    monkeypatch.setenv("OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES", "0")

    assert run(evaluator.build_repository_map(RepositoryMapEnv())) == ""


@pytest.mark.parametrize("max_bytes", [1, 17, 18, 19])
def test_repository_map_never_exceeds_tiny_byte_limit(monkeypatch, max_bytes):
    class RepositoryMapEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return ExecResult(returncode=0, stdout="src/module.py\0", stderr="")

    monkeypatch.setenv("OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES", str(max_bytes))
    repository_map = run(evaluator.build_repository_map(RepositoryMapEnv()))

    assert len(repository_map.encode("utf-8")) <= max_bytes


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("4", 4), ("32", 32)])
def test_workflow_concurrency_accepts_bounded_values(monkeypatch, raw, expected):
    monkeypatch.setenv("OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY", raw)

    assert evaluator_sessions._workflow_concurrency() == expected


@pytest.mark.parametrize("raw", ["0", "33", "one"])
def test_workflow_concurrency_rejects_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY", raw)

    with pytest.raises(RuntimeError, match="workflow concurrency"):
        evaluator_sessions._workflow_concurrency()


def test_eval_task_round_trips_extras():
    # extras defaults to None and carries an arbitrary benchmark dict unchanged.
    assert EvalTask(task_id="t", description="d").extras is None
    extras = {"test_patch": "diff...", "fail_to_pass": ["pkg::test_a"]}
    task = EvalTask(task_id="t", description="d", extras=extras)
    assert task.extras == extras
