from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.swe_g11_parallel_runner_support import (
    _args,
    _load_module,
    _reusable_summary,
    _terminal_counts,
)


def test_parallel_config_uses_requested_range_and_worker_count():
    module = _load_module()

    config = module.resolve_config(_args())

    assert config.indices == tuple(range(51, 76))
    assert config.max_workers == 5
    assert config.min_workers == 1
    assert config.adaptive_concurrency is True
    assert config.adaptive_recovery_tasks == 2
    assert config.run_id == "swe_g11_prolite51_75_test"
    assert config.remote_base == "/remote/eval_work/swe_g11_prolite51_75_test"
    assert config.remote_runtime_repo == "/remote/eval_work/swe_g11_prolite51_75_test/_runtime/repo"
    assert config.output_dir == Path("/tmp/swe_g11_prolite51_75_test")
    assert config.max_eval_attempts == 2
    assert config.max_empty_patch_retries == 1
    assert config.workflow_env == ()
    assert config.llm_model == "glm-5.2"
    assert config.llm_provider == "anthropic"
    assert config.context_window == 400_000
    assert config.image_repository == "registry.example/swe-images"


def test_parser_defaults_to_g11_three_task_starts_with_explicit_runtime_config():
    module = _load_module()

    args = module.build_parser().parse_args(
        [
            "--start-index",
            "51",
            "--end-index",
            "75",
            "--remote-eval-work-root",
            "/remote/eval",
            "--remote-root",
            "/remote/root",
            "--image-repository",
            "registry.example/swe-images",
            "--model-name",
            "model",
            "--llm-model",
            "llm",
            "--host",
            "host",
            "--proxy-env-file",
            "/tmp/proxy.env",
            "--remote-proxy-base-url",
            "http://remote-proxy.invalid",
            "--local-proxy-base-url",
            "http://local-proxy.invalid",
        ]
    )
    config = module.resolve_config(args)
    assert config.indices == tuple(range(51, 76))
    assert config.max_workers == 5
    assert config.max_task_starts == 3
    command = module.task_command(config, 51)
    assert command[command.index("--max-task-starts") + 1] == "3"


def test_task_starts_are_clamped_to_one_through_three():
    module = _load_module()

    assert module.resolve_config(_args(max_task_starts=9)).max_task_starts == 3
    assert module.resolve_config(_args(max_task_starts=0)).max_task_starts == 1


def test_empty_patch_retries_are_clamped_to_zero_or_one():
    module = _load_module()

    assert module.resolve_config(_args(max_empty_patch_retries=-1)).max_empty_patch_retries == 0
    assert module.resolve_config(_args(max_empty_patch_retries=9)).max_empty_patch_retries == 1


def test_parser_accepts_compact_sparse_ranges():
    module = _load_module()

    config = module.resolve_config(_args(indices="1-3,7,10-12", start_index=None, end_index=None))

    assert config.indices == (1, 2, 3, 7, 10, 11, 12)
    assert module.range_label(config.indices) == "1-3,7,10-12"


def test_workflow_env_is_validated_and_forwarded():
    module = _load_module()
    expected = (
        "OPENCOLLAB_TEMPERATURE=1",
        "OPENCOLLAB_TOP_P=1",
        "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY=1",
        "OPENCOLLAB_LLM_USER_AGENT=compatible-client/1.0", "OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT=1200",
    )
    config = module.resolve_config(_args(workflow_env=list(expected)))
    command = module.task_command(config, 51)
    assert config.workflow_env == expected
    assert command.count("--workflow-env") == len(expected)
    assert all(item in command for item in expected)


def test_task_command_forwards_typed_llm_settings():
    module = _load_module()
    config = module.resolve_config(_args(remote_python="/remote/runtime/bin/python"))

    command = module.task_command(config, 51)

    assert command[command.index("--llm-model") + 1] == "glm-5.2"
    assert command[command.index("--context-window") + 1] == "400000"
    assert command[command.index("--temperature") + 1] == "1.0"
    assert command[command.index("--top-p") + 1] == "1.0"
    assert command[command.index("--max-output-tokens") + 1] == "32768"
    assert command[command.index("--image-repository") + 1] == "registry.example/swe-images"
    assert command[command.index("--llm-provider") + 1] == "anthropic"
    assert command[command.index("--remote-python") + 1] == (
        "/remote/runtime/bin/python"
    )


