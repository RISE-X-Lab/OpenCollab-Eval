"""Preserve image dependencies with an ordinary user and a read-only parent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab_eval.engine import eval_runtime_dependencies
from opencollab_eval.generation import gen_prediction_runtime as runtime
from opencollab_eval.generation import generation_runtime_dependencies as helper


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses ordinary-user directory permissions")
@pytest.mark.parametrize("read_only_parent", [False, True])
def test_real_container_helper_stashes_and_restores_as_ordinary_user(tmp_path, monkeypatch, read_only_parent):
    image = tmp_path / "image"
    repo = image / "testbed"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    git(repo, "add", ".gitignore", "package.json", "package-lock.json")
    git(repo, "-c", "user.name=OpenCollab Test", "-c", "user.email=test@opencollab.invalid",
        "commit", "-m", "fixture")
    base = git(repo, "rev-parse", "HEAD")
    executable = repo / "node_modules" / ".bin" / "test-runner"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf 'dependency-restored\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    installed = tmp_path / "helpers"
    installed.mkdir()
    generation_helper = installed / "opencollab_generation_runtime_dependencies.py"
    shutil.copyfile(helper.__file__, generation_helper)
    shutil.copyfile(eval_runtime_dependencies.__file__, installed / "opencollab_eval_runtime_dependencies.py")
    actions = []

    def execute(*args, input_text="", timeout=None):
        assert args[:4] == ("exec", "-i", "-w", "/tmp")
        assert "-u" not in args
        actions.append(args[-3])
        return subprocess.run(
            [sys.executable, "-c",
             "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv.pop(0); "
             "runpy.run_path(sys.argv[0],run_name='__main__')",
             str(installed), str(generation_helper), *args[-3:]],
            input=input_text, capture_output=True, text=True, timeout=timeout,
        )

    monkeypatch.setattr(runtime, "_install_helpers", lambda _: None)
    monkeypatch.setattr(runtime, "_docker_with_stdin", execute)
    if read_only_parent:
        image.chmod(0o555)
        with pytest.raises(PermissionError):
            (image / "forbidden-store").mkdir()
    store = None
    try:
        state = runtime.stash_solver_runtime_dependencies("owned", base, workspace=str(repo))
        store = Path(state.store)
        expected_parent = Path("/tmp") if read_only_parent else image.resolve()
        assert store.parent == expected_parent
        assert store.name.startswith(".opencollab-generation-runtime-")
        assert store.stat().st_uid == os.geteuid()
        assert store.stat().st_mode & 0o777 == 0o700
        assert state.roots == ("node_modules",)
        assert not (repo / "node_modules").exists()
        runtime.restore_solver_runtime_dependencies("owned", state)
        assert not store.exists()
        completed = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
        assert completed.stdout == "dependency-restored\n"
        assert git(repo, "status", "--porcelain") == ""
        assert actions == ["select-store", "stash", "restore"]
    finally:
        image.chmod(0o755)
        if store is not None and store.exists():
            shutil.rmtree(store)


def test_store_selection_resolves_the_real_workspace_parent(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "links" / "workspace"
    alias.parent.mkdir()
    alias.symlink_to(actual, target_is_directory=True)
    assert helper.select_store(alias, "fixture").parent == actual.resolve().parent


def test_store_selection_falls_back_when_workspace_is_a_separate_mount(tmp_path, monkeypatch):
    original = Path.stat
    workspace = tmp_path.resolve()

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == workspace:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    assert helper.select_store(tmp_path, "fixture") == Path("/tmp/.opencollab-generation-runtime-fixture")
