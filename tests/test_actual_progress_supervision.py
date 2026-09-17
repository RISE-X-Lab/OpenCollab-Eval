from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from generation_proof_test_support import trusted_patch_proof_fields
from opencollab import OpenCollab
from opencollab.environments import local_environment

from opencollab_eval.engine import completed_progress_watch as progress
from opencollab_eval.engine import native_progress_watch as native
from opencollab_eval.generation.gen_prediction_agent import _result_metrics
from opencollab_eval.generation.gen_prediction_recovery import (
    evaluation_candidate_pair,
    publish_failed_capture_recovery,
)
from opencollab_eval.generation.gen_prediction_safe_output import append_output_records, build_output_records


def append(path, row):
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def config(root, source, seconds=0.04):
    return {
        **native.config_for(root, seconds=seconds),
        "native_workflow_sources": [str(source)],
        "progress_poll_seconds": 0.005,
    }


@pytest.mark.parametrize("event", ["keepalive", "response.created", "http_200", "request", "exception"])
def test_transport_and_logger_activity_do_not_renew_progress(tmp_path, event):
    source = tmp_path / "trajectory.jsonl"
    started = time.time() - 10
    append(source, {"event": event, "epoch": time.time(), "content_chars": 0})
    os.utime(source, None)
    probe = {"started_epoch": started}
    assert progress.observe(config(tmp_path, source, seconds=1), probe)
    assert probe["progress"]["epoch"] == started


def test_only_completed_native_rounds_and_tools_renew_progress(tmp_path):
    source = tmp_path / "trajectory.jsonl"
    started = time.time() - 10
    append(source, {"type": "llm_call_started", "timestamp": started + 1, "payload": {"model": "m"}})
    append(source, {"type": "tool_exec", "timestamp": started + 2,
                    "payload": {"tool_call_id": "incomplete"}})
    append(source, {"type": "llm_call", "timestamp": started + 3,
                    "payload": {"model": "m", "finish_reason": None, "content": "", "tool_calls": []}})
    probe = {"started_epoch": started}
    settings = config(tmp_path, source, seconds=1)
    assert progress.observe(settings, probe)
    append(source, {"type": "llm_call", "timestamp": time.time(), "step": 1,
                    "payload": {"model": "m", "finish_reason": "stop", "content": "", "tool_calls": []}})
    assert not progress.observe(settings, probe)
    assert probe["progress"]["kind"] == "model_completed"
    append(source, {"type": "tool_exec", "timestamp": time.time(),
                    "payload": {"tool_call_id": "tool-1", "result": "read succeeded"}})
    assert not progress.observe(settings, probe)
    assert probe["progress"]["phase"] == "tool"


@pytest.mark.parametrize("event", ["model_content", "tool_arguments"])
def test_partial_real_model_output_renews_progress(tmp_path, event):
    source = tmp_path / "trajectory.jsonl"
    stream = tmp_path / "model-progress.jsonl"
    started = time.time() - 10
    append(stream, {"event": event, "epoch": time.time(), "call": "request-1", "content_chars": 3})
    probe = {"started_epoch": started}
    assert not progress.observe(config(tmp_path, source, seconds=1), probe)
    assert probe["progress"]["kind"] == event


def test_shared_gateway_progress_requires_exact_task_binding(tmp_path):
    shared = tmp_path / "shared-progress.jsonl"
    settings = native.config_for(tmp_path, seconds=1, environment={
        "OPENCOLLAB_EVAL_MODEL_PROGRESS_PATH": str(shared), "OPENCOLLAB_EVAL_PROGRESS_ID": "ours",
    })
    settings["native_workflow_sources"] = []
    started = time.time() - 10
    probe = {"started_epoch": started}
    for binding in (None, "another-task"):
        append(shared, {"event": "model_content", "epoch": time.time(), "content_chars": 1,
                        "progress_id": binding})
    assert progress.observe(settings, probe)
    append(shared, {"event": "model_content", "epoch": time.time(), "content_chars": 1,
                    "progress_id": "ours"})
    assert not progress.observe(settings, probe)