def test_workflow_env_rejects_secret_or_arbitrary_keys():
    module = _load_module()
    with pytest.raises(ValueError, match="unsupported --workflow-env"):
        module.resolve_config(_args(workflow_env=["OPENCOLLAB_API_KEY=secret"]))


def test_preflight_forwards_budget_and_step_limit(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            budget=4_000_000,
            max_steps=60,
            workflow_env=["OPENCOLLAB_MAX_OUTPUT_TOKENS=32768"],
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        report = {
            "status": "dry_run",
            "runtime_tree_sha256": "a" * 64,
            "workflow": config.workflow,
            "workflow_env": {"OPENCOLLAB_MAX_OUTPUT_TOKENS": "32768"},
            "budget": 4_000_000,
            "max_steps": 60,
            "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
            "max_empty_patch_retries": config.max_empty_patch_retries,
            "max_task_starts": config.max_task_starts,
            "max_eval_attempts": config.max_eval_attempts,
        }
        (tmp_path / "shared_runtime_preflight.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return module.subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    original = module._run_task_process
    try:
        module._run_task_process = fake_run
        runtime_tree_sha256 = module.prepare_runtime(config)
    finally:
        module._run_task_process = original

    command = captured["command"]
    assert command[command.index("--budget") + 1] == "4000000"
    assert command[command.index("--max-steps") + 1] == "60"
    assert command[command.index("--image-repository") + 1] == "registry.example/swe-images"
    assert "OPENCOLLAB_MAX_OUTPUT_TOKENS=32768" in command
    assert runtime_tree_sha256 == "a" * 64


def test_task_command_is_built_from_requested_index():
    module = _load_module()
    config = module.resolve_config(
        _args(
            no_sync_runtime=True,
            no_ensure_remote_proxy=True,
            expected_runtime_tree_sha256="a" * 64,
        )
    )
    command = module.task_command(config, 75)
    joined = " ".join(command)

    assert "--start-index" in command
    assert command[command.index("--start-index") + 1] == "75"
    assert command[command.index("--limit") + 1] == "1"
    assert command[command.index("--base-run-dir") + 1] == "/remote/eval_work/swe_g11_prolite51_75_test/task_75"
    assert command[command.index("--json-output") + 1] == "/tmp/swe_g11_prolite51_75_test/task_75_report.json"
    assert command[command.index("--max-task-starts") + 1] == "1"
    assert command[command.index("--llm-provider") + 1] == "anthropic"
    assert "--no-sync-runtime" in command
    assert command[command.index("--expected-runtime-tree-sha256") + 1] == "a" * 64
    assert "--no-ensure-remote-proxy" in command
    assert "39_50" not in joined
    assert "36_50" not in joined


def test_task_command_forwards_openhands_command():
    module = _load_module()
    config = module.resolve_config(
        _args(
            workflow="openhands-external",
            openhands_command="openhands --prompt-file {prompt_file}",
        )
    )

    command = module.task_command(config, 51)

    assert "--openhands-command" in command
    assert command[command.index("--openhands-command") + 1] == "openhands --prompt-file {prompt_file}"
    assert command[command.index("--openhands-empty-patch-rejections") + 1] == "2"


def test_openhands_command_is_not_read_directly_from_environment(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("OPENCOLLAB_OPENHANDS_COMMAND", "hidden-command")

    args = module.build_parser().parse_args(["--indices", "1"])

    assert args.openhands_command == ""


def test_openhands_completed_report_reuse_requires_same_command():
    module = _load_module()
    command = "openhands --headless --file {prompt_file} --override-with-envs"
    config = replace(
        module.resolve_config(
            _args(
                start_index=1,
                end_index=1,
                workflow="openhands-external",
                openhands_command=command,
                remote_base="/remote/openhands",
            )
        ),
        runtime_tree_sha256="a" * 64,
    )
    summary = _reusable_summary(config, 1, openhands=True)

    assert module.report_is_reusable(summary, config, 1) is True
    assert module.report_is_reusable(
        summary,
        replace(config, runtime_tree_sha256="b" * 64),
        1,
    ) is False
    assert summary["rows"][0]["eval"]["executed"] is False
    without_snapshot = json.loads(json.dumps(summary))
    del without_snapshot["rows"][0]["generation"]["solver_git_snapshot"]
    assert module.report_is_reusable(without_snapshot, config, 1) is False
    changed = replace(
        module.resolve_config(
            _args(
                start_index=1,
                end_index=1,
                workflow="openhands-external",
                openhands_command="openhands --version",
                remote_base="/remote/openhands",
            )
        ),
        runtime_tree_sha256="a" * 64,
    )
    assert module.report_is_reusable(summary, changed, 1) is False
    weak_summary = json.loads(json.dumps(summary))
    weak_summary["rows"][0]["eval"]["summary"].pop("tests_status")
    assert module.report_is_reusable(weak_summary, config, 1) is False
    with pytest.raises(ValueError, match="openhands-external requires"):
        module.resolve_config(
            _args(
                start_index=1,
                end_index=1,
                workflow="openhands-external",
                openhands_command="",
                remote_base="/remote/openhands",
            )
        )


def test_aggregate_uses_configured_indices_for_done_status():
    module = _load_module()
    config = module.resolve_config(_args(indices="51,53", start_index=None, end_index=None))
    results = [
        dict(index=51, completed=True, returncode=0, tasks=1, generation_done=1, eval_done=1, attempts=1),
        dict(index=53, completed=True, returncode=1, tasks=1, generation_done=1, eval_done=1, attempts=1),
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "done"
    assert summary["range"] == "51,53"
    assert summary["indices"] == [51, 53]
    assert summary["counts"]["tasks"] == 2
    assert summary["workflow"] == "validation-council-solve"
    assert summary["workflow_env"] == []
    assert summary["llm_model"] == "glm-5.2"
    assert summary["llm_provider"] == "anthropic"
    assert summary["context_window"] == 400_000
    assert summary["temperature"] == 1.0
    assert summary["top_p"] == 1.0
    assert summary["max_output_tokens"] == 32_768
    assert summary["budget"] == 16
    assert summary["max_steps"] == 60


def test_aggregate_marks_completed_technical_failures_explicitly():
    module = _load_module()
    config = module.resolve_config(_args(indices="51", start_index=None, end_index=None))
    results = [
        {
            "index": 51,
            "completed": True,
            "returncode": 1,
            "tasks": 0,
            "generation_done": 0,
            "eval_done": 0,
            "technical_failed": 1,
            "runner_status": "preflight_failed",
            "attempts": 1,
        }
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["technical_failed"] == 1


def test_run_one_retries_transient_preflight_report(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
            runner_attempts=3,
            retry_delay_seconds=0,
        )
    )
    report_path = module.task_paths(config, 51)["json_report"]
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = {
                "schema": "opencollab.swe_g11_prolite_runner.v1",
                "status": "preflight_failed",
                "counts": _terminal_counts(technical=1),
                "rows": [],
            }
            returncode = 2
        else:
            payload = _reusable_summary(config, 51)
            returncode = 0
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    old_run = module._run_task_process
    try:
        module._run_task_process = fake_run
        result = module.run_one(config, 51)
    finally:
        module._run_task_process = old_run

    assert calls == 2
    assert result["runner_status"] == "done"
    assert result["completed"] is True
    assert result["attempts"] == 2
    assert result["technical_failed"] == 0


def test_run_one_returns_last_preflight_failure_after_runner_attempts(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
            runner_attempts=3,
            retry_delay_seconds=0,
        )
    )
    report_path = module.task_paths(config, 51)["json_report"]
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        report_path.write_text(
            json.dumps(
                {
                    "schema": "opencollab.swe_g11_prolite_runner.v1",
                    "status": "preflight_failed",
                    "counts": _terminal_counts(technical=1),
                    "rows": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=2, stdout="", stderr="")

    old_run = module._run_task_process
    try:
        module._run_task_process = fake_run
        result = module.run_one(config, 51)
    finally:
        module._run_task_process = old_run

    assert calls == 3
    assert result["runner_status"] == "preflight_failed"
    assert result["completed"] is True
    assert result["attempts"] == 3
    assert result["technical_failed"] == 1


def test_run_one_reuses_completed_report_without_subprocess(tmp_path):
    module = _load_module()
    config = replace(
        module.resolve_config(
            _args(
                indices="51",
                start_index=None,
                end_index=None,
                output_dir=tmp_path,
            )
        ),
        runtime_tree_sha256="a" * 64,
    )
    report_path = module.task_paths(config, 51)["json_report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(_reusable_summary(config, 51)),
        encoding="utf-8",
    )

    old_run = module._run_task_process
    try:
        module._run_task_process = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("completed report must be reused")
        )
        result = module.run_one(config, 51)
    finally:
        module._run_task_process = old_run

    assert result["runner_status"] == "done"
    assert result["reused_existing_report"] is True
    assert result["attempts"] == 0
    assert result["elapsed_seconds"] == 0.0


def test_completed_report_reuse_requires_same_remote_python():
    module = _load_module()
    config = replace(module.resolve_config(_args()), runtime_tree_sha256="a" * 64)
    summary = _reusable_summary(config, 51)
    assert module.report_is_reusable(summary, config, 51) is True
    other = replace(config, remote_python="/another/runtime/bin/python")
    assert module.report_is_reusable(summary, other, 51) is False


def test_scheduler_decreases_only_on_direct_shared_failure_and_recovers():
    module = _load_module()
    config = module.resolve_config(_args())
    state = module.SchedulerState(current_workers=5)

    module.update_scheduler_state(
        config,
        state,
        {
            "index": 51,
            "runner_status": "missing_report",
            "completed": False,
            "returncode": 2,
            "failure_scope": "shared_infrastructure",
            "failure_probe": {"direct": True, "status": "failed"},
        },
    )

    assert state.current_workers == 4
    assert state.clean_streak == 0
    assert state.events[-1]["action"] == "decrease"

    clean = {"index": 52, "runner_status": "done", "completed": True, "returncode": 0}
    module.update_scheduler_state(config, state, clean)
    assert state.current_workers == 4
    assert state.clean_streak == 1
    module.update_scheduler_state(config, state, {**clean, "index": 53})

    assert state.current_workers == 5
    assert state.clean_streak == 0
    assert state.events[-1]["action"] == "increase"


def test_scheduler_ignores_semantic_eval_failures():
    module = _load_module()
    config = module.resolve_config(_args())
    state = module.SchedulerState(current_workers=5)

    result = {
        "index": 54,
        "runner_status": "done",
        "completed": True,
        "returncode": 1,
        "technical_failed": 0,
        "rows": [
            {
                "generation": {"status": "generation_done"},
                "eval": {
                    "status": "eval_done",
                    "summary": {
                        "resolved": False,
                        "technical_reasons": [],
                        "command_log": "/srv/opencollab/eval_work/task/command.log",
                        "tests_status": {
                            "f2p_log_tail": "assertion failed: ssh timeout banner should stay visible",
                            "p2p_log_tail": "expected docker label text in rendered output",
                        },
                    },
                },
            }
        ],
    }

    assert module.result_resource_reasons(result) == []
    module.update_scheduler_state(config, state, result)
    assert state.current_workers == 5
    assert state.clean_streak == 1


def test_remote_health_check_builds_parameterized_ssh_probe(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            remote_python="/remote/runtime with space/bin/python",
        )
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return module.subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_remote_health_checks(config)
    finally:
        module.subprocess.run = old_run

    assert result["status"] == "ok"
    assert calls
    joined = " ".join(calls[0])
    assert "swe_g11_prolite51_75_test" in joined
    assert "docker info" in joined
    assert "test -d" in joined
    assert "/remote/runtime with space/bin/python" in joined


def test_remote_health_check_skips_without_ssh_for_dry_run_or_explicit_skip(tmp_path):
    module = _load_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise AssertionError("ssh should not be called")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        dry_config = module.resolve_config(_args(output_dir=tmp_path / "dry", dry_run=True))
        dry_config.output_dir.mkdir(parents=True)
        dry_result = module.run_remote_health_checks(dry_config)
        skip_config = module.resolve_config(_args(output_dir=tmp_path / "skip", skip_health_checks=True))
        skip_config.output_dir.mkdir(parents=True)
        skip_result = module.run_remote_health_checks(skip_config)
    finally:
        module.subprocess.run = old_run

    assert calls == []
    assert dry_result == {"status": "skipped", "reason": "dry_run"}
    assert skip_result == {"status": "skipped", "reason": "disabled"}
