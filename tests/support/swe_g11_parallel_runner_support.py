"""Shared preparation for swe g11 parallel runner."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import tests.support.generation_proof_test_support as proof_support
from opencollab_eval.engine import swe_v1_remote_records as remote_records
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


def _load_module():
    module = importlib.import_module("opencollab_eval.commands.swe_g11_parallel_runner")
    return importlib.reload(module)



def _args(**overrides):
    values = {
        "start_index": 51,
        "end_index": 75,
        "indices": "",
        "max_workers": 5,
        "min_workers": 1,
        "adaptive_recovery_tasks": 2,
        "run_id": "swe_g11_prolite51_75_test",
        "output_dir": Path("/tmp/swe_g11_prolite51_75_test"),
        "remote_base": "",
        "remote_eval_work_root": "/remote/eval_work",
        "remote_runtime_repo": "",
        "model_name": "model",
        "llm_model": "glm-5.2",
        "llm_provider": "anthropic",
        "context_window": 400_000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
        "session_prefix": "",
        "host": "host",
        "ssh_command": "ssh",
        "remote_python": "python3",
        "remote_root": "/remote/root",
        "image_repository": "registry.example/swe-images",
        "workflow": "validation-council-solve",
        "workflow_env": [],
        "openhands_command": "",
        "max_empty_patch_retries": 1,
        "remote_proxy_base_url": "http://127.0.0.1:18788",
        "local_proxy_base_url": "http://127.0.0.1:8878",
        "proxy_env_file": Path("/tmp/proxy.env"),
        "remote_api_env_file": "",
        "budget": 16,
        "max_steps": 60,
        "swe_timeout": 14400,
        "task_wall_timeout": 15300,
        "eval_timeout": 7200,
        "llm_timeout": 900,
        "checkpoint_interval": 0,
        "max_task_starts": 1,
        "max_eval_attempts": 9,
        "total_timeout": 240000,
        "runner_attempts": 3,
        "retry_delay_seconds": 60,
        "usd_cny": 6.76,
        "no_sync_runtime": False,
        "expected_runtime_tree_sha256": "",
        "no_ensure_remote_proxy": False,
        "skip_preflight": False,
        "skip_health_checks": False,
        "no_adaptive_concurrency": False,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)



def _direct_eval_summary(task: str, patch_sha: str, record_id: str) -> dict:
    target = f"pkg/{task.replace('-', '_')}_test.go::TestCase"
    f2p_plan = prolite_test_plan({"repo_language": "go"}, [target])
    p2p_plan = prolite_test_plan({"repo_language": "go"}, [])
    evidence = {
        "status": 0,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": True,
        "artifact_safe": True,
    }
    candidate_proof = proof_support.candidate_eval_proof_fields(
        task, record_id, patch_sha, base_commit="b" * 40, base_tree="c" * 40)
    return {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "task": task,
        "resolved": True,
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
        "filtered_patch_paths": [],
        "record_id": record_id, "eval_image_id": "sha256:" + "d" * 64,
        "eval_spec_sha256": "e" * 64,
        "technical_reasons": [],
        "output_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "candidate_expectation": candidate_proof[0], "candidate_projection": candidate_proof[1],
        "source_candidate_projection": proof_support.candidate_source_projection_fields(candidate_proof[0]),
        "tests_status": {
            "base_commit_status": 0,
            "service_bootstrap_status": 0,
            "before_repo_status": 0,
            "post_before_base_status": 0,
            "model_patch_status": 0,
            "test_patch_status": 0,
            "fail_to_pass_status": 0,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": [evidence],
            "pass_to_pass_evidence": [],
        },
    }



def _production_row(index: int, task: str, *, openhands: bool = False) -> dict:
    patch = "diff --git a/a.py b/a.py\n+fixed = True\n"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    record_id = f"record-{task}"
    metric = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": True,
        **proof_support.trusted_patch_proof_fields(patch),
    }
    prediction = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
    }
    generation = remote_records.generation_done_result(
        task, prediction, metric, "record_id"
    )
    return {
        "index": index,
        "task": task,
        "generation": generation,
        "eval": {
            "status": "eval_done",
            "task": task,
            "executed": False,
            "summary": _direct_eval_summary(task, patch_sha, record_id),
        },
    }



def _terminal_counts(*, technical: int = 0) -> dict:
    return {
        "tasks": 0 if technical else 1,
        "generation_done": 0 if technical else 1,
        "empty_patch": 0,
        "eval_done": 0 if technical else 1,
        "eval_attempts": 0 if technical else 1,
        "eval_retry_tasks": 0,
        "resolved": 0 if technical else 1,
        "unresolved": 0,
        "technical_failed": technical,
    }



def _reusable_summary(config, index: int, *, openhands: bool = False) -> dict:
    row = _production_row(index, f"task-{index}", openhands=openhands)
    summary = {
        "schema": "opencollab.swe_g11_prolite_runner.v1",
        "status": "done",
        "workflow": config.workflow,
        "model_name": config.model_name,
        "llm_model": config.llm_model,
        "llm_provider": config.llm_provider,
        "context_window": config.context_window,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_output_tokens,
        "budget": config.budget,
        "max_steps": config.max_steps,
        "max_task_starts": config.max_task_starts,
        "max_empty_patch_retries": config.max_empty_patch_retries,
        "max_eval_attempts": config.max_eval_attempts,
        "eval_container_bind_timeout": config.eval_container_bind_timeout,
        "workflow_env": {},
        "eval_only": False,
        "solver_attribution": "current_run",
        "remote_runtime_repo": config.remote_runtime_repo,
        "remote_python": config.remote_python,
        "base_run_dir": f"{config.remote_base}/task_{index}",
        "counts": _terminal_counts(),
        "rows": [row],
    }
    if openhands:
        summary["openhands_empty_patch_rejections"] = config.openhands_empty_patch_rejections
        summary["openhands_command_sha256"] = hashlib.sha256(
            config.openhands_command.encode()
        ).hexdigest()
    if config.runtime_tree_sha256:
        summary["runtime_tree_sha256"] = config.runtime_tree_sha256
    return summary

