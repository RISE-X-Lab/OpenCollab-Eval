from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from generation_proof_test_support import trusted_patch_proof_fields
from test_swe_g11_parallel_runner import (
    _args,
    _load_module,
    _reusable_summary,
)

from opencollab_eval.engine import swe_v1_remote_records as remote_records


def test_parallel_no_sync_runtime_requires_shared_preflight_identity():
    module = _load_module()

    with pytest.raises(ValueError, match="requires --expected-runtime-tree-sha256"):
        module.resolve_config(_args(no_sync_runtime=True))


def test_preflight_forwards_no_sync_runtime_identity(tmp_path):
    module = _load_module()
    expected = "a" * 64
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            no_sync_runtime=True,
            expected_runtime_tree_sha256=expected,
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        report = {
            "status": "dry_run",
            "runtime_tree_sha256": expected,
            "workflow": config.workflow,
            "workflow_env": {},
            "budget": config.budget,
            "max_steps": config.max_steps,
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
        assert module.prepare_runtime(config) == expected
    finally:
        module._run_task_process = original

    command = captured["command"]
    assert "--no-sync-runtime" in command
    assert command[command.index("--expected-runtime-tree-sha256") + 1] == expected


def test_preflight_rejects_child_limit_drift(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        report = {
            "status": "dry_run",
            "runtime_tree_sha256": "a" * 64,
            "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
            "max_empty_patch_retries": config.max_empty_patch_retries,
            "max_task_starts": config.max_task_starts,
            "max_eval_attempts": config.max_eval_attempts + 1,
        }
        (tmp_path / "shared_runtime_preflight.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return module.subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    original = module._run_task_process
    try:
        module._run_task_process = fake_run
        with pytest.raises(RuntimeError, match="max_eval_attempts"):
            module.prepare_runtime(config)
    finally:
        module._run_task_process = original
    command = captured["command"]
    expected = {
        "--openhands-empty-patch-rejections": config.openhands_empty_patch_rejections,
        "--max-empty-patch-retries": config.max_empty_patch_retries,
        "--max-task-starts": config.max_task_starts,
        "--max-eval-attempts": config.max_eval_attempts,
    }
    assert all(command[command.index(option) + 1] == str(value) for option, value in expected.items())


@pytest.mark.parametrize("attempts", [None, True, "1", -1, 2])
def test_aggregate_rejects_invalid_runner_attempt_evidence(attempts):
    module = _load_module()
    config = module.resolve_config(
        _args(indices="51", start_index=None, end_index=None, runner_attempts=1)
    )
    result = {
        "index": 51,
        "completed": True,
        "returncode": 0,
        "tasks": 1,
        "generation_done": 1,
        "eval_done": 1,
        "attempts": attempts,
    }

    summary = module.aggregate(config, [result])

    reason = "runner_attempt_budget_exceeded" if attempts == 2 else "runner_attempt_evidence_invalid"
    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["technical_failed"] == 1
    assert summary["results"][0]["summary_validation_reasons"] == [reason]


def test_aggregate_allows_zero_attempts_for_reused_report():
    module = _load_module()
    config = module.resolve_config(_args(output_dir="/tmp/reused", indices="51"))
    result = {
        "index": 51,
        "completed": True,
        "returncode": 0,
        "tasks": 1,
        "generation_done": 1,
        "eval_done": 1,
        "attempts": 0,
        "reused_existing_report": True,
    }

    summary = module.aggregate(config, [result])

    assert summary["status"] == "done"
    assert summary["counts"]["technical_failed"] == 0


def test_aggregate_rejects_nonzero_attempts_for_reused_report():
    module = _load_module()
    config = module.resolve_config(_args(output_dir="/tmp/reused", indices="51"))
    result = {
        "index": 51,
        "completed": True,
        "returncode": 0,
        "tasks": 1,
        "generation_done": 1,
        "eval_done": 1,
        "attempts": 1,
        "reused_existing_report": True,
    }

    summary = module.aggregate(config, [result])

    assert summary["status"] == "done_with_technical_failures"
    assert summary["results"][0]["summary_validation_reasons"] == [
        "runner_attempt_evidence_invalid"
    ]


def _task_summary(config, status: str = "done") -> dict:
    summary = _reusable_summary(config, 51)
    summary["status"] = status
    return summary


def _fact_report(indices: tuple[int, ...], *, technical: tuple[int, ...] = ()) -> dict:
    tasks = [
        {
            "index": index,
            "task": f"task-{index}",
            "generation_status": "generation_failed" if index in technical else "generation_done",
            "eval_attempt_count": 0 if index in technical else 1,
            "eval_success": index not in technical,
            "eval_pending": False,
            "resolved": None if index in technical else True,
            "technical_failed": index in technical,
        }
        for index in indices
    ]
    successful = len(indices) - len(technical)
    return {
        "schema": "opencollab.swe_eval_layer_final_report.v1",
        "expected_indices": list(indices),
        "counts": {
            "tasks": len(indices),
            "eval_attempts": successful,
            "eval_retry_tasks": 0,
            "eval_success": successful,
            "empty_patch": 0,
            "eval_pending": 0,
            "resolved": successful,
            "unresolved": 0,
            "technical_failed_final": len(technical),
        },
        "tasks": tasks,
    }


def test_task_result_accepts_only_a_strict_terminal_summary(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    valid = module.task_result_from_summary(
        config,
        51,
        _task_summary(config),
        reused=False,
        elapsed=1.0,
        process_returncode=0,
    )
    assert valid["completed"] is True
    assert valid["summary_validation_reasons"] == []

    cases = {
        "running": _task_summary(config, "running"),
        "empty_counts": {**_task_summary(config), "counts": {}},
        "wrong_schema": {**_task_summary(config), "schema": "legacy"},
    }
    for name, summary in cases.items():
        result = module.task_result_from_summary(
            config,
            51,
            summary,
            reused=False,
            elapsed=1.0,
            process_returncode=0,
        )
        assert result["completed"] is False, name
        assert result["summary_validation_reasons"], name


def test_task_result_rejects_returncode_status_conflict(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    result = module.task_result_from_summary(
        config,
        51,
        _task_summary(config),
        reused=False,
        elapsed=1.0,
        process_returncode=1,
    )

    assert result["completed"] is False
    assert "returncode_status_conflict" in result["summary_validation_reasons"]


def test_strict_technical_terminal_is_completed_without_contributing_a_verdict(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    summary = _reusable_summary(config, 51)
    summary["status"] = "done_with_technical_failures"
    summary["counts"].update(eval_done=0, resolved=0, technical_failed=1)
    evaluation = summary["rows"][0]["eval"]
    evaluation["status"] = "technical_eval_failed"
    evaluation["summary"].update(
        status="technical_eval_failed",
        resolved=False,
        technical_reasons=["fail_to_pass_infra"],
    )

    result = module.task_result_from_summary(
        config,
        51,
        summary,
        reused=False,
        elapsed=1.0,
        process_returncode=1,
    )

    assert result["completed"] is True
    assert result["resolved"] == 0
    assert result["unresolved"] == 0
    assert result["technical_failed"] == 1
    assert result["summary_validation_reasons"] == []


def test_build_eval_fact_report_passes_the_trusted_config_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            indices="3,7,9",
            start_index=None,
            end_index=None,
        )
    )
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((3, 7, 9)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "done"
    expected_values = [
        captured[index + 1]
        for index, value in enumerate(captured)
        if value == "--expected-index"
    ]
    assert expected_values == ["3", "7", "9"]


def test_build_eval_fact_report_rejects_a_wrong_output_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="3,7", start_index=None, end_index=None)
    )

    def fake_run(command, **kwargs):
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((3,)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "invalid_fact_report"


def test_build_eval_fact_report_does_not_call_a_technical_row_done(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))

    def fake_run(command, **kwargs):
        output = command[command.index("--json-output") + 1]
        module.write_json(output, _fact_report((51,), technical=(51,)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.build_eval_fact_report(config)
    finally:
        module.subprocess.run = original

    assert result["status"] == "done_with_technical_failures"
    assert result["counts"]["resolved"] == 0
    assert result["counts"]["technical_failed_final"] == 1


def test_incomplete_task_does_not_build_a_terminal_fact_report(tmp_path, monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="8"))
    stale_names = (
        "final_eval_layer_report.json",
        "final_eval_layer_report.md",
        "final_eval_layer_report.stdout.log",
        "final_eval_layer_report.stderr.log",
    )
    for name in stale_names:
        (tmp_path / name).write_text("stale terminal artifact", encoding="utf-8")

    def fail_build(_config):
        raise AssertionError("incomplete tasks must not build a fact report")

    incomplete = {
        "index": 8,
        "returncode": 1,
        "runner_status": "missing_report",
        "completed": False,
        "failure_scope": "task",
        "failure_probe": {},
    }
    monkeypatch.setattr(module, "prepare_runtime", lambda config: None)
    monkeypatch.setattr(module, "run_remote_health_checks", lambda config: {})
    monkeypatch.setattr(module, "run_remote_model_probe", lambda config: {})
    monkeypatch.setattr(module, "run_one", lambda config, index: incomplete)
    monkeypatch.setattr(module, "confirm_shared_runtime_after_task_failure", lambda config, result: result)
    monkeypatch.setattr(module, "systemic_failure_reasons", lambda result: [])
    monkeypatch.setattr(module, "build_token_summary", lambda config: {})
    monkeypatch.setattr(module, "save_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "build_eval_fact_report", fail_build)

    summary = module.run_parallel(config)

    assert summary["status"] == "running"
    assert summary["fact_report"]["status"] == "not_built_incomplete_tasks"
    assert summary["fact_report"]["incomplete_indices"] == [8]
    assert all(not (tmp_path / name).exists() for name in stale_names)


def test_preflight_runtime_identity_reaches_task_reuse_checks(tmp_path, monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="8"))
    received = []
    incomplete = {
        "index": 8,
        "returncode": 1,
        "runner_status": "missing_report",
        "completed": False,
        "failure_scope": "task",
        "failure_probe": {},
    }

    def capture(per_task_config, _index):
        received.append(per_task_config)
        return incomplete

    monkeypatch.setattr(module, "prepare_runtime", lambda _config: "a" * 64)
    monkeypatch.setattr(module, "run_remote_health_checks", lambda _config: {})
    monkeypatch.setattr(module, "run_remote_model_probe", lambda _config: {})
    monkeypatch.setattr(module, "run_one", capture)
    monkeypatch.setattr(
        module,
        "confirm_shared_runtime_after_task_failure",
        lambda _config, result: result,
    )
    monkeypatch.setattr(module, "systemic_failure_reasons", lambda _result: [])
    monkeypatch.setattr(module, "build_token_summary", lambda _config: {})
    monkeypatch.setattr(module, "save_progress", lambda *args, **kwargs: None)

    module.run_parallel(config)

    assert len(received) == 1
    assert received[0].runtime_tree_sha256 == "a" * 64
    assert received[0].no_sync_runtime is True
    assert received[0].no_ensure_remote_proxy is True


def test_aggregate_cannot_finish_when_the_fact_report_census_is_invalid(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="1"))
    results = [
        {
            "index": 1,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
            "attempts": 1,
        }
    ]

    summary = module.aggregate(
        config,
        results,
        fact_report={"status": "invalid_census"},
    )

    assert summary["status"] == "done_with_report_validation_failure"
    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["technical_failed"] == 0
    assert summary["report_validation_failed"] is True


def test_fresh_success_without_identity_or_direct_proof_cannot_reach_top_level_done(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    weak = _reusable_summary(config, 51)
    weak["rows"][0]["generation"].pop("record_id")
    weak["rows"][0]["eval"]["summary"].pop("tests_status")

    result = module.task_result_from_summary(
        config,
        51,
        weak,
        reused=False,
        elapsed=1.0,
        process_returncode=0,
    )
    summary = module.aggregate(config, [result])

    assert result["completed"] is False
    assert result["resolved"] == 0
    assert "missing_generation_record_id" in result["summary_validation_reasons"]
    assert "missing_direct_execution_proof" in result["summary_validation_reasons"]
    assert summary["status"] == "running"
    assert summary["counts"]["resolved"] == 0


def test_fact_report_with_a_technical_row_cannot_claim_done(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    report = _fact_report((51,), technical=(51,))

    status, counts, reasons = module._reports._validate_fact_report(report, config)

    assert status == "done_with_technical_failures"
    assert counts["technical_failed_final"] == 1
    assert reasons == []


def test_parallel_and_fact_count_conflict_becomes_technical_and_preserves_unresolved(
    tmp_path,
):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    results = [
        {
            "index": 51,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "empty_patch": 0,
            "eval_done": 1,
            "eval_attempts": 1,
            "eval_retry_tasks": 0,
            "resolved": 1,
            "unresolved": 0,
            "technical_failed": 0,
            "attempts": 1,
        }
    ]
    fact_report = {
        "status": "done",
        "counts": {
            **_fact_report((51,))["counts"],
            "resolved": 0,
            "unresolved": 1,
        },
    }

    summary = module.aggregate(config, results, fact_report=fact_report)

    assert summary["status"] == "done_with_report_validation_failure"
    assert summary["counts"]["resolved"] == 1
    assert summary["counts"]["unresolved"] == 0
    assert summary["counts"]["technical_failed"] == 0
    assert summary["report_validation_failed"] is True
    assert "parallel_fact_count_mismatch:resolved" in summary[
        "fact_report_validation_reasons"
    ]


def test_invalid_fact_report_preserves_verified_parallel_unresolved(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path, indices="51"))
    results = [
        {
            "index": 51,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "empty_patch": 0,
            "eval_done": 1,
            "eval_attempts": 1,
            "eval_retry_tasks": 0,
            "resolved": 0,
            "unresolved": 1,
            "technical_failed": 0,
            "attempts": 1,
        }
    ]

    summary = module.aggregate(
        config,
        results,
        fact_report={"status": "invalid_census"},
    )

    assert summary["status"] == "done_with_report_validation_failure"
    assert summary["counts"]["resolved"] == 0
    assert summary["counts"]["unresolved"] == 1
    assert summary["counts"]["technical_failed"] == 0
    assert summary["report_validation_failed"] is True


def test_shared_outage_does_not_erase_an_earlier_verified_resolution(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="11,16", start_index=None, end_index=None)
    )
    results = [
        {
            "index": 11,
            "completed": True,
            "returncode": 0,
            "tasks": 1,
            "generation_done": 1,
            "empty_patch": 0,
            "eval_done": 1,
            "eval_attempts": 1,
            "eval_retry_tasks": 0,
            "resolved": 1,
            "unresolved": 0,
            "technical_failed": 0,
            "attempts": 1,
        }
    ]
    scheduler = {
        "halted": True,
        "halt_index": 15,
        "not_started": [16],
    }

    summary = module.aggregate(
        config,
        results,
        fact_report={"status": "invalid_census"},
        scheduler=scheduler,
    )

    assert summary["status"] == "halted_on_technical_failure"
    assert summary["counts"]["resolved"] == 1
    assert summary["counts"]["technical_failed"] == 0
    assert summary["report_validation_failed"] is True
    assert summary["scheduler"]["not_started"] == [16]


def test_aggregate_rejects_duplicate_indices_as_a_complete_census(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="1,2", start_index=None, end_index=None)
    )
    results = [
        {"index": 1, "completed": True, "returncode": 0, "tasks": 1},
        {"index": 1, "completed": True, "returncode": 0, "tasks": 1},
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "running"


def test_report_reuse_accepts_only_a_production_proven_empty_patch(tmp_path):
    module = _load_module()
    config = replace(
        module.resolve_config(_args(output_dir=tmp_path, indices="51")),
        runtime_tree_sha256="a" * 64,
    )
    task = "task-51"
    record_id = "record-task-51"
    metric = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
        "workflow_status": "empty_patch_after_done",
        "runner_returncode": 1,
        "submission_eligible": False,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": False,
        "llm_model": "deepseek-v4-pro",
        "trajectory_models": ["deepseek-v4-pro"],
        "provider_models": ["deepseek-v4-pro"],
        "trajectory_sha256": "b" * 64,
        "trajectory_llm_call_count": 3,
        "wire_protocol": "chat_completions",
        **trusted_patch_proof_fields(""),
    }
    prediction = {
        "instance_id": task,
        "record_id": record_id,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
        "model_patch": "",
        "workflow_metric": metric,
    }
    generation = remote_records.empty_patch_result(
        task, prediction, metric, "record_id"
    )
    summary = _reusable_summary(config, 51)
    summary["counts"].update(
        generation_done=0,
        empty_patch=1,
        eval_done=0,
        eval_attempts=0,
        resolved=0,
    )
    summary["rows"] = [
        {
            "index": 51,
            "task": task,
            "generation": generation,
            "eval": {"status": "skipped_empty_patch", "task": task},
        }
    ]

    assert module.report_is_reusable(summary, config, 51) is True

    missing_evidence = json.loads(json.dumps(summary))
    missing_evidence["rows"][0]["generation"].pop("execution_quiesced")
    assert module.report_is_reusable(missing_evidence, config, 51) is False

    missing_call = json.loads(json.dumps(summary))
    missing_call["rows"][0]["generation"]["trajectory_llm_call_count"] = 0
    assert module.report_is_reusable(missing_call, config, 51) is False

    zero_call_metric = {
        **metric,
        "trajectory_models": [],
        "trajectory_sha256": None,
        "trajectory_llm_call_count": 0,
    }
    zero_call = remote_records.empty_patch_result(
        task, prediction, zero_call_metric, "record_id"
    )
    assert zero_call["status"] == "generation_failed"
    assert zero_call["submission_integrity"] == "empty_patch_unproven"

    forged = {**summary, "rows": [dict(summary["rows"][0])]}
    forged["rows"][0]["generation"] = {
        "status": "empty_patch",
        "patch_len": 0,
        "workflow_status": "empty_patch_after_done",
    }
    assert module.report_is_reusable(forged, config, 51) is False


def test_parser_has_no_private_host_proxy_or_env_file_defaults(monkeypatch):
    for name in (
        "OPENCOLLAB_SWE_HOST",
        "OPENCOLLAB_SWE_LLM_MODEL",
        "OPENCOLLAB_REMOTE_PROXY_BASE_URL",
        "OPENCOLLAB_LOCAL_PROXY_BASE_URL",
        "OPENCOLLAB_PROXY_ENV_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    module = _load_module()

    args = module.build_parser().parse_args(["--indices", "1"])

    assert args.host == ""
    assert args.llm_model == ""
    assert args.remote_proxy_base_url == ""
    assert args.local_proxy_base_url == ""
    assert args.proxy_env_file is None


def test_run_one_does_not_rewrite_a_weak_legacy_empty_patch_report(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(output_dir=tmp_path, indices="51", runner_attempts=1)
    )
    report_path = module.task_paths(config, 51)["json_report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "status": "done_with_technical_failures",
        "counts": {"tasks": 1, "technical_failed": 1},
        "rows": [
            {
                "index": 51,
                "task": "task-51",
                "generation": {
                    "status": "generation_failed",
                    "workflow_status": "empty_patch_after_done",
                    "patch_len": 0,
                },
                "eval": {"status": "skipped_no_generation_patch"},
            }
        ],
    }
    original_text = json.dumps(legacy)
    report_path.write_text(original_text, encoding="utf-8")
    calls = 0
    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        assert report_path.read_text(encoding="utf-8") == original_text
        module.write_json(report_path, _reusable_summary(config, 51))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original_run = module._run_task_process
    try:
        module._run_task_process = fake_run
        result = module.run_one(config, 51)
    finally:
        module._run_task_process = original_run
    assert calls == 1
    assert result["completed"] is True
    assert result["reused_existing_report"] is False
