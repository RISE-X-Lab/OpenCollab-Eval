from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from opencollab_eval.engine.swe_v1_remote_pytest_controller import (
    prolite_pytest_controller_source,
)
from opencollab_eval.engine.swe_v1_remote_pytest_proof import (
    prolite_pytest_proof_plugin_source,
)


def test_trusted_worker_cannot_be_shadowed_by_candidate_module(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    repo = tmp_path / "repo"
    trusted.mkdir()
    repo.mkdir()
    controller = trusted / "controller.py"
    plugin = trusted / "opencollab_pytest_proof.py"
    controller.write_text(prolite_pytest_controller_source(), encoding="utf-8")
    plugin.write_text(prolite_pytest_proof_plugin_source(), encoding="utf-8")
    marker = tmp_path / "candidate-loaded"
    (repo / "opencollab_pytest_proof.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )
    (repo / "test_target.py").write_text(
        "def test_target():\n    assert True\n", encoding="utf-8"
    )
    read_fd, write_fd = os.pipe()
    environment = os.environ.copy()
    environment["OPENCOLLAB_PYTEST_EVENT_FD"] = str(write_fd)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                    (
                        "import sys;sys.path.insert(0,sys.argv[1]);p=sys.argv[2];"
                        "sys.argv=[p,*sys.argv[3:]];"
                        "exec(compile(open(p,'rb').read(),p,'exec'))"
                    ),
                str(trusted),
                str(controller),
                "--trusted-pytest-worker",
                str(plugin),
                "--",
                "-q",
                "-o",
                "addopts=",
                "test_target.py::test_target",
            ],
            cwd=repo,
            env=environment,
            pass_fds=(write_fd,),
            text=True,
            capture_output=True,
            timeout=60,
        )
    finally:
        os.close(write_fd)
    raw = b""
    while chunk := os.read(read_fd, 65536):
        raw += chunk
    os.close(read_fd)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    events = [json.loads(line) for line in raw.decode().splitlines()]
    assert events[0]["event"] == "session_start"
    assert events[-1] == {"event": "session_finish", "exitstatus": 0}
