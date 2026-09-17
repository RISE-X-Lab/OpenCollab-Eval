"""Configured slow preparation, finite hangs, and owned temporary storage."""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import candidate_environment as candidate
from opencollab_eval.generation import candidate_environment_lean as lean_candidate
from opencollab_eval.generation import gen_prediction_config as config
from opencollab_eval.generation import gen_prediction_docker as docker
from opencollab_eval.generation import gen_prediction_runtime as runtime
from opencollab_eval.generation import gen_prediction_snapshot as snapshot
from opencollab_eval.generation import gen_prediction_workflow as workflow


@pytest.mark.parametrize("action", ["stash", "restore", "remove"])
def test_public_preparation_allowance_reaches_dependency_transport(monkeypatch, action):
    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900")
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "300")
    monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", "43200")
    captured = []
    def run(command, **kwargs):
        captured.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "[]", "")
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    runtime._run("owned", action, "/testbed", "/tmp/owned")
    snapshot._docker_with_stdin("exec", "owned", "true", input_text="")
    assert captured == [43200.0, 300.0]
    assert config._workspace_archive_timeout_from_env() == 900.0


@pytest.mark.parametrize("adapter", [candidate, lean_candidate])
def test_candidate_prepare_uses_same_public_allowance(monkeypatch, adapter):
    monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", "43200")
    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900")
    monkeypatch.setattr(adapter, "image_activation_prefix", lambda *args: "")
    if adapter is lean_candidate:
        monkeypatch.setattr(adapter, "image_helper_python", lambda *args: "/usr/bin/python3")
    monkeypatch.setattr(snapshot, "_install_snapshot_helper", lambda *args: None)
    captured = []
    def run(command, **kwargs):
        captured.append(kwargs["timeout"])
        assert command[-3] == "prepare"
        return subprocess.CompletedProcess(command, 0, "[]", "")
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    adapter.install_candidate_environment(
        "owned", SimpleNamespace(store="/tmp/owned", workspace="/testbed", roots=[])
    )
    assert captured == [43200.0]


def test_default_and_invalid_preparation_allowances(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900")
    assert config._dependency_preparation_timeout_from_env() == 900
    for value in ("0", "nan", "inf", "86401"):
        monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", value)
        with pytest.raises(ValueError):
            config._dependency_preparation_timeout_from_env()


def test_real_slow_preparation_old_bound_fails_new_bound_and_queue_continue(monkeypatch, tmp_path):
    original = subprocess.run
    def execute(command, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(.12); print('[]')"], **kwargs)
    monkeypatch.setattr(snapshot.subprocess, "run", execute)
    monkeypatch.delenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", ".04")
    with pytest.raises(subprocess.TimeoutExpired):
        runtime._run("owned", "stash", "/testbed", "/tmp/owned")
    monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", "1")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(runtime._run, f"owned-{i}", "stash", "/testbed", f"/tmp/owned-{i}") for i in range(5)]
        assert [future.result() for future in futures] == ["[]\n"] * 5


def test_real_hang_remains_bounded_and_owned_process_is_reaped(monkeypatch, tmp_path):
    original = subprocess.run
    pidfile = tmp_path / "owned.pid"
    code = f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(10)"
    def execute(command, **kwargs):
        return original([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(snapshot.subprocess, "run", execute)
    monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", ".25")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runtime._run("owned", "restore", "/testbed", "/tmp/owned")
    assert time.monotonic() - started < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_workflow_tmp_uses_configured_local_storage_with_unique_names(monkeypatch, tmp_path):
    disk = tmp_path / "local-disk"
    reports = tmp_path / "network-reports"
    captured = []
    class StopAtCreation(Exception):
        pass
    def create(*args, **kwargs):
        captured.append(kwargs["temporary_directory"])
        raise StopAtCreation
    monkeypatch.setattr(workflow.tempfile, "gettempdir", lambda: str(disk))
    monkeypatch.setattr(workflow.gp, "start_container_with_marker", create)
    from opencollab_eval.engine import native_progress_watch
    monkeypatch.setattr(native_progress_watch, "register_generator", lambda *args: None)
    args = SimpleNamespace(output=str(reports / "prediction.jsonl"), agent_profile=None)
    for _i in range(2):
        with pytest.raises(StopAtCreation):
            asyncio.run(workflow.generate({"instance_id": "valid-instance"}, "image", {}, args, lambda: None))
    assert captured[0] != captured[1]
    assert all(path.parent == disk / "opencollab-container-tmp" for path in captured)


def test_local_tmp_retains_existing_creation_ownership_and_isolation(monkeypatch, tmp_path):
    temporary = tmp_path / "local-disk" / "owned"
    commands = []
    cid = "a" * 64
    def call(*args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, cid, "")
    monkeypatch.setattr(docker, "_docker", call)
    assert docker.start_container("image", "unique-name", "b" * 32, temporary_directory=temporary) == cid
    command = commands[0]
    assert docker.CONTAINER_OWNER_LABEL + "=" + "b" * 32 in command
    assert "none" == command[command.index("--network") + 1]
    assert f"type=bind,src={temporary.resolve()},dst=/tmp" in command
    assert temporary.stat().st_mode & 0o7777 == 0o1777
    with pytest.raises(FileExistsError):
        docker.start_container("image", "same-name", "b" * 32, temporary_directory=temporary)
