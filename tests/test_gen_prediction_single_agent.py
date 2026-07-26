from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab import RunError as AgentRunLifecycleError
from opencollab import RunResult as AgentRunResult

from opencollab_eval.engine.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    latest_paired_rows,
    metric_submission_integrity,
)

gp = pytest.importorskip("opencollab_eval.generation.gen_prediction")


def test_single_agent_output_records_share_identity():
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"

    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-1",
        workflow_name="single-agent",
    )

    assert prediction["record_id"] == "record-1"
    assert metric["record_id"] == "record-1"
    assert prediction["patch_sha256"] == gp._patch_sha256(patch)
    assert metric["patch_sha256"] == prediction["patch_sha256"]
    assert metric["workflow_status"] == "done"
    assert metric["runner_returncode"] == 0
    assert prediction["workflow_metric"]["runner_returncode"] == 0
    assert prediction["workflow"] == metric["workflow"] == "single-agent"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("done", 0), ("done_with_timeout_patch", 124), ("error", 1)],
)
def test_output_records_write_runner_returncode_for_status(status, expected):
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="+fixed",
        metrics={"workflow_status": status},
    )

    assert metric["runner_returncode"] == expected
    assert prediction["workflow_metric"]["runner_returncode"] == expected


def test_current_single_agent_integrity_fields_are_proven():
    patch = "+fixed"
    metrics = {
        "workflow_status": "done",
        "execution_quiesced": True,
        "submission_eligible": True,
    }

    gp.complete_single_agent_integrity(
        metrics,
        patch=patch,
        patch_extraction_succeeded=True,
    )
    metrics["patch_produced"] = True

    assert metric_submission_integrity(metrics) != SUBMISSION_INTEGRITY_PROVEN


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("done", "empty_patch_after_done"),
        ("done_with_timeout_patch", "done_with_timeout_patch"),
    ],
)
def test_zero_byte_trusted_extraction_preserves_timeout_failure(status, expected):
    metrics = {"workflow_status": status, "submission_eligible": True}

    gp.normalize_trusted_extraction_status(metrics, "")

    assert metrics["workflow_status"] == expected
    assert metrics["submission_eligible"] is False


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("done", 1), ("done_with_timeout_patch", 1), ("done_with_timeout_patch", 0)],
)
def test_output_records_reject_conflicting_runner_returncode(status, returncode):
    with pytest.raises(ValueError, match="conflicts with workflow_status"):
        gp.build_output_records(
            instance_id="task-1",
            model_name="model",
            patch="+fixed",
            metrics={"workflow_status": status, "runner_returncode": returncode},
        )


@pytest.mark.parametrize("returncode", [True, False, "0", 0.0, None])
def test_output_records_reject_invalid_existing_runner_returncode(returncode):
    with pytest.raises(ValueError, match="non-boolean integer"):
        gp.build_output_records(
            instance_id="task-1",
            model_name="model",
            patch="+fixed",
            metrics={"workflow_status": "done", "runner_returncode": returncode},
        )


def test_single_agent_cli_accepts_metrics_argument():
    result = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.generation.gen_prediction", "--help"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "--metrics" in result.stdout


class RecordingRuntime:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.requests = []

    async def agent(self, prompt, **kwargs):
        request = SimpleNamespace(prompt=prompt, **kwargs)
        self.requests.append(request)
        assert request.artifacts is not None
        assert list(request.artifacts.iterdir()) == []
        if self.error is not None:
            raise self.error
        return self.result


def _runtime_result(
    *,
    outcome="completed",
    phase="done",
    error_type=None,
    error_message=None,
    tokens_spent=10,
    step_count=1,
    cleanup_quiesced=True,
):
    status = {
        "completed": "completed",
        "timed_out": "stopped",
        "failed": "failed",
    }[outcome]
    if phase in {"budget_exceeded", "step_limit_exceeded", "context_overflow"}:
        status = "stopped"
    error = None
    if error_message:
        error = (
            TimeoutError(error_message)
            if error_type == "TimeoutError"
            else RuntimeError(error_message)
        )
    return AgentRunResult(
        output="result" if outcome == "completed" else None,
        status=status,
        reason=("timeout" if outcome == "timed_out" else phase if status == "stopped" else None),
        tokens=tokens_spent,
        error=error,
        metrics={
            "phase": phase,
            "steps": step_count,
            "session_quiesced": cleanup_quiesced,
            "execution_quiesced": None,
        },
    )


