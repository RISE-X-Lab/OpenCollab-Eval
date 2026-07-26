"""Task-bound gateway for commands in one offline evaluation container."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import selectors
import socket
import subprocess
import time
from pathlib import Path

CID_RE = re.compile(r"[0-9a-f]{64}")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 1800


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def _bounded_exec(command: list[str], container_id: str) -> tuple[int, bytes, bytes] | str:
    process = subprocess.Popen(
        ["docker", "exec", "-w", "/testbed", container_id, *command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    assert process.stdout is not None and process.stderr is not None
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                return "command timed out"
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
        chunk = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(data)))
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
                command = _read_request(connection)
                payload = (
                    _response(command, container_id)
                    if command is not None
                    else {"returncode": 126, "stdout": "", "stderr": "invalid request"}
                )
                connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
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
