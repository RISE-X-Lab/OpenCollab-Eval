"""Bounded local/remote proxy probes and SSH tunnel lifecycle helpers."""

from __future__ import annotations

import atexit
import math
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any

from opencollab_eval.commands.swe_v1_prolite_common import (
    REMOTE_HEALTH_SSH_TIMEOUT_FLOOR,
    REMOTE_PROXY_TUNNELS,
    _redacted,
)
from opencollab_eval.commands.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _ensure_local_process_group_quiesced_after_wait,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)


def _deadline_remaining(deadline: float | None) -> float | None:
    """Return remaining monotonic budget, failing closed when it is spent."""
    if deadline is None:
        return None
    if isinstance(deadline, bool):
        raise ValueError("deadline must be finite")
    try:
        value = float(deadline)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("deadline must be finite") from exc
    if not math.isfinite(value):
        raise ValueError("deadline must be finite")
    remaining = value - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("remote preflight deadline expired")
    return remaining


def url_with_healthz(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    root = urllib.parse.urlunsplit(parsed._replace(path="", query="", fragment="")).rstrip("/")
    return root + "/healthz" if parsed.path.rstrip("/") == "/v1" else base_url.rstrip("/") + "/healthz"


def local_http_ok(
    base_url: str,
    timeout: float = 5.0,
    *,
    deadline: float | None = None,
) -> bool:
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("HTTP probe timeout must be finite and positive") from exc
    if isinstance(timeout, bool) or not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("HTTP probe timeout must be finite and positive")
    remaining = _deadline_remaining(deadline)
    timeout = min(timeout_value, remaining) if remaining is not None else timeout_value
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def remote_http_ok(
    *,
    ssh_command: list[str],
    host: str,
    base_url: str,
    remote_python: str = "python3",
    timeout: float = 10,
    deadline: float | None = None,
) -> bool:
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("HTTP probe timeout must be finite and positive") from exc
    if isinstance(timeout, bool) or not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("HTTP probe timeout must be finite and positive")
    remaining = _deadline_remaining(deadline)
    timeout = min(timeout_value, remaining) if remaining is not None else timeout_value
    probe = "import sys,urllib.request;urllib.request.urlopen(sys.argv[1], timeout=" + str(timeout) + ").read()"
    remote_command = f"{shlex.quote(remote_python)} -c {shlex.quote(probe)} {shlex.quote(url_with_healthz(base_url))}"
    try:
        outer_timeout = max(REMOTE_HEALTH_SSH_TIMEOUT_FLOOR, timeout + 8)
        if remaining is not None:
            outer_timeout = min(outer_timeout, remaining)
        result = subprocess.run(
            [*ssh_command, host, remote_command],
            text=True,
            capture_output=True,
            timeout=outer_timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def loopback_port(base_url: str, *, default: int | None = None) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if parsed.port is None:
        if default is None:
            raise RuntimeError(f"proxy URL must include an explicit port: {base_url}")
        return int(default)
    return int(parsed.port)


def loopback_url_with_port(base_url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if host == "::1":
        netloc = f"[::1]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def remote_forward_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "remote port forwarding failed" in lowered
        or "address already in use" in lowered
        or "cannot listen to port" in lowered
    )


def stop_remote_proxy_tunnel(proc: subprocess.Popen[str]) -> bool:
    return terminate_local_process_group(proc)


def cleanup_remote_proxy_tunnels() -> None:
    for proc in list(REMOTE_PROXY_TUNNELS):
        try:
            cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        except BaseException:
            cleanup_quiesced = False
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)


atexit.register(cleanup_remote_proxy_tunnels)


def start_remote_proxy_tunnel(
    command: list[str], *, deadline: float | None = None
) -> tuple[subprocess.Popen[str] | None, str]:
    _deadline_remaining(deadline)
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    REMOTE_PROXY_TUNNELS.append(proc)
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        sleep_for = 0.2
        remaining = _deadline_remaining(deadline)
        if remaining is not None:
            sleep_for = min(sleep_for, remaining)
        time.sleep(sleep_for)
        if proc.poll() is not None:
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_quiesced = terminate_local_process_group(proc)
                if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
                    REMOTE_PROXY_TUNNELS.remove(proc)
                return None, "ssh tunnel output drain timed out"
            cleanup_quiesced = _ensure_local_process_group_quiesced_after_wait(proc)
            if cleanup_quiesced:
                REMOTE_PROXY_TUNNELS.remove(proc)
            else:
                return (
                    None,
                    "ssh tunnel leader exited with residual process-group descendants that could not be cleaned",
                )
            message = _redacted(stderr or stdout or f"{command[0]} exited {proc.returncode}")
            return None, message
        return proc, ""
    except BaseException:
        cleanup_quiesced = False
        try:
            cleanup_quiesced = terminate_local_process_group(proc)
        except BaseException:
            pass
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        raise


def ensure_remote_proxy(
    *,
    ssh_command: list[str],
    host: str,
    local_proxy_base_url: str,
    remote_proxy_base_url: str,
    remote_python: str = "python3",
    enabled: bool,
    deadline: float | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    probe_kwargs: dict[str, Any] = {
        "ssh_command": ssh_command,
        "host": host,
        "base_url": remote_proxy_base_url,
        "remote_python": remote_python,
    }
    if deadline is not None:
        probe_kwargs["deadline"] = deadline
    if remote_http_ok(**probe_kwargs):
        return {"status": "already_healthy", "remote_proxy_base_url": remote_proxy_base_url}
    local_kwargs: dict[str, Any] = {"base_url": local_proxy_base_url}
    if deadline is not None:
        local_kwargs["deadline"] = deadline
    if not local_http_ok(**local_kwargs):
        raise RuntimeError(f"local proxy health check failed: {url_with_healthz(local_proxy_base_url)}")
    local_port = loopback_port(local_proxy_base_url)
    remote_port = loopback_port(remote_proxy_base_url)
    attempts: list[str] = []
    for candidate_port in range(remote_port, remote_port + 21):
        _deadline_remaining(deadline)
        candidate_base_url = loopback_url_with_port(remote_proxy_base_url, candidate_port)
        forward = f"127.0.0.1:{candidate_port}:127.0.0.1:{local_port}"
        command = [
            *ssh_command,
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            forward,
            host,
        ]
        tunnel_kwargs = {} if deadline is None else {"deadline": deadline}
        proc, message = start_remote_proxy_tunnel(command, **tunnel_kwargs)
        if proc is None:
            attempts.append(f"{candidate_port}: {message}")
            if remote_forward_port_conflict(message):
                health_kwargs = {
                    "ssh_command": ssh_command,
                    "host": host,
                    "base_url": candidate_base_url,
                    "remote_python": remote_python,
                    "timeout": 2,
                }
                if deadline is not None:
                    health_kwargs["deadline"] = deadline
                if remote_http_ok(**health_kwargs):
                    return {
                        "status": "already_healthy",
                        "remote_proxy_base_url": candidate_base_url,
                        "selected_remote_port": candidate_port,
                    }
                continue
            raise RuntimeError(message)
        for _ in range(6):
            health_kwargs = {
                "ssh_command": ssh_command,
                "host": host,
                "base_url": candidate_base_url,
                "remote_python": remote_python,
                "timeout": 2,
            }
            if deadline is not None:
                health_kwargs["deadline"] = deadline
            if remote_http_ok(**health_kwargs):
                return {
                    "status": "started" if candidate_port == remote_port else "started_fallback_port",
                    "local_proxy_base_url": local_proxy_base_url,
                    "remote_proxy_base_url": candidate_base_url,
                    "forward": forward,
                    "selected_remote_port": candidate_port,
                }
            remaining = _deadline_remaining(deadline)
            time.sleep(0.5 if remaining is None else min(0.5, remaining))
        cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        if not cleanup_quiesced:
            raise RuntimeError(f"remote proxy tunnel on port {candidate_port} did not stop")
        attempts.append(f"{candidate_port}: tunnel started but health check failed")
    detail = "; ".join(attempts[-5:])
    raise RuntimeError(f"remote proxy tunnel did not become healthy near port {remote_port}: {detail}")


__all__ = [name for name in globals() if not name.startswith("__")]