def test_proven_provider_failure_wait_does_not_reset_progress_or_other_calls(tmp_path):
    stream = tmp_path / "model-progress.jsonl"
    settings = config(tmp_path, tmp_path / "trajectory.jsonl", seconds=1)
    started = time.time() - 10
    append(stream, {"event": "provider_error", "epoch": time.time(), "call": "failed-call",
                    "original_status": 429, "error_type": "rate_limit", "error_code": "rate_limit_exceeded"})
    probe = {"started_epoch": started}
    assert progress.observe(settings, probe)
    assert probe["provider_fault_waits"]["failed-call"]["http_status"] == 429
    append(stream, {"event": "model_content", "epoch": time.time(), "call": "another-call", "content_chars": 1})
    assert not progress.observe(settings, probe)
    assert "failed-call" in probe["provider_fault_waits"]
    append(stream, {"event": "model_content", "epoch": time.time(), "call": "failed-call", "content_chars": 1})
    assert not progress.observe(settings, probe)
    assert not probe["provider_fault_waits"]


def test_idle_stops_without_an_outer_stop_request(tmp_path):
    async def scenario():
        finalized = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        decision = {}
        with pytest.raises(progress.EvaluationNoProgressTimeout):
            await progress.run_with_stop_request(
                operation(), config(tmp_path, tmp_path / "trajectory.jsonl"), time.time(), decision,
            )
        assert finalized.is_set()
        assert decision["reason"] == "evaluation_no_progress"
        assert not (tmp_path / "no-progress-stop.json").exists()

    asyncio.run(scenario())


def test_real_progress_continues_beyond_the_previous_wall_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(native.ENV_NAME, "0.04")
    monkeypatch.delenv(native.WALL_ENV_NAME, raising=False)
    source = tmp_path / "trajectory.jsonl"
    stream = tmp_path / "model-progress.jsonl"

    async def operation():
        for _ in range(12):
            append(stream, {"event": "model_content", "epoch": time.time(), "content_chars": 1})
            await asyncio.sleep(0.01)
        return "finished"

    started = time.time()
    result = asyncio.run(progress.run_with_stop_request(operation(), config(tmp_path, source), started, {}))
    assert result == "finished"
    assert time.time() - started > 0.05
    assert native.generation_wall_timeout(0.05) is None
    assert native.controller_wall_timeout(0.05) is None
    assert native.generation_wall_timeout(0.05, {native.ENV_NAME: "1", native.WALL_ENV_NAME: "2"}) == 2
    assert native.controller_wall_timeout(0.05, {native.ENV_NAME: "1", native.CONTROLLER_WALL_ENV_NAME: "3"}) == 3
    assert native.generation_wall_timeout(0.05, {}) == 0.05


