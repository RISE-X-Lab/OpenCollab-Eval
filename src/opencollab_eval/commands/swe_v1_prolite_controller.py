"""Top-level remote execution controller for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from opencollab_eval.commands.swe_v1_parent_eval_lock import (
    ParentEvalLock,  # noqa: F401
    parent_eval_lock,  # noqa: F401
    parent_report_lock,  # noqa: F401
)
from opencollab_eval.commands.swe_v1_prolite_common import (
    LOCAL_SPAWN_SIGNALS,
    MAX_TOTAL_EVAL_ATTEMPTS,
    REMOTE_COMPLETION_POLL_SECONDS,
    REPO_ROOT,
    _redacted,
)
from opencollab_eval.commands.swe_v1_prolite_config import (
    ensure_remote_proxy,
    expected_candidate_identity,
    get_proxy_token,
    normalize_workflow_env,
    sync_runtime,
    verify_remote_runtime,
)
from opencollab_eval.commands.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _bounded_remote_communicate,
    _cleanup_remote_execution,
    _local_process_group_exists,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)
from opencollab_eval.commands.swe_v1_prolite_report import eval_only_reconciliation_reports
from opencollab_eval.commands.swe_v1_transport_recovery import (
    RemoteRunnerUnavailable,
    matching_terminal_remote_summary,
    probe_remote_execution_state,
    recover_existing_remote_summary,
    runner_owner_identity,
    wait_for_remote_ownership_fact,
    wait_for_terminal_remote_summary,
)

_SSH_LIVENESS_OPTIONS = (
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3", "-o", "TCPKeepAlive=yes",)

def _ssh_with_liveness_options(command: list[str]) -> list[str]:
    if not command or Path(command[0]).name != "ssh":
        return command
    return [*command, *_SSH_LIVENESS_OPTIONS]


def _install_local_abort_handlers() -> dict[signal.Signals, Any]:
    previous: dict[signal.Signals, Any] = {}

    def abort(signum: int, _frame: object) -> None:
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    for signum in LOCAL_SPAWN_SIGNALS:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, abort)
    return previous


def _restore_local_abort_handlers(previous: dict[signal.Signals, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    abort_signal_state = _install_local_abort_handlers()
    try:
        return _run_remote(args)
    finally:
        _restore_local_abort_handlers(abort_signal_state)


def prepare_runtime_summary(
    args: argparse.Namespace,
    ssh_command: list[str],
    *,
    eval_only: bool,
) -> dict[str, Any]:
    expected = str(getattr(args, "expected_runtime_tree_sha256", "") or "")
    if not args.no_sync_runtime:
        return sync_runtime(
            ssh_command=ssh_command,
            host=args.host,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(getattr(args, "remote_python", "python3")),
        )
    if not expected:
        raise RuntimeError("--no-sync-runtime requires --expected-runtime-tree-sha256")
    observed = verify_remote_runtime(
        ssh_command=ssh_command,
        host=args.host,
        remote_runtime_repo=args.remote_runtime_repo,
        expected=None,
        remote_python=str(getattr(args, "remote_python", "python3")),
    )
    if observed.get("sha256") != expected:
        raise RuntimeError(
            "installed remote runtime source tree does not match the shared preflight"
        )
    return {
        "source_tree": {
            "local": observed,
            "remote": observed,
            "verified": True,
        }
    }


def _remote_payload(
    args: argparse.Namespace,
    *,
    owner_nonce: str,
    invocation_id: str,
    runtime_tree_sha256: str,
    remote_proxy_base_url: str,
) -> dict[str, Any]:
    eval_only = bool(getattr(args, "eval_only", False))
    remote_api_env_file = str(getattr(args, "remote_api_env_file", "") or "").strip()
    return {
        "token": "" if eval_only or remote_api_env_file else get_proxy_token(args.proxy_env_file),
        "remote_api_env_file": remote_api_env_file,
        "llm_transport": "direct" if remote_api_env_file else "reverse_proxy",
        "owner_nonce": owner_nonce,
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "remote_python": str(args.remote_python),
        "base_run_dir": args.base_run_dir,
        "workflow": args.workflow,
        "workflow_env": normalize_workflow_env(args.workflow_env),
        "openhands_command": args.openhands_command,
        "openhands_empty_patch_rejections": max(
            0, args.openhands_empty_patch_rejections
        ),
        "max_empty_patch_retries": min(1, max(0, args.max_empty_patch_retries)),
        "model_name": args.model_name,
        "llm_model": args.llm_model,
        "llm_provider": args.llm_provider,
        "context_window": args.context_window,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_output_tokens": args.max_output_tokens,
        "invocation_id": invocation_id,
        "run_id": args.run_id,
        "runtime_tree_sha256": runtime_tree_sha256,
        "session_prefix": args.session_prefix,
        "image_repository": args.image_repository,
        "remote_proxy_base_url": remote_proxy_base_url,
        "start_index": args.start_index,
        "limit": args.limit,
        "budget": args.budget,
        "max_steps": args.max_steps,
        "swe_timeout": args.swe_timeout,
        "task_wall_timeout": args.task_wall_timeout,
        "eval_timeout": args.eval_timeout,
        "llm_timeout": args.llm_timeout,
        "checkpoint_interval": args.checkpoint_interval,
        "max_task_starts": args.max_task_starts,
        "max_eval_attempts": args.max_eval_attempts,
        "eval_only": eval_only,
        "eval_dir_name": str(getattr(args, "eval_dir_name", "official_eval")),
        **expected_candidate_identity(args),
        "dry_run": args.dry_run,
    }


def _recovery_runtime_tree(observed: dict[str, Any]) -> str:
    owner = observed.get("runner_owner")
    if not isinstance(owner, dict):
        return ""
    value = str(owner.get("runtime_tree_sha256") or "")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _recovery_invocation_id(observed: dict[str, Any]) -> str:
    owner = observed.get("runner_owner")
    if not isinstance(owner, dict):
        return ""
    value = str(owner.get("invocation_id") or "")
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def probe_preexisting_remote_execution(**kwargs: Any) -> dict[str, Any] | None:
    """Probe before runtime or proxy mutation so an old owner remains untouched."""
    return wait_for_remote_ownership_fact(**kwargs)


def _run_remote(args: argparse.Namespace) -> dict[str, Any]:
    defaults = {
        "run_id": "",
        "remote_python": "python3",
        "workflow_env": [],
        "openhands_command": "",
        "openhands_empty_patch_rejections": 2,
        "max_empty_patch_retries": 1,
        "llm_model": "",
        "llm_provider": "anthropic",
        "context_window": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "max_eval_attempts": 2,
        "expected_runtime_tree_sha256": "",
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    ssh_command = _ssh_with_liveness_options(shlex.split(args.ssh_command))
    eval_only = bool(getattr(args, "eval_only", False))
    remote_api_env_file = str(getattr(args, "remote_api_env_file", "") or "").strip()
    completion_deadline = time.monotonic() + max(0.0, args.total_timeout)
    preexisting = probe_preexisting_remote_execution(
        ssh_command=ssh_command,
        host=args.host,
        base_run_dir=args.base_run_dir,
        remote_runtime_repo=args.remote_runtime_repo,
        remote_python=str(args.remote_python),
        deadline=completion_deadline,
    )
    if preexisting is not None and not (
        preexisting.get("runner_state") == "missing"
        and preexisting.get("summary") is None
    ):
        expected_owner = runner_owner_identity(preexisting)
        if expected_owner is None:
            raise RemoteRunnerUnavailable(preexisting)
        runtime_tree_sha256 = _recovery_runtime_tree(preexisting)
        invocation_id = _recovery_invocation_id(preexisting)
        if not runtime_tree_sha256 or not invocation_id:
            raise RemoteRunnerUnavailable(preexisting)
        expected_runtime_tree_sha256 = str(
            getattr(args, "expected_runtime_tree_sha256", "") or ""
        )
        if (
            expected_runtime_tree_sha256
            and runtime_tree_sha256 != expected_runtime_tree_sha256
        ):
            raise RemoteRunnerUnavailable(preexisting)
        payload = _remote_payload(
            args,
            owner_nonce=expected_owner[2],
            invocation_id=invocation_id,
            runtime_tree_sha256=runtime_tree_sha256,
            remote_proxy_base_url=args.remote_proxy_base_url,
        )
        existing_summary = recover_existing_remote_summary(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(args.remote_python),
            payload=payload,
            deadline=completion_deadline,
            expected_owner=expected_owner,
        )
        if existing_summary is not None:
            existing_summary["remote_transport"] = {
                "status": "recovered_terminal_summary",
                "reason": "existing_remote_owner",
                "base_run_dir": args.base_run_dir,
            }
            existing_summary["runtime_sync"] = {
                "status": "skipped_existing_remote_owner",
                "runtime_tree_sha256": runtime_tree_sha256,
            }
            existing_summary["remote_proxy"] = {
                "status": "skipped_existing_remote_owner",
                "remote_proxy_base_url": args.remote_proxy_base_url,
            }
            return existing_summary
    if eval_only:
        proxy_summary = {
            "status": "skipped_eval_only",
            "remote_proxy_base_url": args.remote_proxy_base_url,
        }
    elif remote_api_env_file:
        proxy_summary = {
            "status": "direct_remote_api",
            "remote_proxy_base_url": args.remote_proxy_base_url,
        }
    else:
        proxy_summary = ensure_remote_proxy(
            ssh_command=ssh_command,
            host=args.host,
            local_proxy_base_url=args.local_proxy_base_url,
            remote_proxy_base_url=args.remote_proxy_base_url,
            remote_python=str(args.remote_python),
            enabled=not args.no_ensure_remote_proxy,
        )
    sync_summary = prepare_runtime_summary(
        args,
        ssh_command,
        eval_only=eval_only,
    )
    selected_remote_proxy_base_url = proxy_summary.get("remote_proxy_base_url", args.remote_proxy_base_url)
    source_tree = sync_summary.get("source_tree") if isinstance(sync_summary, dict) else None
    if isinstance(source_tree, dict) and isinstance(source_tree.get("local"), dict):
        source_tree["pre_generation_remote"] = verify_remote_runtime(
            ssh_command=ssh_command,
            host=args.host,
            remote_runtime_repo=args.remote_runtime_repo,
            expected=source_tree["local"],
            remote_python=str(args.remote_python),
        )
    owner_nonce = uuid.uuid4().hex
    payload = _remote_payload(
        args,
        owner_nonce=owner_nonce,
        invocation_id=uuid.uuid4().hex,
        runtime_tree_sha256=(
            str(sync_summary.get("source_tree", {}).get("local", {}).get("sha256") or "")
            if isinstance(sync_summary, dict)
            else ""
        ),
        remote_proxy_base_url=selected_remote_proxy_base_url,
    )
    remote_pythonpath = str(Path(args.remote_runtime_repo) / "src")
    remote_python = str(args.remote_python)
    path_entries = [str(entry) for entry in getattr(args, "remote_path_entry", [])]
    if "/" in remote_python:
        path_entries.insert(0, str(Path(remote_python).parent))
    remote_path = (
        "PATH=" + ":".join(shlex.quote(entry) for entry in path_entries) + ':"$PATH" '
        if path_entries
        else ""
    )
    remote_command = (
        "env "
        + remote_path
        + "PYTHONPATH="
        + shlex.quote(remote_pythonpath)
        + " "
        + shlex.quote(remote_python)
        + " -m opencollab_eval.engine.swe_v1_remote_runner "
        + shlex.quote(owner_nonce)
    )
    command = [*ssh_command, args.host, remote_command]
    primary_failure_detail = ""
    existing_summary = recover_existing_remote_summary(
        ssh_command=ssh_command,
        host=args.host,
        base_run_dir=args.base_run_dir,
        remote_runtime_repo=args.remote_runtime_repo,
        remote_python=str(args.remote_python),
        payload=payload,
        deadline=completion_deadline,
    )
    if existing_summary is not None:
        existing_summary["remote_transport"] = {
            "status": "recovered_terminal_summary",
            "reason": "existing_remote_owner",
            "base_run_dir": args.base_run_dir,
        }
        existing_summary["runtime_sync"] = sync_summary
        existing_summary["remote_proxy"] = proxy_summary
        return existing_summary
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
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
        def recovered(summary: dict[str, Any], reason: str) -> dict[str, Any]:
            if not terminate_local_process_group(proc):
                raise RuntimeError(
                    "remote summary completed but the local SSH process group did not quiesce"
                )
            summary["remote_transport"] = {
                "status": "recovered_terminal_summary",
                "reason": reason,
                "base_run_dir": args.base_run_dir,
            }
            summary["runtime_sync"] = sync_summary
            summary["remote_proxy"] = proxy_summary
            return summary

        def poll_remote_runner() -> None:
            observed = probe_remote_execution_state(
                ssh_command=ssh_command,
                host=args.host,
                base_run_dir=args.base_run_dir,
                remote_runtime_repo=args.remote_runtime_repo,
                remote_python=str(args.remote_python),
                owner_nonce=owner_nonce,
            )
            if observed is not None and observed.get("runner_state") != "alive":
                raise RemoteRunnerUnavailable(observed)

        stdout, stderr = _bounded_remote_communicate(
            proc,
            json.dumps(payload),
            timeout=args.total_timeout,
            poll_interval=REMOTE_COMPLETION_POLL_SECONDS,
            poll_callback=poll_remote_runner,
        )
        if _local_process_group_exists(proc.pid):
            cleanup, interruption = _cleanup_remote_execution(
                ssh_command=ssh_command,
                host=args.host,
                base_run_dir=args.base_run_dir,
                remote_runtime_repo=args.remote_runtime_repo,
                remote_python=str(args.remote_python),
                proc=proc,
            )
            if interruption is not None:
                raise interruption
            if not cleanup.get("ok"):
                raise RuntimeError(
                    "ssh leader exited with residual process-group descendants; "
                    f"technical cleanup failure: {cleanup}"
                )
        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        if result.returncode not in (0, 1, 2):
            primary_failure_detail = _redacted(result.stderr or result.stdout or f"ssh exited {result.returncode}")
            summary = wait_for_terminal_remote_summary(
                ssh_command=ssh_command,
                host=args.host,
                base_run_dir=args.base_run_dir,
                remote_runtime_repo=args.remote_runtime_repo,
                remote_python=str(args.remote_python),
                owner_nonce=owner_nonce,
                payload=payload,
                deadline=completion_deadline,
            )
            if summary is not None:
                return recovered(summary, "primary_transport_lost")
            raise RuntimeError(
                _redacted(result.stderr or result.stdout or f"ssh exited {result.returncode}")
            )
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            primary_failure_detail = _redacted(result.stderr or result.stdout or "remote runner returned no report")
            recovered_summary = wait_for_terminal_remote_summary(
                ssh_command=ssh_command,
                host=args.host,
                base_run_dir=args.base_run_dir,
                remote_runtime_repo=args.remote_runtime_repo,
                remote_python=str(args.remote_python),
                owner_nonce=owner_nonce,
                payload=payload,
                deadline=completion_deadline,
            )
            if recovered_summary is not None:
                return recovered(recovered_summary, "primary_output_invalid")
            raise RuntimeError(
                _redacted(result.stdout[-4000:] or result.stderr[-4000:])
            ) from exc
        summary["runtime_sync"] = sync_summary
        summary["remote_proxy"] = proxy_summary
        return summary
    except RemoteRunnerUnavailable as exc:
        summary = matching_terminal_remote_summary(exc.observed, payload)
        if summary is not None:
            return recovered(summary, "periodic_probe")
        if exc.observed.get("runner_state") in {"invalid", "missing"}:
            terminate_local_process_group(proc)
            detail = f"; startup detail: {primary_failure_detail}" if primary_failure_detail else ""
            raise RuntimeError(
                "remote runner became unavailable because ownership could not be "
                f"verified; refusing remote cleanup{detail}"
            ) from exc
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(args.remote_python),
            proc=proc,
        )
        if interruption is not None:
            raise interruption from exc
        raise RuntimeError(
            f"remote runner became unavailable before a matching terminal summary; "
            f"state={exc.observed.get('runner_state')}; cleanup={cleanup}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        observed = probe_remote_execution_state(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(args.remote_python),
            owner_nonce=owner_nonce,
        )
        recovered_summary = (
            matching_terminal_remote_summary(observed, payload)
            if observed is not None
            else None
        )
        if recovered_summary is not None:
            return recovered(recovered_summary, "primary_timeout")
        cleanup, interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(args.remote_python),
            proc=proc,
        )
        if interruption is not None:
            raise interruption from exc
        raise RuntimeError(f"remote run timed out after {args.total_timeout}s; cleanup={cleanup}") from exc
    except BaseException:
        cleanup, _interruption = _cleanup_remote_execution(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
            remote_runtime_repo=args.remote_runtime_repo,
            remote_python=str(args.remote_python),
            proc=proc,
        )
        print(
            "remote execution aborted; cleanup requested: " + json.dumps(cleanup, ensure_ascii=False),
            file=sys.stderr,
        )
        raise

def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    for result in report.get("results") or []:
        if isinstance(result, dict):
            rows.extend(row for row in result.get("rows") or [] if isinstance(row, dict))
    return rows


def _row_eval_attempt_count(row: dict[str, Any]) -> int:
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    if evaluation.get("executed") is False:
        return 0
    value = evaluation.get("attempt_count")
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in _report_rows(report):
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        counts[index] = counts.get(index, 0) + _row_eval_attempt_count(row)
    return counts


def _final_report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        try:
            index = int(task.get("index"))
            count = int(
                task.get("observed_eval_attempt_count")
                or task.get("eval_attempt_count")
                or 0
            )
        except (TypeError, ValueError):
            continue
        counts[index] = max(counts.get(index, 0), count)
    return counts


def apply_parent_eval_budget(args: argparse.Namespace) -> dict[str, Any] | None:
    if not (args.eval_only and args.parent_output_dir):
        return None
    parent_summary = args.parent_output_dir.resolve() / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")
    report = json.loads(parent_summary.read_text(encoding="utf-8", errors="replace"))
    counts_by_index = _report_task_eval_counts(report)
    final_report_path = args.parent_output_dir.resolve() / "final_eval_layer_report.json"
    final_report_counts: dict[int, int] = {}
    if final_report_path.exists():
        final_report = json.loads(
            final_report_path.read_text(encoding="utf-8", errors="replace")
        )
        final_report_counts = _final_report_task_eval_counts(final_report)
        for index, count in final_report_counts.items():
            counts_by_index[index] = max(counts_by_index.get(index, 0), count)
    selected = range(args.start_index, args.start_index + max(args.limit, 0))
    remaining_by_index = {
        index: MAX_TOTAL_EVAL_ATTEMPTS - counts_by_index.get(index, 0)
        for index in selected
    }
    exhausted = [index for index, remaining in remaining_by_index.items() if remaining <= 0]
    if exhausted:
        joined = ", ".join(str(index) for index in exhausted)
        raise RuntimeError(
            "eval retry budget exhausted for task indices: "
            f"{joined}; max total is {MAX_TOTAL_EVAL_ATTEMPTS}"
        )
    effective_additional_attempts = min(
        args.max_eval_attempts,
        *remaining_by_index.values(),
    )
    projected_total_attempts = max(
        counts_by_index.get(index, 0) + effective_additional_attempts
        for index in selected
    )
    args.max_eval_attempts = projected_total_attempts
    return {
        "max_total_eval_attempts": MAX_TOTAL_EVAL_ATTEMPTS,
        "previous_eval_attempts": counts_by_index,
        "final_report_eval_attempts": final_report_counts,
        "remaining_by_index": remaining_by_index,
        "effective_additional_eval_attempts": effective_additional_attempts,
        "effective_max_eval_attempts": projected_total_attempts,
        "projected_total_eval_attempts": projected_total_attempts,
    }


def update_parent_fact_report(args: argparse.Namespace) -> dict[str, Any]:
    parent_output_dir = args.parent_output_dir.resolve()
    parent_summary = parent_output_dir / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")
    command = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_eval_layer_report",
        "--report-json",
        str(parent_summary),
        "--max-rounds",
        "2",
        "--allow-over-budget-evidence",
        "--json-output",
        str(parent_output_dir / "final_eval_layer_report.json"),
        "--markdown-output",
        str(parent_output_dir / "final_eval_layer_report.md"),
    ]
    for report_path in eval_only_reconciliation_reports(parent_output_dir, args.json_output):
        command.extend(["--report-json", str(report_path)])
    token_cost = parent_output_dir / "parallel_token_cost_summary.json"
    if token_cost.exists():
        command.extend(["--token-cost-json", str(token_cost)])
    if args.usd_cny is not None:
        command.extend(["--usd-cny", str(args.usd_cny)])
    proc = subprocess.run(command, text=True, capture_output=True, cwd=REPO_ROOT)
    log_path = parent_output_dir / "eval_only_reconciliation.log"
    log_path.write_text(proc.stdout + proc.stderr, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"parent fact report failed rc={proc.returncode}; see {log_path}")
    report_path = parent_output_dir / "final_eval_layer_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
    return {
        "status": "done",
        "report_json": str(report_path),
        "report_markdown": str(parent_output_dir / "final_eval_layer_report.md"),
        "counts": report.get("counts") if isinstance(report, dict) else {},
    }


__all__ = [name for name in globals() if not name.startswith("__")]
