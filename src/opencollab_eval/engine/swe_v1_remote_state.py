"""Configuration and shared mutable state for the V1 remote runner."""

# ruff: noqa: F401

from __future__ import annotations

import ast
import atexit
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any

from opencollab_eval.engine.solver_backend import (
    KIMI_CODING_BASE_URL,
    is_kimi_direct_model,
    normalize_llm_user_agent,
)
from opencollab_eval.engine.swe_eval_records import (
    SUBMISSION_INTEGRITY_INELIGIBLE,
    embedded_workflow_metric,
    metric_submission_integrity,
    open_regular_binary,
    patch_sha,
    patch_sha_matches,
    prediction_patch,
    row_explicit_patch_sha,
    row_patch_sha,
    row_record_id,
    row_task_id,
)

cfg: dict[str, Any] = {}
token = ""
owner_nonce = ""
remote_root = pathlib.Path(".")
remote_repo = pathlib.Path(".")
base_run_dir = pathlib.Path(".")
package_root = pathlib.Path(".")
dataset_path = pathlib.Path(".")
workflow = ""
workflow_env: dict[str, str] = {}
openhands_command = ""
openhands_command_sha256 = ""
openhands_empty_patch_rejections = 2
max_empty_patch_retries = 1
model_name = ""
llm_model = ""
llm_provider = ""
llm_transport = "reverse_proxy"
remote_api_network_env: dict[str, str] = {}
context_window: int | None = None
temperature: float | None = None
top_p: float | None = None
max_output_tokens: int | None = None
invocation_id = ""
run_id = ""
runtime_tree_sha256 = ""
session_prefix = ""
image_repository = ""
remote_proxy_base_url = ""
start_index = 0
limit = 0
budget = 0
max_steps = 0
swe_timeout = 0
task_wall_timeout = 0
eval_timeout = 0
checkpoint_interval = 0
max_task_starts = 0
max_eval_attempts = 2
eval_only = False
eval_dir_name = ""
expected_task = ""
expected_record_id = ""
expected_source_patch_sha256 = ""
expected_eval_patch_sha256 = ""
dry_run = False

ACTIVE_CHILD_PGIDS: set[int] = set()
ACTIVE_FIFO_PATHS: set[pathlib.Path] = set()
RUNNER_LOCK_FD: int | None = None
RUNNER_OWNER_RECORD: dict[str, Any] | None = None
RUNNER_STATE_THREAD_LOCK = threading.RLock()
PROCESS_TERM_GRACE_SECONDS = 30.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
PROCESS_CLEANUP_FAILED_EXIT_CODE = 125
SPAWN_SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_JSONL_RETAINED_BYTES = 64 * 1024 * 1024
MAX_JSONL_RETAINED_ROWS = 10_000
MAX_JSONL_SCAN_BYTES = 256 * 1024 * 1024
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_ROWS = 1_000_000
MAX_LOG_TAIL_BYTES = 4 * 1024 * 1024
MAX_TEST_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_TASK_ID_BYTES = 240
MAX_TASKS_PER_RUN = 1_000
MAX_DURABLE_JSONL_BYTES = 256 * 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_EXIT_STATUS_BYTES = 128
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
MAX_REMOTE_API_ENV_BYTES = 64 * 1024
REMOTE_API_TOKEN_KEYS = frozenset({
    "OPENCOLLAB_API_KEY",
    "OPENCOLLAB_UPSTREAM_API_KEY",
    "KIMI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
})
REMOTE_API_NETWORK_KEYS = frozenset({"HTTPS_PROXY"})


def stable_runtime_identity(identity):
    """Remove the per-process invocation field from a persisted identity."""
    if not isinstance(identity, dict):
        return {}
    return {key: value for key, value in identity.items() if key != "invocation_id"}


def read_remote_api_environment(path_value: str) -> dict[str, Any]:
    path = pathlib.Path(path_value)
    if not path.is_absolute():
        raise ValueError("remote API env file must use an absolute path")
    with open_regular_binary(path) as handle:
        opened = os.fstat(handle.fileno())
        if opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600:
            raise PermissionError("remote API env file must be owned by the runner and use mode 0600")
        raw = handle.read(MAX_REMOTE_API_ENV_BYTES + 1)
    if len(raw) > MAX_REMOTE_API_ENV_BYTES:
        raise ValueError("remote API env file exceeds its byte limit")
    token_value = ""
    network_env: dict[str, str] = {}
    for raw_line in raw.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        normalized_key = key.strip().upper()
        if not separator or normalized_key not in REMOTE_API_TOKEN_KEYS | REMOTE_API_NETWORK_KEYS:
            continue
        parsed = shlex.split(value, posix=True)
        if len(parsed) != 1 or not parsed[0]:
            raise ValueError(f"invalid {normalized_key} in remote API env file")
        parsed_value = parsed[0]
        if normalized_key in REMOTE_API_TOKEN_KEYS:
            token_value = token_value or parsed_value
            continue
        proxy = urllib.parse.urlsplit(parsed_value)
        if proxy.scheme not in {"http", "https"} or not proxy.hostname:
            raise ValueError(f"invalid {normalized_key} in remote API env file")
        previous = network_env.get(normalized_key)
        if previous and previous != parsed_value:
            raise ValueError(f"conflicting {normalized_key} in remote API env file")
        network_env[normalized_key] = parsed_value
    if not token_value:
        raise ValueError("remote API env file contains no supported API key")
    return {"token": token_value, "network_env": network_env}


