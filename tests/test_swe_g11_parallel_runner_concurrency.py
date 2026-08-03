from __future__ import annotations

import threading
import time

import pytest
from test_swe_g11_parallel_runner import _args, _load_module


def test_run_parallel_stops_before_generation_when_health_check_fails(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            start_index=51,
            end_index=52,
            output_dir=tmp_path,
            no_ensure_remote_proxy=True,
        )
    )
    called = {"run_one": 0}

    def fake_run_one(*args, **kwargs):
        called["run_one"] += 1
        return {}

    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: (_ for _ in ()).throw(
            RuntimeError("remote health check failed")
        )
        module.run_one = fake_run_one
        with pytest.raises(RuntimeError, match="remote health check failed"):
            module.run_parallel(config)
    finally:
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one

    assert called["run_one"] == 0


def test_run_parallel_submits_only_the_current_worker_window(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            start_index=51,
            end_index=55,
            max_workers=3,
            output_dir=tmp_path,
            no_sync_runtime=True,
            expected_runtime_tree_sha256="a" * 64,
            no_ensure_remote_proxy=True,
            skip_preflight=True,
            skip_health_checks=True,
            retry_delay_seconds=0,
        )
    )
    started: list[int] = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_window_started = threading.Event()
    release = threading.Event()

    def fake_run_one(cfg, index):
        nonlocal active, max_active
        with lock:
            started.append(index)
            active += 1
            max_active = max(max_active, active)
            if len(started) == 3:
                first_window_started.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return {
            "index": index,
            "returncode": 0,
            "runner_status": "done",
                "tasks": 1,
                "generation_done": 1,
                "empty_patch": 0,
                "eval_done": 1,
                "eval_attempts": 1,
                "eval_retry_tasks": 0,
                "resolved": 1,
                "unresolved": 0,
                "technical_failed": 0,
                "completed": True,
                "attempts": 1,
            }

    final: dict[str, object] = {}
    errors: list[BaseException] = []
    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    old_token = module.build_token_summary
    old_fact = module.build_eval_fact_report
    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: {"status": "skipped"}
        module.run_one = fake_run_one
        module.build_token_summary = lambda cfg: {"status": "done"}
        module.build_eval_fact_report = lambda cfg: {
            "status": "done",
            "counts": {
                "tasks": 5,
                "eval_attempts": 5,
                "eval_retry_tasks": 0,
                "eval_success": 5,
                "empty_patch": 0,
                "resolved": 5,
                "unresolved": 0,
                "technical_failed_final": 0,
            },
        }

        def target():
            try:
                final.update(module.run_parallel(config))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=target)
        thread.start()
        assert first_window_started.wait(timeout=2)
        time.sleep(0.05)
        with lock:
            assert started == [51, 52, 53]
            assert max_active == 3
        release.set()
        thread.join(timeout=5)
    finally:
        release.set()
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one
        module.build_token_summary = old_token
        module.build_eval_fact_report = old_fact

    assert not errors
    assert final["status"] == "done"
    assert sorted(started) == [51, 52, 53, 54, 55]


@pytest.mark.parametrize(
    ("indices", "expected_halted", "expected_not_started"),
    [("51", False, []), ("51,52", True, [52])],
)
def test_shared_probe_failure_only_halts_unsubmitted_tasks(
    tmp_path, indices, expected_halted, expected_not_started
):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices=indices,
            start_index=None,
            end_index=None,
            max_workers=1,
            output_dir=tmp_path,
            no_sync_runtime=True,
            expected_runtime_tree_sha256="a" * 64,
            no_ensure_remote_proxy=True,
            skip_preflight=True,
            skip_health_checks=True,
        )
    )
    result = {
        "index": 51,
        "returncode": 1,
        "runner_status": "done_with_technical_failures",
        "tasks": 1,
        "generation_done": 0,
        "empty_patch": 0,
        "eval_done": 0,
        "eval_attempts": 0,
        "eval_retry_tasks": 0,
        "resolved": 0,
        "unresolved": 0,
        "technical_failed": 1,
        "completed": True,
        "failure_scope": "shared_infrastructure",
        "failure_probe": {"direct": True, "status": "failed"},
    }
    fact_calls: list[tuple[int, ...]] = []
    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    old_confirm = module.confirm_shared_runtime_after_task_failure
    old_token = module.build_token_summary
    old_fact = module.build_eval_fact_report
    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: {"status": "skipped"}
        module.run_one = lambda cfg, index: dict(result)
        module.confirm_shared_runtime_after_task_failure = lambda cfg, item: item
        module.build_token_summary = lambda cfg: {"status": "done"}

        def build_fact(cfg):
            fact_calls.append(cfg.indices)
            return {
                "status": "done_with_technical_failures",
                "counts": {
                    "tasks": 1,
                    "eval_attempts": 0,
                    "eval_retry_tasks": 0,
                    "eval_success": 0,
                    "empty_patch": 0,
                    "resolved": 0,
                    "unresolved": 0,
                    "technical_failed_final": 1,
                },
            }

        module.build_eval_fact_report = build_fact
        final = module.run_parallel(config)
    finally:
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one
        module.confirm_shared_runtime_after_task_failure = old_confirm
        module.build_token_summary = old_token
        module.build_eval_fact_report = old_fact

    assert final["scheduler"]["halted"] is expected_halted
    assert final["scheduler"]["not_started"] == expected_not_started
    if expected_halted:
        assert fact_calls == []
        assert final["fact_report"]["status"] == "not_built_batch_halted"
    else:
        assert fact_calls == [(51,)]
        assert final["fact_report"]["status"] == "done_with_technical_failures"


def test_unquiesced_generation_prevents_the_next_serial_task_from_starting(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51,52",
            start_index=None,
            end_index=None,
            max_workers=1,
            output_dir=tmp_path,
            no_sync_runtime=True,
            expected_runtime_tree_sha256="a" * 64,
            no_ensure_remote_proxy=True,
            skip_preflight=True,
            skip_health_checks=True,
        )
    )
    started: list[int] = []
    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    old_confirm = module.confirm_shared_runtime_after_task_failure
    old_token = module.build_token_summary
    old_fact = module.build_eval_fact_report

    def unquiesced_result(_cfg, index):
        started.append(index)
        return {
            "index": index,
            "returncode": 1,
            "runner_status": "done_with_technical_failures",
            "tasks": 1,
            "generation_done": 0,
            "empty_patch": 0,
            "eval_done": 0,
            "eval_attempts": 0,
            "eval_retry_tasks": 0,
            "resolved": 0,
            "unresolved": 0,
            "technical_failed": 1,
            "completed": True,
            "failure_scope": "task",
            "failure_probe": {},
            "rows": [
                {
                    "task": f"task-{index}",
                    "generation": {
                        "status": "technical_failed",
                        "execution_quiesced": False,
                    },
                }
            ],
        }

    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: {"status": "skipped"}
        module.run_one = unquiesced_result
        module.confirm_shared_runtime_after_task_failure = lambda cfg, item: item
        module.build_token_summary = lambda cfg: {"status": "done"}
        module.build_eval_fact_report = lambda cfg: pytest.fail(
            "fact report must not run after an unquiesced generation"
        )
        final = module.run_parallel(config)
    finally:
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one
        module.confirm_shared_runtime_after_task_failure = old_confirm
        module.build_token_summary = old_token
        module.build_eval_fact_report = old_fact

    assert started == [51]
    assert final["scheduler"]["halted"] is True
    assert final["scheduler"]["not_started"] == [52]
    assert final["scheduler"]["halt_reasons"] == [
        "generation_execution_not_quiesced"
    ]
