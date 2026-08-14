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
