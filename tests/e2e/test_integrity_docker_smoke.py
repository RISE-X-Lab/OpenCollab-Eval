"""Exercise background smoke startup with real isolated local processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval.engine.workspace_integrity import FailureScope, WorkspaceIntegrityError
from tests.e2e import integrity_docker_smoke as smoke


@pytest.fixture
def local_background_container(monkeypatch, tmp_path):
    source = tmp_path / smoke.SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    marker = tmp_path / "start-background"
    supervisors = []
    exec_timeouts = []
    cleaned = []
    state = SimpleNamespace(
        source=source,
        marker=marker,
        exec_timeouts=exec_timeouts,
        exec_timeout=5,
        cleaned=cleaned,
    )

    def run(arguments, *, timeout):
        assert arguments[0] == "docker"
        if arguments[1] == "run":
            code = arguments[-1].replace("/tmp/start-background", str(marker))
            code = code.replace(
                "subprocess.Popen(['python3','-c',code])",
                f"subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(.4);'+code])",
            )
            supervisors.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=tmp_path,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            return subprocess.CompletedProcess(arguments, 0, "a" * 64 + "\n", "")
        assert arguments[1:4] == ["exec", "-w", "/testbed"]
        exec_timeouts.append(timeout)
        command = arguments[-1].replace("/tmp/start-background", str(marker))
        return subprocess.run(
            ["sh", "-c", command],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=min(timeout, state.exec_timeout),
        )

    monkeypatch.setattr(smoke, "_run", run)
    monkeypatch.setattr(smoke, "prepare_solver_git_snapshot", lambda *_args: object())
    monkeypatch.setattr(
        smoke,
        "prepare_trusted_patch_baseline",
        lambda *_args: SimpleNamespace(cleanup=lambda: cleaned.append(True)),
    )
    try:
        yield state
    finally:
        for process in supervisors:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.parametrize("scope", [FailureScope.TASK, FailureScope.IMAGE, None])
def test_background_smoke_waits_for_actual_delayed_write(
    monkeypatch, local_background_container, scope,
):
    state = local_background_container
    extracted = []

    def extract(*_args):
        assert state.marker.read_text(encoding="utf-8") == "ready\n"
        assert "# background\n" in state.source.read_text(encoding="utf-8")
        extracted.append(True)
        if scope is not None:
            raise WorkspaceIntegrityError("background churn", scope=scope, report={})
        return "accepted", object()

    monkeypatch.setattr(smoke, "extract_patch_trusted", extract)
    if scope is FailureScope.TASK:
        result = smoke._background_task("image", "base", "delayed-start")
        assert result == {"scenario": "background_write", "status": "blocked", "failure_scope": "task"}
    else:
        message = "escaped local task scope" if scope is FailureScope.IMAGE else "accepted as a stable candidate"
        with pytest.raises(RuntimeError, match=message):
            smoke._background_task("image", "base", "delayed-start")
    assert extracted == [True]
    assert state.exec_timeouts == [30]
    assert state.cleaned == [True]


def test_background_smoke_keeps_readiness_wait_bounded(monkeypatch, local_background_container):
    state = local_background_container
    state.exec_timeout = .1
    extracted = []
    monkeypatch.setattr(smoke, "extract_patch_trusted", lambda *_args: extracted.append(True))
    with pytest.raises(subprocess.TimeoutExpired):
        smoke._background_task("image", "base", "startup-timeout")
    assert state.marker.is_file()
    assert state.marker.stat().st_size == 0
    assert state.exec_timeouts == [30]
    assert extracted == []
    assert state.cleaned == [True]
