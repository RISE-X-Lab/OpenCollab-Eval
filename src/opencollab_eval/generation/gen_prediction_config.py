"""Prediction-generation identity, image, and limit validation."""

from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import re
import unicodedata
import uuid
from pathlib import PureWindowsPath

from .gen_prediction_constants import MAX_INSTANCE_ID_BYTES


def unique_container_name(prefix: str, instance_id: str) -> str:
    """Return an ASCII Docker name without embedding attacker-controlled text."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix) is None:
        raise ValueError("container name prefix is unsafe")
    validated = validate_instance_id(instance_id)
    digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()[:12]
    suffix = uuid.uuid4().hex[:16]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", validated).strip(".-")
    slug = slug or "instance"
    max_slug_chars = max(1, 63 - len(prefix) - len(digest) - len(suffix) - 2)
    return f"{prefix}{slug[:max_slug_chars]}-{digest}-{suffix}"


def validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows_path = PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows_path.is_absolute()
        or "/" in value
        or "\\" in value
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
    ):
        raise ValueError("instance_id must be one safe path component")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_INSTANCE_ID_BYTES:
        raise ValueError("instance_id exceeds its UTF-8 byte limit")
    return value


def _stable_docker_component(value: str, *, max_chars: int = 96) -> str:
    """Map arbitrary valid text to a stable lowercase Docker name component."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip(".-_")
    slug = slug or "instance"
    safe_unchanged = slug == value and len(value) <= max_chars
    if safe_unchanged:
        return value
    slug = slug[: max(1, max_chars - len(digest) - 1)].rstrip(".-_") or "instance"
    return f"{slug}-{digest}"


def default_container_image(arch: str, instance_id: str) -> str:
    validated = validate_instance_id(instance_id)
    arch_component = _stable_docker_component(str(arch), max_chars=32)
    instance_component = _stable_docker_component(validated)
    return f"sweb.eval.{arch_component}.{instance_component}:latest"


def _docker_timeout_from_env() -> float:
    raw = os.environ.get("OPENCOLLAB_DOCKER_TIMEOUT", "60").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"OPENCOLLAB_DOCKER_TIMEOUT must be a positive number, got {raw!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"OPENCOLLAB_DOCKER_TIMEOUT must be a positive number, got {raw!r}")
    return timeout


def _workspace_archive_timeout_from_env() -> float:
    raw = os.environ.get("OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT", "900").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(
            "OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT must be a positive number, "
            f"got {raw!r}"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT must be a positive number, "
            f"got {raw!r}"
        )
    return timeout


