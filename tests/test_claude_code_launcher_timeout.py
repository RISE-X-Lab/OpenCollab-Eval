from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from package_test_support import resource_path

SCRIPT = resource_path("run_claude_code_cli.sh")


def test_claude_health_probe_uses_small_monotonic_budget_without_seconds_counter(
    tmp_path: Path,
) -> None:
    """A hung health call is cut off by the per-probe and total monotonic bounds."""

    script = SCRIPT.read_text(encoding="utf-8")
    functions = script[
        script.index("docker_control_with_timeout()") : script.index(
            "# Initialize cleanup state"
        )
    ]
    assert "SECONDS" not in functions
    assert 'docker_health_timeout="${OPENCOLLAB_CLAUDE_DOCKER_HEALTH_TIMEOUT_SECONDS:-5}"' in script
    assert (
        'docker_health_retry_budget="${OPENCOLLAB_CLAUDE_DOCKER_HEALTH_RETRY_BUDGET_SECONDS:-30}"'
        in script
    )
    assert "OPENCOLLAB_DOCKER_DEADLINE_MONOTONIC" in functions

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    harness = tmp_path / "health-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"python_bin={shlex.quote(sys.executable)}\n"
        "docker_control_timeout=10\n"
        "docker_health_timeout=0.2\n"
        "docker_health_retry_budget=0.3\n"
        f"{functions}\n"
        "wait_for_docker_health fake-container 'raise SystemExit(1)'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "FAKE_DOCKER_CALLS": str(calls),
        }
    )
    started = time.monotonic()
    completed = subprocess.run(
        ["bash", str(harness)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert completed.returncode == 124
    assert time.monotonic() - started < 2
    assert "bounded retry budget" in completed.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "exec fake-container python3 -c raise SystemExit(1)"
    ]
