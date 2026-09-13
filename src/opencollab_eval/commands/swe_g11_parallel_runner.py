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
from opencollab_eval.commands import swe_g11_task_execution as _task_execution
from opencollab_eval.commands import swe_g11_technical_queue as _technical_queue
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
single_task_summary_validation_reasons = _config.single_task_summary_validation_reasons
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


task_paths = _task_execution.task_paths
task_command = _task_execution.task_command
task_result_from_summary = _task_execution.task_result_from_summary


def run_one(
    config: ParallelConfig,
    index: int,
    attempt: _technical_queue.RecoveryAttempt | None = None,
) -> dict[str, Any]:
    return _task_execution.run_one(
        config,
        index,
        attempt,
        run_task_process=_run_task_process,
        load_json=load_json,
        write_text=write_text,
        ensure_directory=ensure_directory,
    )


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
        "--runner-transport",
        getattr(config, "runner_transport", "ssh"),
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
        "--max-task-starts",
        str(config.max_task_starts),
        "--max-eval-attempts",
        str(config.max_eval_attempts),
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
        command += ["--no-sync-runtime", "--expected-runtime-tree-sha256", config.runtime_tree_sha256]
    if config.no_ensure_remote_proxy:
        command.append("--no-ensure-remote-proxy")
    proc = _run_task_process(command)
    write_text(config.output_dir / "shared_runtime_preflight.stdout.log", proc.stdout)
    write_text(config.output_dir / "shared_runtime_preflight.stderr.log", proc.stderr)
    summary = load_json(preflight_json)
    if proc.returncode != 0 or summary.get("status") != "dry_run":
        raise RuntimeError(f"shared runtime preflight failed rc={proc.returncode} status={summary.get('status')}")
    expected_limits = {
        "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
        "max_empty_patch_retries": config.max_empty_patch_retries,
        "max_task_starts": config.max_task_starts,
        "max_eval_attempts": config.max_eval_attempts,
    }
    mismatches = [key for key, value in expected_limits.items() if summary.get(key) != value]
    if mismatches:
        raise RuntimeError("shared runtime preflight limit mismatch: " + ", ".join(mismatches))
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
    except Exception as exc:  # noqa: BLE001 - probe setup failure stays task-scoped
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


def confirm_eval_only_runtime_after_task_failure(
    config: ParallelConfig,
    result: dict[str, Any],
) -> dict[str, Any]:
    return _technical_queue.confirm_eval_only_runtime_after_failure(
        config,
        result,
        run_runtime_health=run_remote_health_checks,
        shared_probe_failure=_shared_health.SharedProbeFailure,
    )


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


