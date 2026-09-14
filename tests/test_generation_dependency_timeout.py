"""Bulk dependency I/O uses the configured workspace transfer allowance."""

import subprocess

import pytest

from opencollab_eval.generation import gen_prediction_runtime as runtime
from opencollab_eval.generation import gen_prediction_snapshot as snapshot


@pytest.mark.parametrize("action", ["stash", "restore", "remove"])
@pytest.mark.parametrize("archive_timeout", [None, "1200.5"])
def test_dependency_operations_use_archive_timeout(monkeypatch, action, archive_timeout):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "2.5")
    if archive_timeout is None:
        monkeypatch.delenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", archive_timeout)
    captured = []

    def execute(command, **kwargs):
        captured.append(kwargs["timeout"])
        if command[-3] in {"stash", "restore", "remove"} and kwargs["timeout"] < 90:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(snapshot.subprocess, "run", execute)

    assert runtime._run("owned-container", action, "/testbed", "/tmp/owned-dependencies") == "[]"
    snapshot._docker_with_stdin("exec", "owned-container", "true", input_text="")

    assert captured == [900.0 if archive_timeout is None else 1200.5, 2.5]
