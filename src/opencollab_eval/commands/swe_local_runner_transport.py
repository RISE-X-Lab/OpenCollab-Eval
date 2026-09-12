"""Direct local transport for a server-resident SWE runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from opencollab_eval.commands.swe_v1_prolite_common import _redacted
from opencollab_eval.commands.swe_v1_prolite_config import (
    local_http_ok,
    verify_runtime_manifest,
)
from opencollab_eval.commands.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _bounded_remote_communicate,
    _local_process_group_exists,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)
from opencollab_eval.engine import swe_v1_remote_cleanup as remote_cleanup


def probe_local_execution_state(
    args: Any,
    *,
    owner_nonce: str = "",
) -> dict[str, Any]:
    """Read the same ownership facts locally without an SSH handshake."""
    base = Path(args.base_run_dir)
    try:
        owner = remote_cleanup.read_runner_owner(base / "runner.pid")
        if (
            re.fullmatch(r"[0-9a-f]{64}", str(owner.get("claim_sha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{32}", str(owner.get("invocation_id") or "")) is None
        ):
            raise remote_cleanup.CleanupInputError("runner owner identity is invalid")
    except FileNotFoundError:
        owner = None
        runner_state = "missing"
    except (OSError, remote_cleanup.CleanupInputError):
        owner = None
        runner_state = "invalid"
    if owner is not None:
        if owner_nonce and owner["owner_nonce"] != owner_nonce:
            runner_state = "invalid"
        else:
            current_identity = remote_cleanup.process_start_identity(int(owner["pid"]), [])
            if current_identity == owner["start_identity"]:
                runner_state = "alive"
            elif current_identity:
                runner_state = "identity_mismatch"
            else:
                runner_state = "dead"
    try:
        summary = remote_cleanup.read_bounded_json(
            base / "summary.json",
            max_bytes=16 * 1024 * 1024,
        )
    except (FileNotFoundError, OSError, remote_cleanup.CleanupInputError):
        summary = None
    return {
        "runner_state": runner_state,
        "runner_owner": owner,
        "summary": summary,
    }


def prepare_local_runtime_summary(args: Any) -> dict[str, Any]:
    """Verify the already installed runtime used by a local worker."""
    runtime_repo = Path(args.remote_runtime_repo)
    observed = verify_runtime_manifest(runtime_repo)
    expected = str(getattr(args, "expected_runtime_tree_sha256", "") or "")
    if expected and observed.get("sha256") != expected:
        raise RuntimeError("installed local runtime source tree does not match the shared preflight")
    return {
        "transport": "local",
        "remote_runtime_repo": str(runtime_repo),
        "source_tree": {
            "local": observed,
            "remote": observed,
            "verified": True,
        },
    }


def prepare_local_proxy_summary(args: Any) -> dict[str, Any]:
    """Use the server-local relay directly without creating an SSH tunnel."""
    base_url = str(args.remote_proxy_base_url)
    if not local_http_ok(base_url):
        raise RuntimeError(f"local worker proxy health check failed: {base_url}")
    return {
        "status": "already_healthy",
        "transport": "local",
        "remote_proxy_base_url": base_url,
    }


def local_runner_command(args: Any, owner_nonce: str) -> tuple[list[str], dict[str, str]]:
    """Build the direct worker command and its server-local runtime environment."""
    runtime_repo = Path(args.remote_runtime_repo)
    remote_python = str(args.remote_python)
    command = [
        remote_python,
        "-m",
        "opencollab_eval.engine.swe_v1_remote_runner",
        owner_nonce,
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(runtime_repo / "src")
    path_entries = [str(entry) for entry in getattr(args, "remote_path_entry", [])]
    if "/" in remote_python:
        path_entries.insert(0, str(Path(remote_python).parent))
    if path_entries:
        current_path = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join([*path_entries, current_path])
    return command, environment


def _cleanup_local_runner(args: Any, proc: subprocess.Popen[str]) -> dict[str, Any]:
    _runner_command, environment = local_runner_command(args, "cleanup")
    command = [
        str(args.remote_python),
        "-m",
        "opencollab_eval.engine.swe_v1_remote_cleanup",
        str(args.base_run_dir),
    ]
    cleanup_result: dict[str, Any]
    try:
        result = subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        try:
            detail = json.loads(result.stdout)
        except json.JSONDecodeError:
            detail = {
                "stdout": _redacted(result.stdout),
                "stderr": _redacted(result.stderr),
            }
        cleanup_result = {"returncode": result.returncode, "detail": detail}
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup_result = {"returncode": None, "error_type": type(exc).__name__}
    cleanup_result["process_group_quiesced"] = terminate_local_process_group(proc)
    return cleanup_result


def run_local_runner(
    args: Any,
    *,
    owner_nonce: str,
    payload: dict[str, Any],
    runtime_summary: dict[str, Any],
    proxy_summary: dict[str, Any],
) -> dict[str, Any]:
    """Run the worker as an owned local subprocess and return its terminal report."""
    command, environment = local_runner_command(args, owner_nonce)
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        stdout, stderr = _bounded_remote_communicate(
            proc,
            json.dumps(payload),
            timeout=args.total_timeout,
        )
        if _local_process_group_exists(proc.pid) and not terminate_local_process_group(proc):
            raise RuntimeError("local runner exited with residual process-group descendants")
        if proc.returncode not in (0, 1, 2):
            raise RuntimeError(_redacted(stderr or stdout or f"local runner exited {proc.returncode}"))
        try:
            summary = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                _redacted(stdout[-4000:] or stderr[-4000:] or "local runner returned no report")
            ) from exc
        summary["runtime_sync"] = runtime_summary
        summary["remote_proxy"] = proxy_summary
        summary["runner_transport"] = "local"
        summary["remote_transport"] = {
            "status": "local_subprocess",
            "base_run_dir": args.base_run_dir,
        }
        return summary
    except BaseException as exc:
        cleanup = _cleanup_local_runner(args, proc)
        if not cleanup.get("process_group_quiesced"):
            raise RuntimeError(f"local runner cleanup did not quiesce its process group: {cleanup}") from exc
        raise


__all__ = [
    "local_runner_command",
    "prepare_local_proxy_summary",
    "prepare_local_runtime_summary",
    "probe_local_execution_state",
    "run_local_runner",
]