def read_remote_api_token(path_value: str) -> str:
    return str(read_remote_api_environment(path_value)["token"])


def bind_remote_api_network_environment(
    target: dict[str, str], network_env: dict[str, str]
) -> None:
    for key in REMOTE_API_NETWORK_KEYS:
        target.pop(key, None)
        target.pop(key.lower(), None)
    target.update(network_env)


def configure(config: dict[str, Any]) -> None:
    """Validate and install one remote-run configuration."""
    global cfg, token, owner_nonce, remote_root, remote_repo, base_run_dir
    global package_root, dataset_path, workflow, workflow_env
    global openhands_command, openhands_command_sha256
    global openhands_empty_patch_rejections, max_empty_patch_retries
    global model_name, llm_model, llm_provider, llm_transport, remote_api_network_env
    global context_window, temperature
    global top_p, max_output_tokens, invocation_id, run_id, runtime_tree_sha256, session_prefix
    global image_repository
    global remote_proxy_base_url, start_index, limit, budget, max_steps
    global swe_timeout, task_wall_timeout, eval_timeout, checkpoint_interval
    global max_task_starts, max_eval_attempts, eval_only, eval_dir_name
    global expected_task, expected_record_id, expected_source_patch_sha256, expected_eval_patch_sha256
    global dry_run
    global ACTIVE_CHILD_PGIDS, ACTIVE_FIFO_PATHS
    global RUNNER_LOCK_FD, RUNNER_OWNER_RECORD, RUNNER_STATE_THREAD_LOCK

    requested_eval_only = bool(config.get("eval_only", False))
    required = (
        "owner_nonce",
        "remote_root",
        "remote_repo",
        "base_run_dir",
        "workflow",
        "model_name",
        "session_prefix",
        "image_repository",
        "remote_proxy_base_url",
    )
    missing = [name for name in required if not str(config.get(name) or "").strip()]
    remote_api_env_file = str(config.get("remote_api_env_file") or "").strip()
    configured_token = str(config.get("token") or "").strip()
    requested_transport = str(config.get("llm_transport") or "reverse_proxy")
    if requested_transport == "direct":
        if configured_token:
            raise ValueError("direct transport must not include a payload token")
        if not requested_eval_only and not remote_api_env_file:
            missing.insert(0, "remote_api_env_file")
    elif remote_api_env_file:
        raise ValueError("reverse_proxy transport must not include a remote API env file")
    elif not requested_eval_only and not configured_token:
        missing.insert(0, "token")
    if missing:
        raise ValueError("missing remote runner configuration: " + ", ".join(missing))
    cfg = dict(config)
    remote_api_environment = (
        read_remote_api_environment(remote_api_env_file)
        if remote_api_env_file and not requested_eval_only
        else {"token": configured_token, "network_env": {}}
    )
    token = str(remote_api_environment["token"])
    remote_api_network_env = dict(remote_api_environment["network_env"])
    owner_nonce = str(cfg["owner_nonce"])
    remote_root = pathlib.Path(cfg["remote_root"])
    remote_repo = pathlib.Path(cfg["remote_repo"])
    base_run_dir = pathlib.Path(cfg["base_run_dir"])
    package_root = remote_repo / "src"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    dataset_path = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    workflow = str(cfg["workflow"])
    workflow_env = {
        str(key): str(value) for key, value in (cfg.get("workflow_env") or {}).items()
    }
    allowed_workflow_env = {
        "OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES",
        "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY",
        "OPENCOLLAB_EAGER_TOOL_KEEP_RECENT",
        "OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS",
        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
        "OPENCOLLAB_TEMPERATURE",
        "OPENCOLLAB_THINKING",
        "OPENCOLLAB_THINKING_PARAMS",
        "OPENCOLLAB_TOP_P",
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS",
        "OPENCOLLAB_WIRE_PROTOCOL",
        "OPENCOLLAB_REASONING_EFFORT",
        "OPENCOLLAB_LLM_MAX_RETRIES",
        "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
        "OPENCOLLAB_LLM_USER_AGENT",
    }
    unsupported_workflow_env = sorted(set(workflow_env) - allowed_workflow_env)
    if unsupported_workflow_env:
        raise ValueError(
            "unsupported workflow env: " + ", ".join(unsupported_workflow_env)
        )
    if "OPENCOLLAB_LLM_USER_AGENT" in workflow_env:
        workflow_env["OPENCOLLAB_LLM_USER_AGENT"] = normalize_llm_user_agent(
            workflow_env["OPENCOLLAB_LLM_USER_AGENT"]
        )
    openhands_command = str(cfg.get("openhands_command") or "")
    openhands_command_sha256 = (
        hashlib.sha256(openhands_command.encode("utf-8")).hexdigest()
        if openhands_command
        else ""
    )
    openhands_empty_patch_rejections = max(
        0, int(cfg.get("openhands_empty_patch_rejections", 2))
    )
    max_empty_patch_retries = min(
        1, max(0, int(cfg.get("max_empty_patch_retries", 1)))
    )
    model_name = str(cfg["model_name"])
    llm_model = str(cfg.get("llm_model") or "")
    llm_provider = str(cfg.get("llm_provider") or "")
    remote_proxy_base_url = str(cfg["remote_proxy_base_url"]).rstrip("/")
    llm_transport = requested_transport
    if llm_transport not in {"direct", "reverse_proxy"}:
        raise ValueError("llm_transport must be direct or reverse_proxy")
    if llm_transport == "direct" and (
        llm_provider != "openai" or not is_kimi_direct_model(llm_model)
    ):
        raise ValueError("direct transport is supported only for OpenAI-compatible Kimi models")
    if llm_transport == "direct" and remote_proxy_base_url != KIMI_CODING_BASE_URL:
        raise ValueError("Kimi direct transport requires the official coding API base URL")
    context_window = cfg.get("context_window")
    temperature = cfg.get("temperature")
    top_p = cfg.get("top_p")
    max_output_tokens = cfg.get("max_output_tokens")
    invocation_id = str(cfg.get("invocation_id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise ValueError("invocation_id must be a 32-character lowercase hex UUID")
    run_id = str(cfg.get("run_id") or "").strip()
    runtime_tree_sha256 = str(cfg.get("runtime_tree_sha256") or "").strip()
    if runtime_tree_sha256 and re.fullmatch(r"[0-9a-f]{64}", runtime_tree_sha256) is None:
        raise ValueError("runtime_tree_sha256 must be a lowercase SHA-256 digest")
    session_prefix = str(cfg["session_prefix"]).rstrip("_")
    image_repository = str(cfg["image_repository"]).rstrip(":")
    start_index = int(cfg["start_index"])
    limit = int(cfg["limit"])
    budget = int(cfg["budget"])
    max_steps = int(cfg["max_steps"])
    swe_timeout = int(cfg["swe_timeout"])
    task_wall_timeout = int(cfg["task_wall_timeout"])
    eval_timeout = int(cfg["eval_timeout"])
    checkpoint_interval = int(cfg["checkpoint_interval"])
    max_task_starts = min(3, int(cfg["max_task_starts"]))
    max_eval_attempts = min(2, int(cfg.get("max_eval_attempts", 2)))
    eval_only = bool(cfg.get("eval_only", False))
    eval_dir_name = str(cfg.get("eval_dir_name") or "official_eval").strip()
    if (
        not eval_dir_name
        or "/" in eval_dir_name
        or "\\" in eval_dir_name
        or eval_dir_name in {".", ".."}
    ):
        raise ValueError("eval_dir_name must be a single directory name")
    expected_task = str(cfg.get("expected_task") or "")
    expected_record_id = str(cfg.get("expected_record_id") or "")
    expected_source_patch_sha256 = str(cfg.get("expected_source_patch_sha256") or "")
    expected_eval_patch_sha256 = str(cfg.get("expected_eval_patch_sha256") or "")
    expected_candidate_fields = (
        expected_task,
        expected_record_id,
        expected_source_patch_sha256,
        expected_eval_patch_sha256,
    )
    if any(expected_candidate_fields) and (
        not eval_only
        or not all(expected_candidate_fields)
        or len(expected_task.encode("utf-8")) > 256
        or len(expected_record_id.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in expected_task)
        or any(ord(character) < 32 for character in expected_record_id)
        or re.fullmatch(r"[0-9a-f]{64}", expected_source_patch_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_eval_patch_sha256) is None
    ):
        raise ValueError("invalid expected eval-only candidate identity")
    dry_run = bool(cfg["dry_run"])
    ACTIVE_CHILD_PGIDS = set()
    ACTIVE_FIFO_PATHS = set()
    RUNNER_LOCK_FD = None
    RUNNER_OWNER_RECORD = None
    RUNNER_STATE_THREAD_LOCK = threading.RLock()


def validate_runner_config():
    errors = []
    if start_index < 1:
        errors.append("start_index must be >= 1")
    if limit <= 0:
        errors.append("limit must be > 0")
    if limit > MAX_TASKS_PER_RUN:
        errors.append(f"limit must be <= {MAX_TASKS_PER_RUN}")
    if max_task_starts < 0:
        errors.append("max_task_starts must be >= 0")
    if max_eval_attempts <= 0:
        errors.append("max_eval_attempts must be > 0")
    return errors


def state_names() -> tuple[str, ...]:
    return tuple(name for name in globals() if not name.startswith("__") and name not in {"configure", "state_names"})


__all__ = [name for name in globals() if not name.startswith("__")]
