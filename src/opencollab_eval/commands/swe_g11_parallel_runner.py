#!/usr/bin/env python3
"""Parameterized G1.1 Pro-Lite parallel runner."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_g11_config as _config
from opencollab_eval.commands import _swe_g11_reports as _reports
from opencollab_eval.commands import swe_g11_parallel_process as _parallel_process
from opencollab_eval.commands import swe_g11_shared_health as _shared_health
from opencollab_eval.commands.swe_v1_prolite_common import (
    ALLOWED_WORKFLOW_ENV_KEYS as _ALLOWED_WORKFLOW_ENV_KEYS,
)
from opencollab_eval.commands.swe_v1_prolite_config import get_proxy_token

# Preserve the original import surface while keeping implementation modules focused.
REPO = _config.REPO
DEFAULT_REMOTE_ROOT = _config.DEFAULT_REMOTE_ROOT
DEFAULT_EVAL_WORK_ROOT = _config.DEFAULT_EVAL_WORK_ROOT
DEFAULT_MODEL_NAME = _config.DEFAULT_MODEL_NAME
ALLOWED_WORKFLOW_ENV_KEYS = _ALLOWED_WORKFLOW_ENV_KEYS
ParallelConfig = _config.ParallelConfig
SchedulerState = _config.SchedulerState
RETRYABLE_TASK_REPORT_STATUSES = _config.RETRYABLE_TASK_REPORT_STATUSES
_safe_slug = _config._safe_slug
_openhands_command_sha256 = _config._openhands_command_sha256
_snapshot_evidence_valid = _config._snapshot_evidence_valid
parse_indices = _config.parse_indices
range_label = _config.range_label
normalize_workflow_env = _config.normalize_workflow_env
default_run_id = _config.default_run_id
resolve_config = _config.resolve_config
report_is_reusable = _config.report_is_reusable
single_task_summary_validation_reasons = (
    _config.single_task_summary_validation_reasons
)
normalize_legacy_empty_patch_summary = _config.normalize_legacy_empty_patch_summary
result_resource_reasons = _config.result_resource_reasons
update_scheduler_state = _config.update_scheduler_state
systemic_failure_reasons = _config.systemic_failure_reasons
scheduler_snapshot = _config.scheduler_snapshot

write_json = _reports.write_json
write_text = _reports.write_text
ensure_directory = _reports.ensure_directory
load_json = _reports.load_json
_compact_token_summary = _reports._compact_token_summary
build_token_summary = _reports.build_token_summary
build_eval_fact_report = _reports.build_eval_fact_report
aggregate = _reports.aggregate
compact_progress = _reports.compact_progress
write_markdown = _reports.write_markdown
save_progress = _reports.save_progress


def _run_task_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    return _parallel_process.run_task_process(command, cwd=REPO)


def task_paths(config: ParallelConfig, index: int) -> dict[str, Path]:
    return {
        "json_report": config.output_dir / f"task_{index}_report.json",
        "markdown_report": config.output_dir / f"task_{index}_report.md",
        "stdout_log": config.output_dir / f"task_{index}.stdout.log",
        "stderr_log": config.output_dir / f"task_{index}.stderr.log",
    }


def task_command(config: ParallelConfig, index: int) -> list[str]:
    paths = task_paths(config, index)
    command = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_g11_prolite_runner",
        "--host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--remote-python",
        config.remote_python,
        "--remote-root",
        config.remote_root,
        "--image-repository",
        config.image_repository,
        "--run-id",
        f"{config.run_id}_task{index}",
        "--session-prefix",
        config.session_prefix,
        "--model-name",
        config.model_name,
        "--llm-provider",
        config.llm_provider,
        "--start-index",
        str(index),
        "--limit",
        "1",
        "--base-run-dir",
        f"{config.remote_base}/task_{index}",
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
        "--llm-timeout",
        str(config.llm_timeout),
        "--provider-error-time-budget",
        str(config.provider_error_time_budget),
        "--checkpoint-interval",
        str(config.checkpoint_interval),
        "--max-task-starts",
        str(config.max_task_starts),
        "--max-eval-attempts",
        str(config.max_eval_attempts),
        "--total-timeout",
        str(config.total_timeout),
        "--json-output",
        str(paths["json_report"]),
        "--markdown-output",
        str(paths["markdown_report"]),
    ]
    if config.remote_api_env_file:
        command += ["--remote-api-env-file", config.remote_api_env_file]
    else:
        command += [
            "--local-proxy-base-url", config.local_proxy_base_url,
            "--proxy-env-file", str(config.proxy_env_file),
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
        command += ["--no-sync-runtime", "--expected-runtime-tree-sha256", config.runtime_tree_sha256]
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    if config.dry_run:
        command.append("--dry-run")
    return command


def task_result_from_summary(
    config: ParallelConfig,
    index: int,
    summary: dict[str, Any],
    *,
    reused: bool,
    elapsed: float,
    process_returncode: int | None = None,
) -> dict[str, Any]:
    paths = task_paths(config, index)
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    rows = summary.get("rows") if isinstance(summary.get("rows"), list) else []
    status = str(summary.get("status") or "")
    reasons = list(
        single_task_summary_validation_reasons(summary, config, index)
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
    accepted_counts = {
        field: counts.get(field, 0)
        for field in _config.SINGLE_TASK_COUNT_FIELDS
    }
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
        "tasks": accepted_counts["tasks"],
        "generation_done": accepted_counts["generation_done"],
        "empty_patch": accepted_counts["empty_patch"],
        "eval_done": accepted_counts["eval_done"],
        "eval_attempts": accepted_counts["eval_attempts"],
        "eval_retry_tasks": accepted_counts["eval_retry_tasks"],
        "resolved": accepted_counts["resolved"],
        "unresolved": accepted_counts["unresolved"],
        "technical_failed": accepted_counts["technical_failed"],
        "rows": rows,
        "completed": not reasons,
        "summary_validation_reasons": reasons,
        "reused_existing_report": reused,
        "failure_scope": failure_scope,
        "failure_probe": failure_probe,
    }


def run_one(config: ParallelConfig, index: int) -> dict[str, Any]:
    started = time.time()
    paths = task_paths(config, index)
    if paths["json_report"].exists():
        summary = load_json(paths["json_report"])
        if report_is_reusable(summary, config, index):
            result = task_result_from_summary(
                config,
                index,
                summary,
                reused=True,
                elapsed=0.0,
            )
            result["attempts"] = 0
            return result

    ensure_directory(paths["stdout_log"].parent)
    proc: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, config.runner_attempts + 1):
        proc = _run_task_process(task_command(config, index))
        write_text(paths["stdout_log"], proc.stdout)
        write_text(paths["stderr_log"], proc.stderr)
        summary = load_json(paths["json_report"])
        if summary:
            result = task_result_from_summary(
                config,
                index,
                summary,
                reused=False,
                elapsed=time.time() - started,
                process_returncode=proc.returncode,
            )
            result["attempts"] = attempt
            if (
                str(summary.get("status") or "") not in RETRYABLE_TASK_REPORT_STATUSES
                or attempt >= config.runner_attempts
            ):
                return result
        if attempt < config.runner_attempts and config.retry_delay_seconds:
            if _parallel_process.interrupted():
                break
            time.sleep(config.retry_delay_seconds * attempt)
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
        "failure_scope": "task",
        "failure_probe": {},
    }


def prepare_runtime(config: ParallelConfig) -> str:
    if config.skip_preflight:
        return ""
    preflight_json = config.output_dir / "shared_runtime_preflight.json"
    preflight_md = config.output_dir / "shared_runtime_preflight.md"
    preflight_run_dir = f"{config.remote_base}/_preflight"
    command = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_g11_prolite_runner",
        "--host",
        config.host,
        "--ssh-command",
        config.ssh_command,
        "--remote-python",
        config.remote_python,
        "--remote-root",
        config.remote_root,
        "--image-repository",
        config.image_repository,
        "--run-id",
        f"{config.run_id}_preflight",
        "--session-prefix",
        config.session_prefix,
        "--model-name",
        config.model_name,
        "--llm-provider",
        config.llm_provider,
        "--start-index",
        str(config.indices[0]),
        "--limit",
        "1",
        "--base-run-dir",
        preflight_run_dir,
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
        "--llm-timeout",
        str(config.llm_timeout),
        "--provider-error-time-budget",
        str(config.provider_error_time_budget),
        "--checkpoint-interval",
        str(config.checkpoint_interval),
        "--max-task-starts",
        str(config.max_task_starts),
        "--max-eval-attempts",
        str(config.max_eval_attempts),
        "--total-timeout",
        str(config.total_timeout),
        "--json-output",
        str(preflight_json),
        "--markdown-output",
        str(preflight_md),
        "--dry-run",
    ]
    if config.remote_api_env_file:
        command += ["--remote-api-env-file", config.remote_api_env_file]
    else:
        command += [
            "--local-proxy-base-url", config.local_proxy_base_url,
            "--proxy-env-file", str(config.proxy_env_file),
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
        command += ["--no-sync-runtime", "--expected-runtime-tree-sha256", config.runtime_tree_sha256]
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    proc = _run_task_process(command)
    write_text(config.output_dir / "shared_runtime_preflight.stdout.log", proc.stdout)
    write_text(config.output_dir / "shared_runtime_preflight.stderr.log", proc.stderr)
    summary = load_json(preflight_json)
    if proc.returncode != 0 or summary.get("status") != "dry_run":
        raise RuntimeError(
            f"shared runtime preflight failed rc={proc.returncode} "
            f"status={summary.get('status')}"
        )
    expected_identity = _config.preflight_identity(config)
    mismatches = [key for key, value in expected_identity.items() if summary.get(key) != value]
    if mismatches:
        raise RuntimeError("shared runtime preflight identity mismatch: " + ", ".join(mismatches))
    runtime_tree_sha256 = str(summary.get("runtime_tree_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", runtime_tree_sha256) is None:
        raise RuntimeError("shared runtime preflight lacks a valid runtime tree identity")
    return runtime_tree_sha256


remote_health_script = _shared_health.remote_health_script


def run_remote_health_checks(config: ParallelConfig) -> dict[str, Any]:
    return _shared_health.run_remote_health_checks(
        config,
        repo=REPO,
        write_json=write_json,
        write_text=write_text,
    )


def confirm_shared_runtime_after_task_failure(
    config: ParallelConfig,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Pause only when fresh public-service probes confirm a shared outage."""
    if config.skip_health_checks or config.dry_run:
        return result
    if (
        result.get("completed")
        and not int(result.get("technical_failed") or 0)
        and not int(result.get("empty_patch") or 0)
    ):
        return result
    try:
        runtime_probe = run_remote_health_checks(config)
        model_probe = run_remote_model_probe(config)
    except _shared_health.SharedProbeFailure as exc:
        result["failure_scope"] = "shared_infrastructure"
        result["failure_probe"] = {
            "direct": True,
            "status": "failed",
            "evidence": exc.result,
            "error_type": type(exc).__name__,
        }
        return result
    except Exception as exc:
        result.setdefault("failure_scope", "task")
        result["failure_probe"] = {
            "direct": False,
            "status": "setup_error",
            "error_type": type(exc).__name__,
        }
        return result
    result.setdefault("failure_scope", "task")
    result["failure_probe"] = {
        "direct": True,
        "status": "passed",
        "runtime": runtime_probe,
        "model": model_probe,
    }
    return result


