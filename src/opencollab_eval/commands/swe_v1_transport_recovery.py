"""Identity-bound recovery for a detached SWE remote runner."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import subprocess
import time
from typing import Any

from opencollab_eval.commands.swe_v1_prolite_common import (
    REMOTE_COMPLETION_POLL_SECONDS,
    REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS,
    REMOTE_TERMINAL_STATUSES,
)
from opencollab_eval.engine.swe_v1_remote_state import (
    DEFAULT_EVAL_CONTAINER_BIND_TIMEOUT_SECONDS,
)
from opencollab_eval.engine.swe_v1_runner_claim import runner_claim_sha256


class RemoteRunnerUnavailable(RuntimeError):
    def __init__(self, observed: dict[str, Any]) -> None:
        super().__init__(f"remote runner unavailable: {observed.get('runner_state')}")
        self.observed = observed


def _probe_timeout_for_remaining(remaining: float) -> float:
    """Keep each recovery probe bounded while honoring the caller deadline."""
    return min(remaining, REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS)


def probe_remote_execution_state(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str = "",
    remote_python: str = "python3",
    owner_nonce: str = "",
    timeout: float | None = None,
) -> dict[str, Any] | None:
    probe = r'''import json,os,pathlib,re,stat,subprocess,sys
base = pathlib.Path(sys.argv[1])
expected_nonce = sys.argv[2]
summary_path = base / "summary.json"
runner_pid_path = base / "runner.pid"

def read_json(path, limit):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, limit + 1)
        current = path.lstat()
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or len(raw) > limit
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise ValueError("file changed while reading")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value

def process_start_identity(pid):
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        if len(remainder) > 19 and remainder[19].isdigit():
            return f"proc:{remainder[19]}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""

try:
    owner = read_json(runner_pid_path, 4096)
    if (
        owner.get("schema") != "opencollab.prolite_runner_owner.v1"
        or isinstance(owner.get("pid"), bool)
        or not isinstance(owner.get("pid"), int)
        or owner["pid"] <= 1
        or not isinstance(owner.get("start_identity"), str)
        or not owner["start_identity"]
        or re.fullmatch(r"[0-9a-f]{32}", str(owner.get("owner_nonce") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(owner.get("claim_sha256") or "")) is None
        or re.fullmatch(r"[0-9a-f]{32}", str(owner.get("invocation_id") or "")) is None
    ):
        raise ValueError("invalid runner owner")
except FileNotFoundError:
    owner = None
    runner_state = "missing"
except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
    owner = None
    runner_state = "invalid"
if owner is not None:
    if expected_nonce and owner["owner_nonce"] != expected_nonce:
        runner_state = "invalid"
    else:
        current_identity = process_start_identity(owner["pid"])
        if current_identity == owner["start_identity"]:
            runner_state = "alive"
        elif current_identity:
            runner_state = "identity_mismatch"
        else:
            runner_state = "dead"
try:
    summary = read_json(summary_path, 16 * 1024 * 1024)
except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
    summary = None
print(json.dumps({"runner_state": runner_state, "runner_owner": owner, "summary": summary}, ensure_ascii=False))
'''
    probe_timeout = (
        REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    )
    if isinstance(probe_timeout, bool):
        raise ValueError("remote probe timeout must be finite and positive")
    try:
        probe_timeout = float(probe_timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("remote probe timeout must be finite and positive") from exc
    if not math.isfinite(probe_timeout) or probe_timeout <= 0:
        raise ValueError("remote probe timeout must be finite and positive")
    remote_interpreter = shlex.quote(remote_python)
    command = [
        *ssh_command,
        host,
        remote_interpreter + " -c " + shlex.quote(probe) + " "
        + shlex.quote(base_run_dir) + " " + shlex.quote(owner_nonce),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=probe_timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if observed.get("runner_state") not in {
        "alive", "dead", "identity_mismatch", "invalid", "missing"
    }:
        return None
    return observed


def probe_terminal_remote_summary(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str = "",
    remote_python: str = "python3",
    owner_nonce: str = "",
) -> dict[str, Any] | None:
    observed = probe_remote_execution_state(
        ssh_command=ssh_command,
        host=host,
        base_run_dir=base_run_dir,
        remote_runtime_repo=remote_runtime_repo,
        remote_python=remote_python,
        owner_nonce=owner_nonce,
    )
    if observed is None or observed.get("runner_state") not in {
        "dead", "identity_mismatch"
    }:
        return None
    summary = observed.get("summary")
    if not isinstance(summary, dict):
        return None
    if summary.get("status") not in REMOTE_TERMINAL_STATUSES:
        return None
    return summary


def wait_for_remote_ownership_fact(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str,
    remote_python: str,
    deadline: float,
) -> dict[str, Any]:
    """Retain the caller's worker slot until remote ownership is observable."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "remote ownership remained unknown until the task deadline"
            )
        observed = probe_remote_execution_state(
            ssh_command=ssh_command,
            host=host,
            base_run_dir=base_run_dir,
            remote_runtime_repo=remote_runtime_repo,
            remote_python=remote_python,
            timeout=_probe_timeout_for_remaining(remaining),
        )
        if observed is not None:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "remote ownership remained unknown until the task deadline"
            )
        time.sleep(min(REMOTE_COMPLETION_POLL_SECONDS, remaining))


