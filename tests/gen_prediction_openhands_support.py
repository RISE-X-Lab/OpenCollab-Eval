"""Shared process doubles for OpenHands prediction-generation tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_openhands as gpo


def write_openhands_state(output_dir: Path, model: str = "provider/model") -> None:
    state_dir = output_dir / "persistence" / "conversations" / "conversation-1"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "base_state.json").write_text(
        json.dumps(
            {
                "stats": {
                    "usage_to_metrics": {
                        "agent": {
                            "accumulated_token_usage": {
                                "prompt_tokens": 20,
                                "completion_tokens": 5,
                                "cache_read_tokens": 0,
                                "cache_write_tokens": 0,
                            },
                            "token_usages": [
                                {
                                    "model": model,
                                    "prompt_tokens": 20,
                                    "completion_tokens": 5,
                                    "response_id": "response-1",
                                }
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )


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
