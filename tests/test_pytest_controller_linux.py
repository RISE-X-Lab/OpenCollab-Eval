from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab_eval.engine.swe_v1_remote_pytest_controller import (
    prolite_pytest_controller_source,
)
from opencollab_eval.engine.swe_v1_remote_pytest_proof import (
    prolite_pytest_proof_plugin_source,
)


def _root_prefix() -> list[str] | None:
    if sys.platform != "linux":
        return None
    if os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo is None:
        return None
    probe = subprocess.run([sudo, "-n", "true"], capture_output=True, check=False)
    return [sudo, "-n"] if probe.returncode == 0 else None


ROOT_PREFIX = _root_prefix()
pytestmark = pytest.mark.skipif(
    ROOT_PREFIX is None,
    reason="Linux root privilege is required for the controller/worker identity test",
)


def _controller_case(
    tmp_path: Path,
    source: str,
    request: pytest.FixtureRequest,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    case_root = Path("/tmp") / f"oc-pytest-controller-{os.getpid()}-{tmp_path.name}"
    request.addfinalizer(
        lambda: subprocess.run(
            [*(ROOT_PREFIX or []), "rm", "-rf", "--", str(case_root)],
            capture_output=True,
            check=False,
        )
    )
    plugin_dir = case_root / "input"
    output_dir = case_root / "output"
    repo_dir = case_root / "repo"
    plugin_dir.mkdir(parents=True)
    output_dir.mkdir()
    repo_dir.mkdir()
    controller = plugin_dir / "controller.py"
    controller.write_text(prolite_pytest_controller_source(), encoding="utf-8")
    controller.chmod(0o755)
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        prolite_pytest_proof_plugin_source(),
        encoding="utf-8",
    )
    (repo_dir / "test_target.py").write_text(source, encoding="utf-8")
    proof = output_dir / "proof.jsonl"
    worker_command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "opencollab_pytest_proof",
        "-q",
        "-o",
        "addopts=",
        "test_target.py::test_target",
    ]
    command_sha = hashlib.sha256("\0".join(worker_command).encode()).hexdigest()
    command = [
        *(ROOT_PREFIX or []),
        sys.executable,
        str(controller),
        "--proof-output",
        str(proof),
        "--command-sha256",
        command_sha,
        "--plugin-dir",
        str(plugin_dir),
        "--output-root",
        str(output_dir),
        "--",
        *worker_command,
    ]
    result = subprocess.run(
        command,
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result, proof


def test_pytest_controller_publishes_only_a_complete_low_privilege_run(tmp_path, request):
    result, proof = _controller_case(tmp_path, "def test_target():\n    assert True\n", request)

    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in proof.read_text(encoding="utf-8").splitlines()]
    controller = events[0]["controller"]
    assert controller["schema"] == "opencollab.pytest_controller.v1"
    assert controller["worker_uid"] != controller["controller_uid"]
    assert events[-1]["controller"]["termination"] == "normal_protocol_eof"
    assert proof.parent.stat().st_mode & 0o777 == 0o700


def test_pytest_controller_preserves_structured_assertion_failure(tmp_path, request):
    result, proof = _controller_case(tmp_path, "def test_target():\n    assert False\n", request)

    assert result.returncode == 1
    events = [json.loads(line) for line in proof.read_text(encoding="utf-8").splitlines()]
    assert any(
        event.get("event") == "runtest_logreport"
        and event.get("when") == "call"
        and event.get("outcome") == "failed"
        for event in events
    )
    assert events[-1]["controller"]["worker_returncode"] == 1


def test_pytest_controller_rejects_proof_rewrite_and_abrupt_zero_exit(tmp_path, request):
    source = (
        "import json\n"
        "import os\n\n"
        "def test_target():\n"
        "    path = os.environ.get('OPENCOLLAB_PYTEST_PROOF_PATH')\n"
        "    if path:\n"
        "        with open(path, 'w', encoding='utf-8') as handle:\n"
        "            json.dump({'forged': True}, handle)\n"
        "    os._exit(0)\n"
    )
    result, proof = _controller_case(tmp_path, source, request)

    assert result.returncode == 86
    assert "protocol" in result.stderr
    assert not proof.exists()
