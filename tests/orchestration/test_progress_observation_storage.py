"""Storage faults in progress metadata leave native execution and timeouts intact."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab import OpenCollab
from opencollab.environments import local_environment

from opencollab_eval.engine import completed_progress_watch as progress
from opencollab_eval.engine import native_progress_watch as native

OBSERVATIONS = {
    "native-progress-owner.json", "native-progress-status.json", "provider-wait-status.json",
    "provider-wait-stop.json", "no-progress-stop.json", "no-progress-decision.json",
}
STORAGE_ERRORS = [errno.ENOSPC, errno.EIO, errno.EACCES]


def settings(root, source, seconds=0.08):
    return {
        **native.config_for(root, seconds=seconds),
        "native_workflow_sources": [str(source)],
        "progress_poll_seconds": 0.005,
    }


def append(path, row):
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def fail_observations(monkeypatch, code, names=OBSERVATIONS):
    actual = Path.open

    def injected(path, mode="r", *args, **kwargs):
        writing = any(flag in mode for flag in "wa+")
        matches = any(path.name == name or path.name.startswith(name + ".") for name in names)
        if writing and matches:
            raise OSError(code, os.strerror(code), str(path))
        return actual(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", injected)


class ReplyModel:
    supports_response_session_identity = True

    def __init__(self):
        self.calls = 0

    def context_window(self):
        return 32768

    async def complete(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.025)
        return SimpleNamespace(content="finished", tool_calls=[], finish_reason="stop", reasoning=None,
                               provider_items=[], provider_state=None, transport_timing=None,
                               usage=SimpleNamespace(input_tokens=4, output_tokens=2, total_tokens=6,
                                                     cache_read_tokens=0, cache_creation_tokens=0,
                                                     estimated=False, raw_usage={}))

    async def close(self):
        pass


@pytest.mark.parametrize("code", STORAGE_ERRORS)
@pytest.mark.parametrize("name", ["native-progress-status.json", "provider-wait-status.json"])
def test_snapshot_failure_retains_real_progress_and_finishes(tmp_path, monkeypatch, caplog, code, name):
    source = tmp_path / "trajectory.jsonl"
    fail_observations(monkeypatch, code, {name})
    decision = {}

    async def operation():
        for index in range(20):
            append(source, {"type": "tool_exec", "timestamp": time.time(),
                            "payload": {"tool_call_id": str(index), "result": "done"}})
            await asyncio.sleep(0.01)
        return "finished"

    started = time.time()
    result = asyncio.run(progress.run_with_stop_request(operation(), settings(tmp_path, source), started, decision))
    assert result == "finished"
    assert time.time() - started > 0.08
    assert len(list(progress.records(source))) == 20
    fault = decision["progress_observation_errors"][str(tmp_path / name)]
    assert fault["errno"] == code
    assert fault["count"] > 1
    assert "reason" not in decision
    assert sum("Progress observation storage failed" in item.message for item in caplog.records) == 1
    metrics = progress.apply_decision({}, decision)
    assert metrics["progress_observation_errors"]
    assert "no_progress_timeout" not in metrics


@pytest.mark.parametrize("code", STORAGE_ERRORS)
def test_inactivity_cancels_and_finalizes_when_all_observation_writes_fail(tmp_path, monkeypatch, code):
    fail_observations(monkeypatch, code)

    async def scenario():
        finalized = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        decision = {}
        with pytest.raises(progress.EvaluationNoProgressTimeout):
            await asyncio.wait_for(progress.run_with_stop_request(
                operation(), settings(tmp_path, tmp_path / "trajectory.jsonl", 0.02), time.time(), decision,
            ), timeout=1)
        assert finalized.is_set()
        assert decision["reason"] == "evaluation_no_progress"
        assert decision["progress_observation_errors"][str(tmp_path / "no-progress-decision.json")]["errno"] == code

    asyncio.run(scenario())


def test_role_timeout_survives_snapshot_and_stop_receipt_failure(tmp_path, monkeypatch):
    fail_observations(monkeypatch, errno.ENOSPC)
    source = tmp_path / "trajectory.jsonl"

    async def scenario():
        started = time.time()
        append(source, {"type": "llm_call_started", "timestamp": started,
                        "payload": {"aid": 1, "session_step": 0, "response_session_id": "waiting",
                                    "role": "coder"}})
        finalized = asyncio.Event()

        async def operation():
            try:
                while True:
                    append(source, {"type": "tool_exec", "timestamp": time.time(),
                                    "payload": {"tool_call_id": "other-role", "result": "done"}})
                    await asyncio.sleep(0.005)
            finally:
                finalized.set()

        config = {**settings(tmp_path, source), "stop_stalled_role": True,
                  "provider_call_no_progress_timeout_seconds": 0.02}
        decision = {}
        with pytest.raises(progress.EvaluationNoProgressTimeout):
            await asyncio.wait_for(progress.run_with_stop_request(operation(), config, started, decision), timeout=1)
        assert finalized.is_set()
        assert decision["reason"] == "provider_call_no_progress"
        assert decision["provider_wait"]["response_session_id"] == "waiting"
        assert str(tmp_path / "provider-wait-stop.json") in decision["progress_observation_errors"]

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [900, 14400])
@pytest.mark.parametrize("role_timeout", [False, True])
def test_configured_watchdog_limit_survives_observation_failure(tmp_path, monkeypatch, seconds, role_timeout):
    fail_observations(monkeypatch, errno.ENOSPC)
    now = [0]
    monkeypatch.setattr(progress.time, "time", lambda: now[0])
    source = tmp_path / "trajectory.jsonl"
    if role_timeout:
        append(source, {"type": "llm_call_started", "timestamp": 0,
                        "payload": {"aid": 1, "session_step": 0, "response_session_id": "waiting"}})

    async def scenario():
        finalized = asyncio.Event()

        async def operation():
            try:
                while True:
                    if role_timeout:
                        append(source, {"type": "tool_exec", "timestamp": now[0],
                                        "payload": {"tool_call_id": "other-role", "result": "done"}})
                    await asyncio.sleep(0.001)
            finally:
                finalized.set()

        config = {**settings(tmp_path, source, seconds), "stop_stalled_role": role_timeout}
        decision = {}
        supervised = asyncio.create_task(progress.run_with_stop_request(operation(), config, 0, decision))
        now[0] = seconds - 1
        await asyncio.sleep(0.02)
        assert not supervised.done()
        now[0] = seconds
        with pytest.raises(progress.EvaluationNoProgressTimeout):
            await asyncio.wait_for(supervised, timeout=1)
        assert finalized.is_set()
        assert decision["reason"] == ("provider_call_no_progress" if role_timeout else "evaluation_no_progress")

    asyncio.run(scenario())


def test_external_cancellation_survives_observation_failure(tmp_path, monkeypatch):
    fail_observations(monkeypatch, errno.ENOSPC)

    async def scenario():
        finalized = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        decision = {}
        supervised = asyncio.create_task(progress.run_with_stop_request(
            operation(), settings(tmp_path, tmp_path / "trajectory.jsonl", 10), time.time(), decision,
        ))
        while not decision.get("progress_observation_errors"):
            await asyncio.sleep(0.005)
        supervised.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervised
        assert finalized.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", STORAGE_ERRORS)
@pytest.mark.parametrize("name", ["trajectory.jsonl", "agent.json", "candidate.json"])
def test_required_writer_error_and_cause_propagate(tmp_path, monkeypatch, code, name):
    fail_observations(monkeypatch, errno.ENOSPC)
    error = OSError(code, os.strerror(code), str(tmp_path / name))
    cause = RuntimeError("required artifact persistence failed")
    actual = Path.open

    def injected(path, mode="r", *args, **kwargs):
        if path.name == name and any(flag in mode for flag in "wa+"):
            raise error from cause
        return actual(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", injected)

    async def operation():
        await asyncio.sleep(0.02)
        (tmp_path / "retained-workspace.txt").write_text("retained work")
        (tmp_path / name).write_text("required artifact")

    with pytest.raises(OSError) as caught:
        asyncio.run(progress.run_with_stop_request(
            operation(), settings(tmp_path, tmp_path / "trajectory.jsonl"), time.time(), {},
        ))
    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert (tmp_path / "retained-workspace.txt").read_text() == "retained work"


def test_unrelated_observer_failure_still_propagates_and_finalizes(tmp_path, monkeypatch):
    error = ValueError("invalid progress document")
    monkeypatch.setattr(progress, "save", lambda *args: (_ for _ in ()).throw(error))

    async def scenario():
        finalized = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        with pytest.raises(ValueError) as caught:
            await progress.run_with_stop_request(
                operation(), settings(tmp_path, tmp_path / "trajectory.jsonl"), time.time(), {},
            )
        assert caught.value is error
        assert finalized.is_set()

    asyncio.run(scenario())


def test_real_sdk_workflow_result_survives_owner_and_stage_maintenance_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(native.ENV_NAME, "1")
    fail_observations(monkeypatch, errno.EIO)
    artifacts = tmp_path / "artifacts"

    async def flow(ctx, args):
        native.record_stage(tmp_path, "candidate_capture")
        await asyncio.sleep(0.02)
        candidate = tmp_path / "candidate.json"
        candidate.write_text(json.dumps({"status": "completed", "patch": "retained valid candidate"}))
        return {"candidate": str(candidate)}

    async def scenario():
        client = OpenCollab(tmp_path, model="unused", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        guarded = native.guarded_workflow(flow, tmp_path, config=settings(tmp_path, artifacts / "orchestration.jsonl"),
                                          orchestration_path=artifacts / "orchestration.jsonl")
        return await client.workflow(guarded, {}, artifacts=artifacts, timeout=None)

    result = asyncio.run(scenario())
    assert result.status == "completed"
    assert result.error is None
    assert json.loads(Path(result.output["candidate"]).read_text())["patch"] == "retained valid candidate"
    native.record_stage(tmp_path, "scoring")
    assert json.loads(Path(result.output["candidate"]).read_text())["status"] == "completed"
    assert (artifacts / "workflow.json").is_file()
    assert any("native-progress-owner.json" in item.message for item in caplog.records)


@pytest.mark.parametrize("code", STORAGE_ERRORS)
def test_real_sdk_waiting_agent_still_stops_and_saves_session(tmp_path, monkeypatch, code):
    monkeypatch.setenv(native.ENV_NAME, "0.03")
    fail_observations(monkeypatch, code)

    class WaitingModel:
        supports_response_session_identity = True

        def context_window(self):
            return 32768

        async def complete(self, *args, **kwargs):
            await asyncio.Event().wait()

        async def close(self):
            pass

    artifacts = tmp_path / "artifacts"

    async def scenario():
        client = OpenCollab(tmp_path, model="unused", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        return await asyncio.wait_for(native.guarded_agent(
            client.agent2("retain this task", llm=WaitingModel(), artifacts=artifacts, timeout=None),
            tmp_path, artifacts, config=settings(tmp_path, artifacts / "trajectory.jsonl", 0.03),
        ), timeout=2)

    result = asyncio.run(scenario())
    assert result.reason == "evaluation_no_progress"
    assert result.metrics["session_quiesced"] is True
    snapshot = OpenCollab.read_session_snapshot(artifacts / "agent.json")
    assert any("retain this task" in str(row.get("content")) for row in snapshot["messages"])
    assert result.metrics["no_progress_timeout"]["progress_observation_errors"]


def test_outer_monitor_retains_stop_grace_when_request_cannot_be_saved(tmp_path, monkeypatch):
    fail_observations(monkeypatch, errno.ENOSPC)
    now = [10]
    monkeypatch.setattr(progress.time, "time", lambda: now[0])
    monkeypatch.setattr(progress.time, "sleep", lambda interval: now.__setitem__(0, now[0] + interval))
    monkeypatch.setattr(progress, "same_process", lambda identity: True)
    signals = []
    monkeypatch.setattr(progress.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    config = {**settings(tmp_path, tmp_path / "trajectory.jsonl", 1), "no_progress_cleanup_grace_seconds": 2,
              "progress_poll_seconds": 0.5}
    result = progress.monitor(config, {"started_epoch": 0, "generation_identity": {"pid": 123}},
                              tmp_path / "native-progress-status.json")
    assert now[0] == 12
    assert result["phase"] == "paused_for_artifact_recovery"
    assert signals == [(123, signal.SIGSTOP)]
    assert result["artifacts_preserved"] is True


def test_outer_controller_waits_for_healthy_child_despite_snapshot_failure(tmp_path, monkeypatch):
    source = tmp_path / "trajectory.jsonl"
    script = (
        "import json,sys,time\n"
        "for index in range(20):\n"
        " with open(sys.argv[1],'a') as stream:\n"
        "  stream.write(json.dumps({'type':'tool_exec','timestamp':time.time(),"
        "'payload':{'tool_call_id':str(index),'result':'done'}})+'\\n')\n"
        " time.sleep(.01)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, str(source)])
    identity = progress.process_identity(proc.pid)
    progress.save(tmp_path / "native-progress-owner.json", {"generation_identity": identity,
                  "started_epoch": time.time(), "native_progress_sources": [str(source)]})
    fail_observations(monkeypatch, errno.ENOSPC, {"native-progress-status.json"})
    try:
        assert native.wait_generation(proc, tmp_path, wall_timeout=0.01, environment={native.ENV_NAME: "0.1"},
                                      config=settings(tmp_path, source, 0.1)) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
    assert len(list(progress.records(source))) == 20


def test_stage_update_read_failure_preserves_existing_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.json"
    candidate.write_text("valid result")
    error = OSError(errno.EIO, "snapshot read failed")
    actual = progress.read

    def injected(path, default=None):
        if Path(path).name == "native-progress-status.json":
            raise error
        return actual(path, default)

    monkeypatch.setattr(progress, "read", injected)
    native.record_stage(tmp_path, "scoring")
    assert candidate.read_text() == "valid result"


@pytest.mark.parametrize("code", STORAGE_ERRORS)
def test_stage_write_failure_preserves_existing_result(tmp_path, monkeypatch, caplog, code):
    progress.save(tmp_path / "native-progress-status.json", {"phase": "candidate_capture"})
    result = tmp_path / "completed-result.json"
    result.write_text(json.dumps({"status": "completed", "patch": "retained candidate"}))
    fail_observations(monkeypatch, code)
    native.record_stage(tmp_path, "scoring")
    assert json.loads(result.read_text())["status"] == "completed"
    assert any("native-progress-status.json" in item.message for item in caplog.records)


@pytest.mark.parametrize("code", STORAGE_ERRORS)
def test_real_sdk_agent_completes_with_broken_observations(tmp_path, monkeypatch, code):
    monkeypatch.setenv(native.ENV_NAME, "1")
    fail_observations(monkeypatch, code)
    model = ReplyModel()
    artifacts = tmp_path / "artifacts"

    async def scenario():
        client = OpenCollab(tmp_path, model="unused", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        return await native.guarded_agent(
            client.agent2("finish once", llm=model, artifacts=artifacts, timeout=None),
            tmp_path, artifacts, config=settings(tmp_path, artifacts / "trajectory.jsonl", 1),
        )

    result = asyncio.run(scenario())
    assert result.status == "completed"
    assert result.output == "finished"
    assert model.calls == 1
    snapshot = OpenCollab.read_session_snapshot(artifacts / "agent.json")
    assert snapshot["session_state"]["step_count"] == result.metrics["steps"]
    assert any(row.get("type") == "llm_call" for row in progress.records(artifacts / "trajectory.jsonl"))


@pytest.mark.parametrize("code", STORAGE_ERRORS)
@pytest.mark.parametrize("target", ["agent.json", "trajectory.jsonl"])
def test_real_sdk_required_writer_failure_is_preserved(tmp_path, monkeypatch, code, target):
    monkeypatch.setenv(native.ENV_NAME, "1")
    fail_observations(monkeypatch, errno.ENOSPC)
    actual_open = os.open
    storage_error = OSError(code, os.strerror(code), str(tmp_path / "artifacts" / target))
    writes = []

    def injected(path, flags, *args, **kwargs):
        name = Path(path).name
        matches = name == target or name.startswith(f".{target}.") or name == f"{target}.journal"
        if flags & (os.O_WRONLY | os.O_RDWR) and matches:
            writes.append(str(path))
            raise storage_error
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", injected)

    model = ReplyModel()
    artifacts = tmp_path / "artifacts"
    (tmp_path / "retained-workspace.txt").write_text("retained work")

    async def scenario():
        client = OpenCollab(tmp_path, model="unused", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        try:
            result = await native.guarded_agent(
                client.agent2("finish once", llm=model, artifacts=artifacts, timeout=None),
                tmp_path, artifacts, config=settings(tmp_path, artifacts / "trajectory.jsonl", 1),
            )
        except Exception as error:
            return error
        assert result.status == "failed"
        return result.error

    error = asyncio.run(scenario())
    chain = []
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert storage_error in chain
    assert writes
    assert model.calls <= 1
    assert (tmp_path / "retained-workspace.txt").read_text() == "retained work"


def test_snapshot_error_keeps_native_operation_exception_during_finalization(tmp_path, monkeypatch):
    monkeypatch.setenv(native.ENV_NAME, "1")
    fail_observations(monkeypatch, errno.EACCES)
    original = RuntimeError("native operation failed")

    async def flow(ctx, args):
        raise original

    guarded = native.guarded_workflow(flow, tmp_path, config=settings(tmp_path, tmp_path / "orchestration.jsonl"),
                                      orchestration_path=tmp_path / "orchestration.jsonl")
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(guarded(SimpleNamespace(), {}))
    assert caught.value is original


@pytest.mark.parametrize("name", ["native-progress-status.json", "native-progress-owner.json"])
@pytest.mark.parametrize("body", [b"", b'{"started_epoch":', b"null", b"[]", b'"unexpected"', b"\xff"])
def test_damaged_observation_document_leaves_healthy_workflow_running(tmp_path, monkeypatch, caplog, name, body):
    monkeypatch.setenv(native.ENV_NAME, "1")
    (tmp_path / name).write_bytes(body)
    fail_observations(monkeypatch, errno.ENOSPC)
    source = tmp_path / "orchestration.jsonl"

    async def flow(ctx, args):
        for index in range(12):
            append(source, {"type": "tool_exec", "timestamp": time.time(),
                            "payload": {"tool_call_id": str(index), "result": "done"}})
            await asyncio.sleep(0.01)
        return "finished"

    guarded = native.guarded_workflow(flow, tmp_path, config=settings(tmp_path, source), orchestration_path=source)
    assert asyncio.run(guarded(SimpleNamespace(), {})) == "finished"
    assert len(list(progress.records(source))) == 12
    assert any(name in item.message for item in caplog.records)


def test_logging_storage_failure_retains_diagnostics_and_finishes(tmp_path, monkeypatch):
    fail_observations(monkeypatch, errno.EIO)

    class FullDiskHandler(logging.Handler):
        def emit(self, record):
            raise OSError(errno.ENOSPC, "log file is full")

    handler = FullDiskHandler()
    progress.logger.addHandler(handler)
    decision = {}

    async def operation():
        await asyncio.sleep(0.03)
        return "finished"

    try:
        result = asyncio.run(progress.run_with_stop_request(
            operation(), settings(tmp_path, tmp_path / "trajectory.jsonl", 1), time.time(), decision,
        ))
    finally:
        progress.logger.removeHandler(handler)
    assert result == "finished"
    failures = decision["progress_observation_errors"]
    assert failures
    assert all(failure["errno"] == errno.EIO and failure["logging_errno"] == errno.ENOSPC
               and failure["count"] > 1 for failure in failures.values())


@pytest.mark.parametrize("guard", ["agent", "workflow"])
def test_successful_guard_retains_observation_fault_after_storage_recovers(tmp_path, monkeypatch, guard):
    monkeypatch.setenv(native.ENV_NAME, "1")
    actual = Path.open
    unavailable = [True]
    failures = []

    def injected(path, mode="r", *args, **kwargs):
        if unavailable[0] and any(flag in mode for flag in "wa+") and any(
            path.name == name or path.name.startswith(name + ".") for name in OBSERVATIONS
        ):
            failures.append(str(path))
            raise OSError(errno.ENOSPC, "observation volume is full", str(path))
        return actual(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", injected)
    artifacts = tmp_path / "artifacts"

    async def scenario():
        client = OpenCollab(tmp_path, model="unused", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        if guard == "agent":
            class RecoveringModel(ReplyModel):
                async def complete(self, *args, **kwargs):
                    result = await super().complete(*args, **kwargs)
                    unavailable[0] = False
                    return result

            return await native.guarded_agent(
                client.agent2("finish once", llm=RecoveringModel(), artifacts=artifacts, timeout=None),
                tmp_path, artifacts, config=settings(tmp_path, artifacts / "trajectory.jsonl", 1),
            )

        async def flow(ctx, args):
            await asyncio.sleep(0.03)
            unavailable[0] = False
            return {"answer": "finished"}

        guarded = native.guarded_workflow(flow, tmp_path, config=settings(tmp_path, artifacts / "orchestration.jsonl"),
                                          orchestration_path=artifacts / "orchestration.jsonl")
        return await client.workflow(guarded, {}, artifacts=artifacts, timeout=None)

    result = asyncio.run(scenario())
    assert result.status == "completed"
    assert result.output == ("finished" if guard == "agent" else {"answer": "finished"})
    assert failures
    owner = json.loads((tmp_path / "native-progress-owner.json").read_text())
    assert owner["generation_completed_epoch"] >= owner["started_epoch"]
    assert owner["progress_observation_errors"]
    assert all(failure["errno"] == errno.ENOSPC for failure in owner["progress_observation_errors"].values())


def test_damaged_stop_request_is_still_a_control_error(tmp_path):
    (tmp_path / "no-progress-stop.json").write_text("{")

    async def operation():
        await asyncio.Event().wait()

    with pytest.raises(json.JSONDecodeError):
        asyncio.run(progress.run_with_stop_request(
            operation(), settings(tmp_path, tmp_path / "trajectory.jsonl"), time.time(), {},
        ))


def test_real_trajectory_read_storage_failure_still_propagates(tmp_path, monkeypatch):
    source = tmp_path / "trajectory.jsonl"
    append(source, {"type": "tool_exec", "timestamp": time.time(),
                    "payload": {"tool_call_id": "tool", "result": "done"}})
    actual = Path.open
    error = OSError(errno.EIO, "required trajectory read failed", str(source))

    def injected(path, mode="r", *args, **kwargs):
        if path == source and "r" in mode:
            raise error
        return actual(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", injected)

    async def operation():
        await asyncio.Event().wait()

    with pytest.raises(OSError) as caught:
        asyncio.run(progress.run_with_stop_request(operation(), settings(tmp_path, source), time.time(), {}))
    assert caught.value is error
