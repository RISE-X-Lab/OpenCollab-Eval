from __future__ import annotations

from typing import Any


def preflight_summary(config: Any, runtime_tree_sha256: str = "a" * 64) -> dict[str, Any]:
    budget = config.provider_error_time_budget
    return {
        "status": "dry_run",
        "runtime_tree_sha256": runtime_tree_sha256,
        "workflow": config.workflow,
        "workflow_env": dict(item.split("=", 1) for item in config.workflow_env),
        "model_name": config.model_name,
        "llm_model": config.llm_model,
        "llm_provider": config.llm_provider,
        "context_window": config.context_window,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_output_tokens,
        "budget": config.budget,
        "max_steps": config.max_steps,
        "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
        "max_empty_patch_retries": config.max_empty_patch_retries,
        "max_task_starts": config.max_task_starts,
        "max_eval_attempts": config.max_eval_attempts,
        "provider_time_budget": {
            "error_seconds": budget,
            "base": {
                "llm": config.llm_timeout,
                "generation": config.swe_timeout,
                "task_wall": config.task_wall_timeout,
                "controller": config.total_timeout,
                "official_eval": config.eval_timeout,
            },
            "effective": {
                "llm_normal": config.llm_timeout,
                "llm_wall": config.llm_timeout + budget,
                "generation": config.swe_timeout + budget,
                "task_wall": config.task_wall_timeout + budget,
                "controller": config.total_timeout + budget,
                "official_eval": config.eval_timeout,
            },
        },
    }