def test_outer_generation_wait_does_not_reapply_the_old_wall_limit(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        args = ["generator"]
        observed_timeout = "unset"

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.observed_timeout = timeout
            return 0

    identity = {"pid": 123, "born": "1"}
    progress.save(tmp_path / "native-progress-owner.json",
                  {"generation_identity": identity, "started_epoch": time.time()})
    monkeypatch.setattr(progress, "same_process", lambda value: value == identity)
    monkeypatch.setattr(progress, "monitor", lambda *args, **kwargs: {"phase": "candidate_capture"})
    process = Process()
    assert native.wait_generation(process, tmp_path, wall_timeout=0.01,
                                  environment={native.ENV_NAME: "1"}) == 0
    assert process.observed_timeout is None


def test_generation_duration_ends_before_candidate_capture_and_scoring(tmp_path, monkeypatch):
    record = {"generation_identity": {"pid": 123}, "started_epoch": 10}
    progress.save(tmp_path / "native-progress-owner.json",
                  {**record, "generation_completed_epoch": 20, "phase": "candidate_capture"})
    monkeypatch.setattr(progress, "same_process", lambda _: True)
    monkeypatch.setattr(progress.time, "time", lambda: 1000)
    result = progress.monitor(native.config_for(tmp_path, seconds=1), record, tmp_path / "status.json")
    assert result["phase"] == "candidate_capture"
    assert result["generation_duration_seconds"] == 10
    assert not (tmp_path / "no-progress-stop.json").exists()


def test_public_agent_cancellation_preserves_full_native_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv(native.ENV_NAME, "0.04")

    class WaitingModel:
        supports_response_session_identity = True

        def context_window(self):
            return 32768

        async def complete(self, *args, **kwargs):
            await asyncio.Event().wait()

        async def close(self):
            pass

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    async def scenario():
        client = OpenCollab(tmp_path, model="model", provider="openai", api_key="test",
                            environment=local_environment(str(tmp_path)))
        return await native.guarded_agent(
            client.agent2("repair the project", llm=WaitingModel(), artifacts=artifacts, timeout=None),
            tmp_path, artifacts, config=config(tmp_path, artifacts / "trajectory.jsonl"),
        )

    result = asyncio.run(scenario())
    snapshot = OpenCollab.read_session_snapshot(artifacts / "agent.json")
    assert snapshot["session_state"]["step_count"] == result.metrics["steps"]
    assert any("repair the project" in str(message.get("content")) for message in snapshot["messages"])
    metrics = _result_metrics(result)
    assert metrics["candidate_probe_eligible"] is True
    assert metrics["submission_eligible"] is False
    assert metrics["failure_origin"] == "evaluation_no_progress"
    assert metrics["technical_interruption_recoverable"] is True
    assert metrics["oc_failure"] is False
    assert metrics["resume_snapshot"] == str(artifacts / "agent.json")


def test_paused_candidate_uses_existing_public_recovery_and_eligibility(tmp_path):
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    metrics = {
        **trusted_patch_proof_fields(patch), "workflow_status": "error", "agent_status": "stopped",
        "failure_origin": "evaluation_no_progress", "error_type": "EvaluationNoProgressTimeout",
        "error": "idle", "technical_interruption_recoverable": True, "oc_failure": False,
        "submission_eligible": False, "session_quiesced": True, "execution_quiesced": True,
        "container_execution_quiesced": True, "container_cleanup_succeeded": True,
        "patch_extraction_succeeded": True, "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True, "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True, "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True, "patch_produced": True, "runtime_tree_sha256": "1" * 64,
    }
    prediction, metric = build_output_records(instance_id="task-1", model_name="model", patch=patch, metrics=metrics)
    pp, mp = tmp_path / "predictions.jsonl", tmp_path / "metrics.jsonl"
    append_output_records(pp, mp, prediction, metric)
    recovered = publish_failed_capture_recovery(
        run_dir=tmp_path, source_predictions_path=pp, source_metrics_path=mp, prediction=prediction, metric=metric,
        capture_metrics=metrics, expected_base_commit="e" * 40, expected_runtime_tree_sha256="1" * 64, cid="2" * 64,
    )
    selected = evaluation_candidate_pair(tmp_path, prediction, metric)
    assert selected is not None
    assert selected[0]["model_patch"] == patch
    assert selected[1]["submission_eligible"] is True
    assert selected[1]["failure_origin"] == "evaluation_no_progress"
    assert selected[1]["oc_failure"] is False
    assert recovered["model_calls"] == 0


def test_progress_controller_communication_has_no_cumulative_deadline():
    from opencollab_eval.commands.swe_v1_prolite_process import _bounded_remote_communicate

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.05); print('done')"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    polls = []
    timeout = native.controller_wall_timeout(0.01, {native.ENV_NAME: "1"})
    stdout, stderr = _bounded_remote_communicate(proc, "", timeout=timeout, poll_interval=0.01,
                                                poll_callback=lambda: polls.append(True))
    assert stdout.strip() == "done"
    assert stderr == ""
    assert polls


def test_local_runner_exceeds_old_controller_wall_and_returns(tmp_path, monkeypatch):
    from opencollab_eval.commands import swe_local_runner_transport as transport

    command = [sys.executable, "-c", "import time; time.sleep(.05); print('{\"status\": \"done\"}')"]
    monkeypatch.setattr(transport, "local_runner_command", lambda *args: (command, dict(os.environ)))
    args = SimpleNamespace(total_timeout=0.01, base_run_dir=str(tmp_path))
    started = time.monotonic()
    result = transport.run_local_runner(
        args, owner_nonce="1" * 32, payload={"workflow_env": {native.ENV_NAME: "1"}},
        runtime_summary={}, proxy_summary={},
    )
    assert time.monotonic() - started > args.total_timeout
    assert result["status"] == "done"
    assert result["runner_transport"] == "local"


def test_journal_only_role_snapshot_replays_state_through_public_api(tmp_path):
    from opencollab_eval.generation.gen_prediction_workflow_state import _role_states

    path = tmp_path / "0_coder.json.journal"
    append(path, {"journal_version": 1, "sequence": 1, "replace_from": 0, "message_count": 1,
                  "messages": [{"role": "user", "content": "retained context"}],
                  "meta": {"role": "coder", "session_state": {
                      "phase": "stopped", "step_count": 9, "used_tokens": 42, "terminal_reason": "idle"}},
                  "seen_result_hashes_reset": False, "seen_result_hashes_added": []})
    roles, errors = _role_states(str(tmp_path / "orchestration.jsonl"))
    assert not errors
    assert roles[0]["steps"] == 9
    assert roles[0]["used_tokens"] == 42
    snapshot = OpenCollab.read_session_snapshot(path.with_suffix(""))
    assert snapshot["messages"][0]["content"] == "retained context"