def _reserve_empty_artifact_dir(monkeypatch, tmp_path):
    artifact_dir = tmp_path / "agent-artifacts"

    def reserve(_root):
        assert Path(_root) == tmp_path
        artifact_dir.mkdir()
        return str(artifact_dir)

    monkeypatch.setattr(gp.gen_prediction_agent, "reserve_run_directory", reserve)
    return artifact_dir


def _agent_config():
    return {
        "model": "model",
        "provider": "provider",
        "api_key": "key",
        "base_url": "http://local",
    }


def test_single_agent_sealed_fields_do_not_reach_runtime_request(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(_runtime_result())
    sealed_values = (
        "private-instance-id",
        "private-base-commit",
        "tests/private_target.py::test_secret",
        "private test patch",
        "private reference patch",
    )
    prompt = gp.build_task(
        {
            "repo": "owner/repo",
            "problem_statement": "Fix the public behavior.",
            "instance_id": sealed_values[0],
            "base_commit": sealed_values[1],
            "FAIL_TO_PASS": [sealed_values[2]],
            "test_patch": sealed_values[3],
            "reference_patch": sealed_values[4],
        }
    )

    asyncio.run(
        gp.run_agent(
            prompt,
            "cid",
            _agent_config(),
            4,
            100,
            1,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    assert "owner/repo" in prompt
    assert "Fix the public behavior." in prompt
    assert runtime.requests[0].prompt == prompt
    assert all(secret not in prompt for secret in sealed_values)


def test_single_agent_builds_stable_runtime_request(monkeypatch, tmp_path):
    artifact_dir = _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(_runtime_result())
    config = {
        **_agent_config(),
        "llm_timeout": 45.0,
        "temperature": 0.7,
        "top_p": 0.8,
        "max_output_tokens": 2048,
        "thinking": True,
        "thinking_params": {"enable_thinking": True},
    }

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            config,
            4,
            100,
            12.5,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    request = runtime.requests[0]
    assert request.prompt == "task"
    assert request.name == "swe_agent"
    assert request.system_prompt == gp.AGENT_PROMPT.strip()
    assert request.trace is True
    assert request.artifacts == artifact_dir
    assert request.max_steps == 4
    assert request.budget == 100
    assert request.timeout == 12.5
    assert request.cleanup_timeout == gp.AGENT_CANCELLATION_GRACE_SECONDS
    assert [type(tool).__name__ for tool in request.tools] == [
        "BashTool",
        "FileReadTool",
        "FileWriteTool",
        "GrepTool",
    ]
    assert metrics["workflow_status"] == "done"


def test_single_agent_reports_real_terminal_phase(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            outcome="failed",
            phase="budget_exceeded",
            tokens_spent=100,
            step_count=4,
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 4, 100, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["workflow_status"] == "budget_exceeded"
    assert metrics["session_phase"] == "budget_exceeded"
    assert metrics["step_count"] == 4
    assert metrics["used_tokens"] == 100
    assert metrics["wall_clock_timeout"] is False


def test_single_agent_does_not_relabel_provider_timeout(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            outcome="failed",
            phase="calling_llm",
            error_type="TimeoutError",
            error_message="provider timeout",
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 4, 100, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["workflow_status"] == "error"
    assert metrics["wall_clock_timeout"] is False
    assert metrics["error_type"] == "TimeoutError"
    assert metrics["error"] == "provider timeout"


def test_single_agent_timeout_preserves_partial_patch_metrics(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            outcome="timed_out",
            phase="cancelled",
            tokens_spent=321,
            step_count=7,
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            40,
            1000,
            0.01,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    assert metrics["workflow_status"] == "done_with_timeout_patch"
    assert metrics["wall_clock_timeout"] is True
    assert metrics["session_quiesced"] is True
    assert metrics["execution_quiesced"] is False
    assert metrics["candidate_probe_eligible"] is True
    assert metrics["submission_eligible"] is False
    assert metrics["session_phase"] == "cancelled"
    assert metrics["step_count"] == 7
    assert metrics["used_tokens"] == 321


def test_single_agent_rejects_non_quiescent_runtime_result(monkeypatch, tmp_path):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            outcome="timed_out",
            phase="cancelled",
            cleanup_quiesced=False,
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task",
            "cid",
            _agent_config(),
            4,
            100,
            0.05,
            artifact_root=tmp_path,
            runtime=runtime,
        )
    )

    assert metrics["execution_quiesced"] is False
    assert metrics["candidate_probe_eligible"] is False
    assert metrics["submission_eligible"] is False
    assert metrics["workflow_status"] == "error"
    assert metrics["error_type"] == "SessionNotQuiesced"
    assert gp.metrics_have_completed_identity(metrics, "+candidate") is False


def test_single_agent_failed_done_phase_cannot_become_submission_eligible(
    monkeypatch, tmp_path
):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(
            outcome="failed",
            phase="done",
            error_type="RuntimeError",
            error_message="completion evidence failed",
        )
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 4, 100, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["workflow_status"] == "error"
    assert metrics["session_quiesced"] is True
    assert metrics["execution_quiesced"] is False
    assert metrics["candidate_probe_eligible"] is False
    assert metrics["submission_eligible"] is False
    assert metrics["error_type"] == "RuntimeError"


@pytest.mark.parametrize(
    "error",
    [
        AgentRunLifecycleError("agent evidence incomplete"),
        RuntimeError("runtime unavailable"),
    ],
)
def test_single_agent_runtime_call_failures_are_ineligible(monkeypatch, tmp_path, error):
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(error=error)

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 4, 100, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["workflow_status"] == "error"
    assert metrics["session_phase"] == "error"
    assert metrics["execution_quiesced"] is False
    assert metrics["submission_eligible"] is False
    assert metrics["error_type"] == type(error).__name__
    assert metrics["error"] == str(error)


def test_docker_timeout_accepts_positive_float(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "2.5")

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)

    gp._docker("ps")

    assert captured["timeout"] == 2.5


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "nan", "inf"])
def test_docker_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", value)

    with pytest.raises(ValueError, match="must be a positive number"):
        gp._docker("ps")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 0),
        ("max_steps", -1),
        ("max_steps", 1.5),
        ("max_steps", True),
        ("budget", 0),
        ("budget", -1),
        ("budget", "10"),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", float("nan")),
        ("timeout", float("inf")),
        ("timeout", True),
    ],
)
def test_generation_limits_reject_invalid_values(field, value):
    values = {"max_steps": 10, "budget": 100, "timeout": 5.0}
    values[field] = value

    with pytest.raises(ValueError, match=rf"--{field.replace('_', '-')}"):
        gp.validate_generation_limits(**values)


