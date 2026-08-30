from __future__ import annotations

import math
import shlex
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_swe_eval_run_entry import _load_entry_module

from opencollab_eval.commands import ssh_reverse_proxy as srp


def test_stale_remote_relay_cleanup_requires_an_owned_socket() -> None:
    socket_path = "/tmp/opencollab-llmproxy-18790.sock"
    remote_command = srp._cleanup_command(socket_path)
    assert socket_path in remote_command
    assert "stat.S_ISSOCK" in remote_command
    assert "info.st_uid == os.getuid()" in remote_command
    assert "errno.ECONNREFUSED" in remote_command
    assert "os.unlink(path)" in remote_command
    assert subprocess.run(["sh", "-n", "-c", remote_command], check=False).returncode == 0


def test_remote_cleanup_does_not_unlink_socket_replaced_after_probe() -> None:
    """A pathname replacement between connect and unlink must be preserved."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "relay.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(candidate))
        stale.close()

        command = shlex.split(srp._cleanup_command(str(candidate)))
        assert command[:2] == ["python3", "-c"]
        marker = (
            "if exc.errno not in {errno.ECONNREFUSED,errno.ENOENT}: "
            "raise SystemExit(4)"
        )
        assert marker in command[2]
        replacement_hook = "\n".join(
            (
                marker,
                "    os.unlink(path)",
                "    replacement=socket.socket(socket.AF_UNIX)",
                "    replacement.bind(path)",
                "    replacement.close()",
            )
        )
        probe = command[2].replace(marker, replacement_hook, 1)
        result = subprocess.run(
            [command[0], command[1], probe, str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 5
        assert candidate.exists()
        candidate.unlink()


def test_stale_remote_relay_cleanup_rejects_an_unowned_path(monkeypatch: Any) -> None:
    module = _load_entry_module()

    def reject(**_kwargs: Any) -> None:
        raise RuntimeError("remote proxy socket cannot be safely replaced")

    monkeypatch.setattr(module, "wait_for_remote_socket_release", reject)

    with pytest.raises(RuntimeError, match="cannot be safely replaced"):
        module._remove_stale_remote_proxy_socket(
            ssh_command="ssh",
            host="host",
            socket_path="/tmp/opencollab-llmproxy-18790.sock",
        )


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_optimized_remote_cleanup_preserves_non_sockets(
    tmp_path: Path, kind: str
) -> None:
    candidate = tmp_path / "relay.sock"
    if kind == "regular":
        candidate.write_text("owned data", encoding="utf-8")
    else:
        target = tmp_path / "target"
        target.write_text("owned data", encoding="utf-8")
        candidate.symlink_to(target)
    result = subprocess.run(
        ["env", "PYTHONOPTIMIZE=1", "sh", "-c", srp._cleanup_command(str(candidate))],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    assert candidate.exists() or candidate.is_symlink()


def test_remote_cleanup_removes_only_a_refused_unix_socket() -> None:
    import socket

    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "relay.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(candidate))
        stale.close()

        result = subprocess.run(
            ["sh", "-c", srp._cleanup_command(str(candidate))],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert not candidate.exists()


def test_remote_cleanup_preserves_a_live_unix_socket() -> None:
    import socket

    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "relay.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(candidate))
        listener.listen()
        try:
            result = subprocess.run(
                ["sh", "-c", srp._cleanup_command(str(candidate))],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            listener.close()

        assert result.returncode == 5
        assert candidate.exists()


def test_restartable_proxy_cleans_before_exec(monkeypatch: Any) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        srp,
        "remove_stale_remote_socket",
        lambda **kwargs: calls.append(("cleanup", kwargs)),
    )
    monkeypatch.setattr(
        srp.os,
        "execvp",
        lambda executable, command: calls.append((executable, command)),
    )

    result = srp.main(
        [
            "--ssh-command",
            "/usr/bin/ssh -p 2222",
            "--host",
            "worker",
            "--local-port",
            "8890",
            "--remote-port",
            "18891",
            "--remote-socket",
            "/tmp/opencollab-llmproxy-18891.sock",
        ]
    )

    assert result == 127
    assert calls[0] == (
        "cleanup",
        {
            "ssh_command": "/usr/bin/ssh -p 2222",
            "host": "worker",
            "socket_path": "/tmp/opencollab-llmproxy-18891.sock",
        },
    )
    executable, command = calls[1]
    assert executable == "/usr/bin/ssh"
    assert "StreamLocalBindUnlink=yes" in command
    assert "127.0.0.1:18891:127.0.0.1:8890" in command
    assert "/tmp/opencollab-llmproxy-18891.sock:127.0.0.1:8890" in command


def test_restartable_proxy_waits_for_a_live_socket(monkeypatch: Any) -> None:
    outcomes: list[Exception | None] = [
        srp.RemoteSocketStillActive("live"),
        srp.RemoteSocketStillActive("live"),
        None,
    ]
    sleeps: list[float] = []

    def cleanup(**_kwargs: Any) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(srp, "remove_stale_remote_socket", cleanup)
    monkeypatch.setattr(srp.time, "sleep", sleeps.append)

    srp.wait_for_remote_socket_release(
        ssh_command="ssh",
        host="worker",
        socket_path="/tmp/opencollab-llmproxy-18891.sock",
    )

    assert sleeps == [1.0, 2.0]
    assert outcomes == []


def test_restartable_proxy_waits_through_an_ssh_outage(monkeypatch: Any) -> None:
    outcomes: list[Exception | None] = [
        srp.RemoteSocketProbeUnavailable("offline"),
        None,
    ]

    def cleanup(**_kwargs: Any) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(srp, "remove_stale_remote_socket", cleanup)
    monkeypatch.setattr(srp.time, "sleep", lambda _seconds: None)

    srp.wait_for_remote_socket_release(
        ssh_command="ssh",
        host="worker",
        socket_path="/tmp/opencollab-llmproxy-18891.sock",
    )

    assert outcomes == []


def test_restartable_proxy_clamps_remote_probe_to_remaining_budget(monkeypatch: Any) -> None:
    calls: list[dict[str, object]] = []

    def cleanup(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(srp, "remove_stale_remote_socket", cleanup)

    srp.wait_for_remote_socket_release(
        ssh_command="ssh",
        host="worker",
        socket_path="/tmp/opencollab-llmproxy-18891.sock",
        timeout_seconds=0.5,
    )

    assert len(calls) == 1
    assert 0 < float(calls[0]["probe_timeout_seconds"]) <= 0.5


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("timeout_seconds", True),
        ("timeout_seconds", "nan"),
        ("timeout_seconds", math.nan),
        ("timeout_seconds", math.inf),
        ("timeout_seconds", 0.0),
        ("initial_delay_seconds", math.nan),
        ("initial_delay_seconds", "0"),
        ("initial_delay_seconds", math.inf),
        ("initial_delay_seconds", 0.0),
        ("maximum_delay_seconds", math.nan),
        ("maximum_delay_seconds", math.inf),
        ("maximum_delay_seconds", 0.0),
    ],
)
def test_restartable_proxy_rejects_nonfinite_or_nonpositive_wait_values(
    argument: str, value: float
) -> None:
    kwargs: dict[str, Any] = {
        "ssh_command": "ssh",
        "host": "worker",
        "socket_path": "/tmp/opencollab-llmproxy-18891.sock",
    }
    kwargs[argument] = value
    with pytest.raises(ValueError, match="finite and positive"):
        srp.wait_for_remote_socket_release(**kwargs)


def test_restartable_proxy_does_not_retry_an_unsafe_path(monkeypatch: Any) -> None:
    def reject(**_kwargs: Any) -> None:
        raise RuntimeError("remote proxy socket cannot be safely replaced")

    monkeypatch.setattr(srp, "remove_stale_remote_socket", reject)
    monkeypatch.setattr(
        srp.time,
        "sleep",
        lambda _seconds: pytest.fail("unsafe paths must fail without retrying"),
    )

    with pytest.raises(RuntimeError, match="cannot be safely replaced"):
        srp.wait_for_remote_socket_release(
            ssh_command="ssh",
            host="worker",
            socket_path="/tmp/opencollab-llmproxy-18891.sock",
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--remote-socket", "/tmp/unrelated.sock"], "does not match"),
        (["--host", "bad host"], "SSH destination"),
        (["--ssh-command", ""], "SSH command is empty"),
    ],
)
def test_restartable_proxy_rejects_unsafe_identity(
    arguments: list[str], message: str
) -> None:
    defaults = {
        "--ssh-command": "/usr/bin/ssh",
        "--host": "worker",
        "--local-port": "8890",
        "--remote-port": "18891",
        "--remote-socket": "/tmp/opencollab-llmproxy-18891.sock",
    }
    defaults.update(dict(zip(arguments[::2], arguments[1::2], strict=True)))
    argv = [item for pair in defaults.items() for item in pair]

    with pytest.raises(SystemExit, match=message):
        srp.main(argv)


def test_persistent_relay_adds_private_unix_forward(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    written: list[dict[str, Any]] = []
    tcp_health = iter([False, True])
    monkeypatch.setattr(
        module,
        "_ensure_local_proxy_agent",
        lambda **_kwargs: {"status": "already_healthy"},
    )
    monkeypatch.setattr(
        module,
        "_remote_proxy_healthy",
        lambda **_kwargs: next(tcp_health),
    )
    monkeypatch.setattr(module, "_remote_proxy_socket_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(module, "_remove_stale_remote_proxy_socket", lambda **_kwargs: None)
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *args, **_kwargs: SimpleNamespace(returncode=1 if args[0] == "print" else 0),
    )
    monkeypatch.setattr(module, "_write_plist", lambda _path, payload: written.append(payload))
    monkeypatch.setattr(module.shutil, "copy2", lambda _source, _target: None)

    result = module._ensure_proxy_agent(
        output_dir=tmp_path,
        remaining=[
            "--host",
            "host",
            "--local-proxy-base-url",
            "http://127.0.0.1:8880",
            "--remote-proxy-base-url",
            "http://127.0.0.1:18790",
        ],
        upstream_base_url="https://api.example.invalid/v1",
    )

    assert result["status"] == "started"
    program = written[0]["ProgramArguments"]
    assert program[1:3] == ["-m", "opencollab_eval.commands.ssh_reverse_proxy"]
    assert program[program.index("--local-port") + 1] == "8880"
    assert program[program.index("--remote-port") + 1] == "18790"
    assert program[program.index("--remote-socket") + 1] == (
        "/tmp/opencollab-llmproxy-18790.sock"
    )
