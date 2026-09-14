"""Real pytest execution with a container-style mismatch of shell and repo cwd."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.verification.run_tests import RunTestsTool


class ShellEnvironment:
    process_isolated = True

    def __init__(self, workspace: Path, shell_directory: Path):
        self.workspace = str(workspace)
        self.shell_directory = shell_directory
        self.commands: list[str] = []

    async def exec_cmd(self, command, timeout=120.0):
        self.commands.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=self.shell_directory,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        return SimpleNamespace(returncode=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())


def runtime(environment):
    return SimpleNamespace(environment=environment, safety_policy=None, confirm_fn=lambda: None)


def _write_test(directory: Path, *, passed: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "test_example.py"
    path.write_text(f"def test_value():\n    assert {passed!r}\n")
    return path


@pytest.mark.parametrize("selector", ["", "::test_value"])
def test_absolute_pytest_target_passes_from_different_shell_directory(tmp_path, selector):
    repo = tmp_path / "repo with spaces"
    path = _write_test(repo, passed=True)
    # A same-named failing test in the image cwd must not be selected instead.
    shell = tmp_path / "image-cwd"
    _write_test(shell, passed=False)
    env = ShellEnvironment(repo, shell)
    tool = RunTestsTool()
    target = str(path) + selector

    result = asyncio.run(tool.execute_with_runtime(
        {"target": target, "runner": f"{shlex.quote(sys.executable)} -m pytest"}, runtime(env),
    ))

    assert "Verdict: GREEN" in result
    assert "passed=1" in result
    assert tool.verified_targets == frozenset({target})
    assert env.commands[-1].startswith(f"cd -- {shlex.quote(str(repo))} && ")


def test_absolute_target_outside_workspace_preserves_exact_file_identity(tmp_path):
    repo = tmp_path / "repo"
    _write_test(repo, passed=True)
    other = _write_test(tmp_path / "other", passed=False)
    env = ShellEnvironment(repo, repo)
    tool = RunTestsTool()

    result = asyncio.run(tool.execute_with_runtime(
        {"target": str(other), "runner": f"{shlex.quote(sys.executable)} -m pytest"}, runtime(env),
    ))

    assert "failed=1" in result
    assert "Verdict: RED" in result
    assert not tool.verified_targets
    assert "../other/test_example.py" in env.commands[-1]


def test_failed_relative_rerun_invalidates_previous_absolute_pass(tmp_path):
    repo = tmp_path / "repo"
    path = _write_test(repo, passed=True)
    env = ShellEnvironment(repo, tmp_path)
    tool = RunTestsTool()
    runner = f"{shlex.quote(sys.executable)} -m pytest"
    first = asyncio.run(tool.execute_with_runtime({"target": str(path), "runner": runner}, runtime(env)))
    assert "Verdict: GREEN" in first
    path.write_text("def test_value():\n    raise AssertionError('regression')\n")

    second = asyncio.run(tool.execute_with_runtime(
        {"target": "./test_example.py", "runner": runner}, runtime(env),
    ))

    assert "Verdict: RED" in second
    assert not tool.verified_targets


def test_runner_detection_uses_declared_workspace(tmp_path):
    from opencollab_eval.verification._run_tests_workspace import workspace_environment
    from opencollab_eval.verification._test_results import _detect_native_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/sample\n")
    env = ShellEnvironment(repo, tmp_path)

    runner = asyncio.run(_detect_native_runner(workspace_environment(env)))

    assert runner == "go test"
    assert all(command.startswith(f"cd -- {shlex.quote(str(repo))} && ") for command in env.commands)


def test_candidate_workspaces_keep_comparable_test_commands(tmp_path):
    records = []
    for name in ["candidate-a", "candidate-b"]:
        repo = tmp_path / name
        _write_test(repo, passed=True)
        tool = RunTestsTool()
        result = asyncio.run(tool.execute_with_runtime(
            {"target": "test_example.py", "runner": f"{shlex.quote(sys.executable)} -m pytest"},
            runtime(ShellEnvironment(repo, tmp_path)),
        ))
        assert "Verdict: GREEN" in result
        records.append(tool.verification_records[-1])

    assert records[0]["command"] == records[1]["command"]
    assert records[0]["workspace"] != records[1]["workspace"]