def validate_generation_limits(
    *,
    max_steps: object,
    budget: object,
    timeout: object,
) -> tuple[int, int, float]:
    values: dict[str, int] = {}
    for name, value in (("--max-steps", max_steps), ("--budget", budget)):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive integer")
        try:
            integer = operator.index(value)
        except TypeError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if integer <= 0:
            raise ValueError(f"{name} must be a positive integer")
        values[name] = integer
    if isinstance(timeout, bool):
        raise ValueError("--timeout must be a positive finite number")
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("--timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("--timeout must be a positive finite number")
    return values["--max-steps"], values["--budget"], timeout_seconds


def bind_llm_transport(metrics: dict) -> None:
    if os.environ.get("OPENCOLLAB_LLM_TRANSPORT") == "direct":
        metrics["llm_transport"] = "direct"
    for metric_key, env_key in (
        ("invocation_id", "OPENCOLLAB_EVAL_INVOCATION_ID"),
        ("run_id", "OPENCOLLAB_EVAL_RUN_ID"),
        ("runtime_tree_sha256", "OPENCOLLAB_RUNTIME_TREE_SHA256"),
    ):
        value = os.environ.get(env_key, "").strip()
        if value:
            metrics[metric_key] = value
    base_url_sha256 = os.environ.get("OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256", "").strip()
    if base_url_sha256:
        if re.fullmatch(r"[0-9a-f]{64}", base_url_sha256) is None:
            raise ValueError("OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256 must be a SHA-256 digest")
        metrics["llm_base_url_sha256"] = base_url_sha256
    workflow_env_json = os.environ.get("OPENCOLLAB_EVAL_WORKFLOW_ENV", "").strip()
    if workflow_env_json:
        workflow_env = json.loads(workflow_env_json)
        if not isinstance(workflow_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in workflow_env.items()
        ):
            raise ValueError("OPENCOLLAB_EVAL_WORKFLOW_ENV must be a string mapping")
        metrics["workflow_env"] = dict(sorted(workflow_env.items()))
        for metric_key, env_key in (
            ("wire_protocol", "OPENCOLLAB_WIRE_PROTOCOL"),
            ("reasoning_effort", "OPENCOLLAB_REASONING_EFFORT"),
        ):
            value = workflow_env.get(env_key)
            if value:
                metrics[metric_key] = value


#: The environment switches that decide how a run talks to its provider.
#:
#: Recorded rather than assumed, because none of them is visible in the
#: prediction a run produces and several of them change what the run *is*.
#: ``OPENCOLLAB_LLM_STREAM_CHAT`` is the sharpest case: the reasoning body of a
#: response cannot be retrieved from a non-streaming chat completion at all, so
#: a batch run with it off has paid for reasoning it did not keep -- and with
#: the switch unrecorded, "was it on?" is not answerable afterwards from the
#: run's own record.
LLM_ENV_KEYS: tuple[str, ...] = (
    "OPENCOLLAB_MAX_OUTPUT_TOKENS",
    "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY",
    "OPENCOLLAB_TEMPERATURE",
    "OPENCOLLAB_THINKING",
    "OPENCOLLAB_THINKING_PARAMS",
    "OPENCOLLAB_TOP_P",
    "OPENCOLLAB_WIRE_PROTOCOL",
    "OPENCOLLAB_REASONING_EFFORT",
    "OPENCOLLAB_LLM_MAX_RETRIES",
    "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
    "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT",
    "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
    "OPENCOLLAB_LLM_STREAM_CHAT",
    "OPENCOLLAB_LLM_USER_AGENT",
    "OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT",
)

#: The metric keys ``llm_transport_metrics`` writes. Named so the cross-arm
#: alignment check can ask each generator whether it writes them without
#: tabulating the answer.
LLM_TRANSPORT_METRIC_KEYS: tuple[str, ...] = (
    "llm_base_url_sha256",
    "reasoning_effort",
    "wire_protocol",
    "workflow_env",
)


def observed_llm_env() -> dict[str, str]:
    """The provider-transport environment this process was started with.

    Only the keys that are set: an absent key and an empty one are different
    facts, and writing ``""`` for "not set" would make them read the same.
    """
    return {key: os.environ[key] for key in LLM_ENV_KEYS if key in os.environ}


def llm_transport_metrics(cfg: dict) -> dict:
    """How this run reached the provider, in the four keys every arm records.

    One function rather than one block per generator. These four keys were
    written by the workflow/team generator and by no other: the single-agent
    path recorded the model and the sampling settings but nothing about the
    wire, so of the arms a comparison is made between, three could answer "which
    protocol, which reasoning effort, which endpoint, which switches" and one
    could not. That is a difference in an input to every per-run analysis, on an
    axis the arms are supposed to be identical on, and it is invisible in the
    predictions themselves.

    ``bind_llm_transport`` may overwrite any of these afterwards from the
    environment a harness exported; the order is the same on every arm.
    """
    return {
        "wire_protocol": cfg.get("wire_protocol", "chat_completions"),
        "reasoning_effort": cfg.get("reasoning_effort"),
        "llm_base_url_sha256": cfg.get("base_url_sha256"),
        "workflow_env": observed_llm_env(),
    }
