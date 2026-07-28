from __future__ import annotations

import json

import pytest
from test_swe_g11_parallel_runner import _args, _load_module


def test_remote_health_records_successful_transport_attempt(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return module.subprocess.CompletedProcess(command, 0, "ok\n", "")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_remote_health_checks(config)
    finally:
        module.subprocess.run = old_run

    assert result["attempts"] == [
        {
            "attempt": 1,
            "status": "ok",
            "returncode": 0,
            "failure_kind": "",
            "retried": False,
        }
    ]
    assert calls[0][1]["cwd"] == (
        module.run_remote_health_checks.__globals__["REPO"]
    )


def test_remote_health_retries_pre_session_ssh_failure(tmp_path, monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            return module.subprocess.CompletedProcess(
                command,
                255,
                "",
                "Connection timed out during banner exchange\n",
            )
        return module.subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run_remote_health_checks(config)

    assert len(calls) == 3
    assert [item["retried"] for item in result["attempts"]] == [
        True,
        True,
        False,
    ]
    assert result["attempts"][-1]["status"] == "ok"


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (255, "Permission denied (publickey).\n"),
        (1, "remote command failed\n"),
    ],
)
def test_remote_health_does_not_retry_non_transport_failure(
    tmp_path,
    monkeypatch,
    returncode,
    stderr,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return module.subprocess.CompletedProcess(
            command,
            returncode,
            "",
            stderr,
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(module._shared_health.SharedProbeFailure):
        module.run_remote_health_checks(config)

    assert len(calls) == 1
    evidence = json.loads(
        (tmp_path / "remote_health_check.json").read_text(encoding="utf-8")
    )
    assert evidence["attempts"][0]["failure_kind"] == "non_retryable"
    assert evidence["attempts"][0]["retried"] is False
