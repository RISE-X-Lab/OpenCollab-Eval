from __future__ import annotations

from asyncio_test_support import assert_cancel_note, assert_cancel_reason
from evaluator_workflow_test_support import (
    CheckpointEnv,
    EvalTask,
    ExecResult,
    FakeEnv,
    LocalEnvironment,
    WorktreeCheckpoint,
    asyncio,
    checkpoint_mod,
    hashlib,
    is_worktree_diff_cmd,
    json,
    os,
    pytest,
    run,
    run_eval_task,
    subprocess,
)


def test_checkpoint_never_maps_host_artifacts_into_non_local_workspace():
    class NonLocalEnv:
        workspace = "/testbed"
        local_filesystem = False

    checkpoint = WorktreeCheckpoint("/testbed/eval_results/trajectories/container-task")

    assert checkpoint._artifact_exclude_paths(NonLocalEnv()) == ()


def test_public_local_environment_maps_repo_artifacts_out_of_candidates(tmp_path):
    from opencollab_eval.engine.evaluator import _host_workspace_root
    from opencollab_eval.engine.swe_checkpoint_artifacts import (
        checkpoint_artifact_exclude_paths,
    )

    class PublicEnvironment:
        workspace = str(tmp_path)
        host_workspace = None
        source_workspace = None
        local_filesystem = True

    env = PublicEnvironment()
    artifact = tmp_path / "eval-output" / "trajectory.jsonl"
    assert _host_workspace_root(env) == tmp_path
    assert checkpoint_artifact_exclude_paths(env, (artifact,)) == (
        "eval-output/trajectory.jsonl",
    )


def _git_patch_context(repo):
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return base, os.path.join(git_dir, "objects")


def test_worktree_diff_uses_only_alternate_index_while_real_lock_is_held(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "source.py").write_text("new\n", encoding="utf-8")
    (repo / "harness.tmp").write_text("secret\n", encoding="utf-8")
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    index = repo / git_dir / "index"
    index_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    lock = repo / git_dir / "index.lock"
    lock.write_text("held\n", encoding="utf-8")
    base, objects = _git_patch_context(repo)

    try:
        result = subprocess.run(
            [
                "bash",
                "-lc",
                checkpoint_mod.worktree_diff_command(
                    ["harness.tmp"],
                    base_revision=base,
                    object_directory=objects,
                    working_tree=str(repo),
                ),
            ],
            cwd=repo,
            text=True,
            capture_output=True,
        )
    finally:
        lock.unlink()

    assert result.returncode == 0, result.stderr
    assert "source.py" in result.stdout
    assert "harness.tmp" not in result.stdout
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_hash
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert staged == ""


def test_worktree_diff_exclusion_reset_failure_cannot_fall_through_to_diff():
    command = checkpoint_mod.worktree_diff_command(
        ["harness.tmp"],
        base_revision="0" * 40,
        object_directory="/tmp/opencollab-test-objects",
        working_tree="/tmp/opencollab-test-worktree",
    )

    assert "|| true" not in command
    assert '--literal-pathspecs reset -q ' in command
    assert " diff --no-ext-diff --no-textconv --cached --binary " in command


def test_worktree_diff_includes_formerly_reserved_prefix_as_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (repo / ".opencollab-retired-model").write_text("hidden\n", encoding="utf-8")
    base, objects = _git_patch_context(repo)

    result = subprocess.run(
        [
            "bash",
            "-lc",
            checkpoint_mod.worktree_diff_command(
                base_revision=base,
                object_directory=objects,
                working_tree=str(repo),
            ),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert ".opencollab-retired-model" in result.stdout


def test_bind_mounted_docker_artifacts_never_enter_patch_or_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )

    class BindMountedEnv(FakeEnv):
        workspace = "/workspace"
        local_filesystem = False

        def __init__(self):
            super().__init__()
            self.host_workspace = str(repo)
            self.host = LocalEnvironment(str(repo))
            self.workspace = str(repo)

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return await self.host.exec_cmd(cmd, timeout)

        async def read_file(self, path: str) -> str:
            return await self.host.read_file(path)

        async def write_file(self, path: str, content: str) -> None:
            await self.host.write_file(path, content)

    env = BindMountedEnv()
    output_dir = repo / "eval_results"

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="bind-artifact-isolation", description="fix"),
            output_dir=str(output_dir),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    assert result.patch == ""
    assert result.patch_extraction_succeeded is True
    assert result.submission_eligible is True
    assert result.checkpoint_result["final"]["submission_eligible"] is False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "eval_results/" in status


