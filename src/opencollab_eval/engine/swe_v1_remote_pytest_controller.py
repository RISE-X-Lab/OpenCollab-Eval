"""Source for the privileged Pytest proof controller used inside eval containers."""

from __future__ import annotations

from opencollab_eval.engine.swe_v1_remote_pytest_proof import (
    _PYTHON_SOURCE_LAYOUT_ROOTS,
)


def prolite_pytest_controller_source() -> str:
    """Return a small controller that publishes proof outside candidate permissions."""

    source = r'''#!/usr/bin/env python3
import argparse
import hashlib
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TECHNICAL_EXIT = 86
MAX_EVENT_BYTES = 8 * 1024 * 1024
WORKER_UID = 65532
WORKER_GID = 65532
SOURCE_LAYOUT_ROOTS = ()


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof-output", type=Path, required=True)
    parser.add_argument("--command-sha256", required=True)
    parser.add_argument("--plugin-dir", type=Path, default=Path("/eval_input"))
    parser.add_argument("--output-root", type=Path, default=Path("/eval_output"))
    parser.add_argument("--candidate-source-path", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    digest = hashlib.sha256("\0".join(args.command).encode("utf-8")).hexdigest()
    if not args.command or not re.fullmatch(r"[0-9a-f]{64}", args.command_sha256):
        raise ValueError("invalid pytest worker command")
    if digest != args.command_sha256:
        raise ValueError("pytest worker command identity changed")
    return args


def _prepare_output(path, root):
    output = root.resolve()
    if path.parent.resolve() != output or not re.fullmatch(r"[A-Za-z0-9_.-]+\.jsonl", path.name):
        raise ValueError("unsafe pytest proof output path")
    mode = output.stat().st_mode
    if not mode & stat.S_ISVTX or not mode & stat.S_IWOTH:
        raise PermissionError("pytest output root must be sticky and world-writable")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        opened = os.fstat(fd)
        return opened.st_dev, opened.st_ino, opened.st_uid
    finally:
        os.close(fd)


def _prepare_worker(repo):
    if os.geteuid() != 0 or repo.resolve() == Path("/"):
        raise PermissionError("pytest controller requires root and a repository directory")
    os.lchown(repo, WORKER_UID, WORKER_GID)
    for current, directories, files in os.walk(repo, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        for name in [*directories, *(name for name in files if name != ".git")]:
            try:
                os.lchown(os.path.join(current, name), WORKER_UID, WORKER_GID)
            except FileNotFoundError:
                pass
    home = Path(tempfile.mkdtemp(prefix="opencollab-pytest-", dir="/tmp"))
    os.chown(home, WORKER_UID, WORKER_GID)
    os.chmod(home, 0o700)
    return home


def _worker_environment(home, event_fd):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OPENCOLLAB_") and key != "PYTHONPATH"
    }
    environment.update(
        HOME=str(home),
        TMPDIR=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_ADDOPTS="-p no:cacheprovider",
        OPENCOLLAB_PYTEST_EVENT_FD=str(event_fd),
    )
    return environment


def _trusted_worker_command(command, plugin_dir, candidate_source_paths):
    plugin = (plugin_dir.resolve() / "opencollab_pytest_proof.py")
    if plugin.is_symlink() or not plugin.is_file() or plugin.parent != plugin_dir.resolve():
        raise ValueError("trusted pytest proof plugin is unavailable")
    positions = [index for index, value in enumerate(command[:-1]) if value == "-p"]
    if len(positions) != 1 or command[positions[0] + 1] != "opencollab_pytest_proof":
        raise ValueError("pytest worker command lacks the trusted proof plugin")
    position = positions[0]
    controller = str(Path(__file__).resolve())
    if command[:1] == ["pytest"]:
        prefix, pytest_args = [sys.executable, controller], command[1:position] + command[position + 2 :]
    elif len(command) >= 3 and command[1:3] == ["-m", "pytest"]:
        prefix = [command[0], controller]
        pytest_args = command[3:position] + command[position + 2 :]
    elif command[:5] == ["xvfb-run", "-a", "python", "-m", "pytest"]:
        prefix = ["xvfb-run", "-a", "python", controller]
        pytest_args = command[5:position] + command[position + 2 :]
    else:
        raise ValueError("unsupported pytest worker launcher")
    candidate_args = [
        value
        for path in candidate_source_paths
        for value in ("--candidate-source-path", path)
    ]
    return [
        *prefix,
        "--trusted-pytest-worker",
        str(plugin),
        *candidate_args,
        "--",
        *pytest_args,
    ]


def _worker_arguments(argv):
    if not argv:
        raise ValueError("invalid trusted pytest worker arguments")
    try:
        separator = argv.index("--")
    except ValueError as exc:
        raise ValueError("invalid trusted pytest worker arguments") from exc
    options = argv[1:separator]
    if len(options) % 2 or any(options[index] != "--candidate-source-path" for index in range(0, len(options), 2)):
        raise ValueError("invalid trusted pytest worker arguments")
    return Path(argv[0]).resolve(strict=True), options[1::2], argv[separator + 1 :]


def _release_candidate_modules(candidate_source_paths, cwd):
    roots = set()
    import_roots = set()
    for raw in candidate_source_paths:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096 or "\x00" in raw:
            raise ValueError("invalid candidate Python source path")
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("invalid candidate Python source path")
        parts = list(path.parts)
        if parts[:1] and parts[0] in SOURCE_LAYOUT_ROOTS:
            import_roots.add(cwd / parts[0])
            parts = parts[1:]
        if not parts:
            raise ValueError("invalid candidate Python source path")
        module_parts = parts[:-1] if parts[-1] == "__init__.py" else [*parts[:-1], path.stem]
        if module_parts and module_parts[0].isidentifier():
            roots.add(module_parts[0])
    for name, module in list(sys.modules.items()):
        if name.partition(".")[0] not in roots:
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        try:
            Path(origin).resolve().relative_to(cwd)
        except ValueError:
            sys.modules.pop(name, None)
    for import_root in sorted(import_roots, reverse=True):
        if import_root.is_dir():
            sys.path.insert(0, str(import_root))


def _trusted_pytest_worker(argv):
    plugin_path, candidate_source_paths, pytest_args = _worker_arguments(argv)
    cwd = Path.cwd().resolve()
    sys.path[:] = [
        entry
        for entry in sys.path
        if Path(entry or os.getcwd()).resolve() != cwd
    ]
    spec = importlib.util.spec_from_file_location("opencollab_pytest_proof", plugin_path)
    if spec is None or spec.loader is None:
        raise ValueError("trusted pytest proof plugin cannot be loaded")
    plugin = importlib.util.module_from_spec(spec)
    sys.modules["opencollab_pytest_proof"] = plugin
    spec.loader.exec_module(plugin)
    import pytest

    sys.path.insert(0, str(cwd))
    _release_candidate_modules(candidate_source_paths, cwd)
    return int(pytest.main(pytest_args, plugins=[plugin]))


def _drop_privileges():
    os.setgroups([])
    os.setgid(WORKER_GID)
    os.setuid(WORKER_UID)
    os.umask(0o077)


def _collect_events(process, descriptor):
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    chunks = []
    size = 0
    eof = False
    finished_at = None
    try:
        while not eof:
            for _key, _mask in selector.select(0.1):
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    eof = True
                    break
                size += len(chunk)
                if size > MAX_EVENT_BYTES:
                    raise ValueError("pytest event stream exceeds the bounded size")
                chunks.append(chunk)
            if process.poll() is not None:
                finished_at = finished_at or time.monotonic()
                if not eof and time.monotonic() - finished_at > 1.0:
                    raise ValueError("pytest worker left an event writer alive")
    finally:
        selector.close()
        os.close(descriptor)
    return b"".join(chunks)


def _kill_surviving_group(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _decode(raw, returncode):
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("pytest worker protocol ended without EOF")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pytest worker emitted invalid events") from exc
    kinds = [event.get("event") for event in events if isinstance(event, dict)]
    if len(kinds) != len(events) or not kinds or kinds[0] != "session_start" or kinds[-1] != "session_finish":
        raise ValueError("pytest worker protocol is incomplete")
    if kinds.count("session_start") != 1 or kinds.count("collection_finish") != 1 or kinds.count("session_finish") != 1:
        raise ValueError("pytest worker protocol is incomplete")
    if any(kind not in {"session_start", "collection_finish", "runtest_logreport", "session_finish"} for kind in kinds):
        raise ValueError("pytest worker protocol contains an unknown event")
    collection_index = kinds.index("collection_finish")
    if collection_index == 0 or any(kind == "runtest_logreport" for kind in kinds[:collection_index]):
        raise ValueError("pytest worker events are out of order")
    exitstatus = events[-1].get("exitstatus")
    if isinstance(exitstatus, bool) or not isinstance(exitstatus, int) or exitstatus != returncode or returncode < 0:
        raise ValueError("pytest process and session status disagree")
    nodeids = events[collection_index].get("nodeids")
    if (
        not isinstance(nodeids, list)
        or len(nodeids) != len(set(nodeids))
        or any(not isinstance(node, str) or not node for node in nodeids)
    ):
        raise ValueError("pytest collection census is invalid")
    phases = {}
    for event in events[collection_index + 1 : -1]:
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
            raise ValueError("pytest phase evidence is invalid")
        phases[node][phase] = outcome
    complete_pass = {"setup": "passed", "call": "passed", "teardown": "passed"}

    def complete_skip(reports):
        if reports == {"setup": "skipped"}:
            return True
        return (
            set(reports) == {"setup", "call", "teardown"}
            and "skipped" in reports.values()
            and all(outcome in {"passed", "skipped"} for outcome in reports.values())
        )

    if returncode == 0 and (
        not nodeids
        or any(
            phases.get(node) != complete_pass
            and not complete_skip(phases.get(node, {}))
            for node in nodeids
        )
    ):
        raise ValueError("pytest success lacks complete per-node evidence")
    return events


def _publish(path, identity, events, metadata):
    raw = b"".join((json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode() for event in events)
    events[0]["controller"] = {
        "schema": "opencollab.pytest_controller.v1",
        "worker_pid": metadata["pid"],
        "worker_uid": WORKER_UID,
        "controller_uid": os.geteuid(),
        "command_sha256": metadata["command_sha256"],
    }
    events[-1]["controller"] = {
        "termination": "normal_protocol_eof",
        "worker_returncode": metadata["returncode"],
        "event_stream_sha256": hashlib.sha256(raw).hexdigest(),
    }
    payload = b"".join((json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode() for event in events)
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid) != identity or not stat.S_ISREG(opened.st_mode):
            raise OSError("pytest proof reservation identity changed")
        os.ftruncate(fd, 0)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pytest proof write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)


def main():
    args = _arguments()
    proof_identity = _prepare_output(args.proof_output, args.output_root)
    home = _prepare_worker(Path.cwd())
    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            _trusted_worker_command(
                args.command,
                args.plugin_dir,
                args.candidate_source_path,
            ),
            env=_worker_environment(home, write_fd),
            pass_fds=(write_fd,),
            close_fds=True,
            start_new_session=True,
            preexec_fn=_drop_privileges,
        )
        os.close(write_fd)
        write_fd = -1
        owned_read_fd = read_fd
        read_fd = -1
        raw = _collect_events(process, owned_read_fd)
        returncode = process.wait()
        if _kill_surviving_group(process.pid):
            raise ValueError("pytest worker left a process alive")
        events = _decode(raw, returncode)
        _publish(
            args.proof_output,
            proof_identity,
            events,
            {"pid": process.pid, "command_sha256": args.command_sha256, "returncode": returncode},
        )
        return returncode
    except (OSError, ValueError) as exc:
        if process is not None:
            _kill_surviving_group(process.pid)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        args.proof_output.unlink(missing_ok=True)
        print(str(exc), file=sys.stderr)
        return TECHNICAL_EXIT
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--trusted-pytest-worker"]:
        try:
            raise SystemExit(_trusted_pytest_worker(sys.argv[2:]))
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(TECHNICAL_EXIT)
    raise SystemExit(main())
'''
    return source.replace(
        "SOURCE_LAYOUT_ROOTS = ()",
        f"SOURCE_LAYOUT_ROOTS = {tuple(sorted(_PYTHON_SOURCE_LAYOUT_ROOTS))!r}",
        1,
    )


__all__ = ["prolite_pytest_controller_source"]
