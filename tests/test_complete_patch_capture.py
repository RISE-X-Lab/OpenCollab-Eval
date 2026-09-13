from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from opencollab.environments import local_environment

from opencollab_eval.engine import evaluator
from opencollab_eval.engine.environment import ExecResult
from opencollab_eval.engine.evaluator_patch import cleanup_injected_paths_and_extract_patch
from opencollab_eval.engine.patch_capture import capture_complete_diff


async def wait(operation):
    return await operation


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "init")


def test_large_single_line_diff_is_transferred_completely_without_temporary_artifacts(tmp_path):
    repository(tmp_path)
    # Exceeds the public command output limit and crosses UTF-8 chunk boundaries.
    (tmp_path / "large.txt").write_text("€" * 500_000 + "\n")
    (tmp_path / "small.txt").write_text("small\n")

    async def scenario():
        env = local_environment(str(tmp_path))
        await env.setup()
        first = await env.exec_cmd(evaluator.worktree_diff_command())
        assert first.stdout_truncated
        cleaned, patch, extracted, error = await cleanup_injected_paths_and_extract_patch(
            evaluator, env=env, execution_quiesced=True, injected_paths=[], harness_artifact_paths=[],
            cleanup_timeout=10, error=None, test_patch_isolation_failed=False,
            harness_artifact_exclusion_proven=True, checkpoint_restore_integrity_proven=True,
            task_stage_integrity_proven=True, await_teardown=wait,
        )
        assert cleaned and extracted and error is None
        assert "€" * 500_000 in patch
        assert "opencollab-eval-diff-" not in patch
        assert "small.txt" in patch
        assert list(tmp_path.glob("opencollab-eval-diff-*")) == []
        # The resulting artifact really applies to the base tree.
        result = subprocess.run(["git", "apply", "--check", "--reverse", "-"], cwd=tmp_path, input=patch.encode())
        assert result.returncode == 0
        await env.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["bad_encoding", "truncated", "short_chunk", "oversized"])
def test_incomplete_or_oversized_transfer_is_rejected_and_disposed(mode):
    class Environment:
        workspace = "/work"
        removed = False

        async def write_temp_file(self, *args, **kwargs):
            return "/work/owned.patch"

        async def exec_cmd(self, command):
            if command.startswith("("):
                assert "owned.patch" in command
                return ExecResult(0, "", "")
            if command.startswith("wc"):
                return ExecResult(0, "100" if mode == "oversized" else "4", "")
            assert command.startswith("bash -o pipefail -c ")
            return ExecResult(0, "invalid!" if mode == "bad_encoding" else "YQ==", "", mode == "truncated")

        async def remove_file(self, path):
            assert path == "/work/owned.patch"
            self.removed = True

    env = Environment()
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(capture_complete_diff(env, lambda exclusions: "git diff", max_bytes=10, await_teardown=wait))
    assert env.removed


def test_temporary_file_disposal_failure_retains_complete_artifact(tmp_path):
    repository(tmp_path)
    (tmp_path / "fix.txt").write_text("fixed\n")

    async def scenario():
        delegate = local_environment(str(tmp_path))
        await delegate.setup()

        async def removal_failed(path):
            raise OSError("disposal failed")

        env = SimpleNamespace(workspace=delegate.workspace, write_temp_file=delegate.write_temp_file,
                              exec_cmd=delegate.exec_cmd, remove_file=removal_failed)
        patch, error = await capture_complete_diff(
            env, evaluator.worktree_diff_command, max_bytes=100_000, await_teardown=wait,
        )
        assert "+fixed" in patch
        assert isinstance(error, OSError)
        assert "opencollab-eval-diff-" not in patch
        await delegate.cleanup()

    asyncio.run(scenario())
