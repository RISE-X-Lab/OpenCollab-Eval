from __future__ import annotations

import io
import subprocess

import pytest

from opencollab_eval.generation import gen_prediction_patch_git as patch_git


def test_bounded_git_output_stubborn_wait_is_a_bounded_technical_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float | None] = []

    class StubbornProcess:
        stdout = io.BytesIO()

        def kill(self) -> None:
            pass

        def wait(self, timeout=None) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired(["git"], timeout)

    process = StubbornProcess()

    class InertTimer:
        def __init__(self, _seconds, _callback) -> None:
            pass

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(patch_git.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(patch_git.threading, "Timer", InertTimer)

    with pytest.raises(RuntimeError, match="did not exit after SIGKILL"):
        patch_git.bounded_git_output(
            "git",
            ["diff"],
            env={},
            timeout=30,
            max_bytes=1024,
            label="patch output",
        )

    assert waits
    assert all(timeout is not None for timeout in waits)
    assert all(timeout <= patch_git._PROCESS_KILL_REAP_TIMEOUT_SECONDS for timeout in waits)


def test_patch_kill_race_still_reaps_exited_process() -> None:
    waits: list[float | None] = []

    class ExitedProcess:
        def kill(self) -> None:
            raise ProcessLookupError

        def wait(self, timeout=None) -> int:
            waits.append(timeout)
            return -9

    patch_git._kill_and_reap(ExitedProcess(), label="test")

    assert waits == [patch_git._PROCESS_KILL_REAP_TIMEOUT_SECONDS]
