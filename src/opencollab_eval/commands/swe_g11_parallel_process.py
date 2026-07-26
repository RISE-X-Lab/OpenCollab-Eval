"""Owned subprocess cleanup for the parallel Pro-Lite scheduler."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from opencollab_eval.commands.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _restore_local_spawn_signals,
    terminate_local_process_group,
    wait_for_local_process_groups_exit,
)

_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SNAPSHOT: tuple[subprocess.Popen[str], ...] = ()
_INTERRUPTED = threading.Event()
TERM_GRACE_SECONDS = 40.0
KILL_GRACE_SECONDS = 5.0


def clear_interrupted() -> None:
    _INTERRUPTED.clear()


def set_interrupted() -> None:
    _INTERRUPTED.set()


def interrupted() -> bool:
    return _INTERRUPTED.is_set()


def run_task_process(
    command: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    global _ACTIVE_SNAPSHOT
    proc: subprocess.Popen[str] | None = None
    registered = False
    spawn_signal_state = (
        _block_local_spawn_signals()
        if threading.current_thread() is threading.main_thread()
        else None
    )
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with _ACTIVE_LOCK:
            _ACTIVE_PROCESSES.add(proc)
            _ACTIVE_SNAPSHOT = tuple(_ACTIVE_PROCESSES)
            registered = True
        if spawn_signal_state is not None:
            _restore_local_spawn_signals(spawn_signal_state)
        if interrupted():
            raise InterruptedError("parallel evaluation interrupted")
        stdout, stderr = proc.communicate()
    except BaseException:
        if proc is not None:
            terminate_local_process_group(
                proc,
                term_timeout=TERM_GRACE_SECONDS,
                kill_timeout=KILL_GRACE_SECONDS,
            )
        raise
    finally:
        try:
            if spawn_signal_state is not None:
                _restore_local_spawn_signals(spawn_signal_state)
        finally:
            if registered:
                with _ACTIVE_LOCK:
                    _ACTIVE_PROCESSES.discard(proc)
                    _ACTIVE_SNAPSHOT = tuple(_ACTIVE_PROCESSES)
    assert proc is not None
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def terminate_active_task_groups() -> None:
    processes = list(_ACTIVE_SNAPSHOT)
    signal_active_task_groups(signal.SIGTERM, processes=processes)
    pending = wait_for_local_process_groups_exit(
        {proc.pid for proc in processes}, timeout=TERM_GRACE_SECONDS
    )
    for pgid in pending:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    wait_for_local_process_groups_exit(pending, timeout=KILL_GRACE_SECONDS)


def signal_active_task_groups(
    signum: int, *, processes: list[subprocess.Popen[str]] | None = None
) -> None:
    if processes is None:
        processes = list(_ACTIVE_SNAPSHOT)
    for proc in processes:
        try:
            os.killpg(proc.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass


def install_signal_handlers() -> dict[signal.Signals, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[signal.Signals, Any] = {}

    def interrupt(signum: int, _frame: object) -> None:
        set_interrupted()
        signal_active_task_groups(signal.SIGTERM)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    return previous


def restore_signal_handlers(previous: dict[signal.Signals, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)
