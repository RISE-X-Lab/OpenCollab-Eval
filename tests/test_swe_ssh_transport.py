from __future__ import annotations

import subprocess

import pytest
from swe_v1_prolite_runner_test_support import runner

from opencollab_eval.commands.swe_ssh_transport import CheckedCommandError


def test_ssh_checked_retries_only_pre_session_transport(monkeypatch):
    calls = []
    sleeps = []

    def run_checked(command, *, timeout=120, input_text=None):
        calls.append(command)
        if len(calls) < 3:
            raise CheckedCommandError(
                command,
                subprocess.CompletedProcess(
                    command,
                    255,
                    "",
                    "kex_exchange_identification: read: Connection reset by peer\n",
                ),
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    evidence = []

    result = runner.run_ssh_checked(
        ["ssh", "remote-host", "true"],
        retry_log=evidence,
    )

    assert result.returncode == 0
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert [item["retried"] for item in evidence] == [True, True, False]


def test_runtime_sync_retries_pre_session_mkdir_and_install(monkeypatch):
    attempts = {"mkdir": 0, "install": 0}

    def run_checked(command, *, timeout=120, input_text=None):
        if command[0] == "rsync":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-1].startswith("mkdir -p -- "):
            phase = "mkdir"
        elif "target_moved=0" in command[-1]:
            phase = "install"
        else:
            return subprocess.CompletedProcess(command, 0, "", "")
        attempts[phase] += 1
        if attempts[phase] == 1:
            raise CheckedCommandError(
                command,
                subprocess.CompletedProcess(
                    command,
                    255,
                    "",
                    "Connection timed out during banner exchange\n",
                ),
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    assert attempts == {"mkdir": 2, "install": 2}
    assert [
        item["retried"]
        for item in summary["ssh_transport_attempts"]["mkdir"]
    ] == [True, False]
    assert [
        item["retried"]
        for item in summary["ssh_transport_attempts"]["install"]
    ] == [True, False]


def test_runtime_sync_retries_pre_session_archive_transfer(monkeypatch):
    transfers = []

    def run_checked(command, *, timeout=120, input_text=None):
        if command[0] == "rsync":
            transfers.append(command)
            if len(transfers) < 3:
                raise CheckedCommandError(
                    command,
                    subprocess.CompletedProcess(
                        command,
                        255,
                        "",
                        "kex_exchange_identification: Connection reset by peer\n",
                    ),
                )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    assert len(transfers) == 3
    assert all("--partial" in command for command in transfers)
    assert [
        item["retried"]
        for item in summary["ssh_transport_attempts"]["transfer"]
    ] == [True, True, False]


def test_runtime_sync_cleans_archive_after_transport_retry_exhaustion(monkeypatch):
    transfers = []
    cleanup_commands = []

    def run_checked(command, *, timeout=120, input_text=None):
        if command[0] == "rsync":
            transfers.append(command)
            raise CheckedCommandError(
                command,
                subprocess.CompletedProcess(
                    command,
                    255,
                    "",
                    "Connection timed out during banner exchange\n",
                ),
            )
        if command[0] == "ssh" and command[-1].startswith("rm -f -- "):
            cleanup_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    with pytest.raises(CheckedCommandError):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert len(transfers) == 3
    assert len(cleanup_commands) == 1


def test_runtime_sync_cleanup_timeout_preserves_transfer_error(monkeypatch):
    transfer_error = CheckedCommandError(
        ["rsync"],
        subprocess.CompletedProcess(
            ["rsync"],
            255,
            "",
            "Permission denied (publickey).\n",
        ),
    )

    def run_checked(command, *, timeout=120, input_text=None):
        if command[0] == "rsync":
            raise transfer_error
        if command[0] == "ssh" and command[-1].startswith("rm -f -- "):
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)

    with pytest.raises(CheckedCommandError) as exc:
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert exc.value is transfer_error


@pytest.mark.parametrize(
    "failure",
    [
        CheckedCommandError(
            ["rsync"],
            subprocess.CompletedProcess(
                ["rsync"],
                255,
                "",
                "Permission denied (publickey).\n",
            ),
        ),
        CheckedCommandError(
            ["rsync"],
            subprocess.CompletedProcess(
                ["rsync"],
                1,
                "",
                "remote rsync failed\n",
            ),
        ),
        subprocess.TimeoutExpired(["rsync"], 300),
    ],
)
def test_runtime_sync_does_not_retry_unproven_transfer_failure(
    monkeypatch,
    failure,
):
    transfers = []

    def run_checked(command, *, timeout=120, input_text=None):
        if command[0] == "rsync":
            transfers.append(command)
            raise failure
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)

    with pytest.raises((CheckedCommandError, subprocess.TimeoutExpired)):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert len(transfers) == 1


def test_ssh_checked_records_timeout_without_retry(monkeypatch):
    calls = []

    def run_checked(command, *, timeout=120, input_text=None):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(runner, "run_checked", run_checked)
    evidence = []

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_ssh_checked(
            ["ssh", "remote-host", "true"],
            retry_log=evidence,
        )

    assert len(calls) == 1
    assert evidence == [
        {
            "attempt": 1,
            "status": "failed",
            "returncode": None,
            "failure_kind": "command_timeout",
            "retried": False,
        }
    ]


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (255, "Permission denied (publickey).\n"),
        (1, "remote command failed\n"),
    ],
)
def test_ssh_checked_rejects_non_transport_failure_without_retry(
    monkeypatch,
    returncode,
    stderr,
):
    calls = []

    def run_checked(command, *, timeout=120, input_text=None):
        calls.append(command)
        raise CheckedCommandError(
            command,
            subprocess.CompletedProcess(
                command,
                returncode,
                "",
                stderr,
            ),
        )

    monkeypatch.setattr(runner, "run_checked", run_checked)

    with pytest.raises(CheckedCommandError):
        runner.run_ssh_checked(["ssh", "remote-host", "true"])

    assert len(calls) == 1
