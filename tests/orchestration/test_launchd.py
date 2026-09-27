from __future__ import annotations

import subprocess
from pathlib import Path

from opencollab_eval.commands import _launchd


def test_bootstrap_launch_agent_retries_transient_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bootstrap_results = iter([1, 0])
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            next(bootstrap_results) if arguments[0] == "bootstrap" else 1,
            stdout="",
            stderr="Bootstrap failed: 5: Input/output error",
        )

    monkeypatch.setattr(_launchd.time, "sleep", lambda _seconds: None)
    _launchd.bootstrap_launch_agent(
        target="gui/501/com.example.proxy",
        installed_path=tmp_path / "proxy.plist",
        launchctl=fake_launchctl,
    )

    assert [call[0] for call in calls] == ["bootstrap", "print", "bootstrap"]
