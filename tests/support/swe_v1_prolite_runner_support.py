"""Shared preparation for swe v1 prolite runner."""

from __future__ import annotations


def _complete_remote_config(config: dict) -> dict:
    completed = dict(config)
    completed.setdefault("owner_nonce", "d" * 32)
    completed.setdefault("invocation_id", "e" * 32)
    completed.setdefault("workflow_env", {})
    completed.setdefault("openhands_command", "")
    completed.setdefault("openhands_empty_patch_rejections", 2)
    completed.setdefault("max_empty_patch_retries", 1)
    completed.setdefault("llm_model", "")
    completed.setdefault("llm_provider", "anthropic")
    completed.setdefault("context_window", None)
    completed.setdefault("temperature", None)
    completed.setdefault("top_p", None)
    completed.setdefault("max_output_tokens", None)
    completed.setdefault("image_repository", "registry.example/swebench")
    completed.setdefault("max_eval_attempts", 2)
    completed.setdefault("eval_only", False)
    completed.setdefault("eval_dir_name", "official_eval")
    return completed

