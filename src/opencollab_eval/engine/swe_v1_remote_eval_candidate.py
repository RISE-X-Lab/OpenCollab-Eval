"""Strict historical candidate selection for eval-only runs."""

from __future__ import annotations

import math
import re

from opencollab_eval.engine.provider_failures import summarize_terminal_provider_failures
from opencollab_eval.engine.swe_v1_remote_records import (
    embedded_workflow_metric,
    generation_done,
    historical_generation_identity_status,
    latest_pair,
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


def eval_only_generation_identity_status(prediction, metric, task):
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
        identity_metric = dict(metric)
        identity_metric.pop("provider_failure", None)
        # A blocked workflow can retain an agent-stage failure after producing a
        # complete, quiesced candidate.  The eval-only gate proves that candidate
        # independently, while the original failure evidence remains untouched.
        identity_metric["agent_failures"] = []
    return historical_generation_identity_status(prediction, identity_metric, task)


def generation_done_for_mode(run_dir, task, *, eval_only):
    if not eval_only:
        return generation_done(run_dir, task, require_identity=True)
    prediction, metric, pairing = latest_pair(run_dir, task)
    status = eval_only_generation_identity_status(prediction, metric, task)
    return status != "invalid", prediction, metric, pairing
