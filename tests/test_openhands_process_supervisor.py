from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from opencollab_eval.generation import openhands_process_supervisor as supervisor

_SUPERVISOR_MODULE = "opencollab_eval.generation.openhands_process_supervisor"


def test_supervisor_decodes_wait_status_without_python39_helper(monkeypatch):
    monkeypatch.setattr(supervisor.os, "waitstatus_to_exitcode", None)

    assert supervisor._decode_wait_status(7 << 8) == 7
    assert supervisor._decode_wait_status(signal.SIGTERM) == -signal.SIGTERM


def test_container_final_empty_verification_rejects_late_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = iter(({}, {41: "late-start"}))
    clock = iter((0.0, 0.0, 0.1, 0.21))
    monkeypatch.setattr(
        supervisor,
        "_live_container_processes",
        lambda preserved: next(scans),
    )
    monkeypatch.setattr(
        supervisor,
        "_signal_container_processes",
        lambda identities, sig: None,
    )
    monkeypatch.setattr(supervisor, "_reap_adopted", lambda: None)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: None)

    with pytest.raises(supervisor.SupervisorError, match="remained after SIGKILL"):
        supervisor._require_stable_container_empty({1, 99})


def test_supervisor_converts_unexpected_errors_to_technical_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cleaned: list[int] = []

    def fail(*args, **kwargs):
        raise OSError("synthetic supervisor failure")

    monkeypatch.setattr(supervisor, "run", fail)
    monkeypatch.setattr(
        supervisor,
        "terminate_descendants",
        lambda pid: cleaned.append(pid),
    )

    assert supervisor.main(["--", "ignored-command"]) == 125
    assert cleaned == [supervisor.os.getpid()]
    assert "synthetic supervisor failure" in capsys.readouterr().err


def test_outer_supervisor_group_is_killed_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, signal.Signals]] = []

    class Process:
        pid = 41

        def wait(self, timeout):
            assert timeout == supervisor.KILL_GRACE_SECONDS + 1.0
            return -signal.SIGKILL

    monkeypatch.setattr(
        supervisor.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    assert supervisor.terminate_supervisor_process(Process()) == ""

    assert killed == [(41, signal.SIGKILL)]


def test_outer_supervisor_reap_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 41

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("supervisor", timeout)

    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: None)

    assert supervisor.terminate_supervisor_process(Process()) == (
        "supervisor process did not exit after SIGKILL"
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux subreaper regression",
)
def test_supervisor_captures_double_fork_setsid_escape(tmp_path: Path) -> None:
    sentinel = tmp_path / "setsid-leak"
    code = textwrap.dedent(
        f"""
        import os
        import pathlib
        import time

        if os.fork():
            os._exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        os.close(1)
        os.close(2)
        time.sleep(0.4)
        pathlib.Path({str(sentinel)!r}).write_text("leaked")
        os._exit(0)
        """
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            _SUPERVISOR_MODULE,
            "--",
            sys.executable,
            "-c",
            code,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux subreaper regression",
)
def test_supervisor_timeout_cleans_setsid_escape_before_return(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "timeout-setsid-leak"
    escaped_pid = tmp_path / "timeout-setsid-pid"
    code = textwrap.dedent(
        f"""
        import os
        import pathlib
        import signal
        import time

        if os.fork():
            time.sleep(2.0)
            os._exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.close(1)
        os.close(2)
        pathlib.Path({str(escaped_pid)!r}).write_text(str(os.getpid()))
        time.sleep(2.0)
        pathlib.Path({str(sentinel)!r}).write_text("leaked")
        os._exit(0)
        """
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            _SUPERVISOR_MODULE,
            "--timeout-seconds",
            "1.0",
            "--",
            sys.executable,
            "-c",
            code,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    assert escaped_pid.exists()
    assert not Path(f"/proc/{escaped_pid.read_text()}").exists()
    assert not sentinel.exists()


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="Unsupported-platform fail-closed regression",
)
def test_supervisor_fails_closed_without_linux_proc() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            _SUPERVISOR_MODULE,
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 125
    assert "Linux /proc is required" in result.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_supervisor_rejects_non_finite_timeout(value: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", _SUPERVISOR_MODULE, "--timeout-seconds", value, "--", sys.executable, "-c", "pass"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    assert result.returncode == 125
    assert "finite and positive" in result.stderr


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf"), True])
def test_run_rejects_invalid_timeout_before_starting_supervisor(
    value: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "enable_subreaper", pytest.fail)

    with pytest.raises(supervisor.SupervisorError, match="finite and positive"):
        supervisor.run(["ignored-command"], timeout_seconds=value)
