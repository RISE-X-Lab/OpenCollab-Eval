"""Run a restartable SSH reverse proxy with safe stale-socket cleanup."""

from __future__ import annotations

import argparse
import math
import os
import shlex
import subprocess
import time


class RemoteSocketStillActive(RuntimeError):
    """The previous SSH server process still owns the relay socket."""


class RemoteSocketProbeUnavailable(RuntimeError):
    """The remote socket could not be inspected because SSH is unavailable."""


def _finite_positive(value: object, message: str) -> float:
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(message)
    return normalized


def _cleanup_command(socket_path: str) -> str:
    probe = "\n".join(
        (
            "import errno,os,socket,stat,sys",
            "path=sys.argv[1]",
            "try: info=os.lstat(path)",
            "except FileNotFoundError: raise SystemExit(0)",
            "if not (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()): raise SystemExit(3)",
            "identity=(info.st_dev,info.st_ino)",
            "client=socket.socket(socket.AF_UNIX)",
            "client.settimeout(2)",
            "try: client.connect(path)",
            "except OSError as exc:",
            "    if exc.errno not in {errno.ECONNREFUSED,errno.ENOENT}: raise SystemExit(4)",
            "else: raise SystemExit(5)",
            "try: current=os.lstat(path)",
            "except FileNotFoundError: raise SystemExit(0)",
            "if (current.st_dev,current.st_ino) != identity:",
            "    if stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid(): raise SystemExit(5)",
            "    raise SystemExit(3)",
            "if not (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()): raise SystemExit(3)",
            "try: os.unlink(path)",
            "except FileNotFoundError: pass",
        )
    )
    return shlex.join(["python3", "-c", probe, socket_path])


def remove_stale_remote_socket(
    *,
    ssh_command: str,
    host: str,
    socket_path: str,
    probe_timeout_seconds: float | None = None,
) -> None:
    if probe_timeout_seconds is not None:
        probe_timeout = min(
            20.0,
            _finite_positive(
                probe_timeout_seconds,
                "remote socket probe timeout must be finite and positive",
            ),
        )
    else:
        probe_timeout = 20.0
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        _cleanup_command(socket_path),
    ]
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=probe_timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteSocketProbeUnavailable(
            "timed out while checking the remote proxy socket"
        ) from exc
    if result.returncode == 5:
        raise RemoteSocketStillActive(
            "remote proxy socket is still accepting connections"
        )
    if result.returncode in {3, 4}:
        raise RuntimeError("remote proxy socket cannot be safely replaced")
    if result.returncode != 0:
        raise RemoteSocketProbeUnavailable(
            "remote proxy socket inspection is temporarily unavailable"
        )


def wait_for_remote_socket_release(
    *,
    ssh_command: str,
    host: str,
    socket_path: str,
    timeout_seconds: float | None = None,
    initial_delay_seconds: float = 1.0,
    maximum_delay_seconds: float = 30.0,
) -> None:
    """Wait for an old relay listener to disappear without unlinking it live."""
    if timeout_seconds is not None:
        timeout_seconds = _finite_positive(
            timeout_seconds, "socket release timeout must be finite and positive"
        )
    initial_delay_seconds = _finite_positive(
        initial_delay_seconds, "socket release delays must be finite and positive"
    )
    maximum_delay_seconds = _finite_positive(
        maximum_delay_seconds, "socket release delays must be finite and positive"
    )

    started = time.monotonic()
    delay = min(initial_delay_seconds, maximum_delay_seconds)
    while True:
        probe_kwargs = {
            "ssh_command": ssh_command,
            "host": host,
            "socket_path": socket_path,
        }
        if timeout_seconds is not None:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise RuntimeError("timed out while waiting for the remote proxy socket")
            probe_kwargs["probe_timeout_seconds"] = min(20.0, remaining)
        try:
            remove_stale_remote_socket(**probe_kwargs)
            return
        except (RemoteSocketStillActive, RemoteSocketProbeUnavailable) as exc:
            if (
                timeout_seconds is not None
                and time.monotonic() - started >= timeout_seconds
            ):
                raise RuntimeError(
                    "timed out while waiting for the remote proxy socket"
                ) from exc
            print(f"ssh reverse proxy waiting for remote socket release: {exc}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2.0, maximum_delay_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-command", default="/usr/bin/ssh")
    parser.add_argument("--host", required=True)
    parser.add_argument("--local-port", type=int, required=True)
    parser.add_argument("--remote-port", type=int, required=True)
    parser.add_argument("--remote-socket", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.local_port <= 65535 or not 1 <= args.remote_port <= 65535:
        raise SystemExit("proxy ports must be between 1 and 65535")
    expected_socket = f"/tmp/opencollab-llmproxy-{args.remote_port}.sock"
    if args.remote_socket != expected_socket:
        raise SystemExit("remote proxy socket does not match the remote port")
    if args.host.startswith("-") or any(character.isspace() for character in args.host):
        raise SystemExit("proxy host must be a single SSH destination")
    if not shlex.split(args.ssh_command):
        raise SystemExit("SSH command is empty")
    wait_for_remote_socket_release(
        ssh_command=args.ssh_command,
        host=args.host,
        socket_path=args.remote_socket,
    )
    command = [
        *shlex.split(args.ssh_command),
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StreamLocalBindUnlink=yes",
        "-o",
        "StreamLocalBindMask=0177",
        "-R",
        f"127.0.0.1:{args.remote_port}:127.0.0.1:{args.local_port}",
        "-R",
        f"{args.remote_socket}:127.0.0.1:{args.local_port}",
        args.host,
    ]
    os.execvp(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
