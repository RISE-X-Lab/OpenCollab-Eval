"""Run one command with process-group-aware timeout and signal cleanup."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess


def terminate(process: subprocess.Popen[bytes], grace: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--grace", type=float, default=30)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    process = subprocess.Popen(command, start_new_session=True)

    def interrupted(signum: int, _frame: object) -> None:
        terminate(process, args.grace)
        raise SystemExit(128 + signum)

    previous = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        try:
            return process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return 124 if terminate(process, args.grace) else 125
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