def _remote_summary_expectation(payload: dict[str, Any]) -> dict[str, Any]:
    start_index = int(payload["start_index"])
    end_index = start_index + max(int(payload["limit"]), 0) - 1
    expected_slice = (
        str(start_index)
        if end_index <= start_index
        else f"{start_index}-{end_index}"
    )
    expected = {
        "slice": expected_slice,
        "base_run_dir": payload["base_run_dir"],
        "remote_runtime_repo": payload["remote_repo"],
        "remote_python": payload["remote_python"],
        "invocation_id": payload["invocation_id"],
        "workflow": payload["workflow"],
        "workflow_env": payload["workflow_env"],
        "model_name": payload["model_name"],
        "llm_model": payload["llm_model"],
        "llm_provider": payload["llm_provider"],
        "context_window": payload["context_window"],
        "temperature": payload["temperature"],
        "top_p": payload["top_p"],
        "max_output_tokens": payload["max_output_tokens"],
        "budget": payload["budget"],
        "max_steps": payload["max_steps"],
        "max_task_starts": max(0, min(3, int(payload["max_task_starts"]))),
        "max_empty_patch_retries": min(
            1, max(0, int(payload["max_empty_patch_retries"]))
        ),
        "max_eval_attempts": min(2, max(1, int(payload["max_eval_attempts"]))),
        "eval_container_bind_timeout": int(
            payload.get(
                "eval_container_bind_timeout",
                DEFAULT_EVAL_CONTAINER_BIND_TIMEOUT_SECONDS,
            )
        ),
        "eval_only": payload["eval_only"],
        "eval_dir_name": payload["eval_dir_name"],
        "solver_attribution": (
            "historical_artifact" if payload["eval_only"] else "current_run"
        ),
    }
    if payload.get("llm_transport"):
        expected["llm_transport"] = payload["llm_transport"]
    for field in ("run_id", "runtime_tree_sha256"):
        if payload.get(field):
            expected[field] = payload[field]
    if payload.get("workflow") == "openhands-external":
        expected["openhands_empty_patch_rejections"] = max(
            0, int(payload["openhands_empty_patch_rejections"])
        )
        expected["openhands_command_sha256"] = hashlib.sha256(
            payload["openhands_command"].encode("utf-8")
        ).hexdigest()
    return expected


