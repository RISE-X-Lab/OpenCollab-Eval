"""Own generator processes and stop dispatch at a batch signal."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

STOP_GRACE_SECONDS = 10.0


class GeneratorContainerRecoveryError(RuntimeError):
    """One or more owned containers failed after every recovery attempt."""

    def __init__(self, failures: list[tuple[Path, BaseException]]) -> None:
        self.failures = tuple(failures)
        details = "; ".join(f"{path}: {type(error).__name__}: {error}" for path, error in self.failures)
        super().__init__(f"generator container recovery failed for {len(self.failures)} owned containers: {details}")


class BatchStop:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.signum = 0
        self._handlers: dict[signal.Signals, Any] = {}

    def request(self, signum: int = signal.SIGINT, _frame: Any = None) -> None:
        self.signum = self.signum or signum
        self.event.set()

    def __enter__(self) -> BatchStop:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._handlers[signum] = signal.signal(signum, self.request)
        return self

    def __exit__(self, *_args: Any) -> None:
        for signum, handler in self._handlers.items():
            signal.signal(signum, handler)


def _recover_generator_containers(command: list[str], pid: int, identity: str) -> None:
    from . import gen_prediction_docker as owners
    from .container_quiescence import isolate_container_for_preservation
    from .gen_prediction_safe_output import persist_generation_failure

    root = Path(command[command.index("--output") + 1]).parent
    instance_id = Path(command[command.index("--instance-file") + 1]).stem
    failures: list[tuple[Path, BaseException]] = []
    for path in root.glob("**/.opencollab/container_owners/*.json"):
        record = owners._read_owner(path)
        if (
            record is None
            or record["owner_pid"] != pid
            or not identity
            or record["owner_start_identity"] != identity
            or owners._owner_is_live(record)
        ):
            continue
        run_dir = path.parents[2]
        reference = record.get("container_id") or record["container_name"]
        try:
            if record["state"] in {"kept", "preservation_required"}:
                if owners._container_owner_label_state(reference, record["owner_token"]) != "matching":
                    raise RuntimeError("retained container ownership could not be confirmed")
                isolated = isolate_container_for_preservation(reference)
                persist_generation_failure(
                    run_dir, instance_id=instance_id,
                    phase="batch_stop", error=RuntimeError("generator interrupted by batch stop"),
                    evidence={"container_retained": True, "container_isolated": isolated},
                )
            else:
                if not owners._remove_owned_container(record):
                    raise RuntimeError("interrupted generator container cleanup failed")
                owners._clear_compatibility_markers(
                    run_dir, record.get("container_id") or None, record["container_name"],
                )
                owners._unlink_owner(path)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failures.append((path, exc))
            persist_generation_failure(
                run_dir, instance_id=instance_id,
                phase="batch_stop", error=exc,
                evidence={"container_owner_path": str(path), "container_isolation_error": str(exc)},
            )
    if failures:
        raise GeneratorContainerRecoveryError(failures) from failures[0][1]


def run_generator(command: list[str], sink: Any, env: dict[str, str], stop: BatchStop) -> int | None:
    """Wait for one process and quiesce its owned resources on interruption."""
    if stop.event.is_set():
        return None
    process = subprocess.Popen(command, stdout=sink, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    from .gen_prediction_docker import _process_start_identity

    identity = _process_start_identity(process.pid)
    sent_at: float | None = None
    killed = False
    while True:
        if stop.event.is_set():
            if sent_at is None:
                # Python CLI generators unwind their cleanup on SIGINT.
                # SIGTERM has the default immediate-exit action in those CLIs.
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                sent_at = time.monotonic()
            elif not killed and time.monotonic() - sent_at >= STOP_GRACE_SECONDS:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                killed = True
        try:
            returncode = process.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue
    if stop.event.is_set():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Reap the handle before owner PID checks or container recovery.
        _recover_generator_containers(command, process.pid, identity)
    return returncode
