from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import public_preparation_runner as runner


def _script(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "prepare.sh"
    script.write_text(source, encoding="utf-8")
    return script


def _run_cli(script: Path, log_path: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "opencollab_eval.generation.public_preparation_runner",
            str(script),
            str(log_path),
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_preparation_returns_the_script_status(tmp_path: Path) -> None:
    completed = _run_cli(
        _script(tmp_path, "echo prepared\nexit 7\n"),
        tmp_path / "prepare.log",
        tmp_path,
    )

    assert completed.returncode == 7
    assert (tmp_path / "prepare.log").read_text(encoding="utf-8") == "prepared\n"


@pytest.mark.skipif(sys.platform == "darwin", reason="Codex macOS profile denies process-group signals")
def test_public_preparation_quiesces_a_late_background_writer(tmp_path: Path) -> None:
    target = tmp_path / "late.txt"
    completed = _run_cli(
        _script(tmp_path, f"(sleep 0.3; echo late > {target}) &\nexit 0\n"),
        tmp_path / "prepare.log",
        tmp_path,
    )
    time.sleep(0.4)

    assert completed.returncode == 0
    assert not target.exists()


@pytest.mark.skipif(sys.platform == "darwin", reason="Codex macOS profile denies process-group signals")
def test_public_preparation_kills_a_term_ignoring_writer(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    target = tmp_path / "late.txt"
    child = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(10);"
        f"pathlib.Path({str(target)!r}).write_text('late')"
    )
    source = f"python3 -c {child!r} &\nwhile [ ! -e {ready} ]; do sleep 0.01; done\nexit 0\n"

    completed = _run_cli(
        _script(tmp_path, source),
        tmp_path / "prepare.log",
        tmp_path,
    )
    time.sleep(0.1)

    assert completed.returncode == 0
    assert not target.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper required")
def test_public_preparation_quiesces_a_setsid_writer(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    target = tmp_path / "late.txt"
    child = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(0.3);"
        f"pathlib.Path({str(target)!r}).write_text('late')"
    )
    source = f"python3 -c {child!r} &\nwhile [ ! -e {ready} ]; do sleep 0.01; done\nexit 0\n"

    completed = _run_cli(
        _script(tmp_path, source),
        tmp_path / "prepare.log",
        tmp_path,
    )
    time.sleep(0.4)

    assert completed.returncode == 0
    assert not target.exists()


@pytest.mark.skipif(
    sys.platform.startswith("linux") or os.name != "posix",
    reason="non-/proc process-tree fallback is only exercised on POSIX hosts",
)
def test_public_preparation_ps_tracking_quiesces_a_setsid_writer(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    target = tmp_path / "late.txt"
    child = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(0.3);"
        f"pathlib.Path({str(target)!r}).write_text('late')"
    )
    source = f"python3 -c {child!r} &\nwhile [ ! -e {ready} ]; do sleep 0.01; done\nexit 0\n"

    completed = _run_cli(
        _script(tmp_path, source),
        tmp_path / "prepare.log",
        tmp_path,
    )
    time.sleep(0.4)

    assert completed.returncode == 0
    assert not target.exists()


@pytest.mark.skipif(
    sys.platform.startswith("linux") or os.name != "posix",
    reason="non-/proc process-tree fallback is only exercised on POSIX hosts",
)
def test_public_preparation_cleans_a_disowned_setsid_writer(
    tmp_path: Path,
) -> None:
    """A disowned child must not survive a successful preparation return."""
    ready = tmp_path / "ready"
    pid_path = tmp_path / "child.pid"
    target = tmp_path / "late.txt"
    child = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
        "time.sleep(10);"
        f"pathlib.Path({str(target)!r}).write_text('late')"
    )
    source = (
        f"python3 -c {child!r} &\n"
        "child=$!\n"
        f"while [ ! -e {ready} ]; do sleep 0.01; done\n"
        'disown "$child"\n'
        "exit 0\n"
    )

    completed = _run_cli(
        _script(tmp_path, source),
        tmp_path / "prepare.log",
        tmp_path,
    )

    assert completed.returncode == 0
    assert pid_path.exists()
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and runner._pid_is_live(child_pid):
            time.sleep(0.01)
        assert not runner._pid_is_live(child_pid)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert not target.exists()


def test_public_preparation_reports_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_enable_subreaper", lambda: None)
    monkeypatch.setattr(runner, "_quiesce_group", lambda group_id: False)

    status = runner.run_public_preparation(
        _script(tmp_path, "exit 0\n"),
        tmp_path / "prepare.log",
        tmp_path,
    )

    assert status == 125


def test_descendant_inspection_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> set[int]:
        raise runner.ProcessInspectionError("/proc unavailable")

    monkeypatch.setattr(runner, "_descendants", unavailable)

    assert runner._quiesce_descendants() is False


def test_proc_stat_read_failure_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_args, **_kwargs):
        raise PermissionError("hidden process")

    monkeypatch.setattr(Path, "read_text", denied)

    with pytest.raises(runner.ProcessInspectionError):
        runner._proc_stat(123)


