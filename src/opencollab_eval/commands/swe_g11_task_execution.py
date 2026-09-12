"""Single work-item execution for the G1.1 parallel controller."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_g11_config as _config
from opencollab_eval.commands import swe_g11_parallel_process as _parallel_process
from opencollab_eval.commands.swe_g11_technical_queue import RecoveryAttempt

ParallelConfig = _config.ParallelConfig


def task_paths(
    config: ParallelConfig,
    index: int,
    attempt: RecoveryAttempt | None = None,
) -> dict[str, Path]:
    if attempt is not None:
        return {
            "json_report": attempt.json_report,
            "markdown_report": attempt.markdown_report,
            "stdout_log": attempt.stdout_log,
            "stderr_log": attempt.stderr_log,
        }
    return {
        "json_report": config.output_dir / f"task_{index}_report.json",
        "markdown_report": config.output_dir / f"task_{index}_report.md",
        "stdout_log": config.output_dir / f"task_{index}.stdout.log",
        "stderr_log": config.output_dir / f"task_{index}.stderr.log",
    }


def _max_task_starts(config: ParallelConfig, attempt: RecoveryAttempt | None) -> int:
    if attempt is None:
        return config.max_task_starts
    return 0 if attempt.mode == "eval_only" else 1


def task_command(
    config: ParallelConfig,
    index: int,
    attempt: RecoveryAttempt | None = None,
) -> list[str]:
    paths = task_paths(config, index, attempt)
    run_id = attempt.run_id if attempt is not None else f"{config.run_id}_task{index}"
    base_run_dir = attempt.base_run_dir if attempt is not None else f"{config.remote_base}/task_{index}"
    command = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_g11_prolite_runner",
        "--host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--runner-transport",
        getattr(config, "runner_transport", "ssh"),
        "--remote-python",
        config.remote_python,
        "--remote-root",
        config.remote_root,
        "--image-repository",
        config.image_repository,
        "--run-id",
        run_id,
        "--session-prefix",
        (attempt.session_prefix(config.session_prefix) if attempt is not None else config.session_prefix),
        "--model-name",
        config.model_name,
        "--llm-provider",
        config.llm_provider,
        "--start-index",
        str(index),
        "--limit",
        "1",
        "--base-run-dir",
        base_run_dir,
        "--remote-runtime-repo",
        config.remote_runtime_repo,
        "--workflow",
        config.workflow,
        "--remote-proxy-base-url",
        config.remote_proxy_base_url,
        "--budget",
        str(config.budget),
        "--max-steps",
        str(config.max_steps),
        "--openhands-empty-patch-rejections",
        str(config.openhands_empty_patch_rejections),
        "--max-empty-patch-retries",
        str(config.max_empty_patch_retries),
        "--swe-timeout",
        str(config.swe_timeout),
        "--task-wall-timeout",
        str(config.task_wall_timeout),
        "--eval-timeout",
        str(config.eval_timeout),
        "--eval-container-bind-timeout",
        str(config.eval_container_bind_timeout),
        "--llm-timeout",
        str(config.llm_timeout),
        "--checkpoint-interval",
        str(config.checkpoint_interval),
        "--max-task-starts",
        str(_max_task_starts(config, attempt)),
        "--max-eval-attempts",
        str(attempt.max_eval_attempts if attempt is not None else config.max_eval_attempts),
        "--total-timeout",
        str(config.total_timeout),
        "--json-output",
        str(paths["json_report"]),
        "--markdown-output",
        str(paths["markdown_report"]),
    ]
    if attempt is not None and attempt.mode == "eval_only":
        pass
    elif config.remote_api_env_file:
        command += ["--remote-api-env-file", config.remote_api_env_file]
    else:
        command += [
            "--local-proxy-base-url",
            config.local_proxy_base_url,
            "--proxy-env-file",
            str(config.proxy_env_file),
        ]
    for option, value in (
        ("--llm-model", config.llm_model),
        ("--context-window", config.context_window),
        ("--temperature", config.temperature),
        ("--top-p", config.top_p),
        ("--max-output-tokens", config.max_output_tokens),
    ):
        if value not in (None, ""):
            command += [option, str(value)]
    for item in config.workflow_env:
        command += ["--workflow-env", item]
    if config.openhands_command:
        command += ["--openhands-command", config.openhands_command]
    if config.no_sync_runtime:
        command += [
            "--no-sync-runtime",
            "--expected-runtime-tree-sha256",
            config.runtime_tree_sha256,
        ]
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    if attempt is not None and attempt.mode == "eval_only":
        assert attempt.candidate is not None
        command += [
            "--eval-only",
            "--expected-task",
            attempt.candidate.task,
            "--expected-record-id",
            attempt.candidate.record_id,
            "--expected-source-patch-sha256",
            attempt.candidate.source_patch_sha256,
            "--expected-eval-patch-sha256",
            attempt.candidate.eval_patch_sha256,
            "--eval-dir-name",
            attempt.eval_dir_name,
            "--parent-output-dir",
            str(config.output_dir),
            "--eval-only-source-base-run-dir",
            attempt.candidate.base_run_dir,
            "--defer-parent-fact-report",
        ]
    if config.dry_run:
        command.append("--dry-run")
    return command


def _identity_overrides(attempt: RecoveryAttempt | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    values = {
        "base_run_dir": attempt.base_run_dir,
        "eval_only": attempt.mode == "eval_only",
        "solver_attribution": ("historical_artifact" if attempt.mode == "eval_only" else "current_run"),
        "max_task_starts": 0 if attempt.mode == "eval_only" else 1,
        "max_eval_attempts": attempt.max_eval_attempts,
        "run_id": attempt.run_id,
    }
    if attempt.candidate is not None:
        values["eval_only_source_base_run_dir"] = attempt.candidate.base_run_dir
    return values


def task_result_from_summary(
    config: ParallelConfig,
    index: int,
    summary: dict[str, Any],
    *,
    reused: bool,
    elapsed: float,
    process_returncode: int | None = None,
    attempt: RecoveryAttempt | None = None,
) -> dict[str, Any]:
    paths = task_paths(config, index, attempt)
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    rows = summary.get("rows") if isinstance(summary.get("rows"), list) else []
    status = str(summary.get("status") or "")
    reasons = list(
        _config.single_task_summary_validation_reasons(
            summary,
            config,
            index,
            identity_overrides=_identity_overrides(attempt),
        )
    )
    expected_returncodes = {
        "done": 0,
        "done_with_technical_failures": 1,
        "preflight_failed": 2,
        "invalid_config": 2,
    }
    expected_returncode = expected_returncodes.get(status)
    if expected_returncode is None:
        reasons.append("nonterminal_runner_status")
    actual_returncode = expected_returncode if process_returncode is None else process_returncode
    if isinstance(actual_returncode, bool) or not isinstance(actual_returncode, int):
        reasons.append("invalid_runner_returncode")
    elif expected_returncode is not None and actual_returncode != expected_returncode:
        reasons.append("returncode_status_conflict")
    accepted_counts = {field: counts.get(field, 0) for field in _config.SINGLE_TASK_COUNT_FIELDS}
    if reasons:
        accepted_counts = dict.fromkeys(_config.SINGLE_TASK_COUNT_FIELDS, 0)
        accepted_counts["technical_failed"] = 1
    failure_scope = str(summary.get("failure_scope") or "")
    if failure_scope not in {"task", "image", "shared_infrastructure"}:
        failure_scope = "task" if reasons or accepted_counts["technical_failed"] else "none"
    failure_probe = summary.get("failure_probe") if isinstance(summary.get("failure_probe"), dict) else {}
    if failure_scope == "shared_infrastructure" and not (
        failure_probe.get("direct") is True and failure_probe.get("status") == "failed"
    ):
        failure_scope = "task"
        failure_probe = {}
    return {
        "index": index,
        "returncode": actual_returncode if actual_returncode is not None else 1,
        "elapsed_seconds": round(elapsed, 1),
        "json_report": str(paths["json_report"]),
        "markdown_report": str(paths["markdown_report"]),
        "stdout_log": str(paths["stdout_log"]),
        "stderr_log": str(paths["stderr_log"]),
        "runner_status": status,
        **{field: accepted_counts[field] for field in _config.SINGLE_TASK_COUNT_FIELDS},
        "rows": rows,
        "completed": not reasons,
        "summary_validation_reasons": reasons,
        "reused_existing_report": reused,
        "failure_scope": failure_scope,
        "failure_probe": failure_probe,
        "base_run_dir": str(summary.get("base_run_dir") or ""),
        "run_id": str(summary.get("run_id") or ""),
    }


def run_one(
    config: ParallelConfig,
    index: int,
    attempt: RecoveryAttempt | None,
    *,
    run_task_process: Callable[[list[str]], subprocess.CompletedProcess[str]],
    load_json: Callable[[Path], dict[str, Any]],
    write_text: Callable[[Path, str], None],
    ensure_directory: Callable[[Path], None],
) -> dict[str, Any]:
    started = time.time()
    paths = task_paths(config, index, attempt)
    if paths["json_report"].exists():
        summary = load_json(paths["json_report"])
        if _config.report_is_reusable(
            summary,
            config,
            index,
            identity_overrides=_identity_overrides(attempt),
            allow_technical=config.max_technical_recoveries > 0,
        ):
            result = task_result_from_summary(
                config,
                index,
                summary,
                reused=True,
                elapsed=0.0,
                attempt=attempt,
            )
            result["attempts"] = 0
            return result

    ensure_directory(paths["stdout_log"].parent)
    proc: subprocess.CompletedProcess[str] | None = None
    for runner_attempt in range(1, config.runner_attempts + 1):
        proc = run_task_process(task_command(config, index, attempt))
        write_text(paths["stdout_log"], proc.stdout)
        write_text(paths["stderr_log"], proc.stderr)
        summary = load_json(paths["json_report"])
        if not summary:
            return {
                "index": index,
                "returncode": proc.returncode,
                "elapsed_seconds": round(time.time() - started, 1),
                "json_report": str(paths["json_report"]),
                "markdown_report": str(paths["markdown_report"]),
                "stdout_log": str(paths["stdout_log"]),
                "stderr_log": str(paths["stderr_log"]),
                "runner_status": "missing_report",
                "completed": False,
                "attempts": runner_attempt,
                "tasks": 0,
                "generation_done": 0,
                "empty_patch": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
                "failure_scope": "task",
                "failure_probe": {},
                "base_run_dir": attempt.base_run_dir if attempt is not None else "",
                "run_id": attempt.run_id if attempt is not None else "",
            }
        result = task_result_from_summary(
            config,
            index,
            summary,
            reused=False,
            elapsed=time.time() - started,
            process_returncode=proc.returncode,
            attempt=attempt,
        )
        result["attempts"] = runner_attempt
        if (
            str(summary.get("status") or "") not in _config.RETRYABLE_TASK_REPORT_STATUSES
            or runner_attempt >= config.runner_attempts
        ):
            return result
        if runner_attempt < config.runner_attempts and config.retry_delay_seconds:
            if _parallel_process.interrupted():
                break
            time.sleep(config.retry_delay_seconds * runner_attempt)
        elif _parallel_process.interrupted():
            break

    assert proc is not None
    return {
        "index": index,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 1),
        "json_report": str(paths["json_report"]),
        "markdown_report": str(paths["markdown_report"]),
        "stdout_log": str(paths["stdout_log"]),
        "stderr_log": str(paths["stderr_log"]),
        "runner_status": "missing_report",
        "completed": False,
        "attempts": config.runner_attempts,
        "tasks": 0,
        "generation_done": 0,
        "empty_patch": 0,
        "eval_done": 0,
        "eval_attempts": 0,
        "eval_retry_tasks": 0,
        "resolved": 0,
        "unresolved": 0,
        "technical_failed": 1,
        "failure_scope": "task",
        "failure_probe": {},
        "base_run_dir": attempt.base_run_dir if attempt is not None else "",
        "run_id": attempt.run_id if attempt is not None else "",
    }


__all__ = ["run_one", "task_command", "task_paths", "task_result_from_summary"]