def test_generation_limits_normalize_valid_values():
    assert gp.validate_generation_limits(max_steps=4, budget=100, timeout="2.5") == (
        4,
        100,
        2.5,
    )


def test_default_metrics_path_matches_status_layout(tmp_path):
    output = tmp_path / "predictions.jsonl"

    assert gp.default_metrics_path(output) == tmp_path / "metrics.jsonl"


def test_output_paths_reject_same_file_before_generation(tmp_path):
    output = tmp_path / "records.jsonl"

    with pytest.raises(ValueError, match="must use different files"):
        gp.output_paths(output, output)

    assert output.exists() is False


def test_output_paths_reject_hard_links_to_same_inode(tmp_path):
    predictions = tmp_path / "predictions.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    predictions.write_text("", encoding="utf-8")
    os.link(predictions, metrics)

    with pytest.raises(ValueError, match="must use different files"):
        gp.output_paths(predictions, metrics)

    with pytest.raises(ValueError, match="must use different files"):
        gp.append_output_records(
            predictions,
            metrics,
            {"instance_id": "task", "model_patch": "+x"},
            {"instance_id": "task", "workflow_status": "done"},
        )

    assert predictions.read_text(encoding="utf-8") == ""


def test_output_paths_reject_symlink_before_generation(tmp_path):
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    output = tmp_path / "predictions.jsonl"
    output.symlink_to(victim)

    with pytest.raises(ValueError, match="regular file or absent"):
        gp.output_paths(output, tmp_path / "metrics.jsonl")

    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_output_paths_return_absolute_targets_for_relative_arguments(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)

    predictions, metrics = gp.output_paths(
        Path("results/predictions.jsonl"),
        Path("results/metrics.jsonl"),
    )

    assert predictions == tmp_path / "results" / "predictions.jsonl"
    assert metrics == tmp_path / "results" / "metrics.jsonl"
    assert predictions.is_absolute()
    assert metrics.is_absolute()


