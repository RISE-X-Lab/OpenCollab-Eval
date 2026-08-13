"""Stable ownership claim for one SWE remote runner invocation."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def runner_claim_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return the stable fields that may identify a recoverable remote run."""
    openhands_command = str(config.get("openhands_command") or "")
    return {
        "base_run_dir": str(config.get("base_run_dir") or ""),
        "remote_root": str(config.get("remote_root") or ""),
        "remote_repo": str(config.get("remote_repo") or ""),
        "remote_python": str(config.get("remote_python") or "python3"),
        "run_id": str(config.get("run_id") or ""),
        "runtime_tree_sha256": str(config.get("runtime_tree_sha256") or ""),
        "session_prefix": str(config.get("session_prefix") or ""),
        "image_repository": str(config.get("image_repository") or ""),
        "start_index": int(config.get("start_index") or 0),
        "limit": int(config.get("limit") or 0),
        "workflow": str(config.get("workflow") or ""),
        "workflow_env": {
            str(key): str(value)
            for key, value in sorted((config.get("workflow_env") or {}).items())
        },
        "model_name": str(config.get("model_name") or ""),
        "llm_model": str(config.get("llm_model") or ""),
        "llm_provider": str(config.get("llm_provider") or ""),
        "llm_transport": str(config.get("llm_transport") or "reverse_proxy"),
        "context_window": config.get("context_window"),
        "temperature": config.get("temperature"),
        "top_p": config.get("top_p"),
        "max_output_tokens": config.get("max_output_tokens"),
        "budget": int(config.get("budget") or 0),
        "max_steps": int(config.get("max_steps") or 0),
        "swe_timeout": int(config.get("swe_timeout") or 0),
        "task_wall_timeout": int(config.get("task_wall_timeout") or 0),
        "eval_timeout": int(config.get("eval_timeout") or 0),
        "llm_timeout": int(config.get("llm_timeout") or 0),
        "max_task_starts": max(0, min(3, int(config.get("max_task_starts") or 0))),
        "max_empty_patch_retries": max(
            0, min(1, int(config.get("max_empty_patch_retries") or 0))
        ),
        "max_eval_attempts": max(
            1, min(2, int(config.get("max_eval_attempts") or 1))
        ),
        "eval_only": bool(config.get("eval_only", False)),
        "eval_dir_name": str(config.get("eval_dir_name") or "official_eval"),
        "dry_run": bool(config.get("dry_run", False)),
        "expected_task": str(config.get("expected_task") or ""),
        "expected_record_id": str(config.get("expected_record_id") or ""),
        "expected_source_patch_sha256": str(
            config.get("expected_source_patch_sha256") or ""
        ),
        "expected_eval_patch_sha256": str(
            config.get("expected_eval_patch_sha256") or ""
        ),
        "openhands_command_sha256": (
            hashlib.sha256(openhands_command.encode("utf-8")).hexdigest()
            if openhands_command
            else ""
        ),
    }


def runner_claim_sha256(config: dict[str, Any]) -> str:
    encoded = json.dumps(
        runner_claim_identity(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["runner_claim_identity", "runner_claim_sha256"]
