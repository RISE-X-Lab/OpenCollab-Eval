#!/usr/bin/env python3
"""Resume an evidence-bound queue of existing candidates without model calls."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opencollab_eval.commands import _swe_eval_layer_integrity, _swe_report_io
from opencollab_eval.commands.swe_v1_parent_eval_lock import ParentEvalLock
from opencollab_eval.commands.swe_v1_prolite_common import MAX_TOTAL_EVAL_ATTEMPTS
from opencollab_eval.commands.swe_v1_prolite_controller import update_parent_fact_report
from opencollab_eval.commands.swe_v1_prolite_process import (
    _ensure_local_process_group_quiesced_after_wait,
    terminate_local_process_group,
)
from opencollab_eval.commands.swe_v1_prolite_report import (
    _has_quarantine_marker,
    quarantine_report,
)
from opencollab_eval.engine.swe_eval_records import strict_integer
from opencollab_eval.safe_files import write_regular_bytes_atomic

SCHEMA = "opencollab.eval_only_queue.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BLOCKED_RUNNER_OPTIONS = set(
    "--base-run-dir --eval-dir-name --eval-only --json-output --limit "
    "--markdown-output --max-empty-patch-retries --max-task-starts "
    "--parent-output-dir --remote-runtime-repo --run-id --start-index".split()
)
DEFAULT_EVAL_TIMEOUT_SECONDS = 7_200.0
QUEUE_EVAL_TIMEOUT_GRACE_SECONDS = 120.0
QUEUE_PROCESS_TERM_GRACE_SECONDS = 5.0
QUEUE_PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
_state_lock = threading.Lock()
def _positive_finite_timeout(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive finite number of seconds")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive finite number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{label} must be a positive finite number of seconds")
    return timeout
def _runner_eval_timeout(runner_args: list[str]) -> float | None:
    values: list[str] = []
    index = 0
    while index < len(runner_args):
        item = runner_args[index]
        if item == "--eval-timeout":
            if index + 1 >= len(runner_args):
                raise ValueError("--eval-timeout requires a value")
            values.append(runner_args[index + 1])
            index += 2
            continue
        if item.startswith("--eval-timeout="):
            values.append(item.split("=", 1)[1])
        index += 1
    if not values:
        return None
    parsed: list[float] = []
    for value in values:
        timeout = _positive_finite_timeout(value, label="--eval-timeout")
        if not timeout.is_integer():
            raise ValueError("--eval-timeout must be an integer number of seconds")
    parsed.append(timeout)
    return parsed[-1]
def _job_eval_timeout(plan: dict[str, Any], job: dict[str, Any]) -> float:
    if "eval_timeout" in job and job["eval_timeout"] is not None:
        return _positive_finite_timeout(job["eval_timeout"], label="eval_timeout")
    runner_timeout = _runner_eval_timeout(plan["runner_args"])
    if runner_timeout is not None:
        return runner_timeout
    environment_timeout = os.environ.get("OPENCOLLAB_EVAL_TIMEOUT_SECONDS")
    if environment_timeout is not None and environment_timeout.strip():
        return _positive_finite_timeout(environment_timeout, label="OPENCOLLAB_EVAL_TIMEOUT_SECONDS")
    return DEFAULT_EVAL_TIMEOUT_SECONDS
def _absolute_directory(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a directory")
    return path
def _read_plan(path: Path) -> dict[str, Any]:
    value = _swe_report_io.load_json(path)
    if value.get("schema") != SCHEMA:
        raise ValueError(f"queue plan schema must be {SCHEMA}")
    runner_args = value.get("runner_args")
    if not isinstance(runner_args, list) or any(not isinstance(item, str) for item in runner_args):
        raise ValueError("runner_args must be a list of strings")
    _runner_eval_timeout(runner_args)
    for item in runner_args:
        option = item.split("=", 1)[0]
        if option in _BLOCKED_RUNNER_OPTIONS:
            raise ValueError(f"runner_args cannot set queue-owned option {option}")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs must be a non-empty list")
    seen_routes: set[tuple[str, int]] = set()
    seen_candidates: set[tuple[str, str, str, str]] = set()
    normalized = []
    for raw in jobs:
        if not isinstance(raw, dict):
            raise ValueError("each queue job must be an object")
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise ValueError("job index must be a positive integer")
        parent = _absolute_directory(raw.get("parent_output_dir"), label="parent_output_dir")
        base_run_dir = raw.get("base_run_dir")
        if not isinstance(base_run_dir, str) or not base_run_dir.startswith("/"):
            raise ValueError("base_run_dir must be an absolute worker path")
        remote_runtime_repo = raw.get("remote_runtime_repo")
        if not isinstance(remote_runtime_repo, str) or not remote_runtime_repo.startswith("/"):
            raise ValueError("remote_runtime_repo must be an absolute worker path")
        run_id = raw.get("run_id")
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id has an invalid format")
        eval_dir_name = raw.get("eval_dir_name")
        if (
            not isinstance(eval_dir_name, str)
            or not eval_dir_name
            or "/" in eval_dir_name
            or eval_dir_name in {".", ".."}
        ):
            raise ValueError("eval_dir_name must be one path component")
        eval_timeout = raw.get("eval_timeout")
        if eval_timeout is not None:
            eval_timeout_value = _positive_finite_timeout(eval_timeout, label="eval_timeout")
            if not eval_timeout_value.is_integer():
                raise ValueError("eval_timeout must be an integer number of seconds")
            eval_timeout = int(eval_timeout_value)
        task = raw.get("task")
        record_id = raw.get("record_id")
        source_patch_sha256 = raw.get("source_patch_sha256")
        eval_patch_sha256 = raw.get("eval_patch_sha256")
        if not isinstance(task, str) or not task or len(task.encode("utf-8")) > 256:
            raise ValueError("task must be a bounded non-empty string")
        if not isinstance(record_id, str) or not record_id or len(record_id.encode("utf-8")) > 256:
            raise ValueError("record_id must be a bounded non-empty string")
        for label, digest in (
            ("source_patch_sha256", source_patch_sha256),
            ("eval_patch_sha256", eval_patch_sha256),
        ):
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256")
        route = (str(parent), index)
        candidate = (task, record_id, source_patch_sha256, eval_patch_sha256)
        if route in seen_routes:
            raise ValueError(f"duplicate queue job for index {index} in {parent}")
        if candidate in seen_candidates:
            raise ValueError(f"duplicate candidate identity for task {task}")
        seen_routes.add(route)
        seen_candidates.add(candidate)
        normalized_job = {
            "index": index,
            "parent_output_dir": str(parent),
            "base_run_dir": base_run_dir,
            "remote_runtime_repo": remote_runtime_repo,
            "run_id": run_id,
            "eval_dir_name": eval_dir_name,
            "task": task,
            "record_id": record_id,
            "source_patch_sha256": source_patch_sha256,
            "eval_patch_sha256": eval_patch_sha256,
        }
        if eval_timeout is not None:
            normalized_job["eval_timeout"] = eval_timeout
        normalized.append(normalized_job)
    return {"schema": SCHEMA, "runner_args": runner_args, "jobs": normalized}
def _row_identity(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    generation = row.get("generation")
    if not isinstance(generation, dict):
        return None
    task = row.get("task")
    record_id = generation.get("record_id")
    source_patch_sha256 = generation.get("source_patch_sha256") or generation.get("patch_sha256")
    eval_patch_sha256 = generation.get("eval_patch_sha256") or generation.get(
        "patch_sha256"
    )
    if (
        not isinstance(task, str)
        or not task
        or not isinstance(record_id, str)
        or not record_id
        or not isinstance(source_patch_sha256, str)
        or _SHA256_RE.fullmatch(source_patch_sha256) is None
        or not isinstance(eval_patch_sha256, str)
        or _SHA256_RE.fullmatch(eval_patch_sha256) is None
    ):
        return None
    return task, record_id, source_patch_sha256, eval_patch_sha256
def _job_identity(job: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        job["task"],
        job["record_id"],
        job["source_patch_sha256"],
        job["eval_patch_sha256"],
    )
def _row_matches_job(row: dict[str, Any], job: dict[str, Any]) -> bool:
    """Match a report row to a job's immutable candidate binding.

    The evaluation patch is derived from the source patch and may be
    recomputed by an eval-only runner.  It is retained in the job identity for
    provenance and queue keys, but must not make an otherwise exact candidate
    look missing.
    """
    identity = _row_identity(row)
    return identity is not None and identity[:3] == _job_identity(job)[:3]
def _row_terminal(row: dict[str, Any], *, job: dict[str, Any]) -> bool:
    evaluation = row.get("eval")
    if (
        row.get("index") != job["index"]
        or not _row_matches_job(row, job)
        or not isinstance(evaluation, dict)
        or evaluation.get("status") != "eval_done"
    ):
        return False
    summary = evaluation.get("summary")
    integrity = _swe_eval_layer_integrity.attempt_integrity(row, job["task"])
    return (
        isinstance(summary, dict)
        and isinstance(summary.get("resolved"), bool)
        and integrity.direct_execution_proven
        and not integrity.reasons
    )
def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    for result in report.get("results") or []:
        if isinstance(result, dict):
            rows.extend(
                row for row in result.get("rows") or [] if isinstance(row, dict)
            )
    return rows
def _candidate_identity_status(job: dict[str, Any]) -> str:
    parent = Path(job["parent_output_dir"])
    paths = [parent / "parallel_summary.json", *parent.glob("task_*_eval_only_*.json")]
    identities: set[tuple[str, str, str]] = set()
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _has_quarantine_marker(path):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"queue report must be a regular file: {path}")
        report = _swe_report_io.load_json(path)
        for row in _report_rows(report):
            if row.get("index") == job["index"] and (identity := _row_identity(row)):
                identities.add(identity[:3])
    if _job_identity(job)[:3] in identities:
        return "verified"
    if not identities:
        return "candidate_identity_missing"
    if len(identities) > 1:
        return "candidate_identity_conflict"
    return "candidate_identity_missing"
def _terminal_report(job: dict[str, Any]) -> tuple[Path | None, str]:
    parent = Path(job["parent_output_dir"])
    matches: list[tuple[int, str, Path, bool]] = []
    paths = {
        parent / "parallel_summary.json",
        *parent.glob(f"task_{job['index']}_eval_only_*.json"),
        *parent.glob(f"task_{job['index']}_*_eval_only_*.json"),
    }
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _has_quarantine_marker(path):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"queue report must be a regular file: {path}")
        report = _swe_report_io.load_json(path)
        for row in _report_rows(report):
            if _row_terminal(row, job=job):
                resolved = bool(row["eval"]["summary"]["resolved"])
                matches.append((info.st_mtime_ns, str(path), path, resolved))
    if len({match[3] for match in matches}) > 1:
        return None, "terminal_verdict_conflict"
    return (max(matches)[2], "verified") if matches else (None, "missing")
def _observed_eval_attempts(job: dict[str, Any]) -> int:
    parent = Path(job["parent_output_dir"])
    counts = []
    parent_summary = parent / "parallel_summary.json"
    if parent_summary.is_file():
        report = _swe_report_io.load_json(parent_summary)
        total = 0
        for result in report.get("results") or []:
            if not isinstance(result, dict):
                continue
            for row in result.get("rows") or []:
                if (
                    isinstance(row, dict)
                    and row.get("index") == job["index"]
                    and _row_matches_job(row, job)
                ):
                    evaluation = row.get("eval")
                    if isinstance(evaluation, dict):
                        count = strict_integer(
                            evaluation.get("attempt_count", 0), nonnegative=True
                        )
                        if count is not None:
                            total += count
        counts.append(total)
    final_report = parent / "final_eval_layer_report.json"
    if final_report.is_file():
        report = _swe_report_io.load_json(final_report)
        for task in report.get("tasks") or []:
            if isinstance(task, dict) and _final_task_matches_job(task, job):
                raw_count = task.get("observed_eval_attempt_count")
                if raw_count is None:
                    raw_count = task.get("eval_attempt_count", 0)
                count = strict_integer(raw_count, nonnegative=True)
                if count is None:
                    continue
                counts.append(count)
    return max(counts, default=0)
def _final_task_matches_job(task: dict[str, Any], job: dict[str, Any]) -> bool:
    if task.get("index") != job["index"] or task.get("task") != job["task"]:
        return False
    record_id = task.get("record_id")
    source_sha = task.get("source_patch_sha256") or task.get("patch_sha256")
    return (record_id, source_sha) == (
        job["record_id"],
        job["source_patch_sha256"],
    )
def _write_state_unlocked(path: Path, state: dict[str, Any]) -> None:
    payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    write_regular_bytes_atomic(path, payload)
def _write_state(path: Path, state: dict[str, Any]) -> None:
    with _state_lock:
        _write_state_unlocked(path, state)
def _state_launch_count(previous: object) -> int:
    if not isinstance(previous, dict):
        return 0
    raw = previous.get("launch_count")
    if raw is None:
        return 0
    count = strict_integer(raw, nonnegative=True)
    if count is None:
        return 2
    return count
def _set_job_state(
    path: Path,
    state: dict[str, Any],
    key: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    with _state_lock:
        state["jobs"][key] = result
        _write_state_unlocked(path, state)
    return result
def _reserve_launch(
    path: Path,
    state: dict[str, Any],
    key: str,
    job: dict[str, Any],
) -> int | None:
    with _state_lock:
        previous = state["jobs"].get(key)
        launch_count = _state_launch_count(previous)
        if launch_count >= 2:
            return None
        launch_count += 1
        state["jobs"][key] = {"status": "running", "launch_count": launch_count, **job}
        _write_state_unlocked(path, state)
    return launch_count
def _job_key(job: dict[str, Any]) -> str:
    return hashlib.sha256("\0".join(_job_identity(job)).encode("utf-8")).hexdigest()[:16]
def _child_argv(
    plan: dict[str, Any],
    job: dict[str, Any],
    *,
    queue_id: str,
    output_dir: Path,
) -> tuple[list[str], Path, Path]:
    parent = Path(job["parent_output_dir"])
    base_prefix = f"task_{job['index']}_{_job_key(job)}_eval_only_queue_{queue_id}"
    attempt = 1
    while (
        (parent / f"{base_prefix}_attempt_{attempt}.json").exists()
        or (output_dir / f"{base_prefix}_attempt_{attempt}.log").exists()
    ):
        attempt += 1
    prefix = f"{base_prefix}_attempt_{attempt}"
    json_output = parent / f"{prefix}.json"
    markdown_output = parent / f"{prefix}.md"
    argv = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_v1_prolite_runner",
        *plan["runner_args"],
        "--eval-only",
        "--start-index",
        str(job["index"]),
        "--limit",
        "1",
        "--max-task-starts",
        "0",
        "--max-empty-patch-retries",
        "0",
        "--parent-output-dir",
        job["parent_output_dir"],
        "--base-run-dir",
        job["base_run_dir"],
        "--remote-runtime-repo",
        job["remote_runtime_repo"],
        "--run-id",
        job["run_id"],
        "--eval-dir-name",
        job["eval_dir_name"],
        "--expected-task",
        job["task"],
        "--expected-record-id",
        job["record_id"],
        "--expected-source-patch-sha256",
        job["source_patch_sha256"],
        "--expected-eval-patch-sha256",
        job["eval_patch_sha256"],
        "--json-output",
        str(json_output),
        "--markdown-output",
        str(markdown_output),
    ]
    if job.get("eval_timeout") is not None:
        timeout = _positive_finite_timeout(job["eval_timeout"], label="eval_timeout")
        if not timeout.is_integer():
            raise ValueError("eval_timeout must be an integer number of seconds")
        argv.extend(["--eval-timeout", str(int(timeout))])
    return argv, json_output, markdown_output
def _run_bounded_child(
    argv: list[str],
    *,
    log,
    timeout: float,
) -> subprocess.CompletedProcess:
    timeout_value = _positive_finite_timeout(timeout, label="queue child timeout")
    popen_kwargs: dict[str, Any] = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **popen_kwargs)
    returncode: int
    try:
        try:
            returncode = int(process.wait(timeout=timeout_value))
        except subprocess.TimeoutExpired:
            log.write(f"\nqueue child timeout after {timeout_value:g}s\n")
            cleanup_ok = terminate_local_process_group(
                process,
                term_timeout=QUEUE_PROCESS_TERM_GRACE_SECONDS,
                kill_timeout=QUEUE_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            )
            returncode = 124 if cleanup_ok else 125
        else:
            if os.name == "posix" and not _ensure_local_process_group_quiesced_after_wait(
                process,
                term_timeout=QUEUE_PROCESS_TERM_GRACE_SECONDS,
                kill_timeout=QUEUE_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            ):
                log.write("\nqueue child cleanup left a residual process group\n")
                returncode = 125
    except BaseException:
        try:
            terminate_local_process_group(
                process,
                term_timeout=QUEUE_PROCESS_TERM_GRACE_SECONDS,
                kill_timeout=QUEUE_PROCESS_KILL_REAP_TIMEOUT_SECONDS,
            )
        except BaseException:
            pass
        raise
    return subprocess.CompletedProcess(argv, returncode)
def _run_job(
    plan: dict[str, Any],
    job: dict[str, Any],
    *,
    queue_id: str,
    output_dir: Path,
    state_path: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    key = _job_key(job)
    identity_status = _candidate_identity_status(job)
    if identity_status != "verified":
        return _set_job_state(
            state_path, state, key, {"status": identity_status, **job}
        )
    previous_state = state.get("jobs", {}).get(key)
    previous_failed = isinstance(previous_state, dict) and previous_state.get("status") in {
        "command_failed", "technical_failed"
    }
    if not previous_failed:
        terminal, terminal_status = _terminal_report(job)
        if terminal_status == "terminal_verdict_conflict":
            return _set_job_state(
                state_path, state, key, {"status": terminal_status, **job}
            )
        if terminal is not None:
            return _set_job_state(
                state_path, state, key,
                {"status": "skipped_terminal", "report": str(terminal), **job},
            )
    if _observed_eval_attempts(job) >= MAX_TOTAL_EVAL_ATTEMPTS:
        return _set_job_state(
            state_path, state, key, {"status": "budget_exhausted", **job}
        )
    try:
        child_timeout = _job_eval_timeout(plan, job) + QUEUE_EVAL_TIMEOUT_GRACE_SECONDS
        argv, json_output, markdown_output = _child_argv(
            plan,
            job,
            queue_id=queue_id,
            output_dir=output_dir,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:8_192]
        return _set_job_state(
            state_path,
            state,
            key,
            {
                "status": "invalid_timeout",
                "returncode": 125,
                "error": detail,
                **job,
            },
        )
    log_path = output_dir / (json_output.stem + ".log")
    launch_count = _reserve_launch(state_path, state, key, job)
    if launch_count is None:
        previous = state["jobs"].get(key)
        previous_count = _state_launch_count(previous)
        return _set_job_state(
            state_path,
            state,
            key,
            {"status": "launch_budget_exhausted", "launch_count": previous_count, **job},
        )
    child_error: Exception | None = None
    proc: subprocess.CompletedProcess | None = None
    try:
        with log_path.open("x", encoding="utf-8") as log:
            proc = _run_bounded_child(argv, log=log, timeout=child_timeout)
    except Exception as exc:
        child_error = exc
    if child_error is not None:
        returncode = 124 if isinstance(child_error, subprocess.TimeoutExpired) else 125
        error = f"{type(child_error).__name__}: {child_error}"[:8_192]
    elif proc is None:
        returncode, error = 125, "child runner returned no process result"
    else:
        returncode, error = proc.returncode, ""
    result = {
        "status": "command_failed", "returncode": returncode, "error": error,
        "log": str(log_path), "json_output": str(json_output),
        "markdown_output": str(markdown_output), "launch_count": launch_count, **job,
    }
    retryable_result = child_error is not None or proc is None or proc.returncode != 125
    if json_output.is_file():
        report = _swe_report_io.load_json(json_output)
        rows = report.get("rows")
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
            if proc is not None and proc.returncode == 0 and _row_terminal(
                rows[0], job=job
            ):
                result["status"] = "terminal"
            elif proc is not None and _row_terminal(rows[0], job=job):
                failed_path = quarantine_report(
                    json_output, ".command_failed", replace_first=True
                )
                if failed_path is None:
                    result["error"] = "failed to quarantine child report"
                    result["status"] = "technical_failed"
                else:
                    result["json_output"] = str(failed_path)
            elif isinstance(rows[0].get("eval"), dict) and rows[0]["eval"].get(
                "status"
            ) == "technical_eval_failed":
                result["status"] = "technical_failed"
    persisted = _set_job_state(state_path, state, key, result)
    if (
        result["status"] in {"command_failed", "technical_failed"}
        and launch_count < 2
        and retryable_result
        and _observed_eval_attempts(job) < MAX_TOTAL_EVAL_ATTEMPTS
    ):
        return _run_job(
            plan,
            job,
            queue_id=queue_id,
            output_dir=output_dir,
            state_path=state_path,
            state=state,
        )
    return persisted
def _refresh_parent_report(
    parent: str,
    report: Path,
    *,
    candidate_identities: dict[int, tuple[str, str, str, str]] | None = None,
) -> dict[str, Any]:
    with ParentEvalLock(Path(parent), "report"):
        ignored_reports = _quarantine_invalid_reports(Path(parent), report)
        return update_parent_fact_report(
            SimpleNamespace(
                parent_output_dir=Path(parent),
                json_output=report,
                usd_cny=None,
                ignored_reports=tuple(ignored_reports),
                candidate_identities=candidate_identities,
            )
        )
def _quarantine_invalid_reports(parent: Path, current_report: Path) -> list[Path]:
    """Move stale malformed task reports aside without replacing backups."""
    current = current_report.absolute()
    ignored: list[Path] = []
    for path in parent.glob("task_*_eval_only_*.json"):
        if path.absolute() == current:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if _has_quarantine_marker(path):
                continue
            report, error = _swe_report_io.load_json_with_error(path)
            rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
            if not error and len(rows) == 1 and strict_integer(rows[0].get("index")) is not None:
                continue
            if quarantine_report(path, ".invalid") is None:
                ignored.append(path.absolute())
        except (FileNotFoundError, OSError):
            continue
    return ignored
def _future_result(future: Any, job: dict[str, Any], state_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    try:
        result = future.result()
        if isinstance(result, dict):
            return result
        raise RuntimeError("queue job returned a non-object result")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:8_192]
        launch_count = _state_launch_count(state.get("jobs", {}).get(_job_key(job)))
        return _set_job_state(
            state_path, state, _job_key(job),
            {"status": "command_failed", "returncode": 125, "error": detail,
             "launch_count": launch_count, **job},
        )
def _run_queue_locked(
    plan: dict[str, Any],
    output_dir: Path,
    *,
    workers: int,
    queue_id: str,
) -> dict[str, Any]:
    state_path = output_dir / f"rejudge_queue_{queue_id}.json"
    if state_path.exists():
        state = _swe_report_io.load_json(state_path)
        if (
            state.get("schema") != "opencollab.eval_only_queue_state.v1"
            or state.get("queue_id") != queue_id
            or not isinstance(state.get("jobs"), dict)
        ):
            raise RuntimeError(f"invalid existing queue state: {state_path}")
        state["workers"] = workers
    else:
        state = {
            "schema": "opencollab.eval_only_queue_state.v1",
            "queue_id": queue_id,
            "workers": workers,
            "model_generation": "disabled",
            "jobs": {},
        }
    _write_state(state_path, state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _run_job,
                plan,
                job,
                queue_id=queue_id,
                output_dir=output_dir,
                state_path=state_path,
                state=state,
            )
            for job in plan["jobs"]
        ]
        results = [
            _future_result(future, job, state_path, state)
            for future, job in zip(futures, plan["jobs"], strict=True)
        ]
    result_by_key = {
        _job_key(job): result for job, result in zip(plan["jobs"], results, strict=True)
    }
    parent_reports = {}
    jobs_by_parent: dict[str, list[dict[str, Any]]] = {}
    for job in plan["jobs"]:
        jobs_by_parent.setdefault(job["parent_output_dir"], []).append(job)
    for parent, parent_jobs in jobs_by_parent.items():
        eligible = [
            job for job in parent_jobs
            if result_by_key[_job_key(job)].get("status") in {
                "terminal", "skipped_terminal"
            }
        ]
        if not eligible:
            first = result_by_key[_job_key(parent_jobs[0])]
            parent_reports[parent] = {"status": first.get("status") or "command_failed"}
            continue
        report = None
        parent_summary = Path(parent) / "parallel_summary.json"
        terminal_status = "missing"
        for job in eligible:
            candidate, candidate_status = _terminal_report(job)
            if candidate_status == "terminal_verdict_conflict":
                terminal_status = candidate_status
                break
            if candidate is not None and candidate != parent_summary:
                # A task report is the queue's new evidence.  Prefer it over
                # the cumulative parent summary even when the latter is also
                # terminal for an earlier index.
                report, terminal_status = candidate, candidate_status
            elif candidate is not None and report is None:
                report, terminal_status = candidate, candidate_status
        if terminal_status == "terminal_verdict_conflict":
            parent_reports[parent] = {"status": terminal_status}
            continue
        if report == Path(parent) / "parallel_summary.json":
            parent_reports[parent] = {"status": "unchanged", "source": str(report)}
            continue
        if report is not None:
            candidate_identities = {
                job["index"]: (
                    job["task"],
                    job["record_id"],
                    job["source_patch_sha256"],
                    job["eval_patch_sha256"],
                )
                for job in eligible
            }
            parent_reports[parent] = _refresh_parent_report(
                parent,
                report,
                candidate_identities=candidate_identities,
            )
        else:
            parent_reports[parent] = {"status": "terminal_report_missing"}
    counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
    state.update({"status": "done", "counts": counts, "parent_reports": parent_reports})
    _write_state(state_path, state)
    return state
def run_queue(plan_path: Path, output_dir: Path, *, workers: int) -> dict[str, Any]:
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    plan = _read_plan(plan_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    queue_id = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    with ParentEvalLock(output_dir, f"rejudge-queue-{queue_id}", blocking=False):
        return _run_queue_locked(plan, output_dir, workers=workers, queue_id=queue_id)
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    return parser
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_queue(args.plan, args.output_dir, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    completed = {"terminal", "skipped_terminal"}
    return 0 if set(result["counts"]).issubset(completed) else 1
if __name__ == "__main__":
    raise SystemExit(main())
