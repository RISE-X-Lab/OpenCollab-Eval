from __future__ import annotations

import io
import subprocess
import tarfile
from contextlib import nullcontext
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_patch as patcher
from opencollab_eval.generation import workspace_archive


def _timeout() -> patcher._WorkspaceArchiveTimeout:
    return patcher._WorkspaceArchiveTimeout(
        timeout_seconds=900,
        elapsed_seconds=900.25,
        archive_bytes=123,
        archive_entries=4,
        extracted_bytes=50,
        docker_stderr="daemon busy",
    )


def test_workspace_copy_retries_a_timeout_in_a_fresh_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots: list[Path] = []
    quiescence_calls = 0

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        roots.append(root)
        (root / "partial").write_text("discard me", encoding="utf-8")
        if len(roots) == 1:
            raise _timeout()
        (root / "complete").write_text("keep me", encoding="utf-8")
        return "c" * 64, 10, 2, 14

    def quiesce(_container: str) -> None:
        nonlocal quiescence_calls
        quiescence_calls += 1

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "require_container_quiescence", quiesce)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    root, archive = patcher._copy_frozen_workspace("cid", tmp_path)

    assert archive == ("c" * 64, 10, 2, 14)
    assert len(roots) == 2
    assert not roots[0].exists()
    assert (root / "complete").read_text(encoding="utf-8") == "keep me"
    assert quiescence_calls == 2


def test_workspace_copy_discards_both_timed_out_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots: list[Path] = []
    quiescence_calls = 0

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        roots.append(root)
        (root / "partial").write_text("discard me", encoding="utf-8")
        raise _timeout()

    def quiesce(_container: str) -> None:
        nonlocal quiescence_calls
        quiescence_calls += 1

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "require_container_quiescence", quiesce)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    with pytest.raises(patcher._WorkspaceArchiveTimeout):
        patcher._copy_frozen_workspace("cid", tmp_path)

    assert len(roots) == 2
    assert all(not root.exists() for root in roots)
    assert quiescence_calls == 1


def test_workspace_archive_timeout_reports_bounded_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Process:
        def __init__(self, stderr) -> None:
            self.stdout = io.BytesIO(b"")
            self.stderr = stderr
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def poll(self):
            return -9 if self.killed else None

        def wait(self, timeout=None) -> int:
            return -9 if self.killed else 0

    class ImmediateTimer:
        def __init__(self, _seconds, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback()

        def cancel(self) -> None:
            pass

    observed: dict[str, Process] = {}

    def popen(_command, *, stdout, stderr):
        assert stdout == subprocess.PIPE
        stderr.write(b"daemon busy")
        process = Process(stderr)
        observed["process"] = process
        return process

    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900")
    monkeypatch.setattr(workspace_archive.subprocess, "Popen", popen)
    monkeypatch.setattr(workspace_archive.threading, "Timer", ImmediateTimer)
    times = iter((10.0, 910.25))
    monkeypatch.setattr(workspace_archive.time, "monotonic", lambda: next(times))

    with pytest.raises(patcher._WorkspaceArchiveTimeout) as raised:
        patcher._copy_workspace_archive("cid", tmp_path)

    error = raised.value
    assert error.timeout_seconds == 900
    assert error.elapsed_seconds == 900.25
    assert error.archive_bytes == 0
    assert error.archive_entries == 0
    assert error.extracted_bytes == 0
    assert error.docker_stderr == "daemon busy"
    assert observed["process"].killed is True


def test_workspace_archive_stubborn_wait_is_a_bounded_technical_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    waits: list[float | None] = []

    class StubbornProcess:
        stdout = io.BytesIO()

        def poll(self):
            return None

        def kill(self) -> None:
            pass

        def wait(self, timeout=None) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired(["docker", "cp"], timeout)

    process = StubbornProcess()

    def popen(_command, *, stdout, stderr):
        assert stdout == subprocess.PIPE
        return process

    monkeypatch.setattr(workspace_archive.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="did not exit after SIGKILL"):
        workspace_archive._copy_workspace_archive("cid", tmp_path)

    assert waits
    assert all(timeout is not None for timeout in waits)
    assert all(
        timeout <= workspace_archive._PROCESS_KILL_REAP_TIMEOUT_SECONDS
        for timeout in waits
    )


def test_workspace_archive_kill_race_still_reaps_exited_process() -> None:
    waits: list[float | None] = []

    class ExitedProcess:
        def kill(self) -> None:
            raise ProcessLookupError

        def wait(self, timeout=None) -> int:
            waits.append(timeout)
            return -9

    workspace_archive._kill_and_reap(ExitedProcess())

    assert waits == [workspace_archive._PROCESS_KILL_REAP_TIMEOUT_SECONDS]


def test_workspace_archive_timeout_covers_local_processing_after_docker_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo("result.txt")
        member.size = 6
        archive.addfile(member, io.BytesIO(b"result"))
    archive_bytes = payload.getvalue()

    class ExitedProcess:
        def __init__(self, stderr) -> None:
            self.stdout = io.BytesIO(archive_bytes)
            self.stderr = stderr
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def poll(self) -> int:
            return 0

        def wait(self, timeout=None) -> int:
            return 0

    class ImmediateTimer:
        def __init__(self, _seconds, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback()

        def cancel(self) -> None:
            pass

    observed: dict[str, ExitedProcess] = {}

    def popen(_command, *, stdout, stderr):
        assert stdout == subprocess.PIPE
        process = ExitedProcess(stderr)
        observed["process"] = process
        return process

    monkeypatch.setenv("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900")
    monkeypatch.setattr(workspace_archive.subprocess, "Popen", popen)
    monkeypatch.setattr(workspace_archive.threading, "Timer", ImmediateTimer)
    times = iter((10.0, 910.5))
    monkeypatch.setattr(workspace_archive.time, "monotonic", lambda: next(times))

    with pytest.raises(patcher._WorkspaceArchiveTimeout) as raised:
        patcher._copy_workspace_archive("cid", tmp_path)

    error = raised.value
    assert error.elapsed_seconds == 900.5
    assert error.archive_bytes == len(archive_bytes)
    assert error.archive_entries == 1
    assert error.extracted_bytes == 6
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "result"
    assert observed["process"].killed is False
