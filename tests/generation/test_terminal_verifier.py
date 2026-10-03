"""Preparation failures, original results, and one shared scoring budget."""

from __future__ import annotations

import asyncio

import pytest

from opencollab_eval.generation import terminal_verifier as scoring
from opencollab_eval.generation.terminal_container_backend import CommandResult


class Environment:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.commands = []
        self.aborted = False

    async def ensure_quiescent(self):
        pass

    async def exec_cmd(self, command, timeout=None, stdin=None):
        self.commands.append((command, stdin))
        return CommandResult(self.exit_code)

    async def abort(self):
        self.aborted = True


@pytest.mark.parametrize("reward", [0, 1])
async def test_official_result_keeps_pass_and_semantic_failure(tmp_path, reward):
    calls = []
    raw = {"reward": reward, "exit_code": 0, "timed_out": False, "error": None, "tests": "original"}

    async def verifier(remaining):
        assert 0 < remaining <= 5
        calls.append(remaining)
        return raw

    result = await scoring.run_terminal_verifier("another-task", Environment(), tmp_path, verifier, timeout_seconds=5)

    assert result["status"] == "normal"
    assert result["reward"] == reward
    assert result["verifier"] is raw
    assert len(calls) == 1


async def test_missing_login_probe_stops_before_official_scoring(tmp_path):
    async def verifier(remaining):
        pytest.fail("official scoring must wait for provided login")

    result = await scoring.run_terminal_verifier(
        "configure-git-webserver", Environment(), tmp_path, verifier, timeout_seconds=5,
    )
    assert result["status"] == "facility_error"
    assert result["reward"] is None
    assert result["failure_phase"] == "preparation"
    assert result["retry_kind"] == "score_only"
    assert result["verifier"] is None


async def test_failed_login_does_not_become_candidate_failure(tmp_path):
    async def verifier(remaining):
        pytest.fail("failed task environment must not score")

    result = await scoring.run_terminal_verifier(
        "configure-git-webserver", Environment(exit_code=255), tmp_path, verifier,
        timeout_seconds=5, login_probe="ssh configured-host id -u",
    )
    assert result["status"] == "facility_error"
    assert result["reward"] is None
    assert "configured-host" not in str(result)


async def test_working_login_preserves_real_deployment_failure(tmp_path):
    async def verifier(remaining):
        return {"reward": 0, "exit_code": 1, "timed_out": False, "error": None, "failed": "wrong HTTP content"}

    result = await scoring.run_terminal_verifier(
        "configure-git-webserver", Environment(), tmp_path, verifier,
        timeout_seconds=5, login_probe="ssh configured-host id -u",
    )
    assert result["status"] == "normal"
    assert result["reward"] == 0
    assert result["verifier"]["failed"] == "wrong HTTP content"


