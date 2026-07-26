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
    if sudo and subprocess.run([sudo, "-n", "true"], capture_output=True).returncode == 0:
        return [sudo, "-n"]
    return None


ROOT_PREFIX = _root_prefix()
pytestmark = pytest.mark.skipif(ROOT_PREFIX is None, reason="Linux root is required")


def _run_case(
    tmp_path: Path,
    source: str,
    request: pytest.FixtureRequest,
    *,
    candidate_layout: str | None = None,
):
    root = Path("/tmp") / f"oc-pytest-controller-{os.getpid()}-{tmp_path.name}"
    request.addfinalizer(
        lambda: subprocess.run(
            [*(ROOT_PREFIX or []), "rm", "-rf", "--", str(root)], capture_output=True
        )
    )
    plugin_dir, output_dir, repo = root / "input", root / "output", root / "repo"
    for path in (plugin_dir, output_dir, repo):
        path.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o1777)
    controller = plugin_dir / "controller.py"
    controller.write_text(prolite_pytest_controller_source(), encoding="utf-8")
    controller.chmod(0o755)
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        prolite_pytest_proof_plugin_source(), encoding="utf-8"
    )
    (repo / "opencollab_pytest_proof.py").write_text(
        "raise RuntimeError('candidate plugin loaded')\n",
        encoding="utf-8",
    )
    package = repo / "local_package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    candidate_args = []
    if candidate_layout is not None:
        dependency_root = repo / candidate_layout if candidate_layout else repo
        dependency = dependency_root / "iniconfig"
        dependency.mkdir(parents=True)
        (dependency / "__init__.py").write_text("ORIGIN = 'candidate'\n", encoding="utf-8")
        relative = f"{candidate_layout}/" if candidate_layout else ""
        candidate_args = [
            "--candidate-source-path",
            relative + "iniconfig/__init__.py",
        ]
    (repo / "test_target.py").write_text(source, encoding="utf-8")
    proof = output_dir / "proof.jsonl"
    worker = [
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
    digest = hashlib.sha256("\0".join(worker).encode()).hexdigest()
    result = subprocess.run(
        [
            *(ROOT_PREFIX or []),
            sys.executable,
            str(controller),
            "--proof-output",
            str(proof),
            "--command-sha256",
            digest,
            "--plugin-dir",
            str(plugin_dir),
            "--output-root",
            str(output_dir),
            *candidate_args,
            "--",
            *worker,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return result, proof


def test_controller_publishes_complete_low_privilege_pass(tmp_path, request):
    result, proof = _run_case(
        tmp_path,
        "from local_package import VALUE\ndef test_target():\n    assert VALUE == 42\n",
        request,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in proof.read_text().splitlines()]
    assert events[0]["controller"]["worker_uid"] != events[0]["controller"]["controller_uid"]
    assert events[-1]["controller"]["termination"] == "normal_protocol_eof"
    assert proof.parent.stat().st_mode & 0o1777 == 0o1777


def test_controller_preserves_assertion_failure(tmp_path, request):
    result, proof = _run_case(tmp_path, "def test_target():\n    assert False\n", request)

    assert result.returncode == 1
    events = [json.loads(line) for line in proof.read_text().splitlines()]
    assert any(
        event.get("event") == "runtest_logreport"
        and event.get("when") == "call"
        and event.get("outcome") == "failed"
        for event in events
    )


@pytest.mark.parametrize("layout", ["", "src", "lib"])
def test_controller_reloads_candidate_module_preloaded_by_pytest(tmp_path, request, layout):
    result, proof = _run_case(
        tmp_path,
        "from iniconfig import ORIGIN\ndef test_target():\n    assert ORIGIN == 'candidate'\n",
        request,
        candidate_layout=layout,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert proof.exists()


def test_controller_rejects_abrupt_zero_exit(tmp_path, request):
    result, proof = _run_case(
        tmp_path,
        "import os\ndef test_target():\n    os._exit(0)\n",
        request,
    )

    assert result.returncode == 86
    assert not proof.exists()
