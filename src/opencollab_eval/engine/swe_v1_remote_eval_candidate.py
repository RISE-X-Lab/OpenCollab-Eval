"""Strict historical candidate selection for eval-only runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

from opencollab_eval.engine.provider_failures import summarize_terminal_provider_failures
from opencollab_eval.engine.swe_eval_records import open_regular_binary
from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid
from opencollab_eval.engine.swe_v1_remote_records import (
    embedded_workflow_metric,
    generation_done,
    historical_generation_identity_status,
    latest_pair,
    prediction_patch,
    read_jsonl,
)

_BLOCKED_IDENTITY_FIELDS = (
    "invocation_id",
    "run_id",
    "runtime_tree_sha256",
    "budget",
    "max_steps",
    "llm_base_url_sha256",
    "workflow_env",
    "llm_model",
    "llm_provider",
    "context_window",
    "temperature",
    "top_p",
    "max_output_tokens",
    "wire_protocol",
    "reasoning_effort",
)
_BLOCKED_CAUSAL_FIELDS = (
    "workflow_status",
    "runner_returncode",
    "runtime_status",
    "error",
    "submission_eligible",
    "execution_quiesced",
    "container_execution_quiesced",
    "workflow_result",
)
_EXECUTION_ABORT_MARKER = "Execution environment has been aborted"
_MAX_TRAJECTORY_BYTES = 16 * 1024 * 1024
_MAX_TRAJECTORY_LINE_BYTES = 2 * 1024 * 1024
_MAX_TRAJECTORY_ROWS = 10_000
_GO_PROBE_RE = re.compile(r"(?:^|[;&|]\s*)go\s+(?:build|test)\b")
_CORRECTED_GO_PROBE_RE = re.compile(
    r"\Aexport\s+PATH=\$PATH:/usr/local/go/bin\s*&&\s*"
    r"cd\s+/testbed\s*&&\s*(?:go\s+version\s*&&\s*)?"
    r"go\s+(?:build|test)\b(?P<tail>.*)\Z",
    re.DOTALL,
)
_UNCORRECTED_GO_PROBE_RE = re.compile(
    r"\Acd\s+/testbed\s*&&\s*go\s+(?:build|test)\b(?P<tail>.*)\Z",
    re.DOTALL,
)
_GO_DIAGNOSTIC_RE = re.compile(r"^(?P<path>[^:\n]+\.go):\d+:\d+:\s+(?P<detail>.+)$")
_TOOL_EXIT_RE = re.compile(r"\AExit code:\s*(?P<code>\d+)\s*(?:\n|\Z)")


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _normalized_historical_llm_transport(embedded, metric):
    embedded_present = "llm_transport" in embedded
    metric_present = "llm_transport" in metric
    if embedded_present != metric_present:
        return None
    if not embedded_present:
        return "reverse_proxy"
    embedded_value = embedded["llm_transport"]
    metric_value = metric["llm_transport"]
    if embedded_value not in {"direct", "reverse_proxy"}:
        return None
    if metric_value != embedded_value:
        return None
    return embedded_value


def _historical_provider_failure_clear(embedded, metric):
    embedded_present = "provider_failure" in embedded
    metric_present = "provider_failure" in metric
    if embedded_present != metric_present:
        return False
    if not embedded_present:
        return True
    return embedded["provider_failure"] is False and metric["provider_failure"] is False


def _bounded_failure_text(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        return False


def _agent_failure_evidence_valid(value):
    if not isinstance(value, list) or len(value) > 100:
        return False
    for failure in value:
        if not isinstance(failure, dict):
            return False
        if not _bounded_failure_text(failure.get("label")):
            return False
        if not _bounded_failure_text(failure.get("exception_type")):
            return False
        provider_error_type = failure.get("provider_error_type")
        if provider_error_type is not None and not _bounded_failure_text(
            provider_error_type
        ):
            return False
        status_code = failure.get("status_code")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            return False
    return summarize_terminal_provider_failures(value) is None


def _blocked_technical_interruption_proven(metric, matching_official_eval_attempts):
    if matching_official_eval_attempts != 0:
        return False
    if (
        metric.get("runner_returncode") != 1
        or metric.get("runtime_status") != "completed"
        or metric.get("error") not in (None, "")
        or metric.get("submission_eligible") is not True
        or metric.get("execution_quiesced") is not True
        or metric.get("container_execution_quiesced") is not True
    ):
        return False
    result = metric.get("workflow_result")
    if not isinstance(result, dict) or result.get("status") != "blocked":
        return False
    blocker = result.get("blocker")
    attempts = result.get("attempts")
    if not isinstance(blocker, str) or _EXECUTION_ABORT_MARKER not in blocker:
        return False
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return False
    verdict = attempts[-1].get("final_verdict")
    return bool(
        isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED" and verdict.get("findings") == blocker
    )


def _bounded_verified_trajectory(metric):
    path_text = metric.get("trajectory_path")
    expected_sha = metric.get("trajectory_sha256")
    instance_id = metric.get("instance_id")
    if (
        not isinstance(path_text, str)
        or not path_text
        or not isinstance(expected_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
        or not isinstance(instance_id, str)
        or not instance_id
    ):
        return None
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path.name != "orchestration.jsonl"
        or instance_id not in path.parts
        or ".." in path.parts
    ):
        return None
    try:
        if path.resolve(strict=True) != path:
            return None
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > _MAX_TRAJECTORY_BYTES:
                return None
            raw = handle.read(_MAX_TRAJECTORY_BYTES + 1)
            after = os.fstat(handle.fileno())
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        len(raw) > _MAX_TRAJECTORY_BYTES
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or hashlib.sha256(raw).hexdigest() != expected_sha
    ):
        return None
    rows = []
    for line in raw.splitlines():
        if (
            not line.strip()
            or len(line) > _MAX_TRAJECTORY_LINE_BYTES
            or len(rows) >= _MAX_TRAJECTORY_ROWS
        ):
            return None
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(row, dict):
            return None
        rows.append(row)
    return rows or None


def _candidate_go_paths(metric):
    audit = metric.get("patch_path_audit")
    paths = audit.get("actual_paths") if isinstance(audit, dict) else None
    extraction = metric.get("trusted_patch_extraction")
    changed_paths = (
        extraction.get("changed_paths") if isinstance(extraction, dict) else None
    )
    if not isinstance(paths, list) or not paths or len(paths) > 1024:
        return None
    if (
        not isinstance(changed_paths, list)
        or any(not isinstance(value, str) for value in paths)
        or any(not isinstance(value, str) for value in changed_paths)
        or len(paths) != len(set(paths))
        or set(paths) != set(changed_paths)
    ):
        return None
    result = []
    for value in paths:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("/")
            or ".." in Path(value).parts
        ):
            return None
        if value.endswith(".go"):
            result.append(value)
    return tuple(result) or None


def _tool_exec(row):
    if row.get("type") != "tool_exec":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("tool") != "bash":
        return None
    args = payload.get("args")
    command = args.get("command") if isinstance(args, dict) else None
    result = payload.get("result")
    if not isinstance(command, str) or not isinstance(result, str):
        return None
    return command, result


def _candidate_go_compile_failure(result, candidate_paths):
    for line in result.splitlines():
        match = _GO_DIAGNOSTIC_RE.fullmatch(line.strip())
        if match is None or match.group("path") not in candidate_paths:
            continue
        detail = match.group("detail").strip().lower()
        if detail and not detail.startswith(("warning:", "note:")):
            return True
    return False


def _tool_exit_code(result):
    match = _TOOL_EXIT_RE.match(result)
    return int(match.group("code")) if match is not None else None


def _corrected_go_probe(command, result):
    match = _CORRECTED_GO_PROBE_RE.fullmatch(command.strip())
    exit_code = _tool_exit_code(result)
    if match is None or exit_code is None:
        return False
    tail = match.group("tail").strip()
    if tail == "./...":
        return exit_code != 0
    return bool(re.fullmatch(r"\./\.\.\.\s+2>&1\s*\|\s*head\s+-\d+", tail))


def _uncorrected_go_probe(command, result):
    match = _UNCORRECTED_GO_PROBE_RE.fullmatch(command.strip())
    exit_code = _tool_exit_code(result)
    if match is None or exit_code is None or "go: command not found" not in result.lower():
        return False
    tail = match.group("tail").strip()
    if tail == "./...":
        return exit_code != 0
    return bool(re.fullmatch(r"\./\.\.\.\s+2>&1\s*\|\s*head\s+-\d+", tail))


def _blocked_candidate_documents_match(prediction, metric):
    embedded = embedded_workflow_metric(prediction)
    fields = (
        "trajectory_path",
        "trajectory_sha256",
        "patch_path_audit",
        "trusted_patch_extraction",
    )
    return bool(
        isinstance(embedded, dict)
        and all(field in embedded and field in metric for field in fields)
        and all(embedded[field] == metric[field] for field in fields)
    )


def _trajectory_final_verdict_matches(row, blocker):
    if row.get("type") != "llm_call":
        return False
    payload = row.get("payload")
    tool_calls = payload.get("tool_calls") if isinstance(payload, dict) else None
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, dict) or call.get("name") != "structured_output":
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > 64 * 1024:
            continue
        try:
            verdict = json.loads(arguments)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(verdict, dict)
            and verdict.get("verdict") == "BLOCKED"
            and verdict.get("findings") == blocker
        ):
            return True
    return False


def _blocked_candidate_failure_proven(
    prediction,
    metric,
    matching_official_eval_attempts,
):
    if matching_official_eval_attempts != 0:
        return False
    result = metric.get("workflow_result")
    blocker = result.get("blocker") if isinstance(result, dict) else None
    attempts = result.get("attempts") if isinstance(result, dict) else None
    if (
        not isinstance(blocker, str)
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(attempts[-1], dict)
        or result.get("status") != "blocked"
        or metric.get("execution_quiesced") is not True
        or metric.get("container_execution_quiesced") is not True
        or not _blocked_candidate_documents_match(prediction, metric)
        or not current_generation_proof_valid(metric, prediction_patch(prediction))
    ):
        return False
    verdict = attempts[-1].get("final_verdict")
    if not (
        isinstance(verdict, dict)
        and verdict.get("verdict") == "BLOCKED"
        and verdict.get("findings") == blocker
    ):
        return False
    blocker_lower = blocker.lower()
    if not all(
        marker in blocker_lower
        for marker in (
            "go: command not found",
            "/usr/local/go/bin/go",
            "no usable output",
        )
    ):
        return False
    candidate_paths = _candidate_go_paths(metric)
    rows = _bounded_verified_trajectory(metric)
    if candidate_paths is None or rows is None:
        return False
    command_not_found_at = None
    candidate_failure_at = None
    for index, row in enumerate(rows):
        execution = _tool_exec(row)
        if execution is None:
            continue
        command, output = execution
        if (
            command_not_found_at is None
            and _uncorrected_go_probe(command, output)
        ):
            command_not_found_at = index
            continue
        if (
            command_not_found_at is not None
            and index > command_not_found_at
            and _corrected_go_probe(command, output)
            and _candidate_go_compile_failure(output, candidate_paths)
        ):
            candidate_failure_at = index
    if candidate_failure_at is None:
        return False
    return any(
        index > candidate_failure_at and _trajectory_final_verdict_matches(row, blocker)
        for index, row in enumerate(rows)
    )


def _matching_official_eval_attempt_count(run_dir, task):
    return sum(
        1
        for row in read_jsonl(run_dir / "eval_attempts.jsonl")
        if row.get("phase") == "eval_attempt_started" and row.get("task") == task
    )


def _blocked_identity_document_proven(prediction, metric):
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        return False
    embedded = embedded_workflow_metric(prediction)
    if not isinstance(embedded, dict):
        return False
    models = [row.get("model_name_or_path") or row.get("model_name") for row in (prediction, embedded, metric)]
    workflows = [row.get("workflow") or row.get("workflow_name") for row in (prediction, embedded, metric)]
    if not all(isinstance(value, str) and bool(value) for value in (*models, *workflows)):
        return False
    if len(set(models)) != 1 or len(set(workflows)) != 1:
        return False
    if not _historical_provider_failure_clear(embedded, metric):
        return False
    for field in ("agent_failures",):
        if field not in embedded or field not in metric:
            return False
        if embedded[field] != metric[field]:
            return False
    if any(field not in embedded or field not in metric for field in _BLOCKED_IDENTITY_FIELDS):
        return False
    if any(embedded[field] != metric[field] for field in _BLOCKED_IDENTITY_FIELDS):
        return False
    if any(field not in embedded or field not in metric for field in _BLOCKED_CAUSAL_FIELDS):
        return False
    if any(embedded[field] != metric[field] for field in _BLOCKED_CAUSAL_FIELDS):
        return False
    if _normalized_historical_llm_transport(embedded, metric) is None:
        return False
    invocation = str(metric["invocation_id"])
    run = str(metric["run_id"])
    runtime_tree = str(metric["runtime_tree_sha256"])
    if re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
        return False
    if not run or run != run.strip() or len(run) > 1024:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", runtime_tree) is None:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", str(metric["llm_base_url_sha256"])) is None:
        return False
    if not all(_positive_integer(metric[field]) for field in ("budget", "max_steps")):
        return False
    if not all(
        _positive_integer(metric[field])
        for field in ("context_window", "max_output_tokens")
    ):
        return False
    if not _finite_number(metric["temperature"]) or float(metric["temperature"]) < 0:
        return False
    if not _finite_number(metric["top_p"]) or not 0 <= float(metric["top_p"]) <= 1:
        return False
    workflow_env = metric["workflow_env"]
    if not isinstance(workflow_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in workflow_env.items()
    ):
        return False
    return all(
        isinstance(metric[field], str) and bool(metric[field].strip())
        for field in ("llm_model", "llm_provider", "wire_protocol", "reasoning_effort")
    )


def eval_only_generation_identity_status(
    prediction,
    metric,
    task,
    *,
    matching_official_eval_attempts,
):
    identity_metric = metric
    if (
        isinstance(metric, dict)
        and metric.get("workflow_status") == "blocked"
    ):
        if not _blocked_identity_document_proven(prediction, metric):
            return "invalid"
        agent_failures = metric.get("agent_failures")
        if not _agent_failure_evidence_valid(agent_failures):
            return "invalid"
        technical = _blocked_technical_interruption_proven(
            metric,
            matching_official_eval_attempts,
        )
        candidate_failure = _blocked_candidate_failure_proven(
            prediction,
            metric,
            matching_official_eval_attempts,
        )
        if not technical and not candidate_failure:
            return "invalid"
        identity_metric = dict(metric)
        identity_metric.pop("provider_failure", None)
        # A blocked workflow can retain an agent-stage failure after producing a
        # complete, quiesced candidate.  The eval-only gate proves that candidate
        # independently, while the original failure evidence remains untouched.
        identity_metric["agent_failures"] = []
        status = historical_generation_identity_status(prediction, identity_metric, task)
        if status != "verified":
            return "invalid"
        return (
            "blocked_technical_verified"
            if technical
            else "blocked_candidate_failure_verified"
        )
    return historical_generation_identity_status(prediction, identity_metric, task)


def generation_done_for_mode(run_dir, task, *, eval_only):
    if not eval_only:
        return generation_done(run_dir, task, require_identity=True)
    prediction, metric, pairing = latest_pair(run_dir, task)
    status = eval_only_generation_identity_status(
        prediction,
        metric,
        task,
        matching_official_eval_attempts=_matching_official_eval_attempt_count(
            run_dir,
            task,
        ),
    )
    return status != "invalid", prediction, metric, pairing