async def test_preparation_and_scoring_share_the_original_budget(tmp_path, monkeypatch):
    ticks = [10.0]
    monkeypatch.setattr(scoring, "monotonic", lambda: ticks[0])

    async def preparation(*args, **kwargs):
        assert kwargs["timeout"] == 5
        ticks[0] += 2
        return {"status": "ready"}

    async def verifier(remaining):
        assert remaining == 3
        return {"reward": 1, "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr(scoring, "prepare_terminal_verifier", preparation)
    result = await scoring.run_terminal_verifier("task", Environment(), tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "normal"
    assert result["elapsed_seconds"] == 2


async def test_budget_exhausted_by_preparation_never_runs_verifier(tmp_path, monkeypatch):
    ticks = [10.0]
    monkeypatch.setattr(scoring, "monotonic", lambda: ticks[0])

    async def preparation(*args, **kwargs):
        ticks[0] += 5
        return {"status": "ready"}

    async def verifier(remaining):
        pytest.fail("preparation used the original whole budget")

    monkeypatch.setattr(scoring, "prepare_terminal_verifier", preparation)
    environment = Environment()
    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "facility_error"
    assert result["reward"] is None
    assert environment.aborted


async def test_real_timeout_aborts_the_scoring_environment(tmp_path):
    entered = asyncio.Event()

    async def verifier(remaining):
        entered.set()
        await asyncio.Event().wait()

    environment = Environment()
    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=0.05)
    assert entered.is_set()
    assert result["timed_out"] is True
    assert result["reward"] is None
    assert environment.aborted


async def test_external_cancellation_stays_cancelled(tmp_path):
    entered = asyncio.Event()

    async def verifier(remaining):
        entered.set()
        await asyncio.Event().wait()

    environment = Environment()
    task = asyncio.create_task(
        scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert environment.aborted


@pytest.mark.parametrize("raw", [
    None, [], {"reward": 1}, {"reward": True, "exit_code": 0, "timed_out": False, "error": None},
    {"reward": 1, "exit_code": 1, "timed_out": False, "error": None},
    {"reward": 0, "exit_code": 0, "timed_out": True, "error": None},
    {"reward": 0, "exit_code": 0, "timed_out": False, "error": "missing report"},
])
async def test_unusable_result_keeps_raw_evidence_and_requests_review(tmp_path, raw):
    async def verifier(remaining):
        return raw

    result = await scoring.run_terminal_verifier("task", Environment(), tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "facility_error"
    assert result["reward"] is None
    assert result["verifier"] is raw


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
async def test_invalid_budget_rejected_before_scoring(tmp_path, timeout):
    async def verifier(remaining):
        pytest.fail("invalid budget")

    with pytest.raises(ValueError):
        await scoring.run_terminal_verifier("task", Environment(), tmp_path, verifier, timeout_seconds=timeout)


async def test_late_reward_is_retained_as_evidence_instead_of_a_pass(tmp_path, monkeypatch):
    ticks = [10.0]
    monkeypatch.setattr(scoring, "monotonic", lambda: ticks[0])
    raw = {"reward": 1, "exit_code": 0, "timed_out": False, "error": None}

    async def verifier(remaining):
        ticks[0] += 6
        return raw

    environment = Environment()
    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    assert result["reward"] is None
    assert result["verifier"] is raw
    assert result["timed_out"] is True
    assert environment.aborted


async def test_preparation_failure_retains_recovery_paths(tmp_path, monkeypatch):
    receipt = {"quarantine_path": "/tmp/saved-frame", "archive_path": str(tmp_path / "frame.tar")}

    async def preparation(*args, **kwargs):
        raise scoring.VerifierPreparationError("frame_changed", "changed", receipt=receipt)

    async def verifier(remaining):
        pytest.fail("failed preparation")

    monkeypatch.setattr(scoring, "prepare_terminal_verifier", preparation)
    result = await scoring.run_terminal_verifier("task", Environment(), tmp_path, verifier, timeout_seconds=5)
    assert result["preparation"] == receipt
    assert result["reward"] is None


async def test_failed_abort_prevents_automatic_retry(tmp_path):
    environment = Environment()

    async def failed_abort():
        raise RuntimeError("command still running")

    environment.abort = failed_abort

    async def verifier(remaining):
        raise OSError("verifier transport failed")

    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "facility_error"
    assert result["cleanup_error_type"] == "RuntimeError"
    assert result["retry_kind"] is None


async def test_callback_leaving_foreground_work_cannot_produce_pass(tmp_path):
    environment = Environment()
    running = False

    async def ensure_quiescent():
        if running:
            raise RuntimeError("foreground command active")

    environment.ensure_quiescent = ensure_quiescent

    async def verifier(remaining):
        nonlocal running
        running = True
        return {"reward": 1, "exit_code": 0, "timed_out": False, "error": None}

    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "facility_error"
    assert result["reward"] is None
    assert environment.aborted


async def test_score_evidence_survives_post_verifier_quiescence_failure(tmp_path):
    environment = Environment()
    scored = False
    raw = {"reward": 0, "exit_code": 0, "timed_out": False, "error": None, "report": "original-ctrf"}

    async def ensure_quiescent():
        if scored:
            raise RuntimeError("output still being collected")

    async def verifier(remaining):
        nonlocal scored
        scored = True
        return raw

    environment.ensure_quiescent = ensure_quiescent
    result = await scoring.run_terminal_verifier("task", environment, tmp_path, verifier, timeout_seconds=5)
    assert result["status"] == "facility_error"
    assert result["verifier"] is raw
    assert result["reward"] is None


async def test_caller_owns_preparation_record_when_cancellation_cleanup_fails(tmp_path, monkeypatch):
    entered = asyncio.Event()
    receipt = {}
    environment = Environment()

    async def preparation(*args, **kwargs):
        kwargs["receipt"].update(archive_path="saved/frame.tar", quarantine_path="/tmp/saved/frame.bmp")
        entered.set()
        await asyncio.Event().wait()

    async def failed_abort():
        raise OSError("cleanup transport unavailable")

    async def verifier(remaining):
        pytest.fail("preparation is still in progress")

    monkeypatch.setattr(scoring, "prepare_terminal_verifier", preparation)
    environment.abort = failed_abort
    task = asyncio.create_task(scoring.run_terminal_verifier(
        "task", environment, tmp_path, verifier, timeout_seconds=5, preparation_receipt=receipt,
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(OSError, match="cleanup transport"):
        await task
    assert receipt == {"archive_path": "saved/frame.tar", "quarantine_path": "/tmp/saved/frame.bmp"}


async def test_caller_receipts_stay_separate_for_concurrent_cancelled_tasks(tmp_path, monkeypatch):
    entered = {name: asyncio.Event() for name in ["first", "second"]}
    receipts = {name: {} for name in entered}

    async def preparation(name, environment, artifacts, **kwargs):
        kwargs["receipt"].update(task_name=name, archive_path=f"{name}/frame.tar")
        entered[name].set()
        await asyncio.Event().wait()

    async def verifier(remaining):
        pytest.fail("preparation is still in progress")

    monkeypatch.setattr(scoring, "prepare_terminal_verifier", preparation)
    tasks = [asyncio.create_task(scoring.run_terminal_verifier(
        name, Environment(), tmp_path / name, verifier, timeout_seconds=5,
        preparation_receipt=receipts[name],
    )) for name in entered]
    await asyncio.gather(*(event.wait() for event in entered.values()))
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert receipts["first"] == {"task_name": "first", "archive_path": "first/frame.tar"}
    assert receipts["second"] == {"task_name": "second", "archive_path": "second/frame.tar"}