def remote_summary_matches_payload(
    summary: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    expected = _remote_summary_expectation(payload)
    return all(summary.get(key) == value for key, value in expected.items())


def runner_owner_identity(
    observed: dict[str, Any],
) -> tuple[int, str, str, str, str] | None:
    owner = observed.get("runner_owner")
    if not isinstance(owner, dict):
        return None
    pid = owner.get("pid")
    start_identity = owner.get("start_identity")
    owner_nonce = owner.get("owner_nonce")
    claim_sha256 = owner.get("claim_sha256")
    invocation_id = owner.get("invocation_id")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or not isinstance(start_identity, str)
        or not start_identity
        or not isinstance(owner_nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", owner_nonce) is None
        or not isinstance(claim_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", claim_sha256) is None
        or not isinstance(invocation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
    ):
        return None
    return pid, start_identity, owner_nonce, claim_sha256, invocation_id


def matching_terminal_remote_summary(
    observed: dict[str, Any],
    payload: dict[str, Any],
    *,
    expected_owner: tuple[int, str, str, str, str] | None = None,
) -> dict[str, Any] | None:
    if observed.get("runner_state") not in {"dead", "identity_mismatch"}:
        return None
    identity = runner_owner_identity(observed)
    if (
        identity is None
        or (expected_owner is not None and identity != expected_owner)
        or identity[2] != payload.get("owner_nonce")
        or identity[3] != runner_claim_sha256(payload)
        or identity[4] != payload.get("invocation_id")
    ):
        return None
    summary = observed.get("summary")
    if (
        not isinstance(summary, dict)
        or summary.get("status") not in REMOTE_TERMINAL_STATUSES
        or not remote_summary_matches_payload(summary, payload)
    ):
        return None
    return summary


def wait_for_terminal_remote_summary(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str,
    owner_nonce: str,
    payload: dict[str, Any],
    deadline: float,
    remote_python: str = "python3",
    expected_owner: tuple[int, str, str, str, str] | None = None,
) -> dict[str, Any] | None:
    """Keep task ownership while transport is unavailable and await one terminal fact."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        observed = probe_remote_execution_state(
            ssh_command=ssh_command,
            host=host,
            base_run_dir=base_run_dir,
            remote_runtime_repo=remote_runtime_repo,
            remote_python=remote_python,
            owner_nonce=owner_nonce,
            timeout=_probe_timeout_for_remaining(remaining),
        )
        if observed is not None:
            state = observed.get("runner_state")
            if state in {"alive", "dead", "identity_mismatch"}:
                identity = runner_owner_identity(observed)
                if (
                    identity is None
                    or identity[2] != owner_nonce
                    or identity[3] != runner_claim_sha256(payload)
                    or identity[4] != payload.get("invocation_id")
                ):
                    raise RemoteRunnerUnavailable(observed)
                if expected_owner is None:
                    expected_owner = identity
                elif identity != expected_owner:
                    raise RemoteRunnerUnavailable(observed)
            if state in {"dead", "identity_mismatch"}:
                terminal = matching_terminal_remote_summary(
                    observed,
                    payload,
                    expected_owner=expected_owner,
                )
                if terminal is not None:
                    return terminal
                raise RemoteRunnerUnavailable(observed)
            if state != "alive":
                raise RemoteRunnerUnavailable(observed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(REMOTE_COMPLETION_POLL_SECONDS, remaining))


def recover_existing_remote_summary(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    remote_runtime_repo: str,
    remote_python: str,
    payload: dict[str, Any],
    deadline: float,
    expected_owner: tuple[int, str, str, str, str] | None = None,
) -> dict[str, Any] | None:
    """Adopt a matching runner from a lost controller before any new launch."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "remote ownership remained unknown until the task deadline"
            )
        observed = probe_remote_execution_state(
            ssh_command=ssh_command,
            host=host,
            base_run_dir=base_run_dir,
            remote_runtime_repo=remote_runtime_repo,
            remote_python=remote_python,
            timeout=_probe_timeout_for_remaining(remaining),
        )
        if observed is not None:
            state = observed.get("runner_state")
            if state == "missing" and observed.get("summary") is None:
                return None
            if state in {"alive", "dead", "identity_mismatch"}:
                identity = runner_owner_identity(observed)
                if identity is None:
                    raise RemoteRunnerUnavailable(observed)
                if expected_owner is not None and identity != expected_owner:
                    raise RemoteRunnerUnavailable(observed)
                if identity[3] != runner_claim_sha256(payload):
                    raise RemoteRunnerUnavailable(observed)
                if identity[4] != payload.get("invocation_id"):
                    raise RemoteRunnerUnavailable(observed)
                if state == "alive":
                    return wait_for_terminal_remote_summary(
                        ssh_command=ssh_command,
                        host=host,
                        base_run_dir=base_run_dir,
                        remote_runtime_repo=remote_runtime_repo,
                        remote_python=remote_python,
                        owner_nonce=identity[2],
                        payload=payload,
                        deadline=deadline,
                        expected_owner=identity,
                    )
                terminal = matching_terminal_remote_summary(
                    observed,
                    payload,
                    expected_owner=identity,
                )
                if terminal is not None:
                    return terminal
            raise RemoteRunnerUnavailable(observed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "remote ownership remained unknown until the task deadline"
            )
        time.sleep(min(REMOTE_COMPLETION_POLL_SECONDS, remaining))


__all__ = [
    "RemoteRunnerUnavailable",
    "probe_remote_execution_state",
    "probe_terminal_remote_summary",
    "recover_existing_remote_summary",
    "matching_terminal_remote_summary",
    "remote_summary_matches_payload",
    "runner_owner_identity",
    "wait_for_remote_ownership_fact",
    "wait_for_terminal_remote_summary",
]
