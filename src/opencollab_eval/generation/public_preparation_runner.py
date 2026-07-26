"""Run trusted public repository setup and quiesce its process group."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_CLEANUP_SECONDS = 3.0
_PR_SET_CHILD_SUBREAPER = 36


def _enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _reap_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _descendants() -> set[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return set()
    parents: dict[int, tuple[int, str]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="ascii")
            fields = text[text.rfind(")") + 2 :].split()
            parents[int(entry.name)] = (int(fields[1]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    family = {os.getpid()}
    changed = True
    while changed:
        changed = False
        for pid, (parent, state) in parents.items():
            if state != "Z" and parent in family and pid not in family:
                family.add(pid)
                changed = True
    family.remove(os.getpid())
    return family


def _quiesce_descendants() -> bool:
    for sent_signal in (signal.SIGTERM, signal.SIGKILL):
        descendants = _descendants()
        if not descendants:
            _reap_children()
            return True
        for pid in descendants:
            try:
                os.kill(pid, sent_signal)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < deadline:
            _reap_children()
            if not _descendants():
                return True
            time.sleep(0.05)
    _reap_children()
    return not _descendants()


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def _quiesce_group(group_id: int) -> bool:
    if not _group_exists(group_id):
        return True
    os.killpg(group_id, signal.SIGTERM)
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        _reap_children()
        if not _group_exists(group_id):
            return True
        time.sleep(0.05)
    os.killpg(group_id, signal.SIGKILL)
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        _reap_children()
        if not _group_exists(group_id):
            return True
        time.sleep(0.05)
    return not _group_exists(group_id)


def run_public_preparation(script: Path, log_path: Path, workspace: Path) -> int:
    """Run one setup script and return its status after process-group cleanup."""
    _enable_subreaper()
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            ["bash", str(script)],
            cwd=workspace,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        status = process.wait()
    group_quiet = _quiesce_group(process.pid)
    descendants_quiet = _quiesce_descendants()
    return status if group_quiet and descendants_quiet else 125


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("usage: public_preparation_runner.py SCRIPT LOG WORKSPACE", file=sys.stderr)
        return 2
    try:
        return run_public_preparation(Path(args[0]), Path(args[1]), Path(args[2]))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"public preparation failed: {exc}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
