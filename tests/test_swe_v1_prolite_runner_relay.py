from __future__ import annotations

from types import SimpleNamespace

import pytest
from swe_v1_prolite_runner_test_support import (
    _complete_remote_config,
    remote_state,
    runner,
)


def _eval_only_args() -> SimpleNamespace:
    return SimpleNamespace(
        ssh_command="ssh",
        eval_only=True,
        no_sync_runtime=True,
        expected_runtime_tree_sha256="a" * 64,
        host="example",
        remote_proxy_base_url="http://remote",
        remote_runtime_repo="/remote/repo",
        remote_root="/remote",
        base_run_dir="/remote/run",
        workflow="team-pro",
        workflow_env=[],
        openhands_command="",
        openhands_empty_patch_rejections=2,
        max_empty_patch_retries=1,
        model_name="model",
        llm_model="glm-5.2",
        llm_provider="anthropic",
        context_window=400000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32768,
        session_prefix="session",
        image_repository="registry.example/swebench",
        start_index=1,
        limit=1,
        budget=4000000,
        max_steps=60,
        swe_timeout=14400,
        task_wall_timeout=15300,
        eval_timeout=7200,
        llm_timeout=900,
        checkpoint_interval=0,
        max_task_starts=1,
        max_eval_attempts=2,
        eval_dir_name="official_eval",
        dry_run=False,
        total_timeout=240000,
    )


@pytest.fixture(autouse=True)
def _verified_runtime(monkeypatch):
    monkeypatch.setattr(
        runner,
        "verify_remote_runtime",
        lambda **kwargs: {"sha256": "a" * 64},
    )


def test_startup_failure_preserves_remote_error_without_unowned_cleanup(monkeypatch):
    class ExitedProcess:
        pid = 4321
        returncode = 1

    cleanup_calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda *args, **kwargs: ("", "unsupported workflow env: setting"),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pid: False)
    monkeypatch.setattr(
        runner,
        "probe_remote_execution_state",
        lambda **kwargs: {"runner_state": "missing", "summary": None},
    )
    monkeypatch.setattr(runner, "terminate_local_process_group", lambda proc: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: (cleanup_calls.append(kwargs) or {"ok": True}, None),
    )

    with pytest.raises(RuntimeError, match="unsupported workflow env: setting"):
        runner.run_remote(_eval_only_args())

    assert cleanup_calls == []


def test_workflow_env_accepts_sampling_settings_and_rejects_secrets():
    assert runner.normalize_workflow_env(
        [
            "OPENCOLLAB_TEMPERATURE=1",
            "OPENCOLLAB_MAX_OUTPUT_TOKENS=32768",
            "OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES=0",
            "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY=1",
            "OPENCOLLAB_EAGER_TOOL_KEEP_RECENT=1",
            "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS=1200",
        ]
    ) == {
        "OPENCOLLAB_TEMPERATURE": "1",
        "OPENCOLLAB_MAX_OUTPUT_TOKENS": "32768",
        "OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES": "0",
        "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY": "1",
        "OPENCOLLAB_EAGER_TOOL_KEEP_RECENT": "1",
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS": "1200",
    }
    with pytest.raises(ValueError, match="unsupported --workflow-env"):
        runner.normalize_workflow_env(["OPENCOLLAB_API_KEY=secret"])


def test_workflow_env_accepts_responses_runtime_settings():
    assert runner.normalize_workflow_env(
        [
            "OPENCOLLAB_WIRE_PROTOCOL=responses",
            "OPENCOLLAB_REASONING_EFFORT=xhigh",
            "OPENCOLLAB_LLM_MAX_RETRIES=10000",
            "OPENCOLLAB_LLM_CONNECT_TIMEOUT=30",
            "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=300",
            "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=300",
            "OPENCOLLAB_LLM_USER_AGENT=compatible-client/1.0",
        ]
    ) == {
        "OPENCOLLAB_WIRE_PROTOCOL": "responses",
        "OPENCOLLAB_REASONING_EFFORT": "xhigh",
        "OPENCOLLAB_LLM_MAX_RETRIES": "10000",
        "OPENCOLLAB_LLM_CONNECT_TIMEOUT": "30",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT": "300",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT": "300",
        "OPENCOLLAB_LLM_USER_AGENT": "compatible-client/1.0",
    }


@pytest.mark.parametrize(
    "value",
    ["bad\nheader", "client-é", "x" * 257],
)
def test_workflow_env_rejects_unsafe_model_user_agent(value):
    with pytest.raises(ValueError, match="OPENCOLLAB_LLM_USER_AGENT"):
        runner.normalize_workflow_env([f"OPENCOLLAB_LLM_USER_AGENT={value}"])


def test_remote_runner_accepts_bounded_repository_map_setting():
    config = _complete_remote_config(
        {
            "token": "x",
            "remote_root": "/tmp/remote",
            "remote_repo": "/tmp/repo",
            "base_run_dir": "/tmp/run",
            "workflow": "validation-council-solve",
            "workflow_env": {
                "OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES": "0",
                "OPENCOLLAB_LLM_USER_AGENT": "compatible-client/1.0",
                "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS": "1200",
            },
            "model_name": "model",
            "session_prefix": "session",
            "remote_proxy_base_url": "http://127.0.0.1:1",
            "start_index": 1,
            "limit": 1,
            "budget": 1,
            "max_steps": 1,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 0,
            "max_task_starts": 1,
            "max_eval_attempts": 1,
            "dry_run": True,
        }
    )

    remote_state.configure(config)

    assert remote_state.workflow_env == {
        "OPENCOLLAB_EVAL_REPOSITORY_MAP_BYTES": "0",
        "OPENCOLLAB_LLM_USER_AGENT": "compatible-client/1.0",
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS": "1200",
    }
    config["workflow_env"] = {"OPENCOLLAB_LLM_USER_AGENT": "bad\nheader"}
    with pytest.raises(ValueError, match="OPENCOLLAB_LLM_USER_AGENT"):
        remote_state.configure(config)
