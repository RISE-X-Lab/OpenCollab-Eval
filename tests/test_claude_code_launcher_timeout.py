from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from package_test_support import resource_path

SCRIPT = resource_path("run_claude_code_cli.sh")


def _sha256_harness(tmp_path: Path, *, path: str) -> subprocess.CompletedProcess[str]:
    script = SCRIPT.read_text(encoding="utf-8")
    functions = script[
        script.index("sha256_stdin()") : script.index(
            "if ! validate_positive_timeout"
        )
    ]
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"opencollab\x00portable-hash\n")
    harness = tmp_path / "sha256-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"python_bin={shlex.quote(sys.executable)}\n"
        f"{functions}\n"
        f"sha256_file {shlex.quote(str(sample))}\n"
        f"printf '%s' {shlex.quote('stdin payload')} | sha256_stdin\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", str(harness)],
        env={**os.environ, "PATH": path},
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )


def test_claude_sha256_helpers_use_macos_shasum_without_gnu_sha256sum(
    tmp_path: Path,
) -> None:
    completed = _sha256_harness(tmp_path, path="/usr/bin:/bin")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        hashlib.sha256(b"opencollab\x00portable-hash\n").hexdigest(),
        hashlib.sha256(b"stdin payload").hexdigest(),
    ]


def test_claude_sha256_helpers_fall_back_to_required_python(
    tmp_path: Path,
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    completed = _sha256_harness(tmp_path, path=str(empty_bin))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        hashlib.sha256(b"opencollab\x00portable-hash\n").hexdigest(),
        hashlib.sha256(b"stdin payload").hexdigest(),
    ]


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
        # The values remain far below the production defaults, but leave
        # process-startup headroom on a busy macOS/CI host.
        "docker_control_timeout=10\n"
        "docker_health_timeout=0.5\n"
        "docker_health_retry_budget=1.5\n"
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
    assert time.monotonic() - started < 2.5
    assert "bounded retry budget" in completed.stderr
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert 1 <= len(call_lines) <= 3
    assert set(call_lines) == {"exec fake-container python3 -c raise SystemExit(1)"}


def test_claude_health_probe_retries_docker_status_125(tmp_path: Path) -> None:
    """Docker's transient daemon/runtime status 125 is not a watchdog failure."""

    script = SCRIPT.read_text(encoding="utf-8")
    functions = script[
        script.index("docker_control_with_timeout()") : script.index(
            "# Initialize cleanup state"
        )
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    state = tmp_path / "first-call"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "if [[ ! -e \"$FAKE_DOCKER_STATE\" ]]; then\n"
        "  : > \"$FAKE_DOCKER_STATE\"\n"
        "  exit 125\n"
        "fi\n"
        "exit 0\n",
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
        # Leave startup headroom on busy CI/macOS hosts; the fake daemon still
        # exercises the same two-call retry path immediately.
        "docker_health_retry_budget=3\n"
        f"{functions}\n"
        "wait_for_docker_health fake-container 'raise SystemExit(0)'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "FAKE_DOCKER_CALLS": str(calls),
            "FAKE_DOCKER_STATE": str(state),
        }
    )
    completed = subprocess.run(
        ["bash", str(harness)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "exec fake-container python3 -c raise SystemExit(0)",
        "exec fake-container python3 -c raise SystemExit(0)",
    ]
    assert "could not be reaped" not in completed.stderr


def test_claude_cleanup_retries_transient_daemon_inspection_errors(
    tmp_path: Path,
) -> None:
    """A transient daemon error must not turn a successful cleanup into 125."""

    script = SCRIPT.read_text(encoding="utf-8")
    cleanup_functions = script[
        script.index("remove_container_and_prove_absent()") : script.index(
            "cleanup()"
        )
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    container_state = tmp_path / "container-transient-seen"
    network_state = tmp_path / "network-transient-seen"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "if [[ \"$1\" == container && \"$2\" == inspect ]]; then\n"
        "  if [[ ! -e \"$FAKE_CONTAINER_STATE\" ]]; then\n"
        "    : > \"$FAKE_CONTAINER_STATE\"\n"
        "    echo 'Cannot connect to the Docker daemon' >&2\n"
        "    exit 125\n"
        "  fi\n"
        "  echo 'Error: No such container' >&2\n"
        "  exit 1\n"
        "fi\n"
        "if [[ \"$1\" == network && \"$2\" == inspect ]]; then\n"
        "  if [[ ! -e \"$FAKE_NETWORK_STATE\" ]]; then\n"
        "    : > \"$FAKE_NETWORK_STATE\"\n"
        "    echo 'Cannot connect to the Docker daemon' >&2\n"
        "    exit 125\n"
        "  fi\n"
        "  echo 'Error: No such network' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    harness = tmp_path / "cleanup-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "docker_control() { docker \"$@\"; }\n"
        f"{cleanup_functions}\n"
        "if ! remove_container_and_prove_absent transient-container; then\n"
        "  exit 1\n"
        "fi\n"
        "if ! remove_network_and_prove_absent transient-network; then\n"
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "FAKE_DOCKER_CALLS": str(calls),
            "FAKE_CONTAINER_STATE": str(container_state),
            "FAKE_NETWORK_STATE": str(network_state),
        }
    )
    completed = subprocess.run(
        ["bash", str(harness)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert completed.returncode == 0, completed.stderr
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert call_lines.count("container inspect transient-container") == 2
    assert call_lines.count("network inspect transient-network") == 2


def test_claude_cleanup_recovers_id_from_cidfile_after_run_failure(
    tmp_path: Path,
) -> None:
    """A daemon-created detached container is removed even when run returned no ID."""

    script = SCRIPT.read_text(encoding="utf-8")
    cleanup_functions = script[
        script.index("cleanup_container_by_id_name_or_cidfile()") : script.index(
            "cleanup()"
        )
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "if [[ \"$1\" == container && \"$2\" == inspect ]]; then\n"
        "  echo 'Error: No such container' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    cidfile = tmp_path / "runtime-container.id"
    recovered_id = "a" * 64
    cidfile.write_text(recovered_id + "\n", encoding="ascii")
    harness = tmp_path / "cleanup-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "docker_control() { docker \"$@\"; }\n"
        f"{cleanup_functions}\n"
        f"cleanup_container_by_id_name_or_cidfile '' runtime-name {shlex.quote(str(cidfile))}\n",
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
    completed = subprocess.run(
        ["bash", str(harness)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert completed.returncode == 0, completed.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"rm -f {recovered_id}",
        f"container inspect {recovered_id}",
    ]
