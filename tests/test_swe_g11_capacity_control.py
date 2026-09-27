"""Capacity changes dispatch work while earlier tasks remain in flight."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from test_swe_g11_parallel_runner import _args, _load_module


@pytest.mark.parametrize("recovery", [False, True])
def test_capacity_expands_blocked_tasks_and_shrinks_without_cancelling(tmp_path, monkeypatch, recovery):
    module = _load_module()
    config = module.resolve_config(_args(
        indices="1,2,3,4", start_index=None, end_index=None,
        max_workers=3, min_workers=1, max_technical_recoveries=int(recovery),
        output_dir=tmp_path, skip_health_checks=True, no_sync_runtime=True,
        expected_runtime_tree_sha256="a" * 64,
        no_ensure_remote_proxy=True, skip_preflight=True,
    ))
    control = tmp_path / "capacity.json"
    control.write_text(json.dumps({"generation_workers": 1}))
    monkeypatch.setenv(module.CAPACITY_CONTROL_FILE_ENV, str(control))
    condition = threading.Condition()
    started = []
    releases = {index: threading.Event() for index in config.indices}
    errors = []
    shrunk = threading.Event()
    scheduler = module.SchedulerState(current_workers=1)
    refresh = module._refresh_capacity_control

    def observe_capacity(cfg, state):
        refresh(cfg, state)
        if state.events and state.events[-1].get("previous_workers") == 3 and state.current_workers == 1:
            shrunk.set()

    monkeypatch.setattr(module, "_refresh_capacity_control", observe_capacity)
    monkeypatch.setattr(module, "update_scheduler_state", lambda *_: None)
    monkeypatch.setattr(module, "save_progress", lambda *_, **__: None)
    monkeypatch.setattr(module, "prepare_runtime", lambda _: None)
    monkeypatch.setattr(module, "run_remote_health_checks", lambda _: {"status": "skipped"})
    monkeypatch.setattr(module, "wait_for_remote_model_probe", lambda _: {"status": "skipped"})
    monkeypatch.setattr(module, "build_token_summary", lambda _: {})
    monkeypatch.setattr(module, "build_eval_fact_report", lambda _: {})
    monkeypatch.setattr(module, "aggregate", lambda *_, **__: {"status": "done"})
    monkeypatch.setattr(module, "compact_progress", lambda result: result)

    def run_one(_config, index, *_args):
        with condition:
            started.append(index)
            condition.notify_all()
        assert releases[index].wait(timeout=20)
        return {"index": index, "completed": True, "technical_failed": 0, "runner_status": "done"}

    monkeypatch.setattr(module, "run_one", run_one)
    if recovery:
        queue = module._technical_queue
        monkeypatch.setattr(queue, "plan_recovery_attempt", lambda **options: SimpleNamespace(
            index=options["result"]["index"], ordinal=1, mode="generation", run_id="fixture",
            attempt_id=str(options["result"]["index"]), decision_path=tmp_path / "decision.json",
            json_report=tmp_path / "report.json",
        ))
        monkeypatch.setattr(queue, "decision_payload", lambda *_, **__: {})
        monkeypatch.setattr(queue, "write_decision_once", lambda *_: None)
        monkeypatch.setattr(queue, "select_recovery_result", lambda result, **_: result)
        results = [{"index": index, "technical_failed": 1} for index in config.indices]
        def operation():
            module._run_technical_recovery_tail(config, config, results, scheduler, {})
    else:
        def operation():
            module._run_parallel(config)

    def target():
        try:
            operation()
        except BaseException as error:
            errors.append(error)
        finally:
            with condition:
                condition.notify_all()

    thread = threading.Thread(target=target)
    thread.start()
    try:
        with condition:
            assert condition.wait_for(lambda: len(started) == 1 or errors, timeout=5)
            assert started == [1]
        control.write_text(json.dumps({"generation_workers": 3}))
        with condition:
            assert condition.wait_for(lambda: len(started) == 3 or errors, timeout=5)
            assert sorted(started) == [1, 2, 3]
        assert not any(event.is_set() for event in releases.values())
        control.write_text(json.dumps({"generation_workers": 1}))
        assert shrunk.wait(timeout=5)
        releases[1].set()
        releases[2].set()
        with condition:
            assert not condition.wait_for(lambda: len(started) > 3 or errors, timeout=1.2)
        assert not releases[3].is_set()
        releases[3].set()
        with condition:
            assert condition.wait_for(lambda: len(started) == 4 or errors, timeout=5)
        releases[4].set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
    finally:
        for event in releases.values():
            event.set()
        thread.join(timeout=5)


def test_capacity_wait_preserves_completion_wait_when_unconfigured(monkeypatch):
    from opencollab_eval.commands import swe_g11_capacity_control as capacity

    monkeypatch.delenv(capacity.CAPACITY_CONTROL_FILE_ENV, raising=False)
    assert capacity.wait_timeout() is None
    monkeypatch.setenv(capacity.CAPACITY_CONTROL_FILE_ENV, "capacity.json")
    assert capacity.wait_timeout() == 1.0
