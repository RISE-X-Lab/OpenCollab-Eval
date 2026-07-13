"""Shared process doubles for OpenHands prediction-generation tests."""

from __future__ import annotations

import subprocess

import pytest

from opencollab_eval.generation import gen_prediction_openhands as gpo


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