def run_remote_model_probe(config: ParallelConfig) -> dict[str, Any]:
    return _shared_health.run_remote_model_probe(config, get_token=get_proxy_token)


def wait_for_remote_model_probe(config: ParallelConfig) -> dict[str, Any]:
    return _shared_health.wait_for_remote_model_probe(
        config,
        run_probe=run_remote_model_probe,
        write_json=write_json,
        interrupted=_parallel_process.interrupted,
    )


def clear_stale_fact_report(config: ParallelConfig) -> None:
    """Remove terminal artifacts when the current task census is not terminal."""
    for name in (
        "final_eval_layer_report.json",
        "final_eval_layer_report.md",
        "final_eval_layer_report.stdout.log",
        "final_eval_layer_report.stderr.log",
    ):
        (config.output_dir / name).unlink(missing_ok=True)


def run_parallel(config: ParallelConfig) -> dict[str, Any]:
    _parallel_process.clear_interrupted()
    signal_handlers = _parallel_process.install_signal_handlers()
    try:
        return _run_parallel(config)
    except BaseException:
        _parallel_process.set_interrupted()
        raise
    finally:
        _parallel_process.terminate_active_task_groups()
        _parallel_process.restore_signal_handlers(signal_handlers)


def _run_parallel(config: ParallelConfig) -> dict[str, Any]:
    ensure_directory(config.output_dir)
    runtime_prepared = False
    runtime_tree_sha256 = ""
    preflight_error: dict[str, str] | None = None
    try:
        runtime_tree_sha256 = prepare_runtime(config)
        runtime_prepared = True
    except Exception as exc:
        preflight_error = {"type": type(exc).__name__, "message": str(exc)}
    remote_health = run_remote_health_checks(config)
    remote_health["model_probe"] = wait_for_remote_model_probe(config)
    if preflight_error:
        remote_health["task_preflight"] = {
            "status": "deferred_to_tasks",
            "failure_scope": "image",
            "error": preflight_error,
        }
    per_task_config = (
        replace(config, runtime_tree_sha256=runtime_tree_sha256)
        if runtime_prepared and runtime_tree_sha256
        else config
    )
    if runtime_prepared and (not config.no_sync_runtime or not config.no_ensure_remote_proxy):
        per_task_config = replace(
            per_task_config,
            no_sync_runtime=True,
            no_ensure_remote_proxy=True,
        )
    results: list[dict[str, Any]] = []
    futures: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
    pending = list(config.indices)
    scheduler = SchedulerState(current_workers=config.max_workers)

    def current_scheduler() -> dict[str, Any]:
        return scheduler_snapshot(config, scheduler, pending=list(pending))

    def submit_ready(executor: concurrent.futures.ThreadPoolExecutor) -> None:
        while pending and not scheduler.halted and len(futures) < scheduler.current_workers:
            index = pending.pop(0)
            futures[executor.submit(run_one, per_task_config, index)] = index

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.max_workers
    ) as executor:
        submit_ready(executor)
        while futures or pending:
            if not futures:
                if scheduler.halted:
                    break
                submit_ready(executor)
                continue
            done, _ = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                index = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "index": index,
                        "returncode": 99,
                        "elapsed_seconds": 0,
                        "runner_status": "orchestrator_exception",
                        "error": str(exc),
                        "completed": False,
                        "failure_scope": "task",
                        "failure_probe": {},
                    }
                result = confirm_shared_runtime_after_task_failure(config, result)
                results.append(result)
                update_scheduler_state(config, scheduler, result)
                halt_reasons = systemic_failure_reasons(result)
                if halt_reasons and pending and not scheduler.halted:
                    scheduler.halted = True
                    scheduler.halt_index = index
                    scheduler.halt_reasons = halt_reasons
                    scheduler.not_started = list(pending)
                    scheduler.events.append(
                        {
                            "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                            "index": index,
                            "action": "halt_pending",
                            "reasons": halt_reasons,
                            "not_started": list(pending),
                        }
                    )
                submit_ready(executor)
                save_progress(
                    config,
                    results,
                    sorted(futures.values()),
                    scheduler=current_scheduler(),
                    remote_health=remote_health,
                )
                progress = aggregate(
                    config,
                    results,
                    sorted(futures.values()),
                    scheduler=current_scheduler(),
                    remote_health=remote_health,
                )
                print(
                    json.dumps(compact_progress(progress), ensure_ascii=False),
                    flush=True,
                )
    token_cost = build_token_summary(config)
    save_progress(
        config,
        results,
        token_cost=token_cost,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )
    incomplete = [
        result["index"] for result in results if result.get("completed") is not True
    ]
    if scheduler.halted and scheduler.not_started:
        clear_stale_fact_report(config)
        fact_report = {
            "status": "not_built_batch_halted",
            "validation_reasons": ["batch_halted_before_complete_census"],
        }
    elif incomplete:
        clear_stale_fact_report(config)
        fact_report = {
            "status": "not_built_incomplete_tasks",
            "incomplete_indices": incomplete,
            "validation_reasons": ["task_results_are_not_terminal"],
        }
    else:
        fact_report = build_eval_fact_report(config)
    save_progress(
        config,
        results,
        token_cost=token_cost,
        fact_report=fact_report,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )
    return aggregate(
        config,
        results,
        token_cost=token_cost,
        fact_report=fact_report,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run G1.1 Pro-Lite tasks in parallel and produce final reports."
    )
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--indices", default="")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--min-workers", type=int, default=1)
    parser.add_argument("--adaptive-recovery-tasks", type=int, default=2)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--remote-base", default="")
    parser.add_argument("--remote-eval-work-root", default=DEFAULT_EVAL_WORK_ROOT)
    parser.add_argument("--remote-runtime-repo", default="")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--llm-model", default=os.environ.get("OPENCOLLAB_SWE_LLM_MODEL", ""))
    parser.add_argument(
        "--llm-provider",
        default=os.environ.get("OPENCOLLAB_SWE_LLM_PROVIDER", "anthropic"),
    )
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--session-prefix", default="")
    parser.add_argument("--host", default=os.environ.get("OPENCOLLAB_SWE_HOST", ""))
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--remote-python", default="python3")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument(
        "--image-repository",
        default=_config.DEFAULT_IMAGE_REPOSITORY,
    )
    parser.add_argument("--workflow", default="validation-council-solve")
    parser.add_argument("--workflow-env", action="append", default=[])
    parser.add_argument("--openhands-command", default="")
    parser.add_argument("--openhands-empty-patch-rejections", type=int, default=2)
    parser.add_argument("--max-empty-patch-retries", type=int, default=1)
    parser.add_argument(
        "--remote-proxy-base-url",
        default=os.environ.get("OPENCOLLAB_REMOTE_PROXY_BASE_URL", ""),
    )
    parser.add_argument(
        "--local-proxy-base-url",
        default=os.environ.get("OPENCOLLAB_LOCAL_PROXY_BASE_URL", ""),
    )
    parser.add_argument(
        "--proxy-env-file",
        type=Path,
        default=(
            Path(os.environ["OPENCOLLAB_PROXY_ENV_FILE"])
            if os.environ.get("OPENCOLLAB_PROXY_ENV_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--remote-api-env-file",
        default="",
    )
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--swe-timeout", type=int, default=14_400)
    parser.add_argument("--task-wall-timeout", type=int, default=15_300)
    parser.add_argument("--eval-timeout", type=int, default=7_200)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--provider-error-time-budget", type=int, default=0)
    parser.add_argument("--checkpoint-interval", type=int, default=0)
    parser.add_argument("--max-task-starts", type=int, default=3)
    parser.add_argument("--max-eval-attempts", type=int, default=2)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--runner-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=int, default=60)
    parser.add_argument("--usd-cny", type=float)
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--expected-runtime-tree-sha256", default="")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-health-checks", action="store_true")
    parser.add_argument("--no-adaptive-concurrency", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = resolve_config(args)
        final = run_parallel(config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(compact_progress(final), ensure_ascii=False, indent=2))
    return 0 if final["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
