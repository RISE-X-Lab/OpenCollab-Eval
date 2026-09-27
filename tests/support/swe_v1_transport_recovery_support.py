"""Shared evaluation-only runner arguments."""

from __future__ import annotations

from types import SimpleNamespace


def _eval_only_args(**overrides: object) -> SimpleNamespace:
    values = {
        "ssh_command": "ssh",
        "eval_only": True,
        "no_sync_runtime": True,
        "expected_runtime_tree_sha256": "a" * 64,
        "host": "example",
        "remote_proxy_base_url": "http://remote",
        "remote_runtime_repo": "/remote/repo",
        "remote_root": "/remote",
        "base_run_dir": "/remote/run",
        "workflow": "team-pro",
        "model_name": "model",
        "session_prefix": "session",
        "image_repository": "registry.example/swebench",
        "start_index": 1,
        "limit": 1,
        "budget": 1000,
        "max_steps": 3,
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "llm_timeout": 10,
        "checkpoint_interval": 0,
        "max_task_starts": 1,
        "dry_run": False,
        "total_timeout": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