def test_ps_identity_normalizes_bsd_date_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SimpleNamespace(returncode=0, stdout=" Ss   Tue Sep  1 01:06:49 2026\n")
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: result)

    assert runner._ps_process_state_identity(123) == (
        "Ss",
        "ps:Tue Sep 1 01:06:49 2026",
    )


def test_zombie_is_not_counted_as_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_process_state_identity",
        lambda _pid: ("Z+", "proc:42"),
    )

    assert runner._pid_is_live(42) is False


def test_observed_pid_reuse_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(runner, "_descendants", lambda: set())
    monkeypatch.setattr(runner, "_pid_is_live", lambda _pid: True)
    monkeypatch.setattr(
        runner,
        "_process_state_identity",
        lambda _pid: ("S", "proc:new"),
    )
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert runner._quiesce_descendants({42}, {42: "proc:old"}) is True
    assert signals == []


def test_reused_process_group_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        runner,
        "_process_state_identity",
        lambda _pid: ("S", "proc:new"),
    )
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda group, sig: signals.append((group, sig)),
    )

    assert runner._quiesce_group(42, "proc:old", leader_pid=42) is True
    assert signals == []


def test_ps_descendant_fallback_is_bounded_and_ignores_its_probe_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = 400
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                f"{root} 1 S /usr/bin/python\n"
                "401 400 S /usr/bin/worker\n"
                "402 400 S /usr/bin/ps\n"
            ),
        ),
    )

    assert runner._ps_descendants(root) == {401}


def test_ps_descendant_fallback_timeout_is_not_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "run", timed_out)

    with pytest.raises(runner.ProcessInspectionError):
        runner._ps_descendants(os.getpid())


def test_public_preparation_timeout_is_bounded_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HungProcess:
        pid = 1234
        returncode = None

        def __init__(self) -> None:
            self.wait_calls: list[float | None] = []
            self._polls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired("before_repo", timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

        def poll(self) -> int | None:
            self._polls += 1
            return self.returncode

    process = HungProcess()
    monkeypatch.setattr(runner, "_enable_subreaper", lambda: None)
    monkeypatch.setattr(runner, "_PREPARATION_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner, "_quiesce_group", lambda group_id: True)
    monkeypatch.setattr(runner, "_quiesce_descendants", lambda: True)

    status = runner.run_public_preparation(
        _script(tmp_path, "exit 0\n"),
        tmp_path / "prepare.log",
        tmp_path,
    )

    assert status == 124
    assert process.wait_calls == [0.25, runner._CLEANUP_SECONDS]
    assert "timed out after 0.25 seconds" in (
        tmp_path / "prepare.log"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "value", ["0", "-1", "nan", "inf", "-inf", "86401", "not-a-number"]
)
def test_public_preparation_timeout_env_rejects_nonfinite_or_nonpositive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="finite.*positive"):
        runner._preparation_timeout_seconds()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl required")
def test_public_preparation_cli_does_not_change_parent_subreaper_state(
    tmp_path: Path,
) -> None:
    pr_get_child_subreaper = 37
    libc = ctypes.CDLL(None, use_errno=True)

    def current_state() -> int:
        value = ctypes.c_int()
        result = libc.prctl(
            pr_get_child_subreaper,
            ctypes.byref(value),
            0,
            0,
            0,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, "PR_GET_CHILD_SUBREAPER failed")
        return value.value

    before = current_state()
    completed = _run_cli(
        _script(tmp_path, "exit 0\n"),
        tmp_path / "prepare.log",
        tmp_path,
    )

    assert completed.returncode == 0
    assert current_state() == before


def test_process_group_cleanup_escalates_from_term_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    def fake_killpg(group_id: int, sent_signal: int) -> None:
        assert group_id == 123
        if sent_signal == 0 and signal.SIGKILL in signals:
            raise OSError(errno.ESRCH, "gone")
        if sent_signal:
            signals.append(sent_signal)

    ticks = iter((0.0, 0.0, 4.0, 4.0, 4.0))
    monkeypatch.setattr(runner.os, "killpg", fake_killpg)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)

    assert runner._quiesce_group(123) is True
    assert signals == [signal.SIGTERM, signal.SIGKILL]
