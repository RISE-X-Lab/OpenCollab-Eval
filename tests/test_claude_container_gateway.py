from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from opencollab_eval.generation import claude_container_gateway as gateway

CONTAINER_ID = "c" * 64


def test_gateway_binds_every_command_to_the_offline_task_container(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arguments = tmp_path / "docker-arguments"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" > "$GATEWAY_DOCKER_ARGUMENTS"
printf 'gateway-output\n'
exit 7
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    socket_directory = Path(tempfile.mkdtemp(prefix="oc-gateway-test-", dir="/tmp"))
    socket_path = socket_directory / "gateway.sock"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "GATEWAY_DOCKER_ARGUMENTS": str(arguments),
        }
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "opencollab_eval.generation.claude_container_gateway",
            "--socket",
            str(socket_path),
            "--container",
            CONTAINER_ID,
        ],
        env=environment,
    )
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            assert server.poll() is None
            time.sleep(0.02)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "opencollab_eval.resources.claude_gateway_client",
                str(socket_path),
                "bash",
                "-lc",
                "printf fixed",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    finally:
        server.terminate()
        server.wait(timeout=5)
        socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()

    assert completed.returncode == 7
    assert completed.stdout == "gateway-output\n"
    assert arguments.read_text().strip() == (
        f"exec -w /testbed {CONTAINER_ID} bash -lc printf fixed"
    )


def test_gateway_stops_streaming_output_at_the_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\nhead -c 4096 /dev/zero\n",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1024)

    response = gateway._response(["ignored"], CONTAINER_ID)

    assert response == {
        "returncode": 125,
        "stdout": "",
        "stderr": "command output exceeded limit",
    }


def test_gateway_rejects_a_silent_or_partial_request_after_io_timeout() -> None:
    left, right = socket.socketpair()
    try:
        left.settimeout(0.01)
        started = time.monotonic()
        assert gateway._read_request(left) is None
        assert time.monotonic() - started < 1.0

        right.sendall(b'["unterminated"')
        assert gateway._read_request(left) is None
    finally:
        left.close()
        right.close()


def test_gateway_timeout_is_not_bypassed_after_output_eof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A child that closes both pipes but keeps running must still time out."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import time\n"
        "os.close(1)\n"
        "os.close(2)\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(gateway, "COMMAND_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    response = gateway._response(["ignored"], CONTAINER_ID)
    elapsed = time.monotonic() - started

    assert response == {
        "returncode": 125,
        "stdout": "",
        "stderr": "command timed out",
    }
    assert elapsed < 1.0


def test_gateway_timeout_kills_the_docker_exec_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A timed-out CLI must not leave a docker-exec descendant behind."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "descendant-marker"
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        f"    time.sleep(0.5); open({str(marker)!r}, 'w').write('leaked')\n"
        "else:\n"
        "    os.close(1); os.close(2); time.sleep(2)\n",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(gateway, "COMMAND_TIMEOUT_SECONDS", 0.1)

    response = gateway._response(["ignored"], CONTAINER_ID)
    time.sleep(0.7)

    assert response == {
        "returncode": 125,
        "stdout": "",
        "stderr": "command timed out",
    }
    assert marker.exists() is False


def test_gateway_stop_process_repeats_kill_for_a_late_group_fork(monkeypatch) -> None:
    """A group member appearing after the first signal is killed as well."""

    clock = [100.0]
    signals: list[int] = []

    class FakeProcess:
        pid = 4242
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 137
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = 137

    process = FakeProcess()

    def fake_monotonic() -> float:
        return clock[0]

    def fake_sleep(delay: float) -> None:
        clock[0] += delay

    def fake_killpg(_group_id: int, sent_signal: int) -> None:
        signals.append(sent_signal)
        if sent_signal == 0 and signals.count(signal.SIGKILL) >= 2:
            raise ProcessLookupError

    monkeypatch.setattr(gateway.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gateway.time, "sleep", fake_sleep)
    monkeypatch.setattr(gateway.os, "killpg", fake_killpg)
    monkeypatch.setattr(gateway, "PROCESS_STOP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(gateway, "PROCESS_STOP_POLL_SECONDS", 0.01)

    gateway._stop_process(process)

    assert signals.count(signal.SIGKILL) >= 2
    assert signals.count(0) >= 2
    assert process.returncode == 137
