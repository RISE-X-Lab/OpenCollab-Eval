"""Source for the trusted controller that owns Pytest proof publication."""

from __future__ import annotations


def prolite_pytest_controller_source() -> str:
    """Return the root controller installed read-only in evaluation containers."""

    return r'''#!/usr/bin/env python3
import argparse
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TECHNICAL_EXIT = 86
MAX_EVENT_BYTES = 8 * 1024 * 1024
WORKER_UID = 65534
WORKER_GID = 65534
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof-output", type=Path, required=True)
    parser.add_argument("--command-sha256", required=True)
    parser.add_argument("--plugin-dir", type=Path, default=Path("/eval_input"))
    parser.add_argument("--output-root", type=Path, default=Path("/eval_output"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command or SHA256_RE.fullmatch(args.command_sha256) is None:
        raise ValueError("invalid pytest worker command")
    expected = hashlib.sha256("\0".join(args.command).encode("utf-8")).hexdigest()
    if expected != args.command_sha256:
        raise ValueError("pytest worker command identity changed")
    return args


def _safe_proof_path(path, output_root):
    output = output_root.resolve()
    parent = path.parent.resolve()
    if parent != output or not re.fullmatch(r"[A-Za-z0-9_.-]+\.jsonl", path.name):
        raise ValueError("unsafe pytest proof output path")
    os.chmod(output, 0o700)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise FileExistsError(f"pytest proof output already exists: {path}")


def _prepare_worker_tree(root):
    if os.geteuid() != 0 or WORKER_UID == os.geteuid():
        raise PermissionError("pytest controller requires a distinct root identity")
    if root.resolve() == Path("/"):
        raise ValueError("pytest controller requires a repository working directory")
    os.lchown(root, WORKER_UID, WORKER_GID)
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        for name in directories:
            os.lchown(os.path.join(current, name), WORKER_UID, WORKER_GID)
        for name in files:
            path = os.path.join(current, name)
            try:
                os.lchown(path, WORKER_UID, WORKER_GID)
            except FileNotFoundError:
                pass
    worker_home = Path(tempfile.mkdtemp(prefix="opencollab-pytest-worker-", dir="/tmp"))
    os.chown(worker_home, WORKER_UID, WORKER_GID)
    os.chmod(worker_home, 0o700)
    return worker_home


def _worker_environment(home, event_fd, plugin_dir):
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "DISPLAY",
        "XAUTHORITY",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "PYTHONPATH": str(plugin_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "OPENCOLLAB_PYTEST_EVENT_FD": str(event_fd),
        }
    )
    return environment


def _drop_worker_privileges():
    os.setgroups([])
    os.setgid(WORKER_GID)
    os.setuid(WORKER_UID)
    os.umask(0o077)


def _read_events(descriptor, result):
    chunks = []
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_EVENT_BYTES:
                result["error"] = "pytest event stream exceeds the bounded size"
                break
            chunks.append(chunk)
    except OSError as exc:
        result["error"] = f"pytest event stream read failed: {exc}"
    finally:
        os.close(descriptor)
    result["raw"] = b"".join(chunks)


def _process_group_survived(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _decode_complete_events(raw, returncode):
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("pytest worker event stream ended without protocol EOF")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pytest worker emitted invalid structured events") from exc
    if not events or any(not isinstance(event, dict) for event in events):
        raise ValueError("pytest worker event stream is empty or malformed")
    kinds = [event.get("event") for event in events]
    if (
        kinds[0] != "session_start"
        or kinds[-1] != "session_finish"
        or kinds.count("session_start") != 1
        or kinds.count("collection_finish") != 1
        or kinds.count("session_finish") != 1
        or any(
            kind not in {"session_start", "collection_finish", "runtest_logreport", "session_finish"}
            for kind in kinds
        )
    ):
        raise ValueError("pytest worker protocol is incomplete")
    collection_index = kinds.index("collection_finish")
    finish_index = len(kinds) - 1
    if collection_index == 0 or any(
        kind == "runtest_logreport"
        for kind in kinds[:collection_index]
    ):
        raise ValueError("pytest worker emitted events out of order")
    finish_status = events[-1].get("exitstatus")
    if isinstance(finish_status, bool) or not isinstance(finish_status, int):
        raise ValueError("pytest worker has no numeric session status")
    if returncode < 0 or finish_status != returncode:
        raise ValueError("pytest worker process status disagrees with session status")
    nodeids = events[collection_index].get("nodeids")
    if (
        not isinstance(nodeids, list)
        or len(nodeids) != len(set(nodeids))
        or any(not isinstance(node, str) or not node for node in nodeids)
    ):
        raise ValueError("pytest worker collection census is invalid")
    phases = {}
    for event in events[collection_index + 1 : finish_index]:
        node = event.get("nodeid")
        phase = event.get("when")
        outcome = event.get("outcome")
        if (
            event.get("event") != "runtest_logreport"
            or node not in nodeids
            or phase not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
            or phase in phases.setdefault(node, {})
        ):
            raise ValueError("pytest worker test-phase evidence is invalid")
        phases[node][phase] = outcome
    if returncode == 0 and (
        not nodeids
        or any(
            phases.get(node) != {"setup": "passed", "call": "passed", "teardown": "passed"}
            for node in nodeids
        )
    ):
        raise ValueError("pytest worker success lacks complete per-node evidence")
    return events


def _publish(path, events, metadata):
    event_stream = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for event in events
    )
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1",
        "worker_pid": metadata["worker_pid"],
        "worker_uid": WORKER_UID,
        "controller_uid": os.geteuid(),
        "command_sha256": metadata["command_sha256"],
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof",
        "worker_returncode": metadata["returncode"],
        "event_stream_sha256": hashlib.sha256(event_stream).hexdigest(),
    }
    payload = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for event in events
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("pytest controller proof write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)


def main():
    args = _arguments()
    _safe_proof_path(args.proof_output, args.output_root)
    worker_home = _prepare_worker_tree(Path.cwd())
    read_fd, write_fd = os.pipe()
    event_result = {}
    reader = threading.Thread(target=_read_events, args=(read_fd, event_result), daemon=True)
    reader.start()
    process = subprocess.Popen(
        args.command,
        env=_worker_environment(worker_home, write_fd, args.plugin_dir.resolve()),
        pass_fds=(write_fd,),
        close_fds=True,
        start_new_session=True,
        preexec_fn=_drop_worker_privileges,
    )
    os.close(write_fd)
    returncode = process.wait()
    reader.join(timeout=2.0)
    survived = _process_group_survived(process.pid)
    if reader.is_alive() or survived:
        print("pytest worker left an event writer or process alive", file=sys.stderr)
        return TECHNICAL_EXIT
    if event_result.get("error"):
        print(event_result["error"], file=sys.stderr)
        return TECHNICAL_EXIT
    try:
        events = _decode_complete_events(event_result.get("raw", b""), returncode)
        _publish(
            args.proof_output,
            events,
            {
                "worker_pid": process.pid,
                "command_sha256": args.command_sha256,
                "returncode": returncode,
            },
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return TECHNICAL_EXIT
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


__all__ = ["prolite_pytest_controller_source"]
