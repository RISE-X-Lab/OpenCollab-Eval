from __future__ import annotations

import json
from pathlib import Path

from swe_v1_prolite_runner_test_support import os, pytest, runner, subprocess, sys


def test_remote_probe_prefers_linux_proc_start_identity(monkeypatch, tmp_path):
    proc_stat = Path(f"/proc/{os.getpid()}/stat")
    if not proc_stat.is_file():
        pytest.skip("Linux procfs is required")
    raw = proc_stat.read_text(encoding="utf-8")
    remainder = raw.rsplit(")", 1)[1].split()
    start_ticks = int(remainder[19])
    base = tmp_path / "run"
    base.mkdir()
    nonce = "a" * 32
    invocation_id = "b" * 32

    def write_owner(identity: str) -> None:
        (base / "runner.pid").write_text(
            json.dumps(
                {
                    "schema": "opencollab.prolite_runner_owner.v1",
                    "pid": os.getpid(),
                    "start_identity": identity,
                    "owner_nonce": nonce,
                    "claim_sha256": "c" * 64,
                    "invocation_id": invocation_id,
                }
            ),
            encoding="utf-8",
        )

    write_owner(f"proc:{start_ticks}")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ps = fake_bin / "ps"
    fake_ps.write_text("#!/bin/sh\necho 'conflicting ps identity'\n", encoding="utf-8")
    fake_ps.chmod(0o755)
    real_run = subprocess.run

    def run_probe(command, **kwargs):
        return real_run(
            ["sh", "-c", command[-1]],
            env={"PATH": f"{fake_bin}:{Path(sys.executable).parent}:/usr/bin:/bin"},
            text=True,
            capture_output=True,
            timeout=kwargs["timeout"],
            check=False,
        )

    monkeypatch.setattr(runner.subprocess, "run", run_probe)
    arguments = {
        "ssh_command": ["ssh"],
        "host": "remote-host",
        "base_run_dir": str(base),
        "owner_nonce": nonce,
    }
    assert runner.probe_remote_execution_state(**arguments)["runner_state"] == "alive"

    write_owner(f"proc:{start_ticks + 1}")
    assert (
        runner.probe_remote_execution_state(**arguments)["runner_state"]
        == "identity_mismatch"
    )
