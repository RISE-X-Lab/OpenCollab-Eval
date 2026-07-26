"""Run a restartable SSH reverse proxy with safe stale-socket cleanup."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess


def _cleanup_command(socket_path: str) -> str:
    probe = "\n".join(
        (
            "import errno,os,socket,stat,sys",
            "path=sys.argv[1]",
            "try: info=os.lstat(path)",
            "except FileNotFoundError: raise SystemExit(0)",
            "if not (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()): raise SystemExit(3)",
            "client=socket.socket(socket.AF_UNIX)",
            "client.settimeout(2)",
            "try: client.connect(path)",
            "except OSError as exc:",
            "    if exc.errno not in {errno.ECONNREFUSED,errno.ENOENT}: raise SystemExit(4)",
            "else: raise SystemExit(5)",
            "try: os.unlink(path)",
            "except FileNotFoundError: pass",
        )
    )
    return shlex.join(["python3", "-c", probe, socket_path])


def remove_stale_remote_socket(
    *, ssh_command: str, host: str, socket_path: str
) -> None:
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
            command, text=True, capture_output=True, timeout=20, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while checking the remote proxy socket") from exc
    if result.returncode == 5:
        raise RuntimeError("remote proxy socket is still accepting connections")
    if result.returncode != 0:
        raise RuntimeError("remote proxy socket cannot be safely replaced")


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
    remove_stale_remote_socket(
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
