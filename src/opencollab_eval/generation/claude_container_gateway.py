"""Task-bound gateway for commands in one offline evaluation container."""

from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import time
from pathlib import Path

CID_RE = re.compile(r"[0-9a-f]{64}")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 1800
CONNECTION_IO_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 1.0
PROCESS_STOP_POLL_SECONDS = 0.02


def _process_group_exists(group_id: int) -> bool:
    """Return whether a process group still has a member."""

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        killpg(group_id, 0)
    except (AttributeError, ProcessLookupError):
        return False
    except OSError as exc:
        # ESRCH is the only reliable empty-group indication.  Permission and
        # other kernel errors are treated conservatively as still present.
        return exc.errno != errno.ESRCH
    return True


def _signal_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, OSError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    # ``docker exec`` can leave grandchildren alive after the CLI is killed.
    # Each gateway command owns a fresh process group so timeout cleanup can
    # terminate the whole invocation, not just the client wrapper.
    # A member can fork just after the first group signal has been delivered.
    # Re-signal and probe the group until two consecutive empty observations,
    # while keeping one finite cleanup window so this path cannot hang.
    deadline = time.monotonic() + PROCESS_STOP_TIMEOUT_SECONDS
    empty_scans = 0
    max_polls = max(
        2, int(PROCESS_STOP_TIMEOUT_SECONDS / PROCESS_STOP_POLL_SECONDS) + 2
    )
    for _ in range(max_polls):
        _signal_process_group(process)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                # A child can remain in an uninterruptible kernel wait.  Do
                # not let reaping consume the whole cleanup window.
                process.wait(timeout=min(remaining, PROCESS_STOP_POLL_SECONDS))
            except (OSError, ChildProcessError, subprocess.TimeoutExpired):
                pass
        else:
            process.poll()
        leader_alive = process.poll() is None
        group_alive = _process_group_exists(process.pid)
        if not leader_alive and not group_alive:
            empty_scans += 1
            if empty_scans >= 2:
                return
        else:
            empty_scans = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, PROCESS_STOP_POLL_SECONDS))


def _bounded_exec(command: list[str], container_id: str) -> tuple[int, bytes, bytes] | str:
    process = subprocess.Popen(
        ["docker", "exec", "-w", "/testbed", container_id, *command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    assert process.stdout is not None and process.stderr is not None
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                return "command timed out"
            if not selector.get_map():
                # Both output streams reached EOF, but the docker exec process
                # may still be alive after closing inherited descriptors.
                # Poll it without calling wait() outside the deadline.
                time.sleep(min(remaining, 0.1))
                continue
            for key, _events in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.data].extend(chunk)
                if sum(map(len, streams.values())) > MAX_RESPONSE_BYTES:
                    _stop_process(process)
                    return "command output exceeded limit"
        return process.wait(), bytes(streams["stdout"]), bytes(streams["stderr"])
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _response(command: list[str], container_id: str) -> dict[str, object]:
    if not command or len(command) > 256 or any(
        not isinstance(item, str) or len(item.encode("utf-8")) > 128 * 1024
        for item in command
    ):
        return {"returncode": 126, "stdout": "", "stderr": "invalid command"}
    try:
        result = _bounded_exec(command, container_id)
    except OSError as exc:
        return {"returncode": 125, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
    if isinstance(result, str):
        return {"returncode": 125, "stdout": "", "stderr": result}
    returncode, stdout, stderr = result
    return {
        "returncode": returncode,
        "stdout": base64.b64encode(stdout).decode("ascii"),
        "stderr": base64.b64encode(stderr).decode("ascii"),
        "encoding": "base64",
    }


def _read_request(connection: socket.socket) -> list[str] | None:
    data = bytearray()
    while b"\n" not in data:
        try:
            chunk = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(data)))
        except (OSError, TimeoutError):
            # A client that sends only a prefix (or nothing) must not occupy
            # the single gateway accept loop forever.
            return None
        if not chunk:
            return None
        data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            return None
    try:
        value = json.loads(bytes(data).split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def _serve_connection(connection: socket.socket, container_id: str) -> None:
    connection.settimeout(CONNECTION_IO_TIMEOUT_SECONDS)
    command = _read_request(connection)
    payload = (
        _response(command, container_id)
        if command is not None
        else {"returncode": 126, "stdout": "", "stderr": "invalid request"}
    )
    wire = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    connection.sendall(wire)


def serve(address: str | Path, container_id: str) -> None:
    if CID_RE.fullmatch(container_id) is None:
        raise SystemExit("gateway requires a full container identity")
    unix_path = address if isinstance(address, Path) else None
    if unix_path is not None:
        unix_path.unlink(missing_ok=True)
    server = socket.socket(
        socket.AF_UNIX if unix_path is not None else socket.AF_INET,
        socket.SOCK_STREAM,
    )
    try:
        if unix_path is not None:
            server.bind(str(unix_path))
            os.chmod(unix_path, 0o600)
        else:
            host, port = str(address).rsplit(":", 1)
            server.bind((host, int(port)))
        server.listen(8)
        while True:
            connection, _address = server.accept()
            with connection:
                try:
                    _serve_connection(connection, container_id)
                except (OSError, TimeoutError):
                    # A disconnected or slow client must not stop the
                    # gateway from accepting subsequent task commands.
                    continue
    finally:
        server.close()
        if unix_path is not None:
            unix_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--socket", type=Path)
    endpoint.add_argument("--listen")
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    serve(args.socket or args.listen, args.container)


if __name__ == "__main__":
    main()
