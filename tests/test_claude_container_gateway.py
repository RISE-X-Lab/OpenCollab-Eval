from __future__ import annotations

import os
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