def test_workflow_checkpoint_capture_failure_does_not_reendorse_orphan_patch(tmp_path):
    class FailingCheckpointEnv(CheckpointEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            self.cmds.append(cmd)
            if is_worktree_diff_cmd(cmd):
                return ExecResult(returncode=1, stdout="", stderr="diff failed")
            return ExecResult(returncode=0, stdout="", stderr="")

    env = FailingCheckpointEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return {"status": "done"}

    run_dir = tmp_path / "trajectories" / "preserve"
    run_dir.mkdir(parents=True)
    old_patch = "diff --git a/pkg/a.py b/pkg/a.py\n+old\n"
    (run_dir / "checkpoint.worktree.patch").write_text(old_patch, encoding="utf-8")

    result = run(
        run_eval_task(
            EvalTask(task_id="preserve", description="x"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    meta = json.loads((run_dir / "checkpoint.worktree.json").read_text(encoding="utf-8"))
    assert (run_dir / "checkpoint.worktree.patch").read_text(encoding="utf-8") == old_patch
    assert meta["status"] == "failed"
    assert meta["preserved_previous_patch"] is True
    assert meta["submission_eligible"] is False
    assert result.checkpoint_result["final"]["submission_eligible"] is False
    assert result.patch_produced is False


def test_environment_setup_late_result_is_adopted_and_cleaned(tmp_path):
    env = FakeEnv(diff="diff --git a/preexisting b/preexisting\n+dirty\n")

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            return env

    result = run(
        run_eval_task(
            EvalTask(task_id="late-env", description="x", timeout=0.02),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.1,
        )
    )

    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.execution_quiesced is True
    assert result.task_stage_integrity_proven is False
    assert result.submission_eligible is False


def test_environment_setup_late_result_cleanup_failure_blocks_submission(tmp_path):
    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self):
            raise OSError("late cleanup failed")

    env = CleanupFailureEnv()

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return env

    result = run(
        run_eval_task(
            EvalTask(task_id="late-env-cleanup-failure", description="x", timeout=0.02),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=lambda ctx, args: None,
            cancellation_cleanup_timeout=0.05,
        )
    )

    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert "environment cleanup failed" in result.error


def test_late_environment_cancelled_teardown_is_bounded_and_visible(tmp_path):
    class CancelledTeardownEnv(FakeEnv):
        async def abort(self):
            raise asyncio.CancelledError("abort cancelled forever")

        async def cleanup(self):
            raise asyncio.CancelledError("cleanup cancelled forever")

    env = CancelledTeardownEnv()

    async def env_factory(task):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return env

    async def scenario():
        return await asyncio.wait_for(
            run_eval_task(
                EvalTask(
                    task_id="cancelled-late-teardown",
                    description="x",
                    timeout=0.01,
                ),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )

    result = run(scenario())

    assert env.revoked is True
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert "environment abort failed: CancelledError" in result.error
    assert "environment cleanup failed: CancelledError" in result.error


def test_caller_cancel_adopts_late_environment_before_propagating(tmp_path):
    env = FakeEnv()
    setup_started = asyncio.Event()

    async def env_factory(task):
        setup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            return env

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancel-late-env", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.1,
            )
        )
        await setup_started.wait()
        evaluation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await evaluation

    run(scenario())

    assert env.cleaned_up is True


def test_caller_cancel_keeps_environment_cleanup_failure_in_note(tmp_path):
    started = asyncio.Event()

    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self):
            raise OSError("environment cleanup exploded")

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        evaluation = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancel-cleanup-failure", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
                cancellation_cleanup_timeout=0.05,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        evaluation.cancel("primary cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(evaluation, timeout=0.5)
        return caught.value

    cancellation = run(scenario())

    assert_cancel_reason(cancellation, "primary cancellation")
    assert_cancel_note(
        cancellation,
        "environment cleanup failed",
        "environment cleanup exploded",
    )
