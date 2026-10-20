"""Shared process doubles for OpenHands prediction-generation tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import gen_prediction_openhands as gpo


@pytest.fixture(autouse=True)
def isolated_external_runtime_dependencies(monkeypatch):
    """Keep unit-test container doubles independent of Docker dependency I/O."""
    runtime = SimpleNamespace(store="/tmp/fixture-runtime", roots=())
    monkeypatch.setattr(gpo.gp, "stash_solver_runtime_dependencies", lambda *a, **k: runtime)
    monkeypatch.setattr(gpo.gp, "restore_solver_runtime_dependencies", lambda *a, **k: None)
    monkeypatch.setattr(gpo.gp, "remove_solver_runtime_dependencies", lambda *a, **k: None)


def install_fake_openhands_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    times_out: bool = False,
    captured: dict | None = None,
) -> None:
    """Replace the external OpenHands process with a deterministic test double."""
    monkeypatch.setattr(
        gpo,
        "_prepare_openhands_container_guard",
        lambda container_id: "/usr/bin/python3",
    )
    monkeypatch.setattr(
        gpo,
        "_quiesce_openhands_container",
        lambda container_id, python_bin: {
            "proven": True,
            "returncode": 0,
            "error": "",
        },
    )

    class FakeProcess:
        pid = 424242

        def __init__(self, code: int):
            self.returncode = None if times_out else code

        def wait(self, timeout=None):
            if captured is not None:
                captured.setdefault("wait_timeouts", []).append(timeout)
            if times_out:
                raise subprocess.TimeoutExpired("openhands", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            if captured is not None:
                captured.setdefault("signals", []).append(sig)

    def fake_popen(*args, **kwargs):
        if captured is not None:
            captured.update(kwargs)
            captured["command"] = args[0]
        kwargs["stdout"].write(stdout)
        kwargs["stdout"].flush()
        kwargs["stderr"].write(stderr)
        kwargs["stderr"].flush()
        return FakeProcess(returncode)

    monkeypatch.setattr(gpo.subprocess, "Popen", fake_popen)
    if times_out:
        monkeypatch.setattr(
            gpo.container_guard,
            "terminate_supervisor_process",
            lambda process: (
                captured.setdefault("terminated_supervisor", process.pid)
                if captured is not None
                else None
            )
            or "",
        )


def prepare_openhands_main(tmp_path, monkeypatch):
    instance_file = tmp_path / "instance.json"
    instance_file.write_text(
        json.dumps(
            {
                "instance_id": "acme__widget-1",
                "base_commit": "b" * 40,
                "repo": "acme/widget",
                "problem_statement": "Fix the widget.",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gpo.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "container-123",
    )
    monkeypatch.setattr(
        gpo.container_guard,
        "container_image_id",
        lambda container_id: "sha256:" + "8" * 64,
    )
    return instance_file


def trusted_openhands_extraction(snapshot, patch):
    return gpo.gp.gen_prediction_patch.TrustedPatchExtraction(
        fixed_anonymous_base=snapshot.anonymous_head,
        base_tree=snapshot.base_tree,
        baseline_archive_sha256="e" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="f" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        patch_bytes=len(patch.encode()),
        candidate_tree="a" * 40,
        changed_paths=(),
        path_modes=(),
    ).as_dict()