def _run_technical_recovery_tail(
    config: ParallelConfig,
    per_task_config: ParallelConfig,
    results: list[dict[str, Any]],
    scheduler: SchedulerState,
    remote_health: dict[str, Any],
) -> None:
    if not config.max_technical_recoveries or scheduler.halted:
        return
    selected = {int(result["index"]): result for result in results}
    events = _technical_queue.load_manifest_events(config.output_dir)
    for ordinal in range(1, config.max_technical_recoveries + 1):
        attempts: list[_technical_queue.RecoveryAttempt] = []
        for index in config.indices:
            result = selected[index]
            if not int(result.get("technical_failed") or 0):
                continue
            blocked = _technical_queue.recovery_block_reason(result)
            if blocked:
                _technical_queue.record_event(
                    events,
                    {
                        "state": "blocked",
                        "index": index,
                        "ordinal": ordinal,
                        "reason": blocked,
                    },
                )
                continue
            attempt = _technical_queue.plan_recovery_attempt(
                config=config,
                result=result,
                ordinal=ordinal,
            )
            if attempt is None:
                continue
            _technical_queue.write_decision_once(
                attempt.decision_path,
                _technical_queue.decision_payload(
                    attempt,
                    result=result,
                    runtime_tree_sha256=per_task_config.runtime_tree_sha256,
                ),
            )
            _technical_queue.record_event(
                events,
                {
                    "state": "scheduled",
                    "attempt_id": attempt.attempt_id,
                    "index": index,
                    "ordinal": ordinal,
                    "mode": attempt.mode,
                    "run_id": attempt.run_id,
                    "decision": str(attempt.decision_path),
                },
            )
            attempts.append(attempt)
        _technical_queue.write_manifest(config.output_dir, events)
        if not attempts:
            break

        results[:] = [selected[index] for index in config.indices]
        save_progress(
            config,
            results,
            [attempt.index for attempt in attempts],
            scheduler=scheduler_snapshot(
                config,
                scheduler,
                pending=[attempt.index for attempt in attempts],
            ),
            remote_health=remote_health,
        )
        pending = list(attempts)
        futures: dict[
            concurrent.futures.Future[dict[str, Any]],
            _technical_queue.RecoveryAttempt,
        ] = {}

        def submit_ready(
            executor: concurrent.futures.ThreadPoolExecutor,
            pending: list[_technical_queue.RecoveryAttempt] = pending,
            futures: dict[
                concurrent.futures.Future[dict[str, Any]],
                _technical_queue.RecoveryAttempt,
            ] = futures,
        ) -> None:
            while pending and not scheduler.halted and len(futures) < scheduler.current_workers:
                attempt = pending.pop(0)
                futures[
                    executor.submit(
                        run_one,
                        per_task_config,
                        attempt.index,
                        attempt,
                    )
                ] = attempt

        with concurrent.futures.ThreadPoolExecutor(max_workers=config.max_workers) as executor:
            submit_ready(executor)
            while futures:
                done, _ = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    attempt = futures.pop(future)
                    previous = selected[attempt.index]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - one recovery task cannot kill the queue
                        result = {
                            "index": attempt.index,
                            "returncode": 99,
                            "runner_status": "orchestrator_exception",
                            "error": str(exc),
                            "completed": False,
                            "technical_failed": 1,
                            "failure_scope": "task",
                            "failure_probe": {},
                            "json_report": str(attempt.json_report),
                        }
                    result = (
                        confirm_eval_only_runtime_after_task_failure(config, result)
                        if attempt.mode == "eval_only"
                        else confirm_shared_runtime_after_task_failure(config, result)
                    )
                    result = _technical_queue.select_recovery_result(
                        result,
                        previous=previous,
                        attempt=attempt,
                    )
                    selected[attempt.index] = result
                    _technical_queue.record_event(
                        events,
                        {
                            "state": "completed",
                            "attempt_id": attempt.attempt_id,
                            "index": attempt.index,
                            "ordinal": attempt.ordinal,
                            "mode": attempt.mode,
                            "report": str(attempt.json_report),
                            "report_sha256": (_technical_queue.artifact_sha256(attempt.json_report)),
                            "runner_status": result.get("runner_status"),
                            "technical_failed": int(result.get("technical_failed") or 0),
                        },
                    )
                    update_scheduler_state(config, scheduler, result)
                    halt_reasons = systemic_failure_reasons(result)
                    if halt_reasons and not scheduler.halted:
                        scheduler.halted = True
                        scheduler.halt_index = attempt.index
                        scheduler.halt_reasons = halt_reasons
                        scheduler.not_started = [item.index for item in pending]
                        scheduler.events.append(
                            {
                                "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                                "index": attempt.index,
                                "action": "halt_technical_recovery",
                                "reasons": halt_reasons,
                                "not_started": list(scheduler.not_started),
                            }
                        )
                    submit_ready(executor)
                    results[:] = [selected[index] for index in config.indices]
                    _technical_queue.write_manifest(config.output_dir, events)
                    save_progress(
                        config,
                        results,
                        sorted(item.index for item in futures.values()),
                        scheduler=scheduler_snapshot(
                            config,
                            scheduler,
                            pending=[item.index for item in pending],
                        ),
                        remote_health=remote_health,
                    )
        if scheduler.halted:
            break
    if not scheduler.halted:
        _technical_queue.record_exhausted(
            events,
            selected,
            config.indices,
            config.max_technical_recoveries,
        )
        _technical_queue.write_manifest(config.output_dir, events)
    results[:] = [selected[index] for index in config.indices]


def _run_parallel(config: ParallelConfig) -> dict[str, Any]:
    ensure_directory(config.output_dir)
    runtime_prepared = False
    runtime_tree_sha256 = ""
    preflight_error: dict[str, str] | None = None
    try:
        runtime_tree_sha256 = prepare_runtime(config)
        runtime_prepared = True
    except Exception as exc:  # noqa: BLE001 - task preflight failure is reported structurally
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
        replace(config, runtime_tree_sha256=runtime_tree_sha256) if runtime_prepared and runtime_tree_sha256 else config
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.max_workers) as executor:
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
            completed_batch: list[tuple[int, dict[str, Any]]] = []
            for future in done:
                index = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - one task cannot kill the batch
                    result = {
                        "index": index,
                        "returncode": 99,
                        "elapsed_seconds": 0,
                        "runner_status": "orchestrator_exception",
                        "error": str(exc),
                        "completed": False,
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
                    }
                result = confirm_shared_runtime_after_task_failure(config, result)
                completed_batch.append((index, result))
            for index, result in completed_batch:
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
            for _index, _result in completed_batch:
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
    _run_technical_recovery_tail(
        config,
        per_task_config,
        results,
        scheduler,
        remote_health,
    )
    token_cost = build_token_summary(config)
    save_progress(
        config,
        results,
        token_cost=token_cost,
        scheduler=current_scheduler(),
        remote_health=remote_health,
    )
    incomplete = [result["index"] for result in results if result.get("completed") is not True]
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
    parser = argparse.ArgumentParser(description="Run G1.1 Pro-Lite tasks in parallel and produce final reports.")
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
    parser.add_argument(
        "--runner-transport",
        choices=("ssh", "local"),
        default="ssh",
    )
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
            Path(os.environ["OPENCOLLAB_PROXY_ENV_FILE"]) if os.environ.get("OPENCOLLAB_PROXY_ENV_FILE") else None
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
    parser.add_argument("--eval-container-bind-timeout", type=int,
                        default=_config.DEFAULT_EVAL_CONTAINER_BIND_TIMEOUT_SECONDS)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--checkpoint-interval", type=int, default=0)
    parser.add_argument("--max-task-starts", type=int, default=3)
    parser.add_argument("--max-eval-attempts", type=int, default=2)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--runner-attempts", type=int, default=3)
    parser.add_argument(
        "--max-technical-recoveries",
        type=int,
        default=0,
        help="Append up to this many task-level technical recoveries after the primary queue",
    )
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
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to exit status
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(compact_progress(final), ensure_ascii=False, indent=2))
    return 0 if final["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