def test_pending_staging_rejects_symlink_target_before_owner_state_change(
    tmp_path,
):
    gp.write_container_marker(tmp_path, "cid", "name")
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.symlink_to(victim)
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="diff --git a/a b/a\n+fixed\n",
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )

    with pytest.raises(ValueError, match="regular file or absent"):
        gp.persist_pending_output(
            run_dir=tmp_path,
            predictions_path=predictions,
            metrics_path=tmp_path / "metrics.jsonl",
            prediction=prediction,
            metric=metric,
            cid="cid",
            name="name",
        )

    owner = gp._read_owner(gp.container_owner_path(tmp_path, "name"))
    assert owner is not None and owner["state"] == "active"
    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_cli_runner_bounds_shutdown_of_task_that_refuses_cancellation():
    script = r'''
import asyncio
from opencollab_eval.engine.async_runtime import run_with_bounded_shutdown

async def stubborn_task():
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue

async def scenario():
    asyncio.create_task(stubborn_task())
    await asyncio.sleep(0)

run_with_bounded_shutdown(scenario(), shutdown_timeout=0.01)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    assert "missed the shutdown deadline" in completed.stderr


def test_cli_runner_cancels_task_spawned_during_shutdown_cleanup():
    child_cancelled = []

    async def scenario():
        async def stubborn_child():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.append(True)
                raise

        async def parent():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                asyncio.create_task(stubborn_child())

        asyncio.create_task(parent())
        await asyncio.sleep(0)
        return "completed"

    result = gp.run_with_bounded_shutdown(
        scenario(),
        shutdown_timeout=0.01,
    )

    assert result == "completed"
    assert child_cancelled == [True]


def test_output_commit_recovers_when_metrics_projection_crashes(monkeypatch, tmp_path):
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="diff --git a/a b/a\n+x\n",
        metrics={"workflow_status": "done"},
        record_id="r1",
    )
    prediction_path = tmp_path / "predictions.jsonl"
    metrics_path = tmp_path / "metrics.jsonl"
    original_append = gp._append_jsonl_durable
    calls = []

    def crash_before_metric_projection(path, row):
        calls.append(path.name)
        if path == metrics_path:
            raise RuntimeError("simulated process crash")
        original_append(path, row)

    monkeypatch.setattr(gp, "_append_jsonl_durable", crash_before_metric_projection)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        gp.append_output_records(prediction_path, metrics_path, prediction, metric)

    assert calls == ["predictions.jsonl", "metrics.jsonl"]
    assert not metrics_path.exists()
    predictions = [json.loads(line) for line in prediction_path.read_text().splitlines()]
    pair = latest_paired_rows(predictions, [], "task-1")

    assert pair.status == "embedded_metric"
    assert pair.prediction["record_id"] == "r1"
    assert pair.metric == metric


def test_durable_append_separates_preexisting_truncated_tail(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_bytes(b'{"instance_id":"truncated"')
    row = {"instance_id": "task-1", "model_patch": "+fixed"}

    gp._append_jsonl_durable(path, row)

    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"instance_id":"truncated"',
        json.dumps(row),
    ]
    valid_rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            valid_rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert valid_rows == [row]
